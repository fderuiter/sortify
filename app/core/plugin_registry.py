"""Dynamic plugin registry managing domain extension package loading and lifecycle hooks."""

import importlib
import logging
import os
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


ALLOWED_PLUGIN_PREFIXES: tuple[str, ...] = ("app.plugins.", "sortify_plugin_")


def _iter_bundled_plugin_module_names() -> list[str]:
    """Return sorted module paths for bundled plugins under ``app/plugins``."""
    plugins_dir = Path(__file__).resolve().parent.parent / "plugins"
    if not plugins_dir.is_dir():
        return []
    discovered: list[str] = []
    try:
        entries = sorted(os.scandir(plugins_dir), key=lambda item: item.name)
    except OSError:
        return []
    for entry in entries:
        if not entry.is_dir() or entry.name.startswith("_"):
            continue
        plugin_file = Path(entry.path) / "plugin.py"
        if plugin_file.is_file():
            discovered.append(f"app.plugins.{entry.name}.plugin")
    return discovered


def _get_plugin_candidates(plugin_name_or_module: str) -> list[str]:
    """Resolve candidate module import paths for a plugin in order of priority.

    Evaluates scoped namespaces (app.plugins.<name>) before raw paths, and enforces
    prefix guards to prevent loading unauthorized or standard library modules.
    """
    raw_name = plugin_name_or_module.strip()
    if not raw_name:
        return []

    raw_candidates: list[str] = []

    if raw_name.startswith("app.plugins."):
        raw_candidates.append(raw_name)
        if not raw_name.endswith(".plugin"):
            raw_candidates.append(f"{raw_name}.plugin")
    elif raw_name.startswith("sortify_plugin_"):
        raw_candidates.append(f"app.plugins.{raw_name}")
        if not raw_name.endswith(".plugin"):
            raw_candidates.append(f"app.plugins.{raw_name}.plugin")
        raw_candidates.append(raw_name)
        if not raw_name.endswith(".plugin"):
            raw_candidates.append(f"{raw_name}.plugin")
    else:
        raw_candidates.append(f"app.plugins.{raw_name}")
        if not raw_name.endswith(".plugin"):
            raw_candidates.append(f"app.plugins.{raw_name}.plugin")

    valid_candidates: list[str] = []
    for cand in raw_candidates:
        if cand.startswith(ALLOWED_PLUGIN_PREFIXES) and cand not in valid_candidates:
            valid_candidates.append(cand)

    return valid_candidates


def _canonical_plugin_key(plugin_name_or_module: str) -> str:
    """Normalize a plugin identifier to its canonical import candidate for set comparisons."""
    candidates = _get_plugin_candidates(plugin_name_or_module)
    if candidates:
        return candidates[-1]
    return plugin_name_or_module.strip()


def _unregister_clustering_strategies(strategy_names: Iterable[str]) -> None:
    """Remove plugin-supplied strategy names from ``clustering_registry``."""
    names = list(strategy_names)
    if not names:
        return
    try:
        from app.core.analyzer_strategies import clustering_registry

        for strat_name in names:
            clustering_registry.unregister(strat_name)
    except (ImportError, AttributeError) as exc:
        logger.debug("Could not unregister clustering strategies: %s", exc)


