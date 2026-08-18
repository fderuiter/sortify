"""Unified Priority Overflow Toolbar System module."""

from typing import Callable, List, Optional
from nicegui import ui

_TOOLBAR_CSS_HEAD_INJECTED = False


def _ensure_toolbar_css() -> None:
    """Inject responsive toolbar CSS styles into document head if not already injected."""
    global _TOOLBAR_CSS_HEAD_INJECTED
    if not _TOOLBAR_CSS_HEAD_INJECTED:
        _TOOLBAR_CSS_HEAD_INJECTED = True
        try:
            ui.add_head_html("""
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
    ):
        self.is_primary = is_primary or (priority >= 10)
        self.priority = priority
        self.tooltip_text = tooltip
        self.aria_label_text = aria_label or text
        self._toolbar = toolbar
        self._custom_on_click = on_click
        self.icon = icon

        self.button = ui.button(text, on_click=on_click, icon=icon)

        if tooltip and hasattr(self.button, "__enter__"):
            try:
                with self.button:
                    ui.tooltip(tooltip)
            except Exception:
                pass

        if self.aria_label_text and hasattr(self.button, "props"):
            try:
                self.button.props(f'aria-label="{self.aria_label_text}"')
            except Exception:
                pass

    @property
    def text(self) -> str:
        return getattr(self.button, "text", "")

    @property
    def visible(self) -> bool:
        return getattr(self.button, "visible", True)

    @property
    def enabled(self) -> bool:
        return getattr(self.button, "enabled", True)

    @property
    def _event_listeners(self) -> dict:
        return getattr(self.button, "_event_listeners", {})

    def classes(self, *args, **kwargs):
        if hasattr(self.button, "classes"):
            self.button.classes(*args, **kwargs)
        return self

    def props(self, *args, **kwargs):
        if hasattr(self.button, "props"):
            self.button.props(*args, **kwargs)
        return self

    def set_visibility(self, visible: bool) -> None:
        if hasattr(self.button, "set_visibility"):
            self.button.set_visibility(visible)
        if self._toolbar:
            self._toolbar.update_overflow_state()

    def disable(self) -> None:
        if hasattr(self.button, "disable"):
            self.button.disable()
        if self._toolbar:
            self._toolbar.update_overflow_state()

    def enable(self) -> None:
        if hasattr(self.button, "enable"):
            self.button.enable()
        if self._toolbar:
            self._toolbar.update_overflow_state()

    def set_text(self, text: str) -> None:
        if hasattr(self.button, "set_text"):
            self.button.set_text(text)
        if getattr(self, "aria_label_text", "") == getattr(self, "text", "") or not getattr(self, "aria_label_text", None):
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
        return getattr(self.button, item)


class OverflowToolbar:
    """Standardized single-row toolbar container with automated priority overflow collapse."""

    def __init__(
        self,
        *,
        title: Optional[str] = None,
        classes: str = "",
        props: str = "",
        **kwargs,
    ):
        _ensure_toolbar_css()

        base_classes = (
            "overflow-toolbar-container w-full items-center justify-between flex-nowrap overflow-hidden max-h-12 py-1 px-2 "
            + classes
        )
        self.row = ui.row().classes(base_classes)
        if props and hasattr(self.row, "props"):
            self.row.props(props)

        self.actions: List[ToolbarAction] = []

        with self.row:
            # Left title/content container
            self.left_container = ui.row().classes("items-center gap-3 shrink-0 flex-nowrap")
            if title:
                with self.left_container:
                    ui.label(title).classes("font-bold text-slate-800")

            # Right actions container
            self.actions_container = ui.row().classes("items-center gap-2 flex-nowrap shrink-0 overflow-hidden")

            # Create overflow menu button & menu
            with self.actions_container:
                self.overflow_btn = (
                    ui.button(icon="more_vert")
                    .props('flat round dense aria-label="More Actions Menu"')
                    .classes("overflow-toolbar-menu-btn hidden")
                )
                try:
                    with self.overflow_btn:
                        self.overflow_menu = ui.menu()
                except Exception:
                    self.overflow_menu = None

        if self.overflow_menu and hasattr(self.overflow_menu, "on"):
            self.overflow_menu.on("before-show", self._populate_overflow_menu)
        if hasattr(self.overflow_btn, "on"):
            self.overflow_btn.on("click", self._populate_overflow_menu)

    def __enter__(self):
        return self.row.__enter__()

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self.row.__exit__(exc_type, exc_val, exc_tb)

    @property
    def visible(self) -> bool:
        return getattr(self.row, "visible", True)

    def classes(self, *args, **kwargs):
        if hasattr(self.row, "classes"):
            self.row.classes(*args, **kwargs)
        return self

    def props(self, *args, **kwargs):
        if hasattr(self.row, "props"):
            self.row.props(*args, **kwargs)
        return self

    def set_visibility(self, visible: bool) -> None:
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
    ) -> ToolbarAction:
        """Add an action button control to the toolbar."""
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
        if hasattr(self.overflow_menu, "clear"):
            self.overflow_menu.clear()
        with self.overflow_menu:
            visible_secondaries = [
                a for a in self.actions if getattr(a, "visible", True) and not a.is_primary
            ]
            for action in visible_secondaries:
                item = ui.menu_item(
                    action.text,
                    on_click=lambda a=action: self._on_menu_item_click(a),
                )
                if action.icon:
                    with item:
                        ui.icon(action.icon).classes("mr-2")
                if not getattr(action, "enabled", True) and hasattr(item, "disable"):
                    item.disable()
                if action.aria_label_text and hasattr(item, "props"):
                    item.props(f'aria-label="{action.aria_label_text}"')
                if action.tooltip_text:
                    with item:
                        ui.tooltip(action.tooltip_text)

    def _on_menu_item_click(self, action: ToolbarAction) -> None:
        if self.overflow_menu and hasattr(self.overflow_menu, "close"):
            self.overflow_menu.close()
        action._trigger_click()

    def __getattr__(self, item):
        return getattr(self.row, item)
