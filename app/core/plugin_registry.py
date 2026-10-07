"""Dynamic plugin registry managing domain extension package loading and lifecycle hooks."""

import importlib
import logging
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class PluginRegistry:
    """Registry for discovering, registering, and triggering dynamic extension plugin hooks."""

    _instance: Optional["PluginRegistry"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._quarantine_scan_hooks: List[Callable] = []
        self._clustering_strategies: Dict[str, Any] = {}
        self._tui_strategy_options: List[Tuple[str, str]] = []
        self._tui_views: Dict[str, Any] = {}
        self._tui_switches: List[Dict[str, Any]] = []
        self._loaded_plugins: set[str] = set()

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
        """Reset singleton instance (primarily for testing and context isolation)."""
        with cls._lock:
            cls._instance = cls()

    # --- Lifecycle Hook Registration ---

    def register_quarantine_scan_hook(self, hook_fn: Callable) -> None:
        """Register a callback hook for quarantine scan lifecycle event."""
        if callable(hook_fn) and hook_fn not in self._quarantine_scan_hooks:
            self._quarantine_scan_hooks.append(hook_fn)

    def get_quarantine_scan_hooks(self) -> List[Callable]:
        """Retrieve registered quarantine scan hooks."""
        return list(self._quarantine_scan_hooks)

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

    def register_clustering_strategy(self, name: str, strategy: Any) -> None:
        """Register a clustering strategy supplied by a plugin."""
        self._clustering_strategies[name] = strategy
        try:
            from app.core.analyzer_strategies import clustering_registry

            clustering_registry.register(name, strategy)
        except Exception as e:
            logger.debug(
                f"Note: Could not automatically register strategy into ClusteringRegistry: {e}"
            )

    def get_clustering_strategy(self, name: str) -> Optional[Any]:
        """Retrieve a plugin-supplied clustering strategy by name."""
        if name not in self._clustering_strategies and name.startswith("clinical"):
            self.load_plugin("clinical_compliance")
        return self._clustering_strategies.get(name)

    def register_tui_strategy_options(self, options: List[Tuple[str, str]]) -> None:
        """Register dropdown strategy options for TUI Settings modal."""
        for opt in options:
            if opt not in self._tui_strategy_options:
                self._tui_strategy_options.append(opt)

    def get_tui_strategy_options(self) -> List[Tuple[str, str]]:
        """Retrieve all plugin-registered TUI strategy options."""
        return list(self._tui_strategy_options)

    def register_tui_view(self, view_name: str, view_component: Any) -> None:
        """Register a TUI modal or screen component class."""
        self._tui_views[view_name] = view_component

    def get_tui_view(self, view_name: str) -> Optional[Any]:
        """Retrieve registered TUI view component by name."""
        return self._tui_views.get(view_name)

    def has_tui_view(self, view_name: str) -> bool:
        """Check if TUI view component is registered."""
        return view_name in self._tui_views

    def register_tui_switch(self, switch_config: Dict[str, Any]) -> None:
        """Register a settings switch configuration for TUI Settings modal."""
        if switch_config not in self._tui_switches:
            self._tui_switches.append(switch_config)

    def get_tui_switches(self) -> List[Dict[str, Any]]:
        """Retrieve all plugin-registered TUI setting switches."""
        return list(self._tui_switches)

    # --- Plugin Discovery & Loading ---

    def load_plugin(self, plugin_name_or_module: str) -> bool:
        """Load a single plugin by module path or name.

        Fails gracefully with descriptive logs if plugin module is missing or fails initialization.
        """
        if plugin_name_or_module in self._loaded_plugins:
            return True

        module_candidates = [
            plugin_name_or_module,
            f"app.plugins.{plugin_name_or_module}",
            f"app.plugins.{plugin_name_or_module}.plugin",
        ]

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
        """Discover and load plugins according to settings or active strategy configuration."""
        if settings is not None:
            plugin_list = getattr(settings, "PLUGINS", None) or []
            if isinstance(plugin_list, (list, tuple, set)):
                for p in plugin_list:
                    if p:
                        self.load_plugin(str(p))

            strat = getattr(settings, "SORTING_STRATEGY", "") or ""
            clin_renaming = bool(getattr(settings, "CLINICAL_SMART_RENAMING", False))
            if strat.startswith("clinical") or clin_renaming:
                self.load_plugin("clinical_compliance")

        # Ensure default clinical_compliance plugin is loaded if present
        self.load_plugin("clinical_compliance")
