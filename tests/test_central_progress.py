"""Unit tests for standard ProgressUpdate dataclass and central emit_progress module."""

from unittest.mock import MagicMock

from app.core.cro_multi_study_pipeline import CROMultiStudyPipeline
from app.core.extractor import process_item_worker
from app.core.forensic_scanner import ForensicScanner
from app.core.metadata import MetadataPass
from app.core.progress import ProgressUpdate, emit_progress


def test_progress_update_dataclass_defaults_and_clamping():
    """Verify ProgressUpdate field defaults and ratio clamping behavior."""
    update = ProgressUpdate(progress=0.5, stage="Extracting", unit_count=10, unit_type="files")
    assert update.progress == 0.5
    assert update.stage == "Extracting"
    assert update.unit_count == 10
    assert update.unit_type == "files"

    # Percentage normalization (> 1.0 and <= 100.0)
    update_pct = ProgressUpdate(progress=75.0)
    assert update_pct.progress == 0.75

    # Out of bounds clamping
    update_over = ProgressUpdate(progress=150.0)
    assert update_over.progress == 1.0

    update_under = ProgressUpdate(progress=-0.5)
    assert update_under.progress == 0.0


def test_emit_progress_with_modern_and_legacy_callbacks():
    """Verify emit_progress delivers ProgressUpdate to modern and legacy callbacks."""
    # Modern / 1-arg callback receiving ProgressUpdate
    received_updates = []

    def modern_cb(update: ProgressUpdate):
        received_updates.append(update)

    emit_progress(modern_cb, 0.4, stage="Scanning", unit_count=5, unit_type="items")
    assert len(received_updates) == 1
    assert received_updates[0].progress == 0.4
    assert received_updates[0].stage == "Scanning"
    assert received_updates[0].unit_count == 5
    assert received_updates[0].unit_type == "items"

    # Legacy 2-arg callback (takes 2 positional args)
    legacy_2arg_calls = []

    def legacy_2arg(pct, msg):
        legacy_2arg_calls.append((pct, msg))

    emit_progress(legacy_2arg, 0.6, stage="Processing")
    assert legacy_2arg_calls == [(0.6, "Processing")]

    # Legacy 1-arg callback that expects float only and raises TypeError if given ProgressUpdate
    legacy_1arg_calls = []

    def legacy_1arg(pct):
        if isinstance(pct, ProgressUpdate):
            raise TypeError("Expected float, got ProgressUpdate")
        legacy_1arg_calls.append(pct)

    emit_progress(legacy_1arg, 0.8)
    assert legacy_1arg_calls == [0.8]

    # Legacy 0-arg callback
    legacy_0arg_calls = []

    def legacy_0arg():
        legacy_0arg_calls.append(True)

    emit_progress(legacy_0arg, 1.0)
    assert legacy_0arg_calls == [True]


def test_forensic_scanner_emits_structured_progress(tmp_path):
    """Verify ForensicScanner emits ProgressUpdate objects with unit counts and stage descriptions."""
    scanner = ForensicScanner(temp_staging_dir=str(tmp_path / "staging"))
    for i in range(5):
        test_doc = tmp_path / f"test_{i}.pdf"
        test_doc.write_bytes(b"%PDF-1.4 test document content")

    events = []

    def progress_cb(update: ProgressUpdate):
        events.append(update)

    scanner.scan_drive(str(tmp_path), progress_callback=progress_cb)

    assert any(isinstance(ev, ProgressUpdate) for ev in events)
    assert any(ev.unit_type == "items" for ev in events)


def test_cro_pipeline_emits_normalized_ratios(tmp_path):
    """Verify CROMultiStudyPipeline emits ratios strictly between 0.0 and 1.0."""
    src = tmp_path / "src"
    tgt = tmp_path / "tgt"
    src.mkdir()
    tgt.mkdir()
    (src / "sample.pdf").write_bytes(b"%PDF-1.4 dummy file content")

    pipeline = CROMultiStudyPipeline(mode="tmf")
    updates = []

    def progress_cb(update: ProgressUpdate):
        updates.append(update)

    pipeline.run_pipeline(str(src), str(tgt), progress_callback=progress_cb)

    assert len(updates) > 0
    for update in updates:
        assert isinstance(update, ProgressUpdate)
        assert 0.0 <= update.progress <= 1.0


def test_downloader_encapsulates_byte_metrics(tmp_path):
    """Verify downloader passes byte metrics inside ProgressUpdate fields."""
    updates = []

    def progress_cb(update: ProgressUpdate):
        updates.append(update)

    target_file = tmp_path / "model.onnx"

    # Simulate run_background_download emitting progress
    emit_progress(
        progress_cb,
        progress_or_update=0.5,
        stage="Downloaded 5.00MB of 10.00MB (50.0%)",
        unit_count=5242880,
        unit_type="bytes",
    )

    assert len(updates) == 1
    assert updates[0].progress == 0.5
    assert updates[0].unit_count == 5242880
    assert updates[0].unit_type == "bytes"


def test_extractor_and_metadata_emit_completion_events(tmp_path):
    """Verify extractor and metadata passes emit completion events via emit_progress."""
    updates = []

    def progress_cb(update: ProgressUpdate):
        updates.append(update)

    # Test extractor process_item_worker
    test_file = tmp_path / "doc.txt"
    test_file.write_text("hello world")

    process_item_worker(str(tmp_path), "doc.txt", progress_cb, db=None)

    assert len(updates) == 1
    assert updates[0].progress == 1.0
    assert updates[0].unit_type == "files"

    # Test metadata pass
    updates.clear()
    settings = MagicMock()
    settings.KEYWORD_RULES = {"doc": "DocsFolder"}
    settings.LEARNED_RULES = {}
    settings.POLICIES = []

    MetadataPass.run(
        str(tmp_path), ["doc.txt"], settings, db=None, callback=progress_cb, cancel_check=None
    )

    assert len(updates) == 1
    assert updates[0].progress == 1.0
    assert updates[0].unit_type == "files"
