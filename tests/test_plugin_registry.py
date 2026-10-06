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
