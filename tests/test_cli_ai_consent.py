"""Tests for explicit CLI flags for AI consent pre-configuration and wizard bypass."""

import asyncio
import os
from unittest.mock import patch

from app.config import AppSettings
from app.main import apply_config_overrides, build_parser
from app.ui.tui import AutoSorterTUI, WizardModal


def test_cli_parser_accept_ai_consent_flag():
    """Verify --accept-ai-consent flag parsing at top level and subcommand."""
    parser = build_parser()

    # Top level
    args = parser.parse_args(["--accept-ai-consent"])
    assert args.accept_ai_consent is True
    assert args.decline_ai_consent is False

    # Subcommand
    args_sub = parser.parse_args(
        ["sort", "--accept-ai-consent", "sandbox/demo_workspace"]
    )
    assert args_sub.accept_ai_consent is True
    assert args_sub.decline_ai_consent is False


def test_cli_parser_decline_ai_consent_flag():
    """Verify --decline-ai-consent flag parsing at top level and subcommand."""
    parser = build_parser()

    # Top level
    args = parser.parse_args(["--decline-ai-consent"])
    assert args.decline_ai_consent is True
    assert args.accept_ai_consent is False

    # Subcommand
    args_sub = parser.parse_args(
        ["scan", "--decline-ai-consent", "sandbox/demo_workspace"]
    )
    assert args_sub.decline_ai_consent is True
    assert args_sub.accept_ai_consent is False


def test_cli_parser_skip_wizard_and_non_interactive_flags():
    """Verify --skip-wizard and --non-interactive flags at top level and subcommand."""
    parser = build_parser()

    args_skip = parser.parse_args(["--skip-wizard"])
    assert args_skip.skip_wizard is True

    args_non_int = parser.parse_args(["--non-interactive"])
    assert args_non_int.non_interactive is True

    args_sub = parser.parse_args(["config", "--skip-wizard", "--non-interactive"])
    assert args_sub.skip_wizard is True
    assert args_sub.non_interactive is True


def test_apply_config_overrides_accept_ai_consent(tmp_path):
    """Verify apply_config_overrides updates settings.AI_CONSENT_GRANTED to True."""
    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings._settings_model.AI_CONSENT_GRANTED = None

    parser = build_parser()
    args = parser.parse_args(["--accept-ai-consent"])

    apply_config_overrides(settings, args)
    assert settings.AI_CONSENT_GRANTED is True


def test_apply_config_overrides_decline_ai_consent(tmp_path):
    """Verify apply_config_overrides updates settings.AI_CONSENT_GRANTED to False."""
    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings._settings_model.AI_CONSENT_GRANTED = None

    parser = build_parser()
    args = parser.parse_args(["--decline-ai-consent"])

    apply_config_overrides(settings, args)
    assert settings.AI_CONSENT_GRANTED is False


def test_apply_config_overrides_skip_wizard_defaults_unconfigured_to_false(tmp_path):
    """Verify --skip-wizard defaults unconfigured consent to False."""
    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings._settings_model.AI_CONSENT_GRANTED = None

    parser = build_parser()
    args = parser.parse_args(["--skip-wizard"])

    apply_config_overrides(settings, args)
    assert settings.AI_CONSENT_GRANTED is False
    assert getattr(settings, "_skip_wizard", False) is True


def test_apply_config_overrides_env_sortify_ai_consent(tmp_path):
    """Verify SORTIFY_AI_CONSENT env var updates settings."""
    settings_file = str(tmp_path / "settings.json")

    # Env var = 1
    settings1 = AppSettings(filepath=settings_file)
    settings1._settings_model.AI_CONSENT_GRANTED = None
    parser = build_parser()
    args1 = parser.parse_args([])
    with patch.dict(os.environ, {"SORTIFY_AI_CONSENT": "1"}):
        apply_config_overrides(settings1, args1)
        assert settings1.AI_CONSENT_GRANTED is True

    # Env var = 0
    settings0 = AppSettings(filepath=settings_file)
    settings0._settings_model.AI_CONSENT_GRANTED = None
    args0 = parser.parse_args([])
    with patch.dict(os.environ, {"SORTIFY_AI_CONSENT": "0"}):
        apply_config_overrides(settings0, args0)
        assert settings0.AI_CONSENT_GRANTED is False


