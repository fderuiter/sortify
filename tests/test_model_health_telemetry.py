"""Unit and integration tests for explicit diagnostic error boundaries and fallback telemetry."""

import pytest

from app.core.analyzer import IncrementalAnalyzer, SortingPlan
from app.core.analyzer_strategies import GenerativeNamingStrategy
from app.core.db import Database, DBWorker
from app.core.domain_contracts import SortingPlanModel
from app.core.exceptions import (
    ArchiveCorruptionError,
    HashVerificationError,
    ModelWeightsNotFoundError,
    OfflineLoaderError,
    OfflineModelLoadError,
    SortifyBaseError,
)
from app.core.offline_loader import OfflineModelLoader
from app.core.semantic_embeddings import SemanticEmbeddingManager
from app.core.shared_registry import SharedModelRegistry
from app.ui.notifications import NotificationManager


@pytest.fixture
def db_worker():
    worker = DBWorker()
    yield worker
    worker.stop()


@pytest.fixture
def db(tmp_path, db_worker):
    database = Database(tmp_path / "test.db", db_worker)
    yield database
    from app.core.db_conn import clear_connection_cache

    clear_connection_cache()


def test_exception_subclassing_and_error_boundary():
    """Verify explicit exceptions inherit from OfflineLoaderError, OfflineModelLoadError, and SortifyBaseError."""
    corrupt_err = ArchiveCorruptionError("Corrupted archive")
    hash_err = HashVerificationError("Hash mismatch")
    weights_err = ModelWeightsNotFoundError("florence-2", ["/tmp/test"])

    for err in (corrupt_err, hash_err, weights_err):
        assert isinstance(err, SortifyBaseError)
        assert isinstance(err, OfflineLoaderError)
        assert isinstance(err, OfflineModelLoadError)

    assert isinstance(hash_err, ValueError)
    assert isinstance(weights_err, FileNotFoundError)


def test_shared_model_registry_health_tracking():
    """Verify SharedModelRegistry records, retrieves, and updates model health telemetry."""
    registry = SharedModelRegistry.get_instance()

    # Check uninitialized status
    uninit = registry.get_model_health("test_model_1")
    assert uninit["status"] == "UNINITIALIZED"
    assert uninit["failure_reason"] is None

    # Record health update
    rec = registry.record_model_health(
        "test_model_1",
        status="DEGRADED_FALLBACK",
        failure_reason="Corrupted zip file",
        suggested_recovery_action="RE_DOWNLOAD_MODEL",
    )
    assert rec["status"] == "DEGRADED_FALLBACK"
    assert rec["failure_reason"] == "Corrupted zip file"
    assert rec["suggested_recovery_action"] == "RE_DOWNLOAD_MODEL"

    # Fetch recorded health
    fetched = registry.get_model_health("test_model_1")
    assert fetched["status"] == "DEGRADED_FALLBACK"
    assert fetched["failure_reason"] == "Corrupted zip file"

    # Fetch all health records
    all_health = registry.get_all_model_health()
    assert "test_model_1" in all_health
    assert all_health["test_model_1"]["status"] == "DEGRADED_FALLBACK"


def test_archive_corruption_raises_explicit_exception_and_records_telemetry(tmp_path):
    """Verify sidecar zip corruption raises ArchiveCorruptionError and records registry health."""
    registry = SharedModelRegistry.get_instance()

    corrupt_zip = tmp_path / "smart-autosorter-models.zip"
    corrupt_zip.write_bytes(b"INVALID_ZIP_HEADER_BYTES")

    target_dir = tmp_path / "extract_target"

    with pytest.raises(ArchiveCorruptionError) as exc_info:
        OfflineModelLoader.hydrate_sidecar_models(
            sidecar_zip_path=str(corrupt_zip), target_dir=str(target_dir)
        )

    assert (
        "corrupted" in str(exc_info.value).lower()
        or "zip" in str(exc_info.value).lower()
    )

    health = registry.get_model_health("sidecar_bundle")
    assert health["status"] in ("FAILED", "DEGRADED_FALLBACK")
    assert health["suggested_recovery_action"] == "RE_DOWNLOAD_MODEL"


