"""Responsive layout behaviour and shared modal base screen for TUI dialogs."""

from typing import Any, ClassVar, Optional

from textual import events
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.screen import ModalScreen, ScreenResultType
from textual.widget import Widget

from app.ui.a11y import A11yMixin

BASE_MODAL_CSS = """
ResponsiveModalScreen {
    align: center middle;
    background: rgba(0, 0, 0, 0.6);
}
.modal-box {
    padding: 1 2;
    background: $panel;
    border: thick $primary;
    width: 90%;
    max-width: 80;
    min-width: 30;
    height: auto;
    max-height: 90%;
    overflow-y: hidden;
}
.compact .modal-box {
    padding: 0 1;
    width: 96%;
    max-height: 96%;
}
.narrow .modal-box {
    padding: 0 1;
    width: 96%;
}
.modal-body {
    width: 100%;
    height: auto;
    max-height: 100%;
    overflow-y: auto;
}
.modal-footer {
    dock: bottom;
    width: 100%;
    height: auto;
}
.modal-title {
    text-style: bold;
    color: $accent;
    margin-bottom: 1;
}
.button-row {
    dock: bottom;
    width: 100%;
    margin-top: 1;
    height: auto;
    align: right middle;
}
.modal-footer .button-row {
    dock: none;
}
Button.hidden {
    display: none;
}
"""


class ModalBox(Vertical):
    """Modal outer container that forwards scroll helpers to an inner ``.modal-body``."""

    def _body_scroll_target(self, widget: Optional[Widget] = None) -> Optional[Widget]:
        """Return inner scrollable ``.modal-body`` when outer box itself does not scroll.

        Parameters
        ----------
        widget : Optional[Widget], optional
            Target descendant widget being scrolled into view.

        Returns
        -------
        Optional[Widget]
            Matching ``.modal-body`` container if separated, otherwise ``None``.
        """
        if self.max_scroll_y > 0:
            return None
        if widget is not None:
            for anc in widget.ancestors:
                if anc is self:
                    break
                if isinstance(anc, Widget) and "modal-body" in anc.classes:
                    return anc
        bodies = list(self.query(".modal-body"))
        for body in bodies:
            if body.display and all(anc.display for anc in body.ancestors):
                return body
        if bodies:
            return bodies[0]
        try:
            return self.query_one(".modal-body", Widget)
        except NoMatches:
            return None

    def scroll_to_widget(self, widget: Widget, *args: Any, **kwargs: Any) -> bool:
        """Scroll inner ``.modal-body`` to bring ``widget`` into view when separated.

        Parameters
        ----------
        widget : Widget
            Descendant widget to scroll into view.
        *args : Any
            Positional arguments forwarded to ``Widget.scroll_to_widget``.
        **kwargs : Any
            Keyword arguments forwarded to ``Widget.scroll_to_widget``.

        Returns
        -------
        bool
            True if scrolling was scheduled or performed.
        """
        target = self._body_scroll_target(widget)
        if target is not None:
            return target.scroll_to_widget(widget, *args, **kwargs)
        return super().scroll_to_widget(widget, *args, **kwargs)

    def scroll_home(self, *args: Any, **kwargs: Any) -> None:
        """Scroll inner ``.modal-body`` to top when separated from pinned footer.

        Parameters
        ----------
        *args : Any
            Positional arguments forwarded to ``Widget.scroll_home``.
        **kwargs : Any
            Keyword arguments forwarded to ``Widget.scroll_home``.
        """
        bodies = list(self.query(".modal-body"))
        if self.max_scroll_y <= 0 and bodies:
            for body in bodies:
                body.scroll_home(*args, **kwargs)
            return
        super().scroll_home(*args, **kwargs)


