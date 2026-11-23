"""Comprehensive unit, security, fast-triage, and pipeline tests for expanded format extractors."""

from __future__ import annotations

import hashlib
import json
import os
import zipfile
from types import SimpleNamespace
from unittest.mock import MagicMock

import defusedxml.common
import pytest
from docx import Document
from PIL import Image
from pptx import Presentation
from pptx.util import Inches

from app.config import Settings
from app.core.analyzer import IncrementalAnalyzer
from app.core.exceptions import ArchiveSafetyError
from app.core.extractor import (
    ExtractionStatus,
    build_corpus_generator,
    evaluate_text_confidence,
    extract_file_text,
    process_item_worker,
)
from app.core.extractor_strategies import (
    DocxExtractor,
    EmlExtractor,
    HtmlExtractor,
    JsonExtractor,
    LogExtractor,
    MarkdownExtractor,
    OdtExtractor,
    PptxExtractor,
    RtfExtractor,
    TsvExtractor,
    XmlExtractor,
    YamlExtractor,
    registry,
)
from app.core.forensic_scanner import SUPPORTED_DOC_EXTENSIONS, ForensicScanner
from app.core.jev_classifier import (
    JevClassifierEngine,
    _extract_docx_snippet,
    _extract_pptx_snippet,
)
from app.core.pipeline import SortPipeline
from app.core.policy_engine import PolicyEngine
from app.core.resilient_file_ops import resilient_file_hash
from app.core.session import AppSession

ALL_EXPECTED_EXTENSIONS = {
    # Existing 12
    ".txt",
    ".docx",
    ".csv",
    ".xlsx",
    ".xls",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".mp3",
    ".wav",
    ".m4a",
    # Newly added 17
    ".pptx",
    ".md",
    ".rst",
    ".tsv",
    ".html",
    ".htm",
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".log",
    ".eml",
    ".rtf",
    ".odt",
    ".bmp",
    ".tiff",
    ".webp",
}


def _build_sample_odt(
    path: str,
    *,
    title: str = "Quarterly Tax Summary",
    header_text: str = "Acme Holdings Header",
    footer_text: str = "Confidential Footer Page 1",
    paragraphs: list[str] | None = None,
) -> None:
    """Create a valid minimal OpenDocument Text (.odt) archive on disk."""
    paragraphs = paragraphs or [
        "Quarterly tax payment reconciliation for fiscal year 2026.",
        "Deductible cloud infrastructure expenses totaled $18,400.",
    ]
    meta_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-meta xmlns:office="urn:oasis:office" xmlns:dc="urn:oasis:dc">'
        f"<office:meta><dc:title>{title}</dc:title></office:meta>"
        "</office:document-meta>"
    )
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-styles xmlns:office="urn:oasis:office" '
        'xmlns:style="urn:oasis:style" xmlns:text="urn:oasis:text">'
        "<office:master-styles>"
        "<style:master-page>"
        f"<style:header><text:p>{header_text}</text:p></style:header>"
        f"<style:footer><text:p>{footer_text}</text:p></style:footer>"
        "</style:master-page>"
        "</office:master-styles>"
        "</office:document-styles>"
    )
    para_nodes = "".join(f"<text:p>{p}</text:p>" for p in paragraphs)
    content_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:office" '
        'xmlns:text="urn:oasis:text" xmlns:table="urn:oasis:table">'
        "<office:body><office:text>"
        "<text:h>Tax Filing Overview</text:h>"
        f"{para_nodes}"
        "<table:table><table:table-row>"
        '<table:table-cell><text:p>Vendor<text:s text:c="2"/>Code</text:p></table:table-cell>'
        "<table:table-cell><text:p>INV-7788<text:tab/>Paid</text:p></table:table-cell>"
        "</table:table-row></table:table>"
        "</office:text></office:body>"
        "</office:document-content>"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("meta.xml", meta_xml)
        zf.writestr("styles.xml", styles_xml)
        zf.writestr("content.xml", content_xml)


def _build_multi_slide_pptx(path: str) -> None:
    """Create a multi-slide PPTX with grouped shapes, a slide table, and speaker notes."""
    prs = Presentation()
    blank_layout = prs.slide_layouts[6]

    # Slide 0: Neutral title text only (no Jev category keywords)
    s0 = prs.slides.add_slide(blank_layout)
    tb0 = s0.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1))
    tb0.text_frame.text = "General Overview Deck"

    # Slide 1: Grouped shape (with nested group) + Slide table
    s1 = prs.slides.add_slide(blank_layout)
    outer_grp = s1.shapes.add_group_shape()
    inner_box = outer_grp.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    inner_box.text_frame.text = "Grouped Fiscal Revenue Breakdown"
    nested_grp = outer_grp.shapes.add_group_shape()
    deep_box = nested_grp.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1))
    deep_box.text_frame.text = "Nested Balance Sheet Ledger"

    tbl_shape = s1.shapes.add_table(
        2, 2, Inches(1), Inches(3.5), Inches(5), Inches(1.5)
    )
    tbl = tbl_shape.table
    tbl.cell(0, 0).text = "Line Item"
    tbl.cell(0, 1).text = "Amount"
    tbl.cell(1, 0).text = "Invoice Remittance"
    tbl.cell(1, 1).text = "$92,500"

    # Slide 2: Speaker notes carrying critical keywords
    s2 = prs.slides.add_slide(blank_layout)
    tb2 = s2.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    tb2.text_frame.text = "Appendix Slide Three"
    notes_slide = s2.notes_slide
    notes_slide.notes_text_frame.text = (
        "Speaker note: Submit quarterly tax withholding receipt"
    )

    prs.save(path)


