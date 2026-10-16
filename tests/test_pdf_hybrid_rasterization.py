"""Unit tests for per-page text analysis with hybrid page rasterization."""

import io
import logging
from unittest.mock import MagicMock, patch

from PIL import Image
from reportlab.pdfgen import canvas

from app.config import Settings
from app.core.extractor_strategies import PdfExtractor, extract_text_from_image


def create_sample_pdf_bytes(draw_func) -> bytes:
    """Helper to generate in-memory PDF bytes using ReportLab."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    draw_func(c)
    c.save()
    buf.seek(0)
    return buf.getvalue()


def test_pdf_standard_dense_text_page(tmp_path, caplog):
    """Verify that a page with dense embedded digital text returns standard text with STANDARD status flag."""
    pdf_path = tmp_path / "standard.pdf"

    def draw(c):
        c.drawString(
            50,
            700,
            "This is a full page of embedded digital text containing sufficient characters to meet the digital text density threshold. "
            "It does not require any rasterization or visual OCR fallback because all characters are cleanly extracted as digital text.",
        )

    pdf_bytes = create_sample_pdf_bytes(draw)
    pdf_path.write_bytes(pdf_bytes)

    extractor = PdfExtractor()
    settings = Settings(PDF_TEXT_DENSITY_THRESHOLD=50)

    with caplog.at_level(logging.INFO):
        text = extractor.extract(str(pdf_path), settings=settings)

    assert "digital text density threshold" in text
    assert "status=STANDARD" in caplog.text


def test_pdf_minimal_text_triggers_ocr(tmp_path, caplog):
    """Verify that a page with minimal header text (< threshold) triggers visual OCR."""
    pdf_path = tmp_path / "minimal.pdf"

    def draw(c):
        c.drawString(50, 750, "Page 1")  # Minimal header (< 50 chars)

    pdf_bytes = create_sample_pdf_bytes(draw)
    pdf_path.write_bytes(pdf_bytes)

    extractor = PdfExtractor()
    settings = Settings(PDF_TEXT_DENSITY_THRESHOLD=50)

    # Mock pypdfium2 rasterization to return a rendered image and EasyOCR to return OCR text
    mock_rendered_pil = Image.new("RGB", (200, 200), color="white")
    mock_reader = MagicMock()
    mock_reader.readtext.return_value = [("box", "Scanned Body Content Text", 0.95)]

    with (
        patch.object(
            extractor, "_rasterize_page_to_pil", return_value=mock_rendered_pil
        ),
        patch("app.core.extractor_strategies.get_ocr_reader", return_value=mock_reader),
        patch("numpy.array", return_value="np_array"),
    ):
        with caplog.at_level(logging.INFO):
            text = extractor.extract(str(pdf_path), settings=settings)

    assert "Page 1" in text
    assert "Scanned Body Content Text" in text
    assert "status=RASTERIZED" in caplog.text or "status=EMBEDDED_OCR" in caplog.text


def test_pdf_vector_graphics_triggers_rasterization(tmp_path, caplog):
    """Verify vector PDF pages containing graphic paths convert to full-page bitmaps and yield searchable text."""
    pdf_path = tmp_path / "vector.pdf"

    def draw(c):
        c.drawString(10, 10, "Title")
        c.line(0, 0, 300, 300)  # Vector path: line
        c.rect(20, 20, 150, 100)  # Vector path: rectangle

    pdf_bytes = create_sample_pdf_bytes(draw)
    pdf_path.write_bytes(pdf_bytes)

    extractor = PdfExtractor()
    settings = Settings(PDF_TEXT_DENSITY_THRESHOLD=50)

    mock_rendered_pil = Image.new("RGB", (400, 400), color="white")
    mock_reader = MagicMock()
    mock_reader.readtext.return_value = [("box", "Blueprint Vector Layer Text", 0.98)]

    with (
        patch.object(
            extractor, "_rasterize_page_to_pil", return_value=mock_rendered_pil
        ) as mock_rast,
        patch("app.core.extractor_strategies.get_ocr_reader", return_value=mock_reader),
        patch("numpy.array", return_value="np_array"),
    ):
        with caplog.at_level(logging.INFO):
            text = extractor.extract(str(pdf_path), settings=settings)

    mock_rast.assert_called_once()
    assert "Blueprint Vector Layer Text" in text
    assert "status=RASTERIZED" in caplog.text


def test_pdf_image_decoding_exception_fallback(tmp_path, caplog):
    """Verify that image decoding exceptions (unsupported compression/fax codecs) fall back gracefully to whole-page rasterization."""
    pdf_path = tmp_path / "corrupt_img.pdf"

    def draw(c):
        c.drawString(50, 750, "Doc Header")

    pdf_bytes = create_sample_pdf_bytes(draw)
    pdf_path.write_bytes(pdf_bytes)

    extractor = PdfExtractor()
    settings = Settings(PDF_TEXT_DENSITY_THRESHOLD=50)

    # Mock page.images to contain an image object that raises a decoding exception on Image.open
    mock_bad_image = MagicMock()
    mock_bad_image.data = b"unsupported_fax_codec_data"

    mock_rendered_pil = Image.new("RGB", (300, 300), color="white")
    mock_reader = MagicMock()
    mock_reader.readtext.return_value = [
        ("box", "Recovered Text From Whole Page Rasterization", 0.95)
    ]

    def mock_extract_text(visitor_operand_before=None):
        return "Doc Header"

    with patch("pypdf.PdfReader") as mock_pdf_reader_cls:
        mock_reader_inst = MagicMock()
        mock_page = MagicMock()
        mock_page.extract_text.side_effect = mock_extract_text
        mock_page.images = [mock_bad_image]

        mock_reader_inst.pages = [mock_page]
        mock_pdf_reader_cls.return_value = mock_reader_inst

        with (
            patch.object(
                extractor, "_rasterize_page_to_pil", return_value=mock_rendered_pil
            ) as mock_rast,
            patch(
                "PIL.Image.open",
                side_effect=ValueError("Unsupported CCITTFaxDecode codec"),
            ),
            patch(
                "app.core.extractor_strategies.get_ocr_reader", return_value=mock_reader
            ),
            patch("numpy.array", return_value="np_array"),
        ):
            with caplog.at_level(logging.INFO):
                text = extractor.extract(str(pdf_path), settings=settings)

    mock_rast.assert_called_once()
    assert "Recovered Text From Whole Page Rasterization" in text
    assert "status=RASTERIZED" in caplog.text


def test_pdf_high_resolution_scan_downscaling(tmp_path, caplog):
    """Verify high-resolution page renderings exceeding configured dimensions are automatically downscaled and processed."""
    settings = Settings(IMAGE_MAX_DIMENSION=1000)

    # High-resolution image (4000 x 3000)
    high_res_image = Image.new("RGB", (4000, 3000), color="white")

    mock_reader = MagicMock()
    mock_reader.readtext.return_value = [("box", "Text From Downscaled Scan", 0.99)]

    with (
        patch("app.core.extractor_strategies.get_ocr_reader", return_value=mock_reader),
        patch("numpy.array", return_value="np_array"),
    ):
        with caplog.at_level(logging.INFO):
            result = extract_text_from_image(
                high_res_image, settings=settings, file_path="high_res_scan.pdf"
            )

    assert result == "Text From Downscaled Scan"
    assert "Status flag [DOWNSCALED]" in caplog.text
    assert "Downscaling high-resolution image high_res_scan.pdf" in caplog.text


def test_pdf_multi_page_detailed_status_logging(tmp_path, caplog):
    """Verify that multi-page PDF processing records explicit status flags for standard and rasterized pages."""
    pdf_path = tmp_path / "multipage.pdf"

    def draw(c):
        # Page 1: Dense text
        c.drawString(
            50,
            700,
            "Page 1 contains dense digital text that exceeds the density threshold easily and does not require rasterization.",
        )
        c.showPage()
        # Page 2: Vector drawing
        c.drawString(10, 10, "P2")
        c.rect(10, 10, 100, 100)

    pdf_bytes = create_sample_pdf_bytes(draw)
    pdf_path.write_bytes(pdf_bytes)

    extractor = PdfExtractor()
    settings = Settings(PDF_TEXT_DENSITY_THRESHOLD=50)

    mock_rendered_pil = Image.new("RGB", (300, 300), color="white")
    mock_reader = MagicMock()
    mock_reader.readtext.return_value = [("box", "Page 2 Vector Text", 0.95)]

    with (
        patch.object(
            extractor, "_rasterize_page_to_pil", return_value=mock_rendered_pil
        ),
        patch("app.core.extractor_strategies.get_ocr_reader", return_value=mock_reader),
        patch("numpy.array", return_value="np_array"),
    ):
        with caplog.at_level(logging.INFO):
            text = extractor.extract(str(pdf_path), settings=settings)

    assert "Page 1 contains dense digital text" in text
    assert "Page 2 Vector Text" in text
    assert "Page 1/2: status=STANDARD" in caplog.text
    assert "Page 2/2: status=RASTERIZED" in caplog.text
    assert "Completed PDF extraction" in caplog.text
