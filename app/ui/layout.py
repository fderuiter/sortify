"""Responsive layout behaviour shared by the TUI screens."""

from textual import events


class ResponsiveLayoutMixin:
    """Toggle the ``narrow`` CSS class when the viewport drops below 80 columns."""

    def _update_layout(self, width: int) -> None:
        """Update modal layout based on viewport width breakpoint."""
        if width < 80:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle modal viewport resize event."""
        self._update_layout(event.size.width)