class PluginRegistry:
    """Registry for discovering, registering, and triggering dynamic extension plugin hooks."""

    _instance: "PluginRegistry | None" = None
    _lock = threading.Lock()

    def __init__(self, *, isolated: bool = False) -> None:
        self._isolated = bool(isolated)
        self._quarantine_scan_hooks: list[Callable] = []
        self._clustering_strategies: dict[str, Any] = {}
        self._strategy_configurators: dict[str, Callable[..., None]] = {}
        self._config_defaults: dict[str, Any] = {}
        self._cli_strategies: list[str] = []
        self._tui_strategy_options: list[tuple[str, str]] = []
        self._tui_views: dict[str, Any] = {}
        self._tui_switches: list[dict[str, Any]] = []
        self._tui_commands: list[dict[str, Any]] = []
        self._tui_actions: dict[str, Callable[[Any], None]] = {}
        self._tui_catalog_entries: list[dict[str, Any]] = []
        self._loaded_plugins: set[str] = set()
        self._settings_plugins: set[str] = set()

    def _clear_registered_state(self) -> None:
        """Purge registered plugin hooks and strategies when active settings deactivate plugins."""
        if not self._isolated and self._clustering_strategies:
            _unregister_clustering_strategies(self._clustering_strategies.keys())
        self._quarantine_scan_hooks.clear()
        self._clustering_strategies.clear()
        self._strategy_configurators.clear()
        self._config_defaults.clear()
        self._cli_strategies.clear()
        self._tui_strategy_options.clear()
        self._tui_views.clear()
        self._tui_switches.clear()
        self._tui_commands.clear()
        self._tui_actions.clear()
        self._tui_catalog_entries.clear()
        self._loaded_plugins.clear()
        self._settings_plugins.clear()

    @classmethod
    def get_instance(cls) -> "PluginRegistry":
        """Retrieve thread-safe singleton instance of PluginRegistry."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton instance and purge plugin-owned clustering strategies."""
        with cls._lock:
            previous = cls._instance
            cls._instance = cls()
        if previous is not None and previous._clustering_strategies:
            _unregister_clustering_strategies(previous._clustering_strategies.keys())

    # --- Lifecycle Hook Registration ---

    def register_quarantine_scan_hook(self, hook_fn: Callable) -> None:
        """Register a callback hook for quarantine scan lifecycle event."""
        if callable(hook_fn) and hook_fn not in self._quarantine_scan_hooks:
            self._quarantine_scan_hooks.append(hook_fn)

    def trigger_quarantine_scan(
        self, orig_rel_path: str, extracted_text: str, base_dir: str
    ) -> None:
        """Execute all registered quarantine scan hooks with error isolation."""
        for hook in list(self._quarantine_scan_hooks):
            try:
                hook(orig_rel_path, extracted_text, base_dir)
            except Exception as e:
                logger.warning(
                    f"Plugin quarantine scan hook '{getattr(hook, '__name__', str(hook))}' failed: {e}",
                    exc_info=True,
                )

    def register_config_defaults(self, defaults: dict[str, Any]) -> None:
        """Register default configuration key-value pairs owned by a plugin."""
        if isinstance(defaults, dict):
            self._config_defaults.update(defaults)

    def get_config_defaults(self) -> dict[str, Any]:
        """Return merged configuration defaults declared by all active plugins."""
        return dict(self._config_defaults)

    def register_clustering_strategy(self, name: str, strategy: Any) -> None:
        """Register a clustering strategy supplied by a plugin."""
        self._clustering_strategies[name] = strategy
        if self._isolated:
            return
        try:
            from app.core.analyzer_strategies import clustering_registry

            clustering_registry.register(name, strategy)
        except Exception as e:
            logger.debug(
                f"Note: Could not automatically register strategy into ClusteringRegistry: {e}",
                exc_info=True,
            )

    def get_clustering_strategy(self, name: str) -> Any | None:
        """Retrieve a plugin-supplied clustering strategy by name if its plugin is active."""
        return self._clustering_strategies.get(name)

    def register_strategy_configurator(
        self,
        strategy_name: str,
        configurator_fn: Callable[..., None],
    ) -> None:
        """Register a callback that applies runtime settings to a plugin strategy."""
        if strategy_name and callable(configurator_fn):
            self._strategy_configurators[strategy_name] = configurator_fn

    def configure_strategy(
        self,
        strategy_name: str,
        strategy: Any,
        settings: Any,
        base_dir: str | None = None,
    ) -> None:
        """Invoke a registered plugin configurator for ``strategy_name`` if present."""
        configurator = self._strategy_configurators.get(strategy_name)
        if configurator is not None:
            configurator(strategy, settings, base_dir=base_dir)

    def register_cli_strategies(self, strategy_names: list[str]) -> None:
        """Register CLI ``--strategy`` option values exposed by an active plugin."""
        for name in strategy_names:
            cleaned = str(name).strip()
            if cleaned and cleaned not in self._cli_strategies:
                self._cli_strategies.append(cleaned)

    def get_cli_strategies(self) -> list[str]:
        """Return CLI ``--strategy`` option values registered by active plugins."""
        return list(self._cli_strategies)

    def register_tui_strategy_options(self, options: list[tuple[str, str]]) -> None:
        """Register dropdown strategy options for TUI Settings modal."""
        for opt in options:
            if opt not in self._tui_strategy_options:
                self._tui_strategy_options.append(opt)

    def get_tui_strategy_options(self) -> list[tuple[str, str]]:
        """Retrieve all plugin-registered TUI strategy options."""
        return list(self._tui_strategy_options)

    def register_tui_view(self, view_name: str, view_component: Any) -> None:
        """Register a TUI modal or screen component class."""
        self._tui_views[view_name] = view_component

    def get_tui_view(self, view_name: str) -> Any | None:
        """Retrieve registered TUI view component by name."""
        return self._tui_views.get(view_name)

    def register_tui_switch(self, switch_config: dict[str, Any]) -> None:
        """Register a settings switch configuration for TUI Settings modal."""
        if switch_config not in self._tui_switches:
            self._tui_switches.append(switch_config)

    def get_tui_switches(self) -> list[dict[str, Any]]:
        """Retrieve all plugin-registered TUI setting switches."""
        return list(self._tui_switches)

    def register_tui_command(self, command_config: dict[str, Any]) -> None:
        """Register a Command Palette command descriptor owned by an active plugin."""
        cmd_id = str(command_config.get("id", "")).strip()
        if not cmd_id:
            return
        if not any(existing.get("id") == cmd_id for existing in self._tui_commands):
            self._tui_commands.append(dict(command_config))

    def get_tui_commands(self) -> list[dict[str, Any]]:
        """Return all Command Palette command descriptors registered by active plugins."""
        return [dict(cmd) for cmd in self._tui_commands]

    def register_tui_action(
        self,
        action_name: str,
        handler_fn: Callable[[Any], None],
    ) -> None:
        """Register a dynamic TUI action handler callable with the host TUI app."""
        if action_name and callable(handler_fn):
            self._tui_actions[action_name] = handler_fn

    def get_tui_action(self, action_name: str) -> Callable[[Any], None] | None:
        """Retrieve a dynamic TUI action handler registered by an active plugin."""
        return self._tui_actions.get(action_name)

    def register_tui_catalog_entry(self, entry: dict[str, Any]) -> None:
        """Register a component catalog entry for standalone UI accessibility/geometry checks."""
        entry_id = str(entry.get("id", "")).strip()
        if not entry_id:
            return
        if not any(
            existing.get("id") == entry_id for existing in self._tui_catalog_entries
        ):
            self._tui_catalog_entries.append(dict(entry))

    def get_tui_catalog_entries(self) -> list[dict[str, Any]]:
        """Return component catalog entries registered in this registry instance."""
        return [dict(entry) for entry in self._tui_catalog_entries]

    @classmethod
    def discover_catalog_entries_isolated(cls) -> list[dict[str, Any]]:
        """Discover bundled plugin catalog entries in an isolated registry without activating plugins globally."""
        temp_registry = cls(isolated=True)
        for mod_name in _iter_bundled_plugin_module_names():
            temp_registry.load_plugin(mod_name)
        return temp_registry.get_tui_catalog_entries()

    @classmethod
    def migrate_legacy_settings_data(cls, data: dict[str, Any]) -> bool:
        """Run idempotent legacy configuration migrations declared by bundled plugins."""
        if not isinstance(data, dict):
            return False
        changed = False
        for mod_name in _iter_bundled_plugin_module_names():
            try:
                mod = importlib.import_module(mod_name)
            except (ImportError, OSError, SyntaxError, ValueError) as exc:
                logger.warning(
                    "Skipping legacy settings migration check for '%s': %s",
                    mod_name,
                    exc,
                )
                continue
            migrate_fn = getattr(mod, "migrate_legacy_settings", None)
            if callable(migrate_fn):
                try:
                    if bool(migrate_fn(data)):
                        changed = True
                except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                    logger.warning(
                        "Plugin settings migration '%s' failed: %s",
                        mod_name,
                        exc,
                    )
        return changed

    # --- Plugin Discovery & Loading ---

    def load_plugin(self, plugin_name_or_module: str) -> bool:
        """Load a single plugin by module path or name.

        Fails gracefully with descriptive logs if plugin module is missing or fails initialization.
        """
        if not plugin_name_or_module or not isinstance(plugin_name_or_module, str):
            logger.warning("Invalid or empty plugin identifier supplied.")
            return False

        if plugin_name_or_module in self._loaded_plugins:
            return True

        module_candidates = _get_plugin_candidates(plugin_name_or_module)
        if not module_candidates:
            logger.warning(
                f"Could not load plugin '{plugin_name_or_module}': forbidden or invalid plugin module path."
            )
            return False

        mod = None
        loaded_mod_name = None
        for cand in module_candidates:
            try:
                mod = importlib.import_module(cand)
                loaded_mod_name = cand
                break
            except ModuleNotFoundError:
                continue
            except Exception as e:
                logger.error(
                    f"Failed loading plugin candidate module '{cand}': {e}",
                    exc_info=True,
                )
                return False

        if mod is None:
            logger.warning(
                f"Could not load plugin '{plugin_name_or_module}': plugin files not found."
            )
            return False

        register_fn = getattr(mod, "register_plugin", None)
        if callable(register_fn):
            try:
                register_fn(self)
                self._loaded_plugins.add(plugin_name_or_module)
                if loaded_mod_name:
                    self._loaded_plugins.add(loaded_mod_name)
                if not self._isolated:
                    self._settings_plugins.add(
                        _canonical_plugin_key(plugin_name_or_module)
                    )
                logger.info(
                    f"Successfully registered plugin '{plugin_name_or_module}'."
                )
                return True
            except Exception as e:
                logger.error(
                    f"Error executing register_plugin() for plugin '{plugin_name_or_module}': {e}",
                    exc_info=True,
                )
                return False

        logger.warning(
            f"Plugin module '{loaded_mod_name}' does not export 'register_plugin(registry)' function."
        )
        return False

    def load_plugins_from_settings(self, settings: Any = None) -> None:
        """Activate only plugins explicitly listed in ``settings.PLUGINS``."""
        if settings is None:
            return
        raw_plugins = getattr(settings, "PLUGINS", None) or []
        desired_entries = (
            [str(p).strip() for p in raw_plugins if p and str(p).strip()]
            if isinstance(raw_plugins, (list, tuple, set))
            else []
        )
        desired_keys = {_canonical_plugin_key(p) for p in desired_entries}
        if self._settings_plugins and not self._settings_plugins.issubset(desired_keys):
            self._clear_registered_state()
        for plugin_entry in desired_entries:
            if self.load_plugin(plugin_entry):
                self._settings_plugins.add(_canonical_plugin_key(plugin_entry))
