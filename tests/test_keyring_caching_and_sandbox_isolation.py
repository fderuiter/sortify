"""Regression tests for key caching, bounded keyring probes, and sandbox/demo isolation."""

from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import patch

import keyring
import pytest

import sandbox_cli
from app.core.crypto import CryptoManager, SessionCrypto, get_fallback_keys_dir
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.ledger import KeyAuditLedger
from app.core.model_registry import SharedModelRegistry
from app.core.path_utils import resolve_db_crypto
from app.core.sample_corpus import generate_sample_corpus
from app.core.text_utils import contains_secrets, sanitize_secret_patterns

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_session_crypto_cached_across_threads_and_single_audit_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker threads reuse cached SessionCrypto and emit one KEY_EXPORT per loaded key."""
    keys_dir = tmp_path / "keys"
    monkeypatch.setenv("SORTIFY_KEYS_DIR", str(keys_dir))
    monkeypatch.setattr(keyring, "get_password", lambda *_a, **_k: None)
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no keyring")),
    )
    clear_connection_cache(only_current_and_inactive=False)

    db_path = tmp_path / "shared_session.db"
    ledger_path = str(tmp_path / "audit_ledger.db")

    with patch("app.core.ledger.get_default_ledger_path", return_value=ledger_path):

        def _worker_open(_idx: int) -> tuple[int, str]:
            crypto = resolve_db_crypto(db_path)
            conn = get_db_connection(str(db_path))
            conn.execute("SELECT 1").fetchone()
            return id(crypto), crypto.get_raw_key() or ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(_worker_open, range(6)))

        instance_ids = {inst_id for inst_id, _ in results}
        raw_keys = {rk for _, rk in results}
        assert len(instance_ids) == 1
        assert len(raw_keys) == 1 and next(iter(raw_keys)) != ""

        ledger = KeyAuditLedger(ledger_path)
        db_hash = CryptoManager.derive_db_hash(db_path)
        export_events = ledger.get_key_audit_events(
            event_type="KEY_EXPORT", db_path_hash=db_hash
        )
        assert len(export_events) == 1

        # Rotating the key invalidates cached SessionCrypto and records a fresh export on reopen
        cached_before = resolve_db_crypto(db_path)
        rotated = cached_before.rotate_key()
        cached_after = resolve_db_crypto(db_path)
        assert cached_after is not cached_before
        assert cached_after.get_raw_key() == rotated

        export_events_after = ledger.get_key_audit_events(
            event_type="KEY_EXPORT", db_path_hash=db_hash
        )
        assert len(export_events_after) == 2

        # Concurrent direct get_raw_key() across a barrier emits strictly one new KEY_EXPORT
        fresh_db = tmp_path / "barrier_session.db"
        barrier = __import__("threading").Barrier(6)

        def _barrier_raw_key(_idx: int) -> str:
            c = resolve_db_crypto(fresh_db)
            barrier.wait(timeout=5.0)
            return c.get_raw_key() or ""

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            barrier_keys = list(pool.map(_barrier_raw_key, range(6)))
        assert len(set(barrier_keys)) == 1 and barrier_keys[0] != ""

        fresh_hash = CryptoManager.derive_db_hash(fresh_db)
        barrier_exports = ledger.get_key_audit_events(
            event_type="KEY_EXPORT", db_path_hash=fresh_hash
        )
        assert len(barrier_exports) == 1

        # External rotation via CryptoManager.rotate_database_key resets held cached reference too
        held_ref = resolve_db_crypto(fresh_db)
        ext_rotated = CryptoManager.rotate_database_key(
            fresh_db, fresh_db.parent / "secret.key"
        )
        assert held_ref.get_raw_key() == ext_rotated
        assert resolve_db_crypto(fresh_db).get_raw_key() == ext_rotated

    clear_connection_cache(only_current_and_inactive=False)


def test_sortify_keys_dir_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SORTIFY_KEYS_DIR overrides the fallback keys directory without touching $HOME."""
    custom_keys = tmp_path / "custom-keys"
    monkeypatch.setenv("SORTIFY_KEYS_DIR", str(custom_keys))
    assert get_fallback_keys_dir() == custom_keys

    monkeypatch.delenv("SORTIFY_KEYS_DIR")
    alt_keys = tmp_path / "alt-keys"
    monkeypatch.setenv("AUTOSORTER_KEYS_DIR", str(alt_keys))
    assert get_fallback_keys_dir() == alt_keys


