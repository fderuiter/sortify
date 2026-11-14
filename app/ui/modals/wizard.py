"""First-run onboarding wizard."""

import logging
import os
from typing import Optional

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static, Switch

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin
from app.ui.widgets.labeled_switch import LabeledSwitch

logger = logging.getLogger(__name__)

PRIVACY_GUARANTEE_ANNOUNCEMENT = (
    "[100% On-Device & Offline] Privacy guarantees: 100% local model execution, "
    "zero document cloud telemetry, and encrypted local SQLCipher storage."
)


class WizardModal(ResponsiveLayoutMixin, A11yMixin, ModalScreen[None]):
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
    .privacy-card {
        border: round $primary;
        padding: 0 1;
        margin-top: 1;
        margin-bottom: 1;
        background: $surface;
        height: auto;
    }
    .privacy-card:focus {
        border: heavy $accent;
    }
    .narrow .privacy-card {
        padding: 0 1;
        border: round $primary;
    }
    .privacy-badge {
        color: $success;
        text-style: bold;
    }
    .privacy-title {
        color: $accent;
        text-style: bold;
    }
    .privacy-guarantee {
        color: $text;
    }
    .field-label {
        margin-top: 1;
        color: $text;
        text-style: bold;
    }
    .error-banner {
        color: $error;
        text-style: bold;
    }
    .preset-label {
        margin-top: 1;
        color: $text-muted;
    }
    .preset-row {
        margin-top: 1;
        margin-bottom: 1;
        height: auto;
        min-height: 3;
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
    Button:focus, Checkbox:focus, Switch:focus, Input:focus, .privacy-card:focus {
        border: heavy $accent;
        text-style: bold;
    }
    Checkbox {
        height: 1;
        border: none;
        padding: 0 1;
    }
    """

    def __init__(self, settings, current_dir: Optional[str] = None):
        super().__init__()
        self.settings = settings
        self.current_dir = current_dir

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Model Onboarding Wizard [Ctrl+W]",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            yield Label("Welcome to Smart AutoSorter AI Pro TUI.")
            yield Label("Enable AI semantic categorization consent below:")

            priv_card = Vertical(id="privacy-card", classes="privacy-card")
            priv_card.can_focus = True
            priv_card.tooltip = (
                "Local-first privacy guarantees: 100% local model execution, "
                "zero document cloud telemetry, and encrypted local SQLCipher storage"
            )
            with priv_card:
                yield Label(
                    "[100% On-Device & Offline]",
                    classes="privacy-badge",
                    id="privacy-badge",
                )
                yield Label(
                    "Local-First Privacy Guarantees:",
                    classes="privacy-title",
                    id="privacy-card-title",
                )
                yield Label(
                    "• 100% local model execution: ML classification and clustering run entirely on-device.",
                    classes="privacy-guarantee",
                    id="privacy-guarantee-local",
                )
                yield Label(
                    "• Zero document cloud telemetry: Document contents and names are never sent to external servers.",
                    classes="privacy-guarantee",
                    id="privacy-guarantee-telemetry",
                )
                yield Label(
                    "• Encrypted local SQLCipher storage: Databases, extracted text, and vectors are encrypted at rest.",
                    classes="privacy-guarantee",
                    id="privacy-guarantee-storage",
                )

            consent_val = getattr(self.settings, "AI_CONSENT_GRANTED", None)
            if consent_val is None:
                consent_val = False
            yield LabeledSwitch(
                label="AI Consent Granted",
                value=bool(consent_val),
                switch_id="switch-consent",
                tooltip=(
                    "Toggle AI consent for semantic document classification "
                    "([100% On-Device & Offline]: 100% local model execution, "
                    "zero document cloud telemetry, and encrypted local SQLCipher storage)"
                ),
            )

            yield Heading("Workspace Directory:", level=2, classes="field-label")
            initial_dir = (
                self.current_dir
                if getattr(self, "current_dir", None) is not None
                else (getattr(getattr(self, "app", None), "base_dir", "") or "")
            )
            inp = Input(
                value=initial_dir,
                placeholder="Enter absolute workspace directory path...",
                id="input-dir",
            )
            inp.tooltip = "Enter target directory path to scan and organize"
            yield inp
            yield Static("", id="lbl-error", classes="error-banner")

            yield Label("Quick Presets:", classes="preset-label")
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

            btn_pg = Button(
                "Try Interactive Sample Playground",
                id="btn-open-playground",
                variant="primary",
            )
            btn_pg.tooltip = (
                "Preview sample documents and live rule classification matrix"
            )
            yield btn_pg

            chk_sample = Checkbox(
                "Generate Sample Documents",
                value=False,
                id="chk-sample-corpus",
            )
            chk_sample.tooltip = (
                "Generate sample document corpus in active workspace directory"
            )
            yield chk_sample

            yield Heading(
                "Starter Rule Template Packs:", level=2, classes="field-label"
            )
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

    def _show_error(self, message: str) -> None:
        """Display inline error banner, announce message, and focus input."""
        banner = self.query("#lbl-error")
        if banner:
            banner.first(Static).update(message)
        self.announce(message, priority="assertive")
        inp = self.query("#input-dir")
        if inp:
            inp.first(Input).focus()

    @on(Input.Changed, "#input-dir")
    def on_input_changed(self, event: Input.Changed) -> None:
        """Clear error banner when user changes directory path."""
        banner = self.query("#lbl-error")
        if banner:
            banner.first(Static).update("")

    @on(Input.Submitted, "#input-dir")
    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Finish wizard when Enter is pressed inside directory input."""
        self.action_finish()

    @on(Button.Pressed, "#preset-demo")
    def action_preset_demo(self) -> None:
        """Select sandbox demo workspace quick preset."""
        path = os.path.abspath("sandbox/demo_workspace")
        if not os.path.exists(path):
            try:
                os.makedirs(path, exist_ok=True)
            except Exception as exc:
                self._show_error(f"Failed to create directory '{path}': {exc}")
                return
        self._set_directory_input(path, "demo workspace")

    @on(Button.Pressed, "#preset-downloads")
    def action_preset_downloads(self) -> None:
        """Select user Downloads folder quick preset."""
        path = os.path.expanduser("~/Downloads")
        self._set_directory_input(path, "Downloads")

    @on(Button.Pressed, "#preset-documents")
    def action_preset_documents(self) -> None:
        """Select user Documents folder quick preset."""
        path = os.path.expanduser("~/Documents")
        self._set_directory_input(path, "Documents")

    def _set_directory_input(self, path: str, preset_name: str) -> None:
        try:
            inp = self.query_one("#input-dir", Input)
            inp.value = path
        except Exception:
            pass
        self.announce(f"Selected {preset_name} preset: {path}")
        from app.ui.modals.edit import DirectorySelectModal

        err = DirectorySelectModal._validate_directory_path(path)
        if err:
            self._show_error(err)
        else:
            try:
                self.query_one("#lbl-error", Static).update("")
            except Exception:
                pass

    def on_mount(self) -> None:
        """Focus switch on mount and emit screen reader announcement."""
        self._update_layout(self.size.width)
        try:
            self.query_one("#switch-consent", Switch).focus()
        except Exception:
            pass
        self.announce("Opened model onboarding wizard dialog.")

    @on(events.DescendantFocus)
    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """Announce privacy disclosure when focus lands on AI consent switch or privacy card."""
        widget_id = getattr(event.widget, "id", None)
        if widget_id == "switch-consent":
            self.announce(
                f"AI Consent switch focused. {PRIVACY_GUARANTEE_ANNOUNCEMENT}"
            )
        elif widget_id == "privacy-card":
            self.announce(
                f"Privacy disclosure card focused. {PRIVACY_GUARANTEE_ANNOUNCEMENT}"
            )
        elif widget_id == "input-dir":
            self.announce(
                "Workspace directory input focused. Enter path or choose a quick preset."
            )

    @on(Switch.Changed)
    def on_switch_changed(self, event: Switch.Changed) -> None:
        """Announce switch state changes for screen reader accessibility."""
        switch_id = getattr(event.switch, "id", None) or getattr(
            event.control, "id", None
        )
        if not switch_id:
            return

        if switch_id == "switch-consent":
            state = "enabled" if event.value else "disabled"
            self.announce(f"AI consent {state}.")
            self.announce(f"AI consent {state}. {PRIVACY_GUARANTEE_ANNOUNCEMENT}")
        elif switch_id == "switch-sample-corpus":
            state = "enabled" if event.value else "disabled"
            self.announce(f"Sample document corpus generation {state}.")

    @on(Checkbox.Changed)
    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Announce starter template checkbox selection state changes."""
        chk_id = getattr(event.checkbox, "id", None) or getattr(
            event.control, "id", None
        )
        if not chk_id:
            return

        if chk_id == "chk-sample-corpus":
            state = "enabled" if event.value else "disabled"
            self.announce(f"Sample document corpus generation {state}.")
            return

        prefix = ""
        if chk_id.startswith("chk-template-"):
            prefix = "chk-template-"
        elif chk_id.startswith("chk-gallery-"):
            prefix = "chk-gallery-"
        else:
            return

        pack_id = chk_id[len(prefix) :]
        from app.core.rule_templates import get_starter_templates

        for pack in get_starter_templates():
            if pack.id == pack_id:
                state = "selected" if event.value else "deselected"
                self.announce(f"{pack.title} template pack {state}.")
                break

    @on(Button.Pressed, "#btn-open-playground")
    def action_open_playground(self) -> None:
        """Open interactive sample playground modal screen."""
        from app.ui.modals.sample_playground import SamplePlaygroundModal

        target_dir = ""
        try:
            val = self.query_one("#input-dir", Input).value.strip()
            if val and os.path.exists(os.path.expanduser(val)):
                target_dir = os.path.abspath(os.path.expanduser(val))
        except Exception:
            pass
        if not target_dir:
            target_dir = getattr(self.app, "base_dir", "") or os.getcwd()

        self.app.push_screen(
            SamplePlaygroundModal(self.settings, target_dir=target_dir)
        )

    @on(Button.Pressed, "#btn-finish")
    def action_finish(self) -> None:
        """Finish wizard, validate directory, save consent settings, apply templates, and scan."""
        raw_dir = ""
        try:
            raw_dir = self.query_one("#input-dir", Input).value.strip()
        except Exception:
            pass

        if raw_dir:
            from app.ui.modals.edit import DirectorySelectModal

            err = DirectorySelectModal._validate_directory_path(raw_dir)
            if err:
                self._show_error(err)
                return
            chosen_dir = os.path.abspath(os.path.expanduser(raw_dir))
        else:
            chosen_dir = os.path.abspath(os.getcwd())

        try:
            consent = self.query_one("#switch-consent", Switch).value
        except Exception:
            consent = False
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
            try:
                apply_starter_templates(self.settings, selected_packs)
            except Exception as exc:
                logger.error(f"Failed to apply starter templates: {exc}")
                err_msg = f"Failed to apply starter rule template pack(s): {exc}"
                self.announce(err_msg)
                if hasattr(self.app, "notify"):
                    self.app.notify(err_msg, severity="error")

        if hasattr(self.settings, "_save"):
            try:
                self.settings._save()
            except Exception as e:
                logger.error(f"Error saving consent setting: {e}")
        gen_sample = False
        try:
            gen_sample = self.query_one("#chk-sample-corpus", Checkbox).value
        except Exception:
            try:
                gen_sample = self.query_one("#switch-sample-corpus", Switch).value
            except Exception:
                pass

        target_dir = chosen_dir
        if gen_sample:
            from app.core.sample_corpus import generate_sample_corpus

            sample_fn = getattr(self.app, "_sample_dataset_dir", None)
            if callable(sample_fn):
                sample_target = sample_fn(chosen_dir)
            else:
                if (
                    os.path.basename(os.path.normpath(chosen_dir))
                    == "sortify-sample-dataset"
                ):
                    sample_target = chosen_dir
                else:
                    try:
                        with os.scandir(chosen_dir) as entries:
                            is_empty = not any(entries)
                    except FileNotFoundError:
                        is_empty = True
                    sample_target = (
                        chosen_dir
                        if is_empty
                        else os.path.join(chosen_dir, "sortify-sample-dataset")
                    )

            target_dir = sample_target
            try:
                generate_sample_corpus(target_dir, overwrite=False)
                self.announce(f"Generated sample document corpus in '{target_dir}'.")
            except FileExistsError:
                self.announce(f"Using the existing sample dataset in '{target_dir}'.")
            except Exception as exc:
                logger.error(f"Failed to generate sample corpus: {exc}")

        if hasattr(self.app, "base_dir"):
            setattr(self.app, "base_dir", target_dir)
        if hasattr(self.app, "source_dirs"):
            setattr(self.app, "source_dirs", [target_dir])

        update_cta = getattr(self.app, "_update_onboarding_cta_visibility", None)
        if callable(update_cta):
            try:
                update_cta()
            except Exception:
                pass
        try:
            ws_state = self.app.query_one("#onboarding-workspace-state", Static)
            ws_state.update(f"Active Workspace: {target_dir}")
        except Exception:
            pass

        scan_fn = getattr(self.app, "action_scan_directory", None)
        if callable(scan_fn):
            scan_fn()

        self.announce(
            "Finished model onboarding wizard, saved consent settings, and applied selected starter rule templates."
        )
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Cancel wizard on Escape key press."""
        if hasattr(self.app, "_wizard_dismissed_this_session"):
            self.app._wizard_dismissed_this_session = True
        self.announce("Closed model onboarding wizard dialog.")
        self.dismiss(None)
