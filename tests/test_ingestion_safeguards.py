import logging
import time
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.core.extractor import build_corpus_generator
from app.core.extractor_strategies import extract_text_from_image


def test_config_safeguard_bounds():
    """Verify that timeout limits and image thresholds are validated correctly via Pydantic."""
    # Timeout must be a positive integer
    with pytest.raises(ValidationError):
        Settings(VISUAL_TIMEOUT=0)
    with pytest.raises(ValidationError):
        Settings(VISUAL_TIMEOUT=-5)

    # Image max dimension must be positive
    with pytest.raises(ValidationError):
        Settings(IMAGE_MAX_DIMENSION=0)

    # Image skip threshold must be positive
    with pytest.raises(ValidationError):
        Settings(IMAGE_SKIP_THRESHOLD=0)

    # Valid settings work
    settings = Settings(
        VISUAL_TIMEOUT=5, IMAGE_MAX_DIMENSION=800, IMAGE_SKIP_THRESHOLD=2500
    )
    assert settings.VISUAL_TIMEOUT == 5
    assert settings.IMAGE_MAX_DIMENSION == 800
    assert settings.IMAGE_SKIP_THRESHOLD == 2500


def test_image_skipping_rules(caplog):
    """Verify that extremely large images exceeding the skip threshold are skipped and logged."""
    # Create a mock PIL image
    mock_image = MagicMock()
    mock_image.size = (3500, 2000)  # Exceeds skip threshold of 3000

    settings = Settings(IMAGE_SKIP_THRESHOLD=3000, IMAGE_MAX_DIMENSION=1000)

    # Mock get_ocr_reader to return a dummy reader
    with patch(
        "app.core.extractor_strategies.get_ocr_reader", return_value=MagicMock()
    ):
        with caplog.at_level(logging.WARNING):
            result = extract_text_from_image(
                mock_image, settings=settings, file_path="very_large.jpg"
            )

    assert result == "[STATUS:SKIPPED]"
    assert "Skipping OCR for very_large.jpg" in caplog.text
    assert "exceed the skip threshold of 3000" in caplog.text


def test_image_downscaling_aspect_ratio():
    """Verify that oversized images are downscaled to max dimension while maintaining aspect ratio, but not below 400px."""
    # Case 1: Image exceeds max_dimension and is downscaled keeping aspect ratio (e.g., 2000 x 1500 -> 1000 x 750)
    mock_image1 = MagicMock()
    mock_image1.size = (2000, 1500)
    settings = Settings(IMAGE_SKIP_THRESHOLD=5000, IMAGE_MAX_DIMENSION=1000)

    with (
        patch("app.core.extractor_strategies.get_ocr_reader", return_value=MagicMock()),
        patch("numpy.array") as mock_np_arr,
    ):
        mock_np_arr.return_value = "np_image"
        extract_text_from_image(
            mock_image1, settings=settings, file_path="oversized.jpg"
        )

    # Verify resize was called with (1000, 750)
    mock_image1.resize.assert_called_once()
    resize_args = mock_image1.resize.call_args[0][0]
    assert resize_args == (1000, 750)


def test_image_downscaling_hard_minimum():
    """Verify that downscaling does not shrink images below a hard limit of 400 pixels on either side."""
    # Case 2: Image is extremely wide (2000 x 500) with max_dimension of 1000.
    # Standard scale: 2000 -> 1000, 500 -> 250. But 250 < 400, so height is clamped to 400.
    mock_image2 = MagicMock()
    mock_image2.size = (2000, 500)
    settings = Settings(IMAGE_SKIP_THRESHOLD=5000, IMAGE_MAX_DIMENSION=1000)

    with (
        patch("app.core.extractor_strategies.get_ocr_reader", return_value=MagicMock()),
        patch("numpy.array") as mock_np_arr,
    ):
        mock_np_arr.return_value = "np_image"
        extract_text_from_image(mock_image2, settings=settings, file_path="wide.jpg")

    mock_image2.resize.assert_called_once()
    resize_args = mock_image2.resize.call_args[0][0]
    assert resize_args == (1000, 400)


def test_parallel_future_collection_timeout(caplog):
    """Verify that active extraction tasks exceeding the timeout are aborted and remaining tasks are processed."""
    settings = Settings(VISUAL_TIMEOUT=1)  # 1 second timeout

    # Mock DB
    mock_db = MagicMock()
    mock_db.get_document.return_value = None

    # Side effect for worker: slow_file sleeps for 1.5s, fast_file returns immediately
    def side_effect_worker(base_dir, item, progress_callback, db, settings=None):
        if item == "slow_file.png":
            time.sleep(1.5)
            return item, "Slow text", "slow_hash"
        else:
            return item, "Fast text", "fast_hash"

    with (
        patch("app.core.extractor.process_item_worker", side_effect=side_effect_worker),
        patch("app.config.AppSettings", return_value=MagicMock()),
    ):
        # We temporarily patch VISUAL_TIMEOUT to be 1 second for fast test execution
        settings.VISUAL_TIMEOUT = 1

        with caplog.at_level(logging.WARNING):
            generator = build_corpus_generator(
                base_dir="/base",
                items_to_sort=["slow_file.png", "fast_file.png"],
                progress_callback=MagicMock(),
                max_workers=2,
                db=mock_db,
                chunk_size=10,
                sequential=False,
                settings=settings,
            )
            chunks = list(generator)

    # There should be one chunk containing the results
    assert len(chunks) == 1
    chunk = chunks[0]

    # The slow file should have timed out and have the STATUS:TIMEOUT text
    assert "slow_file.png" in chunk
    assert chunk["slow_file.png"]["text"] == "[STATUS:TIMEOUT]"

    # The fast file should have processed successfully and exist in the chunk
    assert "fast_file.png" in chunk
    assert chunk["fast_file.png"]["text"] == "fast_file.png Fast text"

    # Verify the timeout clean warning was logged
    assert "Extraction of 'slow_file.png' timed out after 1 seconds." in caplog.text
