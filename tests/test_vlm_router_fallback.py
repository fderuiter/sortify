from unittest.mock import MagicMock, call

import pytest

from app.config import AppSettings, Settings
from app.core.extractor import (
    build_corpus_generator,
    build_corpus_generator_async,
    extract_file_text,
)
from app.core.offline_loader import OfflineModelLoadError


def test_settings_vision_engine_default_and_validation(tmp_path):
    # Test default setting
    settings = Settings()
    assert settings.VISION_ENGINE == "easyocr"

    # Test valid setting update
    settings.VISION_ENGINE = "florence-2"
    assert settings.VISION_ENGINE == "florence-2"

    # Test case sensitivity/whitespace handling
    settings.VISION_ENGINE = " Florence-2 "
    assert settings.VISION_ENGINE == "florence-2"

    # Test invalid setting raises ValueError
    with pytest.raises(ValueError):
        settings.VISION_ENGINE = "invalid_engine"

    # Test AppSettings persistence & validation
    settings_file = str(tmp_path / "settings.json")
    app_settings = AppSettings(filepath=settings_file)
    app_settings.VISION_ENGINE = "florence-2"
    assert app_settings.revalidate() is True

    if app_settings._save_timer:
        app_settings._save_timer.cancel()
    app_settings._save()

    # Reload from disk
    reloaded = AppSettings(filepath=settings_file)
    assert reloaded.VISION_ENGINE == "florence-2"


def test_vlm_router_florence2_image_extraction(mocker):
    mock_settings = Settings(VISION_ENGINE="florence-2")

    mock_image = MagicMock()
    mock_image.size = (800, 600)
    mock_PIL = MagicMock()
    mock_PIL.Image.open.return_value = mock_image
    mocker.patch.dict("sys.modules", {"PIL": mock_PIL})

    mock_florence_proc = MagicMock()
    mock_florence_proc.process_image.return_value = {
        "sanitized_text": "Florence-2 extracted visual text from image",
        "raw_output": "<OCR>Florence-2 extracted visual text from image</s>",
    }

    mock_registry = MagicMock()
    mock_registry.get_florence_processor.return_value = mock_florence_proc
    mocker.patch("app.core.shared_registry.SharedModelRegistry.get_instance", return_value=mock_registry)

    text = extract_file_text("document_photo.jpg", settings=mock_settings)

    assert text == "Florence-2 extracted visual text from image"
    mock_florence_proc.process_image.assert_called_once()


def test_vlm_router_florence2_scanned_pdf_extraction(mocker):
    mock_settings = Settings(VISION_ENGINE="florence-2")

    mocker.patch("builtins.open", mocker.mock_open())

    mock_pdf = mocker.patch("app.core.extractor_strategies.pypdf.PdfReader")
    mock_instance = mock_pdf.return_value
    mock_page = MagicMock()
    mock_page.extract_text.return_value = ""  # Scanned PDF without text layer

    mock_img = MagicMock()
    mock_img.data = b"fake_scanned_pdf_image"
    mock_page.images = [mock_img]
    mock_instance.pages = [mock_page]

    mock_pil_image = MagicMock()
    mock_pil_image.size = (800, 1000)

    mock_PIL = MagicMock()
    mock_PIL.Image.open.return_value = mock_pil_image
    mocker.patch.dict("sys.modules", {"PIL": mock_PIL})

    mock_florence_proc = MagicMock()
    mock_florence_proc.process_image.return_value = {
        "sanitized_text": "Scanned PDF page content via Florence-2",
        "raw_output": "Scanned PDF page content via Florence-2",
    }

    mock_registry = MagicMock()
    mock_registry.get_florence_processor.return_value = mock_florence_proc
    mocker.patch("app.core.shared_registry.SharedModelRegistry.get_instance", return_value=mock_registry)

    text = extract_file_text("scanned_invoice.pdf", settings=mock_settings)

    assert text == "Scanned PDF page content via Florence-2"
    mock_florence_proc.process_image.assert_called_once()


def test_vlm_router_fallback_on_florence2_exception(mocker, caplog):
    mock_settings = Settings(VISION_ENGINE="florence-2")

    mock_image = MagicMock()
    mock_image.size = (500, 500)
    mock_PIL = MagicMock()
    mock_PIL.Image.open.return_value = mock_image
    mocker.patch.dict("sys.modules", {"PIL": mock_PIL})

    mock_numpy = MagicMock()
    mock_numpy.array.return_value = "np_image"
    mocker.patch.dict("sys.modules", {"numpy": mock_numpy})

    # Florence-2 raises exception
    mock_florence_proc = MagicMock()
    mock_florence_proc.process_image.side_effect = OfflineModelLoadError("Model weights corrupted or GPU OOM")

    mock_registry = MagicMock()
    mock_registry.get_florence_processor.return_value = mock_florence_proc
    mocker.patch("app.core.shared_registry.SharedModelRegistry.get_instance", return_value=mock_registry)

    # EasyOCR mock for fallback
    mock_reader = MagicMock()
    mock_reader.readtext.return_value = [
        ([[0, 0], [10, 0], [10, 10], [0, 10]], "Fallback EasyOCR extracted text", 0.95)
    ]
    mocker.patch("app.core.extractor_strategies.get_ocr_reader", return_value=mock_reader)

    text = extract_file_text("damaged_scan.png", settings=mock_settings)

    # Assert that Florence-2 failed, warning was logged, and fallback EasyOCR succeeded
    assert text == "Fallback EasyOCR extracted text"
    mock_florence_proc.process_image.assert_called_once()
    mock_reader.readtext.assert_called_once_with("np_image")
    assert "Florence-2 vision engine processing failed" in caplog.text


@pytest.mark.anyio
async def test_vlm_router_memory_unloading_after_batch(tmp_path, mocker):
    mock_registry = MagicMock()
    mocker.patch("app.core.shared_registry.SharedModelRegistry.get_instance", return_value=mock_registry)

    mock_db = MagicMock()
    mock_db.get_document.return_value = None

    test_file = tmp_path / "test.txt"
    test_file.write_text("sample content")

    items = [test_file.name]

    # Run build_corpus_generator_async
    results = []
    async for item in build_corpus_generator_async(str(tmp_path), items, db=mock_db):
        results.append(item)

    # Verify model unloading in finally block
    mock_registry.unload_model.assert_has_calls([call("easyocr"), call("florence-2")], any_order=True)


def test_vlm_router_memory_unloading_sync_generator(tmp_path, mocker):
    mock_registry = MagicMock()
    mocker.patch("app.core.shared_registry.SharedModelRegistry.get_instance", return_value=mock_registry)

    mock_db = MagicMock()
    mock_db.get_document.return_value = None

    test_file = tmp_path / "test.txt"
    test_file.write_text("sample content")

    items = [test_file.name]
    progress_cb = MagicMock()

    gen = build_corpus_generator(str(tmp_path), items, progress_callback=progress_cb, max_workers=1, db=mock_db, sequential=True)
    list(gen)

    # Verify model unloading in finally block
    mock_registry.unload_model.assert_has_calls([call("easyocr"), call("florence-2")], any_order=True)
