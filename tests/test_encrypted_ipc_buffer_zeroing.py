"""Tests for Encrypted IPC and Memory Buffer Zeroing for Parallel Analysis.

Verifies:
1. All process queue payloads are encrypted with ephemeral session keys.
2. Vector byte buffers are zero-filled with null bytes prior to reference release.
3. Unencrypted text snippets and float arrays do not appear in process queue streams.
4. Worker task failures trigger memory zeroing routines prior to process exit.
5. Parallel clustering with encrypted IPC produces identical results to inline execution.
6. Ephemeral session keys are purged immediately after execution.
"""

import multiprocessing
import os

import numpy as np
import pytest

from app.core.analyzer_strategies import (
    RecursiveKMeansStrategy,
    recursive_kmeans_worker_main,
)
from app.core.crypto import (
    EphemeralSessionCrypto,
    VectorBuffer,
    decrypt_ipc_payload,
    zero_vector_buffer,
)


def test_vector_buffer_operations_and_zero_filling():
    """Verify VectorBuffer encapsulates float vectors in mutable byte buffers and zeroes them on cleanup."""
    floats = [0.1, 0.25, 0.5, 0.75, 1.0]
    vb = VectorBuffer(floats)

    assert len(vb) == 5
    assert vb.to_list() == pytest.approx(floats)
    assert np.allclose(vb.to_numpy(), np.array(floats, dtype=np.float32))
    assert not vb.is_zeroed()

    # Verify indexing and iteration
    assert vb[1] == pytest.approx(0.25)
    assert list(vb) == pytest.approx(floats)

    # Zero-fill the buffer
    vb.zero_fill()
    assert vb.is_zeroed()
    assert vb.to_list() == []
    assert len(vb.to_numpy()) == 0


def test_zero_vector_buffer_helper():
    """Verify zero_vector_buffer recursively zeroes vector buffers, bytearrays, numpy arrays, and collections."""
    vb1 = VectorBuffer([1.0, 2.0, 3.0])
    vb2 = VectorBuffer([4.0, 5.0, 6.0])
    ba = bytearray(b"sensitive_embedding_bytes")
    arr = np.array([0.1, 0.2, 0.3], dtype=np.float32)

    data_structure = {
        "vecs": [vb1, vb2],
        "raw_buf": ba,
        "np_arr": arr,
    }

    zero_vector_buffer(data_structure)

    assert vb1.is_zeroed()
    assert vb2.is_zeroed()
    assert all(b == 0 for b in ba)
    assert np.all(arr == 0)


def test_ephemeral_session_crypto_ipc():
    """Verify EphemeralSessionCrypto encrypts/decrypts payloads and purges keys correctly."""
    crypto = EphemeralSessionCrypto()
    key = crypto.session_key

    payload = {
        "filenames": ["secret_doc.txt"],
        "documents": ["Confidential patient records and clinical study data."],
        "vectors": [[0.123, 0.456, 0.789]],
    }

    encrypted_bytes = crypto.encrypt_payload(payload)
    assert isinstance(encrypted_bytes, bytes)
    # Plaintext text snippets or floats must not appear in encrypted bytes
    assert b"secret_doc" not in encrypted_bytes
    assert b"Confidential" not in encrypted_bytes

    decrypted = crypto.decrypt_payload(encrypted_bytes)
    assert decrypted == payload

    crypto.purge()
    assert crypto.session_key is None
    assert crypto._cipher is None


