"""Interactive CLI demo module for Smart AutoSorter."""

import json
import os
import sys
import tempfile


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
            print("    -> Progress: File extraction complete.")

        print("[*] Processing files incrementally...")
        
        def cancel_check():
            return False
            
        generator = build_corpus_generator(
            base_dir=temp_dir,
            items_to_sort=files_to_sort,
            progress_callback=progress_callback,
            max_workers=settings.MAX_WORKERS,
            db=db,
            chunk_size=50,
            active_model_name=analyzer.active_model_name,
            active_dimension=analyzer.active_dimension,
            cancel_check=cancel_check
        )

        for i, chunk in enumerate(generator):
            print(f"    - Processing chunk {i + 1}...")
            analyzer.partial_fit(temp_dir, chunk, settings)

        print("[*] Generating sorting plan...")
        plan = analyzer.generate_sorting_plan(temp_dir, settings)

        print("\n--- Generated Sorting Plan ---")
        print(json.dumps(plan, indent=2))
        print("------------------------------\n")
        
        analyzer.terminate()
        db_worker.stop()

        if plan and isinstance(plan, dict):
            print("[+] Success: Demo completed. Sorting plan successfully generated.")
            sys.exit(0)
        else:
            print("[-] Failure: Failed to generate sorting plan.")
            sys.exit(1)
