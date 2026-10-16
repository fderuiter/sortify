import hashlib
import zipfile

from app.core.extractor import get_file_hash


def create_mock_docx(file_path, modified_time_str="2026-01-01T00:00:00Z", body_text="Hello World"):
    """Helper to build a valid minimal .docx archive."""
    with zipfile.ZipFile(file_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", b"<Types></Types>")
        zf.writestr("_rels/.rels", b"<Relationships></Relationships>")
        zf.writestr(
            "docProps/core.xml",
            f'<cp:coreProperties><dcterms:modified>{modified_time_str}</dcterms:modified></cp:coreProperties>'.encode("utf-8"),
        )
        zf.writestr("docProps/app.xml", b"<Properties><Application>Microsoft Word</Application></Properties>")
        zf.writestr(
            "word/document.xml",
            f"<w:document><w:body><w:p><w:t>{body_text}</w:t></w:p></w:body></w:document>".encode("utf-8"),
        )


def create_mock_xlsx(file_path, modified_time_str="2026-01-01T00:00:00Z", cell_val="100"):
    """Helper to build a valid minimal .xlsx archive."""
    with zipfile.ZipFile(file_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", b"<Types></Types>")
        zf.writestr("_rels/.rels", b"<Relationships></Relationships>")
        zf.writestr(
            "docProps/core.xml",
            f'<cp:coreProperties><dcterms:modified>{modified_time_str}</dcterms:modified></cp:coreProperties>'.encode("utf-8"),
        )
        zf.writestr("xl/workbook.xml", b"<workbook></workbook>")
        zf.writestr(
            "xl/worksheets/sheet1.xml",
            f"<worksheet><sheetData><c><v>{cell_val}</v></c></sheetData></worksheet>".encode("utf-8"),
        )


def test_docx_hash_stability_on_metadata_update(tmp_path):
    """Verify .docx produce identical hash values when docProps metadata and timestamps change."""
    doc_v1 = tmp_path / "document_v1.docx"
    doc_v2 = tmp_path / "document_v2.docx"

    create_mock_docx(doc_v1, modified_time_str="2026-01-01T00:00:00Z", body_text="Test Document Body")
    create_mock_docx(doc_v2, modified_time_str="2026-10-16T12:00:00Z", body_text="Test Document Body")

    # Raw whole-file SHA-256 hashes differ due to metadata/zip timestamp updates
    raw_hash_v1 = hashlib.sha256(doc_v1.read_bytes()).hexdigest()
    raw_hash_v2 = hashlib.sha256(doc_v2.read_bytes()).hexdigest()
    assert raw_hash_v1 != raw_hash_v2

    # Payload-stable OOXML hashes must be identical
    hash_v1 = get_file_hash(str(doc_v1))
    hash_v2 = get_file_hash(str(doc_v2))
    assert hash_v1 == hash_v2


def test_xlsx_hash_stability_on_metadata_update(tmp_path):
    """Verify .xlsx produce identical hash values when docProps metadata and timestamps change."""
    sheet_v1 = tmp_path / "sheet_v1.xlsx"
    sheet_v2 = tmp_path / "sheet_v2.xlsx"

    create_mock_xlsx(sheet_v1, modified_time_str="2026-01-01T00:00:00Z", cell_val="42")
    create_mock_xlsx(sheet_v2, modified_time_str="2026-10-16T12:00:00Z", cell_val="42")

    hash_v1 = get_file_hash(str(sheet_v1))
    hash_v2 = get_file_hash(str(sheet_v2))
    assert hash_v1 == hash_v2


def test_ooxml_hash_detects_body_content_changes(tmp_path):
    """Verify that changes to actual document body streams alter the payload hash."""
    doc_original = tmp_path / "doc_original.docx"
    doc_modified = tmp_path / "doc_modified.docx"

    create_mock_docx(doc_original, body_text="Original Text")
    create_mock_docx(doc_modified, body_text="Modified Text")

    hash_orig = get_file_hash(str(doc_original))
    hash_mod = get_file_hash(str(doc_modified))
    assert hash_orig != hash_mod


def test_corrupt_office_archive_fallback(tmp_path):
    """Verify corrupted archive files safely fall back to standard file hashing without exceptions."""
    corrupt_docx = tmp_path / "corrupt.docx"
    corrupt_docx.write_bytes(b"THIS IS NOT A ZIP ARCHIVE HEADER AT ALL")

    raw_hash = hashlib.sha256(corrupt_docx.read_bytes()).hexdigest()
    digest = get_file_hash(str(corrupt_docx))

    assert digest == raw_hash


def test_rollback_preverification_succeeds_with_modified_metadata(tmp_path):
    """Verify rollback pre-verification succeeds when target Office files have modified metadata timestamps."""
    doc_path = tmp_path / "report.docx"
    create_mock_docx(doc_path, modified_time_str="2026-01-01T00:00:00Z", body_text="Financial Report")

    initial_hash = get_file_hash(str(doc_path))

    # Simulate Office viewing / metadata timestamp update
    create_mock_docx(doc_path, modified_time_str="2026-10-16T12:00:00Z", body_text="Financial Report")

    current_hash = get_file_hash(str(doc_path))
    assert current_hash == initial_hash


def test_non_office_files_unaffected(tmp_path):
    """Verify standard text and non-Office files produce unchanged standard whole-file hashes."""
    txt_file = tmp_path / "sample.txt"
    txt_file.write_text("Hello World Non-Office", encoding="utf-8")

    expected_hash = hashlib.sha256(txt_file.read_bytes()).hexdigest()
    assert get_file_hash(str(txt_file)) == expected_hash
