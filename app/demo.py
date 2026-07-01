import os
import tempfile
import sys
import json

from app.core.analyzer import IncrementalAnalyzer
from app.core.extractor import build_corpus_generator
from app.config import MAX_FOLDERS

def generate_sample_corpus(base_dir: str):
    """Generate a sample corpus with at least 3 documents."""
    os.makedirs(base_dir, exist_ok=True)
    
    with open(os.path.join(base_dir, "demo_finance.txt"), "w") as f:
        f.write("This is a detailed report on finance, money, investment, and banking strategies. The economy is growing.")
        
    with open(os.path.join(base_dir, "demo_tech.txt"), "w") as f:
        f.write("Notes on software engineering, computer science, algorithms, and technology. Python is great.")
        
    with open(os.path.join(base_dir, "demo_health.txt"), "w") as f:
        f.write("Medical science, healthcare, doctor, patient, clinical trials, medicine, and health.")

    # Create empty file as well to test robustness
    with open(os.path.join(base_dir, "empty.txt"), "w") as f:
        f.write("")
        
    return ["demo_finance.txt", "demo_tech.txt", "demo_health.txt", "empty.txt"]

def run_demo():
    """Run an interactive CLI demo."""
    print("Starting Smart AutoSorter AI Pro - Interactive CLI Demo...")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        print(f"[*] Generating sample corpus in {temp_dir}...")
        files_to_sort = generate_sample_corpus(temp_dir)
        print(f"[*] Generated {len(files_to_sort)} files.")
        
        analyzer = IncrementalAnalyzer(max_folders=MAX_FOLDERS)
        
        def progress_callback():
            pass
            
        print("[*] Processing files incrementally...")
        generator = build_corpus_generator(temp_dir, files_to_sort, progress_callback, chunk_size=2)
        
        for i, chunk in enumerate(generator):
            print(f"    - Processing chunk {i+1}...")
            analyzer.partial_fit(chunk)
            
        print("[*] Generating sorting plan...")
        plan = analyzer.generate_sorting_plan()
        
        print("\n--- Generated Sorting Plan ---")
        print(json.dumps(plan, indent=2))
        print("------------------------------\n")
        
        if plan and isinstance(plan, dict):
            print("[+] Success: Demo completed. Sorting plan successfully generated.")
            sys.exit(0)
        else:
            print("[-] Failure: Failed to generate sorting plan.")
            sys.exit(1)