def test_apply_config_overrides_env_non_interactive(tmp_path):
    """Verify NON_INTERACTIVE env var defaults unconfigured consent to False."""
    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings._settings_model.AI_CONSENT_GRANTED = None

    parser = build_parser()
    args = parser.parse_args([])
    with patch.dict(os.environ, {"NON_INTERACTIVE": "1"}):
        apply_config_overrides(settings, args)
        assert settings.AI_CONSENT_GRANTED is False
        assert getattr(settings, "_non_interactive", False) is True


def test_cli_consent_flag_precedence_over_env_and_saved(tmp_path):
    """Verify explicit CLI flag takes precedence over saved settings and env var."""
    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    settings._settings_model.AI_CONSENT_GRANTED = False

    parser = build_parser()
    args = parser.parse_args(["--accept-ai-consent"])

    with patch.dict(os.environ, {"SORTIFY_AI_CONSENT": "0"}):
        apply_config_overrides(settings, args)
        assert settings.AI_CONSENT_GRANTED is True


def test_tui_wizard_modal_bypassed_with_accept_flag(tmp_path):
    """Verify TUI does not launch WizardModal when --accept-ai-consent is specified."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings._settings_model.AI_CONSENT_GRANTED = None

        parser = build_parser()
        args = parser.parse_args(["--accept-ai-consent"])
        apply_config_overrides(settings, args)

        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            assert not isinstance(app.screen, WizardModal)
            assert app.settings.AI_CONSENT_GRANTED is True

    asyncio.run(_test())


def test_tui_wizard_modal_bypassed_with_skip_wizard_flag(tmp_path):
    """Verify TUI does not launch WizardModal when skip_wizard is True."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings._settings_model.AI_CONSENT_GRANTED = None

        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path), skip_wizard=True)
        async with app.run_test() as pilot:
            await pilot.pause(0.1)
            assert not isinstance(app.screen, WizardModal)
            assert app.settings.AI_CONSENT_GRANTED is False

    asyncio.run(_test())


def test_tui_wizard_modal_bypassed_with_non_interactive_env(tmp_path):
    """Verify TUI does not launch WizardModal when NON_INTERACTIVE=1 env var is present."""

    async def _test():
        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings._settings_model.AI_CONSENT_GRANTED = None

        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        with patch.dict(os.environ, {"NON_INTERACTIVE": "1"}):
            async with app.run_test() as pilot:
                await pilot.pause(0.1)
                assert not isinstance(app.screen, WizardModal)
                assert app.settings.AI_CONSENT_GRANTED is False

    asyncio.run(_test())


def test_cli_consent_flags_persist_across_restarts(tmp_path):
    """AC 7 / Req 10: --accept-ai-consent and --decline-ai-consent persist to settings.json across restarts."""
    settings_file = str(tmp_path / "settings.json")
    parser = build_parser()

    settings1 = AppSettings(filepath=settings_file)
    apply_config_overrides(settings1, parser.parse_args(["--accept-ai-consent"]))
    reloaded1 = AppSettings(filepath=settings_file)
    assert reloaded1.AI_CONSENT_GRANTED is True

    settings2 = AppSettings(filepath=settings_file)
    apply_config_overrides(settings2, parser.parse_args(["--decline-ai-consent"]))
    reloaded2 = AppSettings(filepath=settings_file)
    assert reloaded2.AI_CONSENT_GRANTED is False


