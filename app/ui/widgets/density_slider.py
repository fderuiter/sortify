"""Keyboard slider for choosing the layout density."""

from textual import events
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget


class DensitySlider(Widget):
    """Interactive slider widget for text density threshold adjustment."""

    can_focus = True
    value = reactive(50)

    DEFAULT_CSS = """
    DensitySlider {
        width: 100%;
        height: 1;
        margin-top: 1;
        margin-bottom: 1;
    }
    DensitySlider:focus {
        color: $accent;
        text-style: bold;
    }
    """

    class Changed(Message):
        """Event posted when slider value changes."""

        def __init__(self, slider: "DensitySlider", value: int) -> None:
            self.slider = slider
            self.value = value
            super().__init__()

        @property
        def control(self) -> Widget:
            """Return control widget associated with this message."""
            return self.slider

    def __init__(
        self,
        value: int = 50,
        min_value: int = 0,
        max_value: int = 500,
        step: int = 5,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes, disabled=disabled)
        self.min_value = min_value
        self.max_value = max_value
        self.step = step
        # set_reactive avoids a pre-mount watcher firing with the class default.
        self.set_reactive(DensitySlider.value, max(min_value, min(max_value, value)))

    def watch_value(self, new_value: int) -> None:
        """Emit Changed event on value change."""
        self.post_message(self.Changed(self, new_value))

    def on_key(self, event: events.Key) -> None:
        """Handle keyboard key events to update slider position."""
        if event.key in ("left", "down"):
            self.value = max(self.min_value, self.value - self.step)
            event.stop()
        elif event.key in ("right", "up"):
            self.value = min(self.max_value, self.value + self.step)
            event.stop()
        elif event.key == "page_down":
            self.value = max(self.min_value, self.value - self.step * 5)
            event.stop()
        elif event.key == "page_up":
            self.value = min(self.max_value, self.value + self.step * 5)
            event.stop()
        elif event.key == "home":
            self.value = self.min_value
            event.stop()
        elif event.key == "end":
            self.value = self.max_value
            event.stop()

    def on_click(self, event: events.Click) -> None:
        """Handle mouse click to jump slider to position."""
        width = self.size.width
        if width > 0:
            pct = max(0.0, min(1.0, event.x / float(width)))
            new_val = self.min_value + int(pct * (self.max_value - self.min_value))
            rem = new_val % self.step
            if rem != 0:
                new_val = new_val - rem + (self.step if rem >= self.step / 2 else 0)
            self.value = max(self.min_value, min(self.max_value, new_val))

    def render(self) -> str:
        """Render visual slider bar string representation."""
        width = max(10, self.size.width - 8) if self.size.width > 8 else 20
        pct = (self.value - self.min_value) / max(1, (self.max_value - self.min_value))
        pos = int(pct * (width - 1))
        pos = max(0, min(width - 1, pos))
        bar = "━" * pos + "●" + "─" * (width - 1 - pos)
        return f"[{bar}] {self.value}"
