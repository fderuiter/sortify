"""Drop zone dialog for sorting dropped files, with its plan confirmation."""

import logging
import os
import re
import shlex
from datetime import datetime
from typing import Any, Optional

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import Button, DataTable, Input, Label, Static

from app.ui.heading import Heading
from app.ui.layout import ModalBox, ResponsiveModalScreen
from app.ui.notifications import TROUBLESHOOTING_URL

logger = logging.getLogger(__name__)

_WIN_DRIVE_RE = re.compile(r"^[a-zA-Z]:[/\\]")


class DropPlanConfirmModal(ResponsiveModalScreen[bool]):
    """Show the DropZone plan, surface collision warnings, and apply user overrides before moving."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-box {
        max-width: 100;
        max-height: 95%;
    }
    .modal-body {
        height: auto;
        overflow-y: auto;
    }
    #drop-plan-warnings {
        height: auto;
        max-height: 5;
        overflow-y: auto;
        border: solid $warning;
        color: $warning;
        padding: 0 1;
        margin-bottom: 0;
    }
    #drop-plan-warnings.hidden {
        display: none;
    }
    #drop-plan-tree {
        height: auto;
        max-height: 10;
        overflow-y: auto;
        border: solid $secondary;
        padding: 0 1;
    }
    #drop-plan-items-table {
        height: 5;
        min-height: 4;
        max-height: 6;
        border: solid $accent;
        margin-top: 0;
        margin-bottom: 0;
    }
    .plan-policy-row {
        width: 100%;
        height: auto;
        layout: grid;
        grid-size: 4;
        grid-columns: 1fr 1fr 1fr 1fr;
        grid-gutter: 0 1;
        margin-top: 0;
        margin-bottom: 0;
    }
    .plan-policy-row Button {
        width: 100%;
        min-width: 0;
        margin: 0;
    }
    .plan-override-row {
        width: 100%;
        height: auto;
        margin-top: 0;
        margin-bottom: 0;
        align: left middle;
    }
    #input-plan-folder-override {
        width: 1fr;
        margin: 0;
    }
    #btn-apply-folder-override {
        width: auto;
        min-width: 12;
        margin: 0 0 0 1;
    }
    .compact .plan-policy-row Button,
    .compact .plan-override-row Button,
    .compact .button-row Button {
        height: 1;
        min-height: 1;
        border: none;
    }
    """

    def __init__(
        self,
        summary: str,
        plan_lines: list[str],
        *,
        warnings: Optional[list[str]] = None,
        batch_plan: Any = None,
        coordinator: Any = None,
    ):
        super().__init__()
        self.summary = summary
        self.plan_lines = list(plan_lines)
        self.warnings: list[str] = list(
            warnings
            if warnings is not None
            else (getattr(batch_plan, "warnings", None) or [])
        )
        self.batch_plan = batch_plan
        self.coordinator = coordinator

    def _format_warnings_text(self) -> str:
        if not self.warnings:
            return ""
        return "\n".join(f"! {w}" for w in self.warnings)

    def compose(self) -> ComposeResult:
        """Compose the plan preview, collision warnings, override controls, and action buttons."""
        with ModalBox(classes="modal-box"):
            yield Heading("Review DropZone Plan", level=1, classes="modal-title")
            with Vertical(classes="modal-body"):
                yield Label(self.summary, id="lbl-drop-plan-summary")
                warn_classes = "" if self.warnings else "hidden"
                yield Static(
                    self._format_warnings_text(),
                    id="drop-plan-warnings",
                    classes=warn_classes,
                    markup=False,
                )
                yield Static(
                    "\n".join(self.plan_lines), id="drop-plan-tree", markup=False
                )
                if self.batch_plan is not None:
                    items_table = DataTable(id="drop-plan-items-table")
                    items_table.cursor_type = "row"
                    items_table.tooltip = "Planned drop items; select a row to override its folder or exclusion"
                    yield items_table

                    with Horizontal(classes="plan-policy-row"):
                        btn_ren = Button(
                            "Policy: Rename",
                            id="btn-policy-rename",
                            variant="default",
                        )
                        btn_ren.tooltip = "Auto-rename colliding files while keeping clean filenames for higher queue priority"
                        yield btn_ren

                        btn_ovr = Button(
                            "Policy: Overwrite",
                            id="btn-policy-overwrite",
                            variant="warning",
                        )
                        btn_ovr.tooltip = "Overwrite colliding destination files (displaced originals are backed up for undo)"
                        yield btn_ovr

                        btn_skp = Button(
                            "Policy: Skip",
                            id="btn-policy-skip",
                            variant="default",
                        )
                        btn_skp.tooltip = (
                            "Skip moving files whose destination path already exists"
                        )
                        yield btn_skp

                        btn_exc = Button(
                            "Toggle Exclude",
                            id="btn-toggle-plan-exclude",
                            variant="error",
                        )
                        btn_exc.tooltip = "Exclude or re-include the selected file from this drop execution"
                        yield btn_exc

                    with Horizontal(classes="plan-override-row"):
                        folder_inp = Input(
                            placeholder="Override target folder for selected item (e.g. Finance or .)...",
                            id="input-plan-folder-override",
                        )
                        folder_inp.tooltip = "Enter a target category folder override for the selected item"
                        yield folder_inp

                        btn_folder = Button(
                            "Set Folder",
                            id="btn-apply-folder-override",
                            variant="primary",
                        )
                        btn_folder.tooltip = "Apply the entered target folder override to the selected item"
                        yield btn_folder

            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel-drop-plan")
                btn_cancel.tooltip = "Close without moving any files"
                yield btn_cancel
                btn_move = Button(
                    "Move Files", id="btn-confirm-drop-plan", variant="success"
                )
                btn_move.tooltip = "Move the files as shown. You can undo it with Ctrl+Z or History (Ctrl+Y)."
                yield btn_move

    def on_mount(self) -> None:
        """Populate item table and focus Cancel so Enter never moves files by accident."""
        super().on_mount()
        if self.batch_plan is not None:
            self._refresh_plan_view()
        self.query_one("#btn-cancel-drop-plan", Button).focus()

    def _selected_plan_item(self) -> Any:
        if self.batch_plan is None or not getattr(self.batch_plan, "items", None):
            return None
        try:
            table = self.query_one("#drop-plan-items-table", DataTable)
            row_idx = table.cursor_row
        except Exception:
            row_idx = 0
        items = list(self.batch_plan.items)
        if row_idx is not None and 0 <= row_idx < len(items):
            return items[row_idx]
        return items[0] if items else None

    def _refresh_plan_view(self) -> None:
        """Synchronize summary, warnings, plan lines, and interactive item table from batch_plan."""
        if self.batch_plan is not None:
            self.warnings = list(getattr(self.batch_plan, "warnings", None) or [])
            self.plan_lines = list(
                getattr(self.batch_plan, "plan_lines", None) or self.plan_lines
            )
            active_items = [
                it
                for it in self.batch_plan.items
                if not getattr(it, "is_excluded", False)
            ]
            folders = {(it.dest_base_dir, it.target_folder) for it in active_items}
            self.summary = (
                f"Move {len(active_items)} file(s) into {len(folders)} folder(s)? "
                f"[Conflict policy: {self.batch_plan.conflict_policy}] "
                "Nothing has been moved yet."
            )

        try:
            self.query_one("#lbl-drop-plan-summary", Label).update(self.summary)
        except Exception:
            pass

        try:
            warn_widget = self.query_one("#drop-plan-warnings", Static)
            warn_widget.update(self._format_warnings_text())
            if self.warnings:
                warn_widget.remove_class("hidden")
            else:
                warn_widget.add_class("hidden")
        except Exception:
            pass

        try:
            self.query_one("#drop-plan-tree", Static).update("\n".join(self.plan_lines))
        except Exception:
            pass

        if self.batch_plan is not None:
            try:
                table = self.query_one("#drop-plan-items-table", DataTable)
                prev_cursor = table.cursor_row or 0
                table.clear(columns=True)
                table.add_columns("State", "File", "Target Folder", "Policy")
                for it in self.batch_plan.items:
                    if it.is_excluded:
                        state = "Excluded"
                    elif it.has_collision:
                        state = "Collision"
                    else:
                        state = "Include"
                    table.add_row(
                        state,
                        os.path.basename(it.source_path),
                        it.target_folder,
                        it.conflict_policy,
                    )
                if self.batch_plan.items:
                    table.move_cursor(
                        row=min(prev_cursor, len(self.batch_plan.items) - 1)
                    )
            except Exception:
                pass

    def set_conflict_policy(self, policy: str, *, selected_only: bool = False) -> None:
        """Apply ``policy`` ('rename', 'overwrite', 'skip') globally or to the selected item."""
        if self.batch_plan is None or self.coordinator is None:
            return
        if selected_only:
            sel = self._selected_plan_item()
            if sel is None:
                return
            self.coordinator.apply_user_overrides(
                self.batch_plan,
                item_overrides={sel.item_id: {"conflict_policy": policy}},
            )
        else:
            self.coordinator.apply_user_overrides(
                self.batch_plan, conflict_policy=policy
            )
        self._refresh_plan_view()
        self.announce(f"Set drop conflict policy to '{policy}'.")

    def toggle_selected_exclusion(self) -> None:
        """Toggle ``is_excluded`` on the currently highlighted plan item."""
        if self.batch_plan is None or self.coordinator is None:
            return
        sel = self._selected_plan_item()
        if sel is None:
            return
        new_excl = not bool(sel.is_excluded)
        self.coordinator.apply_user_overrides(
            self.batch_plan,
            item_overrides={sel.item_id: {"is_excluded": new_excl}},
        )
        self._refresh_plan_view()
        verb = "Excluded" if new_excl else "Included"
        self.announce(f"{verb} '{os.path.basename(sel.source_path)}'.")

    def apply_folder_override(self, folder: Optional[str] = None) -> None:
        """Override the target category folder of the currently highlighted plan item."""
        if self.batch_plan is None or self.coordinator is None:
            return
        sel = self._selected_plan_item()
        if sel is None:
            return
        raw_folder = folder
        if raw_folder is None:
            try:
                inp = self.query_one("#input-plan-folder-override", Input)
                raw_folder = inp.value.strip()
            except Exception:
                raw_folder = ""
        if raw_folder is None or raw_folder == "":
            return
        try:
            self.coordinator.apply_user_overrides(
                self.batch_plan,
                item_overrides={sel.item_id: {"target_folder": raw_folder}},
            )
            try:
                self.query_one("#input-plan-folder-override", Input).value = ""
            except Exception:
                pass
            self._refresh_plan_view()
            self.announce(
                f"Overrode folder for '{os.path.basename(sel.source_path)}' to '{raw_folder}'."
            )
        except (ValueError, PermissionError) as exc:
            self.announce(f"Invalid folder override: {exc}")

    @on(Button.Pressed, "#btn-policy-rename")
    def _on_policy_rename(self) -> None:
        self.set_conflict_policy("rename")

    @on(Button.Pressed, "#btn-policy-overwrite")
    def _on_policy_overwrite(self) -> None:
        self.set_conflict_policy("overwrite")

    @on(Button.Pressed, "#btn-policy-skip")
    def _on_policy_skip(self) -> None:
        self.set_conflict_policy("skip")

    @on(Button.Pressed, "#btn-toggle-plan-exclude")
    def _on_toggle_exclude(self) -> None:
        self.toggle_selected_exclusion()

    @on(Button.Pressed, "#btn-apply-folder-override")
    def _on_apply_folder_override(self) -> None:
        self.apply_folder_override()

    @on(Input.Submitted, "#input-plan-folder-override")
    def _on_submit_folder_override(self, event: Input.Submitted) -> None:
        if event.value.strip():
            self.apply_folder_override(event.value.strip())

    @on(Button.Pressed, "#btn-confirm-drop-plan")
    def action_confirm(self) -> None:
        """Confirm the moves."""
        if self.batch_plan is not None:
            active_items = [
                it
                for it in self.batch_plan.items
                if not getattr(it, "is_excluded", False)
            ]
            if not active_items:
                self.announce(
                    "All items are excluded; cancel or include at least one item."
                )
                return
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel-drop-plan")
    def action_cancel(self) -> None:
        """Cancel without moving anything."""
        self.dismiss(False)


