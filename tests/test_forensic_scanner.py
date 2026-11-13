"""Tests for ForensicScanner: deep scanning, archive unpacking, email parsing, and SHA-256 provenance."""

import os
import tempfile
import zipfile

from app.core.forensic_scanner import ForensicScanner


def test_sha256_computation():
    with tempfile.NamedTemporaryFile("w", delete=False) as f:
        f.write("Clinical Trial Protocol Testing File")
        f_path = f.name

    try:
        digest = ForensicScanner.compute_sha256(f_path)
        assert isinstance(digest, str)
        assert len(digest) == 64
    finally:
        if os.path.exists(f_path):
            os.remove(f_path)


def test_unpack_zip_archive_safely():
    with tempfile.TemporaryDirectory() as tmpdir:
        scanner = ForensicScanner(temp_staging_dir=os.path.join(tmpdir, "staging"))

        zip_path = os.path.join(tmpdir, "site_docs.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("nested/1572.txt", "STATEMENT OF INVESTIGATOR Form FDA 1572")
            zf.writestr("protocol.txt", "Clinical Study Protocol PROTO-101")

        dest = os.path.join(tmpdir, "extracted")
        extracted = scanner.unpack_archive(zip_path, dest)

        assert len(extracted) == 2
        assert any("1572.txt" in f for f in extracted)
        assert any("protocol.txt" in f for f in extracted)
        scanner.cleanup()


def test_parse_eml_email():
    with tempfile.TemporaryDirectory() as tmpdir:
        scanner = ForensicScanner(temp_staging_dir=os.path.join(tmpdir, "staging"))

        eml_content = (
            b"From: sponsor@pharma.com\r\n"
            b"To: site@hospital.org\r\n"
            b"Subject: Protocol ONCO-99 Amendment 2 Approval\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Dear Investigator, Please find the approved protocol amendment for study ONCO-99."
        )
        eml_path = os.path.join(tmpdir, "message.eml")
        with open(eml_path, "wb") as f:
            f.write(eml_content)

        dest = os.path.join(tmpdir, "eml_out")
        body, attachments = scanner.parse_eml_file(eml_path, dest)

        assert "ONCO-99" in body
        assert "sponsor@pharma.com" in body
        scanner.cleanup()


def test_scan_drive_with_deduplication():
    with tempfile.TemporaryDirectory() as tmpdir:
        scanner = ForensicScanner(temp_staging_dir=os.path.join(tmpdir, "staging"))

        # Create original doc
        doc1 = os.path.join(tmpdir, "study1_1572.txt")
        with open(doc1, "w") as f:
            f.write("STATEMENT OF INVESTIGATOR Form FDA 1572 Protocol ID: PROTO-101")

        # Create exact duplicate in another subfolder
        sub = os.path.join(tmpdir, "subfolder")
        os.makedirs(sub, exist_ok=True)
        doc2 = os.path.join(sub, "study1_1572_copy.txt")
        with open(doc2, "w") as f:
            f.write("STATEMENT OF INVESTIGATOR Form FDA 1572 Protocol ID: PROTO-101")

        # Create unique doc in a zip
        zip_path = os.path.join(tmpdir, "bundle.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(
                "irb_letter.txt",
                "Institutional Review Board IRB Approval Letter PROTO-101",
            )

        docs = scanner.scan_drive(tmpdir)

        assert len(docs) == 3
        # One of the identical files should be flagged as duplicate
        duplicates = [d for d in docs if d.is_duplicate]
        assert len(duplicates) == 1
        assert duplicates[0].duplicate_of is not None

        # Archive origin should be recorded for the zipped file
        zipped_doc = next(d for d in docs if "irb_letter" in d.file_name)
        assert zipped_doc.archive_origin is not None

        scanner.cleanup()


def test_duplicate_text_extraction_bypassed():
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as tmpdir:
        scanner = ForensicScanner(temp_staging_dir=os.path.join(tmpdir, "staging"))

        doc1 = os.path.join(tmpdir, "doc1.txt")
        with open(doc1, "w") as f:
            f.write("Identical file content for testing SHA256 extraction caching")

        doc2 = os.path.join(tmpdir, "doc2.txt")
        with open(doc2, "w") as f:
            f.write("Identical file content for testing SHA256 extraction caching")

        doc3 = os.path.join(tmpdir, "unique.txt")
        with open(doc3, "w") as f:
            f.write("Unique file content")

        with patch("app.core.forensic_scanner.extract_file_text") as mock_extract:
            mock_extract.side_effect = lambda path: (
                f"Extracted text from {os.path.basename(path)}"
            )
            docs = scanner.scan_drive(tmpdir)

            # extract_file_text should be called only 2 times (for doc1/doc2 first seen, and doc3)
            assert mock_extract.call_count == 2

            # Check that all documents have non-empty extracted text
            for doc in docs:
                assert doc.extracted_text != ""

            # Check that duplicate document reused the extracted text of the original document
            dup_docs = [d for d in docs if d.is_duplicate]
            assert len(dup_docs) == 1
            orig_docs = [
                d for d in docs if not d.is_duplicate and "unique" not in d.file_name
            ]
            assert len(orig_docs) == 1
            assert dup_docs[0].extracted_text == orig_docs[0].extracted_text

        scanner.cleanup()


def test_error_hash_not_cached():
    with tempfile.TemporaryDirectory() as tmpdir:
        scanner = ForensicScanner(temp_staging_dir=os.path.join(tmpdir, "staging"))

        doc = os.path.join(tmpdir, "unreadable.txt")
        with open(doc, "w") as f:
            f.write("Some text")

        from unittest.mock import patch

        with patch.object(scanner, "compute_sha256", return_value="ERROR"):
            d1 = scanner._ingest_file(
                source_path=doc,
                actual_file_path=doc,
                rel_path="unreadable.txt",
            )
            assert d1.sha256_hash == "ERROR"
            assert "ERROR" not in scanner.seen_texts

        scanner.cleanup()


def test_parse_eml_attachment_traversal_neutralized():
    """Verify parse_eml_file neutralizes relative traversal in attachment filenames and confines writes to destination_dir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        scanner = ForensicScanner(temp_staging_dir=os.path.join(tmpdir, "staging"))

        eml_content = (
            b"From: attacker@example.com\r\n"
            b"To: victim@example.com\r\n"
            b"Subject: Critical Invoice\r\n"
            b'Content-Type: multipart/mixed; boundary="BOUNDARY"\r\n\r\n'
            b"--BOUNDARY\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Please see attached invoice.\r\n"
            b"--BOUNDARY\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b'Content-Disposition: attachment; filename="../../traversal_test.txt"\r\n\r\n'
            b"malicious payload content\r\n"
            b"--BOUNDARY--"
        )
        eml_path = os.path.join(tmpdir, "traversal.eml")
        with open(eml_path, "wb") as f:
            f.write(eml_content)

        dest = os.path.realpath(os.path.join(tmpdir, "eml_out"))
        body, attachments = scanner.parse_eml_file(eml_path, dest)

        # File must be written strictly inside dest
        assert len(attachments) == 1
        att_file = attachments[0]
        assert att_file == os.path.join(dest, "traversal_test.txt")
        assert os.path.exists(att_file)

        # File must NOT exist outside dest
        assert not os.path.exists(os.path.join(tmpdir, "traversal_test.txt"))
        assert not os.path.exists("/tmp/traversal_test.txt")

        scanner.cleanup()


def test_parse_eml_attachment_windows_backslash_traversal():
    """Verify parse_eml_file neutralizes Windows backslash traversal in attachment filenames."""
    with tempfile.TemporaryDirectory() as tmpdir:
        scanner = ForensicScanner(temp_staging_dir=os.path.join(tmpdir, "staging"))

        eml_content = (
            b"From: attacker@example.com\r\n"
            b"To: victim@example.com\r\n"
            b"Subject: Backslash Traversal\r\n"
            b'Content-Type: multipart/mixed; boundary="BOUNDARY"\r\n\r\n'
            b"--BOUNDARY\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Body\r\n"
            b"--BOUNDARY\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b'Content-Disposition: attachment; filename="..\\..\\win_traversal.txt"\r\n\r\n'
            b"payload\r\n"
            b"--BOUNDARY--"
        )
        eml_path = os.path.join(tmpdir, "win_traversal.eml")
        with open(eml_path, "wb") as f:
            f.write(eml_content)

        dest = os.path.realpath(os.path.join(tmpdir, "eml_out"))
        body, attachments = scanner.parse_eml_file(eml_path, dest)

        assert len(attachments) == 1
        assert attachments[0] == os.path.join(dest, "win_traversal.txt")
        assert os.path.exists(attachments[0])
        assert not os.path.exists(os.path.join(tmpdir, "win_traversal.txt"))

        scanner.cleanup()


def test_parse_eml_attachment_pure_traversal_skipped():
    """Verify parse_eml_file skips attachments that reduce to empty or directory traversal operators."""
    with tempfile.TemporaryDirectory() as tmpdir:
        scanner = ForensicScanner(temp_staging_dir=os.path.join(tmpdir, "staging"))

        eml_content = (
            b"From: attacker@example.com\r\n"
            b"To: victim@example.com\r\n"
            b"Subject: Empty Traversal\r\n"
            b'Content-Type: multipart/mixed; boundary="BOUNDARY"\r\n\r\n'
            b"--BOUNDARY\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b'Content-Disposition: attachment; filename="../../"\r\n\r\n'
            b"empty\r\n"
            b"--BOUNDARY--"
        )
        eml_path = os.path.join(tmpdir, "empty.eml")
        with open(eml_path, "wb") as f:
            f.write(eml_content)

        dest = os.path.realpath(os.path.join(tmpdir, "eml_out"))
        body, attachments = scanner.parse_eml_file(eml_path, dest)

        assert len(attachments) == 0

        scanner.cleanup()


def test_unpack_archive_zip_slip_rejected():
    """Verify unpack_archive ignores zip slip traversal entries and extracts only safe paths."""
    with tempfile.TemporaryDirectory() as tmpdir:
        scanner = ForensicScanner(temp_staging_dir=os.path.join(tmpdir, "staging"))

        zip_path = os.path.join(tmpdir, "slip.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("../../zip_slip.txt", "escaped content")
            zf.writestr(r"..\..\win_slip.txt", "win escaped content")
            zf.writestr("nested/valid.txt", "legit content")

        dest = os.path.realpath(os.path.join(tmpdir, "extracted"))
        extracted = scanner.unpack_archive(zip_path, dest)

        assert len(extracted) == 1
        assert extracted[0] == os.path.join(dest, "nested", "valid.txt")
        assert os.path.exists(extracted[0])
        assert not os.path.exists(os.path.join(tmpdir, "zip_slip.txt"))
        assert not os.path.exists(os.path.join(tmpdir, "win_slip.txt"))

        scanner.cleanup()
