"""Unit test suite for Pydantic domain schema contracts across event loop boundaries."""

import os
import time

import pytest
from pydantic import ValidationError

from app.core.domain_contracts import (
    CorpusExampleModel,
    CorpusPreFetchBatchModel,
    QuarantineRecordModel,
    SortingPlanModel,
    SortingPlanNodeModel,
    VectorBatchPayloadModel,
    validate_corpus_prefetch_batch,
    validate_jev_classification_result,
    validate_quarantine_record,
    validate_sorting_plan_node,
    validate_vector_batch_payload,
)
from app.core.exceptions import SchemaValidationError
from app.core.jev_classifier import JevClassificationResult


def test_quarantine_record_model_validation():
    """Test QuarantineRecordModel validation and dict subscript compatibility."""
    payload = {
        "job_id": "qjob_1234567890ab",
        "base_dir": "/tmp/test_base",
        "original_filepath": "docs/report.pdf",
        "staged_filepath": "/tmp/test_base/_Quarantine_Staging/qjob_1234567890ab_report.pdf",
        "file_hash": "a1b2c3d4e5f6",
        "status": "STAGED",
        "policy_action": "quarantine",
        "audit_log": [{"status": "STAGED", "timestamp": 123456.78}],
    }

    model = validate_quarantine_record(payload)
    assert isinstance(model, QuarantineRecordModel)
    assert model.job_id == "qjob_1234567890ab"
    assert model["status"] == "STAGED"
    assert model.get("policy_action") == "quarantine"
    assert "job_id" in model
    assert model.dict()["job_id"] == "qjob_1234567890ab"


def test_quarantine_record_model_invalid_payload():
    """Test that malformed quarantine records trigger SchemaValidationError and ValidationError."""
    invalid_payload = {
        "base_dir": "/tmp/test_base",
        "status": "INVALID",
        # missing required job_id
    }

    with pytest.raises((SchemaValidationError, ValidationError)) as exc_info:
        validate_quarantine_record(invalid_payload)

    assert "QuarantineRecordModel" in str(exc_info.value) or "job_id" in str(
        exc_info.value
    )


def test_jev_classification_result_validation():
    """Test JevClassificationResult validation at event loop boundaries."""
    valid_payload = {
        "category": "Invoices",
        "confidence": 0.95,
        "is_classified": True,
        "sensitivity_rating": "LOW",
        "sensitivity_score": 0.1,
        "archival_priority": 1,
        "archival_priority_score": 0.2,
    }

    result = validate_jev_classification_result(valid_payload)
    assert isinstance(result, JevClassificationResult)
    assert result.category == "Invoices"
    assert result.confidence == 0.95
    assert result.is_classified is True

    invalid_payload = {
        "category": "Invoices",
        "confidence": "not_a_number",  # Invalid type for float
    }

    with pytest.raises((SchemaValidationError, ValidationError)):
        validate_jev_classification_result(invalid_payload)


def test_corpus_prefetch_batch_model_validation():
    """Test CorpusPreFetchBatchModel validation and helper methods."""
    valid_payload = {
        "model_metadata": {
            "active_model_signature": "sig_123",
            "active_model_dimensions": 384,
            "active_model_version": "v1.0",
        },
        "examples": [
            {
                "filepath": "docs/inv.pdf",
                "user_verified_target_path": "Accounting/inv.pdf",
                "vector": [0.1, 0.2, 0.3],
                "text": "Invoice text content",
            }
        ],
    }

    batch = validate_corpus_prefetch_batch(valid_payload)
    assert isinstance(batch, CorpusPreFetchBatchModel)
    assert batch.get("model_metadata")["active_model_dimensions"] == 384
    assert len(batch.get("examples")) == 1
    assert isinstance(batch["examples"][0], CorpusExampleModel)

    # Test clear helper
    batch.clear()
    assert len(batch.examples) == 0
    assert len(batch.model_metadata) == 0

    invalid_payload = {
        "model_metadata": "not_a_dict",
        "examples": "not_a_list",
    }

    with pytest.raises((SchemaValidationError, ValidationError)):
        validate_corpus_prefetch_batch(invalid_payload)


def test_corpus_example_model_dict_compatibility():
    """Test CorpusExampleModel dict subscripting and get method compatibility."""
    example = CorpusExampleModel(
        filepath="docs/test.pdf",
        user_verified_target_path="Verified/test.pdf",
        vector=[0.1, 0.2],
        text="Sample text",
    )
    assert example["filepath"] == "docs/test.pdf"
    assert example.get("user_verified_target_path") == "Verified/test.pdf"
    assert example.get("non_existent", "default") == "default"
    assert "text" in example
    assert example.dict()["text"] == "Sample text"


