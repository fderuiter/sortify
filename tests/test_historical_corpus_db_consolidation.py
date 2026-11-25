"""Tests for consolidated historical corpus queries on Database."""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.core import analyzer_strategies
from app.core.analyzer import pre_fetch_historical_corpus
from app.core.analyzer_strategies import (
    GenerativeNamingStrategy,
    InMemoryModelMetadataStore,
)
from app.core.db import Database
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.db_worker import DBWorker
from app.core.semantic_embeddings import ModelProperties, SemanticEmbeddingManager


@pytest.fixture
def isolated_db(tmp_path: Path):
    """Provide an isolated Database and DBWorker backed by tmp_path."""
    worker = DBWorker()
    db_file = tmp_path / "historical_consolidation.db"
    database = Database(db_file, worker)
    try:
        yield str(tmp_path / "workspace"), database
    finally:
        worker.stop()
        clear_connection_cache()


def test_get_verified_historical_vectors_join_modes_and_extension_filtering(
    isolated_db: tuple[str, Database],
) -> None:
    """Verify INNER vs LEFT JOIN behavior and pre-decryption extension filtering."""
    base_dir, db = isolated_db

    db.upsert_document(base_dir, "vec_doc.txt", "h1", "verified vectorized text")
    db.set_user_verified_target(base_dir, "h1", "Finance")
    db.upsert_document_vectors(base_dir, [("vec_doc.txt", [1.0, 0.0, 0.0, 0.0])])

    db.upsert_document(base_dir, "unvec_doc.txt", "h2", "verified unvectorized text")
    db.set_user_verified_target(base_dir, "h2", "Legal")

    db.upsert_document(base_dir, "photo.png", "h3", "image placeholder")
    db.set_user_verified_target(base_dir, "h3", "Media")
    db.upsert_document_vectors(base_dir, [("photo.png", [0.0, 1.0, 0.0, 0.0])])

    db.upsert_document(base_dir, "unverified.txt", "h4", "unverified text")
    db.upsert_document_vectors(base_dir, [("unverified.txt", [0.0, 0.0, 1.0, 0.0])])

    inner_rows = db.get_verified_historical_vectors(
        base_dir, include_unvectorized=False
    )
    inner_by_fp = {fp: (folder, vec) for fp, folder, vec in inner_rows}
    assert set(inner_by_fp.keys()) == {"vec_doc.txt", "photo.png"}
    assert inner_by_fp["vec_doc.txt"][0] == "Finance"
    assert inner_by_fp["vec_doc.txt"][1] == [1.0, 0.0, 0.0, 0.0]

    left_rows = db.get_verified_historical_vectors(base_dir, include_unvectorized=True)
    left_by_fp = {fp: (folder, vec) for fp, folder, vec in left_rows}
    assert set(left_by_fp.keys()) == {"vec_doc.txt", "unvec_doc.txt", "photo.png"}
    assert left_by_fp["unvec_doc.txt"] == ("Legal", None)
    # Unvectorized rows in LEFT JOIN must not be flagged as corrupted vectors
    assert db.get_corrupted_vectors_by_base_dir(base_dir) == []

    txt_only = db.get_verified_historical_vectors(
        base_dir,
        include_unvectorized=False,
        allowed_extensions={".txt"},
    )
    assert [fp for fp, _, _ in txt_only] == ["vec_doc.txt"]

    empty_allowed = db.get_verified_historical_vectors(
        base_dir,
        include_unvectorized=False,
        allowed_extensions=set(),
    )
    assert empty_allowed == []


