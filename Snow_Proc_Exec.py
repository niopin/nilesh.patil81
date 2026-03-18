import csv
import os


def _show_popup(message: str, title: str = "Info") -> None:
    """Show a simple popup message; fall back to print if GUI unavailable."""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(title, message)
        root.destroy()
    except Exception:
        # If tkinter or a display is not available, just print the message
        print(message)


def read_csv(file_path):
    """Read input CSV and return a data dictionary used to build the procedure."""
    data = {}
    columns = []

    with open(file_path, mode="r", encoding="utf-8") as file:
        csv_reader = csv.reader(file)
        for row in csv_reader:
            if not row:
                continue

            # Parameter section
            if "Procedure_name" in row:
                data["Procedure_name"] = row[1]
            elif "Truncate_load" in row:
                data["Truncate_load"] = row[1].strip().lower()
            elif "Final Table Schema" in row:
                data["Final_Table_Schema"] = row[1]
            elif "Table_Name" in row:
                data["Table_Name"] = row[1]
            elif "Stage_schema" in row:
                data["Stage_Schema"] = row[1]
            elif "Stage_Table_Name" in row:
                data["Stage_Table_Name"] = row[1]
            # Header for column mapping section
            elif "Final_Table_column" in row:
                columns = row
                data["Final_Table_columns"] = []
            else:
                # Data rows for column mappings
                if columns and any(row):
                    data.setdefault("Final_Table_columns", []).append(row)

    return data


def _is_stage_column_reference(value, stage_column_set):
    """Return True if value should be treated as a STG column reference.

    We treat simple identifiers that match a stage column name as STG columns.
    Literals (e.g. 'SCM') and expressions (e.g. SEQ.NEXTVAL) are returned as-is.
    """
    v = (value or "").strip()
    if not v:
        return False

    # Quoted literal
    if v.startswith("'") and v.endswith("'"):
        return False

    # Expressions with dot or parentheses (e.g. SEQ.NEXTVAL, FUNC(col))
    if "." in v or "(" in v or ")" in v:
        return False

    return v in stage_column_set