def _build_invoice_docx(path: str, *, include_body_paragraph: bool = False) -> None:
    """Create a multi-section DOCX invoice with headers, footers, merged cells, and nested tables."""
    doc = Document()

    sec0 = doc.sections[0]
    sec0.header.paragraphs[0].text = "Northwind Financial Invoice Header #4410"
    sec0.footer.paragraphs[0].text = "Wire remittance due within 30 calendar days"

    if include_body_paragraph:
        doc.add_paragraph("Customer billing statement overview.")

    outer_table = doc.add_table(rows=2, cols=2)
    # Merge row 0 across columns to verify merged _tc cells are not duplicated
    merged = outer_table.cell(0, 0).merge(outer_table.cell(0, 1))
    merged.text = "Consolidated Vendor Banner"

    outer_table.cell(1, 0).text = "Advisory Retainer"
    nested = outer_table.cell(1, 1).add_table(rows=1, cols=2)
    nested.cell(0, 0).text = "Subtotal"
    nested.cell(0, 1).text = "$12,750.00"

    # Add second section linked to previous to verify header/footer deduplication
    sec1 = doc.add_section()
    sec1.header.is_linked_to_previous = True
    sec1.footer.is_linked_to_previous = True

    doc.save(path)


def test_registry_supports_all_29_extensions_and_rejects_out_of_scope():
    """Verify ExtractorRegistry registers all 17 new formats plus 12 existing formats."""
    for ext in ALL_EXPECTED_EXTENSIONS:
        assert registry.is_supported(ext), f"Expected {ext} to be supported"
        assert registry.is_supported(ext.upper()), (
            f"Expected {ext.upper()} to be supported"
        )
        assert registry.get_extractor(ext) is not None

    for unsupported in (".doc", ".ppt", ".msg", ".unknown", ".bin", ".exe"):
        assert not registry.is_supported(unsupported)


def test_forensic_scanner_supported_extensions_cover_new_document_and_image_formats():
    """Verify ForensicScanner accepts all newly supported document and image extensions."""
    expected_forensic = ALL_EXPECTED_EXTENSIONS - {".mp3", ".wav", ".m4a"}
    assert expected_forensic.issubset(SUPPORTED_DOC_EXTENSIONS)


def test_pptx_extractor_and_jev_triage_capture_all_slides_groups_tables_and_notes(
    tmp_path,
):
    """Verify PptxExtractor and Jev _extract_pptx_snippet read slides 0..N, groups, tables, and notes."""
    pptx_path = str(tmp_path / "neutral_deck.pptx")
    _build_multi_slide_pptx(pptx_path)

    extracted = PptxExtractor().extract(pptx_path)
    assert "General Overview Deck" in extracted
    assert "Grouped Fiscal Revenue Breakdown" in extracted
    assert "Nested Balance Sheet Ledger" in extracted
    assert "Invoice Remittance" in extracted
    assert "$92,500" in extracted
    assert "Appendix Slide Three" in extracted
    assert "Speaker note: Submit quarterly tax withholding receipt" in extracted

    snippet = _extract_pptx_snippet(pptx_path)
    assert "grouped fiscal revenue" in snippet
    assert "nested balance sheet" in snippet
    assert "invoice remittance" in snippet
    assert "quarterly tax withholding receipt" in snippet

    engine = JevClassifierEngine()
    res = engine.classify(pptx_path)
    assert res.category == "Financial Reports"
    assert res.confidence >= 0.6


def test_docx_extractor_and_jev_triage_capture_tables_headers_footers_without_duplicates(
    tmp_path,
):
    """Verify DocxExtractor and _extract_docx_snippet extract headers, nested tables, and footers once."""
    docx_path = str(tmp_path / "neutral_form.docx")
    _build_invoice_docx(docx_path, include_body_paragraph=False)

    extracted = DocxExtractor().extract(docx_path)
    assert "Northwind Financial Invoice Header #4410" in extracted
    assert extracted.count("Northwind Financial Invoice Header #4410") == 1
    assert extracted.count("Consolidated Vendor Banner") == 1
    assert "Advisory Retainer" in extracted
    assert "Subtotal" in extracted
    assert "$12,750.00" in extracted
    assert extracted.count("Wire remittance due within 30 calendar days") == 1

    snippet = _extract_docx_snippet(docx_path)
    assert "northwind financial invoice header" in snippet
    assert "advisory retainer" in snippet
    assert "$12,750.00" in snippet
    assert "wire remittance due" in snippet

    engine = JevClassifierEngine()
    res = engine.classify(docx_path)
    assert res.category == "Financial Reports"
    assert res.confidence >= 0.6


