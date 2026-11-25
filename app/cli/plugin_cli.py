"""CLI helpers for read-only plugin discovery, dynamic strategy choices, and plugin config updates."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)


def extract_cli_settings_source_flags(
    raw_argv: list[str],
) -> tuple[str | None, str | None]:
    """Peek ``--settings-file`` and ``--profile`` values from raw argv without side effects."""
    settings_file: str | None = None
    profile: str | None = None
    idx = 0
    total = len(raw_argv)
    while idx < total:
        token = raw_argv[idx]
        if token == "--":
            break
        if token == "--settings-file" and idx + 1 < total:
            settings_file = raw_argv[idx + 1]
            idx += 2
            continue
        if token.startswith("--settings-file="):
            settings_file = token.split("=", 1)[1]
        elif token == "--profile" and idx + 1 < total:
            profile = raw_argv[idx + 1]
            idx += 2
            continue
        elif token.startswith("--profile="):
            profile = token.split("=", 1)[1]
        idx += 1
    return settings_file, profile


def load_cli_plugins_readonly(
    raw_argv: list[str] | None = None,
    *,
    reset_if_source_explicit: bool = False,
) -> None:
    """Inspect configured ``PLUGINS`` read-only so CLI strategy choices reflect active plugins."""
    from app.config import get_app_dir, resolve_cli_settings_filepath
    from app.core.plugin_registry import PluginRegistry

    raw_sf: str | None = None
    raw_prof: str | None = None
    if raw_argv is not None:
        raw_sf, raw_prof = extract_cli_settings_source_flags(raw_argv)

    has_explicit_source = raw_sf is not None or raw_prof is not None
    if reset_if_source_explicit and has_explicit_source:
        PluginRegistry.reset_instance()

    target_filepath: str | None = None
    if has_explicit_source:
        try:
            target_filepath = resolve_cli_settings_filepath(
                settings_file=raw_sf,
                profile=raw_prof,
            )
        except ValueError:
            return
    else:
        target_filepath = str(get_app_dir() / "settings.json")

    configured_plugins: list[str] = []
    env_plugins = os.environ.get("SORTIFY_PLUGINS", "").strip()
    if env_plugins:
        if env_plugins.startswith("["):
            try:
                parsed_env = json.loads(env_plugins)
                if isinstance(parsed_env, list):
                    configured_plugins.extend(str(p) for p in parsed_env if p)
            except ValueError as exc:
                logger.debug("Ignoring malformed SORTIFY_PLUGINS JSON value: %s", exc)
        else:
            configured_plugins.extend(
                item.strip() for item in env_plugins.split(",") if item.strip()
            )

    if target_filepath and os.path.isfile(target_filepath):
        try:
            with open(target_filepath, encoding="utf-8") as fh:
                raw_data = json.load(fh)
            if isinstance(raw_data, dict):
                PluginRegistry.migrate_legacy_settings_data(raw_data)
                file_plugins = raw_data.get("PLUGINS")
                if isinstance(file_plugins, (list, tuple, set)):
                    configured_plugins = [str(p) for p in file_plugins if p]
        except (OSError, ValueError, TypeError) as exc:
            logger.debug("Skipping CLI read-only plugin inspection: %s", exc)

    PluginRegistry.get_instance().load_plugins_from_settings(
        SimpleNamespace(PLUGINS=configured_plugins)
    )


def get_available_cli_strategies(settings: Any = None) -> list[str]:
    """Return core CLI sorting strategies plus strategies registered by active plugins."""
    from app.core.plugin_registry import PluginRegistry

    reg = PluginRegistry.get_instance()
    if settings is not None:
        reg.load_plugins_from_settings(settings)
    else:
        load_cli_plugins_readonly()

    choices = ["default", "generative"]
    for strat_name in reg.get_cli_strategies():
        if strat_name not in choices:
            choices.append(strat_name)
    return choices


def apply_plugin_config_cli_set(
    settings: Any,
    key_upper: str,
    raw_value: str,
    parse_value_fn: Callable[[str, str, Any], Any],
) -> bool:
    """Apply ``config --set`` for a plugin-owned key when active; return False if unknown."""
    from app.core.plugin_registry import PluginRegistry

    reg = PluginRegistry.get_instance()
    reg.load_plugins_from_settings(settings)
    plugin_defaults = reg.get_config_defaults()
    persisted_map = getattr(
        getattr(settings, "_settings_model", None), "PLUGIN_SETTINGS", {}
    )
    if key_upper not in persisted_map and key_upper not in plugin_defaults:
        return False

    curr_val = settings.get_plugin_setting(key_upper, plugin_defaults.get(key_upper))
    new_val = parse_value_fn(key_upper, raw_value, curr_val)
    settings.set_plugin_setting(key_upper, new_val)
    settings.flush()
    return True
