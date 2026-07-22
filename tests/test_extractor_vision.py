from unittest.mock import MagicMock

import pytest

from app.core.extractor import extract_file_text
from app.core.extractor_strategies import get_vision_model


@pytest.fixture(autouse=True)
def reset_vision_model(monkeypatch):
    import app.core.extractor_strategies as strat

    strat._vision_model = None
    strat._vision_model_loaded = False


def test_extract_image_success(mocker):
    mocker.patch("os.path.isfile", return_value=True)
    mocker.patch("os.path.splitext", return_value=("file", ".png"))

    mock_image = MagicMock()
    mock_PIL = MagicMock()
    mock_PIL.Image.open.return_value = mock_image
    mocker.patch.dict("sys.modules", {"PIL": mock_PIL})

    mock_model = MagicMock(
        return_value=[{"generated_text": "a screenshot of a dashboard"}]
    )
    mocker.patch(
        "app.core.extractor_strategies.get_vision_model", return_value=mock_model
    )

    text = extract_file_text("dashboard.png")
    assert text == "a screenshot of a dashboard"
    mock_PIL.Image.open.assert_called_once_with("dashboard.png")
    mock_image.load.assert_called_once()
    mock_model.assert_called_once_with(mock_image)


def test_extract_image_corrupt(mocker):
    mocker.patch("os.path.isfile", return_value=True)
    mocker.patch("os.path.splitext", return_value=("file", ".jpg"))

    mock_PIL = MagicMock()
    mock_PIL.Image.open.side_effect = Exception("Corrupt image")
    mocker.patch.dict("sys.modules", {"PIL": mock_PIL})

    text = extract_file_text("corrupt.jpg")
    assert text == "[STATUS:ERROR: Corrupt Image File]"


def test_extract_image_model_offline(mocker):
    mocker.patch("os.path.isfile", return_value=True)
    mocker.patch("os.path.splitext", return_value=("file", ".jpeg"))

    mock_image = MagicMock()
    mock_PIL = MagicMock()
    mock_PIL.Image.open.return_value = mock_image
    mocker.patch.dict("sys.modules", {"PIL": mock_PIL})

    mocker.patch("app.core.extractor_strategies.get_vision_model", return_value=None)

    text = extract_file_text("offline.jpeg")
    assert text == "[STATUS:ERROR: Vision Model Offline]"


def test_extract_pdf_visual_fallback(mocker):
    mocker.patch("os.path.isfile", return_value=True)
    mocker.patch("os.path.splitext", return_value=("file", ".pdf"))

    mocker.patch("builtins.open", mocker.mock_open())

    mock_pdf = mocker.patch("app.core.extractor_strategies.pypdf.PdfReader")
    mock_instance = mock_pdf.return_value
    mock_page = MagicMock()
    mock_page.extract_text.return_value = ""  # No digital text

    # Mock page images
    mock_img = MagicMock()
    mock_img.data = b"fake_image_data"
    mock_page.images = [mock_img]

    mock_instance.pages = [mock_page]

    mock_model = MagicMock(return_value=[{"generated_text": "scanned contract page"}])
    mocker.patch(
        "app.core.extractor_strategies.get_vision_model", return_value=mock_model
    )

    mock_PIL = MagicMock()
    mock_PIL.Image.open.return_value = "pil_image"
    mocker.patch.dict("sys.modules", {"PIL": mock_PIL})

    text = extract_file_text("scanned.pdf")
    assert text == "scanned contract page"


def test_get_vision_model(mocker):
    mock_transformers = MagicMock()
    mock_pipeline = MagicMock(return_value="model_instance")
    mock_transformers.pipeline = mock_pipeline
    mocker.patch.dict("sys.modules", {"transformers": mock_transformers})

    model1 = get_vision_model()
    model2 = get_vision_model()

    assert model1 == "model_instance"
    assert model2 == "model_instance"
    mock_pipeline.assert_called_once()
