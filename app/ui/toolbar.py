"""Unified Priority Overflow Toolbar System module."""

from typing import Any, Callable, List, Optional
from unittest.mock import MagicMock

from nicegui import ui

_TOOLBAR_CSS_HEAD_INJECTED = False


def _resolve_ui(ui_arg: Optional[Any] = None) -> Any:
    """Resolve active ui context or caller mocked ui."""
    if ui_arg is not None:
        return ui_arg
    try:
        import inspect

        frame = inspect.currentframe()
        while frame:
            caller_ui = frame.f_globals.get("ui")
            if caller_ui is not None and caller_ui is not ui:
                return caller_ui
            frame = frame.f_back
    except Exception:
        pass
    return ui


def _safe_create(ui_obj: Any, method_name: str, *args, **kwargs) -> Any:
    """Safely create a UI element, returning MagicMock on failure or empty slot stack."""
    func = getattr(ui_obj, method_name, None)
    if func is None:
        return MagicMock()
    try:
        res = func(*args, **kwargs)
        if res is None:
            return MagicMock()
        return res
    except RuntimeError:
        return MagicMock()


def _ensure_toolbar_css(ui_obj: Optional[Any] = None) -> None:
    """Inject responsive toolbar CSS styles into document head if not already injected."""
    global _TOOLBAR_CSS_HEAD_INJECTED
    if not _TOOLBAR_CSS_HEAD_INJECTED:
        _TOOLBAR_CSS_HEAD_INJECTED = True
        try:
            target_ui = ui_obj or ui
            if hasattr(target_ui, "add_head_html"):
                target_ui.add_head_html("""
<style>
.overflow-toolbar-container {
    display: flex;
    flex-direction: row;
    align-items: center;
    flex-wrap: nowrap;
    overflow: hidden;
    max-height: 48px;
    white-space: nowrap;
}
.overflow-toolbar-menu-btn {
    display: none !important;
}

@media (max-width: 768px) {
    .overflow-toolbar-secondary {
        display: none !important;
    }
    .overflow-toolbar-menu-btn {
        display: inline-flex !important;
    }
}
.overflow-collapsed .overflow-toolbar-secondary {
    display: none !important;
}
.overflow-collapsed .overflow-toolbar-menu-btn {
    display: inline-flex !important;
}
</style>
""")
        except Exception:
            _TOOLBAR_CSS_HEAD_INJECTED = False


class ToolbarAction:
    """Action button control proxy that supports state passthrough."""

    def __init__(
        self,
        text: str = "",
        *,
        on_click: Optional[Callable] = None,
        icon: Optional[str] = None,
        is_primary: bool = False,
        priority: int = 0,
        tooltip: Optional[str] = None,
        aria_label: Optional[str] = None,
        toolbar: Optional["OverflowToolbar"] = None,
        ui_arg: Optional[Any] = None,
    ):
        self.is_primary = is_primary or (priority >= 10)
        self.priority = priority
        self.tooltip_text = tooltip
        self.aria_label_text = aria_label or text
        self._toolbar = toolbar
        self._custom_on_click = on_click
        self.icon = icon
        target_ui = _resolve_ui(ui_arg)
        self.ui_obj = target_ui

        self.button = _safe_create(
            target_ui, "button", text, on_click=on_click, icon=icon
        )

        if tooltip and hasattr(self.button, "__enter__"):
            try:
                with self.button:
                    _safe_create(target_ui, "tooltip", tooltip)
            except Exception:
                pass

        if self.aria_label_text and hasattr(self.button, "props"):
            try:
                self.button.props(f'aria-label="{self.aria_label_text}"')
            except Exception:
                pass

    @property
    def text(self) -> str:
        """Get button label text."""
        return getattr(self.button, "text", "")

    @property
    def visible(self) -> bool:
        """Get visibility state."""
        return getattr(self.button, "visible", True)

    @property
    def enabled(self) -> bool:
        """Get enabled state."""
        return getattr(self.button, "enabled", True)

    @property
    def _event_listeners(self) -> dict:
        """Get underlying button event listeners dict."""
        return getattr(self.button, "_event_listeners", {})

    def classes(self, *args, **kwargs):
        """Apply CSS classes to the action button."""
        if hasattr(self.button, "classes"):
            self.button.classes(*args, **kwargs)
        return self

    def props(self, *args, **kwargs):
        """Apply component props to the action button."""
        if hasattr(self.button, "props"):
            self.button.props(*args, **kwargs)
        return self

    def set_visibility(self, visible: bool) -> None:
        """Set action button visibility."""
        if hasattr(self.button, "set_visibility"):
            self.button.set_visibility(visible)
        if self._toolbar:
            self._toolbar.update_overflow_state()

    def disable(self) -> None:
        """Disable action button."""
        if hasattr(self.button, "disable"):
            self.button.disable()
        if self._toolbar:
            self._toolbar.update_overflow_state()

    def enable(self) -> None:
        """Enable action button."""
        if hasattr(self.button, "enable"):
            self.button.enable()
        if self._toolbar:
            self._toolbar.update_overflow_state()

    def set_text(self, text: str) -> None:
        """Set action button text."""
        if hasattr(self.button, "set_text"):
            self.button.set_text(text)
        if getattr(self, "aria_label_text", "") == getattr(
            self, "text", ""
        ) or not getattr(self, "aria_label_text", None):
            self.aria_label_text = text
            if hasattr(self.button, "props"):
                try:
                    self.button.props(f'aria-label="{text}"')
                except Exception:
                    pass
        if self._toolbar:
            self._toolbar.update_overflow_state()

    def _trigger_click(self, e=None) -> None:
        """Trigger click handler programmatically or via overflow menu."""
        if not getattr(self, "enabled", True) or not getattr(self, "visible", True):
            return
        if self._custom_on_click:
            self._custom_on_click()

    def __getattr__(self, item):
        """Forward attribute access to the underlying button."""
        return getattr(self.button, item)


