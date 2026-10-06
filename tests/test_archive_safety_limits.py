"""Tests for inline streaming guards and resource limits in forensic scanner archive unpacking."""

import hashlib
import os
import tarfile
import zipfile

import pytest

from app.core.exceptions import ArchiveSafetyError
from app.core.forensic_scanner import ForensicScanner
from app.core.quarantine_interceptor import QuarantineInterceptorService


def test_unpack_zip_max_files_limit_aborts_and_cleans_up(tmp_path):
    """Verify that unpacking a zip archive exceeding max_files aborts and cleans up staging files."""
    zip_path = tmp_path / "multi_file.zip"
    dest_dir = tmp_path / "staging_unpack"

    # Create zip with 5 files
    with zipfile.ZipFile(zip_path, "w") as zf:
        for i in range(5):
            zf.writestr(f"file_{i}.txt", f"Content {i}")

    scanner = ForensicScanner(max_files=3)
    with pytest.raises(ArchiveSafetyError) as exc_info:
        scanner.unpack_archive(str(zip_path), str(dest_dir))

    assert "file count" in str(exc_info.value).lower()
    # Check cleanup: no files should remain in dest_dir
    extracted_items = list(dest_dir.glob("**/*"))
    assert len(extracted_items) == 0


def test_unpack_tar_max_files_limit_aborts_and_cleans_up(tmp_path):
    """Verify that unpacking a tar archive exceeding max_files aborts and cleans up staging files."""
    tar_path = tmp_path / "multi_file.tar"
    dest_dir = tmp_path / "staging_tar_unpack"

    # Create tar with 5 files
    with tarfile.open(tar_path, "w") as tf:
        for i in range(5):
            content = f"Content {i}".encode()
            ti = tarfile.TarInfo(name=f"file_{i}.txt")
            ti.size = len(content)
            tf.addfile(ti, fileobj=zipfile.io.BytesIO(content))

    scanner = ForensicScanner(max_files=2)
    with pytest.raises(ArchiveSafetyError) as exc_info:
        scanner.unpack_archive(str(tar_path), str(dest_dir))

    assert "file count" in str(exc_info.value).lower()
    extracted_items = list(dest_dir.glob("**/*"))
    assert len(extracted_items) == 0


def test_unpack_zip_max_total_size_limit_aborts_and_cleans_up(tmp_path):
    """Verify that unpacking a zip archive exceeding max_total_size_bytes aborts and cleans up."""
    zip_path = tmp_path / "large_total.zip"
    dest_dir = tmp_path / "staging_unpack_total"

    chunk = b"A" * 1024 * 10  # 10 KB
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("f1.txt", chunk)
        zf.writestr("f2.txt", chunk)
        zf.writestr("f3.txt", chunk)

    # Allow max 15 KB total uncompressed
    scanner = ForensicScanner(max_total_size_bytes=15 * 1024)
    with pytest.raises(ArchiveSafetyError) as exc_info:
        scanner.unpack_archive(str(zip_path), str(dest_dir))

    assert "total uncompressed" in str(exc_info.value).lower()
    extracted_items = list(dest_dir.glob("**/*"))
    assert len(extracted_items) == 0


def test_unpack_zip_max_single_file_size_limit_aborts_and_cleans_up(tmp_path):
    """Verify that unpacking an archive with a single file exceeding max_file_size_bytes aborts."""
    zip_path = tmp_path / "single_large.zip"
    dest_dir = tmp_path / "staging_unpack_single"

    large_content = b"X" * 1024 * 50  # 50 KB
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("huge_doc.txt", large_content)

    # Set single file size limit to 20 KB
    scanner = ForensicScanner(max_file_size_bytes=20 * 1024)
    with pytest.raises(ArchiveSafetyError) as exc_info:
        scanner.unpack_archive(str(zip_path), str(dest_dir))

    assert "file uncompressed size" in str(exc_info.value).lower() or "exceeds limit" in str(exc_info.value).lower()
    extracted_items = list(dest_dir.glob("**/*"))
    assert len(extracted_items) == 0


def test_unpack_zip_expansion_ratio_limit_aborts_and_raises(tmp_path):
    """Verify that unpacking an archive exceeding max_ratio raises ArchiveSafetyError and cleans up."""
    zip_path = tmp_path / "zip_bomb.zip"
    dest_dir = tmp_path / "staging_unpack_bomb"

    # Highly compressible content (1 MB of zeros compresses to ~1 KB)
    bomb_content = b"\x00" * (1024 * 1024)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.txt", bomb_content)

    # Set maximum expansion ratio to 5:1
    scanner = ForensicScanner(max_ratio=5.0)
    with pytest.raises(ArchiveSafetyError) as exc_info:
        scanner.unpack_archive(str(zip_path), str(dest_dir))

    assert "expansion ratio" in str(exc_info.value).lower()
    extracted_items = list(dest_dir.glob("**/*"))
    assert len(extracted_items) == 0


def test_unpack_valid_archive_within_limits_succeeds_and_maintains_hashes(tmp_path):
    """Verify that valid archives within configured limits extract completely and maintain hashes."""
    zip_path = tmp_path / "valid_doc_archive.zip"
    dest_dir = tmp_path / "staging_unpack_valid"

    content_f1 = b"Hello, this is standard document 1."
    content_f2 = b"Hello, this is standard document 2 with additional data."

    hash_f1 = hashlib.sha256(content_f1).hexdigest()
    hash_f2 = hashlib.sha256(content_f2).hexdigest()

    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("doc1.txt", content_f1)
        zf.writestr("doc2.txt", content_f2)

    scanner = ForensicScanner()
    extracted = scanner.unpack_archive(str(zip_path), str(dest_dir))

    assert len(extracted) == 2
    for ext_path in extracted:
        assert os.path.exists(ext_path)
        with open(ext_path, "rb") as f:
            data = f.read()
        computed_hash = hashlib.sha256(data).hexdigest()
        assert computed_hash in (hash_f1, hash_f2)


def test_quarantine_interceptor_captures_archive_safety_error(tmp_path):
    """Verify QuarantineInterceptorService captures ArchiveSafetyError and records descriptive job status."""
    from app.core.db import Database
    from app.core.db_worker import DBWorker

    db_worker = DBWorker()
    try:
        db = Database(tmp_path / "test_quarantine.db", db_worker)
        sample_dir = tmp_path / "quarantine_sample"
        sample_dir.mkdir()

        # Create a zip archive with 10 files
        zip_path = sample_dir / "excessive_files.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            for i in range(10):
                zf.writestr(f"item_{i}.txt", f"Data {i}")

        service = QuarantineInterceptorService(db=db)
        # Configure scanner on service to have strict file limit
        service.forensic_scanner.max_files = 3

        staged_info = service.stage_incoming_file(source_path=str(zip_path), base_dir=str(sample_dir))
        job_id = staged_info["job_id"]

        record = service.process_quarantine_job(job_id)

        assert record.status in ("DEAD_LETTER_QUEUE", "MANUAL_REVIEW_REQUIRED")
        assert "Archive safety violation" in str(record.error_message)
    finally:
        db_worker.stop()
        from app.core.db_conn import clear_connection_cache
        clear_connection_cache()
