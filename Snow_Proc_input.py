import os
import sys
import csv
import tkinter as tk
from tkinter import ttk, messagebox

try:
    import snowflake.connector
except ImportError:
    snowflake = None
else:
    snowflake = snowflake.connector


OUTPUT_PATH = r"C:\\Nilesh\\Snow_Procedure\\Input_File_new.csv"


def get_table_columns(cursor, schema_name, table_name):
    """Return ordered list of (column_name, data_type) for a Snowflake table."""

    query = (
        "SELECT COLUMN_NAME, DATA_TYPE "
        "FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
        "ORDER BY ORDINAL_POSITION"
    )
    cursor.execute(query, (schema_name.upper(), table_name.upper()))
    return cursor.fetchall()


def build_mappings(final_cols, stage_cols, sequence_name, winsert_source_value):
    """Build list of (final_col, stage_expression, data_type) rows.

    final_cols: list of (name, data_type) from final table.
    stage_cols: set of column names from stage table.

    sequence_name: value entered for the sequence, used as
      ``<sequence_name>.NEXTVAL`` for ROW_WID / RECORD_ID.
    winsert_source_value: value for W_INSERT_SOURCE, used as
      a quoted literal like ``'<value>'`` for W_INSERT_SOURCE/UPDATE_SOURCE.
    """

    stage_set = {name.upper() for name in stage_cols}
    rows = []

    for col_name, data_type in final_cols:
        col_upper = col_name.upper()

        # Default mapping: same name if it exists in stage table
        stage_expr = ""

        if col_upper in {"ROW_WID", "RECORD_ID"}:
            # Use actual sequence value with .NEXTVAL
            if sequence_name:
                stage_expr = f"{sequence_name}.NEXTVAL"
        elif col_upper == "W_UPDATE_DT":
            stage_expr = "W_INSERT_DT"
        elif col_upper == "W_UPDATE_DATE":
            stage_expr = "W_INSERT_DATE"
        elif col_upper in {"W_INSERT_SOURCE", "W_UPDATE_SOURCE"}:
            # Use W_INSERT_SOURCE value as quoted literal
            if winsert_source_value:
                stage_expr = f"'{winsert_source_value}'"
        elif col_upper in stage_set:
            stage_expr = col_name

        rows.append((col_name, stage_expr, data_type))

    return rows