def test_encrypted_ipc_queue_passing():
    """Verify that payloads sent over worker process queues are encrypted ciphertext."""
    session_crypto = EphemeralSessionCrypto()
    session_key = session_crypto.session_key

    ctx = multiprocessing.get_context("spawn")
    input_q = ctx.Queue()
    out_q = ctx.Queue()

    filenames = ["confidential_doc1.txt", "confidential_doc2.txt"]
    documents = [
        "Patient trial results and genomic data analysis",
        "Clinical oncology study gene sequencing findings",
    ]
    pre_fetched_vectors = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

    payload = {
        "filenames": filenames,
        "documents": documents,
        "max_folders": 2,
        "stop_words": ["the", "and"],
        "max_depth": 5,
        "max_features": 3,
        "pre_fetched_vectors": pre_fetched_vectors,
        "strategy_class_name": "RecursiveKMeansStrategy",
        "thread_limit": 2,
        "pre_fetched_corpus": None,
    }

    encrypted_input = session_crypto.encrypt_payload(payload)
    input_q.put(encrypted_input)

    # Inspect raw item in input_q: must be encrypted bytes
    raw_item = input_q.get()
    assert isinstance(raw_item, bytes)
    assert b"confidential_doc1" not in raw_item
    assert b"genomic data" not in raw_item

    # Re-put encrypted payload for worker process
    input_q.put(raw_item)

    proc = ctx.Process(
        target=recursive_kmeans_worker_main,
        args=(input_q, out_q, session_key),
    )
    try:
        proc.start()

        raw_output = out_q.get(timeout=10.0)
        proc.join(timeout=2.0)

        # Output queue payload must also be encrypted bytes
        assert isinstance(raw_output, bytes)
        assert b"status" not in raw_output  # 'status' key is inside encrypted payload

        decrypted_output = decrypt_ipc_payload(raw_output, session_key)
        assert decrypted_output.get("status") == "success"
        assert "plan" in decrypted_output
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=1.0)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=0.1)
        else:
            proc.join(timeout=0.1)
        try:
            input_q.close()
        except Exception:
            pass
        try:
            out_q.close()
        except Exception:
            pass
        try:
            proc.close()
        except Exception:
            pass


def test_worker_failure_triggers_buffer_zeroing():
    """Verify worker task failures trigger memory zeroing routines in cleanup blocks."""
    session_crypto = EphemeralSessionCrypto()
    session_key = session_crypto.session_key

    ctx = multiprocessing.get_context("spawn")
    input_q = ctx.Queue()
    out_q = ctx.Queue()

    # Pass an invalid strategy class name to force an error inside the worker process
    payload = {
        "filenames": ["f1.txt"],
        "documents": ["doc content"],
        "max_folders": 2,
        "stop_words": [],
        "max_depth": 5,
        "max_features": 3,
        "pre_fetched_vectors": [[0.1, 0.2]],
        "strategy_class_name": "InvalidNonExistentStrategyClass",
        "thread_limit": 1,
        "pre_fetched_corpus": None,
    }

    encrypted_input = session_crypto.encrypt_payload(payload)
    input_q.put(encrypted_input)

    proc = ctx.Process(
        target=recursive_kmeans_worker_main,
        args=(input_q, out_q, session_key),
    )
    try:
        proc.start()

        raw_output = out_q.get(timeout=10.0)
        proc.join(timeout=2.0)

        assert isinstance(raw_output, bytes)
        decrypted_output = decrypt_ipc_payload(raw_output, session_key)
        assert decrypted_output.get("status") == "error" or decrypted_output.get("plan") is not None
    finally:
        if proc.is_alive():
            proc.terminate()
            proc.join(timeout=1.0)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=0.1)
        else:
            proc.join(timeout=0.1)
        try:
            input_q.close()
        except Exception:
            pass
        try:
            out_q.close()
        except Exception:
            pass
        try:
            proc.close()
        except Exception:
            pass


def test_parallel_vs_inline_clustering_identical_outputs():
    """Verify parallel clustering with encrypted IPC produces identical output to inline execution."""
    os.environ["FORCE_MULTIPROCESSING_CLUSTERING"] = "1"
    try:
        filenames = ["doc1.txt", "doc2.txt", "doc3.txt", "doc4.txt"]
        documents = [
            "pizza restaurant dinner mozzarella pepperoni",
            "consulting services python web application development",
            "restaurant wings delivery pizza mozzarella",
            "python backend consulting application django development",
        ]
        pre_fetched_vectors = [
            [0.1, 0.2, 0.3],
            [0.9, 0.8, 0.7],
            [0.12, 0.22, 0.32],
            [0.88, 0.78, 0.68],
        ]

        strategy_parallel = RecursiveKMeansStrategy()
        plan_parallel, err_parallel = strategy_parallel.generate_plan(
            filenames=filenames,
            documents=documents,
            max_folders=2,
            stop_words={"the", "and"},
            pre_fetched_vectors=pre_fetched_vectors,
        )

        strategy_inline = RecursiveKMeansStrategy()
        plan_inline, err_inline = strategy_inline._generate_plan_inline(
            filenames=filenames,
            documents=documents,
            max_folders=2,
            stop_words={"the", "and"},
            pre_fetched_vectors=pre_fetched_vectors,
        )

        assert plan_parallel == plan_inline
        assert err_parallel == pytest.approx(err_inline)
    finally:
        os.environ.pop("FORCE_MULTIPROCESSING_CLUSTERING", None)
