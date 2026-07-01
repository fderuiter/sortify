import json
import os
from unittest.mock import MagicMock

from app.core.analyzer import IncrementalAnalyzer
from app.core.extractor import build_corpus_generator
from tests.generate_corpus import LARGE_CORPUS_DIR, create_large_corpus

BASELINE_FILE = os.path.join(os.path.dirname(__file__), "baseline_metrics.json")

def test_semantic_quality_guardrails():
    """
    Hybrid Quality Guardrail:
    Ensures that the semantic quality of clustering does not regress.
    Uses sequential ingestion to eliminate noise and compares the mathematical
    reconstruction error against a known golden baseline.
    """
    # 1. Generate large synthetic corpus for stress testing (500 documents across 4 themes)
    create_large_corpus(500)
    
    # 2. Set up deterministic sequential ingestion
    files = [f for f in os.listdir(LARGE_CORPUS_DIR) if os.path.isfile(os.path.join(LARGE_CORPUS_DIR, f))]
    files.sort()  # crucial for determinism
    
    analyzer = IncrementalAnalyzer(max_folders=4)
    progress_callback = MagicMock()
    
    generator = build_corpus_generator(
        base_dir=LARGE_CORPUS_DIR,
        items_to_sort=files,
        progress_callback=progress_callback,
        chunk_size=50,
        sequential=True
    )
    
    # 3. Ingest documents and build topic model
    for chunk in generator:
        analyzer.partial_fit(chunk)
        
    # Generate the plan, which triggers the clustering and calculates reconstruction error
    _ = analyzer.generate_sorting_plan()
    
    current_error = analyzer.last_reconstruction_error
    assert current_error > 0.0, "Reconstruction error must be captured and greater than zero."
    
    # Allow developers to update the baseline when algorithmic improvements are made
    update_baseline = os.environ.get("UPDATE_BASELINE") == "1"
    if update_baseline:
        with open(BASELINE_FILE, "w") as f:
            json.dump({"reconstruction_error": current_error}, f, indent=4)
        return
        
    assert os.path.exists(BASELINE_FILE), (
        "Baseline file not found. "
        "Run `UPDATE_BASELINE=1 pytest tests/test_quality_guardrails.py` to generate it."
    )
    
    with open(BASELINE_FILE, "r") as f:
        baseline_data = json.load(f)
        
    baseline_error = baseline_data.get("reconstruction_error")
    assert baseline_error is not None, "Invalid baseline file."
    
    # 4. Compare current error against statistical tolerance interval (+/- 5%)
    tolerance = 0.05
    lower_bound = baseline_error * (1 - tolerance)
    upper_bound = baseline_error * (1 + tolerance)
    
    assert lower_bound <= current_error <= upper_bound, (
        f"Quality regression detected. "
        f"Current error ({current_error:.4f}) is outside acceptable bounds "
        f"({lower_bound:.4f} - {upper_bound:.4f}) of baseline ({baseline_error:.4f}). "
        f"If this is expected, update baseline with UPDATE_BASELINE=1."
    )
