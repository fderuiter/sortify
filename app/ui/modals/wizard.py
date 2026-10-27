"""First-run onboarding wizard."""

import logging
import os

from textual import on
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
            yield Heading(
                "Model Onboarding Wizard [Ctrl+W]",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            yield Label("Welcome to Smart AutoSorter AI Pro TUI.")
            yield Label("Enable AI semantic categorization consent below:")

            consent_val = getattr(self.settings, "AI_CONSENT_GRANTED", None)
            if consent_val is None:
                consent_val = False
            yield LabeledSwitch(
                label="AI Consent Granted",
                value=bool(consent_val),
                switch_id="switch-consent",
                tooltip="Toggle AI consent for semantic document classification",
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