def create_snowflake_procedure(data):
    try:
        procedure_name = data["Procedure_name"]
        truncate_load = data["Truncate_load"]
        final_table_schema = data["Final_Table_Schema"]
        table_name = data["Table_Name"]
        table_suffix = table_name[-1]
        stage_schema = data["Stage_Schema"]
        stage_table_name = data["Stage_Table_Name"]
    except KeyError as e:
        print(f"Error: Missing required key in data - {e}")
        return ""

    # Extract column names and data types from Final_Table_columns
    dim_columns = [col[0].strip() for col in data["Final_Table_columns"]]
    stage_columns = [col[1].strip() for col in data["Final_Table_columns"]]
    data_types = [col[2].strip() if len(col) > 2 else "" for col in data["Final_Table_columns"]]

    stage_column_set = {c for c in stage_columns if c}

    # Build NVL comparison predicates for WHEN MATCHED
    nvl_columns = []
    for col, dt in zip(dim_columns, data_types):
        # Exclude technical/audit columns from change detection
        if col in [
            "ROW_WID",
            "RECORD_ID",
            "INTEGRATION_ID",
            "W_INSERT_DATE",
            "W_INSERT_DT",
            "W_UPDATE_DT",
            "W_UPDATE_DATE",
            "W_INSERT_SOURCE",
            "W_UPDATE_SOURCE",
        ]:
            continue

        if "VARCHAR" in dt or "CHAR" in dt:
            nvl_columns.append(f"NVL({table_suffix}.{col}, 'X') != NVL(STG.{col}, 'X')")
        elif any(t in dt for t in ["NUMBER", "FLOAT", "DECIMAL"]):
            nvl_columns.append(f"NVL({table_suffix}.{col}, 0) != NVL(STG.{col}, 0)")
        elif any(t in dt for t in ["DATE", "TIMESTAMP", "DATETIME"]):
            nvl_columns.append(f"NVL({table_suffix}.{col}, :rundate) != NVL(STG.{col}, :rundate)")

    # Build UPDATE SET clause columns
    update_columns = []
    for col, val in zip(dim_columns, stage_columns):
        if col in [
            "ROW_WID",
            "RECORD_ID",
            "INTEGRATION_ID",
            "W_INSERT_DATE",
            "W_INSERT_DT",
            "W_INSERT_SOURCE",
        ]:
            continue

        if _is_stage_column_reference(val, stage_column_set):
            # Map to corresponding STG column, even when names differ
            update_columns.append(f"{table_suffix}.{col} = STG.{val.strip()}")
        else:
            # Expressions / literals used as-is (e.g. 'SCM')
            update_columns.append(f"{table_suffix}.{col} = {val}")

    # INSERT column lists (merge path uses table alias suffix)
    insert_columns_with_schema = ",\n ".join(f"{table_suffix}.{col}" for col in dim_columns)

    # VALUES list for INSERT; merge path needs STG. prefix because of alias in USING
    select_values = []
    for col, val in zip(dim_columns, stage_columns):
        if _is_stage_column_reference(val, stage_column_set):
            select_values.append(f"STG.{val.strip()}")
        else:
            select_values.append(val)
    select_columns_with_schema = ",\n ".join(select_values)

    # Unqualified column lists for truncate-load insert (no table aliases)
    insert_columns_unqualified = ",\n ".join(dim_columns)

    select_values_unqualified = []
    for col, val in zip(dim_columns, stage_columns):
        if _is_stage_column_reference(val, stage_column_set):
            select_values_unqualified.append(val.strip())
        else:
            select_values_unqualified.append(val)
    select_columns_unqualified = ",\n ".join(select_values_unqualified)

    matched_conditions = "\n     OR ".join(nvl_columns)
    update_set = ",\n   ".join(update_columns)

    # Only columns where final and stage columns are the same are used in USING
    merge_columns = ",\n ".join(
        col for col, val in zip(dim_columns, stage_columns) if col == val
    )

    if truncate_load == "true":
        procedure_template = f"""
CREATE OR REPLACE PROCEDURE {final_table_schema}.{procedure_name} () COPY GRANTS RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
 lc_procedure_name VARCHAR(30) := '{procedure_name}';
 ln_record_merge_cnt NUMBER;
 cust_exception EXCEPTION(-20002, 'Exception raised');
 rundate TIMESTAMP;
 v_count NUMBER;
 v_count_unk NUMBER;
 v_truncate VARCHAR(500);

BEGIN

 BEGIN TRANSACTION;

 CALL {final_table_schema}.cust_etl_logs_proc (
 :lc_procedure_name ,
 :lc_procedure_name,
 'START' ,
 'Start Inserting data into {table_name} table');

 Select COUNT(*) INTO :v_count
 FROM {stage_schema}.{stage_table_name} ;

 IF (v_count > 0) 
 THEN 

 v_truncate := 'TRUNCATE TABLE {final_table_schema}.{table_name}';

 EXECUTE IMMEDIATE v_truncate;

 INSERT INTO {final_table_schema}.{table_name} (
     {insert_columns_unqualified})
     SELECT {select_columns_unqualified}
     FROM {stage_schema}.{stage_table_name};

 ln_record_merge_cnt := SQLROWCOUNT;

 COMMIT WORK;

 CALL {final_table_schema}.cust_etl_logs_proc (
   :lc_procedure_name,
   :lc_procedure_name,
   'DEBUG',
   'Inserted ' || :ln_record_merge_cnt || ' records into {table_name} table'
 );

 ELSE

 CALL {final_table_schema}.cust_etl_logs_proc (
   :lc_procedure_name,
   :lc_procedure_name,
   'DEBUG',
   'Stage has no records So not processing any data'
 );

 COMMIT WORK;

 END IF;

 CALL {final_table_schema}.cust_etl_logs_proc (
 :lc_procedure_name,
 :lc_procedure_name,
 'END',
 'End Inserting data into table {table_name}'
 );

 RETURN 'SUCCESS' ;

EXCEPTION
 WHEN OTHER THEN
 ROLLBACK WORK;

 let v_sqlcode := SQLCODE;
 let v_sqlstate := SQLSTATE;
 let v_sqlerrm := SQLERRM;

 CALL {final_table_schema}.cust_etl_logs_proc (
   :lc_procedure_name,
   :lc_procedure_name,
   'ERROR',
   'Error while inserting data into table {table_name} \\n'||
   'SQLCODE: ' || TO_CHAR(:v_sqlcode) ||
   ' SQLERRM: ' || :v_sqlerrm ||
   ' SQLSTATE: ' || TO_CHAR(:v_sqlstate)
 ); 

 RAISE cust_exception;
 RETURN 'ERROR';
END;
$$;

GRANT USAGE ON PROCEDURE {final_table_schema}.{procedure_name}() TO ROLE GCSCDNA_DEV_BASE_RW ;
"""
    else:
        procedure_template = f"""
CREATE OR REPLACE PROCEDURE {final_table_schema}.{procedure_name} () RETURNS VARCHAR
LANGUAGE SQL
EXECUTE AS CALLER
AS
$$
DECLARE
 lc_procedure_name VARCHAR(30) := '{procedure_name}';
 ln_record_merge_cnt NUMBER;
 cust_exception EXCEPTION(-20002, 'Exception raised');
 rundate timestamp;

BEGIN

 CALL {final_table_schema}.cust_etl_logs_proc (
 :lc_procedure_name ,
 :lc_procedure_name,
 'START' ,
 'Start Merging data into {table_name} table');

 BEGIN TRANSACTION;

 rundate := TO_DATE ('01/01/1980', 'MM/DD/YYYY');

 MERGE INTO {final_table_schema}.{table_name} {table_suffix}
 USING (SELECT {merge_columns}
        FROM {stage_schema}.{stage_table_name} ) STG
 ON ({table_suffix}.INTEGRATION_ID = STG.INTEGRATION_ID)
 WHEN MATCHED AND (
     {matched_conditions}
 )
 THEN UPDATE SET 
  {update_set}
 WHEN NOT MATCHED
 THEN INSERT (
             {insert_columns_with_schema})
 VALUES (
             {select_columns_with_schema});

 ln_record_merge_cnt := SQLROWCOUNT;

 COMMIT WORK;

 CALL {final_table_schema}.cust_etl_logs_proc (
   :lc_procedure_name,
   :lc_procedure_name,
   'DEBUG',
   'Merged ' || :ln_record_merge_cnt || ' records into {table_name} table'
 );

 CALL {final_table_schema}.cust_etl_logs_proc (
 :lc_procedure_name,
 :lc_procedure_name,
 'END',
 'End Inserting data into table {table_name}'
 );

 RETURN 'SUCCESS' ;

EXCEPTION
 WHEN OTHER THEN
 ROLLBACK WORK;

 let v_sqlcode := SQLCODE;
 let v_sqlstate := SQLSTATE;
 let v_sqlerrm := SQLERRM;

 CALL {final_table_schema}.cust_etl_logs_proc (
   :lc_procedure_name,
   :lc_procedure_name,
   'ERROR',
   'Error while inserting data into table {table_name} \\n'||
   'SQLCODE: ' || TO_CHAR(:v_sqlcode) ||
   ' SQLERRM: ' || :v_sqlerrm ||
   ' SQLSTATE: ' || TO_CHAR(:v_sqlstate)
 ); 

 RAISE cust_exception;
 RETURN 'ERROR';
END;
$$;

GRANT USAGE ON PROCEDURE {final_table_schema}.{procedure_name}() TO ROLE GCSCDNA_DEV_BASE_RW ;
"""

    return procedure_template


def run_from_csv(csv_file_path=r"C:\\Nilesh\\Snow_Procedure\\Input_File_new.csv"):
    """Generate procedure code from a CSV file and write it to disk.

    Returns True on success, False on failure.
    """

    input_data = read_csv(csv_file_path)
    procedure_code = create_snowflake_procedure(input_data)

    if procedure_code:
        output_file_path = os.path.join(
            os.path.dirname(csv_file_path), f"{input_data['Procedure_name']}.prc"
        )
        with open(output_file_path, "w", encoding="utf-8") as file:
            file.write(procedure_code)

        _show_popup("Procedure Generated", "Snowflake Procedure")
        return True

    print("Failed to create procedure code due to missing data.")
    return False


if __name__ == "__main__":
    run_from_csv()
