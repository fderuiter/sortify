"""Responsive layout behaviour shared by the TUI screens."""

from typing import Any, Optional

from textual import events
from textual.containers import Vertical
from textual.css.query import NoMatches
from textual.widget import Widget


class ModalBox(Vertical):
    """Modal outer container that forwards scroll helpers to an inner ``.modal-body``."""

    def _body_scroll_target(self, widget: Optional[Widget] = None) -> Optional[Widget]:
        """Return inner scrollable ``.modal-body`` when outer box itself does not scroll."""
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
        """Scroll inner ``.modal-body`` to bring ``widget`` into view when separated."""
        target = self._body_scroll_target(widget)
        if target is not None:
            return target.scroll_to_widget(widget, *args, **kwargs)
        return super().scroll_to_widget(widget, *args, **kwargs)

    def scroll_home(self, *args: Any, **kwargs: Any) -> None:
        """Scroll inner ``.modal-body`` to top when separated from pinned footer."""
        bodies = list(self.query(".modal-body"))
        if self.max_scroll_y <= 0 and bodies:
            for body in bodies:
                body.scroll_home(*args, **kwargs)
            return
        super().scroll_home(*args, **kwargs)


class ResponsiveLayoutMixin:
    """Toggle ``narrow`` (<80 cols) and ``compact`` (<=100 cols or <=30 rows) classes."""

    def _resolve_viewport_height(self, height: Optional[int] = None) -> int:
        """Resolve effective viewport height from argument or mounted widget size."""
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
        """Update layout classes based on viewport width and height breakpoints."""
        effective_height = self._resolve_viewport_height(height)
        add_class = getattr(self, "add_class")
        remove_class = getattr(self, "remove_class")
        if width < 80:
            add_class("narrow")
        else:
            remove_class("narrow")

        if width <= 100 or effective_height <= 30:
            add_class("compact")
        else:
            remove_class("compact")

    def on_resize(self, event: events.Resize) -> None:
        """Handle viewport resize event across width and height dimensions."""
        try:
            self._update_layout(event.size.width, event.size.height)
        except TypeError:
            self._update_layout(event.size.width)
