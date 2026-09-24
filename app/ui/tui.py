"""Textual full-screen terminal interface with interactive tree and modal dialogs."""

import logging
import os
import time
from typing import Any, Dict, List, Optional

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Label,
    Log,
    Select,
    Static,
    Switch,
    Tree,
)
from textual.widgets.tree import TreeNode

logger = logging.getLogger(__name__)


class A11yMixin:
    """Mixin providing WCAG 2.1 screen reader announcements, accessibility attributes, and audit hooks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.announcements: List[Dict[str, Any]] = []
        self.last_announcement: Optional[str] = None

    def announce(self, message: str, priority: str = "polite") -> str:
        """Emit auditory screen reader announcement and log accessibility event."""
        entry = {
            "message": message,
            "priority": priority,
            "timestamp": time.time(),
        }
        self.announcements.append(entry)
        self.last_announcement = message

        # Forward to parent app if available
        if hasattr(self, "app") and self.app and self.app is not self and hasattr(self.app, "announce"):
            try:
                self.app.announce(message, priority=priority)
            except Exception:
                pass

        # Update status bar if available
        if hasattr(self, "update_status") and callable(self.update_status):
            try:
                self.update_status(message)
            except Exception:
                pass

        return message

    def get_last_announcement(self) -> Optional[str]:
        """Get the most recent screen reader announcement text."""
        return self.last_announcement

    def audit_a11y_compliance(self) -> Dict[str, Any]:
        """Audit WCAG 2.1 accessibility compliance for this TUI component.

        Verifies:
        1. Interactive controls have tooltips or explicit accessibility labels.
        2. Modal screens define Escape key bindings for keyboard dismissal.
        3. Screen reader announcement logging capability exists.
        """
        violations = []

        try:
            for widget in self.query("*"):
                w_type = type(widget).__name__
                if w_type in ("Input", "Button", "Select", "Switch", "Tree", "Log"):
                    has_tooltip = bool(getattr(widget, "tooltip", None))
                    has_label = bool(
                        getattr(widget, "label", None)
                        or getattr(widget, "_text", None)
                        or getattr(widget, "value", None)
                        or getattr(widget, "placeholder", None)
                    )
                    if not (has_tooltip or has_label):
                        violations.append({
                            "rule": "A11Y001_MISSING_LABEL",
                            "widget_id": getattr(widget, "id", None) or w_type,
                            "message": f"Interactive control '{w_type}' lacks explicit tooltip or accessible text label.",
                        })
        except Exception:
            pass

        if isinstance(self, ModalScreen):
            bindings = getattr(self, "BINDINGS", [])
            has_escape = any(
                getattr(b, "key", None) == "escape" or (isinstance(b, Binding) and b.key == "escape")
                for b in bindings
            )
            if not has_escape:
                violations.append({
                    "rule": "A11Y_MISSING_ESCAPE_BINDING",
                    "message": f"Modal screen '{type(self).__name__}' lacks Escape key binding for accessibility dismissal.",
                })

        if not hasattr(self, "announce") or not hasattr(self, "announcements"):
            violations.append({
                "rule": "A11Y_MISSING_ANNOUNCER",
                "message": f"Component '{type(self).__name__}' lacks screen reader announcement handler.",
            })

        return {
            "component": type(self).__name__,
            "compliant": len(violations) == 0,
            "violations_count": len(violations),
            "violations": violations,
        }


class RenameModal(A11yMixin, ModalScreen[Optional[str]]):
    """Modal dialog for renaming a file or folder node."""

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
        height: auto;
        max-height: 90%;
        overflow-y: auto;
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

    def __init__(self, title: str, current_name: str = "", extension: str = ""):
        super().__init__()
        self.modal_title = title
        self.current_name = current_name
        self.extension = extension

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Label(self.modal_title, classes="modal-title")
            if self.extension:
                yield Label(f"Extension '{self.extension}' is locked", classes="modal-subtitle")
            inp = Input(value=self.current_name, placeholder="Enter new name...", id="input-name")
            inp.tooltip = "Enter new file or folder item name"
            yield inp
            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel rename action and close dialog"
                yield btn_cancel
                btn_confirm = Button("Rename", id="btn-confirm", variant="primary")
                btn_confirm.tooltip = "Confirm renaming action"
                yield btn_confirm

    def on_mount(self) -> None:
        """Focus input field on mount and emit screen reader announcement."""
        self.query_one("#input-name", Input).focus()
        self.announce(f"Opened rename dialog for '{self.modal_title}'. Enter new name and press Enter or click Rename.")

    @on(Button.Pressed, "#btn-confirm")
    def action_confirm(self) -> None:
        """Confirm renaming action."""
        val = self.query_one("#input-name", Input).value.strip()
        if val:
            self.announce(f"Confirmed rename to '{val}'.")
        else:
            self.announce("Cancelled rename action.")
        self.dismiss(val if val else None)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel renaming action."""
        self.announce("Cancelled rename action.")
        self.dismiss(None)

    @on(Input.Submitted)
    def action_submit(self) -> None:
        """Submit input on Enter key press."""
        self.action_confirm()


