"""Tests for thread-safe AppSettings configuration change observers and DownloadManager dynamic proxy hot-reloading."""

import hashlib
import json
import os
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.downloader import (
    DownloadManager,
    NetworkError,
    run_background_download,
)
from app.core.shared_registry import SharedModelRegistry


@pytest.fixture(autouse=True)
def clean_observers():
    """Ensure AppSettings observers and DownloadManager singleton are reset before and after each test."""
    AppSettings.clear_observers()
    DownloadManager.reset_instance()
    yield
    AppSettings.clear_observers()
    DownloadManager.reset_instance()


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


def test_app_settings_observer_subscription_and_dispatch(tmp_path):
    """Verify that add_observer registers callbacks and triggers them upon setting attribute mutation."""
    config_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=config_file)

    events_1_arg = []
    events_2_args = []
    events_0_args = []

    def on_proxy_1(val):
        events_1_arg.append(val)

    def on_proxy_2(key, val):
        events_2_args.append((key, val))

    def on_proxy_0():
        events_0_args.append("triggered")

    settings.add_observer("PROXY", on_proxy_1)
    settings.add_observer("PROXY", on_proxy_2)
    settings.add_observer("PROXY", on_proxy_0)

    # Mutate PROXY
    test_proxy = "http://user:pass@127.0.0.1:8080"
    settings.PROXY = test_proxy

    assert len(events_1_arg) == 1
    assert events_1_arg[0] == test_proxy

    assert len(events_2_args) == 1
    assert events_2_args[0] == ("PROXY", test_proxy)

    assert len(events_0_args) == 1
    assert events_0_args[0] == "triggered"


def test_wildcard_observer(tmp_path):
    """Verify that observers registered under '*' receive notifications for all setting changes."""
    config_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=config_file)

    events = []

    def on_any_change(key, val):
        events.append((key, val))

    AppSettings.add_observer("*", on_any_change)

    settings.PROXY = "http://proxy.example.com:8080"

    assert len(events) >= 1
    assert ("PROXY", "http://proxy.example.com:8080") in events


def test_observer_exception_isolation(tmp_path):
    """Verify that an exception in one observer does not break setting mutation or prevent other observers from running."""
    config_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=config_file)

    healthy_events = []

    def faulty_observer(val):
        raise RuntimeError("Simulated observer crash!")

    def healthy_observer(val):
        healthy_events.append(val)

    settings.add_observer("PROXY", faulty_observer)
    settings.add_observer("PROXY", healthy_observer)

    test_proxy = "http://10.0.0.1:3128"
    settings.PROXY = test_proxy

    # Setting value should still be updated and healthy_observer must receive notification
    assert settings.PROXY == test_proxy
    assert len(healthy_events) == 1
    assert healthy_events[0] == test_proxy


def test_download_manager_subscribes_and_updates_proxy(tmp_path):
    """Verify DownloadManager registers as PROXY observer and updates its opener dynamically."""
    config_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=config_file)

    dm = DownloadManager.get_instance()

    # Mutate setting to new proxy address
    new_proxy = "http://corporate-proxy.com:8080"
    settings.PROXY = new_proxy

    # DownloadManager should have updated its internal proxy state
    assert dm._current_proxy == new_proxy
    opener = dm.get_opener()
    assert opener is not None
    assert dm._active_proxy_str == new_proxy

    # Clear proxy setting back to direct connection
    settings.PROXY = ""
    assert dm._current_proxy == ""
    direct_opener = dm.get_opener()
    assert direct_opener is not None
    assert dm._active_proxy_str == ""


def test_encrypted_proxy_decryption_and_hot_reload(tmp_path):
    """Verify proxy setting with encryption on disk notifies DownloadManager with decrypted value."""
    config_file = str(tmp_path / "settings.json")
    dm = DownloadManager.get_instance()
    settings = AppSettings(filepath=config_file)

    secret_proxy = "http://admin:secret123@proxy.internal:8080"
    settings.PROXY = secret_proxy

    assert dm._current_proxy == secret_proxy

    # Ensure disk file holds encrypted string
    if settings._save_timer is not None:
        settings._save_timer.join(timeout=2.0)

    with open(config_file, "r", encoding="utf-8") as f:
        disk_data = json.load(f)

    assert "PROXY" in disk_data
    assert disk_data["PROXY"].startswith("enc:")
    assert "secret123" not in disk_data["PROXY"]


def test_invalid_proxy_decryption_failure_handling(tmp_path):
    """Verify invalid proxy or <DECRYPTION_FAILED> string triggers NetworkError safely without crashing observer."""
    config_file = str(tmp_path / "settings.json")
    dm = DownloadManager.get_instance()
    settings = AppSettings(filepath=config_file)

    # Directly set proxy to decryption failed placeholder
    settings.PROXY = "<DECRYPTION_FAILED>"

    assert dm._current_proxy == "<DECRYPTION_FAILED>"

    # Attempting to retrieve opener for decryption failed proxy should raise NetworkError
    with pytest.raises(NetworkError, match="decryption failed"):
        dm.get_opener()


def test_concurrent_observer_registration_and_dispatch(tmp_path):
    """Verify thread-safety when multiple threads register observers and mutate settings concurrently."""
    config_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=config_file)

    counter = {"count": 0}
    counter_lock = threading.Lock()

    def thread_callback(val):
        with counter_lock:
            counter["count"] += 1

    def worker_register(idx):
        AppSettings.add_observer("PROXY", thread_callback)
        time.sleep(0.01)
        settings.PROXY = f"http://proxy{idx}.com:8080"

    threads = [threading.Thread(target=worker_register, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All mutations should execute without deadlocks or race condition exceptions
    assert counter["count"] > 0


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

    def mock_get_opener(*args, **kwargs):
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
