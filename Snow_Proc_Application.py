import os

from Snow_Proc_input import main as run_input_main, OUTPUT_PATH
import Snow_Proc_Exec


def main():
    """Run the input GUI first, then generate the procedure if input succeeded."""

    # Record existing CSV modification time (if any)
    prev_mtime = os.path.getmtime(OUTPUT_PATH) if os.path.exists(OUTPUT_PATH) else None

    # Run the input application (Tkinter GUI)
    try:
        run_input_main()
    except SystemExit:
        # User likely cancelled the input form; do not proceed.
        print("Input step cancelled; skipping procedure generation.")
        return

    # After GUI closes, check whether the CSV was generated/updated
    if not os.path.exists(OUTPUT_PATH):
        print(f"Input file not found after input step: {OUTPUT_PATH}")
        return

    new_mtime = os.path.getmtime(OUTPUT_PATH)
    if prev_mtime is not None and new_mtime == prev_mtime:
        print("Input file was not updated; skipping procedure generation.")
        return

    # Run the procedure generation step using the produced CSV
    success = Snow_Proc_Exec.run_from_csv(OUTPUT_PATH)
    if success:
        print("Procedure generation completed successfully.")
    else:
        print("Procedure generation failed.")


if __name__ == "__main__":
    main()
