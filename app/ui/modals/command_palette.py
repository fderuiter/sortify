"""Command palette overlay modal screen."""

from dataclasses import dataclass
from typing import List, Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, OptionList
from textual.widgets.option_list import Option

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin


@dataclass
class PaletteCommand:
    """Descriptor for a searchable command palette action."""

    id: str
    title: str
    category: str
    shortcut: str = ""
    description: str = ""
    action_name: str = ""


DEFAULT_COMMAND_CATALOG: List[PaletteCommand] = [
    PaletteCommand(
        id="scan_dir",
        title="Scan Active Directory",
        category="Workspace",
        shortcut="Ctrl+S",
        description="Scan current directory and generate sorting plan",
        action_name="action_scan_directory",
    ),
    PaletteCommand(
        id="select_dir",
        title="Choose Working Folder",
        category="Workspace",
        shortcut="Ctrl+B",
        description="Browse and choose directory to sort",
        action_name="action_select_dir",
    ),
    PaletteCommand(
        id="execute_sort",
        title="Execute Sorting Plan",
        category="Workspace",
        shortcut="Ctrl+E",
        description="Execute pending document organization plan",
        action_name="action_execute_sort",
    ),
    PaletteCommand(
        id="sample_corpus",
        title="Generate Sample Dataset",
        category="Workspace",
        shortcut="G",
        description="Generate sample document dataset for testing",
        action_name="action_generate_sample_corpus",
    ),
    PaletteCommand(
        id="open_dropzone",
        title="Open DropZone Overlay",
        category="Workspace",
        shortcut="Ctrl+D",
        description="Batch drag-and-drop triage overlay",
        action_name="action_open_dropzone",
    ),
    PaletteCommand(
        id="open_settings",
        title="Open Settings",
        category="Settings",
        shortcut="Ctrl+O",
        description="Configure application settings and rules",
        action_name="action_open_settings",
    ),
    PaletteCommand(
        id="open_model_manager",
        title="Open Model Manager",
        category="Models",
        shortcut="",
        description="Manage ONNX model downloads and local embeddings",
        action_name="action_open_model_manager",
    ),
    PaletteCommand(
        id="open_wizard",
        title="Open Onboarding Wizard",
        category="Onboarding",
        shortcut="Ctrl+W",
        description="Run initial setup and AI model consent wizard",
        action_name="action_open_wizard",
    ),
    PaletteCommand(
        id="open_cro_forensic",
        title="Open CRO Forensic Ingestion",
        category="Compliance",
        shortcut="Ctrl+C",
        description="Multi-study clinical compliance ingestion workflow",
        action_name="action_open_cro_forensic",
    ),
    PaletteCommand(
        id="open_quarantine_vault",
        title="Open Quarantine Vault",
        category="Compliance",
        shortcut="Ctrl+U",
        description="Inspect and release quarantined files",
        action_name="action_open_quarantine_vault",
    ),
    PaletteCommand(
        id="open_audit_report",
        title="Open Audit Report",
        category="Reports",
        shortcut="Ctrl+A",
        description="View SHA-256 post-sort verification report",
        action_name="action_open_audit_report",
    ),
    PaletteCommand(
        id="export_simulation_report",
        title="Export Simulation Report",
        category="Reports",
        shortcut="",
        description="Export dry-run simulation report to HTML or JSON",
        action_name="action_export_simulation_report",
    ),
    PaletteCommand(
        id="open_history",
        title="Open Session History",
        category="History",
        shortcut="Ctrl+Y",
        description="View past session rollbacks and audit logs",
        action_name="action_open_history",
    ),
    PaletteCommand(
        id="open_extraction_preview",
        title="Preview PDF Extraction",
        category="Tools",
        shortcut="Ctrl+X",
        description="Preview PDF visual text extraction strategy",
        action_name="action_open_extraction_preview",
    ),
    PaletteCommand(
        id="focus_search",
        title="Search Plan Tree",
        category="Tools",
        shortcut="Ctrl+F",
        description="Focus interactive plan tree search filter",
        action_name="action_focus_search",
    ),
    PaletteCommand(
        id="open_cheat_sheet",
        title="Open Shortcut Cheat Sheet",
        category="Help",
        shortcut="? / F1",
        description="View keyboard shortcuts cheat sheet",
        action_name="action_open_cheat_sheet",
    ),
    PaletteCommand(
        id="quit",
        title="Quit Application",
        category="System",
        shortcut="Ctrl+Q",
        description="Exit Smart AutoSorter AI Pro",
        action_name="action_quit",
    ),
]