def test_get_verified_historical_vectors_parallel_decryption_and_corruption_tracking(
    isolated_db: tuple[str, Database],
) -> None:
    """Verify >32 rows use get_decryption_executor and track failed vector decryptions."""
    base_dir, db = isolated_db

    docs_batch = [
        (base_dir, f"doc_{i}.txt", f"hash_{i}", f"document body {i}") for i in range(40)
    ]
    db.upsert_documents(docs_batch)
    for i in range(40):
        db.set_user_verified_target(base_dir, f"hash_{i}", f"Folder_{i % 4}")

    valid_vectors = [(f"doc_{i}.txt", [0.25, 0.75, 0.0, 0.0]) for i in range(38)]
    db.upsert_document_vectors(base_dir, valid_vectors)

    conn = get_db_connection(db.db_path)
    with conn:
        for corrupt_fp in ("doc_38.txt", "doc_39.txt"):
            conn.execute(
                "INSERT INTO document_vectors (base_dir, filepath, vector) VALUES (?, ?, ?)",
                (base_dir, corrupt_fp, "corrupt-vector-payload"),
            )

    with patch.object(
        analyzer_strategies,
        "get_decryption_executor",
        wraps=analyzer_strategies.get_decryption_executor,
    ) as spy_executor:
        rows_untracked = db.get_verified_historical_vectors(
            base_dir,
            include_unvectorized=False,
            track_corrupted=False,
        )
        assert spy_executor.call_count == 1

    assert len(rows_untracked) == 40
    assert db.get_corrupted_vectors_by_base_dir(base_dir) == []

    rows_tracked = db.get_verified_historical_vectors(
        base_dir,
        include_unvectorized=False,
        track_corrupted=True,
    )
    by_fp = {fp: vec for fp, _, vec in rows_tracked}
    assert by_fp["doc_0.txt"] == [0.25, 0.75, 0.0, 0.0]
    assert by_fp["doc_38.txt"] is None
    assert by_fp["doc_39.txt"] is None
    assert set(db.get_corrupted_vectors_by_base_dir(base_dir)) == {
        "doc_38.txt",
        "doc_39.txt",
    }


def test_get_documents_by_filepaths_batches_50_and_handles_corrupted_text(
    isolated_db: tuple[str, Database],
) -> None:
    """Verify 50-item SQLite chunking and empty-string fallback on per-row decrypt_text errors."""
    base_dir, db = isolated_db

    total_docs = 120
    docs_batch = [
        (base_dir, f"batch_{i}.txt", f"bh_{i}", f"clean text {i}")
        for i in range(total_docs)
    ]
    db.upsert_documents(docs_batch)

    conn = get_db_connection(db.db_path)
    with conn:
        for bad_fp in ("batch_5.txt", "batch_77.txt"):
            conn.execute(
                "UPDATE documents SET extracted_text = ? WHERE base_dir = ? AND filepath = ?",
                ("corrupted-ciphertext-payload", base_dir, bad_fp),
            )

    requested_fps = [f"batch_{i}.txt" for i in range(total_docs)]
    rows = db.get_documents_by_filepaths(base_dir, requested_fps)
    text_by_fp = dict(rows)

    assert len(text_by_fp) == total_docs
    assert text_by_fp["batch_0.txt"] == "clean text 0"
    assert text_by_fp["batch_119.txt"] == "clean text 119"
    assert text_by_fp["batch_5.txt"] == ""
    assert text_by_fp["batch_77.txt"] == ""


