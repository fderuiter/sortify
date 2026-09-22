import os
import tempfile
import threading
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.downloader import (
    DownloadManager,
    NetworkError,
    run_background_download,
)


@pytest.fixture
def temp_settings_path():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    yield path
    if os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass


def test_app_settings_observer_registration_and_notification(temp_settings_path):
    settings = AppSettings(filepath=temp_settings_path)
    notifications = []

    def observer_cb(val):
        notifications.append(val)

    # Register observer
    settings.add_observer("PROXY", observer_cb)

    # Mutate PROXY property
    settings.PROXY = "http://proxy.example.com:8080"

    assert len(notifications) == 1
    assert notifications[0] == "http://proxy.example.com:8080"

    # Mutate again with new proxy
    settings.PROXY = "http://user:pass@proxy2.example.com:3128"
    assert len(notifications) == 2
    assert notifications[1] == "http://user:pass@proxy2.example.com:3128"

    # Clean up observer
    settings.remove_observer("PROXY", observer_cb)
    settings.PROXY = ""
    assert len(notifications) == 2


def test_app_settings_observer_flexible_signatures_and_exception_handling(
    temp_settings_path,
):
    settings = AppSettings(filepath=temp_settings_path)
    calls = []

    def no_arg_cb():
        calls.append("no_arg")

    def one_arg_cb(val):
        calls.append(("one_arg", val))

    def two_arg_cb(key, val):
        calls.append(("two_arg", key, val))

    def faulty_cb(val):
        raise ValueError("Observer error simulated")

    settings.add_observer("PROXY", no_arg_cb)
    settings.add_observer("PROXY", one_arg_cb)
    settings.add_observer("PROXY", faulty_cb)
    settings.add_observer("PROXY", two_arg_cb)

    # Setting mutation must not crash even if faulty_cb raises an exception
    settings.PROXY = "http://test-proxy:8000"

    assert "no_arg" in calls
    assert ("one_arg", "http://test-proxy:8000") in calls
    assert ("two_arg", "PROXY", "http://test-proxy:8000") in calls

    # Clean up
    settings.remove_observer("PROXY", no_arg_cb)
    settings.remove_observer("PROXY", one_arg_cb)
    settings.remove_observer("PROXY", faulty_cb)
    settings.remove_observer("PROXY", two_arg_cb)


def test_download_manager_subscribes_to_proxy_changes(temp_settings_path):
    settings = AppSettings(filepath=temp_settings_path)
    dm = DownloadManager(settings=settings)

    assert dm._proxy == ""

    # Mutate settings PROXY -> DownloadManager should update automatically
    settings.PROXY = "http://10.0.0.1:8080"
    assert dm._proxy == "http://10.0.0.1:8080"

    opener = dm.get_opener()
    assert opener is not None

    # Clear proxy -> direct connection
    settings.PROXY = ""
    assert dm._proxy == ""


def test_invalid_decryption_failed_proxy_handling(temp_settings_path):
    settings = AppSettings(filepath=temp_settings_path)
    dm = DownloadManager(settings=settings)

    # Setting PROXY to <DECRYPTION_FAILED> must not crash observer dispatchers
    settings.PROXY = "<DECRYPTION_FAILED>"

    assert dm._proxy == "<DECRYPTION_FAILED>"

    # Opening request must cleanly raise NetworkError
    with pytest.raises(NetworkError, match="Invalid proxy configuration"):
        dm.get_opener()


def test_ui_settings_mutation_hot_reloads_proxy(temp_settings_path):
    settings = AppSettings(filepath=temp_settings_path)
    dm = DownloadManager(settings=settings)

    # Simulate UI entry in settings.py or wizard.py
    proxy_user_input = "http://corporate-proxy.internal:8080"
    settings.PROXY = proxy_user_input

    assert dm._proxy == "http://corporate-proxy.internal:8080"


def test_active_download_adopts_updated_proxy_on_retry(temp_settings_path):
    import hashlib
    from app.core.shared_registry import SharedModelRegistry

    mock_data = b"downloaded"
    expected_hash = hashlib.sha256(mock_data).hexdigest()
    SharedModelRegistry.get_instance().register_expected_hashes(
        "model_download", {"model.onnx": expected_hash}
    )

    settings = AppSettings(filepath=temp_settings_path)
    settings.PROXY = "http://initial-proxy:8080"
    dm = DownloadManager(settings=settings)

    # Simulate network call adopting updated proxy opener
    mock_opener_1 = MagicMock()
    mock_opener_1.open.side_effect = NetworkError("Initial proxy failed")

    mock_response_2 = MagicMock()
    mock_response_2.status = 200
    mock_response_2.info.return_value.get.return_value = "10"
    mock_response_2.read.side_effect = [mock_data, b""]

    mock_opener_2 = MagicMock()
    mock_opener_2.open.return_value.__enter__.return_value = mock_response_2

    openers = [mock_opener_1, mock_opener_2]

    def mock_get_opener():
        if openers:
            return openers.pop(0)
        return mock_opener_2

    with patch.object(dm, "get_opener", side_effect=mock_get_opener):
        success_event = threading.Event()
        failure_event = threading.Event()

        with tempfile.TemporaryDirectory() as tmp_dir:
            run_background_download(
                url="http://example.com/model.onnx",
                model_dir=tmp_dir,
                progress_callback=None,
                on_success=lambda: success_event.set(),
                on_failure=lambda err: failure_event.set(),
                download_manager=dm,
            )

            # Verification: should retry with updated opener and succeed
            assert success_event.wait(timeout=5)