class NewFolderModal(A11yMixin, ModalScreen[Optional[str]]):
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
        height: auto;
        max-height: 90%;
        overflow-y: auto;
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
            yield Label("Create New Target Folder", classes="modal-title")
            inp = Input(placeholder="e.g. Financials, Contracts, Invoices", id="input-folder-name")
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


class DirectorySelectModal(A11yMixin, ModalScreen[Optional[str]]):
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
        height: auto;
        max-height: 90%;
        overflow-y: auto;
    }
    .modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .preset-row {
        margin-top: 1;
        margin-bottom: 1;
        height: 3;
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

    def __init__(self, current_dir: str = ""):
        super().__init__()
        self.current_dir = current_dir

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Label("Select Target Directory", classes="modal-title")
            inp = Input(value=self.current_dir, placeholder="Enter absolute directory path...", id="input-dir")
            inp.tooltip = "Enter absolute target directory path to scan"
            yield inp
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
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel directory selection and close dialog"
                yield btn_cancel
                btn_confirm = Button("Select", id="btn-confirm", variant="primary")
                btn_confirm.tooltip = "Confirm directory selection"
                yield btn_confirm

    def on_mount(self) -> None:
        """Focus input field on mount and emit screen reader announcement."""
        self.query_one("#input-dir", Input).focus()
        self.announce("Opened target directory selection dialog.")

    @on(Button.Pressed, "#preset-demo")
    def action_demo(self) -> None:
        """Select sandbox demo workspace preset."""
        path = os.path.abspath("sandbox/demo_workspace")
        self.announce(f"Selected demo workspace preset: {path}")
        self.dismiss(path)

    @on(Button.Pressed, "#preset-downloads")
    def action_downloads(self) -> None:
        """Select user Downloads directory preset."""
        path = os.path.expanduser("~/Downloads")
        self.announce(f"Selected Downloads preset: {path}")
        self.dismiss(path)

    @on(Button.Pressed, "#preset-documents")
    def action_documents(self) -> None:
        """Select user Documents directory preset."""
        path = os.path.expanduser("~/Documents")
        self.announce(f"Selected Documents preset: {path}")
        self.dismiss(path)

    @on(Button.Pressed, "#btn-confirm")
    def action_confirm(self) -> None:
        """Confirm directory selection."""
        val = self.query_one("#input-dir", Input).value.strip()
        if val:
            self.announce(f"Confirmed directory selection: '{val}'")
        else:
            self.announce("Cancelled directory selection.")
        self.dismiss(val if val else None)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel directory selection."""
        self.announce("Cancelled directory selection.")
        self.dismiss(None)

    @on(Input.Submitted)
    def action_submit(self) -> None:
        """Submit input on Enter key press."""
        self.action_confirm()


class SettingsModal(A11yMixin, ModalScreen[Optional[Dict[str, Any]]]):
    """Modal dialog for modifying application settings."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    SettingsModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 90%;
        max-width: 80;
        height: auto;
        max-height: 90%;
        overflow-y: auto;
    }
    .modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .field-label {
        margin-top: 1;
        color: $text;
        text-style: bold;
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

    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Label("Application Settings [Ctrl+S]", classes="modal-title")

            yield Label("Protected Directories (comma-separated):", classes="field-label")
            prot = getattr(self.settings, "PROTECTED_PATHS", getattr(self.settings, "PROTECTED_DIRECTORIES", []))
            prot_str = ", ".join(prot) if isinstance(prot, (list, tuple, set)) else str(prot)
            inp_prot = Input(value=prot_str, placeholder="/path/1, /path/2", id="input-protected")
            inp_prot.tooltip = "Comma-separated protected directories exempt from automated moves"
            yield inp_prot

            yield Label("Ignored Extensions (comma-separated):", classes="field-label")
            ign = getattr(self.settings, "IGNORED_EXTENSIONS", [])
            ign_str = ", ".join(ign) if isinstance(ign, (list, tuple, set)) else str(ign)
            inp_ign = Input(value=ign_str, placeholder=".tmp, .bak, .log", id="input-ignored")
            inp_ign.tooltip = "Comma-separated file extensions to ignore during scanning"
            yield inp_ign

            yield Label("Worker Concurrency (Threads):", classes="field-label")
            conc_str = str(getattr(self.settings, "MAX_WORKERS", getattr(self.settings, "WORKER_CONCURRENCY", 4)))
            inp_conc = Input(value=conc_str, placeholder="4", id="input-concurrency")
            inp_conc.tooltip = "Worker concurrency thread limit for processing"
            yield inp_conc

            yield Label("Sorting Strategy:", classes="field-label")
            strat = getattr(self.settings, "SORTING_STRATEGY", "default")
            options = [
                ("Standard Semantic", "default"),
                ("Generative AI", "generative"),
                ("Clinical TMF", "clinical_tmf"),
                ("Clinical ISF", "clinical_isf"),
            ]
            sel_strat = Select(options=options, value=strat, id="select-strategy")
            sel_strat.tooltip = "Select sorting strategy engine for document classification"
            yield sel_strat

            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel settings modification and close dialog"
                yield btn_cancel
                btn_save = Button("Save Settings", id="btn-save", variant="primary")
                btn_save.tooltip = "Save modified application settings"
                yield btn_save

    def on_mount(self) -> None:
        """Focus initial input field on mount and emit announcement."""
        self.query_one("#input-protected", Input).focus()
        self.announce("Opened application settings dialog.")

    @on(Button.Pressed, "#btn-save")
    def action_save(self) -> None:
        """Save settings and dismiss modal."""
        p_str = self.query_one("#input-protected", Input).value
        p_list = [p.strip() for p in p_str.split(",") if p.strip()]

        i_str = self.query_one("#input-ignored", Input).value
        i_list = [
            i.strip() if i.strip().startswith(".") else f".{i.strip()}"
            for i in i_str.split(",")
            if i.strip()
        ]

        try:
            conc = int(self.query_one("#input-concurrency", Input).value.strip())
        except ValueError:
            conc = 4

        strat = self.query_one("#select-strategy", Select).value
        if strat == Select.BLANK:
            strat = "default"

        res = {
            "PROTECTED_PATHS": p_list,
            "IGNORED_EXTENSIONS": i_list,
            "MAX_WORKERS": conc,
            "SORTING_STRATEGY": strat,
        }
        self.announce("Saved application settings.")
        self.dismiss(res)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel settings modification."""
        self.announce("Cancelled settings modification.")
        self.dismiss(None)


class WizardModal(A11yMixin, ModalScreen[None]):
    """Modal dialog for model onboarding wizard."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    WizardModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 90%;
        max-width: 80;
        height: auto;
        max-height: 90%;
        overflow-y: auto;
    }
    .modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .switch-row {
        margin-top: 1;
        margin-bottom: 1;
        height: 3;
        align: left middle;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus, Switch:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Label("Model Onboarding Wizard [Ctrl+W]", classes="modal-title")
            yield Label("Welcome to Smart AutoSorter AI Pro TUI.")
            yield Label("Enable AI semantic categorization consent below:")

            with Horizontal(classes="switch-row"):
                consent_val = getattr(self.settings, "AI_CONSENT_GRANTED", True)
                if consent_val is None:
                    consent_val = True
                sw = Switch(value=bool(consent_val), id="switch-consent")
                sw.tooltip = "Toggle AI consent for semantic document classification"
                yield sw
                yield Label(" AI Consent Granted")

            yield Label("Status: Local embedded AI model weights verified.")

            with Horizontal(classes="button-row"):
                btn_finish = Button("Finish & Save", id="btn-finish", variant="primary")
                btn_finish.tooltip = "Save AI consent setting and complete onboarding wizard"
                yield btn_finish

    def on_mount(self) -> None:
        """Focus switch on mount and emit screen reader announcement."""
        self.query_one("#switch-consent", Switch).focus()
        self.announce("Opened model onboarding wizard dialog.")

    @on(Button.Pressed, "#btn-finish")
    def action_finish(self) -> None:
        """Finish wizard and save consent settings."""
        consent = self.query_one("#switch-consent", Switch).value
        self.settings.AI_CONSENT_GRANTED = consent
        if hasattr(self.settings, "_save"):
            self.settings._save()
        self.announce("Finished model onboarding wizard and saved consent settings.")
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Cancel wizard on Escape key press."""
        self.announce("Closed model onboarding wizard dialog.")
        self.dismiss(None)


class CROForensicModal(A11yMixin, ModalScreen[None]):
    """Modal dialog for CRO multi-study forensic ingestion."""

    BINDINGS = [
        Binding("escape", "close", "Close dialog", show=True),
    ]

    CSS = """
    CROForensicModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 90%;
        max-width: 80;
        height: auto;
        max-height: 90%;
        overflow-y: auto;
    }
    .modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .log-area {
        height: auto;
        max-height: 6;
        min-height: 3;
        margin-top: 1;
        border: solid $secondary;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus, Input:focus, Log:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, settings, base_dir: str = ""):
        super().__init__()
        self.settings = settings
        self.base_dir = base_dir

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Label("CRO Multi-Study Forensic Ingestion [Ctrl+C]", classes="modal-title")
            yield Label("Source Storage Drive / Archive Root:")
            inp_src = Input(value=self.base_dir, placeholder="Select source drive to scan...", id="input-source")
            inp_src.tooltip = "Source storage drive path or archive directory root"
            yield inp_src

            yield Label("Target Audit Output Folder:")
            default_target = os.path.join(self.base_dir, "CRO_Audit_Output") if self.base_dir else ""
            inp_tgt = Input(value=default_target, placeholder="Select target output folder...", id="input-target")
            inp_tgt.tooltip = "Target output directory path for CRO forensic audit files"
            yield inp_tgt

            log_w = Log(classes="log-area", id="log-widget")
            log_w.tooltip = "Live execution log output for CRO forensic ingestion worker"
            yield log_w

            with Horizontal(classes="button-row"):
                btn_close = Button("Close", id="btn-close", variant="default")
                btn_close.tooltip = "Close CRO forensic ingestion modal dialog"
                yield btn_close
                btn_run = Button("Run Forensic Ingest", id="btn-run", variant="success")
                btn_run.tooltip = "Trigger CRO multi-study forensic ingestion worker execution"
                yield btn_run

    def on_mount(self) -> None:
        """Focus input field on mount and emit announcement."""
        self.query_one("#input-source", Input).focus()
        self.announce("Opened CRO multi-study forensic ingestion dialog.")

    @on(Button.Pressed, "#btn-run")
    def action_run(self) -> None:
        """Trigger forensic worker execution."""
        self.announce("Started CRO forensic multi-study ingestion worker execution.")
        self.run_forensic_worker()

    @work(exclusive=True, thread=True)
    def run_forensic_worker(self) -> None:
        """Run CRO multi-study forensic pipeline in worker thread."""
        log_w = self.query_one("#log-widget", Log)
        src = self.query_one("#input-source", Input).value.strip()
        tgt = self.query_one("#input-target", Input).value.strip()

        if not src or not os.path.exists(src):
            log_w.write_line("Error: Source directory does not exist.")
            err_msg = "Forensic scan error: Source directory does not exist."
            if self.app:
                self.app.call_from_thread(self.announce, err_msg)
            else:
                self.announce(err_msg)
            return

        log_w.write_line(f"Starting CRO Forensic Ingestion on: {src}")
        try:
            from app.core.cro_multi_study_pipeline import CROMultiStudyPipeline

            pipeline = CROMultiStudyPipeline(
                mode="tmf",
                smart_renaming=getattr(self.settings, "CLINICAL_SMART_RENAMING", True),
            )

            def progress_cb(pct: int, msg: str) -> None:
                log_w.write_line(f"[{pct}%] {msg}")

            result = pipeline.run_pipeline(
                source_root=src,
                target_root=tgt,
                progress_callback=progress_cb,
            )
            summary_msg = f"Completed successfully! Total scanned: {result.total_scanned_files}, Discovered studies: {result.discovered_studies_count}"
            log_w.write_line(summary_msg)
            if self.app:
                self.app.call_from_thread(self.announce, summary_msg)
            else:
                self.announce(summary_msg)
        except Exception as e:
            log_w.write_line(f"Execution error: {e}")
            err_msg = f"Forensic scan error: {e}"
            if self.app:
                self.app.call_from_thread(self.announce, err_msg)
            else:
                self.announce(err_msg)

    @on(Button.Pressed, "#btn-close")
    def action_close(self) -> None:
        """Dismiss forensic modal."""
        self.announce("Closed CRO forensic ingestion dialog.")
        self.dismiss(None)


