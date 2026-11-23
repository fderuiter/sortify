"""Drop zone dialog for sorting dropped files, with its plan confirmation."""

import logging
import os
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Static

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ModalBox, ResponsiveLayoutMixin
from app.ui.notifications import TROUBLESHOOTING_URL

logger = logging.getLogger(__name__)

_WIN_DRIVE_RE = re.compile(r"^[a-zA-Z]:[/\\]")


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
        overflow-y: hidden;
    }
    .modal-body {
        width: 100%;
        height: auto;
        max-height: 100%;
        overflow-y: auto;
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
        dock: bottom;
        width: 100%;
        margin-top: 1;
        height: auto;
        align: right middle;
    }
    """

    def __init__(self, summary: str, plan_lines: list[str]):
        super().__init__()
        self.summary = summary
        self.plan_lines = plan_lines

    def compose(self) -> ComposeResult:
        """Compose the plan preview with Move and Cancel buttons."""
        with ModalBox(classes="modal-box"):
            yield Heading("Review DropZone Plan", level=1, classes="modal-title")
            with Vertical(classes="modal-body"):
                yield Label(self.summary, id="lbl-drop-plan-summary")
                yield Static(
                    "\n".join(self.plan_lines), id="drop-plan-tree", markup=False
                )
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
    ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[dict[str, Any]]]
):
    """Floating overlay modal dialog for quick drag-and-drop or paste file triage."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    DropZoneModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: double $accent;
        width: 92%;
        max-width: 100;
        min-width: 40;
        height: auto;
        max-height: 95%;
        overflow-y: hidden;
    }
    .compact .modal-box, .narrow .modal-box {
        padding: 0 1;
        width: 98%;
    }
    .modal-body {
        width: 100%;
        height: auto;
        max-height: 100%;
        overflow-y: auto;
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
        dock: bottom;
        width: 100%;
        margin-top: 0;
        height: auto;
        align: right middle;
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
    Button.hidden {
        display: none;
    }
    """

    def __init__(
        self,
        settings: Any = None,
        base_dir: Optional[str] = None,
        initial_paths: Optional[list[str]] = None,
    ):
        super().__init__()
        self.settings = settings
        self.base_dir = base_dir
        self.initial_paths: list[str] = list(initial_paths) if initial_paths else []
        self.processed_result: Optional[dict[str, Any]] = None
        self.staged_items: list[dict[str, Any]] = []

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
                        value=self.base_dir or "",
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
        """Check whether the sorting pipeline can classify or route ``abs_p``."""
        from app.core.extractor_strategies import registry
        from app.core.jev_classifier import JevClassifierEngine
        from app.core.metadata import get_extension_category_target
        from app.core.policy_engine import match_pattern_expression

        ext = os.path.splitext(abs_p)[1].lower()
        ignored_raw = (
            getattr(self.settings, "IGNORED_EXTENSIONS", []) if self.settings else []
        ) or []
        ignored_exts = {
            s if s.startswith(".") else f".{s}"
            for s in (str(x).strip().lower() for x in ignored_raw)
            if s
        }
        if ext and ext in ignored_exts:
            return False

        if ext and registry.is_supported(ext):
            return True

        jev_exts = {
            s if s.startswith(".") else f".{s}"
            for s in (
                str(x).strip().lower()
                for x in JevClassifierEngine.get_supported_extensions()
            )
            if s
        }
        if ext and ext in jev_exts:
            return True

        if get_extension_category_target(abs_p, self.settings) is not None:
            return True

        filename_only = os.path.basename(abs_p).lower()
        file_path_lower = abs_p.lower()
        policies = (
            getattr(self.settings, "POLICIES", []) if self.settings else []
        ) or []
        if isinstance(policies, list):
            sorted_policies = sorted(
                (p for p in policies if isinstance(p, dict)),
                key=lambda x: x.get("priority", 0),
                reverse=True,
            )
            for policy in sorted_policies:
                if not policy.get("enabled", True):
                    continue
                p_type = str(policy.get("type", "")).lower()
                expr_lower = str(policy.get("expression", "")).lower()
                target_path = policy.get("target_path")
                is_match = False
                if p_type == "override" and expr_lower:
                    is_match = (
                        expr_lower == filename_only
                        or expr_lower in filename_only
                        or expr_lower in file_path_lower
                    )
                elif p_type == "pattern" and expr_lower:
                    is_match = match_pattern_expression(expr_lower, filename_only)
                elif p_type == "unarchive" and expr_lower:
                    is_match = match_pattern_expression(expr_lower, filename_only)
                elif p_type == "keyword" and expr_lower:
                    is_match = expr_lower in filename_only
                if is_match and target_path:
                    return True
                if not is_match and policy.get("halting", False):
                    return False

        for attr_name in ("KEYWORD_RULES", "LEARNED_RULES"):
            rules = (
                getattr(self.settings, attr_name, {}) if self.settings else {}
            ) or {}
            if isinstance(rules, dict):
                for keyword, target_folder in rules.items():
                    if (
                        str(keyword).strip()
                        and str(keyword).lower() in filename_only
                        and target_folder
                    ):
                        return True

        return False

    def stage_paths(self, raw_text: str) -> None:
        """Parse raw input string, expand directories, validate, and stage items."""
        raw_items = self._parse_raw_drop_input(raw_text)
        if not raw_items:
            return

        from app.core.scanner import ScanFilter, get_files_recursively

        protected = (
            getattr(self.settings, "PROTECTED_PATHS", []) if self.settings else []
        )
        new_count = 0

        for item in raw_items:
            abs_p = os.path.abspath(item)
            if os.path.isdir(abs_p):
                # The scanner returns paths relative to the folder it walked.
                drop_filter = ScanFilter.from_settings(
                    settings=self.settings, base_dir=abs_p
                )
                drop_filter.max_depth = None
                files = get_files_recursively(
                    abs_p, settings=self.settings, scan_filter=drop_filter
                )
                for f_path in files:
                    self._add_single_file_to_staging(
                        os.path.join(abs_p, f_path), protected, root_dir=abs_p
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

    def _add_single_file_to_staging(
        self, abs_p: str, protected: list[str], root_dir: Optional[str] = None
    ) -> None:
        """Validate and append a single file path to staged_items."""
        from app.core.mover import is_subpath_or_equal

        is_prot = False
        for prot in protected:
            if prot and is_subpath_or_equal(abs_p, prot):
                is_prot = True
                break

        filename = os.path.basename(abs_p) or abs_p
        ext = os.path.splitext(abs_p)[1].lower()
        group_root = root_dir or os.path.dirname(abs_p)

        if is_prot:
            status = "BLOCKED"
            size_bytes = os.path.getsize(abs_p) if os.path.exists(abs_p) else 0
        elif not os.path.exists(abs_p):
            status = "Not Found"
            size_bytes = 0
        elif not self._is_supported_file(abs_p):
            status = "Unsupported"
            size_bytes = os.path.getsize(abs_p) if os.path.exists(abs_p) else 0
        else:
            status = "Ready"
            size_bytes = os.path.getsize(abs_p)

        size_str = self._format_size(size_bytes)

        existing = next(
            (item for item in self.staged_items if item["path"] == abs_p), None
        )
        if existing is None:
            self.staged_items.append(
                {
                    "path": abs_p,
                    "filename": filename,
                    "extension": ext,
                    "size": size_bytes,
                    "size_str": size_str,
                    "status": status,
                    "group_root": group_root,
                }
            )
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
        self._update_layout(self.size.width)
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

        raw_dest: Optional[str] = None
        try:
            dest_inp = self.query_one("#input-dest-dir", Input)
            raw_dest = dest_inp.value.strip()
        except Exception:
            raw_dest = (self.base_dir or "").strip()

        resolved_dest: Optional[str] = None
        if raw_dest:
            from app.core.mover import is_subpath_or_equal
            from app.ui.notifications import notify

            decoded_dest = self._decode_path_token(raw_dest)
            resolved_dest = str(Path(decoded_dest).expanduser().resolve())
            protected = (
                getattr(self.settings, "PROTECTED_PATHS", []) if self.settings else []
            ) or []
            if any(
                prot and is_subpath_or_equal(resolved_dest, prot) for prot in protected
            ):
                err_text = f"Protected destination blocked: {resolved_dest}"
                self._show_help_button(help_url)
                notify(f"DropZone error: {err_text}", type="error", help_url=help_url)
                self.update_status_msg(err_text, help_url=help_url)
                return
            if os.path.exists(resolved_dest) and not os.path.isdir(resolved_dest):
                err_text = f"Destination path is not a folder: {resolved_dest}"
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
        """Plan the dropped items in a worker thread, then ask before moving."""
        app_ref = getattr(self, "_app_ref", None)
        self._close_pending_drop()
        pending: list[dict[str, Any]] = []
        try:
            from app.core.path_utils import safe_relpath
            from app.core.pipeline import SortPipeline, reroot_plan
            from app.core.scanner import ScanFilter, get_files_recursively
            from app.core.session import AppSession

            resolved_dest = (
                str(Path(dest_dir).expanduser().resolve()) if dest_dir else None
            )

            # Plan every staged file under its dropped root folder together so a
            # dropped folder (including nested subfolders) stays in one session.
            staged_roots = {
                item["path"]: item.get("group_root")
                for item in list(self.staged_items)
                if isinstance(item, dict) and "path" in item
            }
            groups: dict[str, list[str]] = {}
            for path in target_paths:
                if os.path.isdir(path):
                    drop_filter = ScanFilter.from_settings(
                        settings=self.settings, base_dir=path
                    )
                    drop_filter.max_depth = None
                    groups.setdefault(path, []).extend(
                        get_files_recursively(
                            path, settings=self.settings, scan_filter=drop_filter
                        )
                    )
                else:
                    group_root = staged_roots.get(path) or os.path.dirname(path)
                    rel_item = safe_relpath(path, group_root)
                    groups.setdefault(group_root, []).append(rel_item)

            for base_dir, files in groups.items():
                if app_ref:
                    app_ref.call_from_thread(
                        self.update_status_msg,
                        f"Classifying {len(files)} file(s) in '{os.path.basename(base_dir)}'...",
                    )

                session = AppSession(self.settings, base_dir=base_dir)
                re_rooted = bool(
                    resolved_dest
                    and os.path.normcase(os.path.abspath(resolved_dest))
                    != os.path.normcase(os.path.abspath(base_dir))
                )
                pending.append(
                    {
                        "path": base_dir,
                        "session": session,
                        "plan": None,
                        "file_count": len(files),
                        "re_rooted": re_rooted,
                    }
                )
                plan = SortPipeline(session).plan(files, dest_dir=resolved_dest)
                if re_rooted and resolved_dest is not None:
                    plan = reroot_plan(plan, resolved_dest, source_dir=base_dir)
                    session.base_dir = resolved_dest
                pending[-1]["plan"] = plan

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

        lines: list[str] = []
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
        failed_items = []
        outcomes = []
        try:
            from app.core.pipeline import SortPipeline

            for item in pending:
                try:
                    src_dir = item.get("path")
                    src_dir_was_dir = bool(src_dir and os.path.isdir(src_dir))
                    outcome = SortPipeline(item["session"]).execute(item["plan"])
                    if (
                        item.get("re_rooted")
                        and src_dir
                        and src_dir_was_dir
                        and not os.path.exists(src_dir)
                    ):
                        os.makedirs(src_dir, exist_ok=True)
                    outcomes.append(outcome)
                    outcome_failed = getattr(outcome, "failed", None)
                    if isinstance(outcome_failed, (list, tuple)) and outcome_failed:
                        err_detail = "; ".join(
                            f"{src}: {reason}" for src, reason in outcome_failed
                        )
                        if outcome.moved or outcome.quarantined or outcome.skipped:
                            processed_items.append(
                                {
                                    "path": item["path"],
                                    "summary": outcome.summary,
                                    "file_count": len(outcome.moved)
                                    + len(outcome.quarantined)
                                    + len(outcome.skipped),
                                    "status": "partial",
                                }
                            )
                        failed_items.append(
                            {
                                "path": item.get("path"),
                                "error": err_detail,
                                "file_count": len(outcome_failed),
                                "status": "failed",
                            }
                        )
                    else:
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

            if not failed_items:
                status = "success"
                final_msg = (
                    f"Completed triage of {succeeded_files} dropped item(s): "
                    + " ".join(o.describe() for o in outcomes)
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
                    f"{descriptions} Errors: {err_summary}"
                )
            else:
                status = "failed"
                err_summary = "; ".join(
                    f"{item['path']}: {item['error']}" for item in failed_items
                )
                final_msg = f"Dropzone triage failed for all {len(failed_items)} item(s): {err_summary}"

            if app_ref:
                app_ref.call_from_thread(self.update_status_msg, final_msg)

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
