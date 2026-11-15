"""Regression coverage for credential-safe proxy persistence."""

import json

import pytest

from app.config import AppSettings
from app.core.crypto import CryptoManager


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setattr(AppSettings, "_trigger_save", lambda self: None)
    return AppSettings(str(tmp_path / "settings.json"))


@pytest.mark.parametrize("previous", [None, "enc:previous-ciphertext"])
@pytest.mark.parametrize("failure", ["key", "encrypt"])
def test_encryption_failure_never_persists_or_logs_credentials(
    settings, monkeypatch, caplog, previous, failure
):
    secret = "http://proxy-user:private-password@proxy.invalid:8080"
    settings._raw_encrypted_proxy = previous
    settings.PROXY = secret
    settings.MAX_WORKERS = 3

    def fail(*args, **kwargs):
        raise RuntimeError(f"Could not encrypt {secret}")

    monkeypatch.setattr(
        "app.core.path_utils.resolve_db_crypto",
        fail if failure == "key" else lambda _: None,
    )
    if failure == "encrypt":
        monkeypatch.setattr(CryptoManager, "encrypt_proxy_setting", fail)
    settings._save()

    disk = json.loads(open(settings._filepath, encoding="utf-8").read())
    assert disk.get("PROXY") == previous
    assert disk["MAX_WORKERS"] == 3
    assert settings.PROXY == secret
    for sensitive in (secret, "proxy-user", "private-password"):
        assert sensitive not in json.dumps(disk)
        assert sensitive not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_successful_save_keeps_fallback_until_proxy_is_cleared(settings, monkeypatch):
    monkeypatch.setattr("app.core.path_utils.resolve_db_crypto", lambda _: None)
    monkeypatch.setattr(
        CryptoManager, "encrypt_proxy_setting", lambda value, **kwargs: "enc:last-good"
    )
    settings.PROXY = "http://first.invalid:8080"
    settings._save()
    assert settings._raw_encrypted_proxy == "enc:last-good"
    settings.PROXY = "http://edited.invalid:8080"
    assert settings._raw_encrypted_proxy == "enc:last-good"
    settings.PROXY = ""
    settings._save()
    assert settings._raw_encrypted_proxy is None
    assert json.loads(open(settings._filepath, encoding="utf-8").read())["PROXY"] == ""


def test_already_encrypted_proxy_is_not_encrypted_twice(settings, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Already encrypted settings must not resolve another key")

    monkeypatch.setattr("app.core.path_utils.resolve_db_crypto", unexpected)
    settings.PROXY = "enc:existing"
    settings._save()
    assert settings._raw_encrypted_proxy == "enc:existing"


@pytest.mark.parametrize("previous", [None, "enc:unreadable"])
def test_decryption_placeholder_never_replaces_ciphertext(settings, previous):
    settings._raw_encrypted_proxy = previous
    settings.PROXY = "<DECRYPTION_FAILED>"
    settings._save()
    assert (
        json.loads(open(settings._filepath, encoding="utf-8").read()).get("PROXY")
        == previous
    )
