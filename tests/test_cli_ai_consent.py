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
    args_sub = parser.parse_args(["sort", "--accept-ai-consent", "sandbox/demo_workspace"])
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
    args_sub = parser.parse_args(["scan", "--decline-ai-consent", "sandbox/demo_workspace"])
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