@pytest.mark.parametrize(
    "mode",
    ["demo", "sort", "scan", "sandbox_analyze"],
)
def test_headless_commands_probe_unavailable_keyring_at_most_once(
    tmp_path: Path, mode: str
) -> None:
    """Instrumenting keyring across --demo, sort, scan, and sandbox_cli records <= 1 failed probe."""
    work_dir = tmp_path / mode
    home_dir = work_dir / "home"
    home_dir.mkdir(parents=True)
    counter_file = work_dir / "keyring_calls.json"

    inbox_dir = work_dir / "inbox"
    if mode in ("sort", "scan"):
        inbox_dir.mkdir(parents=True)
        generate_sample_corpus(str(inbox_dir))

    sandbox_root = work_dir / "sandbox"
    if mode == "sandbox_analyze":
        golden = sandbox_root / "dataset_golden"
        golden.mkdir(parents=True)
        generate_sample_corpus(str(golden))

    runner = textwrap.dedent(
        f"""
        import json
        import os
        import sys
        import keyring

        calls = []

        def fail_get(service, username):
            calls.append(("get_password", service, username))
            raise RuntimeError("No recommended backend was available")

        def fail_set(service, username, password):
            calls.append(("set_password", service, username))
            raise RuntimeError("No recommended backend was available")

        keyring.get_password = fail_get
        keyring.set_password = fail_set

        mode = {mode!r}
        try:
            if mode == "demo":
                sys.argv = ["smart-autosorter", "--demo"]
                from app.main import main
                main()
            elif mode == "sort":
                sys.argv = [
                    "smart-autosorter",
                    "sort",
                    {str(inbox_dir)!r},
                    "--dry-run",
                    "--non-interactive",
                    "--decline-ai-consent",
                ]
                from app.main import main
                main()
            elif mode == "scan":
                sys.argv = [
                    "smart-autosorter",
                    "scan",
                    {str(inbox_dir)!r},
                    "--non-interactive",
                    "--decline-ai-consent",
                ]
                from app.main import main
                main()
            elif mode == "sandbox_analyze":
                import sandbox_cli
                sandbox_cli.GOLDEN_DIR = {str(sandbox_root / "dataset_golden")!r}
                sandbox_cli.SANDBOX_DIR = {str(sandbox_root / "dataset")!r}
                sandbox_cli.SANDBOX_DB_PATH = {str(sandbox_root / "sandbox.db")!r}
                sys.argv = ["sandbox_cli.py", "analyze", "-q", "--json"]
                sandbox_cli.main()
        except SystemExit as exc:
            if exc.code not in (None, 0):
                raise
        finally:
            with open({str(counter_file)!r}, "w", encoding="utf-8") as fh:
                json.dump(calls, fh)
        """
    )

    env = dict(os.environ)
    env["HOME"] = str(home_dir)
    env["USERPROFILE"] = str(home_dir)
    env["SMOKE_TEST"] = "0"
    existing_py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{REPO_ROOT}{os.pathsep}{existing_py_path}"
        if existing_py_path
        else str(REPO_ROOT)
    )
    if mode in ("demo", "sort", "scan"):
        env["AUTOSORTER_APP_DIR"] = str(work_dir / "app_state")
        env["SORTIFY_KEYS_DIR"] = str(work_dir / "app_keys")
    else:
        env.pop("AUTOSORTER_APP_DIR", None)
        env.pop("SORTIFY_KEYS_DIR", None)

    run_cwd = str(work_dir) if mode == "demo" else str(REPO_ROOT)
    proc = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=run_cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"mode={mode} failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )

    recorded_calls = json.loads(counter_file.read_text(encoding="utf-8"))
    assert len(recorded_calls) <= 1, (
        f"Expected at most 1 failed keyring probe in {mode}, got {len(recorded_calls)}: {recorded_calls}"
    )

    if mode == "demo":
        assert not (home_dir / ".autosorter").exists()
        assert not (home_dir / ".sortify").exists()
        assert list(home_dir.iterdir()) == []
        assert (work_dir / "sandbox" / "demo_workspace").is_dir()