class ResponsiveLayoutMixin:
    """Toggle ``narrow`` and ``compact`` classes across responsive breakpoints."""

    NARROW_WIDTH_BREAKPOINT: ClassVar[int] = 80
    COMPACT_WIDTH_BREAKPOINT: ClassVar[int] = 100
    COMPACT_HEIGHT_BREAKPOINT: ClassVar[int] = 30

    def _resolve_viewport_height(self, height: Optional[int] = None) -> int:
        """Resolve effective viewport height from argument or mounted widget size.

        Parameters
        ----------
        height : Optional[int], optional
            Explicit viewport height in rows when known.

        Returns
        -------
        int
            Effective positive terminal height in rows.
        """
        if height is not None and height > 0:
            return height
        size = getattr(self, "size", None)
        size_height = getattr(size, "height", 0)
        if isinstance(size_height, int) and size_height > 0:
            return size_height
        app = getattr(self, "app", None)
        app_size = getattr(app, "size", None)
        app_height = getattr(app_size, "height", 0)
        if isinstance(app_height, int) and app_height > 0:
            return app_height
        return 24

    def _update_layout(self, width: int, height: Optional[int] = None) -> None:
        """Update layout classes based on viewport width and height breakpoints.

        Parameters
        ----------
        width : int
            Current viewport width in columns.
        height : Optional[int], optional
            Current viewport height in rows.
        """
        effective_height = self._resolve_viewport_height(height)
        add_class = getattr(self, "add_class")
        remove_class = getattr(self, "remove_class")
        if width < self.NARROW_WIDTH_BREAKPOINT:
            add_class("narrow")
        else:
            remove_class("narrow")

        if (
            width <= self.COMPACT_WIDTH_BREAKPOINT
            or effective_height <= self.COMPACT_HEIGHT_BREAKPOINT
        ):
            add_class("compact")
        else:
            remove_class("compact")

    def on_resize(self, event: events.Resize) -> None:
        """Handle viewport resize event across width and height dimensions.

        Parameters
        ----------
        event : events.Resize
            Textual resize event carrying updated terminal dimensions.
        """
        try:
            self._update_layout(event.size.width, event.size.height)
        except TypeError:
            self._update_layout(event.size.width)


class ResponsiveModalScreen(
    ResponsiveLayoutMixin, A11yMixin, ModalScreen[ScreenResultType]
):
    """Shared responsive, accessible modal base screen composing ``Screen.CSS``."""

    CSS: ClassVar[str] = BASE_MODAL_CSS
    MODAL_CSS: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Compose ``BASE_MODAL_CSS`` and subclass ``MODAL_CSS`` into ``Screen.CSS``.

        Parameters
        ----------
        **kwargs : Any
            Keyword arguments forwarded to superclass ``__init_subclass__``.
        """
        if "CSS" not in cls.__dict__:
            parts = [BASE_MODAL_CSS.replace("ResponsiveModalScreen", cls.__name__, 1)]
            for base in reversed(cls.__mro__):
                if issubclass(base, ResponsiveModalScreen):
                    delta = base.__dict__.get("MODAL_CSS", "")
                    if delta:
                        parts.append(delta)
            cls.CSS = "\n".join(parts)

        inherited_handlers = {
            method
            for base in cls.__mro__[1:]
            for entries in base.__dict__.get("_decorated_handlers", {}).values()
            for method, _ in entries
        }
        cls_handlers = cls.__dict__.get("_decorated_handlers", {})
        for base in cls.__mro__[1:]:
            if "_decorated_handlers" in base.__dict__:
                continue
            for attr_name, value in base.__dict__.items():
                if (
                    callable(value)
                    and hasattr(value, "_textual_on")
                    and getattr(cls, attr_name, None) is value
                    and value not in inherited_handlers
                ):
                    for message_type, selectors in getattr(value, "_textual_on"):
                        cls_handlers.setdefault(message_type, []).append(
                            (value, selectors)
                        )
                    inherited_handlers.add(value)

        super().__init_subclass__(**kwargs)

    def on_mount(self) -> None:
        """Apply responsive layout breakpoint classes when the modal mounts."""
        self._update_layout(self.size.width, self.size.height)
