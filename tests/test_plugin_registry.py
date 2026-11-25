"""Unit tests for PluginRegistry, dynamic plugin loading, and fallback isolation."""

import logging

import pytest

from app.config import AppSettings
from app.core.analyzer_strategies import clustering_registry
from app.core.plugin_registry import PluginRegistry


@pytest.fixture(autouse=True)
def reset_plugin_registry_fixture():
    """Reset PluginRegistry before and after each test."""
    PluginRegistry.reset_instance()
    yield
    PluginRegistry.reset_instance()


def test_plugin_registry_singleton():
    """Verify PluginRegistry behaves as a thread-safe singleton."""
    reg1 = PluginRegistry.get_instance()
    reg2 = PluginRegistry.get_instance()
    assert reg1 is reg2


def test_quarantine_scan_hook_registration_and_trigger():
    """Verify quarantine scan hooks execute and handle errors in isolation."""
    registry = PluginRegistry.get_instance()
    executed = []

    def mock_hook_1(orig_path, text, base_dir):
        executed.append("hook1")

    def failing_hook(orig_path, text, base_dir):
        raise RuntimeError("Simulated plugin crash")

    def mock_hook_2(orig_path, text, base_dir):
        executed.append("hook2")

    registry.register_quarantine_scan_hook(mock_hook_1)
    registry.register_quarantine_scan_hook(failing_hook)
    registry.register_quarantine_scan_hook(mock_hook_2)

    # Triggering should run hook1 and hook2 safely despite failing_hook throwing an exception
    registry.trigger_quarantine_scan("test.txt", "some text", "/tmp")

    assert executed == ["hook1", "hook2"]


def test_load_clinical_compliance_plugin():
    """Verify loading clinical compliance plugin registers strategies, hooks, and views."""
    registry = PluginRegistry.get_instance()
    success = registry.load_plugin("clinical_compliance")
    assert success is True

    # Check strategies registered in ClusteringRegistry
    strat_tmf = clustering_registry.get_strategy("clinical_tmf")
    assert strat_tmf is not None

    strat_isf = clustering_registry.get_strategy("clinical_isf")
    assert strat_isf is not None

    # Check TUI strategy options
    tui_options = registry.get_tui_strategy_options()
    assert ("Clinical TMF", "clinical_tmf") in tui_options
    assert ("Clinical ISF", "clinical_isf") in tui_options

    # Check TUI view
    view_cls = registry.get_tui_view("cro_forensic")
    assert view_cls is not None


def test_load_missing_plugin_graceful_fallback(caplog):
    """Verify loading a non-existent plugin logs a warning and fails gracefully without raising."""
    registry = PluginRegistry.get_instance()
    with caplog.at_level(logging.WARNING):
        success = registry.load_plugin("non_existent_plugin_xyz")
        assert success is False

    assert "Could not load plugin 'non_existent_plugin_xyz'" in caplog.text


def test_load_plugins_from_settings():
    """Verify load_plugins_from_settings loads plugins listed in settings."""
    settings = AppSettings()
    settings._settings_model.PLUGINS = ["clinical_compliance"]

    registry = PluginRegistry.get_instance()
    registry.load_plugins_from_settings(settings)

    assert registry.get_tui_view("cro_forensic") is not None


def test_default_isolation_when_no_plugin_active():
    """Verify default operation when no plugin is active has no clinical strategies or hooks."""
    registry = PluginRegistry.get_instance()
    assert len(registry.get_tui_strategy_options()) == 0
    assert registry.get_tui_view("cro_forensic") is None


def test_disallowed_stdlib_module_rejection_no_import(mocker, caplog):
    """Verify raw stdlib module names like os/sys/subprocess fail safely and NEVER invoke importlib.import_module on raw name."""
    import importlib

    registry = PluginRegistry.get_instance()
    spy_import = mocker.spy(importlib, "import_module")

    with caplog.at_level(logging.WARNING):
        for raw_mod in ["os", "sys", "subprocess", "math", "shutil"]:
            success = registry.load_plugin(raw_mod)
            assert success is False

    imported_args = [call.args[0] for call in spy_import.call_args_list]
    for forbidden in ["os", "sys", "subprocess", "math", "shutil"]:
        assert forbidden not in imported_args


def test_unprefixed_module_path_rejection(mocker, caplog):
    """Verify un-prefixed arbitrary module paths outside allowed prefixes are rejected without raw import."""
    import importlib

    registry = PluginRegistry.get_instance()
    spy_import = mocker.spy(importlib, "import_module")

    with caplog.at_level(logging.WARNING):
        success = registry.load_plugin("some_external_package.some_module")
        assert success is False

    imported_args = [call.args[0] for call in spy_import.call_args_list]
    assert "some_external_package.some_module" not in imported_args
    assert "app.plugins.some_external_package.some_module" in imported_args