def test_simulated_25s_dbus_timeout_bounded_to_2s_in_demo(tmp_path: Path) -> None:
    """A 25s hung keyring backend times out within ~2s so --demo finishes in seconds."""
    home_dir = tmp_path / "empty_home"
    home_dir.mkdir()
    demo_cwd = tmp_path / "demo_run"
    demo_cwd.mkdir()
    metrics_file = tmp_path / "metrics.json"

    runner = textwrap.dedent(
        f"""
        import json
        import sys
        import time
        import keyring

        probe_calls = 0

        def hung_dbus_get(service, username):
            global probe_calls
            probe_calls += 1
            time.sleep(25.0)
            return None

        def hung_dbus_set(service, username, password):
            global probe_calls
            probe_calls += 1
            time.sleep(25.0)

        keyring.get_password = hung_dbus_get
        keyring.set_password = hung_dbus_set

        t0 = time.monotonic()
        exit_code = 0
        try:
            sys.argv = ["smart-autosorter", "--demo"]
            from app.main import main
            main()
        except SystemExit as exc:
            exit_code = 0 if exc.code is None else int(exc.code)
        elapsed = time.monotonic() - t0
        with open({str(metrics_file)!r}, "w", encoding="utf-8") as fh:
            json.dump({{"elapsed": elapsed, "probe_calls": probe_calls, "exit_code": exit_code}}, fh)
        sys.exit(exit_code)
        """
    )

    env = dict(os.environ)
    env["HOME"] = str(home_dir)
    env["USERPROFILE"] = str(home_dir)
    env["SMOKE_TEST"] = "0"
    existing_py_path = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{REPO_ROOT}{os.pathsep}{existing_py_path}"
        if existing_py_path
        else str(REPO_ROOT)
    )
    env["AUTOSORTER_APP_DIR"] = str(tmp_path / "app_state")
    env["SORTIFY_KEYS_DIR"] = str(tmp_path / "app_keys")

    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-c", runner],
        cwd=str(demo_cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=24,
    )
    wall_elapsed = time.monotonic() - start
    assert proc.returncode == 0, f"STDERR:\n{proc.stderr}"
    metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
    assert metrics["probe_calls"] == 1
    assert metrics["elapsed"] < 20.0
    assert wall_elapsed < 22.0
    assert list(home_dir.iterdir()) == []
    assert (demo_cwd / "sandbox" / "demo_workspace").is_dir()


def test_fresh_sandbox_auto_seeds_honors_quiet_no_color_and_resets_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sandbox_cli auto-seeds missing dataset, hides banners with -q, encrypts DB, and deletes .key on reset."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    sandbox_base = tmp_path / "sandbox"
    golden_dir = sandbox_base / "dataset_golden"
    dataset_dir = sandbox_base / "dataset"
    sandbox_db = sandbox_base / "sandbox.db"
    golden_dir.mkdir(parents=True)
    generate_sample_corpus(str(golden_dir))

    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.delenv("AUTOSORTER_APP_DIR", raising=False)
    monkeypatch.delenv("SORTIFY_KEYS_DIR", raising=False)
    monkeypatch.setattr(sandbox_cli, "GOLDEN_DIR", str(golden_dir))
    monkeypatch.setattr(sandbox_cli, "SANDBOX_DIR", str(dataset_dir))
    monkeypatch.setattr(sandbox_cli, "SANDBOX_DB_PATH", str(sandbox_db))
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("keyring disabled")),
    )
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("keyring disabled")),
    )
    clear_connection_cache(only_current_and_inactive=False)

    assert not dataset_dir.exists()

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with (
        patch("sys.argv", ["sandbox_cli.py", "analyze", "-q", "--no-color", "--json"]),
        patch("sys.stdout", out_buf),
        patch("sys.stderr", err_buf),
    ):
        sandbox_cli.main()

    assert dataset_dir.is_dir()
    assert err_buf.getvalue() == ""
    parsed_plan = json.loads(out_buf.getvalue())
    assert isinstance(parsed_plan, dict) and len(parsed_plan) > 0

    # Verify SQLCipher encryption on the sandbox store and isolated key location
    state_db = sandbox_base / "pipeline-state" / "sortify.db"
    assert state_db.exists()
    assert not state_db.read_bytes().startswith(b"SQLite format 3")
    sandbox_keys = list((sandbox_base / "pipeline-state" / "keys").glob("*.key"))
    assert len(sandbox_keys) >= 1
    assert list(home_dir.iterdir()) == []

    # Verify reset removes all .key files and pipeline-state while keeping dataset seeded
    reset_err = io.StringIO()
    with (
        patch("sys.argv", ["sandbox_cli.py", "-q", "reset"]),
        patch("sys.stdout", io.StringIO()),
        patch("sys.stderr", reset_err),
    ):
        sandbox_cli.main()

    assert reset_err.getvalue() == ""
    assert list(sandbox_base.rglob("*.key")) == []
    assert not (sandbox_base / "pipeline-state").exists()
    assert dataset_dir.is_dir()


