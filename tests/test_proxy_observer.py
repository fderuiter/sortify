"""Tests for thread-safe AppSettings configuration change observers and DownloadManager dynamic proxy hot-reloading."""

import json
import threading
import time

import pytest

from app.config import AppSettings
from app.core.downloader import DownloadManager, NetworkError


@pytest.fixture(autouse=True)
def clean_observers():
    """Ensure AppSettings observers are reset before and after each test."""
    AppSettings.clear_observers()
    yield
    AppSettings.clear_observers()


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
