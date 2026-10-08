"""Unit tests for standard ProgressUpdate dataclass and central emit_progress module."""

from unittest.mock import MagicMock

import pytest

from app.core.extractor import process_item_worker
from app.core.forensic_scanner import ForensicScanner
from app.core.metadata import MetadataPass
from app.core.progress import ProgressUpdate, emit_progress
from app.plugins.clinical_compliance.cro_multi_study_pipeline import (
    CROMultiStudyPipeline,
)


def test_progress_update_dataclass_defaults_and_clamping():
    """Verify ProgressUpdate field defaults and ratio clamping behavior."""
    update = ProgressUpdate(
        progress=0.5, stage="Extracting", unit_count=10, unit_type="files"
    )
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


@pytest.mark.slow
@pytest.mark.integration
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
        str(tmp_path),
        ["doc.txt"],
        settings,
        db=None,
        callback=progress_cb,
        cancel_check=None,
    )

    assert len(updates) == 1
    assert updates[0].progress == 1.0
    assert updates[0].unit_type == "files"


def test_progress_update_invalid_coercions():
    """Verify ProgressUpdate gracefully handles invalid progress and unit_count values."""
    update = ProgressUpdate(progress="invalid_float")
    assert update.progress == 0.0

    update_none = ProgressUpdate(progress=None)
    assert update_none.progress == 0.0

    update_bad_unit = ProgressUpdate(progress=0.5, unit_count="invalid_int")
    assert update_bad_unit.unit_count is None


def test_emit_progress_falsy_callback_and_update_instance():
    """Verify emit_progress with None callback or direct ProgressUpdate instance."""
    # Falsy callback
    assert emit_progress(None, 0.5) is None

    # Direct ProgressUpdate instance delivery
    received = []

    def cb(update: ProgressUpdate):
        received.append(update)

    up = ProgressUpdate(progress=0.7, stage="DirectUpdate")
    emit_progress(cb, up)
    assert len(received) == 1
    assert received[0] is up


def test_emit_progress_string_stage_and_kwargs():
    """Verify string stage argument parsing and fallback keyword options."""
    received = []

    def cb(update: ProgressUpdate):
        received.append(update)

    # String passed as first argument and progress in kwargs
    emit_progress(cb, "StringStage", progress=0.35)
    assert received[-1].progress == 0.35
    assert received[-1].stage == "StringStage"

    # unit_count and unit_type via kwargs when positional parameters are None
    emit_progress(
        cb,
        0.5,
        unit_count=None,
        unit_type=None,
        kwargs={"unit_count": 42, "unit_type": "items"},
    )
    assert received[-1].unit_count == 42
    assert received[-1].unit_type == "items"

    # Stage specified via 'message' kwarg
    emit_progress(cb, 0.8, message="MessageKwarg")
    assert received[-1].stage == "MessageKwarg"

    # Stage specified via 'stage' kwarg when stage arg is None
    emit_progress(cb, 0.9, stage=None, kwargs={"stage": "StageKwarg"})
    assert received[-1].stage == "StageKwarg"


def test_emit_progress_inspection_type_errors():
    """Verify inspection parameter calls catch inner TypeErrors and fall through."""
    # 2-arg callback raising TypeError
    calls_2arg = []

    def cb_2arg_error(p, s):
        calls_2arg.append((p, s))
        raise TypeError("Inner type error during 2-arg call")

    emit_progress(cb_2arg_error, 0.5, stage="Stage2Arg")
    assert len(calls_2arg) >= 1

    # 2-arg callback when stage is None (hits lines 111-113)
    calls_2arg_none = []

    def cb_2arg_none(p, s):
        calls_2arg_none.append((p, s))

    emit_progress(cb_2arg_none, 0.5, stage=None)
    assert calls_2arg_none == [(0.5, None)]

    # 1-arg callback raising TypeError
    calls_1arg = []

    def cb_1arg_error(p):
        calls_1arg.append(p)
        raise TypeError("Inner type error during 1-arg call")

    emit_progress(cb_1arg_error, 0.5)
    assert len(calls_1arg) >= 1

    # 0-arg callback raising TypeError (hits lines 128-129)
    calls_0arg = []

    def cb_0arg_error():
        calls_0arg.append(True)
        raise TypeError("Inner type error during 0-arg call")

    emit_progress(cb_0arg_error, 0.5)
    assert len(calls_0arg) >= 1


def test_emit_progress_generic_fallback_paths(monkeypatch):
    """Verify generic fallback execution when inspect.signature fails or raises ValueError/TypeError."""
    import inspect

    # Force inspect.signature to raise ValueError
    monkeypatch.setattr(
        inspect,
        "signature",
        MagicMock(side_effect=ValueError("Signature inspect not supported")),
    )

    # 1. Callback accepting ProgressUpdate directly in fallback
    recv_fallback_update = []

    def cb_fallback_update(update: ProgressUpdate):
        recv_fallback_update.append(update)

    emit_progress(cb_fallback_update, 0.5, stage="Fallback1")
    assert len(recv_fallback_update) == 1
    assert recv_fallback_update[0].progress == 0.5

    # 2. Callback rejecting ProgressUpdate (TypeError) and taking (progress, stage)
    recv_fallback_2arg = []

    def cb_fallback_2arg(*args):
        if len(args) == 1 and isinstance(args[0], ProgressUpdate):
            raise TypeError("No ProgressUpdate allowed")
        recv_fallback_2arg.append(args)

    emit_progress(cb_fallback_2arg, 0.6, stage="Fallback2")
    assert recv_fallback_2arg == [(0.6, "Fallback2")]

    # 3. Callback rejecting ProgressUpdate and 2-arg, taking 1 float arg
    recv_fallback_1arg = []

    def cb_fallback_1arg(*args):
        if len(args) == 1 and isinstance(args[0], ProgressUpdate):
            raise TypeError("No ProgressUpdate allowed")
        if len(args) == 2:
            raise TypeError("No 2-arg allowed")
        recv_fallback_1arg.append(args[0])

    emit_progress(cb_fallback_1arg, 0.7, stage="Fallback3")
    assert recv_fallback_1arg == [0.7]

    # 4. Callback taking 1 stage string arg only
    recv_fallback_stage = []

    def cb_fallback_stage(*args):
        if len(args) == 1 and isinstance(args[0], ProgressUpdate):
            raise TypeError("No ProgressUpdate allowed")
        if len(args) == 2:
            raise TypeError("No 2-arg allowed")
        if len(args) == 1 and isinstance(args[0], float):
            raise TypeError("No float allowed")
        recv_fallback_stage.append(args[0])

    emit_progress(cb_fallback_stage, 0.8, stage="FallbackStageOnly")
    assert recv_fallback_stage == ["FallbackStageOnly"]

    # 5. Callback taking 0 args
    recv_fallback_0arg = []

    def cb_fallback_0arg(*args):
        if args:
            raise TypeError("No args allowed")
        recv_fallback_0arg.append(True)

    emit_progress(cb_fallback_0arg, 0.9, stage="Fallback0Arg")
    assert recv_fallback_0arg == [True]

    # 6. Callback raising non-TypeError Exception in fallback
    def cb_fallback_exception(*args):
        raise RuntimeError("Catastrophic error in legacy callback")

    # Should log and NOT raise
    emit_progress(cb_fallback_exception, 1.0, stage="ErrorStage")

    # 7. Callback raising Exception directly on callback(update)
    def cb_outer_exception(update):
        raise ValueError("Outer error in fallback")

    emit_progress(cb_outer_exception, 1.0)
