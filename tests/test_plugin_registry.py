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
    assert registry.has_tui_view("cro_forensic") is True
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

    assert registry.has_tui_view("cro_forensic") is True


def test_default_isolation_when_no_plugin_active():
    """Verify default operation when no plugin is active has no clinical strategies or hooks."""
    registry = PluginRegistry.get_instance()
    assert len(registry.get_quarantine_scan_hooks()) == 0
    assert len(registry.get_tui_strategy_options()) == 0
    assert registry.has_tui_view("cro_forensic") is False


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

    assert "Invalid PLUGINS in config" in caplog.text or "Forbidden plugin module path" in caplog.text
