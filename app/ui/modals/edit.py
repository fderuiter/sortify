"""Dialogs for editing the plan tree: rename, new folder, reassign, directory pickers and duplicate review."""

import logging
import os
from typing import Any, Dict, List, Optional, Union

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin

logger = logging.getLogger(__name__)


class RenameModal(ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[str]]):
    """Modal dialog for renaming a file or folder node with pattern token formatting support."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    RenameModal {
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
    .modal-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    .token-help {
        color: $text-muted;
        margin-bottom: 1;
    }
    .modal-preview {
        color: $accent;
        margin-top: 1;
        margin-bottom: 1;
        text-style: bold;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus, Input:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(
        self,
        title: str,
        current_name: str = "",
        extension: str = "",
        category: str = "",
        file_date: str = "",
        seq: int = 1,
    ):
        super().__init__()
        self.modal_title = title
        self.current_name = current_name
        self.extension = extension
        self.category = category
        self.file_date = file_date
        self.seq = seq

    def _evaluate_preview(self, pattern_str: str) -> str:
        """Evaluate formatted pattern string for live preview."""
        from app.core.pattern_formatter import PatternTokenFormatter

        metadata = {
            "original": self.current_name,
            "extension": self.extension,
            "category": self.category,
            "date": self.file_date,
            "seq": self.seq,
        }
        fallback = (
            f"{self.current_name}{self.extension}"
            if self.extension
            else self.current_name
        )
        return PatternTokenFormatter.format_pattern(
            pattern=pattern_str,
            metadata=metadata,
            seq=self.seq,
            fallback_original=fallback,
        )

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Heading(
                self.modal_title,
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            if self.extension:
                yield Label(
                    f"Extension '{self.extension}' is locked unless modified in template",
                    classes="modal-subtitle",
                )
            yield Label(
                "Tokens: {date}, {category}, {original}, {extension}, {seq}",
                classes="token-help",
            )
            inp = Input(
                value=self.current_name,
                placeholder="Enter new name or pattern (e.g. {date}_{category}_{original})...",
                id="input-name",
            )
            inp.tooltip = "Enter new file or folder name or pattern template"
            yield inp
            yield Label(
                f"Preview: {self._evaluate_preview(self.current_name)}",
                id="label-preview",
                classes="modal-preview",
            )
            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel rename action and close dialog"
                yield btn_cancel
                btn_confirm = Button("Rename", id="btn-confirm", variant="primary")
                btn_confirm.tooltip = "Confirm renaming action"
                yield btn_confirm

    def on_mount(self) -> None:
        """Focus input field on mount and emit screen reader announcement."""
        self._update_layout(self.size.width)
        self.query_one("#input-name", Input).focus()
        self.announce(
            f"Opened rename dialog for '{self.modal_title}'. Enter new name or pattern and press Enter or click Rename."
        )

    @on(Input.Changed, "#input-name")
    def on_input_changed(self, event: Input.Changed) -> None:
        """Update live preview on input value change."""
        try:
            preview_str = self._evaluate_preview(event.value)
            lbl = self.query_one("#label-preview", Label)
            lbl.update(f"Preview: {preview_str}")
        except Exception:
            pass

    @on(Button.Pressed, "#btn-confirm")
    def action_confirm(self) -> None:
        """Confirm renaming action."""
        val = self.query_one("#input-name", Input).value.strip()
        formatted = self._evaluate_preview(val) if val else ""
        if formatted:
            self.announce(f"Confirmed rename to '{formatted}'.")
        else:
            self.announce("Cancelled rename action.")
        self.dismiss(formatted if formatted else None)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel renaming action."""
        self.announce("Cancelled rename action.")
        self.dismiss(None)

    @on(Input.Submitted)
    def action_submit(self) -> None:
        """Submit input on Enter key press."""
        self.action_confirm()


