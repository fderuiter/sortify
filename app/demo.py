"""Interactive CLI demo module for Smart AutoSorter."""

import json
import os
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

        from app.core.analyzer import IncrementalAnalyzer
        from app.core.db import Database
        from app.core.db_worker import DBWorker
        from app.core.extractor import build_corpus_generator

        db_worker = DBWorker()
        db_path = os.path.join(temp_dir, "demo.db")
        db = Database(db_path, db_worker)

        analyzer = IncrementalAnalyzer(
            max_folders=settings.MAX_FOLDERS,
            stop_words=settings.STOP_WORDS,
            db=db,
        )

        def progress_callback(info=None):
            print("    -> Progress: File extraction complete.", file=sys.stderr)

        print("[*] Processing files incrementally...", file=sys.stderr)

        def cancel_check():
            return False

        generator = build_corpus_generator(
            base_dir=temp_dir,
            items_to_sort=files_to_sort,
            progress_callback=progress_callback,
            max_workers=settings.MAX_WORKERS,
            db=db,
            chunk_size=50,
            cancel_check=cancel_check,
            settings=settings,
        )

        for i, chunk in enumerate(generator):
            print(f"    - Processing chunk {i + 1}...", file=sys.stderr)
            analyzer.partial_fit(temp_dir, chunk, settings)

        print("[*] Generating sorting plan...", file=sys.stderr)
        plan = analyzer.generate_sorting_plan(temp_dir, settings)

        print("\n--- Generated Sorting Plan ---", file=sys.stderr)
        print(json.dumps(plan, indent=2))
        print("------------------------------\n", file=sys.stderr)

        analyzer.terminate()
        db_worker.stop()

        if plan and isinstance(plan, dict):
            print(
                "[+] Success: Demo completed. Sorting plan successfully generated.",
                file=sys.stderr,
            )
            sys.exit(0)
        else:
            print("[-] Failure: Failed to generate sorting plan.", file=sys.stderr)
            sys.exit(1)