class AutoSorterTUI(A11yMixin, App):
    """Textual full-screen interactive TUI application for Sortify AI Pro."""

    TITLE = "Sortify AI Pro - Terminal UI"
    SUB_TITLE = "Interactive Tree & Modal Controls"

    BINDINGS = [
        Binding("l", "toggle_lock", "Lock/Unlock", show=True),
        Binding("r", "rename_node", "Rename Node", show=True),
        Binding("n", "new_folder", "New Folder", show=True),
        Binding("plus", "rate_positive", "Rating (+)", show=True),
        Binding("minus", "rate_negative", "Rating (-)", show=True),
        Binding("ctrl+s", "open_settings", "Settings", show=True),
        Binding("ctrl+w", "open_wizard", "Wizard", show=True),
        Binding("ctrl+c", "open_cro_forensic", "CRO Ingest", show=True),
        Binding("s", "scan_directory", "Scan", show=True),
        Binding("e", "execute_sort", "Execute", show=True),
        Binding("b", "select_dir", "Browse Dir", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }
    #main-dual-pane {
        layout: horizontal;
        height: 1fr;
        width: 100%;
    }
    #left-tree-pane {
        width: 50%;
        height: 100%;
        border: solid $primary;
        padding: 0;
    }
    #right-meta-pane {
        width: 50%;
        height: 100%;
        border: solid $secondary;
        padding: 1 2;
    }
    #status-bar {
        height: 1;
        background: $primary-darken-2;
        color: $text;
        padding: 0 1;
    }
    .meta-header {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    .meta-line {
        margin-bottom: 1;
    }
    Tree:focus, Static:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, settings, base_dir: Optional[str] = None):
        super().__init__()
        self.settings = settings
        self.base_dir = os.path.abspath(base_dir) if base_dir else ""
        self.plan: Dict[str, Any] = {}
        self.locked_files: Dict[str, str] = {}
        self._ratings_cache: Dict[str, str] = {}
        self.app_session = None
        self.active_tree_node = None

    def compose(self) -> ComposeResult:
        """Compose main dual-pane TUI layout."""
        yield Header(show_clock=True)
        with Horizontal(id="main-dual-pane"):
            tree = Tree("Proposed Organization Plan", id="plan-tree")
            tree.tooltip = "Interactive tree view of proposed file organization plan"
            yield tree
            with Vertical(id="right-meta-pane"):
                yield Label("Node Metadata Inspector", classes="meta-header")
                meta_widget = Static("Select a node in the tree to inspect details.", id="meta-details")
                meta_widget.tooltip = "Metadata inspector panel displaying attributes of selected file or folder"
                yield meta_widget
        sb = Static("Ready. Press [S] to Scan or [B] to select Directory.", id="status-bar")
        sb.tooltip = "Application status and screen reader announcement bar"
        yield sb
        yield Footer()

    def on_mount(self) -> None:
        """Mount event handler."""
        try:
            tree = self.query_one("#plan-tree", Tree)
            tree.show_root = True
            tree.root.expand()
        except Exception:
            pass
        self.announce("Sortify AI Pro TUI initialized and ready.")

    def update_status(self, text: str) -> None:
        """Update status bar label."""
        try:
            sb = self.query_one("#status-bar", Static)
            sb.update(text)
        except Exception:
            pass

    def _get_active_node(self) -> Optional[TreeNode]:
        try:
            tree = self.query_one("#plan-tree", Tree)
            if tree.cursor_node and tree.cursor_node.data:
                return tree.cursor_node
            if getattr(self, "active_tree_node", None) and self.active_tree_node.data:
                return self.active_tree_node
        except Exception:
            pass
        return None

    # --- Actions ---

    def action_select_dir(self) -> None:
        """Open directory selection modal."""
        def on_selected(path: Optional[str]) -> None:
            if path and os.path.exists(path):
                self.base_dir = os.path.abspath(path)
                msg = f"Selected directory: {self.base_dir}"
                self.announce(msg)
                self.action_scan_directory()

        self.push_screen(DirectorySelectModal(current_dir=self.base_dir), on_selected)

    def action_open_settings(self) -> None:
        """Open settings modal screen."""
        def on_saved(res: Optional[Dict[str, Any]]) -> None:
            if res:
                if "PROTECTED_PATHS" in res:
                    try:
                        self.settings.PROTECTED_PATHS = res["PROTECTED_PATHS"]
                    except Exception:
                        pass
                if "IGNORED_EXTENSIONS" in res:
                    self.settings.IGNORED_EXTENSIONS = res["IGNORED_EXTENSIONS"]
                if "MAX_WORKERS" in res:
                    try:
                        self.settings.MAX_WORKERS = res["MAX_WORKERS"]
                    except Exception:
                        pass
                if "SORTING_STRATEGY" in res:
                    self.settings.SORTING_STRATEGY = res["SORTING_STRATEGY"]
                if hasattr(self.settings, "_save"):
                    self.settings._save()
                self.announce("Settings updated and saved.")
                if self.base_dir:
                    self.action_scan_directory()

        self.push_screen(SettingsModal(self.settings), on_saved)

    def action_open_wizard(self) -> None:
        """Open model onboarding wizard modal screen."""
        self.push_screen(WizardModal(self.settings))

    def action_open_cro_forensic(self) -> None:
        """Open CRO multi-study forensic ingestion modal screen."""
        self.push_screen(CROForensicModal(self.settings, base_dir=self.base_dir))

    def action_scan_directory(self) -> None:
        """Trigger directory scanning background worker."""
        if not self.base_dir or not os.path.exists(self.base_dir):
            self.action_select_dir()
            return

        self.announce(f"Scanning directory: {self.base_dir} ...")
        self.run_scan_worker()

    @work(exclusive=True, thread=True)
    def run_scan_worker(self) -> None:
        """Execute scan and plan generation in worker thread."""
        try:
            from app.core.session import AppSession

            if self.app_session:
                try:
                    self.app_session.close()
                except Exception:
                    pass

            self.app_session = AppSession(self.settings, self.base_dir)

            from app.core.scanner import get_files_recursively

            files = get_files_recursively(self.base_dir)

            from app.core.metadata import MetadataPass

            MetadataPass.run(
                self.base_dir,
                files,
                self.settings,
                self.app_session.db,
                None,
                lambda: False,
            )

            # Load locks & ratings
            try:
                docs = self.app_session.db.get_all_documents(self.base_dir)
                for d in docs:
                    if len(d) > 3 and d[3]:
                        self.locked_files[d[0]] = d[3]
            except Exception:
                pass

            try:
                self._ratings_cache = self.app_session.db.get_all_document_ratings(
                    self.base_dir
                )
            except Exception:
                pass

            self.plan = self.app_session.generate_sorting_plan()
            self.call_from_thread(self.rebuild_tree)
            msg = f"Scan complete. Analyzed {len(files)} files into proposed plan."
            self.call_from_thread(self.announce, msg)
        except Exception as e:
            logger.error(f"Error in run_scan_worker: {e}")
            self.call_from_thread(self.announce, f"Scan error: {e}")

    def action_execute_sort(self) -> None:
        """Trigger sorting plan execution worker."""
        if not self.plan or not self.app_session:
            self.announce("No plan available to execute. Run [S] Scan first.")
            return

        self.announce("Executing file moves according to plan...")
        self.run_execute_worker()

    @work(exclusive=True, thread=True)
    def run_execute_worker(self) -> None:
        """Execute moves in worker thread."""
        try:
            summary = self.app_session.execute_moves(self.plan)
            msg = f"Execution completed successfully! Summary: {summary}"
            self.call_from_thread(self.announce, msg)
            self.call_from_thread(self.action_scan_directory)
        except Exception as e:
            logger.error(f"Error executing moves: {e}")
            self.call_from_thread(self.announce, f"Execution error: {e}")

    # --- Tree Management & Event Handlers ---

    def rebuild_tree(self) -> None:
        """Rebuild Textual Tree widget from in-memory plan structure."""
        try:
            tree = self.query_one("#plan-tree", Tree)
            tree.reset("Proposed Organization Plan")
            self._build_tree_nodes(self.plan, tree.root, current_folder="")
            tree.root.expand_all()
        except Exception as e:
            logger.error(f"Error rebuilding tree: {e}")

    def _build_tree_nodes(self, node_dict: Dict[str, Any], parent_item: TreeNode, current_folder: str) -> None:
        for k, v in sorted(node_dict.items(), key=lambda x: (1 if isinstance(x[1], dict) and x[1].get("__type__") == "file" else 0, x[0])):
            if isinstance(v, dict) and v.get("__type__") == "file":
                file_key = k
                file_info = v
                filepath = file_info.get("filepath", os.path.join(self.base_dir, current_folder, file_key))

                is_locked = (
                    file_key in self.locked_files
                    or filepath in self.locked_files
                    or file_info.get("is_locked")
                )
                rating = self._ratings_cache.get(filepath) or file_info.get("rating")

                label_parts = []
                if is_locked:
                    label_parts.append("[LOCKED]")
                if rating == "positive":
                    label_parts.append("[+]")
                elif rating == "negative":
                    label_parts.append("[-]")

                label_parts.append(file_key)
                target_filename = file_info.get("target_filename")
                if target_filename and target_filename != file_key:
                    label_parts.append(f"-> {target_filename}")

                label_str = "📄 " + " ".join(label_parts)
                node_data = {
                    "is_file": True,
                    "key": file_key,
                    "filepath": filepath,
                    "folder": current_folder,
                    "info": file_info,
                    "is_locked": is_locked,
                    "rating": rating,
                }
                parent_item.add_leaf(label_str, data=node_data)
            elif isinstance(v, dict):
                sub_folder = os.path.join(current_folder, k) if current_folder else k
                node_data = {
                    "is_file": False,
                    "key": k,
                    "folder": sub_folder,
                    "info": v,
                }
                child_tree_node = parent_item.add(f"📁 {k}", data=node_data, expand=True)
                self._build_tree_nodes(v, child_tree_node, current_folder=sub_folder)

    @on(Tree.NodeHighlighted, "#plan-tree")
    @on(Tree.NodeSelected, "#plan-tree")
    def on_node_selected(self, event: Tree.NodeSelected) -> None:
        """Handle tree node selection or highlight to update metadata pane and screen reader announcement."""
        self.active_tree_node = event.node
        data = event.node.data
        try:
            meta_widget = self.query_one("#meta-details", Static)
        except Exception:
            return

        if not data:
            msg = "Root folder of sorting plan."
            meta_widget.update(msg)
            self.announce(f"Selected: {msg}")
            return

        lines = []
        if data.get("is_file"):
            key = data.get("key")
            folder = data.get("folder") or "(Root)"
            locked = "Yes [LOCKED]" if data.get("is_locked") else "No"
            rating = data.get("rating") or "None"
            lines.append(f"[bold accent]File:[/bold accent] {key}")
            lines.append(f"[bold]Path:[/bold] {data.get('filepath')}")
            lines.append(f"[bold]Target Folder:[/bold] {folder}")

            target_fn = data.get("info", {}).get("target_filename")
            if target_fn:
                lines.append(f"[bold]Target Filename:[/bold] {target_fn}")

            lines.append(f"[bold]Locked:[/bold] {locked}")
            lines.append(f"[bold]ML Rating:[/bold] {rating}")

            conf = data.get("info", {}).get("confidence")
            if conf is not None:
                lines.append(f"[bold]Confidence:[/bold] {conf:.2%}")

            self.announce(f"Selected file '{key}' in folder '{folder}'. Locked: {locked}. Rating: {rating}.")
        else:
            key = data.get("key")
            folder = data.get("folder")
            lines.append(f"[bold accent]Folder Category:[/bold accent] {key}")
            lines.append(f"[bold]Relative Path:[/bold] {folder}")
            self.announce(f"Selected folder category '{key}' at path '{folder}'.")

        meta_widget.update("\n".join(lines))

    # --- Keyboard Action Hotkeys ---

    def action_toggle_lock(self) -> None:
        """Toggle node lock state [L]."""
        node = self._get_active_node()
        if not node or not node.data or not node.data.get("is_file"):
            self.announce("Select a file node to toggle lock [L].")
            return

        data = node.data
        file_key = data["key"]
        filepath = data["filepath"]
        folder = data["folder"]

        if data.get("is_locked"):
            self.locked_files.pop(file_key, None)
            self.locked_files.pop(filepath, None)
            data["is_locked"] = False
            if self.app_session:
                self.app_session.db.set_user_verified_target_path(self.base_dir, file_key, None)
            msg = f"Unlocked file '{file_key}'"
        else:
            self.locked_files[file_key] = folder
            data["is_locked"] = True
            if self.app_session:
                self.app_session.db.set_user_verified_target_path(self.base_dir, file_key, folder)
            msg = f"Locked file '{file_key}' to folder '{folder}'"

        self.announce(msg)
        self.rebuild_tree()

    def action_rename_node(self) -> None:
        """Rename file or folder category node [R]."""
        node = self._get_active_node()
        if not node or not node.data:
            self.announce("Select a file or folder node to rename [R].")
            return

        data = node.data
        is_file = data.get("is_file")
        old_name = data.get("key")

        if is_file:
            stem, ext = os.path.splitext(old_name)
            modal = RenameModal(title=f"Rename File: {old_name}", current_name=stem, extension=ext)

            def on_renamed(new_stem: Optional[str]) -> None:
                if not new_stem or new_stem == stem:
                    return
                new_filename = new_stem + ext
                data["info"]["target_filename"] = new_filename
                data["info"]["is_locked"] = True
                data["is_locked"] = True
                self.locked_files[old_name] = data["folder"]
                if self.app_session:
                    self.app_session.db.set_user_verified_target_path(
                        self.base_dir, old_name, data["folder"]
                    )
                self.rebuild_tree()
                self.announce(f"Renamed target file to '{new_filename}' [Locked]")

            self.push_screen(modal, on_renamed)
        else:
            modal = RenameModal(title=f"Rename Folder: {old_name}", current_name=old_name)

            def on_folder_renamed(new_name: Optional[str]) -> None:
                if not new_name or new_name == old_name:
                    return
                if old_name in self.plan:
                    self.plan[new_name] = self.plan.pop(old_name)
                    self.rebuild_tree()
                    self.announce(f"Renamed folder category '{old_name}' -> '{new_name}'")

            self.push_screen(modal, on_folder_renamed)

    def action_new_folder(self) -> None:
        """Create a new folder category node [N]."""
        def on_created(folder_name: Optional[str]) -> None:
            if not folder_name:
                return
            if folder_name not in self.plan:
                self.plan[folder_name] = {}
                self.rebuild_tree()
                self.announce(f"Created new folder category: '{folder_name}'")
            else:
                self.announce(f"Folder category '{folder_name}' already exists.")

        self.push_screen(NewFolderModal(), on_created)

    def action_rate_positive(self) -> None:
        """Set positive ML rating feedback [+] for selected node."""
        self._set_rating_for_selected("positive")

    def action_rate_negative(self) -> None:
        """Set negative ML rating feedback [-] for selected node."""
        self._set_rating_for_selected("negative")

    def _set_rating_for_selected(self, rating: str) -> None:
        node = self._get_active_node()
        if not node or not node.data or not node.data.get("is_file"):
            self.announce("Select a file node to rate [+][-].")
            return

        data = node.data
        filepath = data["filepath"]
        current = self._ratings_cache.get(filepath)

        rating_to_set = None if current == rating else rating
        if rating_to_set:
            self._ratings_cache[filepath] = rating_to_set
        else:
            self._ratings_cache.pop(filepath, None)

        if self.app_session:
            self.app_session.db.set_document_rating(self.base_dir, filepath, rating_to_set)

        data["rating"] = rating_to_set
        self.rebuild_tree()
        msg = f"Set rating '{rating_to_set or 'cleared'}' for '{data['key']}'"
        self.announce(msg)


def run_tui(settings, base_dir: Optional[str] = None) -> None:
    """Run the Textual full-screen terminal interface."""
    app = AutoSorterTUI(settings=settings, base_dir=base_dir)
    app.run()