class NewFolderModal(ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[str]]):
    """Modal dialog for creating a new folder node in the plan."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    NewFolderModal {
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
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus, Input:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Create New Target Folder",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            inp = Input(
                placeholder="e.g. Financials, Contracts, Invoices",
                id="input-folder-name",
            )
            inp.tooltip = "Enter new target folder category name"
            yield inp
            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel folder creation and close dialog"
                yield btn_cancel
                btn_confirm = Button("Create", id="btn-confirm", variant="primary")
                btn_confirm.tooltip = "Confirm new folder category creation"
                yield btn_confirm

    def on_mount(self) -> None:
        """Focus input field on mount and emit screen reader announcement."""
        self._update_layout(self.size.width)
        self.query_one("#input-folder-name", Input).focus()
        self.announce("Opened create new target folder dialog.")

    @on(Button.Pressed, "#btn-confirm")
    def action_confirm(self) -> None:
        """Confirm folder creation action."""
        val = self.query_one("#input-folder-name", Input).value.strip()
        if val:
            self.announce(f"Confirmed folder creation for '{val}'.")
        else:
            self.announce("Cancelled folder creation.")
        self.dismiss(val if val else None)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel folder creation action."""
        self.announce("Cancelled folder creation.")
        self.dismiss(None)

    @on(Input.Submitted)
    def action_submit(self) -> None:
        """Submit input on Enter key press."""
        self.action_confirm()


class ReassignModal(ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[str]]):
    """Modal dialog for reassigning checked file nodes to a target category folder."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    ReassignModal {
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

    def __init__(self, existing_folders: List[str], count: int = 1):
        super().__init__()
        self.existing_folders = [f for f in existing_folders if f]
        self.count = count

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Heading(
                f"Reassign {self.count} Selected File(s)",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            yield Heading("Select Existing Target Folder:", level=2)
            options = [(f, f) for f in sorted(self.existing_folders)]
            if options:
                yield Select(options, prompt="Choose folder...", id="select-folder")
            yield Heading("Or Enter New Target Category Folder Name:", level=2)
            inp = Input(
                placeholder="e.g. Drafts, Financials, Archive",
                id="input-target-folder",
            )
            inp.tooltip = "Enter target folder category name"
            yield inp
            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel reassign dialog"
                yield btn_cancel
                btn_confirm = Button("Reassign", id="btn-confirm", variant="primary")
                btn_confirm.tooltip = "Confirm bulk reassignment"
                yield btn_confirm

    def on_mount(self) -> None:
        """Focus input field on mount and emit announcement."""
        self._update_layout(self.size.width)
        self.query_one("#input-target-folder", Input).focus()
        self.announce(f"Opened bulk reassign dialog for {self.count} items.")

    @on(Button.Pressed, "#btn-confirm")
    def action_confirm(self) -> None:
        """Confirm reassign action."""
        val = self.query_one("#input-target-folder", Input).value.strip()
        if not val:
            try:
                sel = self.query_one("#select-folder", Select)
                if sel.value is not Select.BLANK and sel.value:
                    val = str(sel.value)
            except Exception:
                pass
        if val:
            self.announce(f"Reassigning to target folder '{val}'.")
        else:
            self.announce("Cancelled bulk reassign.")
        self.dismiss(val if val else None)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel reassign action."""
        self.announce("Cancelled bulk reassign.")
        self.dismiss(None)

    @on(Input.Submitted)
    def action_submit(self) -> None:
        """Submit reassign on Enter key press."""
        self.action_confirm()


