"""Console user interface utilities.

Provides functions to display status and proposal plans via standard output.
"""

def print_status(message: str) -> None:
    """Print a standard status update to the console.

    Parameters
    ----------
    message : str
        The message to display.

    Returns
    -------
    None

    """
    print(f"\n{message}")

def print_proposal(plan: dict) -> None:
    """Draw the visual directory tree of the proposed sorting plan.

    Parameters
    ----------
    plan : dict
        A mapping of folder names to lists of files.

    Returns
    -------
    None

    """
    print("\n" + "="*50)
    print("PROPOSED SORTING PLAN")
    print("=" * 50)

    for folder, files in plan.items():
        if files:
            print(f"\n📂 [{folder}] ({len(files)} items)")
            for f in files[:3]:
                print(f"   ├── {f}")
            if len(files) > 3:
                print(f"   └── ...and {len(files) - 3} more.")

    print("\n" + "=" * 50)


def ask_for_approval() -> bool:
    """Prompt the user to approve or reject the plan.

    Returns
    -------
    bool
        True if the user approves, False otherwise.

    """
    while True:
        choice = input("\nDo you approve this sorting plan? (Y/N): ").strip().lower()
        if choice in ["y", "yes"]:
            return True
        elif choice in ["n", "no"]:
            print("Sorting cancelled by user. No files were moved.")
            return False
        else:
            print("Please enter 'Y' or 'N'.")