def test_hash_verification_failure_raises_explicit_exception_and_records_telemetry(
    tmp_path,
):
    """Verify hash mismatch in verify_integrity raises HashVerificationError and records telemetry."""
    registry = SharedModelRegistry.get_instance()

    model_dir = tmp_path / "test_hash_model"
    model_dir.mkdir()
    bad_file = model_dir / "config.json"
    bad_file.write_text("modified file content")

    registry.register_expected_hashes(
        "hash_test_model",
        {
            "config.json": "0000000000000000000000000000000000000000000000000000000000000000"
        },
    )

    with pytest.raises(HashVerificationError) as exc_info:
        registry.verify_integrity("hash_test_model", str(model_dir))

    assert "integrity check failed" in str(exc_info.value).lower()

    health = registry.get_model_health("hash_test_model")
    assert health["status"] == "FAILED"
    assert health["suggested_recovery_action"] == "RE_DOWNLOAD_MODEL"


def test_semantic_embedding_manager_degradation_telemetry(db):
    """Verify SemanticEmbeddingManager records degradation telemetry and falls back gracefully."""
    registry = SharedModelRegistry.get_instance()

    # Initialize embedding manager with force_validation=False on non-existent path
    manager = SemanticEmbeddingManager(
        db, model_path="/non_existent/model/path", bypass_validation=True
    )

    # Trigger generate_embedding when model path is invalid
    vec = manager.generate_embedding("Sample text for vector generation")
    assert len(vec) == 384
    assert manager.is_degraded is True
    assert manager.degradation_state == "DEGRADED_FALLBACK"

    health = registry.get_model_health("onnx_embeddings")
    assert health["status"] == "DEGRADED_FALLBACK"


def test_generative_naming_strategy_degradation_telemetry():
    """Verify GenerativeNamingStrategy sets degradation state on model loading failure."""
    registry = SharedModelRegistry.get_instance()
    strategy = GenerativeNamingStrategy(model_path="/non_existent/generative/model")

    # Attempt accessing generator property
    gen = strategy.generator
    assert gen is None
    assert strategy.is_degraded is True
    assert strategy.degradation_state == "DEGRADED_FALLBACK"

    health = registry.get_model_health("generative_naming")
    assert health["status"] == "DEGRADED_FALLBACK"


def test_sorting_plan_diagnostic_metadata_contract():
    """Verify SortingPlanModel and SortingPlan store and expose diagnostic metadata fields."""
    plan_model = SortingPlanModel(
        nodes={"Invoices": {}},
        model_status="DEGRADED_FALLBACK",
        degradation_reason="SHA-256 hash mismatch",
        recovery_action="RE_DOWNLOAD_MODEL",
    )
    assert plan_model.model_status == "DEGRADED_FALLBACK"
    assert plan_model.degradation_reason == "SHA-256 hash mismatch"

    plan = SortingPlan(
        plan={"Invoices": {}},
        model_status="DEGRADED_FALLBACK",
        degradation_reason="Archive corruption detected",
        recovery_action="RE_DOWNLOAD_MODEL",
    )
    assert plan.model_status == "DEGRADED_FALLBACK"
    assert plan.degradation_reason == "Archive corruption detected"
    assert plan.recovery_action == "RE_DOWNLOAD_MODEL"
    assert plan["model_status"] == "DEGRADED_FALLBACK"
    assert plan.get("recovery_action") == "RE_DOWNLOAD_MODEL"


