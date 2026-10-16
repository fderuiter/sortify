"""Unit and TUI integration tests for Extraction & OCR tab and Visual Extraction Preview modal."""

import io

from reportlab.pdfgen import canvas

from app.config import AppSettings
from app.core.extractor_strategies import PdfExtractor
from app.ui.tui import (
    AutoSorterTUI,
    DensitySlider,
    ExtractionPreviewModal,
    SettingsModal,
)


def create_multi_page_pdf(tmp_path) -> str:
    """Create a sample multi-page PDF with varying text density and vector paths."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf)

    # Page 1: Dense embedded text (no vector paths)
    c.drawString(
        50,
        700,
        "This is page one containing dense digital embedded text that comfortably exceeds the default density threshold of fifty characters.",
    )
    c.showPage()

    # Page 2: Sparse text (< 50 chars, no vector paths)
    c.drawString(50, 700, "Page 2 Short Text")
    c.showPage()

    # Page 3: Text with vector graphics (line path)
    c.drawString(
        50,
        700,
        "Page three contains dense text but also includes explicit vector path line drawings.",
    )
    c.line(50, 650, 400, 650)
    c.showPage()

    c.save()
    pdf_path = str(tmp_path / "preview_sample.pdf")
    with open(pdf_path, "wb") as f:
        f.write(buf.getvalue())

    return pdf_path


def test_preview_pdf_density_evaluation(tmp_path):
    """Verify preview_pdf_density correctly evaluates character count, vector paths, and predicted status."""
    pdf_path = create_multi_page_pdf(tmp_path)
    extractor = PdfExtractor()

    # Evaluate at threshold = 50
    pages = extractor.preview_pdf_density(pdf_path, density_threshold=50)

    assert len(pages) == 3

    # Page 1: Dense text (>= 50 chars), no vector -> STANDARD
    assert pages[0]["page_index"] == 1
    assert pages[0]["char_count"] >= 50
    assert not pages[0]["has_vector"]
    assert pages[0]["status"] == "STANDARD"

    # Page 2: Sparse text (< 50 chars), no vector -> RASTERIZED
    assert pages[1]["page_index"] == 2
    assert pages[1]["char_count"] < 50
    assert not pages[1]["has_vector"]
    assert pages[1]["status"] == "RASTERIZED"

    # Page 3: Dense text, has vector -> RASTERIZED
    assert pages[2]["page_index"] == 3
    assert pages[2]["has_vector"]
    assert pages[2]["status"] == "RASTERIZED"

    # Evaluate at threshold = 10 (Page 2 text "Page 2 Short Text" has ~17 chars)
    pages_low = extractor.preview_pdf_density(pdf_path, density_threshold=10)
    assert pages_low[1]["status"] == "STANDARD"


def test_settings_modal_extraction_tab(tmp_path):
    """Verify SettingsModal contains Extraction & OCR tab with density controls."""

    async def _test():
        settings_file = str(tmp_path / "settings.json")
        settings = AppSettings(filepath=settings_file)
        settings._settings_model.AI_CONSENT_GRANTED = True
        settings.PDF_TEXT_DENSITY_THRESHOLD = 75
        app = AutoSorterTUI(settings=settings)

        async with app.run_test(size=(100, 35)) as pilot:
            modal = SettingsModal(app.settings)
            app.push_screen(modal)
            await pilot.pause()

            # Check threshold input initial value
            input_density = modal.query_one("#input-pdf-density-threshold")
            assert input_density.value == "75"

            # Check density slider initial value
            slider_density = modal.query_one("#slider-pdf-density-threshold", DensitySlider)
            assert slider_density.value == 75

    import asyncio

    asyncio.run(_test())


def test_extraction_preview_modal_interactive_slider(tmp_path):
    """Verify ExtractionPreviewModal dynamically updates page status decisions as slider moves."""

    async def _test():
        pdf_path = create_multi_page_pdf(tmp_path)
        settings_file = str(tmp_path / "settings.json")
        settings = AppSettings(filepath=settings_file)
        settings._settings_model.AI_CONSENT_GRANTED = True
        settings.PDF_TEXT_DENSITY_THRESHOLD = 50

        app = AutoSorterTUI(settings=settings)

        async with app.run_test(size=(100, 35)) as pilot:
            modal = ExtractionPreviewModal(pdf_path, app.settings)
            app.push_screen(modal)
            await pilot.pause()

            slider = modal.query_one("#preview-slider-threshold", DensitySlider)
            inp = modal.query_one("#preview-input-threshold")

            assert slider.value == 50
            assert inp.value == "50"

            # Change slider value to 10
            slider.value = 10
            await pilot.pause()

            assert inp.value == "10"
            assert modal.current_threshold == 10

            # Change slider value to 200
            slider.value = 200
            await pilot.pause()

            assert inp.value == "200"
            assert modal.current_threshold == 200

            # Click save button and verify threshold updated in settings
            await pilot.click("#btn-preview-save")
            await pilot.pause()

            assert app.settings.PDF_TEXT_DENSITY_THRESHOLD == 200

    import asyncio

    asyncio.run(_test())
