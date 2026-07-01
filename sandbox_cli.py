#!/usr/bin/env python3
"""CLI tool for testing ML extraction and analysis in an isolated sandbox environment."""

import argparse
import os
import shutil

from app.core.analyzer import IncrementalAnalyzer
from app.core.extractor import extract_file_text

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SANDBOX_DIR = os.path.join(BASE_DIR, "sandbox", "dataset")
GOLDEN_DIR = os.path.join(BASE_DIR, "sandbox", "dataset_golden")


def reset_sandbox():
    """Restores the sandbox dataset to its original state from the golden dataset."""
    if os.path.exists(SANDBOX_DIR):
        shutil.rmtree(SANDBOX_DIR)
    shutil.copytree(GOLDEN_DIR, SANDBOX_DIR)
    print("Sandbox dataset has been reset to its original state.")


def extract_file(filename):
    """Extract text from a specific sandbox file."""
    filepath = os.path.join(SANDBOX_DIR, filename)
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return
    text = extract_file_text(filepath)
    print(f"--- Extracted Text for {filename} ---")
    print(text)
    print("-" * 40)


def analyze_all():
    """Run the analysis pipeline on all sandbox files."""
    if not os.path.exists(SANDBOX_DIR):
        print("Sandbox dataset not found. Run reset first.")
        return
    analyzer = IncrementalAnalyzer(max_folders=5)
    corpus = {}
    for filename in os.listdir(SANDBOX_DIR):
        filepath = os.path.join(SANDBOX_DIR, filename)
        if os.path.isfile(filepath):
            text = extract_file_text(filepath)
            corpus[filepath] = f"{filename} {text}"

    analyzer.partial_fit(corpus)
    plan = analyzer.generate_sorting_plan()
    import json

    print("--- Analysis Sorting Plan ---")
    print(json.dumps(plan, indent=2))
    print("-" * 40)


def main():
    """Execute the main CLI logic for the sandbox tool."""
    parser = argparse.ArgumentParser(
        description="Sandbox CLI Tool for ML Accuracy Verification"
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # reset command
    subparsers.add_parser("reset", help="Reset the sandbox dataset to its golden state")

    # extract command
    parser_extract = subparsers.add_parser(
        "extract", help="Extract text from a specific sandbox file"
    )
    parser_extract.add_argument(
        "filename", type=str, help="Name of the file in the sandbox dataset"
    )

    # analyze command
    subparsers.add_parser(
        "analyze", help="Run the analysis pipeline on all sandbox files"
    )

    args = parser.parse_args()

    if args.command == "reset":
        reset_sandbox()
    elif args.command == "extract":
        extract_file(args.filename)
    elif args.command == "analyze":
        analyze_all()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