def test_incremental_analyzer_propagates_degraded_telemetry(db, tmp_path):
    """Verify IncrementalAnalyzer attaches degraded-mode telemetry to the plan.

    The UI reads these fields to warn the user; core does not notify the UI.
    """
    registry = SharedModelRegistry.get_instance()
    registry.record_model_health(
        "onnx_embeddings",
        status="DEGRADED_FALLBACK",
        failure_reason="Model weights not found in search paths",
        suggested_recovery_action="RE_DOWNLOAD_MODEL",
    )

    analyzer = IncrementalAnalyzer(5, set(), db)

    notifications = []
    notif_mgr = NotificationManager.get_instance()
    notif_mgr.register_handler(notifications.append)

    try:
        plan = analyzer.generate_sorting_plan(base_dir=str(tmp_path))

        assert plan.model_status == "DEGRADED_FALLBACK"
        assert "Model weights not found" in str(plan.degradation_reason)
        assert plan.recovery_action == "RE_DOWNLOAD_MODEL"
        assert notifications == []
    finally:
        notif_mgr._custom_handlers.remove(notifications.append)


def test_missing_onnxruntime_records_install_ml_extra_and_marks_runtime_fallback(
    db, tmp_path, monkeypatch
):
    """Req 4 & Req 5: Missing onnxruntime records INSTALL_ML_EXTRA in SharedModelRegistry and reports [AI: Fallback]."""
    import hashlib
    import importlib.machinery
    import importlib.util
    from types import SimpleNamespace
    from unittest.mock import patch

    from app.core.downloader import (
        _VERIFICATION_CACHE,
        DownloadManager,
        get_ai_runtime_state,
        is_onnxruntime_available,
    )

    registry = SharedModelRegistry.get_instance()
    registry.record_model_health("onnx_embeddings", status="HEALTHY")

    model_dir = tmp_path / "verified_model"
    model_dir.mkdir()
    bundle_payloads = {
        "model.onnx": b"onnx-bytes",
        "config.json": b'{"model_type": "bert"}',
        "tokenizer.json": b'{"version": "1.0"}',
        "tokenizer_config.json": b'{"do_lower_case": true}',
        "special_tokens_map.json": b'{"cls_token": "[CLS]"}',
        "vocab.txt": b"[PAD]\n[CLS]\n",
        "version.txt": b"1.0.0",
    }
    expected_hashes = {}
    for fname, payload in bundle_payloads.items():
        (model_dir / fname).write_bytes(payload)
        expected_hashes[fname] = hashlib.sha256(payload).hexdigest()
    registry.register_expected_hashes("model_download", expected_hashes)
    _VERIFICATION_CACHE.clear()

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, pkg=None: (
            importlib.machinery.ModuleSpec("onnxruntime", loader=None)
            if name == "onnxruntime"
            else real_find_spec(name, pkg)
        ),
    )
    DownloadManager.reset_instance()
    assert is_onnxruntime_available() is True

    # Simulate runtime ImportError inside SemanticEmbeddingManager.generate_embedding
    manager = SemanticEmbeddingManager(
        db, model_path=str(model_dir), bypass_validation=True
    )
    manager.is_model_valid = True
    registry._models[f"tokenizer_{model_dir}"] = lambda *a, **kw: {}
    with patch.object(
        registry,
        "get_onnx_session",
        side_effect=ModuleNotFoundError("No module named 'onnxruntime'"),
    ):
        vec = manager.generate_embedding("Tax invoice 2025")
        assert len(vec) == 384

    health = registry.get_model_health("onnx_embeddings")
    assert health["status"] == "DEGRADED_FALLBACK"
    assert health["suggested_recovery_action"] == "INSTALL_ML_EXTRA"
    assert is_onnxruntime_available() is False

    rt = get_ai_runtime_state(
        SimpleNamespace(AI_CONSENT_GRANTED=True), model_dir=str(model_dir)
    )
    assert rt["state"] == "fallback"
    assert rt["badge"] == "[AI: Fallback]"
    assert rt["onnxruntime_available"] is False

    registry.record_model_health("onnx_embeddings", status="HEALTHY")
    DownloadManager.reset_instance()