def test_generative_naming_corrupted_snippet_falls_back_to_basename_without_ciphertext_leak(
    isolated_db: tuple[str, Database], tmp_path: Path
) -> None:
    """Verify corrupted exemplar text falls back to file basename instead of leaking raw ciphertext."""
    base_dir, db = isolated_db

    db.set_model_metadata("active_model_signature", "sig_corrupt_check")
    db.set_model_metadata("active_model_dimensions", "4")
    db.set_model_metadata("active_model_version", "1.0.0")

    db.upsert_document(
        base_dir,
        "sub/annual_budget_summary.txt",
        "h_good_vec_bad_text",
        "original plaintext budget summary",
    )
    db.set_user_verified_target(base_dir, "h_good_vec_bad_text", "Finance")
    db.upsert_document_vectors(
        base_dir,
        [("sub/annual_budget_summary.txt", [0.6, 0.8, 0.0, 0.0])],
    )

    db.upsert_document(
        base_dir,
        "sub/broken_vector.txt",
        "h_bad_vec",
        "broken vector document",
    )
    db.set_user_verified_target(base_dir, "h_bad_vec", "Operations")

    raw_ciphertext_marker = "CORRUPTED_CIPHERTEXT_DO_NOT_LEAK"
    conn = get_db_connection(db.db_path)
    with conn:
        conn.execute(
            "UPDATE documents SET extracted_text = ? WHERE base_dir = ? AND filepath = ?",
            (raw_ciphertext_marker, base_dir, "sub/annual_budget_summary.txt"),
        )
        conn.execute(
            "INSERT INTO document_vectors (base_dir, filepath, vector) VALUES (?, ?, ?)",
            (base_dir, "sub/broken_vector.txt", "invalid-vector-blob"),
        )
    db.invalidate_cache()

    strategy = GenerativeNamingStrategy()
    strategy.set_db_context(db, base_dir)
    strategy.stop_words = {"the", "and"}
    strategy.model_path = str(tmp_path)
    strategy.generator = MagicMock()
    strategy._model_initialized = True

    valid_props = ModelProperties(
        signature="sig_corrupt_check",
        dimensions=4,
        version="1.0.0",
        is_valid=True,
    )

    with (
        patch(
            "app.core.semantic_embeddings.get_active_model_properties",
            return_value=valid_props,
        ),
        patch.object(
            SemanticEmbeddingManager,
            "generate_embedding",
            return_value=[1.0, 0.0, 0.0, 0.0],
        ),
        patch.object(
            strategy, "_run_prompt", return_value="Quarterly Budget"
        ) as mock_run_prompt,
    ):
        folder = strategy._get_cluster_keywords(
            ["quarterly revenue forecast expense report"]
        )

    assert folder == "Quarterly Budget"
    assert mock_run_prompt.called
    prompt_text = mock_run_prompt.call_args[0][0]
    assert raw_ciphertext_marker not in prompt_text
    assert "annual_budget_summary.txt" in prompt_text
    assert "sub/broken_vector.txt" in db.get_corrupted_vectors_by_base_dir(base_dir)


def test_consolidated_callers_avoid_get_db_connection_and_guard_stubs() -> None:
    """Verify zero direct get_db_connection references and stub safety for InMemoryModelMetadataStore/MagicMock."""
    assert "get_db_connection" not in inspect.getsource(pre_fetch_historical_corpus)
    assert "get_db_connection" not in inspect.getsource(
        GenerativeNamingStrategy._get_cluster_keywords
    )

    with patch("app.core.db_conn.get_db_connection") as mock_get_conn:
        in_mem_stub = InMemoryModelMetadataStore(
            {
                "active_model_signature": "sig",
                "active_model_dimensions": "4",
                "active_model_version": "1.0",
            }
        )
        batch_stub = pre_fetch_historical_corpus(
            in_mem_stub,
            base_dir="/nonexistent",
            filenames=["a.txt"],
            documents=["sample document"],
        )
        assert len(batch_stub["examples"]) == 0

        magic_stub = MagicMock()
        magic_stub.get_model_metadata.return_value = None
        batch_magic = pre_fetch_historical_corpus(
            magic_stub,
            base_dir="/nonexistent",
            filenames=["a.txt"],
            documents=["sample document"],
        )
        assert len(batch_magic["examples"]) == 0

        strategy = GenerativeNamingStrategy()
        strategy.stop_words = {"the", "and"}
        strategy.max_features = 3
        strategy.generator = MagicMock()
        strategy._model_initialized = True

        for stub_db in (in_mem_stub, MagicMock()):
            strategy.set_db_context(stub_db, "/nonexistent")
            with patch.object(strategy, "_run_prompt", return_value="Stub Folder"):
                assert (
                    strategy._get_cluster_keywords(["alpha beta gamma"])
                    == "Stub Folder"
                )

        mock_get_conn.assert_not_called()
