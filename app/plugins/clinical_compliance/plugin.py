"""Clinical Compliance Plugin Entry Point."""

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_ID = "clinical_compliance"
PLUGIN_ALIASES = frozenset(
    {
        "clinical_compliance",
        "app.plugins.clinical_compliance",
        "app.plugins.clinical_compliance.plugin",
    }
)
PLUGIN_STRATEGIES: tuple[str, ...] = ("clinical_tmf", "clinical_isf")
PLUGIN_CONFIG_DEFAULTS: dict[str, Any] = {
    "CLINICAL_SMART_RENAMING": False,
    "CLINICAL_GENERATE_AUDIT_REPORT": True,
    "CLINICAL_SUPPRESS_DUPLICATES": False,
}


def read_clinical_setting(settings: Any, key: str, default: Any = None) -> Any:
    """Read a clinical plugin configuration setting from ``settings`` with safe fallback."""
    fallback = PLUGIN_CONFIG_DEFAULTS.get(key, default) if default is None else default
    if settings is None:
        return fallback
    getter = getattr(settings, "get_plugin_setting", None)
    if callable(getter):
        return getter(key, fallback)
    plugin_map = getattr(settings, "PLUGIN_SETTINGS", None)
    if isinstance(plugin_map, dict) and key in plugin_map:
        return plugin_map[key]
    raw_dict = getattr(settings, "__dict__", None)
    if isinstance(raw_dict, dict) and key in raw_dict:
        return raw_dict[key]
    return fallback


def migrate_legacy_settings(data: dict[str, Any]) -> bool:
    """Idempotently migrate legacy clinical configuration keys and auto-enable the plugin when used."""
    if not isinstance(data, dict):
        return False

    changed = False
    raw_plugin_settings = data.get("PLUGIN_SETTINGS")
    plugin_settings: dict[str, Any] = (
        dict(raw_plugin_settings) if isinstance(raw_plugin_settings, dict) else {}
    )
    plugin_settings_modified = False

    legacy_values: dict[str, Any] = {}
    for key, default_val in PLUGIN_CONFIG_DEFAULTS.items():
        if key in data:
            legacy_val = data.pop(key)
            changed = True
            legacy_values[key] = legacy_val
            if key not in plugin_settings and legacy_val != default_val:
                plugin_settings[key] = legacy_val
                plugin_settings_modified = True

    effective_values: dict[str, Any] = {}
    for key, default_val in PLUGIN_CONFIG_DEFAULTS.items():
        if key in plugin_settings:
            effective_values[key] = plugin_settings[key]
        elif key in legacy_values:
            effective_values[key] = legacy_values[key]
        else:
            effective_values[key] = default_val

    raw_strategy = str(data.get("SORTING_STRATEGY", "") or "").strip()
    uses_clinical_strategy = raw_strategy.startswith("clinical")
    uses_clinical_options = (
        bool(effective_values.get("CLINICAL_SMART_RENAMING", False)) is True
        or effective_values.get("CLINICAL_GENERATE_AUDIT_REPORT", True) is False
        or bool(effective_values.get("CLINICAL_SUPPRESS_DUPLICATES", False)) is True
    )

    if uses_clinical_strategy or uses_clinical_options:
        raw_plugins = data.get("PLUGINS")
        if isinstance(raw_plugins, list):
            plugin_list = list(raw_plugins)
        elif isinstance(raw_plugins, (tuple, set)):
            plugin_list = list(raw_plugins)
            changed = True
        else:
            plugin_list = []

        has_clinical = any(
            str(item).strip() in PLUGIN_ALIASES for item in plugin_list if item
        )
        if not has_clinical:
            plugin_list.append(PLUGIN_ID)
            data["PLUGINS"] = plugin_list
            changed = True

        for key, legacy_val in legacy_values.items():
            if key not in plugin_settings:
                plugin_settings[key] = legacy_val
                plugin_settings_modified = True

    if plugin_settings_modified:
        data["PLUGIN_SETTINGS"] = plugin_settings
        changed = True

    return changed


def _configure_clinical_strategy(
    strategy: Any,
    settings: Any,
    base_dir: str | None = None,
) -> None:
    """Apply clinical plugin settings and workspace base directory to a strategy instance."""
    if strategy is None:
        return
    smart_renaming = bool(
        read_clinical_setting(settings, "CLINICAL_SMART_RENAMING", default=False)
    )
    generate_audit = bool(
        read_clinical_setting(
            settings,
            "CLINICAL_GENERATE_AUDIT_REPORT",
            default=True,
        )
    )
    strategy.smart_renaming = smart_renaming
    strategy.generate_audit_report = generate_audit
    strategy.base_dir = base_dir


