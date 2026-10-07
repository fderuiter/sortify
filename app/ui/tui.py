"""Textual full-screen terminal interface with interactive tree and modal dialogs."""

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Log,
    ProgressBar,
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
        self._speech_thread: Optional[threading.Thread] = None

    def update_status(self, text: str) -> None:
        """Update visual status region on self or parent application."""
        if (
            hasattr(self, "app")
            and self.app
            and self.app is not self
            and hasattr(self.app, "update_status")
        ):
            try:
                self.app.update_status(text)
            except Exception:
                pass

    def join_speech_thread(self, timeout: float = 1.0) -> None:
        """Wait for active speech synthesis thread to finish."""
        speech_thread = self._speech_thread
        if speech_thread is not None:
            if speech_thread.is_alive():
                try:
                    speech_thread.join(timeout=timeout)
                except Exception:
                    pass
            if not speech_thread.is_alive():
                self._speech_thread = None
        if (
            hasattr(self, "app")
            and self.app
            and self.app is not self
            and hasattr(self.app, "join_speech_thread")
        ):
            try:
                self.app.join_speech_thread(timeout=timeout)
            except Exception:
                pass

    def on_unmount(self) -> None:
        """Lifecycle hook called when component is unmounted from Textual app."""
        self.join_speech_thread(timeout=0.5)
        if hasattr(super(), "on_unmount"):
            try:
                super().on_unmount()  # type: ignore[misc]
            except Exception:
                pass

    def _get_speech_binary(self) -> Optional[str]:
        """Resolve available speech synthesis executable based on host platform.

        On macOS, returns native 'say' command path. On Linux, returns 'spd-say' or
        'say' path. On Windows (win32), returns None as Linux/macOS speech binaries
        do not exist natively, defaulting to visual status live region fallback.
        """
        try:
            if sys.platform == "darwin":
                return shutil.which("say")
            elif sys.platform == "win32":
                return None
            else:
                return shutil.which("spd-say") or shutil.which("say")
        except (FileNotFoundError, OSError):
            return None

    def announce(
        self,
        message: str,
        priority: str = "polite",
        help_url: Optional[str] = None,
    ) -> str:
        """Emit auditory screen reader announcement and log accessibility event."""
        full_msg = f"{message} [Help: {help_url}]" if help_url else message
        entry = {
            "message": full_msg,
            "priority": priority,
            "timestamp": time.time(),
            "help_url": help_url,
        }
        self.announcements.append(entry)
        self.last_announcement = full_msg

        forwarded = False
        # Forward to parent app if available
        if (
            hasattr(self, "app")
            and self.app
            and self.app is not self
            and hasattr(self.app, "announce")
        ):
            try:
                self.app.announce(message, priority=priority, help_url=help_url)
                forwarded = True
            except Exception:
                pass

        if not forwarded:
            # Check speech binary presence via _get_speech_binary before launching subprocess
            speech_bin = self._get_speech_binary()

            if speech_bin:
                speech_thread = self._speech_thread
                if speech_thread is None or not speech_thread.is_alive():
                    try:

                        def _speak():
                            try:
                                kwargs: Dict[str, Any] = {
                                    "timeout": 1.0,
                                    "stdin": subprocess.DEVNULL,
                                    "stdout": subprocess.DEVNULL,
                                    "stderr": subprocess.DEVNULL,
                                    "check": False,
                                }
                                if sys.platform == "win32":
                                    kwargs["creationflags"] = getattr(
                                        subprocess, "CREATE_NO_WINDOW", 0
                                    )
                                subprocess.run(
                                    [speech_bin, message],
                                    **kwargs,
                                )
                            except BaseException as exc:
                                logger.debug(
                                    f"Speech synthesis execution failed: {exc}"
                                )
                            finally:
                                if (
                                    getattr(self, "_speech_thread", None)
                                    is threading.current_thread()
                                ):
                                    self._speech_thread = None

                        t = threading.Thread(target=_speak, daemon=True)
                        t.start()
                        self._speech_thread = t
                    except Exception as e:
                        logger.debug(f"Speech binary execution failed: {e}")

            # Fallback and update visual status region for every invocation
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
        4. Root application defines visual status region capabilities and speech binary fallback handling.
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
                        violations.append(
                            {
                                "rule": "A11Y001_MISSING_LABEL",
                                "widget_id": getattr(widget, "id", None) or w_type,
                                "message": f"Interactive control '{w_type}' lacks explicit tooltip or accessible text label.",
                            }
                        )
        except Exception:
            pass

        if isinstance(self, ModalScreen):
            bindings = getattr(self, "BINDINGS", [])
            has_escape = any(
                getattr(b, "key", None) == "escape"
                or (isinstance(b, Binding) and b.key == "escape")
                for b in bindings
            )
            if not has_escape:
                violations.append(
                    {
                        "rule": "A11Y_MISSING_ESCAPE_BINDING",
                        "message": f"Modal screen '{type(self).__name__}' lacks Escape key binding for accessibility dismissal.",
                    }
                )

        if not hasattr(self, "announce") or not hasattr(self, "announcements"):
            violations.append(
                {
                    "rule": "A11Y_MISSING_ANNOUNCER",
                    "message": f"Component '{type(self).__name__}' lacks screen reader announcement handler.",
                }
            )

        status_bar_available = hasattr(self, "update_status") or (
            hasattr(self, "app") and self.app and hasattr(self.app, "update_status")
        )
        if not status_bar_available:
            violations.append(
                {
                    "rule": "A11Y_MISSING_STATUS_REGION",
                    "message": f"Component '{type(self).__name__}' or root application lacks visual status region capability.",
                }
            )

        speech_binary = self._get_speech_binary()

        return {
            "component": type(self).__name__,
            "compliant": len(violations) == 0,
            "violations_count": len(violations),
            "violations": violations,
            "speech_binary_available": speech_binary is not None,
            "speech_binary": speech_binary,
            "speech_binary_fallback_ready": True,
            "status_bar_available": status_bar_available,
        }

    def _show_help_button(self, help_url: str) -> None:
        """Show the Help button and record contextual help_url."""
        self.help_url = help_url

        def _unhide() -> None:
            try:
                btn = self.query_one("#btn-help", Button)
                btn.remove_class("hidden")
            except Exception:
                pass

        app_obj = getattr(self, "app", None)
        if app_obj and hasattr(app_obj, "call_from_thread"):
            try:
                app_obj.call_from_thread(_unhide)
            except Exception:
                _unhide()
        else:
            _unhide()


