"""Interactive CLI demo module for Smart AutoSorter."""

import json
import sys
import tempfile

from app.core.sample_corpus import generate_sample_corpus


def run_demo(settings):
    """Run an interactive CLI demo."""
    print("Starting Smart AutoSorter AI Pro - Interactive CLI Demo...", file=sys.stderr)

    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"[*] Generating sample corpus in {temp_dir}...", file=sys.stderr)
        files_to_sort = generate_sample_corpus(temp_dir)
        print(f"[*] Generated {len(files_to_sort)} files.", file=sys.stderr)

        from app.core.pipeline import SortPipeline
        from app.core.session import AppSession

        def progress_callback(info=None):
            print("    -> Progress: File extraction complete.", file=sys.stderr)

        print("[*] Processing files and generating sorting plan...", file=sys.stderr)
        session = AppSession(settings, base_dir=temp_dir)
        try:
            plan = SortPipeline(session).plan(
                files_to_sort, progress_callback=progress_callback
            )
        finally:
            session.close()

        print("\n--- Generated Sorting Plan ---", file=sys.stderr)
        print(json.dumps(plan, indent=2))
        print("------------------------------\n", file=sys.stderr)

        if plan and isinstance(plan, dict):
            print(
                "[+] Success: Demo completed. Sorting plan successfully generated.",
                file=sys.stderr,
            )
            sys.exit(0)
        else:
            print("[-] Failure: Failed to generate sorting plan.", file=sys.stderr)
            sys.exit(1)
