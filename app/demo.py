"""Interactive CLI demo module for Smart AutoSorter."""

import json
import os
import sys
import tempfile

from app.core.session import AppSession


def generate_sample_corpus(base_dir: str):
    """Generate a sample corpus with at least 3 documents."""
    os.makedirs(base_dir, exist_ok=True)

    with open(os.path.join(base_dir, "demo_finance.txt"), "w") as f:
        f.write(
            "This is a detailed report on finance, money, investment, and banking strategies. The economy is growing."
        )

    with open(os.path.join(base_dir, "demo_tech.txt"), "w") as f:
        f.write(
            "Notes on software engineering, computer science, algorithms, and technology. Python is great."
        )

    with open(os.path.join(base_dir, "demo_health.txt"), "w") as f:
        f.write(
            "Medical science, healthcare, doctor, patient, clinical trials, medicine, and health."
        )

    # Create empty file as well to test robustness
    with open(os.path.join(base_dir, "empty.txt"), "w") as f:
        f.write("")

    return ["demo_finance.txt", "demo_tech.txt", "demo_health.txt", "empty.txt"]


def run_demo(settings):
    """Run an interactive CLI demo."""
    print("Starting Smart AutoSorter AI Pro - Interactive CLI Demo...")

    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"[*] Generating sample corpus in {temp_dir}...")
        files_to_sort = generate_sample_corpus(temp_dir)
        print(f"[*] Generated {len(files_to_sort)} files.")

        session = AppSession(settings, base_dir=temp_dir)

        def progress_callback(info=None):
            pass

        print("[*] Processing files incrementally...")
        
        def cancel_check():
            return False
        generator = session.process_items(
            files_to_sort,
            progress_callback,
            cancel_check
        )

        for i, chunk in enumerate(generator):
            print(f"    - Processing chunk {i + 1}...")
            session.partial_fit(chunk)

        print("[*] Generating sorting plan...")
        plan = session.generate_sorting_plan()

        print("\n--- Generated Sorting Plan ---")
        print(json.dumps(plan, indent=2))
        print("------------------------------\n")
        
        session.close()

        if plan and isinstance(plan, dict):
            print("[+] Success: Demo completed. Sorting plan successfully generated.")
            sys.exit(0)
        else:
            print("[-] Failure: Failed to generate sorting plan.")
            sys.exit(1)
