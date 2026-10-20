"""Keyboard shortcut cheat sheet dialog."""

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin


class ShortcutCheatSheetModal(ResponsiveLayoutMixin, A11yMixin, ModalScreen[None]):
    """Modal dialog displaying categorized TUI keyboard shortcuts cheat sheet."""

    BINDINGS = [
        Binding("escape", "close", "Close dialog", show=True),
        Binding("question_mark", "close", "Close dialog", show=False),
        Binding("f1", "close", "Close dialog", show=False),
    ]

    CSS = """
    ShortcutCheatSheetModal {
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
        overflow-y: auto;
    }
    .narrow .modal-box {
        padding: 0 1;
        width: 95%;
    }
    .modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .section-title {
        text-style: bold;
        color: $primary-lighten-2;
        margin-top: 1;
        margin-bottom: 0;
    }
    .shortcut-row {
        margin-bottom: 0;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus, Input:focus, Select:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        """Compose shortcut cheat sheet modal children."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Keyboard Shortcuts Cheat Sheet [? / F1]",
                level=1,
                classes="modal-title",
                id="modal-title",
            )

            yield Heading(
                "Navigation & Tree Controls:", level=2, classes="section-title"
            )
            yield Label(
                "  j / k       - Move selection down / up (Vim motions)",
                classes="shortcut-row",
            )
            yield Label(
                "  h / l       - Collapse / expand folder or go to parent/child",
                classes="shortcut-row",
            )
            yield Label(
                "  Ctrl+L      - Toggle file lock state", classes="shortcut-row"
            )
            yield Label("  Ctrl+R      - Rename selected node", classes="shortcut-row")
            yield Label(
                "  Ctrl+N      - Create new target folder", classes="shortcut-row"
            )
            yield Label(
                "  + / -       - Rate classification quality (+ / -)",
                classes="shortcut-row",
            )

            yield Heading("Selecting Files:", level=2, classes="section-title")
            yield Label(
                "  Space       - Check / uncheck the selected file or folder",
                classes="shortcut-row",
            )
            yield Label(
                "  a           - Check or uncheck everything",
                classes="shortcut-row",
            )
            yield Label(
                "  v           - Range select from the last checked node",
                classes="shortcut-row",
            )
            yield Label(
                "  x / i       - Exclude / include the checked files",
                classes="shortcut-row",
            )
            yield Label(
                "  r           - Move the checked files to another folder",
                classes="shortcut-row",
            )
            yield Label(
                "  Ctrl+F      - Search the plan tree",
                classes="shortcut-row",
            )

            yield Heading("Application Operations:", level=2, classes="section-title")
            yield Label(
                "  Ctrl+S      - Scan directory and generate plan",
                classes="shortcut-row",
            )
            yield Label(
                "  Ctrl+Y      - Session history & audit log export",
                classes="shortcut-row",
            )
            yield Label(
                "  Ctrl+U      - Open Quarantine Vault dialog",
                classes="shortcut-row",
            )
            yield Label("  Ctrl+E      - Execute sorting plan", classes="shortcut-row")
            yield Label(
                "  Ctrl+B      - Choose the folder to sort",
                classes="shortcut-row",
            )
            yield Label(
                "  Ctrl+O      - Open application settings", classes="shortcut-row"
            )
            yield Label(
                "  Ctrl+W      - Open model onboarding wizard", classes="shortcut-row"
            )
            yield Label(
                "  Ctrl+X      - Launch PDF extraction strategy preview",
                classes="shortcut-row",
            )
            yield Label(
                "  Ctrl+C      - Open CRO multi-study forensic ingestion",
                classes="shortcut-row",
            )

            yield Label(
                "  Ctrl+D      - Open DropZone to sort dropped files",
                classes="shortcut-row",
            )
            yield Label(
                "  Ctrl+A      - Open the audit report of the last run",
                classes="shortcut-row",
            )
            yield Label(
                "  Ctrl+K      - Open the model manager",
                classes="shortcut-row",
            )
            yield Label(
                "  G           - Create a sample dataset to try sorting on",
                classes="shortcut-row",
            )

            yield Heading("Help & System:", level=2, classes="section-title")
            yield Label(
                "  ? / F1      - Open shortcut cheat sheet", classes="shortcut-row"
            )
            yield Label("  Escape      - Close modal dialog", classes="shortcut-row")
            yield Label("  Ctrl+Q      - Quit application", classes="shortcut-row")

            with Horizontal(classes="button-row"):
                btn_close = Button("Close", id="btn-close", variant="primary")
                btn_close.tooltip = "Close keyboard shortcut cheat sheet modal dialog"
                yield btn_close

    def on_mount(self) -> None:
        """Focus close button on mount and emit screen reader announcement."""
        self._update_layout(self.size.width)
        self.query_one("#btn-close", Button).focus()
        self.announce("Opened keyboard shortcut cheat sheet dialog.")

    @on(Button.Pressed, "#btn-close")
    def action_close(self) -> None:
        """Close shortcut cheat sheet modal."""
        self.announce("Closed keyboard shortcut cheat sheet dialog.")
        self.dismiss(None)