def _open_cro_forensic_action(tui_app: Any) -> None:
    """Open the CRO Forensic Ingestion modal on the host Textual TUI application."""
    focus_guard = getattr(tui_app, "_is_text_control_focused", None)
    if callable(focus_guard) and focus_guard(allow_tree_search=True):
        return
    from app.plugins.clinical_compliance.tui_views import CROForensicModal

    tui_app.push_screen(
        CROForensicModal(
            getattr(tui_app, "settings", None),
            base_dir=str(getattr(tui_app, "base_dir", "") or ""),
        )
    )


def _build_catalog_cro_modal(settings: Any = None) -> Any:
    """Instantiate ``CROForensicModal`` for standalone component catalog inspection."""
    from app.config import AppSettings
    from app.plugins.clinical_compliance.tui_views import CROForensicModal

    return CROForensicModal(settings=settings or AppSettings())


def quarantine_scan_hook(
    orig_rel_path: str, extracted_text: str, base_dir: str
) -> None:
    """Quarantine inspection hook executing clinical compliance gap analysis when relevant."""
    if extracted_text and (
        "clinical" in orig_rel_path.lower() or "trial" in orig_rel_path.lower()
    ):
        try:
            from app.plugins.clinical_compliance.clinical_compliance import (
                ClinicalComplianceEngine,
            )

            engine = ClinicalComplianceEngine()
            _ = engine.evaluate_compliance(
                classified_artifacts={os.path.basename(orig_rel_path): "01.01.01"},
                all_filenames=[os.path.basename(orig_rel_path)],
                base_dir=base_dir,
            )
        except Exception as e:
            logger.warning(
                f"Clinical compliance quarantine hook evaluation warning: {e}",
                exc_info=True,
            )


def register_plugin(registry: Any) -> None:
    """Register clinical compliance plugin hooks, strategies, and UI components with PluginRegistry."""
    # 0. Register plugin-owned configuration defaults
    registry.register_config_defaults(PLUGIN_CONFIG_DEFAULTS)

    # 1. Register quarantine lifecycle scan hook
    registry.register_quarantine_scan_hook(quarantine_scan_hook)

    # 2. Register clustering strategies & strategy configurators
    try:
        from app.plugins.clinical_compliance.clinical_strategy import (
            ClinicalTMFStrategy,
        )

        registry.register_clustering_strategy(
            "clinical_tmf", ClinicalTMFStrategy(mode="tmf")
        )
        registry.register_clustering_strategy(
            "clinical_isf", ClinicalTMFStrategy(mode="isf")
        )
        for strat_name in PLUGIN_STRATEGIES:
            registry.register_strategy_configurator(
                strat_name,
                _configure_clinical_strategy,
            )
    except Exception as e:
        logger.exception(f"Failed registering clinical clustering strategies: {e}")

    # 3. Register CLI & TUI strategy options
    registry.register_cli_strategies(list(PLUGIN_STRATEGIES))
    registry.register_tui_strategy_options(
        [
            ("Clinical TMF", "clinical_tmf"),
            ("Clinical ISF", "clinical_isf"),
        ]
    )

    # 4. Register TUI modal views, actions, command palette entry, & catalog entry
    try:
        from app.plugins.clinical_compliance.tui_views import CROForensicModal

        registry.register_tui_view("cro_forensic", CROForensicModal)
        registry.register_tui_action(
            "action_open_cro_forensic", _open_cro_forensic_action
        )
        registry.register_tui_command(
            {
                "id": "open_cro_forensic",
                "title": "Open CRO Forensic Ingestion",
                "category": "Compliance",
                "shortcut": "",
                "description": "Multi-study clinical compliance ingestion workflow",
                "action_name": "action_open_cro_forensic",
            }
        )
        registry.register_tui_catalog_entry(
            {
                "id": "cro_forensic_modal",
                "name": "CRO Forensic Modal",
                "category": "modal",
                "description": "Modal dialog for running CRO multi-study forensic ingestion worker.",
                "factory": _build_catalog_cro_modal,
            }
        )
    except Exception as e:
        logger.exception(f"Failed registering clinical TUI views: {e}")

    # 5. Register TUI settings switches
    registry.register_tui_switch(
        {
            "key": "CLINICAL_SMART_RENAMING",
            "id": "switch-clinical-renaming",
            "label": " Clinical Smart Renaming",
            "tooltip": "Toggle clinical smart renaming compliance mode",
            "default": False,
        }
    )