def test_config_validation_rejects_disallowed_plugins():
    """Verify Settings model rejects raw stdlib or un-prefixed module paths in PLUGINS configuration."""
    from pydantic import ValidationError

    from app.config import Settings

    for bad_plugin in ["os", "sys", "subprocess", "some_external_pkg.mod"]:
        with pytest.raises(ValidationError):
            Settings(PLUGINS=[bad_plugin])


def test_valid_plugins_in_settings():
    """Verify Settings accepts valid plugin names and prefixed module paths."""
    from app.config import Settings

    s1 = Settings(PLUGINS=["clinical_compliance"])
    assert s1.PLUGINS == ["clinical_compliance"]

    s2 = Settings(PLUGINS=["app.plugins.clinical_compliance"])
    assert s2.PLUGINS == ["app.plugins.clinical_compliance"]

    s3 = Settings(PLUGINS=["sortify_plugin_external"])
    assert s3.PLUGINS == ["sortify_plugin_external"]


def test_app_settings_graceful_handling_of_invalid_plugins_config(tmp_path, caplog):
    """Verify AppSettings handling invalid plugin in settings file logs warning and resets to default PLUGINS."""
    settings_file = tmp_path / "settings.json"
    settings_file.write_text('{"PLUGINS": ["os"]}', encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        app_settings = AppSettings(filepath=str(settings_file))
        assert app_settings.PLUGINS == []

    assert (
        "Invalid PLUGINS in config" in caplog.text
        or "Forbidden plugin module path" in caplog.text
    )


def test_opt_in_default_settings_do_not_load_clinical_plugin(tmp_path):
    """Verify default settings leave clinical strategies, switches, commands, and CLI choices inactive."""
    settings_path = tmp_path / "settings.json"
    settings = AppSettings(filepath=str(settings_path))
    assert settings.PLUGINS == []

    registry = PluginRegistry.get_instance()
    registry.load_plugins_from_settings(settings)

    assert registry.get_tui_strategy_options() == []
    assert registry.get_tui_switches() == []
    assert registry.get_tui_commands() == []
    assert registry.get_cli_strategies() == []
    assert registry.get_tui_view("cro_forensic") is None
    assert clustering_registry.get_strategy("clinical_tmf") is None
    assert clustering_registry.get_strategy("clinical_isf") is None


@pytest.mark.anyio
async def test_ui_and_cli_surfaces_toggle_with_clinical_plugin_opt_in(
    tmp_path, monkeypatch
):
    """Verify SettingsModal, CommandPaletteModal, and CLI help hide clinical items by default and show them when enabled."""
    from textual.widgets import Select

    from app.main import build_parser
    from app.ui.modals.command_palette import CommandPaletteModal
    from app.ui.modals.settings import SettingsModal
    from app.ui.tui import AutoSorterTUI

    app_dir = tmp_path / "app_default"
    app_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    PluginRegistry.reset_instance()

    default_settings = AppSettings(filepath=str(app_dir / "settings.json"))
    default_settings._settings_model.AI_CONSENT_GRANTED = True

    import argparse

    def _subparser_help(parser: argparse.ArgumentParser, subcmd: str) -> str:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction) and isinstance(
                action.choices, dict
            ):
                return action.choices[subcmd].format_help()
        raise AssertionError(f"Missing subparser {subcmd}")

    default_parser = build_parser()
    sort_help_default = _subparser_help(default_parser, "sort")
    assert "{default,generative}" in sort_help_default
    assert "clinical_tmf" not in sort_help_default
    assert "clinical_isf" not in sort_help_default

    default_palette = CommandPaletteModal(settings=default_settings)
    assert not any(cmd.id == "open_cro_forensic" for cmd in default_palette.commands)

    default_app = AutoSorterTUI(
        settings=default_settings, base_dir=str(tmp_path), skip_wizard=True
    )
    async with default_app.run_test() as pilot:
        default_app.action_open_settings()
        await pilot.pause(0.1)
        modal = default_app.screen
        assert isinstance(modal, SettingsModal)
        strat_select = modal.query_one("#select-strategy", Select)
        option_values = [
            val for _label, val in strat_select._options if isinstance(val, str)
        ]
        assert option_values == ["default", "generative"]
        assert len(modal.query("#switch-clinical-renaming")) == 0

    # Now enable clinical_compliance and verify all clinical controls appear
    enabled_dir = tmp_path / "app_enabled"
    enabled_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(enabled_dir))
    PluginRegistry.reset_instance()

    enabled_settings = AppSettings(filepath=str(enabled_dir / "settings.json"))
    enabled_settings.AI_CONSENT_GRANTED = True
    enabled_settings.PLUGINS = ["clinical_compliance"]
    enabled_settings.flush()

    enabled_parser = build_parser()
    sort_help_enabled = _subparser_help(enabled_parser, "sort")
    assert "clinical_tmf" in sort_help_enabled
    assert "clinical_isf" in sort_help_enabled

    enabled_palette = CommandPaletteModal(settings=enabled_settings)
    assert any(cmd.id == "open_cro_forensic" for cmd in enabled_palette.commands)

    enabled_app = AutoSorterTUI(
        settings=enabled_settings, base_dir=str(tmp_path), skip_wizard=True
    )
    async with enabled_app.run_test() as pilot:
        enabled_app.action_open_settings()
        await pilot.pause(0.1)
        modal_en = enabled_app.screen
        assert isinstance(modal_en, SettingsModal)
        strat_select_en = modal_en.query_one("#select-strategy", Select)
        option_values_en = [
            val for _label, val in strat_select_en._options if isinstance(val, str)
        ]
        assert option_values_en == [
            "default",
            "generative",
            "clinical_tmf",
            "clinical_isf",
        ]
        assert len(modal_en.query("#switch-clinical-renaming")) == 1


