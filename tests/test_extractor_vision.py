from unittest.mock import MagicMock

import pytest

from app.core.extractor import extract_file_text
from app.core.extractor_strategies import get_ocr_reader


@pytest.fixture(autouse=True)
def reset_ocr_reader(monkeypatch):
    import app.core.extractor_strategies as strat

    strat._ocr_reader = None
    strat._ocr_reader_loaded = False


def test_extract_image_success(mocker):
    mocker.patch("os.path.isfile", return_value=True)
    mocker.patch("os.path.splitext", return_value=("file", ".png"))

    mock_image = MagicMock()
    mock_PIL = MagicMock()
    mock_PIL.Image.open.return_value = mock_image
    mocker.patch.dict("sys.modules", {"PIL": mock_PIL})

    mock_numpy = MagicMock()
    mock_numpy.array.return_value = "np_image"
    mocker.patch.dict("sys.modules", {"numpy": mock_numpy})

    mock_reader = MagicMock()
    mock_reader.readtext.return_value = [
        ([[0, 0], [10, 0], [10, 10], [0, 10]], "a screenshot of a dashboard", 0.99)
    ]
    mocker.patch(
        "app.core.extractor_strategies.get_ocr_reader", return_value=mock_reader
    )

    text = extract_file_text("dashboard.png")
    assert text == "a screenshot of a dashboard"
    mock_PIL.Image.open.assert_called_once_with("dashboard.png")
    mock_image.load.assert_called_once()
    mock_reader.readtext.assert_called_once_with("np_image")


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

    mocker.patch("app.core.extractor_strategies.get_ocr_reader", return_value=None)

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

    mock_reader = MagicMock()
    mock_reader.readtext.return_value = [
        ([[0, 0], [10, 0], [10, 10], [0, 10]], "scanned contract page", 0.99)
    ]
    mocker.patch(
        "app.core.extractor_strategies.get_ocr_reader", return_value=mock_reader
    )

    mock_PIL = MagicMock()
    mock_PIL.Image.open.return_value = "pil_image"
    mocker.patch.dict("sys.modules", {"PIL": mock_PIL})

    mock_numpy = MagicMock()
    mock_numpy.array.return_value = "np_image"
    mocker.patch.dict("sys.modules", {"numpy": mock_numpy})

    text = extract_file_text("scanned.pdf")
    assert text == "scanned contract page"


def test_get_ocr_reader(mocker):
    mock_easyocr = MagicMock()
    mock_easyocr_reader = MagicMock(return_value="reader_instance")
    mock_easyocr.Reader = mock_easyocr_reader
    mocker.patch.dict("sys.modules", {"easyocr": mock_easyocr})

    mock_torch = MagicMock()
    mocker.patch.dict("sys.modules", {"torch": mock_torch})

    reader1 = get_ocr_reader()
    reader2 = get_ocr_reader()

    assert reader1 == "reader_instance"
    assert reader2 == "reader_instance"
    mock_easyocr_reader.assert_called_once_with(["en"], gpu=False)
    mock_torch.set_num_threads.assert_called_once_with(2)