class RenameModal(A11yMixin, ModalScreen[Optional[str]]):
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
        fallback = f"{self.current_name}{self.extension}" if self.extension else self.current_name
        return PatternTokenFormatter.format_pattern(
            pattern=pattern_str,
            metadata=metadata,
            seq=self.seq,
            fallback_original=fallback,
        )

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Label(self.modal_title, classes="modal-title")
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

    def _update_layout(self, width: int) -> None:
        """Update modal layout based on viewport width breakpoint."""
        if width < 80:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle modal viewport resize event."""
        self._update_layout(event.size.width)

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
            yield Label("Create New Target Folder", classes="modal-title")
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

    def _update_layout(self, width: int) -> None:
        """Update modal layout based on viewport width breakpoint."""
        if width < 80:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle modal viewport resize event."""
        self._update_layout(event.size.width)

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
    .preset-row {
        margin-top: 1;
        margin-bottom: 1;
        height: auto;
        min-height: 3;
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
            inp = Input(
                value=self.current_dir,
                placeholder="Enter absolute directory path...",
                id="input-dir",
            )
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

    def _update_layout(self, width: int) -> None:
        """Update modal layout based on viewport width breakpoint."""
        if width < 80:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle modal viewport resize event."""
        self._update_layout(event.size.width)

    def on_mount(self) -> None:
        """Focus input field on mount and emit screen reader announcement."""
        self._update_layout(self.size.width)
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
    .field-label {
        margin-top: 1;
        color: $text;
        text-style: bold;
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
    Button:focus, Input:focus, Select:focus, Switch:focus {
        border: heavy $accent;
        text-style: bold;
    }
    Button.hidden {
        display: none;
    }
    """

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.help_url = (
            "https://docs.smartautosorter.com/troubleshooting/#settings-configuration"
        )

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Label("Application Settings [Ctrl+O]", classes="modal-title")

            yield Label(
                "Protected Directories (comma-separated):", classes="field-label"
            )
            prot = getattr(
                self.settings,
                "PROTECTED_PATHS",
                getattr(self.settings, "PROTECTED_DIRECTORIES", []),
            )
            prot_str = (
                ", ".join(prot) if isinstance(prot, (list, tuple, set)) else str(prot)
            )
            inp_prot = Input(
                value=prot_str, placeholder="/path/1, /path/2", id="input-protected"
            )
            inp_prot.tooltip = (
                "Comma-separated protected directories exempt from automated moves"
            )
            yield inp_prot

            yield Label("Ignored Extensions (comma-separated):", classes="field-label")
            ign = getattr(self.settings, "IGNORED_EXTENSIONS", [])
            ign_str = (
                ", ".join(ign) if isinstance(ign, (list, tuple, set)) else str(ign)
            )
            inp_ign = Input(
                value=ign_str, placeholder=".tmp, .bak, .log", id="input-ignored"
            )
            inp_ign.tooltip = (
                "Comma-separated file extensions to ignore during scanning"
            )
            yield inp_ign

            yield Label("Max Folders:", classes="field-label")
            max_f_str = str(getattr(self.settings, "MAX_FOLDERS", 12))
            inp_max_f = Input(value=max_f_str, placeholder="12", id="input-max-folders")
            inp_max_f.tooltip = "Maximum subdirectories generated during sorting"
            yield inp_max_f

            yield Label("Worker Concurrency (Threads):", classes="field-label")
            conc_str = str(
                getattr(
                    self.settings,
                    "MAX_WORKERS",
                    getattr(self.settings, "WORKER_CONCURRENCY", 4),
                )
            )
            inp_conc = Input(value=conc_str, placeholder="4", id="input-concurrency")
            inp_conc.tooltip = "Worker concurrency thread limit for processing"
            yield inp_conc

            yield Label("Sorting Strategy:", classes="field-label")
            strat = getattr(self.settings, "SORTING_STRATEGY", "default")
            options = [
                ("Standard Semantic", "default"),
                ("Generative AI", "generative"),
            ]
            from app.core.plugin_registry import PluginRegistry

            reg = PluginRegistry.get_instance()
            reg.load_plugins_from_settings(self.settings)
            options.extend(reg.get_tui_strategy_options())
            if not any(opt[1] == strat for opt in options):
                options.append((f"Extension ({strat})", strat))
            sel_strat = Select(options=options, value=strat, id="select-strategy")
            sel_strat.tooltip = (
                "Select sorting strategy engine for document classification"
            )
            yield sel_strat

            yield Label("Compliance & Renaming Toggles:", classes="field-label")
            for sw_cfg in reg.get_tui_switches():
                sw_key = sw_cfg.get("key", "")
                sw_id = sw_cfg.get("id", f"switch-{sw_key.lower()}")
                sw_lbl = sw_cfg.get("label", f" {sw_key}")
                sw_tip = sw_cfg.get("tooltip", "")
                sw_val = bool(getattr(self.settings, sw_key, False))
                with Horizontal(classes="switch-row"):
                    sw_elem = Switch(value=sw_val, id=sw_id)
                    sw_elem.tooltip = sw_tip
                    yield sw_elem
                    yield Label(sw_lbl)

            with Horizontal(classes="switch-row"):
                sw_ai = Switch(
                    value=bool(getattr(self.settings, "AI_CONSENT_GRANTED", False)),
                    id="switch-ai-consent",
                )
                sw_ai.tooltip = "Toggle AI consent for semantic document classification"
                yield sw_ai
                yield Label(" AI Consent Granted")

            with Horizontal(classes="switch-row"):
                sw_ctx = Switch(
                    value=bool(getattr(self.settings, "CONTEXTUAL_RENAMING", False)),
                    id="switch-contextual-renaming",
                )
                sw_ctx.tooltip = "Toggle AI contextual file renaming"
                yield sw_ctx
                yield Label(" AI Contextual Renaming")

            btn_templates = Button(
                "Starter Rule Template Gallery",
                id="btn-open-templates",
                variant="primary",
            )
            btn_templates.tooltip = (
                "View and apply pre-configured starter rule template packs"
            )
            yield btn_templates

            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel settings modification and close dialog"
                yield btn_cancel
                btn_model_mgr = Button(
                    "Model Manager", id="btn-model-mgr", variant="primary"
                )
                btn_model_mgr.tooltip = (
                    "Open dedicated interactive model management modal"
                )
                yield btn_model_mgr
                btn_help = Button(
                    "Help", id="btn-help", variant="warning", classes="hidden"
                )
                btn_help.tooltip = "Open settings configuration troubleshooting guide"
                yield btn_help
                btn_save = Button("Save Settings", id="btn-save", variant="primary")
                btn_save.tooltip = "Save modified application settings"
                yield btn_save

    def _update_layout(self, width: int) -> None:
        """Update modal layout based on viewport width breakpoint."""
        if width < 80:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle modal viewport resize event."""
        self._update_layout(event.size.width)

    def on_mount(self) -> None:
        """Focus initial input field on mount and emit announcement."""
        self._update_layout(self.size.width)
        self.query_one("#input-protected", Input).focus()
        self.announce("Opened application settings dialog.")

    @on(Button.Pressed, "#btn-open-templates")
    def action_open_templates(self) -> None:
        """Open starter rule template gallery modal."""
        self.app.push_screen(TemplateGalleryModal(self.settings))

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

        help_url = (
            "https://docs.smartautosorter.com/troubleshooting/#settings-configuration"
        )
        try:
            conc_val = self.query_one("#input-concurrency", Input).value.strip()
            conc = int(conc_val)
            if conc <= 0:
                raise ValueError("Worker concurrency must be greater than 0.")
        except ValueError as e:
            self._show_help_button(help_url)
            from app.ui.notifications import notify

            notify(f"Settings error: {e}", type="error", help_url=help_url)
            self.announce(f"Settings error: {e}", help_url=help_url)
            return

        try:
            max_f_val = self.query_one("#input-max-folders", Input).value.strip()
            max_f = int(max_f_val)
            if max_f <= 0:
                raise ValueError("Max folders must be greater than 0.")
        except ValueError as e:
            self._show_help_button(help_url)
            from app.ui.notifications import notify

            notify(f"Settings error: {e}", type="error", help_url=help_url)
            self.announce(f"Settings error: {e}", help_url=help_url)
            return

        strat = self.query_one("#select-strategy", Select).value
        if strat == Select.BLANK:
            strat = "default"

        clin_sw = self.query(Switch).filter("#switch-clinical-renaming")
        clin_renaming = (
            clin_sw.first().value
            if clin_sw
            else bool(getattr(self.settings, "CLINICAL_SMART_RENAMING", False))
        )
        ctx_renaming = self.query_one("#switch-contextual-renaming", Switch).value
        ai_consent_sw = self.query(Switch).filter("#switch-ai-consent")
        ai_consent = (
            ai_consent_sw.first().value
            if ai_consent_sw
            else bool(getattr(self.settings, "AI_CONSENT_GRANTED", False))
        )

        res = {
            "PROTECTED_PATHS": p_list,
            "IGNORED_EXTENSIONS": i_list,
            "MAX_WORKERS": conc,
            "MAX_FOLDERS": max_f,
            "SORTING_STRATEGY": strat,
            "CLINICAL_SMART_RENAMING": clin_renaming,
            "CONTEXTUAL_RENAMING": ctx_renaming,
            "AI_CONSENT_GRANTED": ai_consent,
        }
        self.announce("Saved application settings.")
        self.dismiss(res)

    @on(Button.Pressed, "#btn-help")
    def action_help(self) -> None:
        """Open settings troubleshooting documentation."""
        url = getattr(
            self,
            "help_url",
            "https://docs.smartautosorter.com/troubleshooting/#settings-configuration",
        )
        from app.ui.notifications import notify

        notify(f"Opening help link: {url}", type="info", help_url=url)
        self.announce(f"Opened documentation link: {url}")

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel settings modification."""
        self.announce("Cancelled settings modification.")
        self.dismiss(None)

    @on(Button.Pressed, "#btn-model-mgr")
    def action_open_model_manager(self) -> None:
        """Open model manager modal."""
        if hasattr(self, "app") and self.app:
            self.app.push_screen(ModelManagerModal(self.settings))


class ModelManagerModal(A11yMixin, ModalScreen[None]):
    """Modal screen for interactive local ONNX model management."""

    BINDINGS = [
        Binding("escape", "cancel", "Close dialog", show=True),
    ]

    CSS = """
    ModelManagerModal {
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
    .field-label {
        margin-top: 1;
        color: $text;
        text-style: bold;
    }
    .info-label {
        margin-top: 0;
        color: $text;
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

    def __init__(self, settings=None):
        super().__init__()
        self.settings = settings
        self._prev_is_downloading = False
        self._prev_status_text = ""

    def _resolve_model_dir(self) -> str:
        """Resolve primary model directory path."""
        try:
            from app.config import get_app_dir
            return str(get_app_dir() / "model")
        except Exception:
            return os.path.expanduser("~/.smart-autosorter/model")

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Label("Interactive Model Management [Ctrl+M]", classes="modal-title")

            yield Label("Model Status:", classes="field-label")
            yield Label("Status: Checking...", id="lbl-model-status", classes="info-label")
            yield Label("Location: Checking...", id="lbl-model-path", classes="info-label")
            yield Label("Size on Disk: 0 MB", id="lbl-model-size", classes="info-label")
            yield Label("Verification: Checking...", id="lbl-model-hash", classes="info-label")

            yield Label("Download Progress & Throughput:", classes="field-label")
            pb = ProgressBar(total=100, show_percentage=True, id="progress-bar")
            pb.tooltip = "Active model download percentage progress"
            yield pb

            lbl_progress = Label("Idle", id="lbl-progress-status", classes="info-label")
            lbl_progress.tooltip = "Current download stage and throughput metrics"
            yield lbl_progress

            yield Label("Network Proxy Options:", classes="field-label")
            proxy_val = getattr(self.settings, "PROXY", "") if self.settings else ""
            inp_proxy = Input(
                value=proxy_val,
                placeholder="http://proxy.example.com:8080",
                id="input-proxy",
            )
            inp_proxy.tooltip = "HTTP/HTTPS proxy URL for model weight transfers"
            yield inp_proxy

            with Horizontal(classes="button-row"):
                btn_start = Button("Start Download", id="btn-start", variant="primary")
                btn_start.tooltip = "Start downloading local ONNX model weights"
                yield btn_start

                btn_cancel = Button("Cancel", id="btn-cancel-dl", variant="warning")
                btn_cancel.tooltip = "Cancel active model download thread"
                yield btn_cancel

                btn_retry = Button("Retry Download", id="btn-retry", variant="primary")
                btn_retry.tooltip = "Retry or restart model download"
                yield btn_retry

                btn_delete = Button("Delete Model", id="btn-delete", variant="error")
                btn_delete.tooltip = "Unload in-memory model instances and delete local files"
                yield btn_delete

                btn_close = Button("Close", id="btn-close", variant="default")
                btn_close.tooltip = "Close Model Management dialog"
                yield btn_close

    def _update_layout(self, width: int) -> None:
        """Update layout based on viewport width breakpoint."""
        if width < 80:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle modal viewport resize event."""
        self._update_layout(event.size.width)

    def on_mount(self) -> None:
        """Focus initial input, set polling timer, and announce modal launch."""
        self._update_layout(self.size.width)
        self.query_one("#input-proxy", Input).focus()
        self.announce("Opened Model Management dialog.")
        self._refresh_ui_state()
        self.set_interval(0.2, self._poll_download_status)

    def _apply_proxy(self) -> str:
        p_val = self.query_one("#input-proxy", Input).value.strip()
        if self.settings:
            setattr(self.settings, "PROXY", p_val)
        from app.core.downloader import DownloadManager
        dm = DownloadManager.get_instance()
        dm.update_proxy(p_val)
        return p_val

    def _refresh_ui_state(self) -> None:
        """Update model file info labels and control button states."""
        from app.core.downloader import DownloadManager, verify_downloaded_model
        dm = DownloadManager.get_instance()

        model_dir = self._resolve_model_dir()
        onnx_file = os.path.join(model_dir, "model.onnx")
        tmp_file = os.path.join(model_dir, "model.onnx.tmp")

        lbl_status = self.query_one("#lbl-model-status", Label)
        lbl_path = self.query_one("#lbl-model-path", Label)
        lbl_size = self.query_one("#lbl-model-size", Label)
        lbl_hash = self.query_one("#lbl-model-hash", Label)

        lbl_path.update(f"Location: {model_dir}")

        model_exists = os.path.exists(onnx_file) and os.path.getsize(onnx_file) > 0
        tmp_exists = os.path.exists(tmp_file)

        if model_exists:
            size_mb = os.path.getsize(onnx_file) / (1024 * 1024)
            lbl_size.update(f"Size on Disk: {size_mb:.2f} MB")
            lbl_status.update("Status: Downloaded & Present")
            if verify_downloaded_model(model_dir):
                lbl_hash.update("Verification: Valid (Cryptographically Verified)")
            else:
                lbl_hash.update("Verification: Unverified or Missing Configuration")
        elif tmp_exists:
            size_mb = os.path.getsize(tmp_file) / (1024 * 1024)
            lbl_size.update(f"Size on Disk: {size_mb:.2f} MB (Incomplete)")
            lbl_status.update("Status: Downloading / Incomplete")
            lbl_hash.update("Verification: Pending Finalization")
        else:
            lbl_size.update("Size on Disk: 0 MB")
            lbl_status.update("Status: Missing (Not Downloaded)")
            lbl_hash.update("Verification: N/A")

        is_dl = dm.state["is_downloading"]
        self.query_one("#btn-start", Button).disabled = is_dl
        self.query_one("#btn-cancel-dl", Button).disabled = not is_dl
        self.query_one("#btn-retry", Button).disabled = is_dl
        self.query_one("#btn-delete", Button).disabled = not (model_exists or tmp_exists or is_dl)

    def _poll_download_status(self) -> None:
        """Periodic non-blocking timer callback to poll download status."""
        from app.core.downloader import DownloadManager
        dm = DownloadManager.get_instance()

        is_dl = dm.state["is_downloading"]
        prog = float(dm.state["progress"])
        status_text = str(dm.state["status_text"] or "")
        err = dm.state["error"]
        succ = dm.state["success"]

        pb = self.query_one("#progress-bar", ProgressBar)
        pb.progress = min(100.0, max(0.0, prog * 100.0))

        lbl_prog = self.query_one("#lbl-progress-status", Label)
        if err:
            lbl_prog.update(f"Error: {err}")
        elif succ:
            lbl_prog.update("Download complete!")
        else:
            lbl_prog.update(status_text if status_text else ("Downloading..." if is_dl else "Idle"))

        if self._prev_is_downloading and not is_dl:
            if succ:
                self.announce("Model download completed successfully.", priority="polite")
            elif err:
                self.announce(f"Model download failed: {err}", priority="assertive")
            elif "cancelled" in status_text.lower():
                self.announce("Model download cancelled.", priority="polite")

        self._prev_is_downloading = is_dl
        self._prev_status_text = status_text

        self._refresh_ui_state()

    @on(Button.Pressed, "#btn-start")
    def action_start_download(self) -> None:
        """Start background model weight download."""
        p_val = self._apply_proxy()
        from app.core.downloader import DEFAULT_MODEL_URL, DownloadManager
        dm = DownloadManager.get_instance()
        model_dir = self._resolve_model_dir()
        try:
            dm.start_download(DEFAULT_MODEL_URL, model_dir, proxy=p_val)
            self.announce("Started model download.", priority="polite")
        except Exception as e:
            from app.ui.notifications import notify
            notify(f"Download error: {e}", type="error")
            self.announce(f"Download error: {e}", priority="assertive")

    @on(Button.Pressed, "#btn-cancel-dl")
    def action_cancel_download(self) -> None:
        """Cancel active model download thread."""
        from app.core.downloader import DownloadManager
        dm = DownloadManager.get_instance()
        dm.cancel_download()
        self.announce("Cancelled model download.", priority="polite")

    @on(Button.Pressed, "#btn-retry")
    def action_retry_download(self) -> None:
        """Retry model weight download operation."""
        p_val = self._apply_proxy()
        from app.core.downloader import DEFAULT_MODEL_URL, DownloadManager
        dm = DownloadManager.get_instance()
        if dm.state["is_downloading"]:
            dm.cancel_download()
        model_dir = self._resolve_model_dir()
        try:
            dm.start_download(DEFAULT_MODEL_URL, model_dir, proxy=p_val)
            self.announce("Retrying model download.", priority="polite")
        except Exception as e:
            from app.ui.notifications import notify
            notify(f"Download error: {e}", type="error")
            self.announce(f"Download error: {e}", priority="assertive")

    @on(Button.Pressed, "#btn-delete")
    def action_delete_model(self) -> None:
        """Asynchronously delete local model files."""
        from app.core.downloader import DownloadManager
        dm = DownloadManager.get_instance()
        model_dir = self._resolve_model_dir()

        def _on_done(success: bool, err: Optional[Exception]):
            if success:
                self.announce("Model deleted successfully.", priority="polite")
            else:
                self.announce(f"Model deletion failed: {err}", priority="assertive")

        dm.delete_model_async(model_dir, on_done=_on_done)
        self.announce("Deleting model weights...", priority="polite")

    @on(Button.Pressed, "#btn-close")
    def action_close(self) -> None:
        """Close model management dialog."""
        self.announce("Closed Model Management dialog.")
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Cancel and close model management dialog."""
        self.announce("Closed Model Management dialog.")
        self.dismiss(None)



class TemplateGalleryModal(A11yMixin, ModalScreen[None]):
    """Modal screen for browsing and applying starter rule template packs."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    TemplateGalleryModal {
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
    .field-label {
        margin-top: 1;
        color: $text;
        text-style: bold;
    }
    .template-card {
        margin-top: 1;
        margin-bottom: 1;
        padding: 0 1;
        border: solid $accent;
        height: auto;
    }
    .template-desc {
        color: $text-muted;
        margin-left: 3;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus, Checkbox:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, settings):
        super().__init__()
        self.settings = settings

    def compose(self) -> ComposeResult:
        """Compose template gallery children."""
        with Vertical(classes="modal-box"):
            yield Label("Starter Rule Template Gallery", classes="modal-title")
            yield Label(
                "Select starter template packs to apply. Template rules merge safely without overwriting custom rules:"
            )

            from app.core.rule_templates import get_starter_templates

            for pack in get_starter_templates():
                with Vertical(classes="template-card"):
                    chk = Checkbox(
                        f"{pack.icon} {pack.title}",
                        value=False,
                        id=f"chk-gallery-{pack.id}",
                    )
                    chk.tooltip = pack.description
                    yield chk
                    yield Label(pack.description, classes="template-desc")

            with Horizontal(classes="button-row"):
                btn_close = Button("Close", id="btn-close-gallery", variant="default")
                btn_close.tooltip = "Close template gallery"
                yield btn_close
                btn_apply = Button(
                    "Apply Selected Templates",
                    id="btn-apply-templates",
                    variant="primary",
                )
                btn_apply.tooltip = "Merge selected template packs into settings"
                yield btn_apply

    def _update_layout(self, width: int) -> None:
        """Update modal layout based on viewport width breakpoint."""
        if width < 80:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle modal viewport resize event."""
        self._update_layout(event.size.width)

    def on_mount(self) -> None:
        """Focus initial checkbox on mount and emit announcement."""
        self._update_layout(self.size.width)
        from app.core.rule_templates import get_starter_templates

        templates = get_starter_templates()
        if templates:
            first_id = templates[0].id
            try:
                self.query_one(f"#chk-gallery-{first_id}", Checkbox).focus()
            except Exception:
                pass
        self.announce("Opened starter rule template gallery.")

    @on(Button.Pressed, "#btn-apply-templates")
    def action_apply_templates(self) -> None:
        """Apply selected starter template packs to settings."""
        from app.core.rule_templates import (
            apply_starter_templates,
            get_starter_templates,
        )

        selected_pack_ids = []
        for pack in get_starter_templates():
            try:
                chk = self.query_one(f"#chk-gallery-{pack.id}", Checkbox)
                if chk.value:
                    selected_pack_ids.append(pack.id)
            except Exception:
                pass

        if selected_pack_ids:
            apply_starter_templates(self.settings, selected_pack_ids)
            msg = f"Applied {len(selected_pack_ids)} starter rule template pack(s) to settings."
            self.announce(msg)
            if hasattr(self.app, "notify"):
                self.app.notify(msg)
        else:
            self.announce("No starter template packs were selected.")

        self.dismiss(None)

    @on(Button.Pressed, "#btn-close-gallery")
    def action_close(self) -> None:
        """Close template gallery dialog."""
        self.announce("Closed starter rule template gallery.")
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Cancel on Escape key."""
        self.announce("Closed starter rule template gallery.")
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
    .field-label {
        margin-top: 1;
        color: $text;
        text-style: bold;
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
    Button:focus, Checkbox:focus, Switch:focus {
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
                consent_val = getattr(self.settings, "AI_CONSENT_GRANTED", None)
                if consent_val is None:
                    consent_val = False
                sw = Switch(value=bool(consent_val), id="switch-consent")
                sw.tooltip = "Toggle AI consent for semantic document classification"
                yield sw
                yield Label(" AI Consent Granted")

            with Horizontal(classes="switch-row"):
                sw_sample = Switch(value=False, id="switch-sample-corpus")
                sw_sample.tooltip = (
                    "Toggle generation of sample document corpus in active workspace"
                )
                yield sw_sample
                yield Label(" Generate Sample Documents")

            yield Label("Starter Rule Template Packs:", classes="field-label")
            yield Label(
                "Select pre-built template packs to populate keyword rules & compliance policies:"
            )

            from app.core.rule_templates import get_starter_templates

            for pack in get_starter_templates():
                is_default = pack.id in ("financial_tax", "personal_admin")
                chk = Checkbox(
                    f"{pack.icon} {pack.title}",
                    value=is_default,
                    id=f"chk-template-{pack.id}",
                )
                chk.tooltip = pack.description
                yield chk

            yield Label("Status: Local embedded AI model weights verified.")

            with Horizontal(classes="button-row"):
                btn_finish = Button("Finish & Save", id="btn-finish", variant="primary")
                btn_finish.tooltip = (
                    "Save AI consent setting and complete onboarding wizard"
                )
                yield btn_finish

    def _update_layout(self, width: int) -> None:
        """Update modal layout based on viewport width breakpoint."""
        if width < 80:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle modal viewport resize event."""
        self._update_layout(event.size.width)

    def on_mount(self) -> None:
        """Focus switch on mount and emit screen reader announcement."""
        self._update_layout(self.size.width)
        self.query_one("#switch-consent", Switch).focus()
        self.announce("Opened model onboarding wizard dialog.")

    @on(Button.Pressed, "#btn-finish")
    def action_finish(self) -> None:
        """Finish wizard, save consent settings, and apply selected starter rule templates."""
        consent = self.query_one("#switch-consent", Switch).value
        self.settings.AI_CONSENT_GRANTED = consent

        from app.core.rule_templates import (
            apply_starter_templates,
            get_starter_templates,
        )

        selected_packs = []
        for pack in get_starter_templates():
            try:
                chk = self.query_one(f"#chk-template-{pack.id}", Checkbox)
                if chk.value:
                    selected_packs.append(pack.id)
            except Exception:
                pass

        if selected_packs:
            apply_starter_templates(self.settings, selected_packs)

        if hasattr(self.settings, "_save"):
            try:
                self.settings._save()
            except Exception as e:
                logger.error(f"Error saving consent setting: {e}")
        gen_sample = False
        try:
            gen_sample = self.query_one("#switch-sample-corpus", Switch).value
        except Exception:
            pass

        if gen_sample:
            from app.core.sample_corpus import generate_sample_corpus

            target_dir = getattr(self.app, "base_dir", "") or os.getcwd()
            if hasattr(self.app, "base_dir"):
                self.app.base_dir = target_dir
            try:
                generate_sample_corpus(target_dir, overwrite=True)
                self.announce(
                    f"Generated sample document corpus in '{target_dir}'."
                )
                if hasattr(self.app, "action_scan_directory"):
                    self.app.action_scan_directory()
            except Exception as exc:
                logger.error(f"Failed to generate sample corpus: {exc}")

        self.announce(
            "Finished model onboarding wizard, saved consent settings, and applied selected starter rule templates."
        )
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Cancel wizard on Escape key press."""
        if getattr(self.settings, "AI_CONSENT_GRANTED", None) is None:
            self.settings.AI_CONSENT_GRANTED = False
            if hasattr(self.settings, "_save"):
                self.settings._save()
        self.announce("Closed model onboarding wizard dialog.")
        self.dismiss(None)


class SessionRecoveryModal(A11yMixin, ModalScreen[Optional[str]]):
    """Modal dialog for recovering interrupted file sorting sessions."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    SessionRecoveryModal {
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
        color: $error;
        margin-bottom: 1;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, session_info: Dict[str, Any]):
        super().__init__()
        self.session_info = session_info

    def compose(self) -> ComposeResult:
        """Compose session recovery modal children."""
        with Vertical(classes="modal-box"):
            yield Label("Interrupted Session Detected", classes="modal-title")
            yield Label(f"Session ID: {self.session_info.get('session_id', 'Unknown')}")
            yield Label(
                f"Target Directory: {self.session_info.get('base_dir', 'Unknown')}"
            )
            yield Label(f"Status: {self.session_info.get('status', 'Unknown')}")
            yield Label("Select session recovery action:")

            with Horizontal(classes="button-row"):
                btn_clean = Button("Clean", id="btn-clean", variant="error")
                btn_clean.tooltip = "Discard session files and clean workspace"
                yield btn_clean

                btn_rollback = Button("Rollback", id="btn-rollback", variant="warning")
                btn_rollback.tooltip = (
                    "Rollback partial file moves from interrupted run"
                )
                yield btn_rollback

                btn_help = Button("Help", id="btn-help", variant="default")
                btn_help.tooltip = "Open session recovery troubleshooting guide"
                yield btn_help

                btn_resume = Button("Resume", id="btn-resume", variant="primary")
                btn_resume.tooltip = "Resume pending file moves for interrupted run"
                yield btn_resume

    def _update_layout(self, width: int) -> None:
        """Update modal layout based on viewport width breakpoint."""
        if width < 80:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle modal viewport resize event."""
        self._update_layout(event.size.width)

    def on_mount(self) -> None:
        """Focus resume button on mount and emit screen reader announcement."""
        self._update_layout(self.size.width)
        self.query_one("#btn-resume", Button).focus()
        self.announce("Opened interrupted session recovery dialog.")

    @on(Button.Pressed, "#btn-resume")
    def action_resume(self) -> None:
        """Confirm resume action."""
        self.announce("Confirmed session resume.")
        self.dismiss("resume")

    @on(Button.Pressed, "#btn-rollback")
    def action_rollback(self) -> None:
        """Confirm rollback action."""
        self.announce("Confirmed session rollback.")
        self.dismiss("rollback")

    @on(Button.Pressed, "#btn-clean")
    def action_clean(self) -> None:
        """Confirm clean action."""
        self.announce("Confirmed session clean.")
        self.dismiss("clean")

    @on(Button.Pressed, "#btn-help")
    def action_help(self) -> None:
        """Open session recovery troubleshooting guide."""
        url = "https://docs.smartautosorter.com/troubleshooting/#session-recovery"
        from app.ui.notifications import notify

        notify(f"Opening help link: {url}", type="info", help_url=url)
        self.announce(f"Opened documentation link: {url}")

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel recovery dialog."""
        self.announce("Cancelled session recovery dialog.")
        self.dismiss(None)


class ShortcutCheatSheetModal(A11yMixin, ModalScreen[None]):
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
            yield Label(
                "Keyboard Shortcuts Cheat Sheet [? / F1]", classes="modal-title"
            )

            yield Label("Navigation & Tree Controls:", classes="section-title")
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

            yield Label("Application Operations:", classes="section-title")
            yield Label(
                "  Ctrl+S      - Scan directory and generate plan",
                classes="shortcut-row",
            )
            yield Label(
                "  Ctrl+H      - Session history & audit log export",
                classes="shortcut-row",
            )
            yield Label("  Ctrl+E/Enter- Execute sorting plan", classes="shortcut-row")
            yield Label(
                "  Ctrl+B      - Browse / select target directory",
                classes="shortcut-row",
            )
            yield Label(
                "  Ctrl+O      - Open application settings", classes="shortcut-row"
            )
            yield Label(
                "  Ctrl+W      - Open model onboarding wizard", classes="shortcut-row"
            )
            yield Label(
                "  Ctrl+C      - Open CRO multi-study forensic ingestion",
                classes="shortcut-row",
            )

            yield Label("Help & System:", classes="section-title")
            yield Label(
                "  ? / F1      - Open shortcut cheat sheet", classes="shortcut-row"
            )
            yield Label("  Escape      - Close modal dialog", classes="shortcut-row")
            yield Label("  Ctrl+Q      - Quit application", classes="shortcut-row")

            with Horizontal(classes="button-row"):
                btn_close = Button("Close", id="btn-close", variant="primary")
                btn_close.tooltip = "Close keyboard shortcut cheat sheet modal dialog"
                yield btn_close

    def _update_layout(self, width: int) -> None:
        """Update modal layout based on viewport width breakpoint."""
        if width < 80:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle modal viewport resize event."""
        self._update_layout(event.size.width)

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


class DropZoneModal(A11yMixin, ModalScreen[Optional[Dict[str, Any]]]):
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
        width: 90%;
        max-width: 80;
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
    .dropzone-target {
        border: dashed $primary;
        background: $surface;
        padding: 1 2;
        margin-top: 1;
        margin-bottom: 1;
        align: center middle;
        height: auto;
    }
    .dropzone-instructions {
        text-style: bold;
        color: $primary;
        text-align: center;
        margin-bottom: 1;
    }
    .dropzone-subinstructions {
        color: $text-muted;
        text-align: center;
    }
    .status-text {
        color: $accent;
        margin-top: 1;
        margin-bottom: 1;
        text-style: italic;
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
    Button.hidden {
        display: none;
    }
    """

    def __init__(self, settings: Any = None, base_dir: Optional[str] = None):
        super().__init__()
        self.settings = settings
        self.base_dir = base_dir
        self.processed_result: Optional[Dict[str, Any]] = None

    def compose(self) -> ComposeResult:
        """Compose floating dropzone modal overlay controls."""
        with Vertical(classes="modal-box"):
            yield Label("Floating DropZone - Quick File Triage", classes="modal-title")
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

    def _update_layout(self, width: int) -> None:
        """Update modal layout based on viewport width breakpoint."""
        if width < 80:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle modal viewport resize event."""
        self._update_layout(event.size.width)

    def on_mount(self) -> None:
        """Focus input on mount and announce modal opening."""
        self._app_ref = self.app
        self._update_layout(self.size.width)
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
                self.announce(
                    f"Pasted path payload into dropzone: {event.text.strip()}"
                )
            except Exception:
                pass

    @on(Input.Submitted, "#input-drop-paths")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle enter key in drop paths input field."""
        self.trigger_drop_triage()

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
            "https://docs.smartautosorter.com/troubleshooting/#dropzone-errors",
        )
        from app.ui.notifications import notify

        notify(f"Opening help link: {url}", type="info", help_url=url)
        self.announce(f"Opened documentation link: {url}")

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Dismiss dropzone modal."""
        self.announce("Cancelled dropzone modal dialog.")
        self.dismiss(None)

    def trigger_drop_triage(self) -> None:
        """Parse input paths and trigger automated classification worker."""
        try:
            inp = self.query_one("#input-drop-paths", Input)
            raw_text = inp.value.strip()
        except Exception:
            raw_text = ""

        help_url = "https://docs.smartautosorter.com/troubleshooting/#dropzone-errors"

        if not raw_text:
            self._show_help_button(help_url)
            from app.ui.notifications import notify

            notify("No file or folder paths provided.", type="error", help_url=help_url)
            self.update_status_msg(
                "No file or folder paths provided. Please paste or type a path.",
                help_url=help_url,
            )
            return

        raw_items = [
            p.strip().strip("'\"")
            for p in raw_text.replace("\r", "\n").split("\n")
            if p.strip()
        ]
        validated_paths = []
        invalid_messages = []

        for item in raw_items:
            abs_p = os.path.abspath(item)
            if not os.path.exists(abs_p):
                invalid_messages.append(f"Path does not exist: {item}")
                continue

            if self.settings:
                protected = getattr(self.settings, "PROTECTED_PATHS", [])
                from app.core.mover import is_subpath_or_equal

                is_prot = False
                for prot in protected:
                    if prot and is_subpath_or_equal(abs_p, prot):
                        is_prot = True
                        break
                if is_prot:
                    invalid_messages.append(f"Protected path blocked: {item}")
                    continue

            validated_paths.append(abs_p)

        if not validated_paths:
            err_text = (
                "; ".join(invalid_messages)
                if invalid_messages
                else "No valid paths found."
            )
            self._show_help_button(help_url)
            from app.ui.notifications import notify

            notify(f"DropZone error: {err_text}", type="error", help_url=help_url)
            self.update_status_msg(f"Error: {err_text}", help_url=help_url)
            return

        self.update_status_msg(f"Processing {len(validated_paths)} dropped item(s)...")
        self.run_drop_worker(validated_paths)

    def update_status_msg(self, msg: str, help_url: Optional[str] = None) -> None:
        """Update live status message region and screen reader announcement."""
        display_msg = f"{msg} [Help: {help_url}]" if help_url else msg
        try:
            lbl = self.query_one("#dropzone-status", Label)
            lbl.update(display_msg)
        except Exception:
            pass
        self.announce(msg, help_url=help_url)

    @work(exclusive=True, thread=True)
    def run_drop_worker(self, target_paths: List[str]) -> None:
        """Execute automated file classification and relocation in worker thread."""
        app_ref = getattr(self, "_app_ref", None)
        try:
            from app.core.extractor import build_corpus_generator
            from app.core.scanner import get_files_recursively
            from app.core.session import AppSession

            processed_items = []
            for path in target_paths:
                if os.path.isdir(path):
                    base_dir = path
                    files = get_files_recursively(base_dir)
                else:
                    base_dir = os.path.dirname(path)
                    files = [path]

                if app_ref:
                    app_ref.call_from_thread(
                        self.update_status_msg,
                        f"Classifying {len(files)} file(s) in '{os.path.basename(path)}'...",
                    )

                session = None
                try:
                    session = AppSession(self.settings, base_dir=base_dir)
                    generator = build_corpus_generator(
                        base_dir=base_dir,
                        items_to_sort=files,
                        progress_callback=lambda info=None: None,
                        max_workers=getattr(self.settings, "MAX_WORKERS", 2),
                        db=session.db,
                        settings=self.settings,
                    )
                    for chunk in generator:
                        session.partial_fit(chunk)

                    plan = (
                        session.generateSorting_plan()
                        if hasattr(session, "generateSorting_plan")
                        else session.generate_sorting_plan()
                    )
                    if app_ref:
                        app_ref.call_from_thread(
                            self.update_status_msg,
                            "Relocating items according to generated sorting plan...",
                        )
                    summary = session.execute_moves(plan)
                    processed_items.append({"path": path, "summary": summary})
                finally:
                    if session:
                        session.close()

            final_msg = f"Completed triage of {len(processed_items)} dropped item(s)!"
            if app_ref:
                app_ref.call_from_thread(self.update_status_msg, final_msg)
            result_dict = {
                "status": "success",
                "processed_count": len(processed_items),
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
            logger.exception(f"Error during dropzone classification: {e}")
            if app_ref:
                app_ref.call_from_thread(
                    self.update_status_msg, f"Dropzone triage error: {e}"
                )


class ExportReportModal(A11yMixin, ModalScreen[Optional[Tuple[str, str]]]):
    """Modal dialog for exporting dry-run simulation reports."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    ExportReportModal {
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

    def __init__(
        self,
        default_path: str = "./simulation_report.html",
        default_format: str = "html",
    ):
        super().__init__()
        self.default_path = default_path
        self.default_format = default_format

    def compose(self) -> ComposeResult:
        """Compose export report modal widgets."""
        with Vertical(classes="modal-box"):
            yield Label("Export Simulation Report", classes="modal-title")
            yield Label(
                "Select output file path and report format:",
                classes="modal-subtitle",
            )
            inp = Input(
                value=self.default_path,
                placeholder="Enter output path (e.g. ./report.html)...",
                id="input-export-path",
            )
            inp.tooltip = "Enter path for exported dry-run simulation report"
            yield inp

            fmt_select = Select(
                [("HTML Report (*.html)", "html"), ("JSON Data (*.json)", "json")],
                value=self.default_format,
                id="select-export-format",
                allow_blank=False,
            )
            yield fmt_select

            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel report export action"
                yield btn_cancel
                btn_export = Button("Export Report", id="btn-export", variant="primary")
                btn_export.tooltip = "Export simulation report to disk"
                yield btn_export

    def _update_layout(self, width: int) -> None:
        """Update layout based on viewport width breakpoint."""
        if width < 80:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle viewport resize event."""
        self._update_layout(event.size.width)

    def on_mount(self) -> None:
        """Focus input field on mount and emit accessibility announcement."""
        self._update_layout(self.size.width)
        self.query_one("#input-export-path", Input).focus()
        self.announce("Opened export simulation report dialog.")

    @on(Input.Submitted, "#input-export-path")
    def action_submit_input(self) -> None:
        """Handle input submission event."""
        self.action_export()

    @on(Button.Pressed, "#btn-export")
    def action_export(self) -> None:
        """Confirm report export and dismiss modal with export parameters."""
        path_val = self.query_one("#input-export-path", Input).value.strip()
        if not path_val:
            self.announce("Output path cannot be empty.")
            return
        format_val = self.query_one("#select-export-format", Select).value or "html"
        self.dismiss((path_val, str(format_val)))

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel report export modal."""
        self.dismiss(None)


class AuditReportModal(A11yMixin, ModalScreen[None]):
    """Interactive Modal Screen for Tabular Verification Tree & Multi-Format Audit Export."""

    BINDINGS = [
        Binding("escape", "cancel", "Close verification dialog", show=True),
        Binding("ctrl+j", "export_json", "Export JSON Report", show=True),
        Binding("ctrl+c", "export_csv", "Export CSV Report", show=True),
    ]

    CSS = """
    AuditReportModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.7);
    }
    .audit-modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 95%;
        max-width: 100;
        min-width: 50;
        height: 85%;
        min-height: 20;
    }
    .narrow .audit-modal-box {
        padding: 0 1;
        width: 98%;
    }
    .audit-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 0;
    }
    .audit-subtitle {
        color: $text-muted;
        margin-bottom: 1;
    }
    .audit-stats-row {
        height: 3;
        margin-bottom: 1;
        align: center middle;
    }
    .stat-card {
        padding: 0 1;
        margin-right: 1;
        border: solid $secondary;
        background: $surface;
        min-width: 15;
    }
    .stat-pass {
        color: $success;
        border: solid $success;
    }
    .stat-fail {
        color: $error;
        border: solid $error;
    }
    .stat-skipped {
        color: $warning;
        border: solid $warning;
    }
    .filter-row {
        height: 3;
        margin-bottom: 1;
    }
    #audit-search-input {
        width: 60%;
        margin-right: 1;
    }
    #audit-status-filter {
        width: 38%;
    }
    #audit-table {
        height: 1fr;
        margin-bottom: 1;
        border: solid $accent;
    }
    .button-row {
        height: 3;
        align: right middle;
    }
    Button:focus, Input:focus, Select:focus, DataTable:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, report_data: Dict[str, Any]):
        super().__init__()
        self.report_data = report_data or {}

    def compose(self) -> ComposeResult:
        """Compose verification audit report modal widgets."""
        with Vertical(classes="audit-modal-box"):
            yield Label(
                "Post-Sort File Integrity Verification Audit Report",
                classes="audit-title",
            )
            base_dir_str = self.report_data.get("base_dir", "")
            yield Label(f"Target Directory: {base_dir_str}", classes="audit-subtitle")

            total = self.report_data.get("total_files", 0)
            pass_cnt = self.report_data.get("verified_pass_count", 0)
            fail_cnt = self.report_data.get("mismatch_fail_count", 0)
            skip_cnt = self.report_data.get("skipped_count", 0)

            with Horizontal(classes="audit-stats-row"):
                yield Static(f"Total: {total}", classes="stat-card")
                yield Static(f"[PASS]: {pass_cnt}", classes="stat-card stat-pass")
                yield Static(f"[FAIL]: {fail_cnt}", classes="stat-card stat-fail")
                yield Static(f"[SKIPPED]: {skip_cnt}", classes="stat-card stat-skipped")

            with Horizontal(classes="filter-row"):
                inp = Input(
                    placeholder="Search by filename or path...",
                    id="audit-search-input",
                )
                inp.tooltip = "Filter verification results by filename or path"
                yield inp

                status_sel = Select(
                    [
                        ("All Statuses", "ALL"),
                        ("Verified Pass [PASS]", "VERIFIED_PASS"),
                        ("Mismatch Fail [FAIL]", "MISMATCH_FAIL"),
                        ("Skipped [SKIPPED]", "SKIPPED"),
                    ],
                    value="ALL",
                    id="audit-status-filter",
                    allow_blank=False,
                )
                status_sel.tooltip = "Filter results by verification status"
                yield status_sel

            table = DataTable(id="audit-table")
            table.cursor_type = "row"
            table.tooltip = "Audit report tabular verification tree"
            yield table

            with Horizontal(classes="button-row"):
                btn_json = Button(
                    "Export JSON", id="btn-export-json", variant="primary"
                )
                btn_json.tooltip = (
                    "Export audit report to JSON file in target directory"
                )
                yield btn_json

                btn_csv = Button("Export CSV", id="btn-export-csv", variant="success")
                btn_csv.tooltip = "Export audit report to CSV file in target directory"
                yield btn_csv

                btn_close = Button("Close", id="btn-close", variant="default")
                btn_close.tooltip = "Close verification audit report dialog"
                yield btn_close

    def _update_layout(self, width: int, height: int = 24) -> None:
        """Update layout based on viewport width and height breakpoint."""
        if width < 80 or height < 24:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle viewport resize event."""
        self._update_layout(event.size.width, event.size.height)

    def on_mount(self) -> None:
        """Initialize table rows, accessibility announcement, and initial focus."""
        self._update_layout(self.size.width, self.size.height)
        self._populate_table()
        total = self.report_data.get("total_files", 0)
        pass_cnt = self.report_data.get("verified_pass_count", 0)
        fail_cnt = self.report_data.get("mismatch_fail_count", 0)
        skip_cnt = self.report_data.get("skipped_count", 0)
        self.query_one("#audit-search-input", Input).focus()
        self.announce(
            f"Opened Audit Verification Report dialog. {total} total files: "
            f"{pass_cnt} passed, {fail_cnt} failed, {skip_cnt} skipped."
        )

    def _populate_table(
        self, search_query: str = "", status_filter: str = "ALL"
    ) -> None:
        """Populate DataTable with verification records matching filter criteria."""
        table = self.query_one("#audit-table", DataTable)
        table.clear(columns=True)
        table.add_columns(
            "Status",
            "Filename",
            "Pre-Sort SHA-256",
            "Post-Sort SHA-256",
            "Destination Path",
        )

        query = search_query.strip().lower()
        records = self.report_data.get("records", [])

        for rec in records:
            status = rec.get("status", "")
            filename = rec.get("filename", "")
            rel_dst = rec.get("relative_destination", "") or rec.get(
                "destination_path", ""
            )
            pre_h = rec.get("pre_hash", "") or ""
            post_h = rec.get("post_hash", "") or ""

            if status_filter != "ALL" and status != status_filter:
                continue

            if query:
                match_fn = query in filename.lower()
                match_src = query in rec.get("relative_source", "").lower()
                match_dst = query in rel_dst.lower()
                match_hash = query in pre_h.lower() or query in post_h.lower()
                if not (match_fn or match_src or match_dst or match_hash):
                    continue

            badge = rec.get("status_badge", f"[{status}]")
            pre_trunc = (pre_h[:12] + "...") if len(pre_h) > 12 else (pre_h or "N/A")
            post_trunc = (
                (post_h[:12] + "...") if len(post_h) > 12 else (post_h or "N/A")
            )

            table.add_row(badge, filename, pre_trunc, post_trunc, rel_dst)

    @on(Input.Changed, "#audit-search-input")
    def on_search_changed(self, event: Input.Changed) -> None:
        """Handle search input filter update."""
        status_val = self.query_one("#audit-status-filter", Select).value
        self._populate_table(search_query=event.value, status_filter=str(status_val))

    @on(Select.Changed, "#audit-status-filter")
    def on_status_changed(self, event: Select.Changed) -> None:
        """Handle status filter dropdown update."""
        search_val = self.query_one("#audit-search-input", Input).value
        self._populate_table(search_query=search_val, status_filter=str(event.value))

    def action_export_json(self) -> None:
        """Export audit report to JSON format."""
        self._export_report("json")

    def action_export_csv(self) -> None:
        """Export audit report to CSV format."""
        self._export_report("csv")

    def _export_report(self, fmt: str) -> None:
        """Export report to disk with error handling."""
        base_dir = self.report_data.get("base_dir", ".") or "."
        out_name = f"audit_report.{fmt}"
        out_path = os.path.join(base_dir, out_name)

        try:
            from app.core.audit_reporter import export_audit_report

            final_path = export_audit_report(
                self.report_data, out_path, format_type=fmt
            )
            msg = f"Audit report successfully exported to {final_path}"
            self.announce(msg)
            self.notify(msg, severity="information")
        except Exception as err:
            err_msg = f"Failed to export audit report: {err}"
            logger.error(err_msg)
            self.announce(err_msg)
            self.notify(err_msg, severity="error")

    def action_cancel(self) -> None:
        """Cancel dialog and dismiss modal."""
        self.announce("Closed verification report dialog.")
        self.dismiss()

    @on(Button.Pressed, "#btn-export-json")
    def on_btn_export_json(self) -> None:
        """Handle JSON export button press."""
        self.action_export_json()

    @on(Button.Pressed, "#btn-export-csv")
    def on_btn_export_csv(self) -> None:
        """Handle CSV export button press."""
        self.action_export_csv()

    @on(Button.Pressed, "#btn-close")
    def on_btn_close(self) -> None:
        """Handle Close button press."""
        self.action_cancel()


class HistoryModal(A11yMixin, ModalScreen[Optional[Dict[str, Any]]]):
    """Modal dialog for viewing session history records and exporting audit logs."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    HistoryModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
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

    def __init__(
        self,
        sessions: Optional[List[Dict[str, Any]]] = None,
        base_dir: str = "",
    ):
        super().__init__()
        if sessions is None:
            from app.main import find_all_history_sessions

            sessions = find_all_history_sessions()
        self.sessions = sessions or []
        self.base_dir = base_dir

    def compose(self) -> ComposeResult:
        """Compose history view and export modal widgets."""
        with Vertical(classes="modal-box"):
            yield Label(
                "Session History & Audit Log Export [Ctrl+H]", classes="modal-title"
            )
            yield Label(
                "Select a session row to export activity audit log:",
                classes="modal-subtitle",
            )

            options = []
            for s in self.sessions:
                sid = s.get("session_id", "unknown")
                b_dir = s.get("base_dir") or "N/A"
                status = s.get("status") or "completed"
                options.append((f"{sid[:8]}... | {status} | {b_dir}", sid))

            if not options:
                options = [("No historical sessions found", "none")]

            select_sess = Select(
                options,
                value=options[0][1] if options else "none",
                id="select-history-session",
                allow_blank=False,
            )
            select_sess.tooltip = "Select historical session row for audit log export"
            yield select_sess

            yield Label("Output File Path:")
            inp_path = Input(
                value="./audit_log.csv",
                placeholder="Enter export path (e.g. ./audit_log.csv)...",
                id="input-history-path",
            )
            inp_path.tooltip = "Enter target output path for exported session audit log file"
            yield inp_path

            yield Label("Export Format:")
            fmt_select = Select(
                [("CSV Format (*.csv)", "csv"), ("JSON Format (*.json)", "json")],
                value="csv",
                id="select-history-format",
                allow_blank=False,
            )
            fmt_select.tooltip = "Select audit log output format"
            yield fmt_select

            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel", variant="default")
                btn_cancel.tooltip = "Cancel history export dialog"
                yield btn_cancel
                btn_export = Button(
                    "Export Audit Log", id="btn-export", variant="primary"
                )
                btn_export.tooltip = (
                    "Export audit log file for selected session history row"
                )
                yield btn_export

    def _update_layout(self, width: int) -> None:
        """Update layout based on viewport width breakpoint."""
        if width < 80:
            self.add_class("narrow")
        else:
            self.remove_class("narrow")

    def on_resize(self, event: events.Resize) -> None:
        """Handle viewport resize event."""
        self._update_layout(event.size.width)

    def on_mount(self) -> None:
        """Focus session select widget on mount and emit accessibility announcement."""
        self._update_layout(self.size.width)
        self.query_one("#select-history-session", Select).focus()
        self.announce("Opened session history export modal dialog.")

    @on(Button.Pressed, "#btn-export")
    def action_export_audit(self) -> None:
        """Confirm session audit log export and dismiss modal screen."""
        session_val = self.query_one("#select-history-session", Select).value
        if not session_val or session_val == "none":
            self.announce("No session selected for audit log export.")
            return

        path_val = self.query_one("#input-history-path", Input).value.strip()
        if not path_val:
            self.announce("Output file path cannot be empty.")
            return

        format_val = self.query_one("#select-history-format", Select).value or "csv"

        self.dismiss(
            {
                "session_id": str(session_val),
                "output_path": path_val,
                "format": str(format_val),
            }
        )

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel history export modal."""
        self.dismiss(None)


class VimTree(Tree):
    """Tree control with native vim motion navigation (h, j, k, l)."""

    def on_key(self, event: events.Key) -> None:
        """Handle key events for native vim tree navigation."""
        if not self.has_focus:
            return

        if event.key == "j":
            self.action_cursor_down()
            event.stop()
            event.prevent_default()
        elif event.key == "k":
            self.action_cursor_up()
            event.stop()
            event.prevent_default()
        elif event.key == "h":
            node = self.cursor_node
            if node:
                if node.is_expanded and node.children:
                    node.collapse()
                else:
                    self.action_cursor_parent()
            event.stop()
            event.prevent_default()
        elif event.key == "l":
            node = self.cursor_node
            if node:
                if not node.is_expanded and (node.children or node.allow_expand):
                    node.expand()
                elif node.is_expanded and node.children:
                    self.action_cursor_down()
            event.stop()
            event.prevent_default()


class AutoSorterTUI(A11yMixin, App):
    """Textual full-screen interactive TUI application for Sortify AI Pro."""

    TITLE = "Sortify AI Pro - Terminal UI"
    SUB_TITLE = "Interactive Tree & Modal Controls"

    BINDINGS = [
        Binding("ctrl+l", "toggle_lock", "Lock/Unlock", show=True),
        Binding("ctrl+r", "rename_node", "Rename Node", show=True),
        Binding("ctrl+n", "new_folder", "New Folder", show=True),
        Binding("plus", "rate_positive", "Rating (+)", show=True),
        Binding("minus", "rate_negative", "Rating (-)", show=True),
        Binding("ctrl+o", "open_settings", "Settings", show=True),
        Binding("ctrl+m", "open_model_manager", "Model Manager", show=True),
        Binding("ctrl+w", "open_wizard", "Wizard", show=True),
        Binding("ctrl+c", "open_cro_forensic", "CRO Ingest", show=True),
        Binding("ctrl+d", "open_dropzone", "DropZone", show=True),
        Binding("ctrl+s", "scan_directory", "Scan", show=True),
        Binding("ctrl+h", "open_history", "History View", show=True),
        Binding("ctrl+e", "export_simulation_report", "Export Report", show=True),
        Binding("ctrl+a", "open_audit_report", "Audit Report", show=True),
        Binding("enter", "execute_sort", "Execute", show=False),
        Binding("ctrl+b", "select_dir", "Browse Dir", show=True),
        Binding("g", "generate_sample_corpus", "Sample Dataset", show=False),
        Binding("question_mark", "open_cheat_sheet", "Help (?)", show=True),
        Binding("f1", "open_cheat_sheet", "Help (F1)", show=False),
        Binding("ctrl+q", "quit", "Quit", show=True),
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
    #main-dual-pane.narrow {
        layout: vertical;
    }
    #left-tree-pane, #plan-tree {
        width: 50%;
        height: 100%;
        border: solid $primary;
        padding: 0;
    }
    #main-dual-pane.narrow #left-tree-pane, #main-dual-pane.narrow #plan-tree {
        width: 100%;
        height: 1fr;
    }
    #right-meta-pane {
        width: 50%;
        height: 100%;
        border: solid $secondary;
        padding: 1 2;
    }
    #main-dual-pane.narrow #right-meta-pane {
        width: 100%;
        height: 1fr;
    }
    .tui-log-area {
        height: 1fr;
        max-height: 10;
        min-height: 3;
        margin-top: 1;
        border: solid $secondary;
    }
    #main-progress-bar {
        width: 100%;
        height: 1;
        margin: 0;
        padding: 0;
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
    Tree:focus, Static:focus, Log:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(
        self,
        settings,
        base_dir: Optional[str] = None,
        skip_wizard: bool = False,
        non_interactive: bool = False,
    ):
        super().__init__()
        self.settings = settings
        if base_dir and str(base_dir).startswith("/"):
            self.base_dir = str(base_dir)
        elif base_dir:
            self.base_dir = os.path.abspath(base_dir)
        else:
            self.base_dir = ""
        self.skip_wizard = skip_wizard
        self.non_interactive = non_interactive
        self.plan: Dict[str, Any] = {}
        self.locked_files: Dict[str, str] = {}
        self._ratings_cache: Dict[str, str] = {}
        self.app_session = None
        self.active_tree_node = None

    def compose(self) -> ComposeResult:
        """Compose main dual-pane TUI layout."""
        yield Header(show_clock=True)
        with Horizontal(id="main-dual-pane"):
            tree = VimTree("Proposed Organization Plan", id="plan-tree")
            tree.tooltip = "Interactive tree view of proposed file organization plan"
            yield tree
            with Vertical(id="right-meta-pane"):
                yield Label("Node Metadata Inspector", classes="meta-header")
                meta_widget = Static(
                    "Select a node in the tree to inspect details.", id="meta-details"
                )
                meta_widget.tooltip = "Metadata inspector panel displaying attributes of selected file or folder"
                yield meta_widget
                yield Label("Live Execution Log", classes="meta-header")
                tui_log = Log(id="tui-log", classes="tui-log-area")
                tui_log.tooltip = "Live operation execution log output feed"
                yield tui_log
        pb = ProgressBar(total=100, show_percentage=True, id="main-progress-bar")
        pb.tooltip = "Active operation background progress indicator"
        pb.display = False
        yield pb
        initial_status = f"{self._get_ai_status_badge()} Ready. Press [Ctrl+S] to Scan or [Ctrl+B] to select Directory."
        sb = Static(
            initial_status,
            id="status-bar",
        )
        sb.tooltip = "Application status and screen reader announcement bar"
        yield sb
        yield Footer()

    def announce(
        self,
        message: str,
        priority: str = "polite",
        help_url: Optional[str] = None,
    ) -> str:
        """Emit screen reader announcement and log to live execution log feed."""
        res = super().announce(message, priority=priority, help_url=help_url)
        try:
            log_w = self.query_one("#tui-log", Log)
            display_msg = f"{message} [Help: {help_url}]" if help_url else message
            log_w.write_line(display_msg)
        except Exception:
            pass
        return res

    def _handle_progress_update(
        self,
        update_or_prog: Any = 0.0,
        stage: Optional[str] = None,
        unit_count: Optional[int] = None,
        unit_type: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        """Handle progress updates dispatched from worker threads to main UI thread."""
        try:
            pb = self.query_one("#main-progress-bar", ProgressBar)
            pb.display = True

            progress_val = 0.0
            stage_text = stage
            count_val = unit_count

            if hasattr(update_or_prog, "progress"):
                progress_val = getattr(update_or_prog, "progress", 0.0)
                if stage_text is None:
                    stage_text = getattr(update_or_prog, "stage", None)
                if count_val is None:
                    count_val = getattr(update_or_prog, "unit_count", None)
                if unit_type is None:
                    unit_type = getattr(update_or_prog, "unit_type", None)
            elif isinstance(update_or_prog, (int, float)):
                progress_val = float(update_or_prog)
            elif isinstance(update_or_prog, str):
                stage_text = update_or_prog

            # Convert 0.0-1.0 ratio to percentage (0.0-100.0) if needed
            if 0.0 <= progress_val <= 1.0:
                pct = progress_val * 100.0
            else:
                pct = progress_val

            pct = min(100.0, max(0.0, pct))
            pb.progress = pct

            if stage_text:
                status_msg = f"[{pct:.0f}%] {stage_text}"
                if count_val is not None:
                    u_type = unit_type or "items"
                    status_msg = f"[{pct:.0f}%] ({count_val} {u_type}) {stage_text}"
                sb = self.query_one("#status-bar", Static)
                sb.update(f"{self._get_ai_status_badge()} {status_msg}")
                try:
                    log_w = self.query_one("#tui-log", Log)
                    log_w.write_line(status_msg)
                except Exception:
                    pass
        except Exception as e:
            logger.debug(f"Error handling progress update: {e}")

    def reset_progress_bar(self) -> None:
        """Reset progress bar to idle state and hide component."""
        try:
            pb = self.query_one("#main-progress-bar", ProgressBar)
            pb.progress = 0.0
            pb.total = 100
            pb.display = False
        except Exception:
            pass

    def _update_layout(self, width: int) -> None:
        """Update container CSS classes based on viewport width breakpoint."""
        try:
            dual_pane = self.query_one("#main-dual-pane")
            if width < 100:
                dual_pane.add_class("narrow")
            else:
                dual_pane.remove_class("narrow")
        except Exception:
            pass

    def on_resize(self, event: events.Resize) -> None:
        """Handle viewport resize lifecycle event to update dual-pane container layout."""
        self._update_layout(event.size.width)

    def on_mount(self) -> None:
        """Mount event handler."""
        self._update_layout(self.size.width)
        try:
            from app.ui.notifications import NotificationManager

            NotificationManager.get_instance().register_tui(self)
        except Exception:
            pass
        try:
            from app.config import AppSettings

            AppSettings.add_observer("AI_CONSENT_GRANTED", self._on_ai_consent_changed)
        except Exception:
            pass
        try:
            tree = self.query_one("#plan-tree", Tree)
            tree.show_root = True
            tree.root.expand()
        except Exception:
            pass
        self.announce("Sortify AI Pro TUI initialized and ready.")
        self.check_abandoned_sessions()

        env_non_interactive = os.environ.get("NON_INTERACTIVE")
        is_non_interactive_env = bool(
            env_non_interactive
            and env_non_interactive.strip().lower() in ("1", "true", "yes", "on")
        )
        env_consent = os.environ.get("SORTIFY_AI_CONSENT")
        if env_consent is not None:
            env_consent_clean = env_consent.strip().lower()
            if env_consent_clean in ("1", "true", "yes", "on"):
                self.settings.AI_CONSENT_GRANTED = True
            elif env_consent_clean in ("0", "false", "no", "off"):
                self.settings.AI_CONSENT_GRANTED = False

        is_bypass = (
            self.skip_wizard
            or self.non_interactive
            or getattr(self.settings, "_skip_wizard", False)
            or getattr(self.settings, "_non_interactive", False)
            or is_non_interactive_env
        )

        if getattr(self.settings, "AI_CONSENT_GRANTED", None) is None:
            if is_bypass:
                self.settings.AI_CONSENT_GRANTED = False
                if hasattr(self.settings, "_save"):
                    try:
                        self.settings._save()
                    except Exception:
                        pass
            else:
                self.call_after_refresh(self.action_open_wizard)

    def on_unmount(self) -> None:
        """Lifecycle hook called when application is unmounted."""
        try:
            from app.ui.notifications import NotificationManager

            NotificationManager.get_instance().unregister_tui(self)
        except Exception:
            pass
        try:
            from app.config import AppSettings

            AppSettings.remove_observer("AI_CONSENT_GRANTED", self._on_ai_consent_changed)
        except Exception:
            pass

    @work
    async def check_abandoned_sessions(self) -> None:
        """Scan for abandoned sessions on startup and prompt for recovery if detected."""
        try:
            from app.core.session import scan_abandoned_sessions_async

            abandoned = await scan_abandoned_sessions_async()
            if not abandoned:
                return

            session_info = abandoned[0]

            def on_recovery_choice(choice: Optional[str]) -> None:
                if not choice:
                    return
                if choice == "resume":
                    self._do_recovery_resume(session_info)
                elif choice == "rollback":
                    self._do_recovery_rollback(session_info)
                elif choice == "clean":
                    self._do_recovery_clean(session_info)

            self.push_screen(SessionRecoveryModal(session_info), on_recovery_choice)
        except Exception as e:
            logger.error(f"Error checking abandoned sessions: {e}")

    def _do_recovery_resume(self, session_info: Dict[str, Any]) -> None:
        """Resume interrupted sorting operation."""
        import json

        from app.core.session import AppSession

        base_dir = session_info.get("base_dir") or self.base_dir
        if base_dir:
            self.base_dir = base_dir

        self.app_session = AppSession(
            self.settings, self.base_dir, session_id=session_info.get("session_id")
        )

        plan_path = session_info.get("plan_path")
        if plan_path and os.path.exists(plan_path):
            try:
                with open(plan_path, "r", encoding="utf-8") as f:
                    self.plan = json.load(f)
            except Exception:
                self.plan = self.app_session.generate_sorting_plan()
        else:
            self.plan = self.app_session.generate_sorting_plan()

        self.rebuild_tree()
        self.announce(
            f"Resuming session '{session_info.get('session_id')}'. Executing pending moves..."
        )
        self.action_execute_sort()

    def _do_recovery_rollback(self, session_info: Dict[str, Any]) -> None:
        """Rollback interrupted sorting operation."""
        import shutil

        from app.core.session import AppSession

        base_dir = session_info.get("base_dir") or self.base_dir
        session_id = session_info.get("session_id")
        if base_dir and session_id:
            try:
                temp_session = AppSession(
                    self.settings, base_dir, session_id=session_id
                )
                if hasattr(temp_session, "rollback"):
                    temp_session.rollback(session_id, True)
                elif hasattr(temp_session, "history_manager"):
                    temp_session.history_manager.unwind_session(
                        session_id, temp_session.db
                    )
                temp_session.close()
            except Exception as e:
                logger.error(f"Error during session rollback: {e}")

        session_dir = session_info.get("session_dir")
        if session_dir and os.path.exists(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)

        self.announce(f"Rolled back session '{session_id}'.")
        if self.base_dir:
            self.action_scan_directory()

    def _do_recovery_clean(self, session_info: Dict[str, Any]) -> None:
        """Clean abandoned session files."""
        import shutil

        session_dir = session_info.get("session_dir")
        if session_dir and os.path.exists(session_dir):
            shutil.rmtree(session_dir, ignore_errors=True)
        self.announce(f"Cleaned session files for '{session_info.get('session_id')}'.")

    def _get_ai_status_badge(self) -> str:
        """Return visual AI consent status badge string."""
        consent = getattr(self.settings, "AI_CONSENT_GRANTED", None)
        return "[AI: Active]" if consent is True else "[AI: Disabled]"

    def _on_ai_consent_changed(self, value: Any = None) -> None:
        """Handle AI_CONSENT_GRANTED setting change notification."""
        self.update_status()

    def update_status(self, text: Optional[str] = None) -> None:
        """Update status bar label with current message and AI consent badge."""
        try:
            if text is not None:
                clean_text = str(text)
                for badge_str in ("[AI: Active]", "[AI: Disabled]"):
                    if clean_text.startswith(badge_str):
                        clean_text = clean_text[len(badge_str) :].lstrip()
                self._current_status_text = clean_text
            elif not hasattr(self, "_current_status_text"):
                self._current_status_text = (
                    "Ready. Press [Ctrl+S] to Scan or [Ctrl+B] to select Directory."
                )

            badge = self._get_ai_status_badge()
            full_text = (
                f"{badge} {self._current_status_text}"
                if getattr(self, "_current_status_text", "")
                else badge
            )
            sb = self.query_one("#status-bar", Static)
            sb.update(full_text)
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

    def _is_text_control_focused(self) -> bool:
        """Check if a text input or selection control currently holds focus."""
        focused = self.focused
        return focused is not None and isinstance(focused, (Input, Select))

    # --- Actions ---

    def action_select_dir(self) -> None:
        """Open directory selection modal."""
        if self._is_text_control_focused():
            return

        def on_selected(path: Optional[str]) -> None:
            if path and os.path.exists(path):
                self.base_dir = os.path.abspath(path)
                msg = f"Selected directory: {self.base_dir}"
                self.announce(msg)
                self.action_scan_directory()

        self.push_screen(DirectorySelectModal(current_dir=self.base_dir), on_selected)

    def action_generate_sample_corpus(self) -> None:
        """Generate sample document corpus in active workspace directory [G]."""
        if self._is_text_control_focused():
            return

        target_dir = self.base_dir or os.getcwd()
        self.base_dir = target_dir

        from app.core.sample_corpus import generate_sample_corpus

        try:
            generate_sample_corpus(target_dir, overwrite=True)
            self.announce(f"Generated sample dataset in {target_dir}.")
            self.action_scan_directory()
        except Exception as exc:
            logger.error(f"Error generating sample dataset: {exc}")
            self.announce(f"Failed to generate sample dataset: {exc}")

    def action_open_settings(self) -> None:
        """Open settings modal screen."""
        if self._is_text_control_focused():
            return

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
                if "MAX_FOLDERS" in res:
                    try:
                        self.settings.MAX_FOLDERS = res["MAX_FOLDERS"]
                    except Exception:
                        pass
                if "SORTING_STRATEGY" in res:
                    self.settings.SORTING_STRATEGY = res["SORTING_STRATEGY"]
                if "CLINICAL_SMART_RENAMING" in res:
                    self.settings.CLINICAL_SMART_RENAMING = res[
                        "CLINICAL_SMART_RENAMING"
                    ]
                if "CONTEXTUAL_RENAMING" in res:
                    self.settings.CONTEXTUAL_RENAMING = res["CONTEXTUAL_RENAMING"]
                if "AI_CONSENT_GRANTED" in res:
                    self.settings.AI_CONSENT_GRANTED = res["AI_CONSENT_GRANTED"]
                if hasattr(self.settings, "_save"):
                    self.settings._save()
                self.update_status()
                self.announce("Settings updated and saved.")
                if self.base_dir:
                    self.action_scan_directory()

        self.push_screen(SettingsModal(self.settings), on_saved)

    def action_open_model_manager(self) -> None:
        """Open model manager modal screen."""
        if self._is_text_control_focused():
            return
        self.push_screen(ModelManagerModal(self.settings))

    def action_open_wizard(self) -> None:
        """Open model onboarding wizard modal screen."""
        if self._is_text_control_focused():
            return
        self.push_screen(WizardModal(self.settings))

    def action_open_cro_forensic(self) -> None:
        """Open CRO multi-study forensic ingestion modal screen."""
        if self._is_text_control_focused():
            return
        from app.core.plugin_registry import PluginRegistry

        reg = PluginRegistry.get_instance()
        reg.load_plugins_from_settings(self.settings)
        view_cls = reg.get_tui_view("cro_forensic")
        if view_cls:
            self.push_screen(view_cls(self.settings, base_dir=self.base_dir))
        else:
            self.announce("CRO Forensic ingestion plugin is not enabled.")

    def action_open_cheat_sheet(self) -> None:
        """Open keyboard shortcut cheat sheet modal screen."""
        if self._is_text_control_focused():
            return
        self.push_screen(ShortcutCheatSheetModal())

    def action_open_dropzone(self) -> None:
        """Open floating dropzone overlay modal screen."""
        if self._is_text_control_focused():
            return

        def on_drop_done(res: Optional[Dict[str, Any]]) -> None:
            if res:
                cnt = res.get("processed_count", 0)
                self.announce(f"DropZone triage finished: {cnt} item(s) organized.")
                if self.base_dir and os.path.exists(self.base_dir):
                    self.action_scan_directory()

        self.push_screen(
            DropZoneModal(settings=self.settings, base_dir=self.base_dir),
            on_drop_done,
        )

    def action_scan_directory(self) -> None:
        """Trigger directory scanning background worker."""
        if self._is_text_control_focused():
            return
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

            def scan_progress_cb(update_or_prog: Any, stage: Optional[str] = None, **kwargs: Any) -> None:
                self.call_from_thread(self._handle_progress_update, update_or_prog, stage, **kwargs)

            MetadataPass.run(
                self.base_dir,
                files,
                self.settings,
                self.app_session.db,
                progress_callback=scan_progress_cb,
                cancel_check=lambda: False,
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
        finally:
            self.call_from_thread(self.reset_progress_bar)

    def action_export_simulation_report(self) -> None:
        """Trigger dry-run simulation report export modal screen."""
        if self._is_text_control_focused():
            return
        if not self.plan or not self.base_dir:
            self.announce("No plan available to export. Run [Ctrl+S] Scan first.")
            return

        default_out = str(Path(self.base_dir) / "simulation_report.html")

        def handle_export_modal_result(result: Optional[Tuple[str, str]]) -> None:
            if not result:
                return
            export_path, report_format = result
            try:
                from app.core.simulation_exporter import SimulationExporter

                exporter = SimulationExporter(self.plan, self.base_dir)
                if report_format == "json":
                    exporter.export_json(export_path)
                else:
                    exporter.export_html(export_path)

                self.announce(f"Simulation report exported to '{export_path}'.")
            except Exception as e:
                logger.error(f"Error exporting simulation report: {e}")
                self.announce(f"Export error: {e}")

        self.push_screen(
            ExportReportModal(default_path=default_out, default_format="html"),
            handle_export_modal_result,
        )

    def action_export_report(self) -> None:
        """Alias for action_export_simulation_report."""
        self.action_export_simulation_report()

    def action_open_history(self) -> None:
        """Trigger session history view and audit log export modal screen."""
        if self._is_text_control_focused():
            return
        from app.main import find_all_history_sessions

        sessions = find_all_history_sessions()

        def handle_history_modal_result(
            result: Optional[Dict[str, Any]]
        ) -> None:
            if not result:
                return
            session_id = result["session_id"]
            output_path = result["output_path"]
            fmt = result["format"]

            try:
                target_db_path = None
                for s in sessions:
                    if s.get("session_id") == session_id:
                        target_db_path = s.get("history_db_path")
                        break
                if not target_db_path:
                    from app.config import get_app_dir

                    target_db_path = str(get_app_dir() / "history.db")

                from app.config import get_app_dir
                from app.core.cache import CacheManager
                from app.core.db import Database
                from app.core.db_worker import DBWorker
                from app.core.history import HistoryManager

                worker = DBWorker()
                db = Database(get_app_dir() / "autosorter.db", worker)
                cache_mgr = CacheManager(str(get_app_dir() / "cache.db"), worker)
                history_mgr = HistoryManager(db, cache_mgr, target_db_path)

                export_res = history_mgr.export_audit_log(
                    output_path=output_path,
                    session_id=session_id if session_id != "none" else None,
                    format=fmt,
                )
                cnt = export_res.get("count", 0)
                msg = f"Audit log exported ({fmt.upper()}) to '{output_path}' ({cnt} records)."
                self.announce(msg)
                from app.ui.notifications import notify

                notify(msg, type="info")
            except Exception as e:
                logger.error(f"Error exporting audit log from TUI: {e}")
                self.announce(f"Export error: {e}")

        self.push_screen(
            HistoryModal(sessions=sessions, base_dir=self.base_dir),
            handle_history_modal_result,
        )

    def action_execute_sort(self) -> None:
        """Trigger sorting plan execution worker."""
        if self._is_text_control_focused():
            return
        if not self.plan or not self.app_session:
            self.announce("No plan available to execute. Run [Ctrl+S] Scan first.")
            return

        self.announce("Executing file moves according to plan...")
        self.run_execute_worker()

    @work(exclusive=True, thread=True)
    def run_execute_worker(self) -> None:
        """Execute moves in worker thread."""
        try:
            def move_progress_cb(update_or_prog: Any, stage: Optional[str] = None, **kwargs: Any) -> None:
                self.call_from_thread(self._handle_progress_update, update_or_prog, stage, **kwargs)

            summary = self.app_session.execute_moves(
                self.plan,
                progress_callback=move_progress_cb,
            )
            msg = f"Execution completed successfully! Summary: {summary}"
            self.call_from_thread(self.announce, msg)
            self.call_from_thread(self.action_scan_directory)

            audit_report = (
                summary.get("audit_report") if isinstance(summary, dict) else None
            )
            if not audit_report and self.plan:
                from app.core.audit_reporter import generate_audit_report
                from app.core.verifier import VerificationEngine

                base_dir = self.app_session.base_dir if self.app_session else "."
                moves = VerificationEngine.get_moves(base_dir, self.plan)
                records = [
                    {"source_path": src, "destination_path": dst}
                    for _, src, dst in moves
                ]
                audit_report = generate_audit_report(base_dir, records)

            if audit_report:
                self.call_from_thread(self.action_open_audit_report, audit_report)
        except Exception as e:
            logger.error(f"Error executing moves: {e}")
            self.call_from_thread(self.announce, f"Execution error: {e}")
        finally:
            self.call_from_thread(self.reset_progress_bar)

    def action_open_audit_report(
        self, audit_report: Optional[Dict[str, Any]] = None
    ) -> None:
        """Open the Audit Report Modal Screen."""
        if not audit_report and self.app_session and self.plan:
            from app.core.audit_reporter import generate_audit_report
            from app.core.verifier import VerificationEngine

            base_dir = self.app_session.base_dir or "."
            moves = VerificationEngine.get_moves(base_dir, self.plan)
            records = [
                {"source_path": src, "destination_path": dst} for _, src, dst in moves
            ]
            audit_report = generate_audit_report(base_dir, records)

        if audit_report:
            self.push_screen(AuditReportModal(audit_report))
        else:
            self.announce(
                "No audit report available. Execute a sorting operation first."
            )

    # --- Tree Management & Event Handlers ---

    def _has_file_nodes(self, plan_dict: Dict[str, Any]) -> bool:
        """Check if plan structure contains any document file nodes."""
        if not plan_dict or not isinstance(plan_dict, dict):
            return False
        for k, v in plan_dict.items():
            if isinstance(v, dict):
                if v.get("__type__") == "file":
                    return True
                if self._has_file_nodes(v):
                    return True
        return False

    def rebuild_tree(self) -> None:
        """Rebuild Textual Tree widget from in-memory plan structure."""
        try:
            tree = self.query_one("#plan-tree", Tree)
            tree.reset("Proposed Organization Plan")
            if not self._has_file_nodes(self.plan):
                tree.root.add_leaf(
                    "💡 Empty Workspace: No documents found in target directory",
                    data={"is_empty_state": True, "action": "info"},
                )
                tree.root.add_leaf(
                    "▶ Press [G] to Load Sample Dataset",
                    data={
                        "is_empty_state": True,
                        "action": "generate_sample_corpus",
                        "shortcut": "G",
                    },
                )
                tree.root.add_leaf(
                    "▶ Press [Ctrl+B] to Select Different Directory",
                    data={
                        "is_empty_state": True,
                        "action": "select_dir",
                        "shortcut": "Ctrl+B",
                    },
                )
            else:
                self._build_tree_nodes(self.plan, tree.root, current_folder="")
            tree.root.expand_all()
        except Exception as e:
            logger.error(f"Error rebuilding tree: {e}")

    def _build_tree_nodes(
        self, node_dict: Dict[str, Any], parent_item: TreeNode, current_folder: str
    ) -> None:
        for k, v in sorted(
            node_dict.items(),
            key=lambda x: (
                1 if isinstance(x[1], dict) and x[1].get("__type__") == "file" else 0,
                x[0],
            ),
        ):
            if isinstance(v, dict) and v.get("__type__") == "file":
                file_key = k
                file_info = v
                filepath = file_info.get(
                    "filepath",
                    (Path(self.base_dir) / current_folder / file_key).as_posix()
                    if self.base_dir
                    else (Path(current_folder) / file_key).as_posix(),
                )

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

                routed_by = file_info.get("routed_by")
                if routed_by == "jev_classifier" or file_info.get("is_jev"):
                    label_parts.append("[JEV]")

                sens_rating = file_info.get("sensitivity_rating")
                if sens_rating:
                    label_parts.append(f"[SENS: {str(sens_rating).upper()}]")

                arch_prio = file_info.get("archival_priority")
                if arch_prio is not None:
                    arch_str = (
                        f"P{arch_prio}"
                        if isinstance(arch_prio, int)
                        or (
                            isinstance(arch_prio, str)
                            and not str(arch_prio).upper().startswith("P")
                        )
                        else str(arch_prio).upper()
                    )
                    label_parts.append(f"[ARCH: {arch_str}]")

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
                sub_folder = (
                    (Path(current_folder) / k).as_posix() if current_folder else k
                )
                node_data = {
                    "is_file": False,
                    "key": k,
                    "folder": sub_folder,
                    "info": v,
                }
                child_tree_node = parent_item.add(
                    f"📁 {k}", data=node_data, expand=True
                )
                self._build_tree_nodes(v, child_tree_node, current_folder=sub_folder)

    def _update_inspector(self, node_or_data: Any = None) -> str:
        """Update and return formatted metadata text for the inspector panel."""
        if node_or_data is None:
            node_or_data = self.active_tree_node

        data = None
        if hasattr(node_or_data, "data"):
            data = getattr(node_or_data, "data", None)
        elif isinstance(node_or_data, dict):
            data = node_or_data

        if not data or not isinstance(data, dict):
            msg = "Root folder of sorting plan."
            try:
                meta_widget = self.query_one("#meta-details", Static)
                meta_widget.update(msg)
            except Exception:
                pass
            return msg

        if data.get("is_empty_state"):
            action = data.get("action")
            if action == "generate_sample_corpus":
                msg = "Empty workspace node. Press [G] to load sample dataset into active directory."
            elif action == "select_dir":
                msg = "Empty workspace node. Press [Ctrl+B] to select a different directory."
            else:
                msg = "Empty workspace node. No documents found in target directory."
            self.announce(msg)
            try:
                meta_widget = self.query_one("#meta-details", Static)
                meta_widget.update(msg)
            except Exception:
                pass
            return msg

        lines = []
        if data.get("is_file"):
            key = data.get("key", "")
            folder = data.get("folder") or "(Root)"
            locked = "Yes [LOCKED]" if data.get("is_locked") else "No"
            rating = data.get("rating") or "None"
            info = data.get("info") if isinstance(data.get("info"), dict) else {}

            lines.append(f"[bold accent]File:[/bold accent] {key}")
            lines.append(f"[bold]Path:[/bold] {data.get('filepath')}")
            lines.append(f"[bold]Target Folder:[/bold] {folder}")

            target_fn = info.get("target_filename")
            if target_fn:
                lines.append(f"[bold]Target Filename:[/bold] {target_fn}")

            lines.append(f"[bold]Locked:[/bold] {locked}")
            lines.append(f"[bold]ML Rating:[/bold] {rating}")

            routing_src = info.get("routed_by")
            if routing_src:
                lines.append(f"[bold]Routing Source:[/bold] {routing_src}")

            cat = info.get("category") or info.get("jev_category")
            if cat:
                lines.append(f"[bold]Category:[/bold] {cat}")

            sens_rating = info.get("sensitivity_rating")
            if sens_rating is not None:
                lines.append(f"[bold]Sensitivity Rating:[/bold] {sens_rating}")

            sens_score = info.get("sensitivity_score")
            if sens_score is not None:
                try:
                    lines.append(
                        f"[bold]Sensitivity Score:[/bold] {float(sens_score):.2f}"
                    )
                except (ValueError, TypeError):
                    lines.append(f"[bold]Sensitivity Score:[/bold] {sens_score}")

            arch_prio = info.get("archival_priority")
            if arch_prio is not None:
                arch_str = (
                    f"P{arch_prio}"
                    if isinstance(arch_prio, int)
                    or (
                        isinstance(arch_prio, str)
                        and not str(arch_prio).upper().startswith("P")
                    )
                    else str(arch_prio).upper()
                )
                lines.append(f"[bold]Archival Priority:[/bold] {arch_str}")

            arch_score = info.get("archival_priority_score")
            if arch_score is not None:
                try:
                    lines.append(
                        f"[bold]Archival Priority Score:[/bold] {float(arch_score):.2f}"
                    )
                except (ValueError, TypeError):
                    lines.append(f"[bold]Archival Priority Score:[/bold] {arch_score}")

            conf = info.get("confidence")
            if conf is not None:
                try:
                    lines.append(f"[bold]Confidence:[/bold] {float(conf):.2%}")
                except (ValueError, TypeError):
                    lines.append(f"[bold]Confidence:[/bold] {conf}")

            self.announce(
                f"Selected file '{key}' in folder '{folder}'. Locked: {locked}. Rating: {rating}."
            )
        else:
            key = data.get("key", "")
            folder = data.get("folder", "")
            lines.append(f"[bold accent]Folder Category:[/bold accent] {key}")
            lines.append(f"[bold]Relative Path:[/bold] {folder}")
            self.announce(f"Selected folder category '{key}' at path '{folder}'.")

        text = "\n".join(lines)
        try:
            meta_widget = self.query_one("#meta-details", Static)
            meta_widget.update(text)
        except Exception:
            pass

        return text

    @on(Tree.NodeHighlighted, "#plan-tree")
    @on(Tree.NodeSelected, "#plan-tree")
    def on_node_selected(self, event: Union[Tree.NodeSelected, Tree.NodeHighlighted]) -> None:
        """Handle tree node selection or highlight to update metadata pane and screen reader announcement."""
        self.active_tree_node = event.node
        self._update_inspector(event.node)
        if isinstance(event, Tree.NodeSelected) and event.node and event.node.data:
            if event.node.data.get("is_empty_state"):
                action = event.node.data.get("action")
                if action == "generate_sample_corpus":
                    self.action_generate_sample_corpus()
                elif action == "select_dir":
                    self.action_select_dir()

    # --- Keyboard Action Hotkeys ---

    def action_toggle_lock(self) -> None:
        """Toggle node lock state [Ctrl+L]."""
        if self._is_text_control_focused():
            return
        node = self._get_active_node()
        if not node or not node.data or not node.data.get("is_file"):
            self.announce("Select a file node to toggle lock [Ctrl+L].")
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
                self.app_session.db.set_user_verified_target_path(
                    self.base_dir, file_key, None
                )
            msg = f"Unlocked file '{file_key}'"
        else:
            self.locked_files[file_key] = folder
            data["is_locked"] = True
            if self.app_session:
                self.app_session.db.set_user_verified_target_path(
                    self.base_dir, file_key, folder
                )
            msg = f"Locked file '{file_key}' to folder '{folder}'"

        self.announce(msg)
        self.rebuild_tree()

    def action_rename_node(self) -> None:
        """Rename file or folder category node [Ctrl+R]."""
        if self._is_text_control_focused():
            return
        node = self._get_active_node()
        if not node or not node.data:
            self.announce("Select a file or folder node to rename [Ctrl+R].")
            return

        data = node.data
        is_file = data.get("is_file")
        old_name = data.get("key")

        if is_file:
            stem, ext = os.path.splitext(old_name)
            folder_cat = data.get("folder", "")
            modal = RenameModal(
                title=f"Rename File: {old_name}",
                current_name=stem,
                extension=ext,
                category=folder_cat,
            )

            def on_renamed(new_formatted: Optional[str]) -> None:
                if not new_formatted:
                    return
                if ext and not new_formatted.endswith(ext):
                    new_filename = f"{new_formatted}{ext}"
                else:
                    new_filename = new_formatted

                if new_filename == old_name:
                    return
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
            modal = RenameModal(
                title=f"Rename Folder: {old_name}",
                current_name=old_name,
                category=old_name,
            )

            def on_folder_renamed(new_name: Optional[str]) -> None:
                if not new_name or new_name == old_name:
                    return
                if old_name in self.plan:
                    self.plan[new_name] = self.plan.pop(old_name)
                    self.rebuild_tree()
                    self.announce(
                        f"Renamed folder category '{old_name}' -> '{new_name}'"
                    )

            self.push_screen(modal, on_folder_renamed)

    def action_new_folder(self) -> None:
        """Create a new folder category node [Ctrl+N]."""
        if self._is_text_control_focused():
            return

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
        if self._is_text_control_focused():
            return
        self._set_rating_for_selected("positive")

    def action_rate_negative(self) -> None:
        """Set negative ML rating feedback [-] for selected node."""
        if self._is_text_control_focused():
            return
        self._set_rating_for_selected("negative")

    def action_quit(self) -> None:
        """Quit application [Ctrl+Q]."""
        if self._is_text_control_focused():
            return
        super().action_quit()

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
            self.app_session.db.set_document_rating(
                self.base_dir, filepath, rating_to_set
            )

        data["rating"] = rating_to_set
        self.rebuild_tree()
        msg = f"Set rating '{rating_to_set or 'cleared'}' for '{data['key']}'"
        self.announce(msg)


def run_tui(
    settings,
    base_dir: Optional[str] = None,
    skip_wizard: bool = False,
    non_interactive: bool = False,
) -> None:
    """Run the Textual full-screen terminal interface."""
    import shutil

    from app.core.path_utils import is_packaged

    if sys.platform == "win32" and is_packaged():
        if (
            sys.stdin is None
            or not hasattr(sys.stdin, "isatty")
            or not sys.stdin.isatty()
        ):
            try:
                import ctypes

                if ctypes.windll.kernel32.AllocConsole():
                    try:
                        sys.stdout = open("CONOUT$", "w", encoding="utf-8")
                    except Exception:
                        pass
                    try:
                        sys.stderr = open("CONERR$", "w", encoding="utf-8")
                    except Exception:
                        pass
                    try:
                        sys.stdin = open("CONIN$", "r", encoding="utf-8")
                    except Exception:
                        pass
            except Exception:
                pass

    if (
        sys.stdin is None
        or not hasattr(sys.stdin, "isatty")
        or not sys.stdin.isatty()
        or (hasattr(sys.stdout, "isatty") and not sys.stdout.isatty())
    ):
        if not os.environ.get("FORCE_TUI"):
            print(
                "Error: Textual TUI requires an interactive TTY terminal environment.",
                file=sys.stderr,
            )
            sys.exit(1)

    cols, lines = shutil.get_terminal_size((80, 24))
    if (cols < 80 or lines < 24) and not os.environ.get("IGNORE_TERMINAL_SIZE"):
        print(
            f"Error: Terminal dimensions ({cols}x{lines}) are below minimum requirement (80x24). "
            "Please resize your terminal window.",
            file=sys.stderr,
        )
        sys.exit(1)

    app = AutoSorterTUI(
        settings=settings,
        base_dir=base_dir,
        skip_wizard=skip_wizard,
        non_interactive=non_interactive,
    )
    app.run()


@dataclass
class A11yViolation:
    """Representation of an accessibility violation in a TUI component."""

    rule_id: str
    component_id: str
    component_name: str
    viewport_name: str
    viewport_width: int
    locator: str
    message: str


def inspect_tui_component(component: Any) -> List[A11yViolation]:
    """Inspect a TUI App or Modal component for WCAG 2.1 accessibility compliance.

    Leverages the component's internal audit hook method 'audit_a11y_compliance'
    to perform programmatic verification.
    """
    violations: List[A11yViolation] = []
    comp_name = type(component).__name__

    if hasattr(component, "audit_a11y_compliance") and callable(
        component.audit_a11y_compliance
    ):
        res = component.audit_a11y_compliance()
        for v in res.get("violations", []):
            violations.append(
                A11yViolation(
                    rule_id=v.get("rule", "A11Y_UNKNOWN"),
                    component_id=comp_name,
                    component_name=comp_name,
                    viewport_name="terminal",
                    viewport_width=80,
                    locator=v.get("widget_id", comp_name),
                    message=v.get("message", "A11y violation detected"),
                )
            )

    return violations


def __getattr__(name: str) -> Any:
    """Lazy module attribute getter for dynamic extension component resolution."""
    if name == "CROForensicModal":
        from app.plugins.clinical_compliance.tui_views import CROForensicModal

        return CROForensicModal
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")