class DirectorySelectModal(
    ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[Union[str, List[str]]]]
):
    """Modal dialog for choosing target base directory or presets."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    DirectorySelectModal {
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
    .error-banner {
        color: $error;
        text-style: bold;
    }
    .preset-row {
        margin-top: 1;
        margin-bottom: 1;
        height: auto;
        min-height: 3;
    }
    .sources-list {
        margin-top: 1;
        margin-bottom: 1;
        color: $text;
        border: solid $primary;
        padding: 0 1;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus, Input:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, current_dir: str = "", source_dirs: Optional[List[str]] = None):
        super().__init__()
        self.current_dir = current_dir
        self.added_via_btn = False
        if source_dirs:
            self.selected_sources = [
                d if str(d).startswith("/") else os.path.abspath(d)
                for d in source_dirs
                if d
            ]
        elif current_dir:
            self.selected_sources = [
                current_dir
                if str(current_dir).startswith("/")
                else os.path.abspath(current_dir)
            ]
        else:
            self.selected_sources = []

    @staticmethod
    def _validate_directory_path(path_str: str) -> Optional[str]:
        """Validate if path_str exists, is a directory, and allows read access.

        Returns error message string if invalid, or None if valid.
        """
        if not path_str or not path_str.strip():
            return "Directory path cannot be empty."

        clean_path = path_str.strip()
        expanded = os.path.abspath(os.path.expanduser(clean_path))

        try:
            if not os.path.exists(expanded):
                return f"Target directory does not exist: {clean_path}"

            if not os.path.isdir(expanded):
                return f"Path is not a directory: {clean_path}"

            if not os.access(expanded, os.R_OK):
                return f"Permission denied for directory: {clean_path}"
        except Exception as exc:
            return f"Invalid directory path: {exc}"

        return None

    def _show_error(self, message: str) -> None:
        """Display inline error banner, announce message, and focus input."""
        try:
            lbl = self.query_one("#lbl-error", Static)
            lbl.update(message)
        except Exception:
            pass
        self.announce(message, priority="assertive")
        try:
            self.query_one("#input-dir", Input).focus()
        except Exception:
            pass

    @on(Input.Changed, "#input-dir")
    def on_input_changed(self, event: Input.Changed) -> None:
        """Clear error banner when user changes input text."""
        try:
            lbl = self.query_one("#lbl-error", Static)
            lbl.update("")
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Select Target Directory",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            inp = Input(
                value=self.current_dir,
                placeholder="Enter absolute directory path...",
                id="input-dir",
            )
            inp.tooltip = "Enter absolute target directory path to scan"
            yield inp
            yield Static("", id="lbl-error", classes="error-banner")
            yield Label("Selected Sources:", id="lbl-sources")
            yield Static(
                self._format_sources_display(),
                id="static-sources-list",
                classes="sources-list",
            )
            yield Label("Quick Presets:")
            with Horizontal(classes="preset-row"):
                p_demo = Button("Demo Workspace", id="preset-demo", variant="default")
                p_demo.tooltip = "Select sandbox demo workspace quick preset"
                yield p_demo
                p_dl = Button("Downloads", id="preset-downloads", variant="default")
                p_dl.tooltip = "Select user Downloads folder quick preset"
                yield p_dl
                p_docs = Button("Documents", id="preset-documents", variant="default")
                p_docs.tooltip = "Select user Documents folder quick preset"
                yield p_docs
            with Horizontal(classes="button-row"):
                btn_browse = Button("Native Browse", id="btn-browse", variant="default")
                btn_browse.tooltip = "Launch native OS directory browser dialog"
                yield btn_browse
                btn_add = Button("Add Path", id="btn-add-dir", variant="default")
                btn_add.tooltip = "Add current path to multi-root source list"
                yield btn_add
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel directory selection and close dialog"
                yield btn_cancel
                btn_confirm = Button("Select", id="btn-confirm", variant="primary")
                btn_confirm.tooltip = "Confirm directory selection"
                yield btn_confirm

    def _format_sources_display(self) -> str:
        if not self.selected_sources:
            return "(No source directories selected)"
        return "\n".join(f"• {d}" for d in self.selected_sources)

    def _update_sources_widget(self) -> None:
        try:
            st = self.query_one("#static-sources-list", Static)
            st.update(self._format_sources_display())
        except Exception:
            pass

    def on_mount(self) -> None:
        """Focus input field on mount and emit screen reader announcement."""
        self._update_layout(self.size.width)
        self.query_one("#input-dir", Input).focus()
        self.announce("Opened target directory selection dialog.")

    @on(Button.Pressed, "#btn-add-dir")
    def action_add_dir(self) -> None:
        """Add input directory path to selected source list."""
        val = self.query_one("#input-dir", Input).value.strip()
        if not val:
            return
        err = self._validate_directory_path(val)
        if err:
            self._show_error(err)
            return

        abs_p = os.path.abspath(os.path.expanduser(val))
        if abs_p not in self.selected_sources:
            self.selected_sources.append(abs_p)
            self.announce(f"Added directory path: {abs_p}")
            self._update_sources_widget()
        self.added_via_btn = True
        try:
            self.query_one("#lbl-error", Static).update("")
        except Exception:
            pass

    @on(Button.Pressed, "#preset-demo")
    def action_demo(self) -> None:
        """Select sandbox demo workspace preset."""
        from app.core.sample_corpus import generate_sample_corpus
        from app.ui.notifications import notify

        path = os.path.abspath("sandbox/demo_workspace")
        if not os.path.exists(path):
            try:
                os.makedirs(path, exist_ok=True)
                created_msg = f"Created demo workspace directory: {path}"
                self.announce(created_msg)
                notify(created_msg, type="info")
            except Exception as exc:
                err_msg = f"Failed to create directory '{path}': {exc}"
                self.announce(err_msg, priority="assertive")
                notify(err_msg, type="error")
                return

        try:
            generate_sample_corpus(path, overwrite=True)
            sample_msg = f"Generated sample corpus documents in '{path}'."
            self.announce(sample_msg)
            notify(sample_msg, type="info")
        except Exception as exc:
            logger.error(f"Failed to generate sample corpus in '{path}': {exc}")
            err_msg = f"Failed to generate sample corpus: {exc}"
            self.announce(err_msg, priority="assertive")
            notify(err_msg, type="error")

        self.announce(f"Selected demo workspace preset: {path}")
        self.dismiss(path)

    @on(Button.Pressed, "#preset-downloads")
    def action_downloads(self) -> None:
        """Select user Downloads directory preset."""
        path = os.path.expanduser("~/Downloads")
        err = self._validate_directory_path(path)
        if err:
            self._show_error(err)
            return
        self.announce(f"Selected Downloads preset: {path}")
        self.dismiss(path)

    @on(Button.Pressed, "#preset-documents")
    def action_documents(self) -> None:
        """Select user Documents directory preset."""
        path = os.path.expanduser("~/Documents")
        err = self._validate_directory_path(path)
        if err:
            self._show_error(err)
            return
        self.announce(f"Selected Documents preset: {path}")
        self.dismiss(path)

    @on(Button.Pressed, "#btn-confirm")
    def action_confirm(self) -> None:
        """Confirm directory selection."""
        val = self.query_one("#input-dir", Input).value.strip()
        if val:
            err = self._validate_directory_path(val)
            if err:
                self._show_error(err)
                return

            abs_p = os.path.abspath(os.path.expanduser(val))
            if not self.added_via_btn and len(self.selected_sources) <= 1:
                self.announce(f"Confirmed directory selection: '{abs_p}'")
                self.dismiss(abs_p)
                return

            if abs_p not in self.selected_sources:
                self.selected_sources.append(abs_p)

        if self.selected_sources:
            for source in self.selected_sources:
                err = self._validate_directory_path(source)
                if err:
                    self._show_error(err)
                    return

            self.announce(f"Confirmed directory selection: '{self.selected_sources}'")
            res = (
                self.selected_sources
                if len(self.selected_sources) > 1
                else self.selected_sources[0]
            )
            self.dismiss(res)
        else:
            self.announce("Cancelled directory selection.")
            self.dismiss(None)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel directory selection."""
        self.announce("Cancelled directory selection.")
        self.dismiss(None)

    @on(Input.Submitted)
    def action_submit(self) -> None:
        """Submit input on Enter key press."""
        self.action_confirm()

    @on(Button.Pressed, "#btn-browse")
    @work(thread=True)
    def action_native_browse(self) -> None:
        """Launch native OS directory browser dialog in background worker thread."""
        from app.core.native_dialog import NativeDialogError, select_native_directory

        curr_val = ""
        try:
            inp = self.query_one("#input-dir", Input)
            curr_val = inp.value.strip()
        except Exception:
            pass

        initial_dir = curr_val or self.current_dir

        try:
            chosen = select_native_directory(initial_dir)
            if chosen:
                self.app.call_from_thread(self._on_native_dir_selected, chosen)
        except NativeDialogError as exc:
            self.app.call_from_thread(self._on_native_dir_failed, str(exc))
        except Exception as exc:
            self.app.call_from_thread(self._on_native_dir_failed, str(exc))

    def _on_native_dir_selected(self, path: str) -> None:
        """Handle decoded UTF-8 path string from native folder browser."""
        abs_p = os.path.abspath(os.path.expanduser(path))
        try:
            inp = self.query_one("#input-dir", Input)
            inp.value = abs_p
        except Exception:
            pass
        if abs_p not in self.selected_sources:
            self.selected_sources.append(abs_p)
            self._update_sources_widget()
        self.announce(f"Selected native folder path: {abs_p}")

    def _on_native_dir_failed(self, reason: str) -> None:
        """Handle native folder dialog failure by falling back to manual path entry."""
        from app.ui.notifications import notify

        msg = "Native folder browser failed to launch. Please enter path manually."
        self.announce(msg, priority="assertive")
        notify(msg, type="warning")
        try:
            inp = self.query_one("#input-dir", Input)
            inp.focus()
        except Exception:
            pass