def test_markup_data_richtext_and_odt_extractors(tmp_path):
    """Verify readable text extraction across .md, .rst, .log, .yaml, .yml, .json, .tsv, .html, .htm, .xml, .rtf, and .odt."""
    md_file = tmp_path / "notes.md"
    md_file.write_text(
        "# Sprint Retrospective\n\nSee [Tax Guide](docs/tax.md) for receipt rules.\n---\n```python\nx = 1\n```\n",
        encoding="utf-8",
    )
    md_text = MarkdownExtractor().extract(str(md_file))
    assert "Sprint Retrospective" in md_text
    assert "See Tax Guide for receipt rules." in md_text
    assert "docs/tax.md" not in md_text

    rst_file = tmp_path / "spec.rst"
    rst_file.write_text(
        "Architecture Overview\n=====================\n\nRefer to `Compliance Policy <policy.rst>`_ for details.\n",
        encoding="utf-8",
    )
    rst_text = str(extract_file_text(str(rst_file)))
    assert "Architecture Overview" in rst_text
    assert "Compliance Policy" in rst_text

    log_file = tmp_path / "service.log"
    log_file.write_text(
        "2026-11-20 INFO Ledger checkpoint completed\n2026-11-20 WARN Retry scheduled\n",
        encoding="utf-8",
    )
    log_text = LogExtractor().extract(str(log_file))
    assert "Ledger checkpoint completed" in log_text

    yaml_file = tmp_path / "deployment.yaml"
    yaml_file.write_text(
        "---\n# top comment\nvendor: Acme Cloud # inline comment\nitems:\n  - invoice_audit\n  - payroll_ledger\n...\n",
        encoding="utf-8",
    )
    yaml_text = YamlExtractor().extract(str(yaml_file))
    assert "vendor: Acme Cloud" in yaml_text
    assert "invoice_audit" in yaml_text
    assert "top comment" not in yaml_text
    assert "inline comment" not in yaml_text

    yml_file = tmp_path / "config.yml"
    yml_file.write_text("department: Legal Compliance\n", encoding="utf-8")
    assert "department: Legal Compliance" in str(extract_file_text(str(yml_file)))

    json_file = tmp_path / "payload.json"
    json_file.write_text(
        json.dumps(
            {
                "customer": {"name": "Globex Corp", "tier": "enterprise"},
                "line_items": [{"description": "Annual Tax Filing", "total": 3200}],
            }
        ),
        encoding="utf-8",
    )
    json_text = JsonExtractor().extract(str(json_file))
    assert "Globex Corp" in json_text
    assert "Annual Tax Filing" in json_text
    assert "3200" in json_text

    tsv_file = tmp_path / "ledger.tsv"
    tsv_file.write_text(
        "Account\tQuarter\tBalance\nOperating\tQ4\t45000\n", encoding="utf-8"
    )
    tsv_text = TsvExtractor().extract(str(tsv_file))
    assert "Account Quarter Balance" in tsv_text
    assert "Operating Q4 45000" in tsv_text

    html_file = tmp_path / "export.html"
    html_file.write_text(
        "<html><head><title>Invoice Statement &amp; Receipt</title>"
        '<meta name="description" content="Quarterly billing summary">'
        "<style>.hidden { display:none; }</style>"
        "<script>const secret = 'ignore_script_token';</script></head>"
        "<body><h1>Billing Summary</h1><p>Total due: &euro;1,250.00</p></body></html>",
        encoding="utf-8",
    )
    html_text = HtmlExtractor().extract(str(html_file))
    assert "Invoice Statement & Receipt" in html_text
    assert "Quarterly billing summary" in html_text
    assert "Total due: €1,250.00" in html_text
    assert "ignore_script_token" not in html_text
    assert "display:none" not in html_text

    htm_file = tmp_path / "legacy.htm"
    htm_file.write_text("<p>Contract Addendum Approved</p>", encoding="utf-8")
    assert "Contract Addendum Approved" in str(extract_file_text(str(htm_file)))

    xml_file = tmp_path / "record.xml"
    xml_file.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<invoice currency="USD"><vendor>Initech LLC</vendor><memo> Consulting Fee </memo></invoice>',
        encoding="utf-8",
    )
    xml_text = XmlExtractor().extract(str(xml_file))
    assert "Initech LLC" in xml_text
    assert "Consulting Fee" in xml_text
    assert "currency=USD" in xml_text

    rtf_file = tmp_path / "letter.rtf"
    rtf_file.write_text(
        r"{\rtf1\ansi{\fonttbl{\f0 Arial;}}{\colortbl;\red0\green0\blue0;}"
        r"{\*\generator CustomWriter;}"
        r"\pard Employment Contract Renewal\par "
        r"Compensation: \'80 95,000 \u8212? Executive Tier\par}",
        encoding="latin-1",
    )
    rtf_text = RtfExtractor().extract(str(rtf_file))
    assert "Employment Contract Renewal" in rtf_text
    assert "€ 95,000" in rtf_text
    assert "— Executive Tier" in rtf_text
    assert "Arial" not in rtf_text
    assert "CustomWriter" not in rtf_text

    odt_file = tmp_path / "report.odt"
    _build_sample_odt(str(odt_file))
    odt_text = OdtExtractor().extract(str(odt_file))
    assert "Quarterly Tax Summary" in odt_text
    assert "Acme Holdings Header" in odt_text
    assert "Confidential Footer Page 1" in odt_text
    assert "Quarterly tax payment reconciliation" in odt_text
    assert "INV-7788" in odt_text

    for ext, sample in (
        (".htm", "valid content"),
        (".yaml", "key: value"),
        (".yml", "key: value"),
        (".eml", "Email Subject: Invoice Billing Statement"),
        (".rtf", "Employment Contract Renewal Terms"),
        (".odt", "Quarterly Tax Summary Reconciliation"),
        (".pptx", "Confidential Oncology Clinical Trial"),
    ):
        assert evaluate_text_confidence(sample, ext) >= 0.8


