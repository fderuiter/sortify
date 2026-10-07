"""Shared sample data generation engine for Smart AutoSorter AI Pro."""

import os
from typing import List


def generate_sample_corpus(base_dir: str, overwrite: bool = False) -> List[str]:
    """Generate a sample corpus with categorized text documents (finance, tech, health, empty).

    Args:
        base_dir: Target directory path where sample documents will be created.
        overwrite: If False and any sample files exist in base_dir, raises FileExistsError.

    Returns
    -------
        List of created relative file names.
    """
    os.makedirs(base_dir, exist_ok=True)

    sample_files = {
        "demo_finance.txt": (
            "This is a detailed report on finance, money, investment, and banking strategies. "
            "The economy is growing."
        ),
        "demo_tech.txt": (
            "Notes on software engineering, computer science, algorithms, and technology. "
            "Python is great."
        ),
        "demo_health.txt": (
            "Medical science, healthcare, doctor, patient, clinical trials, medicine, and health."
        ),
        "empty.txt": "",
    }

    if not overwrite:
        existing = [
            f for f in sample_files if os.path.exists(os.path.join(base_dir, f))
        ]
        if existing:
            raise FileExistsError(
                f"Sample files already exist in {base_dir}: {', '.join(existing)}"
            )

    for filename, content in sample_files.items():
        filepath = os.path.join(base_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

    return list(sample_files.keys())