class CommandPaletteModal(
    ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[PaletteCommand]]
):
    """Modal screen presenting a searchable command palette overlay."""

    BINDINGS = [
        Binding("escape", "cancel", "Close palette", show=True),
        Binding("up", "cursor_up", "Move selection up", show=False),
        Binding("down", "cursor_down", "Move selection down", show=False),
    ]

    CSS = """
    CommandPaletteModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }
    .palette-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 80%;
        max-width: 76;
        min-width: 40;
        height: auto;
        max-height: 85%;
    }
    .narrow .palette-box {
        padding: 0 1;
        width: 95%;
    }
    .palette-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .palette-input {
        margin-bottom: 1;
    }
    .palette-list {
        height: 12;
        max-height: 16;
        border: solid $secondary;
        background: $surface;
    }
    .palette-status {
        margin-top: 1;
        color: $text-muted;
        text-style: italic;
    }
    """

    def __init__(
        self,
        catalog: Optional[List[PaletteCommand]] = None,
        name: Optional[str] = None,
        id: Optional[str] = None,
        classes: Optional[str] = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self.commands: List[PaletteCommand] = catalog or list(DEFAULT_COMMAND_CATALOG)
        self._filtered_commands: List[PaletteCommand] = list(self.commands)

    def compose(self) -> ComposeResult:
        """Compose modal layout widgets."""
        with Vertical(classes="palette-box modal-box"):
            yield Heading(
                "Command Palette Overlay [Ctrl+K]",
                level=1,
                classes="palette-title modal-title",
            )
            yield Input(
                placeholder="Type a command or search actions...",
                id="palette-input",
                classes="palette-input",
            )
            yield OptionList(id="palette-list", classes="palette-list")
            yield Label(
                f"{len(self._filtered_commands)} commands available",
                id="palette-status",
                classes="palette-status",
            )

    def on_mount(self) -> None:
        """Initialize palette list, focus search input, and emit screen reader announcement."""
        self._update_layout(self.size.width)
        self._populate_option_list()
        self.query_one("#palette-input", Input).focus()
        self.announce(
            "Opened command palette overlay dialog. Type to search actions.",
            priority="polite",
        )

    def _populate_option_list(self) -> None:
        """Populate OptionList with filtered command items."""
        option_list = self.query_one("#palette-list", OptionList)
        option_list.clear_options()
        for cmd in self._filtered_commands:
            label = f"[{cmd.category}] {cmd.title}"
            if cmd.shortcut:
                label += f" ({cmd.shortcut})"
            option_list.add_option(Option(label, id=cmd.id))

        status_lbl = self.query_one("#palette-status", Label)
        if self._filtered_commands:
            option_list.highlighted = 0
            status_lbl.update(f"{len(self._filtered_commands)} command(s) available")
        else:
            status_lbl.update("No matching commands found")

    @on(Input.Changed, "#palette-input")
    def _on_input_changed(self, event: Input.Changed) -> None:
        """Filter command list in real time as query text changes."""
        query = event.value.strip().lower()
        if not query:
            self._filtered_commands = list(self.commands)
        else:
            self._filtered_commands = [
                cmd
                for cmd in self.commands
                if query in cmd.title.lower()
                or query in cmd.category.lower()
                or query in cmd.description.lower()
                or query in cmd.shortcut.lower()
            ]
        self._populate_option_list()
        self.announce(
            f"{len(self._filtered_commands)} matching command(s) found.",
            priority="polite",
        )

    def action_cursor_up(self) -> None:
        """Move list selection up."""
        option_list = self.query_one("#palette-list", OptionList)
        if option_list.option_count > 0:
            if option_list.highlighted is None:
                option_list.highlighted = option_list.option_count - 1
            else:
                option_list.highlighted = max(0, option_list.highlighted - 1)

    def action_cursor_down(self) -> None:
        """Move list selection down."""
        option_list = self.query_one("#palette-list", OptionList)
        if option_list.option_count > 0:
            if option_list.highlighted is None:
                option_list.highlighted = 0
            else:
                option_list.highlighted = min(
                    option_list.option_count - 1, option_list.highlighted + 1
                )

    @on(Input.Submitted, "#palette-input")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        """Execute currently highlighted command when Enter is pressed in input field."""
        option_list = self.query_one("#palette-list", OptionList)
        highlighted = option_list.highlighted
        if highlighted is not None and 0 <= highlighted < len(self._filtered_commands):
            self._execute_command(self._filtered_commands[highlighted])

    @on(OptionList.OptionSelected, "#palette-list")
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        """Execute selected command when an OptionList item is clicked or selected."""
        opt_id = event.option.id
        for cmd in self._filtered_commands:
            if cmd.id == opt_id:
                self._execute_command(cmd)
                break

    def _execute_command(self, cmd: PaletteCommand) -> None:
        """Announce action execution and dismiss modal with selected command payload."""
        self.announce(f"Executing action: {cmd.title}", priority="polite")
        self.dismiss(cmd)

    def action_cancel(self) -> None:
        """Close command palette without executing any action."""
        self.announce("Closed command palette overlay dialog.", priority="polite")
        self.dismiss(None)