def test_row_and_character_budgets_enforced_across_text_data_and_email_readers(
    tmp_path,
):
    """Verify TABULAR_MAX_ROWS and TABULAR_MAX_CHARACTERS truncate .tsv, .json, .md, .log, .yaml, and .eml."""
    row_settings = SimpleNamespace(
        TABULAR_MAX_ROWS=3,
        TABULAR_MAX_CHARACTERS=50000,
    )
    char_settings = SimpleNamespace(
        TABULAR_MAX_ROWS=10000,
        TABULAR_MAX_CHARACTERS=45,
    )

    # 1. TSV
    tsv_path = tmp_path / "big.tsv"
    tsv_path.write_text(
        "\n".join(f"colA_{i}\tcolB_{i}" for i in range(10)), encoding="utf-8"
    )
    tsv_ext = TsvExtractor()
    out = tsv_ext.extract(str(tsv_path), settings=row_settings)
    assert tsv_ext.truncated is True
    assert tsv_ext.truncation_reason == "MAX_ROWS_EXCEEDED"
    assert "colA_0" in out and "colA_5" not in out

    out_char = tsv_ext.extract(str(tsv_path), settings=char_settings)
    assert tsv_ext.truncated is True
    assert tsv_ext.truncation_reason == "MAX_CHARACTERS_EXCEEDED"
    assert len(out_char) <= 45

    # 2. JSON
    json_path = tmp_path / "big.json"
    json_path.write_text(
        json.dumps({f"field_{i}": f"value_payload_{i}" for i in range(10)}),
        encoding="utf-8",
    )
    json_ext = JsonExtractor()
    json_out = json_ext.extract(str(json_path), settings=row_settings)
    assert json_ext.truncated is True
    assert json_ext.truncation_reason == "MAX_ROWS_EXCEEDED"
    assert "field_0" in json_out and "field_9" not in json_out

    json_char = json_ext.extract(str(json_path), settings=char_settings)
    assert json_ext.truncated is True
    assert json_ext.truncation_reason == "MAX_CHARACTERS_EXCEEDED"
    assert len(json_char) <= 45

    # 3. Markdown
    md_path = tmp_path / "big.md"
    md_path.write_text(
        "\n".join(f"Paragraph line number {i} with content" for i in range(10)),
        encoding="utf-8",
    )
    md_ext = MarkdownExtractor()
    md_ext.extract(str(md_path), settings=row_settings)
    assert md_ext.truncated is True
    assert md_ext.truncation_reason == "MAX_ROWS_EXCEEDED"

    md_char = md_ext.extract(str(md_path), settings=char_settings)
    assert md_ext.truncated is True
    assert md_ext.truncation_reason == "MAX_CHARACTERS_EXCEEDED"
    assert len(md_char) <= 45

    # 4. Log
    log_path = tmp_path / "big.log"
    log_path.write_text(
        "\n".join(f"2026-11-23 INFO Event entry {i}" for i in range(10)),
        encoding="utf-8",
    )
    log_ext = LogExtractor()
    log_ext.extract(str(log_path), settings=row_settings)
    assert log_ext.truncated is True
    assert log_ext.truncation_reason == "MAX_ROWS_EXCEEDED"

    # 5. YAML
    yaml_path = tmp_path / "big.yaml"
    yaml_path.write_text(
        "\n".join(f"key_{i}: some_configured_value_{i}" for i in range(10)),
        encoding="utf-8",
    )
    yaml_ext = YamlExtractor()
    yaml_ext.extract(str(yaml_path), settings=row_settings)
    assert yaml_ext.truncated is True
    assert yaml_ext.truncation_reason == "MAX_ROWS_EXCEEDED"

    # 6. EML
    eml_path = tmp_path / "big.eml"
    eml_lines = "\r\n".join(f"Body line {i} with invoice details" for i in range(15))
    eml_path.write_bytes(
        (
            "From: billing@vendor.org\r\n"
            "To: finance@client.org\r\n"
            "Subject: Monthly Statement\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{eml_lines}\r\n"
        ).encode("utf-8")
    )
    eml_ext = EmlExtractor()
    eml_ext.extract(str(eml_path), settings=row_settings)
    assert eml_ext.truncated is True
    assert eml_ext.truncation_reason == "MAX_ROWS_EXCEEDED"

    eml_char = eml_ext.extract(str(eml_path), settings=char_settings)
    assert eml_ext.truncated is True
    assert eml_ext.truncation_reason == "MAX_CHARACTERS_EXCEEDED"
    assert len(eml_char) <= 45