def test_cli_consent_preserves_interactive_workspace_prompt_unless_non_interactive(
    tmp_path,
):
    """Req 10: --accept-ai-consent alone does not force NON_INTERACTIVE and keeps interactive folder selection."""
    from app.main import resolve_preset_and_directory

    settings_file = str(tmp_path / "settings.json")
    settings = AppSettings(filepath=settings_file)
    parser = build_parser()
    args = parser.parse_args(["--accept-ai-consent"])

    apply_config_overrides(settings, args)
    assert getattr(settings, "_non_interactive", False) is False
    assert settings.NON_INTERACTIVE is False

    chosen_dir = tmp_path / "chosen_docs"
    chosen_dir.mkdir()
    with (
        patch("sys.stdin.isatty", return_value=True),
        patch("builtins.input", side_effect=["5", str(chosen_dir)]),
    ):
        resolved = resolve_preset_and_directory(args, settings)
        assert str(resolved) == str(chosen_dir.resolve())


def test_cli_accept_ai_consent_provisions_model_with_terminal_progress(
    tmp_path, capsys
):
    """AC 7 / Req 11: Accepting AI consent from CLI triggers model provisioning with terminal progress output."""
    from app.main import _maybe_provision_model_for_cli_consent

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    parser = build_parser()
    args = parser.parse_args(["--accept-ai-consent"])
    apply_config_overrides(settings, args)

    def _fake_provision(
        model_dir=None, url=None, proxy=None, force=False, quiet=False, stream=None
    ):
        if stream is not None and not quiet:
            stream.write("Provisioning local AI model bundle into /tmp/model ...\n")
            stream.write("  Downloading model.onnx: 100%\n")
        return {
            "status": "verified",
            "verified": True,
            "downloaded": True,
            "model_dir": str(tmp_path / "model"),
            "detail": "Verified local AI model weights ready on disk.",
        }

    with patch(
        "app.core.downloader.provision_model_cli", side_effect=_fake_provision
    ) as mock_prov:
        _maybe_provision_model_for_cli_consent(args, settings)
        mock_prov.assert_called_once()

    captured = capsys.readouterr()
    assert "Provisioning local AI model bundle" in captured.err


def test_bare_cli_accept_ai_consent_non_interactive_json(tmp_path, capsys):
    """Bare `--accept-ai-consent --non-interactive --json` persists consent, provisions model, and exits cleanly."""
    import json
    import sys

    from app.main import _main_impl

    settings_file = str(tmp_path / "settings.json")

    with (
        patch(
            "app.main.AppSettings",
            side_effect=lambda *a, **kw: AppSettings(filepath=settings_file),
        ),
        patch(
            "app.core.downloader.provision_model_cli",
            return_value={
                "status": "verified",
                "verified": True,
                "downloaded": False,
                "model_dir": str(tmp_path / "model"),
            },
        ) as mock_prov,
        patch.object(
            sys,
            "argv",
            ["smart-autosorter", "--accept-ai-consent", "--non-interactive", "--json"],
        ),
    ):
        _main_impl()

    mock_prov.assert_called_once()
    data = json.loads(capsys.readouterr().out)
    assert data == {"status": "success", "ai_consent_granted": True}
    assert AppSettings(filepath=settings_file).AI_CONSENT_GRANTED is True


