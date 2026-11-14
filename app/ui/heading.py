"""Centralized native semantic heading helper module for accessible TUI and GUI views."""

from typing import Any, Optional

from rich.console import RenderableType
from textual.reactive import reactive
from textual.widgets import Label


class Heading(Label):
    """Native semantic heading element providing WCAG 2.1 compliance.

    Renders screen headers, modal dialog titles, and card section headers with explicit
    semantic levels (h1 through h6), role attributes, and reactive update capabilities
    while preserving Rich/Textual visual layout and styling.
    """

    level = reactive(1)

    def __init__(
        self,
        content: RenderableType = "",
        level: int = 1,
        *,
        expand: bool = False,
        shrink: bool = False,
        markup: bool = True,
        name: Optional[str] = None,
        id: Optional[str] = None,
        classes: Optional[str] = None,
        disabled: bool = False,
        tooltip: Optional[RenderableType] = None,
    ) -> None:
        clamped_level = max(1, min(6, int(level)))
        super().__init__(
            content,
            expand=expand,
            shrink=shrink,
            markup=markup,
            name=name,
            id=id,
            classes=classes,
            disabled=disabled,
        )
        self.level = clamped_level
        if tooltip is not None:
            self.tooltip = tooltip

    def validate_level(self, value: int) -> int:
        """Validate and clamp heading level between 1 (h1) and 6 (h6)."""
        return max(1, min(6, int(value)))

    def watch_level(self, old_level: int, new_level: int) -> None:
        """Reactive watcher called when heading level changes."""
        self.refresh()

    @property
    def tag(self) -> str:
        """Return native HTML heading tag name (e.g., 'h1', 'h2')."""
        return f"h{self.level}"

    @property
    def role(self) -> str:
        """Return ARIA role for accessibility tree indexing."""
        return "heading"

    @property
    def aria_level(self) -> str:
        """Return string value of ARIA level attribute."""
        return str(self.level)


# Alias for Heading to support explicit SemanticHeading component references
SemanticHeading = Heading


def create_heading(
    text: RenderableType = "",
    level: int = 1,
    classes: Optional[str] = None,
    id: Optional[str] = None,
    **kwargs: Any,
) -> Heading:
    """Centralized helper function to generate reactive semantic Heading instances."""
    return Heading(content=text, level=level, classes=classes, id=id, **kwargs)