def test_xml_and_odt_block_xxe_billion_laughs_and_zip_bombs(tmp_path):
    """Verify defusedxml and archive ratio/size checks block XXE, entity expansion, and zip bombs."""
    xxe_xml = tmp_path / "xxe.xml"
    xxe_xml.write_text(
        '<?xml version="1.0"?>'
        '<!DOCTYPE root [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        "<root>&xxe;</root>",
        encoding="utf-8",
    )
    with pytest.raises(defusedxml.common.DefusedXmlException):
        XmlExtractor().extract(str(xxe_xml))

    safe_res = extract_file_text(str(xxe_xml))
    assert safe_res.status == ExtractionStatus.FAILED
    assert safe_res.text == ""
    assert str(safe_res) == "[STATUS:FAILED]"

    laughs_xml = tmp_path / "laughs.xml"
    laughs_xml.write_text(
        '<?xml version="1.0"?>'
        '<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;">]>'
        "<lolz>&lol2;</lolz>",
        encoding="utf-8",
    )
    with pytest.raises(defusedxml.common.DefusedXmlException):
        XmlExtractor().extract(str(laughs_xml))

    # ODT with entity expansion inside content.xml
    evil_odt = tmp_path / "evil_entity.odt"
    with zipfile.ZipFile(evil_odt, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "content.xml",
            '<?xml version="1.0"?>'
            '<!DOCTYPE doc [<!ENTITY boom "boom">]>'
            "<office:document-content><text:p>&boom;</text:p></office:document-content>",
        )
    with pytest.raises(defusedxml.common.DefusedXmlException):
        OdtExtractor().extract(str(evil_odt))

    # ODT zip bomb (>1 MiB highly compressible payload exceeding 100:1 compression ratio)
    bomb_odt = tmp_path / "bomb.odt"
    with zipfile.ZipFile(bomb_odt, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "content.xml",
            b"<office:document-content>"
            + (b"0" * (2 * 1024 * 1024))
            + b"</office:document-content>",
        )
    with pytest.raises(ArchiveSafetyError):
        OdtExtractor().extract(str(bomb_odt))


def test_eml_extractor_reads_headers_plain_and_html_bodies_without_writing_attachments(
    tmp_path,
):
    """Verify EmlExtractor extracts From/To/Subject and plain or stripped HTML body without writing files."""
    work_dir = tmp_path / "emails"
    work_dir.mkdir()

    plain_eml = work_dir / "saved_plain.eml"
    plain_eml.write_bytes(
        b"From: =?utf-8?B?QWxpY2UgQWNjb3VudGluZw==?= <alice@acme.org>\r\n"
        b"To: bob@client.org\r\n"
        b"Cc: audit@client.org\r\n"
        b"Subject: =?utf-8?Q?Invoice_#8821_Tax_Receipt?=\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B1"\r\n\r\n'
        b"--B1\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Please remit payment for quarterly consulting invoice #8821.\r\n"
        b"--B1\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b'Content-Disposition: attachment; filename="malware_or_blob.bin"\r\n\r\n'
        b"UNWANTED_ATTACHMENT_PAYLOAD\r\n"
        b"--B1--\r\n"
    )

    html_eml = work_dir / "saved_html.eml"
    html_eml.write_bytes(
        b"From: legal@counsel.org\r\n"
        b"To: exec@client.org\r\n"
        b"Subject: Non-Disclosure Agreement Review\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/mixed; boundary="B2"\r\n\r\n'
        b"--B2\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n\r\n"
        b"<html><style>body{color:red}</style><script>evil()</script>"
        b"<body><p>Confidentiality clause approved for signature.</p></body></html>\r\n"
        b"--B2\r\n"
        b"Content-Type: text/plain\r\n"
        b'Content-Disposition: attachment; filename="attached_note.txt"\r\n\r\n'
        b"ATTACHMENT_ONLY_TEXT\r\n"
        b"--B2--\r\n"
    )

    before_files = set(os.listdir(work_dir))

    plain_extracted = EmlExtractor().extract(str(plain_eml))
    assert "Alice Accounting <alice@acme.org>" in plain_extracted
    assert "To: bob@client.org" in plain_extracted
    assert "Cc: audit@client.org" in plain_extracted
    assert "Invoice #8821 Tax Receipt" in plain_extracted
    assert "quarterly consulting invoice #8821" in plain_extracted
    assert "UNWANTED_ATTACHMENT_PAYLOAD" not in plain_extracted

    html_extracted = EmlExtractor().extract(str(html_eml))
    assert "From: legal@counsel.org" in html_extracted
    assert "To: exec@client.org" in html_extracted
    assert "Non-Disclosure Agreement Review" in html_extracted
    assert "Confidentiality clause approved for signature." in html_extracted
    assert "evil()" not in html_extracted
    assert "ATTACHMENT_ONLY_TEXT" not in html_extracted

    # Zero attachments written to disk
    assert set(os.listdir(work_dir)) == before_files

    # Jev fast-path triage classifies neutral-named .eml by interior content
    neutral_eml = work_dir / "msg_001.eml"
    neutral_eml.write_bytes(plain_eml.read_bytes())
    jev_res = JevClassifierEngine().classify(str(neutral_eml))
    assert jev_res.category == "Financial Reports"
    assert set(os.listdir(work_dir)) == before_files | {"msg_001.eml"}


