# ui/console.py


def print_status(message):
    """Prints a standard status update to the console."""
    print(f"\n{message}")


def print_proposal(plan):
    """Draws the visual directory tree of the proposed sorting plan."""
    print("\n" + "=" * 50)
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


def ask_for_approval():
    """Prompts the user to approve or reject the plan."""
    while True:
        choice = input("\nDo you approve this sorting plan? (Y/N): ").strip().lower()
        if choice in ["y", "yes"]:
            return True
        elif choice in ["n", "no"]:
            print("Sorting cancelled by user. No files were moved.")
            return False
        else:
            print("Please enter 'Y' or 'N'.")