def write_csv(params, mappings, output_path):
    """Write the parameter section and column mappings to CSV."""

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    with open(output_path, mode="w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        # Parameter section
        writer.writerow(["Parameter", "Value", ""])
        for key, value in params.items():
            # Do not include Sequence_Name and W_INSERT_SOURCE rows
            if key in {"Sequence_Name", "W_INSERT_SOURCE"}:
                continue
            writer.writerow([key, value, ""])

        # Blank separator row
        writer.writerow(["", "", ""])

        # Column mapping header
        writer.writerow(["Final_Table_column", "Stage_Table_Column", "Data_Type"])

        # Column mapping rows
        for final_col, stage_col, data_type in mappings:
            # Normalize TEXT data type to VARCHAR in the output file
            normalized_type = data_type
            if isinstance(data_type, str) and data_type.strip().upper() == "TEXT":
                normalized_type = "VARCHAR"

            writer.writerow([final_col, stage_col, normalized_type])


def run_main_logic(form_values):
    if snowflake is None:
        messagebox.showerror(
            "Missing dependency",
            "snowflake-connector-python is not installed.\n"
            "Please install it (e.g. 'pip install snowflake-connector-python') and run again.",
        )
        return

    procedure_name = form_values["Procedure_name"].strip()
    if not procedure_name:
        messagebox.showerror("Validation error", "Procedure_name is required.")
        return
    if len(procedure_name) > 30:
        messagebox.showerror("Validation error", "Procedure_name must be at most 30 characters.")
        return

    truncate_load = form_values["Truncate_load"].strip().upper()
    if truncate_load not in {"TRUE", "FALSE"}:
        messagebox.showerror("Validation error", "Truncate_load must be TRUE or FALSE.")
        return

    final_schema = form_values["Final Table Schema"].strip() or "GCSCDNA_BASE"
    final_table = form_values["Table_Name"].strip()
    stage_schema = form_values["Stage_schema"].strip()
    stage_table = form_values["Stage_Table_Name"].strip()
    sequence_name = form_values["Sequence_Name"].strip() or "Sequence_Name"
    winsert_source = form_values["W_INSERT_SOURCE"].strip() or "W_INSERT_SOURCE"
    sf_user = form_values.get("Snowflake_User", "").strip() or "np0279"

    if not final_table or not stage_schema or not stage_table:
        messagebox.showerror(
            "Validation error",
            "Table_Name, Stage_schema, and Stage_Table_Name are required.",
        )
        return

    # Prepare parameters dictionary in the order requested
    params = {
        "Procedure_name": procedure_name,
        "Truncate_load": truncate_load,
        "Final Table Schema": final_schema,
        "Table_Name": final_table,
        "Stage_schema": stage_schema,
        "Stage_Table_Name": stage_table,
        "Sequence_Name": sequence_name,
        "W_INSERT_SOURCE": winsert_source,
    }

    if not sf_user:
        messagebox.showerror(
            "Validation error",
            "Snowflake User is required (e.g. your ATT ID or email).",
        )
        return

    try:
        conn = snowflake.connect(
            account="gscdna.east-us-2.privatelink",
            user=sf_user,
            authenticator="externalbrowser",
            warehouse="GCSCDNA_DEV_BASE_WH",
            database="GCSCDNA_DEV",
            role="GCSCDNA_DEV_SYSADMIN",
        )
    except Exception as exc:
        messagebox.showerror("Connection error", f"Failed to connect to Snowflake:\n{exc}")
        return

    try:
        with conn.cursor() as cur:
            # Final table columns
            final_columns = get_table_columns(cur, final_schema, final_table)
            if not final_columns:
                raise RuntimeError(
                    f"No columns found for final table {final_schema}.{final_table}."
                )

            # Stage table columns (only need names)
            stage_columns_raw = get_table_columns(cur, stage_schema, stage_table)
            if not stage_columns_raw:
                raise RuntimeError(
                    f"No columns found for stage table {stage_schema}.{stage_table}."
                )
            stage_column_names = [name for name, _ in stage_columns_raw]

        mappings = build_mappings(
            final_columns,
            stage_column_names,
            sequence_name=sequence_name,
            winsert_source_value=winsert_source,
        )

        write_csv(params, mappings, OUTPUT_PATH)
    except Exception as exc:
        messagebox.showerror("Error", f"Failed to generate CSV:\n{exc}")
        return
    finally:
        try:
            conn.close()
        except Exception:
            pass

    messagebox.showinfo(
        "Success",
        f"Input file generated successfully at:\n{OUTPUT_PATH}",
    )


class InputForm(tk.Toplevel):
    def __init__(self, master=None):
        super().__init__(master)
        self.title("Snow Procedure Input")
        self.resizable(False, False)

        self.result = None

        self.vars = {
            "Procedure_name": tk.StringVar(),
            "Truncate_load": tk.StringVar(value="FALSE"),
            "Final Table Schema": tk.StringVar(value="GCSCDNA_BASE"),
            "Table_Name": tk.StringVar(),
            "Stage_schema": tk.StringVar(),
            "Stage_Table_Name": tk.StringVar(),
            "Sequence_Name": tk.StringVar(),
            "W_INSERT_SOURCE": tk.StringVar(value="SCM"),
            "Snowflake_User": tk.StringVar(value="np0279"),
        }

        self._build_widgets()

    def _build_widgets(self):
        fields = [
            ("Snowflake_User", "Snowflake User"),
            ("Procedure_name", "Procedure Name"),
            ("Truncate_load", "Truncate Load (TRUE/FALSE)"),
            ("Final Table Schema", "Final Table Schema"),
            ("Table_Name", "Final Table Name"),
            ("Stage_schema", "Stage Schema"),
            ("Stage_Table_Name", "Stage Table Name"),
            ("Sequence_Name", "Sequence Name"),
            ("W_INSERT_SOURCE", "W_INSERT_SOURCE"),
        ]

        for row, (key, label_text) in enumerate(fields):
            label = ttk.Label(self, text=label_text + ":")
            label.grid(row=row, column=0, sticky=tk.W, padx=8, pady=4)

            if key == "Truncate_load":
                combo = ttk.Combobox(
                    self,
                    textvariable=self.vars[key],
                    values=["TRUE", "FALSE"],
                    state="readonly",
                    width=20,
                )
                combo.grid(row=row, column=1, sticky=tk.W, padx=8, pady=4)
            else:
                entry = ttk.Entry(self, textvariable=self.vars[key], width=30)
                entry.grid(row=row, column=1, sticky=tk.W, padx=8, pady=4)

        button_frame = ttk.Frame(self)
        button_frame.grid(row=len(fields), column=0, columnspan=2, pady=10)

        ok_btn = ttk.Button(button_frame, text="Generate", command=self.on_ok)
        ok_btn.grid(row=0, column=0, padx=5)

        cancel_btn = ttk.Button(button_frame, text="Cancel", command=self.on_cancel)
        cancel_btn.grid(row=0, column=1, padx=5)

        self.bind("<Return>", lambda event: self.on_ok())
        self.bind("<Escape>", lambda event: self.on_cancel())

    def on_ok(self):
        self.result = {key: var.get() for key, var in self.vars.items()}
        self.destroy()

    def on_cancel(self):
        self.result = None
        self.destroy()


def main():
    root = tk.Tk()
    root.withdraw()

    form = InputForm(master=root)
    form.grab_set()
    root.wait_window(form)

    if not form.result:
        sys.exit(0)

    run_main_logic(form.result)


if __name__ == "__main__":
    main()