def test_bmp_tiff_and_webp_ocr_respect_dimension_and_timeout_limits(tmp_path, mocker):
    """Verify BMP, TIFF, and WebP images run OCR with IMAGE_MAX_DIMENSION downscaling and VISUAL_TIMEOUT."""
    seen_shapes: list[tuple[int, ...]] = []

    fake_reader = MagicMock()

    def _fake_readtext(img_np):
        seen_shapes.append(img_np.shape)
        return [
            ([[0, 0], [10, 0], [10, 10], [0, 10]], "Scanned Tax Receipt 2026", 0.98)
        ]

    fake_reader.readtext.side_effect = _fake_readtext
    mocker.patch(
        "app.core.extractor_strategies.get_ocr_reader", return_value=fake_reader
    )

    settings = Settings(IMAGE_MAX_DIMENSION=500, VISION_ENGINE="easyocr")

    bmp_path = tmp_path / "scan.bmp"
    Image.new("RGB", (1200, 800), color=(255, 255, 255)).save(bmp_path, format="BMP")

    tiff_path = tmp_path / "scan.tiff"
    Image.new("RGBA", (1000, 1400), color=(240, 240, 240, 255)).save(
        tiff_path, format="TIFF"
    )

    webp_path = tmp_path / "scan.webp"
    Image.new("RGBA", (900, 600), color=(220, 220, 220, 255)).save(
        webp_path, format="WEBP"
    )

    for img_path in (bmp_path, tiff_path, webp_path):
        res = extract_file_text(str(img_path), settings=settings)
        assert res.status == ExtractionStatus.SUCCESS
        assert "Scanned Tax Receipt 2026" in str(res)

    assert len(seen_shapes) == 3
    for shape in seen_shapes:
        assert max(shape[0], shape[1]) <= 500

    # Verify VISUAL_TIMEOUT aborts hung OCR jobs on newly supported image formats
    import time

    def _slow_readtext(_img_np):
        time.sleep(1.5)
        return []

    fake_reader.readtext.side_effect = _slow_readtext
    timeout_settings = Settings(
        IMAGE_MAX_DIMENSION=500, VISION_ENGINE="easyocr", VISUAL_TIMEOUT=1
    )
    timeout_chunks = list(
        build_corpus_generator(
            str(tmp_path),
            ["scan.webp"],
            progress_callback=MagicMock(),
            max_workers=1,
            db=None,
            sequential=False,
            settings=timeout_settings,
        )
    )
    assert timeout_chunks[0]["scan.webp"]["text"] == "[STATUS:TIMEOUT]"


def test_unsupported_binary_files_receive_unique_sha256_digests_without_md5_collision(
    tmp_path,
):
    """Verify process_item_worker computes unique SHA-256 resilient_file_hash digests for unsupported binaries."""
    bin_a = tmp_path / "archive_alpha.unknown"
    bin_b = tmp_path / "archive_beta.unknown"
    bin_a.write_bytes(b"\x00\x01\x02ALPHA_BINARY_PAYLOAD\xff\xfe")
    bin_b.write_bytes(b"\x00\x01\x02BETA_BINARY_PAYLOAD\xaa\xbb")

    expected_hash_a = resilient_file_hash(str(bin_a), skip_media_tags=True)
    expected_hash_b = resilient_file_hash(str(bin_b), skip_media_tags=True)
    assert len(expected_hash_a) == 64
    assert expected_hash_a != expected_hash_b

    cb = MagicMock()
    item_a, text_a, hash_a = process_item_worker(
        str(tmp_path), "archive_alpha.unknown", cb, db=None
    )
    item_b, text_b, hash_b = process_item_worker(
        str(tmp_path), "archive_beta.unknown", cb, db=None
    )

    assert item_a == "archive_alpha.unknown"
    assert item_b == "archive_beta.unknown"
    assert text_a == "[STATUS:UNSUPPORTED]"
    assert text_b == "[STATUS:UNSUPPORTED]"
    assert hash_a == expected_hash_a
    assert hash_b == expected_hash_b

    # Verify IncrementalAnalyzer retains distinct SHA-256 digests rather than shared MD5 of "[STATUS:UNSUPPORTED]"
    chunks = list(
        build_corpus_generator(
            str(tmp_path),
            ["archive_alpha.unknown", "archive_beta.unknown"],
            progress_callback=cb,
            db=None,
            max_workers=1,
            sequential=True,
        )
    )
    captured_rows: list[tuple[str, str, str, str]] = []
    mock_db = MagicMock()
    mock_db.get_document_hashes.return_value = {}
    mock_db.upsert_documents.side_effect = lambda rows: captured_rows.extend(rows)
    analyzer = IncrementalAnalyzer(
        max_folders=5,
        stop_words=set(),
        db=mock_db,
        model_path=None,
    )
    for chunk in chunks:
        analyzer.partial_fit(str(tmp_path), chunk)

    fallback_md5 = hashlib.md5(b"[STATUS:UNSUPPORTED]").hexdigest()
    stored_hashes = {os.path.basename(row[1]): row[2] for row in captured_rows}
    assert stored_hashes["archive_alpha.unknown"] == expected_hash_a
    assert stored_hashes["archive_beta.unknown"] == expected_hash_b
    assert (
        stored_hashes["archive_alpha.unknown"] != stored_hashes["archive_beta.unknown"]
    )
    assert stored_hashes["archive_alpha.unknown"] != fallback_md5