class ManualPathFallbackModal(A11yMixin, ModalScreen[Optional[str]]):
    """Fallback modal dialog for manual directory path entry when native selection fails."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    ManualPathFallbackModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $warning;
        width: 80%;
        max-width: 70;
        height: auto;
    }
    """

    def __init__(self, current_dir: str = "", reason: str = ""):
        super().__init__()
        self.current_dir = current_dir
        self.reason = reason

    def compose(self) -> ComposeResult:
        """Compose modal fallback UI children."""
        with Vertical(classes="modal-box"):
            yield Label("Manual Directory Entry", classes="modal-title")
            if self.reason:
                yield Label(f"Native tool notice: {self.reason}", id="lbl-reason")
            yield Label("Please enter target directory path manually:")
            inp = Input(
                value=self.current_dir,
                placeholder="Enter absolute folder path...",
                id="input-fallback-dir",
            )
            yield inp
            with Horizontal(classes="button-row"):
                btn_cancel = Button(
                    "Cancel", id="btn-cancel-fallback", variant="default"
                )
                yield btn_cancel
                btn_confirm = Button(
                    "Confirm", id="btn-confirm-fallback", variant="primary"
                )
                yield btn_confirm

    def on_mount(self) -> None:
        """Focus input field on mount and announce screen reader alert."""
        self.query_one("#input-fallback-dir", Input).focus()
        self.announce("Opened manual directory entry fallback dialog.")

    @on(Button.Pressed, "#btn-confirm-fallback")
    @on(Input.Submitted, "#input-fallback-dir")
    def action_confirm(self) -> None:
        """Confirm manual path entry."""
        val = self.query_one("#input-fallback-dir", Input).value.strip()
        if val:
            abs_p = os.path.abspath(os.path.expanduser(val))
            self.announce(f"Confirmed manual directory entry: '{abs_p}'")
            self.dismiss(abs_p)
        else:
            self.announce("Cancelled manual directory entry.")
            self.dismiss(None)

    @on(Button.Pressed, "#btn-cancel-fallback")
    def action_cancel(self) -> None:
        """Cancel manual path entry."""
        self.announce("Cancelled manual directory entry.")
        self.dismiss(None)