def test_startup_logging_buffers_warnings_and_preserves_db_key_identifiers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Startup warnings before basicConfig are written to LOG_FILE with DatabaseDecryptionKey_ preserved."""
    app_dir = tmp_path / "app_cfg"
    keys_dir = tmp_path / "keys_cfg"
    app_dir.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    monkeypatch.setenv("SORTIFY_KEYS_DIR", str(keys_dir))

    # Create encrypted proxy in settings.json whose keyring lookup will fail during AppSettings()
    settings_path = app_dir / "settings.json"
    crypto = SessionCrypto(app_dir / "secret.key", settings_path)
    monkeypatch.setattr(keyring, "get_password", lambda *_a, **_k: None)
    monkeypatch.setattr(
        keyring,
        "set_password",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("no keyring")),
    )
    clear_connection_cache(only_current_and_inactive=False)
    enc_proxy = CryptoManager.encrypt_proxy_setting(
        "http://127.0.0.1:8080", crypto=crypto
    )
    settings_path.write_text(json.dumps({"PROXY": enc_proxy}), encoding="utf-8")

    # Now make keyring.get_password fail during AppSettings() startup before basicConfig
    monkeypatch.setattr(
        keyring,
        "get_password",
        lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("No recommended backend was available")
        ),
    )
    clear_connection_cache(only_current_and_inactive=False)

    from types import SimpleNamespace

    from app.main import _configure_settings_and_logging

    tracked: list[logging.Handler] = []
    args = SimpleNamespace(
        max_workers=None,
        max_folders=None,
        max_depth=None,
        max_features=None,
        contextual_renaming=None,
        preserve_hierarchy=None,
        cleanup_empty_folders=None,
        conflict_policy=None,
        sorting_strategy=None,
        coherence_threshold=None,
        vision_engine=None,
        ocr_languages=None,
        ocr_gpu_enabled=None,
        audio_gpu_enabled=None,
        audio_max_workers=None,
        accept_ai_consent=False,
        decline_ai_consent=False,
        non_interactive=True,
        skip_wizard=True,
    )
    loaded_settings = None
    try:
        loaded_settings = _configure_settings_and_logging(
            args, tracked_handlers=tracked
        )
        for handler in tracked:
            handler.flush()
        log_file = Path(loaded_settings.LOG_FILE)
        assert log_file.exists()
        content = log_file.read_text(encoding="utf-8")
        expected_account = CryptoManager.derive_keyring_account(settings_path)
        assert expected_account in content
        assert f"'{expected_account}':" in content
    finally:
        if loaded_settings is not None:
            save_timer = loaded_settings._save_timer
            if save_timer is not None:
                save_timer.cancel()
        root_logger = logging.getLogger()
        for handler in tracked:
            root_logger.removeHandler(handler)
            handler.close()
        clear_connection_cache(only_current_and_inactive=False)


def test_sanitize_secret_patterns_preserves_db_key_id_and_prefix() -> None:
    """DatabaseDecryptionKey_<sha256> stays intact while real secrets after prefix keep the prefix."""
    high_entropy_hash = CryptoManager.derive_db_hash(
        "/app/sortify/sandbox/pipeline-state/sortify.db"
    )
    warning_msg = (
        f"Keyring lookup failed for account 'DatabaseDecryptionKey_{high_entropy_hash}': "
        "No recommended backend was available."
    )
    assert contains_secrets(warning_msg) is False
    assert sanitize_secret_patterns(warning_msg) == warning_msg

    compound_kv = f"Keyring lookup failed (account='DatabaseDecryptionKey_{high_entropy_hash}'): unavailable."
    assert contains_secrets(compound_kv) is False
    assert sanitize_secret_patterns(compound_kv) == compound_kv

    redacted_compound = sanitize_secret_patterns(
        "Leaked 'DatabaseDecryptionKey_gAAAAABk1234567890abcdefsecretkeybytes==': check",
        replacement="[REDACTED]",
    )
    assert "'DatabaseDecryptionKey_[REDACTED]':" in redacted_compound
    assert "gAAAAABk1234567890abcdefsecretkeybytes" not in redacted_compound


def test_in_process_main_closes_log_file_handler_on_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In-process CLI runs detach and close their LOG_FILE FileHandler on exit (Windows safety)."""
    app_dir = tmp_path / "cli_app_dir"
    keys_dir = tmp_path / "cli_keys_dir"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    monkeypatch.setenv("SORTIFY_KEYS_DIR", str(keys_dir))

    from app.main import main

    with (
        patch("sys.argv", ["smart-autosorter", "config", "--show", "--quiet"]),
        patch("sys.stdout", io.StringIO()),
        patch("sys.stderr", io.StringIO()),
    ):
        with contextlib.suppress(SystemExit):
            main()

    expected_log = os.path.normcase(str((app_dir / "autosorter.log").resolve()))
    leaked_handlers = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.FileHandler)
        and os.path.normcase(os.path.abspath(getattr(h, "baseFilename", "")))
        == expected_log
    ]
    assert leaked_handlers == []