def test_forensic_scanner_and_policy_engine_accept_new_formats(tmp_path):
    """Verify ForensicScanner.scan_drive and PolicyEngine evaluate newly supported formats."""
    scan_dir = tmp_path / "drive"
    scan_dir.mkdir()

    (scan_dir / "item1.md").write_text(
        "Confidential clinical study protocol amendment.", encoding="utf-8"
    )
    (scan_dir / "item2.json").write_text(
        json.dumps({"study": "ONCO-2026", "classification": "restricted"}),
        encoding="utf-8",
    )
    (scan_dir / "item3.tsv").write_text(
        "patient_id\tcohort\n101\tarm_a\n", encoding="utf-8"
    )
    _build_sample_odt(str(scan_dir / "item4.odt"))

    scanner = ForensicScanner(temp_staging_dir=str(tmp_path / "staging"))
    try:
        docs = scanner.scan_drive(str(scan_dir))
        rel_paths = {d.relative_path for d in docs}
        assert {"item1.md", "item2.json", "item3.tsv", "item4.odt"}.issubset(rel_paths)
        for d in docs:
            assert d.extracted_text.strip() != ""
    finally:
        scanner.cleanup()

    policy_rule = {
        "type": "keyword",
        "expression": "reconciliation",
        "target_path": "Tax/Reconciliations",
        "priority": 10,
        "enabled": True,
    }
    odt_text = str(extract_file_text(str(scan_dir / "item4.odt")))
    assert (
        PolicyEngine.match_policy(policy_rule, "item4.odt", odt_text, status_match=None)
        is True
    )


@pytest.mark.anyio
async def test_dropzone_modal_accepts_newly_supported_formats(tmp_path):
    """Verify DropZoneModal stages and accepts newly supported document, data, email, and image formats."""
    from app.ui.tui import DropZoneModal
    from tests.test_dropzone_modal import DummySettings, ModalTestApp

    deck = tmp_path / "slides.pptx"
    _build_multi_slide_pptx(str(deck))
    note = tmp_path / "summary.md"
    note.write_text("# Project Summary\n", encoding="utf-8")
    mail = tmp_path / "saved.eml"
    mail.write_bytes(b"From: a@b.org\r\nSubject: Hi\r\n\r\nHello world\r\n")
    odt = tmp_path / "doc.odt"
    _build_sample_odt(str(odt))
    unknown = tmp_path / "mystery.unknown"
    unknown.write_bytes(b"\x00\x01\x02")

    settings = DummySettings(tmp_path)
    app = ModalTestApp(settings=settings, base_dir=str(tmp_path))

    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, DropZoneModal)
        for p in (deck, note, mail, odt):
            assert modal._is_supported_file(str(p)) is True
            modal.stage_paths(str(p))
        modal.stage_paths(str(unknown))
        await pilot.pause()
        by_path = {
            os.path.abspath(entry["path"]): entry for entry in modal.staged_items
        }
        for p in (deck, note, mail, odt):
            abs_p = os.path.abspath(str(p))
            assert abs_p in by_path
            assert by_path[abs_p]["status"] == "Ready"
        assert by_path[os.path.abspath(str(unknown))]["status"] == "Unsupported"


def _flatten_plan_tree(node: object, prefix: str = "") -> dict[str, str]:
    """Recursively map leaf filenames in a SortPipeline plan tree to their target folder path."""
    mapping: dict[str, str] = {}
    raw = getattr(node, "plan", node)
    if not isinstance(raw, dict):
        return mapping
    for key, val in raw.items():
        if val is None or (
            isinstance(val, dict)
            and (
                val.get("node_type") == "file"
                or val.get("__type__") == "file"
                or "source_path" in val
                or "proposed_name" in val
            )
        ):
            mapping[os.path.basename(str(key))] = prefix
        elif isinstance(val, dict):
            next_prefix = f"{prefix}/{key}".strip("/") if prefix else str(key)
            mapping.update(_flatten_plan_tree(val, next_prefix))
        elif isinstance(val, list):
            for entry in val:
                fname = entry["name"] if isinstance(entry, dict) else str(entry)
                mapping[os.path.basename(fname)] = prefix
    return mapping


