"""First-run onboarding wizard."""

import logging
import os

from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import Button, Checkbox, Input, Label, Static, Switch

from app.ui.heading import Heading
from app.ui.layout import ModalBox, ResponsiveModalScreen
from app.ui.widgets.labeled_switch import LabeledSwitch

logger = logging.getLogger(__name__)

PRIVACY_GUARANTEE_ANNOUNCEMENT = (
    "[100% On-Device & Offline] Privacy guarantees: 100% local model execution, "
    "zero document cloud telemetry, and encrypted local SQLCipher storage."
)


class WizardModal(ResponsiveModalScreen[None]):
    """Modal dialog for model onboarding wizard."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-frame {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 90%;
        max-width: 80;
        min-width: 30;
        height: auto;
        max-height: 90%;
        overflow-y: hidden;
    }
    .modal-box {
        padding: 0;
        border: none;
        width: 100%;
        max-width: 100%;
        min-width: 0;
        max-height: 100%;
        overflow-y: auto;
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
        width: 100%;
        layout: grid;
        grid-size: 3;
        grid-gutter: 0 1;
        margin-top: 1;
        margin-bottom: 1;
        height: auto;
        min-height: 3;
    }
    .preset-row Button {
        width: 100%;
        min-width: 0;
    }
    .switch-row {
        margin-top: 1;
        margin-bottom: 1;
        height: 3;
        align: left middle;
    }
    .button-row {
        height: auto;
        min-height: 4;
        align: right middle;
    }
    .rule-summary {
        width: 100%;
        height: 1;
        color: $text-muted;
        text-overflow: ellipsis;
        overflow: hidden;
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
    WizardModal {
        &.compact .modal-frame {
            padding: 0 1;
            width: 96%;
            max-height: 96%;
        }
        &.narrow .modal-frame {
            padding: 0 1;
            width: 96%;
        }
        &.compact .modal-box, &.narrow .modal-box {
            padding: 0;
            width: 100%;
            max-height: 100%;
        }
        &.narrow .privacy-card {
            padding: 0 1;
            border: round $primary;
        }
        &.compact .privacy-card {
            margin-top: 0;
            margin-bottom: 0;
        }
        &.compact .field-label, &.compact .preset-label {
            margin-top: 0;
        }
        &.compact .switch-row {
            margin-top: 0;
            margin-bottom: 0;
        }
        &.compact .button-row {
            margin-top: 0;
            min-height: 2;
        }
        &.compact .preset-row {
            margin-top: 0;
            margin-bottom: 0;
            min-height: 1;
        }
        &.compact .preset-row Button {
            height: 1;
            min-height: 1;
            border: none;
        }
        &.narrow .preset-row {
            grid-size: 2;
        }
        &.narrow .button-row Button {
            min-width: 0;
            padding: 0 1;
        }
    }
    """

    def __init__(self, settings, current_dir: str | None = None):
        super().__init__()
        self.settings = settings
        self.current_dir = current_dir
        self._playground_committed_dir: str | None = None
        self._sample_auto_enabled_by_demo: bool = False

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with ModalBox(classes="modal-frame"):
            with Vertical(classes="modal-body modal-box"):
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

                yield Label(
                    self._format_model_status_text(
                        consent_enabled=bool(consent_val),
                        defer_download=False,
                    ),
                    id="lbl-wizard-model-status",
                    markup=False,
                )

                chk_defer = Checkbox(
                    "Defer model download (metered or offline network)",
                    value=False,
                    id="chk-defer-download",
                )
                chk_defer.tooltip = "Skip automatic background model download and sort immediately with local keyword/TF-IDF rules"
                yield chk_defer

                proxy_val = (
                    getattr(self.settings, "PROXY", "") or ""
                    if self.settings is not None
                    else ""
                )
                yield Label("Network Proxy (optional):", classes="preset-label")
                inp_proxy = Input(
                    value=str(proxy_val),
                    placeholder="Optional HTTP/HTTPS proxy URL (e.g. http://proxy.example.com:8080)",
                    id="input-wizard-proxy",
                )
                inp_proxy.tooltip = "Optional HTTP/HTTPS proxy URL used when downloading local AI model weights"
                yield inp_proxy

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
                    p_demo = Button(
                        "Demo Workspace", id="preset-demo", variant="default"
                    )
                    p_demo.tooltip = "Select sandbox demo workspace quick preset"
                    yield p_demo
                    p_dl = Button("Downloads", id="preset-downloads", variant="default")
                    p_dl.tooltip = "Select user Downloads folder quick preset"
                    yield p_dl
                    p_docs = Button(
                        "Documents", id="preset-documents", variant="default"
                    )
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
                    chk = Checkbox(
                        f"{pack.icon} {pack.title}",
                        value=False,
                        id=f"chk-template-{pack.id}",
                    )
                    chk.tooltip = pack.description
                    yield chk

                yield Heading(
                    "Compliance Sensitivity Quarantine (Opt-In):",
                    level=2,
                    classes="field-label",
                )
                yield Label(
                    "Enable automatic quarantine holds only if you want HIGH or CRITICAL sensitivity files staged for review:"
                )
                current_auto_ratings = {
                    str(r).upper()
                    for r in (
                        getattr(self.settings, "AUTO_QUARANTINE_RATINGS", []) or []
                    )
                    if r
                }
                yield LabeledSwitch(
                    label="Auto-Quarantine CRITICAL Sensitivity",
                    value="CRITICAL" in current_auto_ratings,
                    switch_id="switch-wizard-quarantine-critical",
                    tooltip="Automatically hold CRITICAL sensitivity documents (e.g. clinical/medical records) in compliance quarantine",
                )
                yield LabeledSwitch(
                    label="Auto-Quarantine HIGH Sensitivity",
                    value="HIGH" in current_auto_ratings,
                    switch_id="switch-wizard-quarantine-high",
                    tooltip="Automatically hold HIGH sensitivity documents (e.g. financial, tax, legal, resume records) in compliance quarantine",
                )

            with Vertical(classes="button-row"):
                yield Label(
                    self._format_selected_rules_summary(selected_pack_ids=[]),
                    id="lbl-wizard-rule-summary",
                    classes="rule-summary",
                    markup=False,
                )
                btn_finish = Button("Finish & Save", id="btn-finish", variant="primary")
                btn_finish.tooltip = (
                    "Save AI consent setting and complete onboarding wizard"
                )
                yield btn_finish

    def _format_model_status_text(
        self,
        consent_enabled: bool | None = None,
        defer_download: bool | None = None,
    ) -> str:
        """Build truthful live model status description for the wizard."""
        from app.core.downloader import inspect_local_model_bundle

        if consent_enabled is None:
            try:
                consent_enabled = bool(self.query_one("#switch-consent", Switch).value)
            except Exception:
                consent_enabled = bool(
                    getattr(self.settings, "AI_CONSENT_GRANTED", False)
                )

        if defer_download is None:
            try:
                defer_download = bool(
                    self.query_one("#chk-defer-download", Checkbox).value
                )
            except Exception:
                defer_download = False

        bundle = inspect_local_model_bundle()
        onnx_available = bool(bundle.get("onnxruntime_available", False))
        if bundle["is_downloading"]:
            pct = int(round(bundle["progress_percent"]))
            return (
                f"Model Status: Downloading local AI model weights ({pct}%) — "
                "workspace sorts with keyword clustering until verification completes."
            )
        if bundle["verified"]:
            if not onnx_available:
                return (
                    "Model Status: [AI: Fallback] Verified weights on disk, but optional "
                    "'onnxruntime' is absent — install 'smart-autosorter[ml]' for semantic vectors "
                    "(using local keyword/TF-IDF fallback)."
                )
            if consent_enabled:
                return "Model Status: [AI: Active] Verified local AI model weights ready on disk."
            return "Model Status: Verified local AI model weights ready on disk."
        if bundle["disk_bundle_status"] in ("incomplete", "corrupted"):
            reason = bundle["detail"]
            if consent_enabled and not defer_download:
                return (
                    f"Model Status: Unverified/incomplete weights ({reason}) — "
                    "fresh background download will start on Finish."
                )
            return (
                f"Model Status: [AI: Fallback] Unverified/incomplete weights ({reason}) — "
                "using local keyword/TF-IDF fallback."
            )
        if consent_enabled and not defer_download:
            if not onnx_available:
                return (
                    "Model Status: [AI: Fallback] Missing local model weights "
                    "(install 'smart-autosorter[ml]' for ONNX runtime) — background download (~80 MB) "
                    "will start automatically on Finish."
                )
            return (
                "Model Status: Missing local model weights — background download (~80 MB) "
                "will start automatically on Finish."
            )
        if consent_enabled and defer_download:
            return (
                "Model Status: [AI: Fallback] Missing local model weights — download deferred "
                "(using local keyword/TF-IDF fallback)."
            )
        return (
            "Model Status: Missing local model weights — AI disabled "
            "(using local keyword/TF-IDF sorting)."
        )

    def _selected_pack_rule_totals(
        self, selected_pack_ids: list[str] | None = None
    ) -> tuple[int, int, int]:
        """Return (pack_count, keyword_rule_count, policy_count) for selected starter packs."""
        from app.core.rule_templates import get_starter_templates

        ids = (
            set(selected_pack_ids)
            if selected_pack_ids is not None
            else set(self._get_selected_pack_ids())
        )
        kw_keys: set[str] = set()
        pol_count = 0
        matched_packs = 0
        for pack in get_starter_templates():
            if pack.id in ids:
                matched_packs += 1
                kw_keys.update(k.lower() for k in pack.keyword_rules)
                pol_count += len(pack.policies)
        return matched_packs, len(kw_keys), pol_count

    def _format_selected_rules_summary(
        self, selected_pack_ids: list[str] | None = None
    ) -> str:
        """Build concise summary of selected starter rules pinned above Finish."""
        pack_count, kw_count, pol_count = self._selected_pack_rule_totals(
            selected_pack_ids
        )
        pack_word = "pack" if pack_count == 1 else "packs"
        rule_word = "rule" if kw_count == 1 else "rules"
        pol_word = "policy" if pol_count == 1 else "policies"
        return (
            f"Selected: {kw_count} keyword {rule_word}, "
            f"{pol_count} {pol_word} ({pack_count} {pack_word})"
        )

    def _refresh_rule_summary_label(self) -> None:
        """Refresh the pinned starter rule counter label inside the button row."""
        try:
            lbl = self.query_one("#lbl-wizard-rule-summary", Label)
        except NoMatches:
            return
        lbl.update(self._format_selected_rules_summary())

    def _refresh_model_status_label(self) -> None:
        """Refresh the live model status label inside the wizard."""
        try:
            lbl = self.query_one("#lbl-wizard-model-status", Label)
            lbl.update(self._format_model_status_text())
        except Exception:
            pass

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
        """Select sandbox demo workspace quick preset and enable sample generation."""
        path = os.path.abspath("sandbox/demo_workspace")
        if not os.path.exists(path):
            try:
                os.makedirs(path, exist_ok=True)
            except Exception as exc:
                self._show_error(f"Failed to create directory '{path}': {exc}")
                return
        self._set_directory_input(path, "demo workspace")
        try:
            chk_sample = self.query_one("#chk-sample-corpus", Checkbox)
            if not chk_sample.value:
                self._sample_auto_enabled_by_demo = True
                chk_sample.value = True
        except Exception:
            pass

    @on(Button.Pressed, "#preset-downloads")
    def action_preset_downloads(self) -> None:
        """Select user Downloads folder quick preset."""
        path = os.path.expanduser("~/Downloads")
        if self._sample_auto_enabled_by_demo:
            self._sample_auto_enabled_by_demo = False
            try:
                self.query_one("#chk-sample-corpus", Checkbox).value = False
            except Exception:
                pass
        self._set_directory_input(path, "Downloads")

    @on(Button.Pressed, "#preset-documents")
    def action_preset_documents(self) -> None:
        """Select user Documents folder quick preset."""
        path = os.path.expanduser("~/Documents")
        if self._sample_auto_enabled_by_demo:
            self._sample_auto_enabled_by_demo = False
            try:
                self.query_one("#chk-sample-corpus", Checkbox).value = False
            except Exception:
                pass
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
        """Focus switch on mount, refresh model status, and emit screen reader announcement."""
        super().on_mount()
        self._refresh_model_status_label()
        self._refresh_rule_summary_label()
        self.set_interval(0.5, self._refresh_model_status_label)
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
            self._refresh_model_status_label()
            self.announce(f"AI consent {state}.")
            self.announce(f"AI consent {state}. {PRIVACY_GUARANTEE_ANNOUNCEMENT}")
        elif switch_id == "switch-sample-corpus":
            state = "enabled" if event.value else "disabled"
            self.announce(f"Sample document corpus generation {state}.")
        elif switch_id == "switch-wizard-quarantine-critical":
            state = "enabled" if event.value else "disabled"
            self.announce(f"Automatic CRITICAL sensitivity quarantine {state}.")
        elif switch_id == "switch-wizard-quarantine-high":
            state = "enabled" if event.value else "disabled"
            self.announce(f"Automatic HIGH sensitivity quarantine {state}.")

    @on(Checkbox.Changed)
    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Announce starter template and provisioning checkbox selection state changes."""
        chk_id = getattr(event.checkbox, "id", None) or getattr(
            event.control, "id", None
        )
        if not chk_id:
            return

        if chk_id == "chk-defer-download":
            self._refresh_model_status_label()
            state = "deferred" if event.value else "automatic"
            self.announce(f"Model download mode set to {state}.")
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
        self._refresh_rule_summary_label()
        from app.core.rule_templates import get_starter_templates

        for pack in get_starter_templates():
            if pack.id == pack_id:
                state = "selected" if event.value else "deselected"
                self.announce(f"{pack.title} template pack {state}.")
                break

    def _get_selected_pack_ids(self) -> list[str]:
        """Return starter template pack IDs currently checked in the wizard."""
        from app.core.rule_templates import get_starter_templates

        selected: list[str] = []
        for pack in get_starter_templates():
            try:
                if self.query_one(f"#chk-template-{pack.id}", Checkbox).value:
                    selected.append(pack.id)
            except Exception:
                pass
        return selected

    def _sync_from_playground(self, payload: dict) -> None:
        """Synchronize starter pack toggles and committed sandbox directory from SamplePlaygroundModal."""
        from app.core.rule_templates import get_starter_templates

        selected_packs = set(payload.get("selected_packs") or [])
        for pack in get_starter_templates():
            try:
                chk = self.query_one(f"#chk-template-{pack.id}", Checkbox)
                desired = pack.id in selected_packs
                if chk.value != desired:
                    chk.value = desired
            except Exception:
                pass
        self._refresh_rule_summary_label()

        if payload.get("committed"):
            committed_dir = str(payload.get("target_dir") or "").strip()
            if committed_dir:
                self.current_dir = committed_dir
                self._playground_committed_dir = committed_dir
                try:
                    self.query_one("#input-dir", Input).value = committed_dir
                except Exception:
                    pass
                try:
                    self.query_one("#chk-sample-corpus", Checkbox).value = False
                    self._sample_auto_enabled_by_demo = False
                except Exception:
                    pass

    @on(Button.Pressed, "#btn-open-playground")
    def action_open_playground(self) -> None:
        """Open interactive sample playground modal screen with bidirectional state sync."""
        from app.ui.modals.sample_playground import SamplePlaygroundModal

        target_dir = ""
        try:
            val = self.query_one("#input-dir", Input).value.strip()
            if val:
                target_dir = os.path.abspath(os.path.expanduser(val))
        except Exception:
            pass
        if not target_dir:
            target_dir = getattr(self.app, "base_dir", "") or os.getcwd()

        self.app.push_screen(
            SamplePlaygroundModal(
                self.settings,
                target_dir=target_dir,
                initial_selected_packs=self._get_selected_pack_ids(),
                on_sync=self._sync_from_playground,
            )
        )

    @on(Button.Pressed, "#btn-finish")
    def action_finish(self) -> None:
        """Finish wizard, validate directory, save consent/proxy, start background provisioning, and scan."""
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

        proxy_val = ""
        try:
            proxy_val = self.query_one("#input-wizard-proxy", Input).value.strip()
            self.settings.PROXY = proxy_val
        except Exception:
            proxy_val = str(getattr(self.settings, "PROXY", "") or "").strip()

        existing_ratings = [
            str(r).upper()
            for r in (getattr(self.settings, "AUTO_QUARANTINE_RATINGS", []) or [])
            if r and str(r).upper() not in ("CRITICAL", "HIGH")
        ]
        try:
            q_crit = self.query_one("#switch-wizard-quarantine-critical", Switch).value
        except Exception:
            q_crit = "CRITICAL" in {
                str(r).upper()
                for r in (getattr(self.settings, "AUTO_QUARANTINE_RATINGS", []) or [])
            }
        try:
            q_high = self.query_one("#switch-wizard-quarantine-high", Switch).value
        except Exception:
            q_high = "HIGH" in {
                str(r).upper()
                for r in (getattr(self.settings, "AUTO_QUARANTINE_RATINGS", []) or [])
            }

        updated_ratings: list[str] = []
        if q_crit:
            updated_ratings.append("CRITICAL")
        if q_high:
            updated_ratings.append("HIGH")
        for extra_r in existing_ratings:
            if extra_r not in updated_ratings:
                updated_ratings.append(extra_r)
        try:
            self.settings.AUTO_QUARANTINE_RATINGS = updated_ratings
        except Exception:
            pass

        defer_download = False
        try:
            defer_download = bool(self.query_one("#chk-defer-download", Checkbox).value)
        except Exception:
            defer_download = False

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

            is_demo_preset = os.path.normpath(chosen_dir).endswith(
                os.path.normpath("sandbox/demo_workspace")
            )
            sample_fn = getattr(self.app, "_sample_dataset_dir", None)
            if is_demo_preset or chosen_dir == self._playground_committed_dir:
                sample_target = chosen_dir
            elif callable(sample_fn):
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

        from app.config import resolve_scoped_settings
        from app.core.rule_templates import apply_starter_templates

        selected_packs = self._get_selected_pack_ids()
        applied_starter_ok = True
        toast_msg = "Saved onboarding settings (0 starter rules added)."
        if selected_packs:
            try:
                scoped_settings = resolve_scoped_settings(
                    self.settings, target_dir, fallback_to_global=True
                )
                prev_kw_count = len(getattr(scoped_settings, "KEYWORD_RULES", {}) or {})
                prev_pol_count = len(getattr(scoped_settings, "POLICIES", []) or [])
                new_keywords, new_policies = apply_starter_templates(
                    scoped_settings, selected_packs
                )
                added_kw = max(0, len(new_keywords) - prev_kw_count)
                added_pol = max(0, len(new_policies) - prev_pol_count)
                pack_count, total_kw, total_pol = self._selected_pack_rule_totals(
                    selected_packs
                )
                if added_kw > 0 or added_pol > 0:
                    toast_msg = (
                        f"Applied {pack_count} starter pack(s): "
                        f"added {added_kw} keyword rule(s) and {added_pol} policy rule(s)."
                    )
                else:
                    toast_msg = (
                        f"Saved {pack_count} starter pack(s) "
                        f"({total_kw} keyword rule(s) and {total_pol} policy rule(s) active)."
                    )
            except Exception as exc:
                applied_starter_ok = False
                logger.error(f"Failed to apply starter templates: {exc}")
                err_msg = f"Failed to apply starter rule template pack(s): {exc}"
                self.announce(err_msg)
                if hasattr(self.app, "notify"):
                    self.app.notify(err_msg, severity="error")

        if applied_starter_ok:
            self.announce(toast_msg)
            if hasattr(self.app, "notify"):
                self.app.notify(toast_msg)

        if hasattr(self.settings, "_save"):
            try:
                self.settings._save()
            except Exception as e:
                logger.error(f"Error saving consent setting: {e}")

        if consent and not defer_download:
            try:
                from app.core.downloader import (
                    DEFAULT_MODEL_URL,
                    DownloadManager,
                    resolve_default_model_dir,
                    verify_downloaded_model,
                )

                model_dir = resolve_default_model_dir()
                dm = DownloadManager.get_instance()
                if proxy_val:
                    dm.update_proxy(proxy_val)
                if not dm.state.get(
                    "is_downloading", False
                ) and not verify_downloaded_model(model_dir):
                    dm.start_download(DEFAULT_MODEL_URL, model_dir, proxy=proxy_val)
                    self.announce(
                        "Started background AI model download. Using local keyword clustering until verification completes."
                    )
            except Exception as dl_exc:
                logger.warning(
                    f"Background model provisioning could not start during onboarding: {dl_exc}"
                )
                self.announce(
                    f"Model download unavailable ({dl_exc}). Continuing with local keyword clustering."
                )

        if hasattr(self.app, "base_dir"):
            setattr(self.app, "base_dir", target_dir)
        if hasattr(self.app, "source_dirs"):
            setattr(self.app, "source_dirs", [target_dir])

        update_status_fn = getattr(self.app, "update_status", None)
        if callable(update_status_fn):
            try:
                update_status_fn()
            except Exception:
                pass

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

        self.announce(
            "Finished model onboarding wizard, saved consent settings, and applied selected starter rule templates."
        )
        try:
            self.set_focus(None)
        except Exception:
            pass
        self.dismiss(None)

        scan_fn = getattr(self.app, "action_scan_directory", None)
        if callable(scan_fn):
            scan_fn()

    def action_cancel(self) -> None:
        """Cancel wizard on Escape key press."""
        if hasattr(self.app, "_wizard_dismissed_this_session"):
            setattr(self.app, "_wizard_dismissed_this_session", True)
        self.announce("Closed model onboarding wizard dialog.")
        self.dismiss(None)
