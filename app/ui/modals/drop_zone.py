"""Drop zone dialog for sorting dropped files, with its plan confirmation."""

import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin
from app.ui.notifications import TROUBLESHOOTING_URL

logger = logging.getLogger(__name__)


class DropPlanConfirmModal(A11yMixin, ModalScreen[bool]):
    """Show the DropZone plan and ask before any file is moved."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    DropPlanConfirmModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 90%;
        max-width: 100;
        min-width: 30;
        height: auto;
        max-height: 90%;
    }
    .modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #drop-plan-tree {
        height: auto;
        max-height: 20;
        overflow-y: auto;
        border: solid $secondary;
        padding: 0 1;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    """

    def __init__(self, summary: str, plan_lines: List[str]):
        super().__init__()
        self.summary = summary
        self.plan_lines = plan_lines

    def compose(self) -> ComposeResult:
        """Compose the plan preview with Move and Cancel buttons."""
        with Vertical(classes="modal-box"):
            yield Heading("Review DropZone Plan", level=1, classes="modal-title")
            yield Label(self.summary, id="lbl-drop-plan-summary")
            yield Static("\n".join(self.plan_lines), id="drop-plan-tree", markup=False)
            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel-drop-plan")
                btn_cancel.tooltip = "Close without moving any files"
                yield btn_cancel
                btn_move = Button(
                    "Move Files", id="btn-confirm-drop-plan", variant="success"
                )
                btn_move.tooltip = (
                    "Move the files as shown. You can undo it from History (Ctrl+Y)."
                )
                yield btn_move

    def on_mount(self) -> None:
        """Focus Cancel so Enter never moves files by accident."""
        self.query_one("#btn-cancel-drop-plan", Button).focus()

    @on(Button.Pressed, "#btn-confirm-drop-plan")
    def action_confirm(self) -> None:
        """Confirm the moves."""
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel-drop-plan")
    def action_cancel(self) -> None:
        """Cancel without moving anything."""
        self.dismiss(False)