def test_ipc_payload_serialization_with_domain_models():
    """Verify EphemeralSessionCrypto and encrypt_ipc_payload serialize domain models without error."""
    from app.core.crypto import (
        EphemeralSessionCrypto,
        decrypt_ipc_payload,
        encrypt_ipc_payload,
    )

    batch = validate_corpus_prefetch_batch(
        {
            "model_metadata": {"version": "1.0"},
            "examples": [
                {
                    "filepath": "a.txt",
                    "user_verified_target_path": "Cat/a.txt",
                    "vector": [1.0, 0.0],
                    "text": "sample",
                }
            ],
        }
    )

    payload = {"pre_fetched_corpus": batch}

    # Test EphemeralSessionCrypto
    session_crypto = EphemeralSessionCrypto()
    encrypted = session_crypto.encrypt_payload(payload)
    decrypted = session_crypto.decrypt_payload(encrypted)
    assert decrypted["pre_fetched_corpus"]["model_metadata"]["version"] == "1.0"

    # Test encrypt_ipc_payload / decrypt_ipc_payload
    key = session_crypto.session_key
    ipc_encrypted = encrypt_ipc_payload(payload, key)
    ipc_decrypted = decrypt_ipc_payload(ipc_encrypted, key)
    assert ipc_decrypted["pre_fetched_corpus"]["examples"][0]["filepath"] == "a.txt"


def test_sorting_plan_node_and_plan_validation():
    """Test SortingPlanNodeModel and SortingPlanModel contracts."""
    node_payload = {
        "__type__": "file",
        "routed_by": "jev_classifier",
        "category": "Financial",
        "confidence": 0.92,
        "sensitivity_rating": "HIGH",
    }

    node = validate_sorting_plan_node(node_payload)
    assert isinstance(node, SortingPlanNodeModel)
    assert node.node_type == "file"
    assert node.category == "Financial"
    assert node["confidence"] == 0.92

    plan_payload = {"nodes": {"Financial": {"report.pdf": node.dict()}}}

    plan_model = SortingPlanModel.model_validate(plan_payload)
    assert "Financial" in plan_model
    assert plan_model.get("nodes")["Financial"]["report.pdf"]["category"] == "Financial"


def test_vector_batch_payload_validation():
    """Test VectorBatchPayloadModel validation."""
    payload = {
        "base_dir": "/tmp/workspace",
        "vectors": {
            "file1.pdf": [0.1, 0.2, 0.3],
            "file2.pdf": None,
        },
        "metadata": {"count": 2},
    }

    model = validate_vector_batch_payload(payload)
    assert isinstance(model, VectorBatchPayloadModel)
    assert model.base_dir == "/tmp/workspace"
    assert len(model.vectors) == 2

    invalid_payload = {
        "vectors": "not_a_dict",
    }

    with pytest.raises((SchemaValidationError, ValidationError)):
        validate_vector_batch_payload(invalid_payload)


def test_pydantic_validation_latency_sla():
    """Verify that model validation overhead per hand-off does not exceed 2 milliseconds."""
    quarantine_payload = {
        "job_id": "qjob_perf_test_123",
        "base_dir": "/tmp/workspace",
        "original_filepath": "sample.pdf",
        "staged_filepath": "/tmp/workspace/_Quarantine_Staging/qjob_perf_test_123_sample.pdf",
        "file_hash": "abcdef123456",
        "status": "STAGED",
        "policy_action": "redact",
        "audit_log": [{"timestamp": time.time(), "status": "STAGED"}],
    }

    # Warm up schema compilation before benchmarking
    validate_quarantine_record(quarantine_payload)

    iterations = 500
    start = time.perf_counter()
    for _ in range(iterations):
        validate_quarantine_record(quarantine_payload)
    total_time_ms = (time.perf_counter() - start) * 1000.0
    avg_latency_ms = total_time_ms / iterations

    # Sub-2ms SLA per hand-off (relaxed under parallel xdist or CI runner virtualization)
    is_parallel_or_ci = (
        "PYTEST_XDIST_WORKER" in os.environ
        or "CI" in os.environ
        or os.environ.get("GITHUB_ACTIONS") == "true"
    )
    max_allowed_ms = 25.0 if is_parallel_or_ci else 2.0
    assert avg_latency_ms < max_allowed_ms, (
        f"Average validation latency too high: {avg_latency_ms:.4f} ms (max {max_allowed_ms} ms)"
    )
