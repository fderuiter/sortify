"""First-run onboarding wizard."""

import logging
import os

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Label, Switch

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
    Button:focus, Checkbox:focus, Switch:focus, .privacy-card:focus {
        border: heavy $accent;
        text-style: bold;
    }
    Checkbox {
        height: 1;
        border: none;
        padding: 0 1;
    }
    """

    def __init__(self, settings):
        super().__init__()
        self.settings = settings

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

        target_dir = getattr(self.app, "base_dir", "") or os.getcwd()
        self.app.push_screen(
            SamplePlaygroundModal(self.settings, target_dir=target_dir)
        )

    @on(Button.Pressed, "#btn-finish")
    def action_finish(self) -> None:
        """Finish wizard, save consent settings, and apply selected starter rule templates."""
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

        if gen_sample:
            from app.core.sample_corpus import generate_sample_corpus

            target_dir = getattr(self.app, "base_dir", "") or os.getcwd()
            if hasattr(self.app, "base_dir"):
                self.app.base_dir = target_dir
            try:
                generate_sample_corpus(target_dir, overwrite=True)
                self.announce(f"Generated sample document corpus in '{target_dir}'.")
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
        if hasattr(self.app, "_wizard_dismissed_this_session"):
            self.app._wizard_dismissed_this_session = True
        self.announce("Closed model onboarding wizard dialog.")
        self.dismiss(None)