def test_model_registry_missing_easyocr_emits_zero_tracebacks(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Missing optional easyocr logs cleanly at INFO without tracebacks or duplicate errors."""
    monkeypatch.setitem(sys.modules, "easyocr", None)
    SharedModelRegistry._instance = None
    registry = SharedModelRegistry.get_instance()

    with caplog.at_level(logging.INFO):
        assert registry.get_ocr_reader() is None
        assert registry.get_ocr_reader() is None

    assert "Traceback" not in caplog.text
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records == []
    exc_records = [r for r in caplog.records if r.exc_info]
    assert exc_records == []


def test_in_process_bounded_keyring_timeout_delete_and_selective_cache_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify bounded keyring timeout, delete_password probe handling, and selective cache eviction."""
    from app.core import crypto as crypto_mod

    monkeypatch.setenv("SORTIFY_KEYS_DIR", str(tmp_path / "keys"))
    monkeypatch.setattr(crypto_mod, "_KEYRING_PROBE_TIMEOUT_SECONDS", 0.05)
    crypto_mod._reset_keyring_probe_cache()
    crypto_mod._clear_session_crypto_cache()

    release_hung = __import__("threading").Event()
    calls: list[str] = []

    def _slow_get(_service: str, _account: str) -> str | None:
        calls.append("get")
        release_hung.wait(timeout=0.3)
        return None

    monkeypatch.setattr(keyring, "get_password", _slow_get)
    try:
        assert (
            crypto_mod._keyring_get_password(
                "AutoSorter", "acct", failure_message="probe timed out"
            )
            is None
        )
        # Subsequent get, set, and delete short-circuit without invoking keyring
        assert crypto_mod._keyring_get_password("AutoSorter", "acct2") is None
        assert (
            crypto_mod._keyring_set_password("AutoSorter", "acct2", "secret") is False
        )
        assert crypto_mod._keyring_delete_password("AutoSorter", "acct2") is False
        assert calls == ["get"]
    finally:
        release_hung.set()
        time.sleep(0.05)

    # Verify _keyring_delete_password both on success and backend failure when read is available
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(keyring, "get_password", lambda *_a: "val")
    monkeypatch.setattr(keyring, "delete_password", lambda s, a: deleted.append((s, a)))
    crypto_mod._reset_keyring_probe_cache()
    assert crypto_mod._keyring_get_password("AutoSorter", "acct") == "val"
    assert crypto_mod._keyring_delete_password("AutoSorter", "acct") is True
    assert deleted == [("AutoSorter", "acct")]

    monkeypatch.setattr(
        keyring,
        "delete_password",
        lambda *_a: (_ for _ in ()).throw(RuntimeError("cannot delete")),
    )
    assert crypto_mod._keyring_delete_password("AutoSorter", "acct") is False

    # Verify selective _clear_session_crypto_cache(keep_db_paths=...)
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    inst_a = resolve_db_crypto(db_a)
    inst_b = resolve_db_crypto(db_b)
    crypto_mod._clear_session_crypto_cache(keep_db_paths={str(db_a)})
    assert resolve_db_crypto(db_a) is inst_a
    assert resolve_db_crypto(db_b) is not inst_b
    clear_connection_cache(only_current_and_inactive=False)