class DropZoneModal(
    ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[Dict[str, Any]]]
):
    """Floating overlay modal dialog for quick drag-and-drop or paste file triage."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    SUPPORTED_EXTENSIONS = {".txt", ".docx", ".csv", ".xlsx", ".xls", ".pdf"}

    CSS = """
    DropZoneModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: double $accent;
        width: 90%;
        max-width: 100;
        min-width: 50;
        height: auto;
        max-height: 95%;
        overflow-y: auto;
    }
    .narrow .modal-box {
        padding: 0 1;
        width: 95%;
    }
    .modal-title {
        text-style: bold;
        color: $accent;
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
    .dropzone-split-container {
        height: auto;
        min-height: 8;
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
        min-height: 5;
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
        min-height: 5;
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
        height: 3;
        align: right middle;
    }
    .queue-row {
        margin-top: 0;
        height: 3;
        align: center middle;
    }
    Button:focus, Input:focus, DataTable:focus {
        border: heavy $accent;
        text-style: bold;
    }
    Button.hidden {
        display: none;
    }
    """

    def __init__(self, settings: Any = None, base_dir: Optional[str] = None):
        super().__init__()
        self.settings = settings
        self.base_dir = base_dir
        self.processed_result: Optional[Dict[str, Any]] = None
        self.staged_items: List[Dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        """Compose floating dropzone modal overlay controls."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Floating DropZone - Quick File Triage",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
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
            inp.tooltip = (
                "Enter or drop target file or folder paths to trigger automated sorting"
            )
            yield inp

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
                    yield Label("Path: N/A", id="preview-path", classes="preview-field")
                    yield Label("Size: N/A", id="preview-size", classes="preview-field")
                    yield Label(
                        "Modified: N/A", id="preview-mtime", classes="preview-field"
                    )
                    yield Label(
                        "Status: N/A", id="preview-status", classes="preview-field"
                    )

            with Horizontal(classes="queue-row"):
                btn_filter = Button(
                    "Filter Unsupported", id="btn-filter-unsupported", variant="warning"
                )
                btn_filter.tooltip = "Remove unsupported file formats from the queue"
                yield btn_filter

                btn_remove = Button(
                    "Remove Item", id="btn-remove-item", variant="error"
                )
                btn_remove.tooltip = "Remove the selected row from the staging table"
                yield btn_remove

                btn_up = Button("Move Up", id="btn-move-up", variant="default")
                btn_up.tooltip = "Move selected item up in queue priority"
                yield btn_up

                btn_down = Button("Move Down", id="btn-move-down", variant="default")
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

    def get_filtered_items(self) -> List[Dict[str, Any]]:
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

    def update_metadata_preview(self, item: Optional[Dict[str, Any]] = None) -> None:
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
    def _on_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        """Update metadata preview panel on table row focus/highlight."""
        self.update_active_metadata_preview()

    @on(DataTable.RowSelected)
    def _on_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Update metadata preview panel on table row selection."""
        self.update_active_metadata_preview()

    @on(DataTable.CellHighlighted)
    def _on_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        """Update metadata preview panel on cell selection/focus."""
        self.update_active_metadata_preview()

    def stage_paths(self, raw_text: str) -> None:
        """Parse raw input string, expand directories, validate, and stage items."""
        raw_items = [
            p.strip().strip("'\"")
            for p in raw_text.replace("\r", "\n").split("\n")
            if p.strip()
        ]
        if not raw_items:
            return

        from app.core.scanner import get_files_recursively

        protected = (
            getattr(self.settings, "PROTECTED_PATHS", []) if self.settings else []
        )
        new_count = 0

        for item in raw_items:
            abs_p = os.path.abspath(item)
            if os.path.isdir(abs_p):
                # The scanner returns paths relative to the folder it walked.
                files = get_files_recursively(abs_p)
                for f_path in files:
                    self._add_single_file_to_staging(
                        os.path.join(abs_p, f_path), protected
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

    def _add_single_file_to_staging(self, abs_p: str, protected: List[str]) -> None:
        """Validate and append a single file path to staged_items."""
        from app.core.mover import is_subpath_or_equal

        is_prot = False
        for prot in protected:
            if prot and is_subpath_or_equal(abs_p, prot):
                is_prot = True
                break

        filename = os.path.basename(abs_p) or abs_p
        ext = os.path.splitext(abs_p)[1].lower()

        if is_prot:
            status = "BLOCKED"
            size_bytes = os.path.getsize(abs_p) if os.path.exists(abs_p) else 0
        elif not os.path.exists(abs_p):
            status = "Not Found"
            size_bytes = 0
        elif ext not in self.SUPPORTED_EXTENSIONS:
            status = "Unsupported"
            size_bytes = os.path.getsize(abs_p) if os.path.exists(abs_p) else 0
        else:
            status = "Ready"
            size_bytes = os.path.getsize(abs_p)

        size_str = self._format_size(size_bytes)

        if not any(item["path"] == abs_p for item in self.staged_items):
            self.staged_items.append(
                {
                    "path": abs_p,
                    "filename": filename,
                    "extension": ext,
                    "size": size_bytes,
                    "size_str": size_str,
                    "status": status,
                }
            )

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
        """Focus input on mount, render staging table, and announce modal opening."""
        self._app_ref = self.app
        self._update_layout(self.size.width)
        self._refresh_table()
        try:
            self.query_one("#input-drop-paths", Input).focus()
        except Exception:
            pass
        self.announce("Opened floating dropzone overlay for quick file triage.")

    def on_paste(self, event: events.Paste) -> None:
        """Handle clipboard paste event for dropped path payloads."""
        if event.text:
            try:
                inp = self.query_one("#input-drop-paths", Input)
                inp.value = event.text.strip()
            except Exception:
                pass
            self.stage_paths(event.text)

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

    @on(Button.Pressed, "#btn-help")
    def action_help(self) -> None:
        """Open troubleshooting documentation link for dropzone."""
        url = getattr(
            self,
            "help_url",
            f"{TROUBLESHOOTING_URL}#dropzone-errors",
        )
        from app.ui.notifications import notify

        notify(f"Opening help link: {url}", type="info", help_url=url)
        self.announce(f"Opened documentation link: {url}")

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

        filtered = self.get_filtered_items()
        ready_paths = [item["path"] for item in filtered if item["status"] == "Ready"]

        if not ready_paths:
            blocked_items = [
                item for item in filtered if item["status"] == "BLOCKED"
            ] or [item for item in self.staged_items if item["status"] == "BLOCKED"]
            if blocked_items:
                err_text = f"Protected path blocked: {blocked_items[0]['filename']}"
            elif self.staged_items:
                err_text = "No ready files to sort in queue."
            else:
                err_text = "No file or folder paths provided."

            self._show_help_button(help_url)
            from app.ui.notifications import notify

            notify(f"DropZone error: {err_text}", type="error", help_url=help_url)
            self.update_status_msg(
                f"Protected path blocked: {blocked_items[0]['path']}"
                if blocked_items
                else f"Error: {err_text}",
                help_url=help_url,
            )
            return

        self.update_status_msg(f"Processing {len(ready_paths)} dropped item(s)...")
        self.run_drop_worker(ready_paths)

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
        for item in getattr(self, "_pending_drop", []):
            try:
                item["session"].close()
            except Exception:
                pass
        self._pending_drop = []

    @work(exclusive=True, thread=True)
    def run_drop_worker(self, target_paths: List[str]) -> None:
        """Plan the dropped items in a worker thread, then ask before moving."""
        app_ref = getattr(self, "_app_ref", None)
        self._close_pending_drop()
        pending: List[Dict[str, Any]] = []
        try:
            from app.core.pipeline import SortPipeline
            from app.core.scanner import get_files_recursively
            from app.core.session import AppSession

            # Plan every staged file in a folder together, so a dropped folder
            # is clustered as a whole instead of one file at a time.
            groups: Dict[str, List[str]] = {}
            for path in target_paths:
                if os.path.isdir(path):
                    groups.setdefault(path, []).extend(get_files_recursively(path))
                else:
                    groups.setdefault(os.path.dirname(path), []).append(
                        os.path.basename(path)
                    )

            for base_dir, files in groups.items():
                if app_ref:
                    app_ref.call_from_thread(
                        self.update_status_msg,
                        f"Classifying {len(files)} file(s) in '{os.path.basename(base_dir)}'...",
                    )

                session = AppSession(self.settings, base_dir=base_dir)
                pending.append(
                    {
                        "path": base_dir,
                        "session": session,
                        "plan": None,
                        "file_count": len(files),
                    }
                )
                pending[-1]["plan"] = SortPipeline(session).plan(files)

            self._pending_drop = pending
            if app_ref:
                app_ref.call_from_thread(self._confirm_pending_drop)
            else:
                self._confirm_pending_drop()
        except Exception as e:
            logger.exception(f"Error during dropzone classification: {e}")
            self._pending_drop = pending
            self._close_pending_drop()
            if app_ref:
                app_ref.call_from_thread(
                    self.update_status_msg, f"Dropzone triage error: {e}"
                )

    def _confirm_pending_drop(self) -> None:
        """Show what the drop would do and wait for the user's go-ahead."""
        from app.core.domain_contracts import _make_json_serializable
        from app.core.plan_format import format_plan_tree

        lines: List[str] = []
        total_files = total_folders = 0
        for item in self._pending_drop:
            plan = _make_json_serializable(item["plan"])
            tree, n_files, n_folders = format_plan_tree(plan)
            if not n_files:
                continue
            total_files += n_files
            total_folders += n_folders
            lines.append(f"In {item['session'].base_dir}:")
            lines.extend(f"  {line}" for line in tree)

        if not total_files:
            self._close_pending_drop()
            self.update_status_msg("Nothing to sort: no files could be classified.")
            return

        summary = (
            f"Move {total_files} file(s) into {total_folders} folder(s)? "
            "Nothing has been moved yet."
        )
        self.update_status_msg(f"Review the plan: {summary}")

        def _on_answer(confirmed: Optional[bool]) -> None:
            if confirmed:
                self.update_status_msg(
                    "Relocating items according to generated sorting plan..."
                )
                self.run_drop_execute_worker()
            else:
                self._close_pending_drop()
                self.update_status_msg("Cancelled. No files were moved.")

        self.app.push_screen(DropPlanConfirmModal(summary, lines), _on_answer)

    @work(exclusive=True, thread=True)
    def run_drop_execute_worker(self) -> None:
        """Execute the confirmed drop plans in a worker thread."""
        app_ref = getattr(self, "_app_ref", None)
        pending = list(getattr(self, "_pending_drop", []))
        self._pending_drop = []
        processed_items = []
        outcomes = []
        try:
            from app.core.pipeline import SortPipeline

            for item in pending:
                try:
                    outcome = SortPipeline(item["session"]).execute(item["plan"])
                    outcomes.append(outcome)
                    processed_items.append(
                        {
                            "path": item["path"],
                            "summary": outcome.summary,
                            "file_count": item.get("file_count", 1),
                        }
                    )
                finally:
                    item["session"].close()

            file_count = sum(i["file_count"] for i in processed_items)
            final_msg = (
                f"Completed triage of {file_count} dropped item(s): "
                + " ".join(o.describe() for o in outcomes)
            )
            if app_ref:
                app_ref.call_from_thread(self.update_status_msg, final_msg)
            result_dict = {
                "status": "success",
                "processed_count": file_count,
                "items": processed_items,
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
