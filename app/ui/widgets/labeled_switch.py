"""A switch with a visible label and screen-reader announcements."""

from typing import Any

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widgets import Label, Switch


class LabeledSwitch(Horizontal):
    """A horizontal container combining a Switch and an associated Label with container-level click delegation."""

    DEFAULT_CSS = """
    LabeledSwitch {
        margin-top: 1;
        margin-bottom: 1;
        height: 3;
        align: left middle;
    }
    """

    def __init__(
        self,
        label: str,
        value: bool = False,
        switch_id: str | None = None,
        tooltip: str | None = None,
        *,
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ) -> None:
        cls_list = ["switch-row"]
        if classes:
            cls_list.append(classes)
        super().__init__(
            name=name,
            id=id,
            classes=" ".join(cls_list),
            disabled=disabled,
        )
        self.label_text = label
        effective_switch_id = switch_id or (
            id if id and id.startswith("switch-") else None
        )
        self.switch = Switch(value=value, id=effective_switch_id)
        if tooltip:
            self.tooltip = tooltip
            self.switch.tooltip = tooltip
        formatted_label = label if label.startswith(" ") else f" {label}"
        self.label_widget = Label(formatted_label)

    def compose(self) -> ComposeResult:
        """Yield child Switch and Label controls."""
        yield self.switch
        yield self.label_widget

    @property
    def value(self) -> bool:
        """Get the underlying switch value."""
        return self.switch.value

    @value.setter
    def value(self, val: bool) -> None:
        """Set the underlying switch value."""
        self.switch.value = val

    def _is_switch_target(self, target: Any) -> bool:
        """Check if target widget is the child switch or contained within it."""
        if target is None:
            return False
        curr = target
        while curr is not None and curr is not self:
            if curr is self.switch:
                return True
            curr = getattr(curr, "parent", None)
        return False

    def on_click(self, event: events.Click) -> None:
        """Handle mouse click events on container or label by toggling and focusing the switch."""
        if self.disabled or self.switch.disabled:
            return

        target_widget = getattr(event, "widget", getattr(event, "control", None))
        if not self._is_switch_target(target_widget):
            self.switch.value = not self.switch.value
            self.switch.focus()
            event.stop()
        else:
            self.switch.focus()