def test_sort_pipeline_plan_routes_neutral_named_new_formats_via_rules_and_clustering(
    tmp_path, monkeypatch
):
    """Verify SortPipeline.plan routes neutral-named .pptx, .md, .html, .eml, .json, .tsv, .rtf, .yaml, and table-only .docx via content rules and clustering."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "autosorter_state"))
    monkeypatch.setenv("NON_INTERACTIVE", "1")

    src_dir = tmp_path / "inbox"
    src_dir.mkdir()

    # Group A: Content-rule routed files (neutral names, interior invoice/remittance keywords)
    _build_invoice_docx(str(src_dir / "doc_a1.docx"), include_body_paragraph=False)

    prs_rule = Presentation()
    s0 = prs_rule.slides.add_slide(prs_rule.slide_layouts[6])
    s0.shapes.add_textbox(
        Inches(1), Inches(1), Inches(4), Inches(1)
    ).text_frame.text = "Cover"
    s1 = prs_rule.slides.add_slide(prs_rule.slide_layouts[6])
    tbl = s1.shapes.add_table(1, 2, Inches(1), Inches(1), Inches(4), Inches(1)).table
    tbl.cell(0, 0).text = "Remittance Voucher"
    tbl.cell(0, 1).text = "Tax Audit Invoice #550"
    prs_rule.save(str(src_dir / "doc_a2.pptx"))

    (src_dir / "doc_a3.eml").write_bytes(
        b"From: ap@vendor.org\r\n"
        b"To: finance@acme.org\r\n"
        b"Subject: Monthly Statement\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Attached remittance voucher for consulting services.\r\n"
    )
    (src_dir / "doc_a4.rtf").write_text(
        r"{\rtf1\ansi\pard Official remittance voucher and wire instructions\par}",
        encoding="latin-1",
    )

    # Group B: Topic peers for content clustering (astronomy / astrophysics telescope observations)
    astro_vocab = "astrophysics spectroscopy supernova nebula redshift exoplanet telescope luminosity"
    (src_dir / "peer_b1.md").write_text(
        f"# Observatory Log\n\n{astro_vocab} photometry calibration.\n",
        encoding="utf-8",
    )
    (src_dir / "peer_b2.html").write_text(
        f"<html><body><h1>Dataset</h1><p>{astro_vocab} interferometer array.</p></body></html>",
        encoding="utf-8",
    )
    (src_dir / "peer_b3.json").write_text(
        json.dumps({"project": "deep_sky", "summary": f"{astro_vocab} spectral lines"}),
        encoding="utf-8",
    )
    (src_dir / "peer_b4.tsv").write_text(
        f"target\tnotes\nM31\t{astro_vocab} radial velocity\n",
        encoding="utf-8",
    )
    (src_dir / "peer_b5.yaml").write_text(
        f"survey: deep_field\ndescription: {astro_vocab} bolometric flux\n",
        encoding="utf-8",
    )

    settings = Settings(
        AI_CONSENT_GRANTED=False,
        POLICIES=[
            {
                "type": "keyword",
                "expression": "remittance",
                "target_path": "Finance/Remittances",
                "priority": 100,
                "enabled": True,
            }
        ],
    )

    session = AppSession(settings=settings, base_dir=str(src_dir))
    try:
        pipeline = SortPipeline(session, base_dir=str(src_dir), settings=settings)
        plan = pipeline.plan()

        # Flatten nested plan folder tree -> {filename: folder_path}
        file_to_folder = {
            fname: folder.replace("\\", "/")
            for fname, folder in _flatten_plan_tree(plan).items()
        }

        all_expected_files = {
            "doc_a1.docx",
            "doc_a2.pptx",
            "doc_a3.eml",
            "doc_a4.rtf",
            "peer_b1.md",
            "peer_b2.html",
            "peer_b3.json",
            "peer_b4.tsv",
            "peer_b5.yaml",
        }
        assert all_expected_files.issubset(file_to_folder.keys())

        # Content rule routed files
        for rule_file in ("doc_a1.docx", "doc_a2.pptx", "doc_a3.eml", "doc_a4.rtf"):
            assert "Finance/Remittances" in file_to_folder[rule_file], (
                f"Expected {rule_file} in Finance/Remittances, got {file_to_folder[rule_file]}"
            )

        # Clustered peers must NOT fall back to Miscellaneous
        for peer_file in (
            "peer_b1.md",
            "peer_b2.html",
            "peer_b3.json",
            "peer_b4.tsv",
            "peer_b5.yaml",
        ):
            dest = file_to_folder[peer_file]
            assert "Miscellaneous" not in dest, (
                f"Expected {peer_file} to cluster by content, but got {dest}"
            )
    finally:
        session.close()