class DropZoneModal(ResponsiveModalScreen[Optional[dict[str, Any]]]):
    """Floating overlay modal dialog for quick drag-and-drop or paste file triage."""

    HELP_URL = f"{TROUBLESHOOTING_URL}#dropzone-errors"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    DropZoneModal {
        background: rgba(0, 0, 0, 0.7);
    }
    .modal-box {
        border: double $accent;
        width: 92%;
        max-width: 100;
        min-width: 40;
        max-height: 95%;
    }
    .compact .modal-box, .narrow .modal-box {
        width: 98%;
    }
    .modal-title {
        margin-bottom: 0;
    }
    .dropzone-target {
        border: dashed $primary;
        background: $surface;
        padding: 0 1;
        margin-top: 0;
        margin-bottom: 0;
        align: center middle;
        height: auto;
    }
    .dropzone-instructions {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 0;
    }
    .dropzone-subinstructions {
        color: $text-muted;
        text-align: center;
    }
    #input-queue-filter {
        margin-top: 0;
        margin-bottom: 0;
    }
    .dest-row {
        width: 100%;
        height: auto;
        margin-top: 0;
        margin-bottom: 0;
        align: left middle;
    }
    #input-dest-dir {
        width: 1fr;
        margin-top: 0;
        margin-bottom: 0;
    }
    #btn-browse-dest {
        width: auto;
        min-width: 12;
        margin: 0 0 0 1;
    }
    .compact .dest-row Button {
        height: 1;
        min-height: 1;
        border: none;
        min-width: 10;
    }
    .dropzone-split-container {
        height: auto;
        min-height: 7;
        layout: horizontal;
        margin-top: 0;
        margin-bottom: 0;
    }
    .narrow .dropzone-split-container {
        layout: vertical;
    }
    #dropzone-staging-table {
        width: 60%;
        height: 7;
        min-height: 6;
        margin-top: 0;
        margin-bottom: 0;
        border: solid $accent;
    }
    .narrow #dropzone-staging-table {
        width: 100%;
        height: 6;
    }
    #dropzone-metadata-preview {
        width: 40%;
        height: 7;
        min-height: 6;
        border: solid $primary;
        background: $surface;
        padding: 0 1;
        margin-left: 1;
        overflow-x: hidden;
        overflow-y: auto;
    }
    .narrow #dropzone-metadata-preview {
        width: 100%;
        height: auto;
        min-height: 4;
        margin-left: 0;
        margin-top: 1;
    }
    .preview-header {
        text-style: bold;
        color: $accent;
        margin-bottom: 0;
    }
    .preview-field {
        color: $text;
    }
    .status-text {
        color: $accent;
        margin-top: 0;
        margin-bottom: 0;
        text-style: italic;
    }
    .button-row {
        margin-top: 0;
    }
    .compact .button-row Button {
        height: 1;
        min-height: 1;
        border: none;
    }
    .queue-row {
        width: 100%;
        margin-top: 0;
        layout: grid;
        grid-size: 3;
        grid-columns: 1fr 1fr 1fr;
        grid-gutter: 0 1;
        height: auto;
    }
    .queue-row Button {
        width: 100%;
        min-width: 0;
        margin: 0;
    }
    .compact .queue-row Button {
        height: 1;
        min-height: 1;
        border: none;
    }
    Button:focus, Input:focus, DataTable:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(
        self,
        settings: Any = None,
        base_dir: Optional[str] = None,
        initial_paths: Optional[list[str]] = None,
        initial_dest_dir: Optional[str] = None,
    ):
        super().__init__()
        self.settings = settings
        self.base_dir = base_dir
        self.initial_dest_dir = initial_dest_dir
        self.initial_paths: list[str] = list(initial_paths) if initial_paths else []
        self.processed_result: Optional[dict[str, Any]] = None
        self.staged_items: list[dict[str, Any]] = []
        self._coordinator: Optional[Any] = None
        self._pending_batch_plan: Optional[Any] = None
        self._pending_drop: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        """Compose floating dropzone modal overlay controls."""
        with ModalBox(classes="modal-box"):
            yield Heading(
                "Floating DropZone - Quick File Triage",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            with Vertical(classes="modal-body"):
                with Vertical(classes="dropzone-target"):
                    yield Label(
                        "📥 Drop or Paste File / Folder Paths Here",
                        classes="dropzone-instructions",
                    )
                    yield Label(
                        "Drag items onto terminal or paste absolute file paths to organize automatically",
                        classes="dropzone-subinstructions",
                    )
                inp = Input(
                    placeholder="Paste or drop file / directory paths here...",
                    id="input-drop-paths",
                )
                inp.tooltip = "Enter or drop target file or folder paths to trigger automated sorting"
                yield inp

                with Horizontal(classes="dest-row"):
                    dest_inp = Input(
                        value=self.initial_dest_dir or "",
                        placeholder="Destination folder (optional, leave blank to sort in place)...",
                        id="input-dest-dir",
                    )
                    dest_inp.tooltip = (
                        "Destination directory where sorted files will be placed "
                        "(leave empty to sort each item inside its own folder)"
                    )
                    yield dest_inp

                    btn_browse = Button(
                        "Browse", id="btn-browse-dest", variant="default"
                    )
                    btn_browse.tooltip = (
                        "Browse for a destination directory using native folder dialog"
                    )
                    yield btn_browse

                filter_inp = Input(
                    placeholder="Filter staged queue...",
                    id="input-queue-filter",
                )
                filter_inp.tooltip = "Filter staged queue items by typing keywords (filename, path, status, extension)"
                yield filter_inp

                with Container(classes="dropzone-split-container"):
                    table = DataTable(id="dropzone-staging-table")
                    table.cursor_type = "row"
                    table.tooltip = (
                        "Interactive staging queue displaying file status and details"
                    )
                    yield table

                    with Vertical(id="dropzone-metadata-preview"):
                        yield Label("📋 Metadata Preview", classes="preview-header")
                        yield Label(
                            "Path: N/A", id="preview-path", classes="preview-field"
                        )
                        yield Label(
                            "Size: N/A", id="preview-size", classes="preview-field"
                        )
                        yield Label(
                            "Modified: N/A", id="preview-mtime", classes="preview-field"
                        )
                        yield Label(
                            "Status: N/A", id="preview-status", classes="preview-field"
                        )

                with Horizontal(classes="queue-row"):
                    btn_filter = Button(
                        "Filter Unsupported",
                        id="btn-filter-unsupported",
                        variant="warning",
                    )
                    btn_filter.tooltip = (
                        "Remove unsupported file formats from the queue"
                    )
                    yield btn_filter

                    btn_remove = Button(
                        "Remove Item", id="btn-remove-item", variant="error"
                    )
                    btn_remove.tooltip = (
                        "Remove the selected row from the staging table"
                    )
                    yield btn_remove

                    btn_up = Button("Move Up", id="btn-move-up", variant="default")
                    btn_up.tooltip = "Move selected item up in queue priority"
                    yield btn_up

                    btn_down = Button(
                        "Move Down", id="btn-move-down", variant="default"
                    )
                    btn_down.tooltip = "Move selected item down in queue priority"
                    yield btn_down

                    btn_clear = Button(
                        "Clear Queue", id="btn-clear-queue", variant="default"
                    )
                    btn_clear.tooltip = "Clear all staged items from the modal"
                    yield btn_clear

                status_lbl = Label(
                    "Ready. Drop or enter paths above.",
                    id="dropzone-status",
                    classes="status-text",
                )
                status_lbl.tooltip = (
                    "Live visual progress indicators and classification status messages"
                )
                yield status_lbl

            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel dropzone triage operation and close dialog"
                yield btn_cancel

                btn_help = Button(
                    "Help", id="btn-help", variant="warning", classes="hidden"
                )
                btn_help.tooltip = (
                    "Open troubleshooting documentation for dropzone errors"
                )
                yield btn_help

                btn_process = Button("Sort Items", id="btn-process", variant="primary")
                btn_process.tooltip = (
                    "Start automated classification and relocation on dropped paths"
                )
                yield btn_process

    def _format_size(self, size_bytes: int) -> str:
        """Format size in bytes to human readable string."""
        if size_bytes <= 0:
            return "0 B"
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(size_bytes)
        idx = 0
        while size >= 1024.0 and idx < len(units) - 1:
            size /= 1024.0
            idx += 1
        if idx == 0:
            return f"{int(size)} B"
        return f"{size:.1f} {units[idx]}"

    def get_filter_query(self) -> str:
        """Get lowercase trimmed search query from input-queue-filter field."""
        try:
            inp = self.query_one("#input-queue-filter", Input)
            return inp.value.strip().lower()
        except Exception:
            return ""

    def get_filtered_items(self) -> list[dict[str, Any]]:
        """Return list of staged items filtered by search query."""
        query = self.get_filter_query()
        if not query:
            return list(self.staged_items)
        return [
            item
            for item in self.staged_items
            if query in item["filename"].lower()
            or query in item["path"].lower()
            or query in item["extension"].lower()
            or query in item["status"].lower()
        ]

    def _refresh_table(self) -> None:
        """Re-render staging table with items matching active search filter."""
        try:
            table = self.query_one("#dropzone-staging-table", DataTable)
        except Exception:
            return
        table.clear(columns=True)
        table.add_columns("Status", "Filename", "Extension", "Size", "Full Path")
        filtered = self.get_filtered_items()
        for item in filtered:
            table.add_row(
                item["status"],
                item["filename"],
                item["extension"],
                item["size_str"],
                item["path"],
            )
        self.update_active_metadata_preview()

    def update_metadata_preview(self, item: Optional[dict[str, Any]] = None) -> None:
        """Update metadata preview card panel for given staged item."""
        if item is None:
            path_str = "N/A"
            size_str = "N/A"
            mtime_str = "N/A"
            status_str = "N/A"
        else:
            path_str = item.get("path", "N/A")
            size_str = item.get("size_str", "N/A")
            status_str = item.get("status", "N/A")
            if path_str and path_str != "N/A" and os.path.exists(path_str):
                try:
                    mtime = os.path.getmtime(path_str)
                    mtime_str = datetime.fromtimestamp(mtime).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                except Exception:
                    mtime_str = "Error reading timestamp"
            else:
                mtime_str = "N/A"

        try:
            self.query_one("#preview-path", Label).update(f"Path: {path_str}")
            self.query_one("#preview-size", Label).update(f"Size: {size_str}")
            self.query_one("#preview-mtime", Label).update(f"Modified: {mtime_str}")
            self.query_one("#preview-status", Label).update(f"Status: {status_str}")
        except Exception:
            pass

    def update_active_metadata_preview(self) -> None:
        """Fetch item corresponding to current table cursor row and update metadata preview."""
        try:
            table = self.query_one("#dropzone-staging-table", DataTable)
            row_idx = table.cursor_row
            filtered = self.get_filtered_items()
            if row_idx is not None and 0 <= row_idx < len(filtered):
                self.update_metadata_preview(filtered[row_idx])
            else:
                self.update_metadata_preview(None)
        except Exception:
            self.update_metadata_preview(None)

    @on(Input.Changed, "#input-queue-filter")
    def on_queue_filter_changed(self, event: Input.Changed) -> None:
        """Handle search filter text changes to dynamically update visible table rows."""
        self._refresh_table()

    @on(DataTable.RowHighlighted)
    @on(DataTable.RowSelected)
    @on(DataTable.CellHighlighted)
    def _on_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted
        | DataTable.RowSelected
        | DataTable.CellHighlighted,
    ) -> None:
        """Update metadata preview panel on table row or cell focus/selection."""
        self.update_active_metadata_preview()

    @staticmethod
    def _decode_path_token(token: str) -> str:
        """Strip outer quotes, decode file:// URIs, and expand user home."""
        cleaned = token.strip()
        if len(cleaned) >= 2 and cleaned[0] == cleaned[-1] and cleaned[0] in ("'", '"'):
            cleaned = cleaned[1:-1].strip()
        if cleaned.lower().startswith("file://"):
            rest = cleaned[7:]
            if rest.lower().startswith("localhost/"):
                raw_url_path = rest[9:]
            elif rest.startswith("/"):
                raw_url_path = rest
            else:
                raw_url_path = f"//{rest}"
            decoded = re.sub(
                r"(?:%[0-9a-fA-F]{2})+",
                lambda m: bytes.fromhex(m.group(0).replace("%", "")).decode(
                    "utf-8", errors="replace"
                ),
                raw_url_path,
            )
            if re.match(r"^/[a-zA-Z]:[/\\]", decoded):
                decoded = decoded[1:]
            if os.sep == "\\":
                decoded = decoded.replace("/", "\\")
            return os.path.expanduser(decoded)
        return os.path.expanduser(cleaned)

    @staticmethod
    def _looks_like_standalone_path(token: str) -> bool:
        """Return True when a shell token begins with an explicit path or URI prefix."""
        t = token.strip()
        return bool(
            t.startswith(("/", "~", "./", "../", ".\\", "..\\"))
            or t.lower().startswith("file://")
            or _WIN_DRIVE_RE.match(t)
        )

    def _parse_raw_drop_input(self, raw_text: str) -> list[str]:
        """Parse pasted or dropped text into individual filesystem path strings."""
        parsed_paths: list[str] = []
        for raw_line in raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = raw_line.strip()
            if not line:
                continue

            has_quotes = "'" in line or '"' in line
            has_escaped_space = "\\ " in line
            file_uri_count = line.lower().count("file://")

            whole_candidate = self._decode_path_token(line)
            if not has_quotes and file_uri_count <= 1:
                if os.path.exists(os.path.abspath(whole_candidate)):
                    parsed_paths.append(whole_candidate)
                    continue

            use_posix = not bool(re.search(r"""(?:^|\s|["'])[a-zA-Z]:\\""", line))
            try:
                tokens = shlex.split(line, posix=use_posix)
            except ValueError:
                tokens = [line]

            if not tokens:
                continue

            if len(tokens) == 1:
                decoded_single = self._decode_path_token(tokens[0])
                if decoded_single:
                    parsed_paths.append(decoded_single)
                continue

            decoded_tokens = [self._decode_path_token(tok) for tok in tokens]
            decoded_tokens = [tok for tok in decoded_tokens if tok]
            if not decoded_tokens:
                continue

            if (
                not has_quotes
                and not has_escaped_space
                and file_uri_count == 0
                and not all(self._looks_like_standalone_path(tok) for tok in tokens[1:])
                and not any(
                    os.path.exists(os.path.abspath(tok)) for tok in decoded_tokens
                )
            ):
                parsed_paths.append(whole_candidate)
            else:
                parsed_paths.extend(decoded_tokens)

        return parsed_paths

    def _is_supported_file(self, abs_p: str) -> bool:
        """Check whether the sorting pipeline can classify or route ``abs_p``.

        Accepts all non-ignored file extensions so uncategorized files route cleanly
        into ``Miscellaneous`` unless explicitly ignored or halted by policy.
        """
        from app.core.metadata import evaluate_static_file_routing
        from app.core.scanner import ScanFilter

        scan_filter = ScanFilter.from_settings(settings=self.settings)
        if scan_filter.has_ignored_extension(abs_p):
            return False

        _, _, halt_evaluation = evaluate_static_file_routing(
            abs_p,
            self.settings,
            check_extension_categories=True,
        )
        if halt_evaluation:
            return False
        return True

    def stage_paths(self, raw_text: str) -> None:
        """Parse raw input string, validate dropped paths before expansion, and stage items."""
        raw_items = self._parse_raw_drop_input(raw_text)
        if not raw_items:
            return

        from app.core.mover import is_subpath_or_equal
        from app.core.pipeline import (
            drop_scope,
            expand_drop_scope_files,
            validate_drop_paths,
        )

        protected = (
            getattr(self.settings, "PROTECTED_PATHS", []) if self.settings else []
        )
        new_count = 0

        for item in raw_items:
            abs_p = os.path.abspath(item)
            _, issues = validate_drop_paths([abs_p], settings=self.settings)
            issue = issues[0] if issues else None
            is_prot = (issue is not None and issue.reason == "protected") or any(
                prot and is_subpath_or_equal(abs_p, prot) for prot in (protected or ())
            )

            if is_prot or issue is not None:
                self._stage_rejected_item(abs_p, issue=issue, is_protected=is_prot)
                new_count += 1
                continue

            scope_dir, scope_files = drop_scope(abs_p)
            if scope_files is None:
                _, files = expand_drop_scope_files(abs_p, settings=self.settings)
                for f_path in files:
                    self._add_single_file_to_staging(
                        os.path.join(scope_dir, f_path), protected, root_dir=scope_dir
                    )
                    new_count += 1
            else:
                self._add_single_file_to_staging(abs_p, protected)
                new_count += 1

        self._refresh_table()
        try:
            inp = self.query_one("#input-drop-paths", Input)
            inp.value = ""
        except Exception:
            pass

        ready_cnt = sum(1 for item in self.staged_items if item["status"] == "Ready")
        msg = f"Staged {new_count} item(s). Queue total: {len(self.staged_items)} ({ready_cnt} ready)."
        self.update_status_msg(msg)

    def _stage_rejected_item(
        self,
        abs_p: str,
        *,
        issue: Any = None,
        is_protected: bool = False,
    ) -> None:
        """Stage a blocked, missing, or ignored dropped path without expanding internal files."""
        cleaned_abs = abs_p.rstrip("/\\") or abs_p
        filename = os.path.basename(cleaned_abs) or abs_p
        ext = os.path.splitext(cleaned_abs)[1].lower()
        group_root = os.path.dirname(cleaned_abs) or cleaned_abs
        detail = getattr(issue, "detail", None)
        reason = getattr(issue, "reason", None)

        block_reason: Optional[str] = None
        if is_protected or reason == "protected":
            status = "BLOCKED"
            block_reason = "protected"
        elif detail in ("app_bundle", "quarantine"):
            status = "BLOCKED"
            block_reason = str(detail)
        elif reason == "missing" or not os.path.exists(abs_p):
            status = "Not Found"
        else:
            status = "Unsupported"

        size_bytes = 0
        if os.path.exists(abs_p) and os.path.isfile(abs_p):
            try:
                size_bytes = os.path.getsize(abs_p)
            except OSError:
                size_bytes = 0
        size_str = self._format_size(size_bytes)

        existing = next(
            (item for item in self.staged_items if item["path"] == abs_p), None
        )
        if existing is None:
            entry: dict[str, Any] = {
                "path": abs_p,
                "filename": filename,
                "extension": ext,
                "size": size_bytes,
                "size_str": size_str,
                "status": status,
                "group_root": group_root,
            }
            if block_reason:
                entry["block_reason"] = block_reason
            self.staged_items.append(entry)

    def _add_single_file_to_staging(
        self,
        abs_p: str,
        protected: Optional[list[str]] = None,
        root_dir: Optional[str] = None,
    ) -> None:
        """Validate and append a single file path to staged_items."""
        from app.core.mover import is_subpath_or_equal
        from app.core.pipeline import validate_drop_paths

        _, issues = validate_drop_paths([abs_p], settings=self.settings)
        issue = issues[0] if issues else None
        issue_reason = issue.reason if issue else None
        issue_detail = issue.detail if issue else None
        is_prot = issue_reason == "protected" or any(
            prot and is_subpath_or_equal(abs_p, prot) for prot in (protected or ())
        )

        filename = os.path.basename(abs_p) or abs_p
        ext = os.path.splitext(abs_p)[1].lower()
        group_root = root_dir or os.path.dirname(abs_p)

        block_reason: Optional[str] = None
        if is_prot:
            status = "BLOCKED"
            block_reason = "protected"
            size_bytes = os.path.getsize(abs_p) if os.path.isfile(abs_p) else 0
        elif issue_detail in ("app_bundle", "quarantine"):
            status = "BLOCKED"
            block_reason = str(issue_detail)
            size_bytes = 0
        elif issue_reason == "missing" or not os.path.exists(abs_p):
            status = "Not Found"
            size_bytes = 0
        elif issue_reason == "ignored" or not self._is_supported_file(abs_p):
            status = "Unsupported"
            size_bytes = os.path.getsize(abs_p) if os.path.isfile(abs_p) else 0
        else:
            status = "Ready"
            size_bytes = os.path.getsize(abs_p)

        size_str = self._format_size(size_bytes)

        existing = next(
            (item for item in self.staged_items if item["path"] == abs_p), None
        )
        if existing is None:
            entry: dict[str, Any] = {
                "path": abs_p,
                "filename": filename,
                "extension": ext,
                "size": size_bytes,
                "size_str": size_str,
                "status": status,
                "group_root": group_root,
            }
            if block_reason:
                entry["block_reason"] = block_reason
            self.staged_items.append(entry)
        elif root_dir and is_subpath_or_equal(abs_p, root_dir):
            existing["group_root"] = root_dir

    def filter_unsupported_items(self) -> None:
        """Remove items with 'Unsupported' status from the queue."""
        before = len(self.staged_items)
        self.staged_items = [
            item for item in self.staged_items if item["status"] != "Unsupported"
        ]
        removed = before - len(self.staged_items)
        self._refresh_table()
        msg = f"Filtered {removed} unsupported item(s) from queue. {len(self.staged_items)} item(s) remaining."
        self.update_status_msg(msg)

    def remove_selected_item(self) -> None:
        """Remove currently selected row from the staging table."""
        try:
            table = self.query_one("#dropzone-staging-table", DataTable)
            row_idx = table.cursor_row
        except Exception:
            row_idx = None

        filtered = self.get_filtered_items()
        if row_idx is not None and 0 <= row_idx < len(filtered):
            removed_item = filtered[row_idx]
            if removed_item in self.staged_items:
                self.staged_items.remove(removed_item)
            self._refresh_table()
            msg = f"Removed '{removed_item['filename']}' from queue. {len(self.staged_items)} item(s) remaining."
            self.update_status_msg(msg)
            new_filtered = self.get_filtered_items()
            if new_filtered:
                new_idx = min(row_idx, len(new_filtered) - 1)
                try:
                    table.move_cursor(row=new_idx)
                except Exception:
                    pass
            self.update_active_metadata_preview()
        else:
            self.update_status_msg("No item selected to remove.")

    def move_selected_item_up(self) -> None:
        """Move selected queue item up in priority."""
        try:
            table = self.query_one("#dropzone-staging-table", DataTable)
            row_idx = table.cursor_row
        except Exception:
            row_idx = None

        filtered = self.get_filtered_items()
        if row_idx is not None and 0 < row_idx < len(filtered):
            item_curr = filtered[row_idx]
            item_prev = filtered[row_idx - 1]
            idx_curr = self.staged_items.index(item_curr)
            idx_prev = self.staged_items.index(item_prev)
            self.staged_items[idx_curr], self.staged_items[idx_prev] = (
                self.staged_items[idx_prev],
                self.staged_items[idx_curr],
            )
            self._refresh_table()
            try:
                table.move_cursor(row=row_idx - 1)
            except Exception:
                pass
            self.update_active_metadata_preview()
            self.update_status_msg(
                f"Moved '{item_curr['filename']}' up in queue priority."
            )

    def move_selected_item_down(self) -> None:
        """Move selected queue item down in priority."""
        try:
            table = self.query_one("#dropzone-staging-table", DataTable)
            row_idx = table.cursor_row
        except Exception:
            row_idx = None

        filtered = self.get_filtered_items()
        if row_idx is not None and 0 <= row_idx < len(filtered) - 1:
            item_curr = filtered[row_idx]
            item_next = filtered[row_idx + 1]
            idx_curr = self.staged_items.index(item_curr)
            idx_next = self.staged_items.index(item_next)
            self.staged_items[idx_curr], self.staged_items[idx_next] = (
                self.staged_items[idx_next],
                self.staged_items[idx_curr],
            )
            self._refresh_table()
            try:
                table.move_cursor(row=row_idx + 1)
            except Exception:
                pass
            self.update_active_metadata_preview()
            self.update_status_msg(
                f"Moved '{item_curr['filename']}' down in queue priority."
            )

    def clear_queue(self) -> None:
        """Clear all staged items from modal queue."""
        self.staged_items.clear()
        self._refresh_table()
        self.update_status_msg("Cleared all staged items from the queue.")

    def on_mount(self) -> None:
        """Focus input on mount, stage initial paths if any, and announce modal opening."""
        self._app_ref = self.app
        super().on_mount()
        self._refresh_table()
        try:
            self.query_one("#input-drop-paths", Input).focus()
        except Exception:
            pass
        self.announce("Opened floating dropzone overlay for quick file triage.")
        if self.initial_paths:
            self.stage_paths("\n".join(str(p) for p in self.initial_paths))

    def on_paste(self, event: events.Paste) -> None:
        """Handle clipboard paste event for dropped path payloads."""
        focused = getattr(self, "focused", None)
        if focused is not None and getattr(focused, "id", None) in (
            "input-dest-dir",
            "input-queue-filter",
        ):
            return
        if event.text:
            event.stop()
            try:
                inp = self.query_one("#input-drop-paths", Input)
                inp.value = event.text.strip()
            except Exception:
                pass
            self.stage_paths(event.text)

    @on(Button.Pressed, "#btn-browse-dest")
    def on_btn_browse_dest_pressed(self, event: Button.Pressed) -> None:
        """Open native directory picker to populate the destination folder input."""
        curr_val = ""
        try:
            curr_val = self.query_one("#input-dest-dir", Input).value.strip()
        except Exception:
            pass
        initial_dir = curr_val or self.base_dir
        self._browse_dest_worker(initial_dir)

    @work(thread=True)
    def _browse_dest_worker(self, initial_dir: Optional[str]) -> None:
        """Launch native folder selection dialog in a background thread."""
        from app.core.native_dialog import NativeDialogError, select_native_directory

        app_ref = getattr(self, "_app_ref", None)
        try:
            chosen = select_native_directory(initial_dir)
            if chosen:
                if app_ref:
                    app_ref.call_from_thread(self._on_dest_dir_selected, chosen)
                else:
                    self._on_dest_dir_selected(chosen)
        except NativeDialogError as exc:
            if app_ref:
                app_ref.call_from_thread(self._on_dest_dir_failed, str(exc))
            else:
                self._on_dest_dir_failed(str(exc))
        except Exception as exc:
            if app_ref:
                app_ref.call_from_thread(self._on_dest_dir_failed, str(exc))
            else:
                self._on_dest_dir_failed(str(exc))

    def _on_dest_dir_selected(self, path: str) -> None:
        """Set destination input from native folder browser selection."""
        abs_p = os.path.abspath(os.path.expanduser(path))
        try:
            inp = self.query_one("#input-dest-dir", Input)
            inp.value = abs_p
        except Exception:
            pass
        self.update_status_msg(f"Destination folder set to: {abs_p}")

    def _on_dest_dir_failed(self, reason: str) -> None:
        """Notify user and focus manual destination input when native dialog fails."""
        from app.ui.notifications import notify

        msg = "Native folder browser unavailable; enter destination path manually."
        notify(f"{msg} ({reason})", type="warning")
        self.update_status_msg(msg)
        try:
            self.query_one("#input-dest-dir", Input).focus()
        except Exception:
            pass

    @on(Input.Submitted, "#input-drop-paths")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle enter key in drop paths input field to stage items or process queue."""
        if event.value.strip():
            self.stage_paths(event.value)

    @on(Button.Pressed, "#btn-filter-unsupported")
    def on_btn_filter_unsupported_pressed(self, event: Button.Pressed) -> None:
        """Filter unsupported file formats button handler."""
        self.filter_unsupported_items()

    @on(Button.Pressed, "#btn-remove-item")
    def on_btn_remove_pressed(self, event: Button.Pressed) -> None:
        """Remove selected item button handler."""
        self.remove_selected_item()

    @on(Button.Pressed, "#btn-move-up")
    def on_btn_move_up_pressed(self, event: Button.Pressed) -> None:
        """Move item up button handler."""
        self.move_selected_item_up()

    @on(Button.Pressed, "#btn-move-down")
    def on_btn_move_down_pressed(self, event: Button.Pressed) -> None:
        """Move item down button handler."""
        self.move_selected_item_down()

    @on(Button.Pressed, "#btn-clear-queue")
    def on_btn_clear_pressed(self, event: Button.Pressed) -> None:
        """Clear queue button handler."""
        self.clear_queue()

    @on(Button.Pressed, "#btn-process")
    def on_btn_process_pressed(self, event: Button.Pressed) -> None:
        """Handle Sort Items button press."""
        self.trigger_drop_triage()

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Dismiss dropzone modal."""
        self._close_pending_drop()
        self.announce("Cancelled dropzone modal dialog.")
        self.dismiss(None)

    def trigger_drop_triage(self) -> None:
        """Parse input paths if any, validate active queue, and trigger worker."""
        try:
            inp = self.query_one("#input-drop-paths", Input)
            raw_text = inp.value.strip()
        except Exception:
            raw_text = ""

        if raw_text:
            self.stage_paths(raw_text)

        help_url = f"{TROUBLESHOOTING_URL}#dropzone-errors"

        raw_dest: Optional[str] = None
        try:
            dest_inp = self.query_one("#input-dest-dir", Input)
            raw_dest = dest_inp.value.strip()
        except Exception:
            raw_dest = (self.initial_dest_dir or "").strip()

        resolved_dest: Optional[str] = None
        if raw_dest:
            from app.core.pipeline import validate_drop_destination
            from app.ui.notifications import notify

            decoded_dest = self._decode_path_token(raw_dest)
            resolved_dest, dest_err = validate_drop_destination(
                decoded_dest, settings=self.settings
            )
            if dest_err in ("protected", "not_directory"):
                err_text = (
                    f"Protected destination blocked: {resolved_dest}"
                    if dest_err == "protected"
                    else f"Destination path is not a folder: {resolved_dest}"
                )
                self._show_help_button(help_url)
                notify(f"DropZone error: {err_text}", type="error", help_url=help_url)
                self.update_status_msg(err_text, help_url=help_url)
                return

        filtered = self.get_filtered_items()
        ready_paths = [item["path"] for item in filtered if item["status"] == "Ready"]

        if not ready_paths:
            blocked_items = [
                item for item in filtered if item["status"] == "BLOCKED"
            ] or [item for item in self.staged_items if item["status"] == "BLOCKED"]
            status_err_detail: Optional[str] = None
            if blocked_items:
                first_blk = blocked_items[0]
                blk_reason = first_blk.get("block_reason")
                if blk_reason == "app_bundle":
                    err_text = f"Application bundle blocked: {first_blk['filename']}"
                    status_err_detail = (
                        f"Application bundle blocked: {first_blk['path']}"
                    )
                elif blk_reason == "quarantine":
                    err_text = f"Quarantine folder blocked: {first_blk['filename']}"
                    status_err_detail = (
                        f"Quarantine folder blocked: {first_blk['path']}"
                    )
                else:
                    err_text = f"Protected path blocked: {first_blk['filename']}"
                    status_err_detail = f"Protected path blocked: {first_blk['path']}"
            elif self.staged_items:
                err_text = "No ready files to sort in queue."
            else:
                err_text = "No file or folder paths provided."

            self._show_help_button(help_url)
            from app.ui.notifications import notify

            notify(f"DropZone error: {err_text}", type="error", help_url=help_url)
            self.update_status_msg(
                status_err_detail if status_err_detail else f"Error: {err_text}",
                help_url=help_url,
            )
            return

        self.update_status_msg(f"Processing {len(ready_paths)} dropped item(s)...")
        self.run_drop_worker(ready_paths, dest_dir=resolved_dest)

    def update_status_msg(self, msg: str, help_url: Optional[str] = None) -> None:
        """Update live status message region and screen reader announcement."""
        display_msg = f"{msg} [Help: {help_url}]" if help_url else msg
        try:
            lbl = self.query_one("#dropzone-status", Label)
            lbl.update(display_msg)
        except Exception:
            pass
        self.announce(msg, help_url=help_url)

    def _close_pending_drop(self) -> None:
        """Close the sessions of a planned but unexecuted drop."""
        batch_plan = getattr(self, "_pending_batch_plan", None)
        coord = getattr(self, "_coordinator", None)
        if batch_plan is not None and coord is not None:
            try:
                coord.close_batch(batch_plan)
            except Exception:
                pass
        self._pending_batch_plan = None
        for item in getattr(self, "_pending_drop", []):
            try:
                item["session"].close()
            except Exception:
                pass
        self._pending_drop = []

    @work(exclusive=True, thread=True)
    def run_drop_worker(
        self, target_paths: list[str], dest_dir: Optional[str] = None
    ) -> None:
        """Plan the dropped items via DropBatchCoordinator in a worker thread, then ask before moving."""
        app_ref = getattr(self, "_app_ref", None)
        self._close_pending_drop()
        try:
            from app.core.pipeline import DropBatchCoordinator

            staged_roots = {
                item["path"]: item.get("group_root")
                for item in list(self.staged_items)
                if isinstance(item, dict) and "path" in item
            }
            coordinator = DropBatchCoordinator(self.settings)

            def _on_plan_progress(info: Any) -> None:
                if app_ref is None:
                    return
                stage = (
                    info.get("stage") if isinstance(info, dict) else None
                ) or "Classifying dropped items..."
                app_ref.call_from_thread(self.update_status_msg, str(stage))

            batch_plan = coordinator.plan_batch(
                target_paths,
                dest_dir=dest_dir,
                staged_roots=staged_roots,
                progress_callback=_on_plan_progress if app_ref else None,
            )
            self._coordinator = coordinator
            self._pending_batch_plan = batch_plan
            self._pending_drop = [
                {
                    "path": grp.source_root,
                    "session": grp.session,
                    "plan": grp.plan,
                    "file_count": len(grp.rel_files),
                    "re_rooted": grp.re_rooted,
                }
                for grp in batch_plan.groups
            ]
            if app_ref:
                app_ref.call_from_thread(self._confirm_pending_drop)
            else:
                self._confirm_pending_drop()
        except Exception as e:
            logger.exception(f"Error during dropzone classification: {e}")
            self._close_pending_drop()
            if app_ref:
                app_ref.call_from_thread(
                    self.update_status_msg, f"Dropzone triage error: {e}"
                )

    def _confirm_pending_drop(self) -> None:
        """Show what the drop would do, including collision warnings and overrides, and wait for go-ahead."""
        from app.core.domain_contracts import _make_json_serializable
        from app.core.plan_format import count_plan_actions, format_plan_tree
        from app.core.verifier import VerificationEngine
        from app.ui.modals.edit import DuplicateReviewModal

        batch_plan = getattr(self, "_pending_batch_plan", None)
        coordinator = getattr(self, "_coordinator", None)

        dup_mode = (
            str(getattr(self.settings, "DUPLICATE_ACTION", "off") or "off")
            .strip()
            .lower()
        )
        detect_original_by = str(
            getattr(self.settings, "DETECT_ORIGINAL_BY", "first_seen") or "first_seen"
        )

        if dup_mode == "review" and not getattr(
            self, "_drop_duplicates_reviewed", False
        ):
            self._pending_drop_flush_trash = False
            dup_groups: list[dict[str, Any]] = []
            plan_targets: list[tuple[str, dict[str, Any]]] = []
            if batch_plan is not None:
                if batch_plan.resolved_dest and batch_plan.unified_plan is not None:
                    plan_targets.append(
                        (batch_plan.resolved_dest, batch_plan.unified_plan)
                    )
                else:
                    for grp in batch_plan.groups:
                        plan_targets.append((grp.source_root, grp.plan))
            else:
                for item in self._pending_drop:
                    if isinstance(item.get("plan"), dict):
                        plan_targets.append((item["session"].base_dir, item["plan"]))

            seen_gids: set[str] = set()
            for p_base, p_dict in plan_targets:
                for dg in VerificationEngine.get_duplicate_groups(
                    p_base,
                    p_dict,
                    detect_original_by=detect_original_by,
                ):
                    gid = str(dg.get("group_id") or "")
                    if gid and gid not in seen_gids:
                        seen_gids.add(gid)
                        dup_groups.append(dg)

            if dup_groups:

                def _on_dup_review(selections: Optional[dict[str, str]]) -> None:
                    if selections is None:
                        self._drop_duplicates_reviewed = False
                        self._pending_drop_flush_trash = False
                        self._close_pending_drop()
                        self.update_status_msg(
                            "Cancelled duplicate review. No files were moved."
                        )
                        return

                    self._drop_duplicates_reviewed = True
                    self._pending_drop_flush_trash = any(
                        val == "trash" for val in selections.values()
                    )
                    for p_base, p_dict in plan_targets:
                        local_groups = VerificationEngine.get_duplicate_groups(
                            p_base,
                            p_dict,
                            detect_original_by=detect_original_by,
                        )
                        if local_groups:
                            VerificationEngine.apply_deduplication_selections(
                                p_base,
                                p_dict,
                                selections,
                                groups=local_groups,
                                detect_original_by=detect_original_by,
                            )
                    if batch_plan is not None and batch_plan.unified_plan is not None:
                        for grp in batch_plan.groups:
                            grp_groups = VerificationEngine.get_duplicate_groups(
                                batch_plan.resolved_dest or grp.source_root,
                                grp.plan,
                                detect_original_by=detect_original_by,
                            )
                            if grp_groups:
                                VerificationEngine.apply_deduplication_selections(
                                    batch_plan.resolved_dest or grp.source_root,
                                    grp.plan,
                                    selections,
                                    groups=grp_groups,
                                    detect_original_by=detect_original_by,
                                )
                    if batch_plan is not None and coordinator is not None:
                        coordinator.refresh_verification(batch_plan)
                    self._confirm_pending_drop()

                self.app.push_screen(
                    DuplicateReviewModal(duplicate_groups=dup_groups),
                    _on_dup_review,
                )
                return
        elif not getattr(self, "_drop_duplicates_reviewed", False):
            self._pending_drop_flush_trash = dup_mode == "trash"

        lines: list[str] = []
        total_files = total_folders = total_staged = total_trashed = 0
        for item in self._pending_drop:
            plan = _make_json_serializable(item["plan"])
            tree, n_files, n_folders = format_plan_tree(plan)
            p_counts = count_plan_actions(plan)
            item_staged = p_counts["staged"]
            item_trashed = p_counts["trashed"]
            if not (n_files or item_staged or item_trashed):
                continue
            total_files += n_files
            total_folders += n_folders
            total_staged += item_staged
            total_trashed += item_trashed
            lines.append(f"In {item['session'].base_dir}:")
            lines.extend(f"  {line}" for line in tree)

        if batch_plan is not None and batch_plan.plan_lines and not lines:
            lines = list(batch_plan.plan_lines)
            for it in batch_plan.items:
                if getattr(it, "is_excluded", False):
                    continue
                dup_act = str(it.node_ref.get("duplicate_action") or "").strip().lower()
                pol_act = str(it.node_ref.get("policy_action") or "").strip().lower()
                if dup_act == "trash" or pol_act == "trash":
                    total_trashed += 1
                elif dup_act == "stage":
                    total_staged += 1
                else:
                    total_files += 1
            total_folders = len(
                {
                    (it.dest_base_dir, it.target_folder)
                    for it in batch_plan.items
                    if not getattr(it, "is_excluded", False)
                    and str(it.node_ref.get("duplicate_action") or "").strip().lower()
                    not in ("stage", "trash")
                    and str(it.node_ref.get("policy_action") or "").strip().lower()
                    != "trash"
                }
            )

        if not (total_files or total_staged or total_trashed):
            self._drop_duplicates_reviewed = False
            self._pending_drop_flush_trash = False
            self._close_pending_drop()
            self.update_status_msg("Nothing to sort: no files could be classified.")
            return

        warnings = list(getattr(batch_plan, "warnings", None) or [])
        warn_suffix = f" ({len(warnings)} warning(s) detected)" if warnings else ""
        dup_parts: list[str] = []
        if total_staged > 0:
            dup_parts.append(f"{total_staged} staged")
        if total_trashed > 0:
            dup_parts.append(
                f"{total_trashed} trashed"
                if getattr(self, "_pending_drop_flush_trash", False)
                else f"{total_trashed} staged for trash"
            )
        dup_note = f" ({', '.join(dup_parts)})" if dup_parts else ""
        summary = (
            f"Move {total_files} file(s) into {total_folders} folder(s){dup_note}?{warn_suffix} "
            "Nothing has been moved yet."
        )
        self.update_status_msg(f"Review the plan: {summary}")

        def _on_answer(confirmed: Optional[bool]) -> None:
            self._drop_duplicates_reviewed = False
            if confirmed:
                self.update_status_msg(
                    "Relocating items according to generated sorting plan..."
                )
                self.run_drop_execute_worker()
            else:
                self._pending_drop_flush_trash = False
                self._close_pending_drop()
                self.update_status_msg("Cancelled. No files were moved.")

        self.app.push_screen(
            DropPlanConfirmModal(
                summary,
                lines,
                warnings=warnings,
                batch_plan=batch_plan,
                coordinator=coordinator,
            ),
            _on_answer,
        )

    @work(exclusive=True, thread=True)
    def run_drop_execute_worker(self) -> None:
        """Execute the confirmed drop batch under one undo session in a worker thread."""
        app_ref = getattr(self, "_app_ref", None)
        batch_plan = getattr(self, "_pending_batch_plan", None)
        coordinator = getattr(self, "_coordinator", None)
        flush_trash = bool(getattr(self, "_pending_drop_flush_trash", False))
        self._pending_drop_flush_trash = False
        self._drop_duplicates_reviewed = False

        if batch_plan is not None and coordinator is not None:
            self._pending_batch_plan = None
            self._pending_drop = []
            try:
                batch_result = coordinator.execute_batch(
                    batch_plan,
                    flush_trash=flush_trash,
                    flush_trashed_only=flush_trash,
                )
                active_items = [
                    it
                    for it in batch_plan.items
                    if not getattr(it, "is_excluded", False)
                ]
                handled_total = (
                    batch_result.moved_count
                    + batch_result.staged_count
                    + batch_result.trashed_count
                )
                succeeded_files = handled_total or (
                    len(active_items)
                    if batch_result.failed_count == 0
                    and batch_result.skipped_count == 0
                    else 0
                )
                failed_files = batch_result.failed_count
                processed_items = [
                    {
                        "path": tr.get("base_dir"),
                        "summary": tr.get("summary"),
                        "file_count": len(grp.rel_files),
                        "status": "success",
                    }
                    for grp, tr in zip(batch_plan.groups, batch_result.target_results)
                ]
                count_parts = [f"{batch_result.moved_count} moved"]
                if batch_result.staged_count > 0:
                    count_parts.append(f"{batch_result.staged_count} staged")
                if batch_result.trashed_count > 0:
                    count_parts.append(f"{batch_result.trashed_count} trashed")
                counts_detail = ", ".join(count_parts)
                trash_errors = dict(
                    (batch_result.trash_handoff or {}).get("errors") or {}
                )
                err_note = (
                    f" Warning: OS Trash handoff failed for {len(trash_errors)} item(s) (kept staged)."
                    if trash_errors
                    else ""
                )
                if failed_files == 0:
                    status = "success"
                    final_msg = (
                        f"Completed triage of {succeeded_files} dropped item(s) "
                        f"({counts_detail}).{err_note}"
                    )
                elif succeeded_files > 0:
                    status = "partial"
                    final_msg = (
                        f"Completed triage of {succeeded_files} item(s) "
                        f"({counts_detail}, {failed_files} failed).{err_note}"
                    )
                else:
                    status = "failed"
                    final_msg = f"Dropzone triage failed for {failed_files} item(s)."

                if app_ref:
                    app_ref.call_from_thread(self.update_status_msg, final_msg)
                    if trash_errors:
                        from app.ui.notifications import notify

                        app_ref.call_from_thread(
                            lambda: notify(err_note.strip(), type="warning")
                        )

                result_dict = {
                    "status": status,
                    "session_id": batch_result.session_id,
                    "destination_dir": batch_result.resolved_dest,
                    "processed_count": succeeded_files,
                    "moved_count": batch_result.moved_count,
                    "staged_count": batch_result.staged_count,
                    "trashed_count": batch_result.trashed_count,
                    "trash_handoff": batch_result.trash_handoff,
                    "failed_files": failed_files,
                    "items": processed_items,
                    "failed_items": [],
                    "succeeded_count": len(processed_items),
                    "failed_count": 1 if failed_files > 0 else 0,
                    "total_count": len(batch_plan.groups),
                }
                self.processed_result = result_dict
                try:
                    if app_ref:
                        app_ref.call_from_thread(self.dismiss, result_dict)
                    else:
                        self.dismiss(result_dict)
                except Exception:
                    pass
            except Exception as e:
                logger.exception(f"Error during dropzone relocation: {e}")
                coordinator.close_batch(batch_plan)
                if app_ref:
                    app_ref.call_from_thread(
                        self.update_status_msg, f"Dropzone triage error: {e}"
                    )
            return

        pending = list(getattr(self, "_pending_drop", []))
        self._pending_drop = []
        processed_items = []
        failed_items = []
        outcomes = []
        try:
            from app.core.pipeline import SortPipeline, restore_rerooted_source_dir

            for item in pending:
                try:
                    src_dir = item.get("path")
                    src_dir_was_dir = bool(src_dir and os.path.isdir(src_dir))
                    outcome = SortPipeline(item["session"]).execute(
                        item["plan"],
                        flush_trash=flush_trash,
                        flush_trashed_only=flush_trash,
                    )
                    restore_rerooted_source_dir(
                        src_dir,
                        was_dir=src_dir_was_dir,
                        re_rooted=bool(item.get("re_rooted")),
                    )
                    outcomes.append(outcome)
                    processed_items.append(
                        {
                            "path": item["path"],
                            "summary": outcome.summary,
                            "file_count": item.get("file_count", 1),
                            "status": "success",
                        }
                    )
                except Exception as e:
                    logger.exception(
                        f"Error during dropzone relocation for {item.get('path')}: {e}"
                    )
                    failed_items.append(
                        {
                            "path": item.get("path"),
                            "error": str(e),
                            "file_count": item.get("file_count", 1),
                            "status": "failed",
                        }
                    )
                finally:
                    try:
                        item["session"].close()
                    except Exception:
                        pass

            succeeded_files = sum(
                int(fc) if isinstance(fc := i.get("file_count"), (int, str)) else 0
                for i in processed_items
            )
            failed_files = sum(
                int(fc) if isinstance(fc := i.get("file_count"), (int, str)) else 0
                for i in failed_items
            )
            total_items = len(pending)

            fallback_trash_errors: dict[str, str] = {}
            for o in outcomes:
                th = (o.summary or {}).get("trash_handoff") if o.summary else None
                if isinstance(th, dict):
                    fallback_trash_errors.update(th.get("errors") or {})
            fb_err_note = (
                f" Warning: OS Trash handoff failed for {len(fallback_trash_errors)} item(s) (kept staged)."
                if fallback_trash_errors
                else ""
            )

            if not failed_items:
                status = "success"
                final_msg = (
                    f"Completed triage of {succeeded_files} dropped item(s): "
                    + " ".join(o.describe() for o in outcomes)
                    + fb_err_note
                )
            elif processed_items:
                status = "partial"
                err_summary = "; ".join(
                    f"{item['path']}: {item['error']}" for item in failed_items
                )
                descriptions = " ".join(o.describe() for o in outcomes)
                final_msg = (
                    f"Completed triage of {succeeded_files} item(s) "
                    f"({len(processed_items)} succeeded, {len(failed_items)} failed): "
                    f"{descriptions} Errors: {err_summary}{fb_err_note}"
                )
            else:
                status = "failed"
                err_summary = "; ".join(
                    f"{item['path']}: {item['error']}" for item in failed_items
                )
                final_msg = f"Dropzone triage failed for all {len(failed_items)} item(s): {err_summary}"

            if app_ref:
                app_ref.call_from_thread(self.update_status_msg, final_msg)
                if fallback_trash_errors:
                    from app.ui.notifications import notify

                    app_ref.call_from_thread(
                        lambda: notify(fb_err_note.strip(), type="warning")
                    )

            result_dict = {
                "status": status,
                "processed_count": succeeded_files,
                "failed_files": failed_files,
                "items": processed_items,
                "failed_items": failed_items,
                "succeeded_count": len(processed_items),
                "failed_count": len(failed_items),
                "total_count": total_items,
            }
            self.processed_result = result_dict
            try:
                if app_ref:
                    app_ref.call_from_thread(self.dismiss, result_dict)
                else:
                    self.dismiss(result_dict)
            except Exception:
                pass
        except Exception as e:
            logger.exception(f"Error during dropzone relocation: {e}")
            for item in pending:
                try:
                    item["session"].close()
                except Exception:
                    pass
            if app_ref:
                app_ref.call_from_thread(
                    self.update_status_msg, f"Dropzone triage error: {e}"
                )