def test_legacy_clinical_config_migration_is_idempotent_and_preserves_values(tmp_path):
    """Verify legacy clinical configs migrate idempotently to enable clinical_compliance without losing preferences."""
    import json

    from app.plugins.clinical_compliance.plugin import migrate_legacy_settings

    # 1. Legacy clinical strategy config
    strat_cfg_file = tmp_path / "legacy_strategy.json"
    strat_cfg_file.write_text(
        json.dumps(
            {
                "SORTING_STRATEGY": "clinical_tmf",
                "MAX_FOLDERS": 9,
                "CONFLICT_POLICY": "skip",
            }
        ),
        encoding="utf-8",
    )

    loaded_1 = AppSettings(filepath=str(strat_cfg_file))
    loaded_1.flush()
    assert "clinical_compliance" in loaded_1.PLUGINS
    assert loaded_1.SORTING_STRATEGY == "clinical_tmf"
    assert loaded_1.MAX_FOLDERS == 9
    assert loaded_1.CONFLICT_POLICY == "skip"

    persisted_1 = json.loads(strat_cfg_file.read_text(encoding="utf-8"))
    assert persisted_1["PLUGINS"] == ["clinical_compliance"]
    assert persisted_1["SORTING_STRATEGY"] == "clinical_tmf"
    assert persisted_1["MAX_FOLDERS"] == 9
    assert migrate_legacy_settings(persisted_1) is False

    # Reloading migrated file remains idempotent
    PluginRegistry.reset_instance()
    loaded_2 = AppSettings(filepath=str(strat_cfg_file))
    assert loaded_2.PLUGINS == ["clinical_compliance"]
    assert loaded_2.MAX_FOLDERS == 9

    # 2. Legacy clinical smart renaming toggle config
    PluginRegistry.reset_instance()
    renaming_cfg_file = tmp_path / "legacy_renaming.json"
    renaming_cfg_file.write_text(
        json.dumps(
            {
                "SORTING_STRATEGY": "default",
                "CLINICAL_SMART_RENAMING": True,
                "CLINICAL_SUPPRESS_DUPLICATES": True,
                "MAX_WORKERS": 4,
            }
        ),
        encoding="utf-8",
    )

    loaded_ren = AppSettings(filepath=str(renaming_cfg_file))
    loaded_ren.flush()
    assert loaded_ren.PLUGINS == ["clinical_compliance"]
    assert loaded_ren.get_plugin_setting("CLINICAL_SMART_RENAMING") is True
    assert loaded_ren.get_plugin_setting("CLINICAL_SUPPRESS_DUPLICATES") is True
    assert loaded_ren.MAX_WORKERS == 4

    persisted_ren = json.loads(renaming_cfg_file.read_text(encoding="utf-8"))
    assert "CLINICAL_SMART_RENAMING" not in persisted_ren
    assert persisted_ren["PLUGIN_SETTINGS"]["CLINICAL_SMART_RENAMING"] is True
    assert persisted_ren["PLUGIN_SETTINGS"]["CLINICAL_SUPPRESS_DUPLICATES"] is True
    assert migrate_legacy_settings(persisted_ren) is False

    # 3. Legacy inactive default keys must NOT auto-enable clinical_compliance
    PluginRegistry.reset_instance()
    inactive_cfg_file = tmp_path / "legacy_inactive_defaults.json"
    inactive_cfg_file.write_text(
        json.dumps(
            {
                "SORTING_STRATEGY": "default",
                "CLINICAL_SMART_RENAMING": False,
                "CLINICAL_GENERATE_AUDIT_REPORT": True,
                "CLINICAL_SUPPRESS_DUPLICATES": False,
            }
        ),
        encoding="utf-8",
    )
    loaded_inactive = AppSettings(filepath=str(inactive_cfg_file))
    loaded_inactive.flush()
    assert loaded_inactive.PLUGINS == []