def _format_file_size(size_bytes: int) -> str:
    """Format size in bytes to human readable string."""
    if size_bytes <= 0:
        return "0 B"
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} TB"


class InteractiveCollisionResolverModal(
    ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[Dict[str, str]]]
):
    """Interactive per-file collision resolution modal before execution."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    InteractiveCollisionResolverModal, DuplicateReviewModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.65);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 90%;
        max-width: 90;
        min-width: 40;
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
    .modal-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    .batch-bar {
        margin-top: 1;
        margin-bottom: 1;
        height: 3;
        align: left middle;
    }
    .batch-select {
        width: 1fr;
        margin-right: 1;
    }
    #btn-apply-all {
        min-width: 16;
    }
    .narrow .batch-bar {
        height: auto;
    }
    .duplicate-card {
        background: $surface;
        border: solid $accent;
        padding: 1;
        margin-bottom: 1;
    }
    .card-title {
        text-style: bold;
        color: $accent;
    }
    .card-subtitle {
        color: $text-muted;
    }
    .source-item {
        color: $text;
        margin-left: 2;
    }
    .action-select {
        margin-top: 1;
        width: 100%;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus, Select:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    RESOLVER_OPTIONS = [
        ("Rename (auto-number duplicates)", "rename"),
        ("Overwrite (replace target file)", "overwrite"),
        ("Skip (exclude colliding file)", "skip"),
        ("Keep Both (preserve destination & prefix source)", "keep_both"),
        ("Keep Primary File Only", "keep"),
    ]

    def __init__(
        self,
        duplicate_groups: Optional[List[Dict[str, Any]]] = None,
    ):
        super().__init__()
        self.duplicate_groups = duplicate_groups or []
        self.user_selections: Dict[str, str] = {
            g["group_id"]: g.get("default_action", "rename")
            for g in self.duplicate_groups
        }

    def compose(self) -> ComposeResult:
        """Compose interactive collision resolution dialog widgets."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Interactive Collision Resolver",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            yield Label(
                "Review destination target collisions and select per-file resolution actions:",
                classes="modal-subtitle",
            )

            with Horizontal(classes="batch-bar", id="batch-action-bar"):
                batch_sel = Select(
                    self.RESOLVER_OPTIONS,
                    value="rename",
                    id="batch-action-select",
                    classes="batch-select",
                    allow_blank=False,
                )
                batch_sel.tooltip = (
                    "Select batch deduplication action to apply to all collision groups"
                )
                batch_sel.aria_label = (
                    "Batch resolution action for all duplicate clusters"
                )
                yield batch_sel

                btn_apply = Button("Apply to All", id="btn-apply-all")
                btn_apply.tooltip = (
                    "Apply selected resolution action to all duplicate clusters"
                )
                btn_apply.aria_label = (
                    "Apply selected resolution action to all duplicate clusters"
                )
                yield btn_apply

            with Vertical(id="duplicate-groups-list"):
                for group in self.duplicate_groups:
                    gid = group["group_id"]
                    tf_name = group.get("target_filename", "Collision")
                    t_dir = group.get("target_dir", "")
                    items = group.get("items", [])
                    default_action = group.get("default_action", "rename")

                    with Vertical(classes="duplicate-card", id=f"card-{gid}"):
                        yield Label(
                            f"Destination Target: {tf_name} ({t_dir})",
                            classes="card-title",
                        )
                        yield Label(
                            f"Colliding source files ({len(items)}):",
                            classes="card-subtitle",
                        )
                        for item in items:
                            rel_src = item.get("relative_source") or item.get(
                                "source_path", ""
                            )
                            src_p = item.get("source_path", "")
                            size_info = ""
                            if src_p and os.path.exists(src_p):
                                try:
                                    sz = os.path.getsize(src_p)
                                    size_info = f" ({_format_file_size(sz)})"
                                except OSError:
                                    pass
                            yield Label(
                                f"• {rel_src}{size_info}", classes="source-item"
                            )

                        sel = Select(
                            self.RESOLVER_OPTIONS,
                            value=default_action,
                            id=f"select-action-{gid}",
                            classes="action-select",
                            allow_blank=False,
                        )
                        sel.tooltip = (
                            f"Select collision resolution action for {tf_name}"
                        )
                        sel.aria_label = (
                            f"Resolution action for duplicate group {tf_name}"
                        )
                        yield sel

            with Horizontal(classes="button-row"):
                yield Button(
                    "Apply & Execute Moves",
                    variant="primary",
                    id="btn-confirm-duplicates",
                )
                yield Button("Cancel", id="btn-cancel-duplicates")

    def _update_layout(self, width: int) -> None:
        """Update responsiveness based on modal container width."""
        if width < 60:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_mount(self) -> None:
        """Focus initial widget on mount and announce to screen reader."""
        self._update_layout(self.size.width)
        try:
            self.query_one("#batch-action-select", Select).focus()
        except Exception:
            if self.duplicate_groups:
                first_gid = self.duplicate_groups[0]["group_id"]
                try:
                    self.query_one(f"#select-action-{first_gid}", Select).focus()
                except Exception:
                    try:
                        self.query_one("#btn-confirm-duplicates", Button).focus()
                    except Exception:
                        pass
            else:
                try:
                    self.query_one("#btn-confirm-duplicates", Button).focus()
                except Exception:
                    pass
        self.announce("Opened interactive collision resolver modal.")

    @on(Button.Pressed, "#btn-apply-all")
    def action_apply_to_all(self) -> None:
        """Apply the selected batch action to all collision groups."""
        try:
            batch_sel = self.query_one("#batch-action-select", Select)
            batch_value = (
                str(batch_sel.value) if batch_sel.value is not None else "rename"
            )
        except Exception:
            batch_value = "rename"

        for group in self.duplicate_groups:
            gid = group["group_id"]
            self.user_selections[gid] = batch_value
            try:
                sel_widget = self.query_one(f"#select-action-{gid}", Select)
                sel_widget.value = batch_value
            except Exception:
                pass

        self.announce(f"Applied '{batch_value}' action to all duplicate groups.")

    @on(Select.Changed)
    def on_select_changed(self, event: Select.Changed) -> None:
        """Record user selection updates for duplicate groups."""
        if event.select.id and event.select.id.startswith("select-action-"):
            gid = event.select.id.replace("select-action-", "")
            if event.value is not None:
                self.user_selections[gid] = str(event.value)

    @on(Button.Pressed, "#btn-confirm-duplicates")
    def action_confirm(self) -> None:
        """Confirm deduplication selections and dismiss dialog."""
        self.announce("Applied duplicate resolution selections.")
        self.dismiss(self.user_selections)

    @on(Button.Pressed, "#btn-cancel-duplicates")
    def action_cancel(self) -> None:
        """Cancel duplicate review and abort execution."""
        self.announce("Cancelled duplicate review dialog.")
        self.dismiss(None)


DuplicateReviewModal = InteractiveCollisionResolverModal