def test_subcommand_accept_ai_consent_provisions_unless_deferred_or_tui(
    tmp_path, capsys
):
    """AC 6 / Req 8: scan, sort, drop, and daemon with `--accept-ai-consent` run `provision_model_cli` unless `--defer-model-download` or `--tui` is set."""
    import sys

    import pytest

    from app.core.downloader import NetworkError
    from app.main import _main_impl, _maybe_provision_model_for_cli_consent

    settings_file = str(tmp_path / "settings.json")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "invoice_2025.txt").write_text(
        "Quarterly tax invoice 1099 payment statement.", encoding="utf-8"
    )

    parser = build_parser()

    # 1. Operational subcommands (scan, sort, drop, daemon) call provision_model_cli
    for argv in (
        ["scan", str(workspace), "--accept-ai-consent", "--non-interactive"],
        ["sort", str(workspace), "--accept-ai-consent", "--non-interactive"],
        ["drop", str(workspace / "invoice_2025.txt"), "--accept-ai-consent"],
        ["daemon", str(workspace), "--accept-ai-consent"],
    ):
        sub_args = parser.parse_args(argv)
        settings = AppSettings(filepath=settings_file)
        apply_config_overrides(settings, sub_args)
        with patch("app.core.downloader.provision_model_cli") as mock_prov:
            _maybe_provision_model_for_cli_consent(sub_args, settings)
            mock_prov.assert_called_once()

    # 2. --defer-model-download and --tui / --interactive skip provision_model_cli
    for skip_argv in (
        [
            "scan",
            str(workspace),
            "--accept-ai-consent",
            "--defer-model-download",
            "--non-interactive",
        ],
        ["sort", str(workspace), "--accept-ai-consent", "--tui"],
        ["scan", str(workspace), "--accept-ai-consent", "--interactive"],
        ["config", "--show", "--accept-ai-consent"],
    ):
        skip_args = parser.parse_args(skip_argv)
        settings = AppSettings(filepath=settings_file)
        apply_config_overrides(settings, skip_args)
        with patch("app.core.downloader.provision_model_cli") as mock_prov_skip:
            _maybe_provision_model_for_cli_consent(skip_args, settings)
            mock_prov_skip.assert_not_called()

    # 3. End-to-end `scan <dir> --accept-ai-consent` saves consent and runs provision_model_cli
    settings_e2e = str(tmp_path / "settings_e2e.json")
    with (
        patch(
            "app.main.AppSettings",
            side_effect=lambda *a, **kw: AppSettings(filepath=settings_e2e),
        ),
        patch("app.core.downloader.provision_model_cli") as mock_prov_main,
        patch.object(
            sys,
            "argv",
            [
                "smart-autosorter",
                "scan",
                str(workspace),
                "--accept-ai-consent",
                "--non-interactive",
                "--quiet",
            ],
        ),
        pytest.raises(SystemExit) as excinfo,
    ):
        _main_impl()

    assert excinfo.value.code == 0
    mock_prov_main.assert_called_once()
    assert AppSettings(filepath=settings_e2e).AI_CONSENT_GRANTED is True

    # 4. End-to-end `scan <dir> --accept-ai-consent --defer-model-download` saves consent and skips download
    settings_deferred = str(tmp_path / "settings_deferred.json")
    with (
        patch(
            "app.main.AppSettings",
            side_effect=lambda *a, **kw: AppSettings(filepath=settings_deferred),
        ),
        patch("app.core.downloader.provision_model_cli") as mock_prov_deferred,
        patch.object(
            sys,
            "argv",
            [
                "smart-autosorter",
                "scan",
                str(workspace),
                "--accept-ai-consent",
                "--defer-model-download",
                "--non-interactive",
                "--quiet",
            ],
        ),
        pytest.raises(SystemExit) as excinfo_def,
    ):
        _main_impl()

    assert excinfo_def.value.code == 0
    mock_prov_deferred.assert_not_called()
    assert AppSettings(filepath=settings_deferred).AI_CONSENT_GRANTED is True

    # 5. Offline DownloaderError warns cleanly and falls back without failing scan
    capsys.readouterr()
    with (
        patch(
            "app.main.AppSettings",
            side_effect=lambda *a, **kw: AppSettings(filepath=settings_e2e),
        ),
        patch(
            "app.core.downloader.provision_model_cli",
            side_effect=NetworkError("Offline network"),
        ),
        patch.object(
            sys,
            "argv",
            [
                "smart-autosorter",
                "scan",
                str(workspace),
                "--accept-ai-consent",
                "--non-interactive",
            ],
        ),
        pytest.raises(SystemExit) as excinfo_offline,
    ):
        _main_impl()

    assert excinfo_offline.value.code == 0
    err_out = capsys.readouterr().err
    assert "Proceeding with local keyword/TF-IDF fallback" in err_out