def test_cli_single_run_strategy_override_does_not_overwrite_disk(
    tmp_path, monkeypatch
):
    """Verify single-run CLI --strategy override does not overwrite saved SORTING_STRATEGY on disk."""
    import json

    from app.main import apply_config_overrides, build_parser

    app_dir = tmp_path / "app_cli"
    app_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    settings_file = app_dir / "settings.json"

    settings = AppSettings(filepath=str(settings_file))
    settings.PLUGINS = ["clinical_compliance"]
    settings.SORTING_STRATEGY = "default"
    settings.flush()

    parser = build_parser()
    args = parser.parse_args(["sort", str(tmp_path), "--strategy", "clinical_tmf"])
    apply_config_overrides(settings, args)

    assert settings.SORTING_STRATEGY == "clinical_tmf"
    settings._save()

    on_disk = json.loads(settings_file.read_text(encoding="utf-8"))
    assert on_disk["SORTING_STRATEGY"] == "default"
    assert on_disk["PLUGINS"] == ["clinical_compliance"]


def test_offline_clinical_strategies_route_into_tmf_and_isf_without_embeddings(
    tmp_path, monkeypatch
):
    """Verify clinical_tmf and clinical_isf run deterministically offline without embeddings or network calls."""
    from app.core.network_guard import block_external_network
    from app.core.pipeline import SortPipeline
    from app.core.session import AppSession

    app_dir = tmp_path / "offline_app"
    app_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    monkeypatch.setenv("SORTIFY_KEYS_DIR", str(tmp_path / "keys"))

    workspace = tmp_path / "trial_docs"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "1572_form_signed.txt").write_text(
        "DEPARTMENT OF HEALTH AND HUMAN SERVICES FOOD AND DRUG ADMINISTRATION "
        "STATEMENT OF INVESTIGATOR Form FDA 1572 OMB No. 0910-0014",
        encoding="utf-8",
    )
    (workspace / "study_protocol_v1.txt").write_text(
        "Clinical Study Protocol Version 1.0 Protocol Number PROTO-2026 "
        "Schedule of assessments inclusion criteria exclusion criteria",
        encoding="utf-8",
    )
    (workspace / "irb_approval.txt").write_text(
        "Institutional Review Board IRB Approval Letter Effective Date of Approval FWA00001234",
        encoding="utf-8",
    )

    for mode, expected_folder in (
        ("clinical_tmf", "05 Site Management"),
        ("clinical_isf", "03_FDA_Form_1572_and_Agreements"),
    ):
        PluginRegistry.reset_instance()
        settings = AppSettings(filepath=str(app_dir / f"settings_{mode}.json"))
        settings.AI_CONSENT_GRANTED = False
        settings.PLUGINS = ["clinical_compliance"]
        settings.SORTING_STRATEGY = mode
        settings.set_plugin_setting("CLINICAL_GENERATE_AUDIT_REPORT", False)
        settings.flush()

        with block_external_network(
            reason="offline deterministic clinical strategy test"
        ):
            session = AppSession(settings, base_dir=str(workspace))
            try:
                assert session.analyzer.embedding_manager.is_mock is True
                assert session.analyzer.strategy_name == mode
                plan = SortPipeline(session).plan()
            finally:
                session.close()

        plan_dict = plan.plan if hasattr(plan, "plan") else plan
        assert expected_folder in plan_dict


def test_core_cli_and_tui_modules_have_zero_hardcoded_clinical_plugin_references():
    """Guardrail verifying core config, session, CLI, and TUI modules contain zero clinical plugin names."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parent.parent
    guarded_paths = [
        repo_root / "app" / "config.py",
        repo_root / "app" / "core" / "plugin_registry.py",
        repo_root / "app" / "core" / "session.py",
        repo_root / "app" / "core" / "analyzer.py",
        repo_root / "app" / "main.py",
        repo_root / "app" / "ui" / "modals" / "settings.py",
        repo_root / "app" / "ui" / "modals" / "command_palette.py",
        repo_root / "app" / "ui" / "tui.py",
        repo_root / "app" / "ui" / "catalog.py",
    ]
    forbidden_tokens = (
        "clinical_compliance",
        "clinical_tmf",
        "clinical_isf",
        "CLINICAL_SMART_RENAMING",
        "CLINICAL_GENERATE_AUDIT_REPORT",
        "CLINICAL_SUPPRESS_DUPLICATES",
        "cro_forensic",
        "CROForensicModal",
    )

    for file_path in guarded_paths:
        content = file_path.read_text(encoding="utf-8")
        for token in forbidden_tokens:
            assert token not in content, (
                f"Forbidden clinical plugin token '{token}' found in {file_path}"
            )