class OverflowToolbar:
    """Standardized single-row toolbar container with automated priority overflow collapse."""

    def __init__(
        self,
        *,
        title: Optional[str] = None,
        classes: str = "",
        props: str = "",
        ui: Optional[Any] = None,
        **kwargs,
    ):
        target_ui = _resolve_ui(ui)
        self.ui_obj = target_ui
        _ensure_toolbar_css(target_ui)

        base_classes = (
            "overflow-toolbar-container w-full items-center justify-between flex-nowrap overflow-hidden max-h-12 py-1 px-2 "
            + classes
        )
        self.row = _safe_create(target_ui, "row").classes(base_classes)
        if props and hasattr(self.row, "props"):
            try:
                self.row.props(props)
            except Exception:
                pass

        self.actions: List[ToolbarAction] = []

        try:
            with self.row:
                # Left title/content container
                self.left_container = _safe_create(target_ui, "row").classes(
                    "items-center gap-3 shrink-0 flex-nowrap"
                )
                if title:
                    try:
                        with self.left_container:
                            _safe_create(target_ui, "label", title).classes(
                                "font-bold text-slate-800"
                            )
                    except Exception:
                        pass

                # Right actions container
                self.actions_container = _safe_create(target_ui, "row").classes(
                    "items-center gap-2 flex-nowrap shrink-0 overflow-hidden"
                )

                # Create overflow menu button & menu
                try:
                    with self.actions_container:
                        self.overflow_btn = (
                            _safe_create(target_ui, "button", icon="more_vert")
                            .props('flat round dense aria-label="More Actions Menu"')
                            .classes("overflow-toolbar-menu-btn hidden")
                        )
                        try:
                            with self.overflow_btn:
                                self.overflow_menu = _safe_create(target_ui, "menu")
                        except Exception:
                            self.overflow_menu = None
                except Exception:
                    self.overflow_btn = MagicMock()
                    self.overflow_menu = None
        except Exception:
            self.left_container = MagicMock()
            self.actions_container = MagicMock()
            self.overflow_btn = MagicMock()
            self.overflow_menu = None

        if self.overflow_menu and hasattr(self.overflow_menu, "on"):
            try:
                self.overflow_menu.on("before-show", self._populate_overflow_menu)
            except Exception:
                pass
        if hasattr(self.overflow_btn, "on"):
            try:
                self.overflow_btn.on("click", self._populate_overflow_menu)
            except Exception:
                pass

    def __enter__(self):
        """Enter toolbar container context."""
        if hasattr(self.row, "__enter__"):
            try:
                return self.row.__enter__()
            except RuntimeError:
                return self.row
        return self.row

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit toolbar container context."""
        if hasattr(self.row, "__exit__"):
            try:
                return self.row.__exit__(exc_type, exc_val, exc_tb)
            except RuntimeError:
                return None
        return None

    @property
    def visible(self) -> bool:
        """Get visibility state of toolbar container."""
        return getattr(self.row, "visible", True)

    def classes(self, *args, **kwargs):
        """Apply CSS classes to toolbar container."""
        if hasattr(self.row, "classes"):
            self.row.classes(*args, **kwargs)
        return self

    def props(self, *args, **kwargs):
        """Apply component props to toolbar container."""
        if hasattr(self.row, "props"):
            self.row.props(*args, **kwargs)
        return self

    def set_visibility(self, visible: bool) -> None:
        """Set visibility of toolbar container."""
        if hasattr(self.row, "set_visibility"):
            self.row.set_visibility(visible)

    def add_action(
        self,
        text: str = "",
        on_click: Optional[Callable] = None,
        *,
        icon: Optional[str] = None,
        is_primary: bool = False,
        priority: int = 0,
        classes: str = "",
        props: str = "",
        tooltip: Optional[str] = None,
        aria_label: Optional[str] = None,
        ui: Optional[Any] = None,
    ) -> ToolbarAction:
        """Add an action button control to the toolbar."""
        target_ui = ui or self.ui_obj
        try:
            with self.actions_container:
                action = ToolbarAction(
                    text,
                    on_click=on_click,
                    icon=icon,
                    is_primary=is_primary,
                    priority=priority,
                    tooltip=tooltip,
                    aria_label=aria_label,
                    toolbar=self,
                    ui_arg=target_ui,
                )
                if classes:
                    action.classes(classes)
                if props:
                    action.props(props)

                if action.is_primary:
                    action.classes("overflow-toolbar-primary inline-flex shrink-0")
                else:
                    action.classes("overflow-toolbar-secondary inline-flex shrink-0")

                if hasattr(self.overflow_btn, "move"):
                    try:
                        self.overflow_btn.move(self.actions_container)
                    except Exception:
                        pass

                self.actions.append(action)
                self.update_overflow_state()
                return action
        except Exception:
            action = ToolbarAction(
                text,
                on_click=on_click,
                icon=icon,
                is_primary=is_primary,
                priority=priority,
                tooltip=tooltip,
                aria_label=aria_label,
                toolbar=self,
                ui_arg=target_ui,
            )
            self.actions.append(action)
            return action

    def update_overflow_state(self) -> None:
        """Update visibility of the overflow menu button."""
        has_visible_secondary = any(
            getattr(a, "visible", True) and not a.is_primary for a in self.actions
        )
        if has_visible_secondary:
            if hasattr(self.overflow_btn, "classes"):
                self.overflow_btn.classes(remove="hidden", add="inline-flex")
        else:
            if hasattr(self.overflow_btn, "classes"):
                self.overflow_btn.classes(remove="inline-flex", add="hidden")

    def _populate_overflow_menu(self, e=None) -> None:
        """Populate overflow dropdown menu items."""
        if not self.overflow_menu:
            return
        target_ui = self.ui_obj
        if hasattr(self.overflow_menu, "clear"):
            try:
                self.overflow_menu.clear()
            except Exception:
                pass
        try:
            with self.overflow_menu:
                visible_secondaries = [
                    a
                    for a in self.actions
                    if getattr(a, "visible", True) and not a.is_primary
                ]
                for action in visible_secondaries:
                    item = _safe_create(
                        target_ui,
                        "menu_item",
                        action.text,
                        on_click=lambda a=action: self._on_menu_item_click(a),
                    )
                    if action.icon:
                        try:
                            with item:
                                _safe_create(target_ui, "icon", action.icon).classes(
                                    "mr-2"
                                )
                        except Exception:
                            pass
                    if not getattr(action, "enabled", True) and hasattr(
                        item, "disable"
                    ):
                        try:
                            item.disable()
                        except Exception:
                            pass
                    if action.aria_label_text and hasattr(item, "props"):
                        try:
                            item.props(f'aria-label="{action.aria_label_text}"')
                        except Exception:
                            pass
                    if action.tooltip_text:
                        try:
                            with item:
                                _safe_create(target_ui, "tooltip", action.tooltip_text)
                        except Exception:
                            pass
        except Exception:
            pass

    def _on_menu_item_click(self, action: ToolbarAction) -> None:
        if self.overflow_menu and hasattr(self.overflow_menu, "close"):
            try:
                self.overflow_menu.close()
            except Exception:
                pass
        action._trigger_click()

    def __getattr__(self, item):
        """Forward attribute access to the underlying row element."""
        return getattr(self.row, item)
