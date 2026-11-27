"""Settings dialog and its scrub-artifacts and consent-revocation confirmations."""

import contextlib
import logging
import os
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Select, Switch, TabbedContent, TabPane

from app.ui.heading import Heading
from app.ui.layout import ModalBox, ResponsiveModalScreen
from app.ui.modals.models import ModelManagerModal
from app.ui.modals.rules import TemplateGalleryModal
from app.ui.notifications import TROUBLESHOOTING_URL
from app.ui.widgets.density_slider import DensitySlider
from app.ui.widgets.labeled_switch import LabeledSwitch

logger = logging.getLogger(__name__)


def _get_vector_count(db=None) -> int:
    """Return stored document vector count from database."""
    try:
        if db is not None and hasattr(db, "get_document_vectors_count"):
            return db.get_document_vectors_count()
        from app.core.services import acquire_services

        with acquire_services() as services:
            return services.db.get_document_vectors_count()
    except Exception:
        pass
    return 0


def _get_model_cache_footprint_mb() -> float:
    """Return model cache size on disk in MB."""
    try:
        from app.config import get_app_dir

        model_dir = get_app_dir() / "model"
        if model_dir.exists() and model_dir.is_dir():
            total_bytes = 0
            for root, _, files in os.walk(model_dir):
                for f in files:
                    fp = os.path.join(root, f)
                    if os.path.exists(fp):
                        total_bytes += os.path.getsize(fp)
            return total_bytes / (1024 * 1024)
    except Exception:
        pass
    return 0.0


class _ArtifactConfirmModal(ResponsiveModalScreen[bool]):
    """Private base confirmation modal for AI artifact scrubbing dialogs."""

    MODAL_TITLE: str = ""
    WARNING_TEXT: str = ""
    CANCEL_LABEL: str = "Cancel"
    CANCEL_BUTTON_ID: str = "btn-cancel"
    CONFIRM_LABEL: str = "Confirm"
    CONFIRM_BUTTON_ID: str = "btn-confirm"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .info-label {
        margin-top: 1;
        color: $text;
    }
    .warning-label {
        margin-top: 1;
        margin-bottom: 1;
        color: $warning;
        text-style: bold;
    }
    .button-row {
        min-height: 3;
    }
    .button-row Button {
        margin-left: 1;
    }
    ScrubArtifactsModal, ConsentRevocationModal {
        &.compact .info-label {
            margin-top: 0;
        }
        &.compact .warning-label {
            margin-top: 0;
            margin-bottom: 0;
        }
        &.narrow .button-row {
            layout: grid;
            grid-size: 1;
            grid-columns: 1fr;
        }
        &.narrow .button-row Button {
            width: 100%;
            min-width: 0;
            margin-left: 0;
        }
    }
    """

    def __init__(self, vector_count: int, cache_size_mb: float):
        super().__init__()
        self.vector_count = vector_count
        self.cache_size_mb = cache_size_mb

    def compose(self) -> ComposeResult:
        """Compose modal layout with footprint labels, warning text, and action buttons."""
        with ModalBox(classes="modal-box"):
            with Vertical(classes="modal-body"):
                yield Heading(self.MODAL_TITLE, level=1, classes="modal-title")
                yield Label(
                    f"Stored Document Vectors: {self.vector_count} records",
                    classes="info-label",
                    id="lbl-vector-count",
                )
                yield Label(
                    f"Model Cache Footprint: {self.cache_size_mb:.2f} MB",
                    classes="info-label",
                    id="lbl-cache-size",
                )
                yield Label(self.WARNING_TEXT, classes="warning-label")
            with Horizontal(classes="button-row"):
                yield Button(
                    self.CANCEL_LABEL, id=self.CANCEL_BUTTON_ID, variant="default"
                )
                yield Button(
                    self.CONFIRM_LABEL, id=self.CONFIRM_BUTTON_ID, variant="error"
                )

    @on(Button.Pressed)
    def _on_button_pressed(self, event: Button.Pressed) -> None:
        """Dispatch confirm or cancel action based on configured button IDs."""
        if event.button.id == self.CONFIRM_BUTTON_ID:
            self.action_confirm()
        elif event.button.id == self.CANCEL_BUTTON_ID:
            self.action_cancel()

    def action_confirm(self) -> None:
        """Confirm action and dismiss modal with True."""
        self.dismiss(True)

    def action_cancel(self) -> None:
        """Cancel action and dismiss modal with False."""
        self.dismiss(False)


class ScrubArtifactsModal(_ArtifactConfirmModal):
    """Modal dialog for confirming manual scrubbing of AI vector database rows and model cache files."""

    MODAL_TITLE = "Scrub AI Artifacts Confirmation"
    WARNING_TEXT = (
        "Warning: This operation will permanently remove all stored document vector embeddings "
        "from the SQLite database and purge local model cache files from disk. "
        "Are you sure you want to scrub AI artifacts?"
    )
    CANCEL_LABEL = "Cancel"
    CANCEL_BUTTON_ID = "btn-cancel-scrub"
    CONFIRM_LABEL = "Scrub AI Artifacts"
    CONFIRM_BUTTON_ID = "btn-confirm-scrub"


class ConsentRevocationModal(_ArtifactConfirmModal):
    """Modal dialog for confirming revocation of AI consent and cascading artifact scrubbing."""

    MODAL_TITLE = "Confirm AI Consent Revocation"
    WARNING_TEXT = (
        "Revoking AI consent will disable AI features, unload models from memory, "
        "scrub stored document vectors from SQLite tables, and purge model cache files from disk. "
        "Do you wish to proceed?"
    )
    CANCEL_LABEL = "Keep Consent Active"
    CANCEL_BUTTON_ID = "btn-cancel-revoke"
    CONFIRM_LABEL = "Revoke Consent & Scrub Data"
    CONFIRM_BUTTON_ID = "btn-confirm-revoke"


class SettingsModal(ResponsiveModalScreen[dict[str, Any] | None]):
    """Modal dialog for modifying application settings."""

    AUTO_FOCUS = None
    HELP_URL = f"{TROUBLESHOOTING_URL}#settings-configuration"

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .field-label {
        margin-top: 1;
        color: $text;
        text-style: bold;
    }
    .setting-hint {
        width: 100%;
        color: $text-muted;
        margin-bottom: 1;
    }
    .switch-row {
        margin-top: 1;
        margin-bottom: 1;
        height: 3;
        align: left middle;
    }
    .button-row {
        layout: grid;
        grid-size: 3;
        grid-gutter: 0 1;
        min-height: 3;
    }
    .button-row Button {
        width: 100%;
        min-width: 0;
    }
    Button:focus, Input:focus, Select:focus, Switch:focus {
        border: heavy $accent;
        text-style: bold;
    }
    SettingsModal {
        &.compact .field-label {
            margin-top: 0;
        }
        &.compact .switch-row {
            margin-top: 0;
            margin-bottom: 0;
        }
        &.narrow .button-row {
            grid-size: 2;
        }
    }
    SettingsModal TabbedContent {
        height: 1fr;
        min-height: 0;
    }
    SettingsModal ContentSwitcher {
        height: 1fr;
        min-height: 0;
    }
    SettingsModal TabPane {
        height: 1fr;
        min-height: 0;
        padding: 0;
        overflow-y: auto;
    }
    """

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.help_url = f"{TROUBLESHOOTING_URL}#settings-configuration"
        self._announced_switch_values: dict[str, bool] = {}
        self._staged_consent_revocation: bool = False
        self._ai_assisted_naming_touched: bool = False
        self._syncing_ai_switches: bool = False

    def query_one(self, selector: Any, expect_type: Any = None) -> Any:
        """Query one widget, aliasing alternative switch IDs."""
        if selector in (
            "#switch-enable-character-key-shortcuts",
            "#switch-character-shortcuts",
        ):
            selector = "#switch-character-key-shortcuts"
        elif selector in (
            "#switch-cleanup",
            "#switch-cleanup-folders",
            "#switch-cleanup-empty-directories",
            "#switch-empty-directory-cleanup",
            "#switch-clean-up-empty-folders",
        ):
            selector = "#switch-cleanup-empty-folders"
        elif selector in (
            "#switch-preserve",
            "#switch-preserve-folder-hierarchy",
            "#switch-folder-hierarchy-preservation",
            "#switch-preserve-directory-hierarchy",
            "#switch-hierarchy",
        ):
            selector = "#switch-preserve-hierarchy"
        elif selector in (
            "#switch-ai-assisted",
            "#switch-assisted-naming",
        ):
            selector = "#switch-ai-assisted-naming"
        return super().query_one(selector, expect_type=expect_type)

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with ModalBox(classes="modal-box"):
            yield Heading(
                "Application Settings [Ctrl+O]",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            with TabbedContent(initial="tab-general", id="settings-tabbed-content"):
                with TabPane(
                    "General Settings", id="tab-general", classes="modal-body"
                ):
                    yield Label(
                        "Protected Directories (comma-separated):",
                        classes="field-label",
                    )
                    prot = getattr(self.settings, "PROTECTED_PATHS", [])
                    prot_str = (
                        ", ".join(prot)
                        if isinstance(prot, (list, tuple, set))
                        else str(prot)
                    )
                    inp_prot = Input(
                        value=prot_str,
                        placeholder="/path/1, /path/2",
                        id="input-protected",
                    )
                    inp_prot.tooltip = "Comma-separated protected directories exempt from automated moves"
                    yield inp_prot

                    yield Label(
                        "Ignored Extensions (comma-separated):",
                        classes="field-label",
                    )
                    ign = getattr(self.settings, "IGNORED_EXTENSIONS", [])
                    ign_str = (
                        ", ".join(ign)
                        if isinstance(ign, (list, tuple, set))
                        else str(ign)
                    )
                    inp_ign = Input(
                        value=ign_str,
                        placeholder=".tmp, .bak, .log",
                        id="input-ignored",
                    )
                    inp_ign.tooltip = (
                        "Comma-separated file extensions to ignore during scanning"
                    )
                    yield inp_ign

                    yield Label("Max Folders:", classes="field-label")
                    max_f_str = str(getattr(self.settings, "MAX_FOLDERS", 12))
                    inp_max_f = Input(
                        value=max_f_str, placeholder="12", id="input-max-folders"
                    )
                    inp_max_f.tooltip = (
                        "Maximum subdirectories generated during sorting"
                    )
                    yield inp_max_f

                    yield Label("Worker Concurrency (Threads):", classes="field-label")
                    conc_str = str(getattr(self.settings, "MAX_WORKERS", 4))
                    inp_conc = Input(
                        value=conc_str, placeholder="4", id="input-concurrency"
                    )
                    inp_conc.tooltip = "Worker concurrency thread limit for processing"
                    yield inp_conc

                    yield Label("Conflict Policy:", classes="field-label")
                    conflict_pol = getattr(self.settings, "CONFLICT_POLICY", "rename")
                    conflict_options = [
                        ("Overwrite", "overwrite"),
                        ("Auto-Rename", "rename"),
                        ("Skip", "skip"),
                    ]
                    sel_conflict = Select(
                        options=conflict_options,
                        value=conflict_pol,
                        id="select-conflict-policy",
                    )
                    sel_conflict.tooltip = "Select collision handling strategy for conflicting target files"
                    yield sel_conflict

                    yield Heading("Duplicate Handling:", level=2, classes="field-label")
                    dup_act_raw = str(
                        getattr(self.settings, "DUPLICATE_ACTION", "off") or "off"
                    ).lower()
                    dup_options = [
                        ("Off (ignore byte-identical duplicates)", "off"),
                        ("Interactive Review (confirm before execution)", "review"),
                        ("Skip Redundant Copies (keep original only)", "skip"),
                        ("Stage Copies for Undo (.sortify_duplicates)", "stage"),
                        ("Trash Copies (two-step staging)", "trash"),
                    ]
                    valid_dup_acts = {val for _lbl, val in dup_options}
                    dup_act_val = (
                        dup_act_raw if dup_act_raw in valid_dup_acts else "off"
                    )
                    sel_dup = Select(
                        dup_options,
                        value=dup_act_val,
                        allow_blank=False,
                        id="select-duplicate-action",
                    )
                    sel_dup.tooltip = (
                        "Select how byte-identical duplicate files are handled"
                    )
                    setattr(
                        sel_dup,
                        "aria_label",
                        "Byte-identical duplicate handling mode",
                    )
                    yield sel_dup

                    yield Heading(
                        "Original Selection Criterion:",
                        level=2,
                        classes="field-label",
                    )
                    det_orig_raw = str(
                        getattr(self.settings, "DETECT_ORIGINAL_BY", "first_seen")
                        or "first_seen"
                    ).lower()
                    det_orig_options = [
                        ("First Seen (discovery order)", "first_seen"),
                        ("Last Seen (reverse discovery order)", "-first_seen"),
                        ("Filename (A -> Z)", "name"),
                        ("Filename (Z -> A)", "-name"),
                        ("Oldest Created Timestamp", "created"),
                        ("Newest Created Timestamp", "-created"),
                        ("Oldest Modified Timestamp", "lastmodified"),
                        ("Newest Modified Timestamp", "-lastmodified"),
                    ]
                    valid_det_orig = {val for _lbl, val in det_orig_options}
                    det_orig_val = (
                        det_orig_raw if det_orig_raw in valid_det_orig else "first_seen"
                    )
                    sel_det_orig = Select(
                        det_orig_options,
                        value=det_orig_val,
                        allow_blank=False,
                        id="select-detect-original-by",
                    )
                    sel_det_orig.tooltip = "Select tie-breaker criterion for choosing the surviving original file"
                    setattr(
                        sel_det_orig,
                        "aria_label",
                        "Surviving original selection criterion",
                    )
                    yield sel_det_orig

                    yield LabeledSwitch(
                        label="Cleanup Empty Folders",
                        value=bool(
                            getattr(self.settings, "CLEANUP_EMPTY_FOLDERS", True)
                        ),
                        switch_id="switch-cleanup-empty-folders",
                        tooltip="Automatically prune empty source directories after moving organized files",
                    )

                    yield LabeledSwitch(
                        label="Preserve Folder Hierarchy",
                        value=bool(getattr(self.settings, "PRESERVE_HIERARCHY", False)),
                        switch_id="switch-preserve-hierarchy",
                        tooltip="Maintain source directory levels and folder structure during classification",
                    )

                    yield Label(
                        "Max Scan Depth (0 = top-level only):",
                        classes="field-label",
                    )
                    inp_scan_depth = Input(
                        value=str(getattr(self.settings, "SCAN_MAX_DEPTH", 0)),
                        id="input-scan-max-depth",
                    )
                    inp_scan_depth.tooltip = (
                        "Maximum subfolder depth to scan (0 keeps subfolders untouched)"
                    )
                    yield inp_scan_depth

                with TabPane(
                    "AI & Privacy Settings",
                    id="tab-ai-privacy",
                    classes="modal-body",
                ):
                    yield Heading("Sorting Strategy:", level=2, classes="field-label")
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
                    sel_strat = Select(
                        options=options, value=strat, id="select-strategy"
                    )
                    sel_strat.tooltip = (
                        "Select sorting strategy engine for document classification"
                    )
                    yield sel_strat

                    yield Heading(
                        "Compliance & Renaming Toggles:",
                        level=2,
                        classes="field-label",
                    )
                    for sw_cfg in reg.get_tui_switches():
                        sw_key = sw_cfg.get("key", "")
                        sw_id = sw_cfg.get("id", f"switch-{sw_key.lower()}")
                        sw_lbl = sw_cfg.get("label", f" {sw_key}")
                        sw_tip = sw_cfg.get("tooltip", "")
                        sw_val = bool(getattr(self.settings, sw_key, False))
                        yield LabeledSwitch(
                            label=sw_lbl,
                            value=sw_val,
                            switch_id=sw_id,
                            tooltip=sw_tip,
                        )

                    ai_consent_val = bool(
                        getattr(self.settings, "AI_CONSENT_GRANTED", False)
                    )
                    ai_assisted_val = bool(
                        ai_consent_val
                        and (
                            strat == "generative"
                            or getattr(self.settings, "AI_ASSISTED_NAMING", False)
                        )
                    )

                    yield LabeledSwitch(
                        label="AI Consent Granted",
                        value=ai_consent_val,
                        switch_id="switch-ai-consent",
                        tooltip="Toggle AI consent for semantic document classification and processing",
                    )

                    yield LabeledSwitch(
                        label="AI Assisted Naming",
                        value=ai_assisted_val,
                        switch_id="switch-ai-assisted-naming",
                        disabled=not ai_consent_val,
                        tooltip="Rank semantic keyphrases via local ONNX embeddings or generative AI (requires AI Consent Granted)",
                    )

                    yield LabeledSwitch(
                        label="AI Contextual Renaming",
                        value=bool(
                            getattr(self.settings, "CONTEXTUAL_RENAMING", False)
                        ),
                        switch_id="switch-contextual-renaming",
                        tooltip="Replace generic scan stems (e.g. scan_01) with document keywords while keeping descriptive stems intact unless {keywords} is in template",
                    )

                    yield Label(
                        "Default Rename Template:",
                        classes="field-label",
                    )
                    yield Label(
                        "Applies standalone with literal {stem}/{original}; enable AI Contextual Renaming or add {keywords} to insert document terms.",
                        classes="setting-hint",
                        id="lbl-rename-template-hint",
                    )
                    ren_pat_val = str(
                        getattr(self.settings, "RENAME_PATTERN", "") or ""
                    )
                    inp_ren_pat = Input(
                        value=ren_pat_val,
                        placeholder="e.g. {date}_{stem}{extension} or {date}_{keywords}_{stem}",
                        id="input-rename-pattern",
                    )
                    inp_ren_pat.tooltip = "Standalone rename template supporting literal {stem}/{original}, extracted {keywords}, {date}, {year}, {month}, {day}, {category}, {seq}, and {extension}"
                    yield inp_ren_pat

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
                        switch_id="switch-quarantine-critical",
                        tooltip="Automatically hold CRITICAL sensitivity documents in compliance quarantine during sorting and daemon monitoring",
                    )

                    yield LabeledSwitch(
                        label="Auto-Quarantine HIGH Sensitivity",
                        value="HIGH" in current_auto_ratings,
                        switch_id="switch-quarantine-high",
                        tooltip="Automatically hold HIGH sensitivity documents in compliance quarantine during sorting and daemon monitoring",
                    )

                    yield Label(
                        "Compliance Quarantine Folder Name:",
                        classes="field-label",
                    )
                    q_dir_val = (
                        getattr(
                            self.settings,
                            "QUARANTINE_DIR_NAME",
                            "_Compliance_Quarantine",
                        )
                        or "_Compliance_Quarantine"
                    )
                    inp_qdir = Input(
                        value=str(q_dir_val),
                        placeholder="_Compliance_Quarantine",
                        id="input-quarantine-dir",
                    )
                    inp_qdir.tooltip = "Workspace subfolder name used when staging held compliance files"
                    yield inp_qdir

                    yield LabeledSwitch(
                        label="Speak Status Messages Aloud",
                        value=bool(
                            getattr(self.settings, "SPEAK_ANNOUNCEMENTS", False)
                        ),
                        switch_id="switch-speak-announcements",
                        tooltip="Read each status message aloud with the system speech command (say / spd-say)",
                    )

                    yield LabeledSwitch(
                        label="Single-Character Shortcuts",
                        value=bool(
                            getattr(
                                self.settings,
                                "ENABLE_CHARACTER_KEY_SHORTCUTS",
                                True,
                            )
                        ),
                        switch_id="switch-character-key-shortcuts",
                        tooltip="Enable single-character keyboard shortcuts in the plan tree (WCAG 2.1.4)",
                    )

                    btn_scrub = Button(
                        "Scrub AI Artifacts",
                        id="btn-scrub-artifacts",
                        variant="warning",
                    )
                    btn_scrub.tooltip = "Report vector count and cache footprint, and scrub stored vectors and model files"
                    yield btn_scrub

                    btn_templates = Button(
                        "Starter Rule Template Gallery",
                        id="btn-open-templates",
                        variant="primary",
                    )
                    btn_templates.tooltip = (
                        "View and apply pre-configured starter rule template packs"
                    )
                    yield btn_templates

                    btn_active_rules = Button(
                        "Active Rule Manager",
                        id="btn-open-rules",
                        variant="primary",
                    )
                    btn_active_rules.tooltip = "Inspect, add, edit, toggle, and delete active rules and policies"
                    yield btn_active_rules

                with TabPane(
                    "Extraction & OCR",
                    id="tab-extraction",
                    classes="modal-body",
                ):
                    yield Label(
                        "PDF Text Density Threshold (chars/page):",
                        classes="field-label",
                    )
                    density_val = getattr(
                        self.settings, "PDF_TEXT_DENSITY_THRESHOLD", 50
                    )
                    inp_density = Input(
                        value=str(density_val),
                        placeholder="50",
                        id="input-pdf-density-threshold",
                    )
                    inp_density.tooltip = "Pages with fewer embedded characters trigger EasyOCR rasterization fallback"
                    yield inp_density

                    slider_density = DensitySlider(
                        value=int(density_val),
                        min_value=0,
                        max_value=500,
                        step=5,
                        id="slider-pdf-density-threshold",
                    )
                    slider_density.tooltip = "Adjust PDF text density threshold slider"
                    yield slider_density

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

    def on_mount(self) -> None:
        """Focus initial input field on mount and emit announcement."""
        super().on_mount()
        self.query_one("#input-protected", Input).focus(scroll_visible=False)
        self.query_one(".modal-box", ModalBox).scroll_home(
            animate=False, immediate=True
        )
        for sw_id in (
            "#switch-character-key-shortcuts",
            "#switch-quarantine-critical",
            "#switch-quarantine-high",
        ):
            try:
                sw = self.query_one(sw_id, Switch)
                self._announced_switch_values[sw_id] = bool(sw.value)
                self.watch(
                    sw,
                    "value",
                    lambda val, sid=sw_id: self._announce_switch_toggle(sid, bool(val)),
                    init=False,
                )
            except Exception:
                pass
        self.announce("Opened application settings dialog.")

    def _announce_switch_toggle(self, switch_id: str, value: bool) -> None:
        """Emit deduplicated accessibility announcement when a settings switch changes."""
        if self._announced_switch_values.get(switch_id) == value:
            return
        self._announced_switch_values[switch_id] = value
        state_str = "enabled" if value else "disabled"
        if not hasattr(self, "announce"):
            return
        if switch_id == "#switch-character-key-shortcuts":
            self.announce(f"Character key shortcuts {state_str}.", priority="polite")
        elif switch_id == "#switch-quarantine-critical":
            self.announce(
                f"Automatic CRITICAL sensitivity quarantine {state_str}.",
                priority="polite",
            )
        elif switch_id == "#switch-quarantine-high":
            self.announce(
                f"Automatic HIGH sensitivity quarantine {state_str}.",
                priority="polite",
            )

    def _get_scoped_rule_settings(self) -> Any:
        """Resolve folder-scoped settings for rule dialogs when a workspace folder is active."""
        from app.config import resolve_scoped_settings

        get_active_fn = (
            getattr(self.app, "get_active_settings", None)
            if hasattr(self, "app") and self.app
            else None
        )
        if callable(get_active_fn):
            return get_active_fn()
        base_dir = (
            getattr(self.app, "base_dir", None)
            if hasattr(self, "app") and self.app
            else None
        )
        return resolve_scoped_settings(self.settings, base_dir, fallback_to_global=True)

    @on(Button.Pressed, "#btn-open-templates")
    def action_open_templates(self) -> None:
        """Open starter rule template gallery modal."""
        self.app.push_screen(TemplateGalleryModal(self._get_scoped_rule_settings()))

    @on(Button.Pressed, "#btn-open-rules")
    def action_open_active_rules(self) -> None:
        """Open active rule manager modal."""
        from app.ui.modals.rules import ActiveRuleManagerModal

        def on_rules_closed(mutated: bool | None = None) -> None:
            if mutated:
                update_fn = getattr(self.app, "update_status", None)
                if callable(update_fn):
                    update_fn()
                if getattr(self.app, "base_dir", None):
                    scan_fn = getattr(self.app, "action_scan_directory", None)
                    if callable(scan_fn):
                        scan_fn()

        self.app.push_screen(
            ActiveRuleManagerModal(self._get_scoped_rule_settings()), on_rules_closed
        )

    @on(DensitySlider.Changed, "#slider-pdf-density-threshold")
    def _on_density_slider_changed(self, event: DensitySlider.Changed) -> None:
        try:
            inp = self.query_one("#input-pdf-density-threshold", Input)
            if inp.value != str(event.value):
                inp.value = str(event.value)
        except Exception:
            pass

    @on(Input.Changed, "#input-pdf-density-threshold")
    def _on_density_input_changed(self, event: Input.Changed) -> None:
        try:
            val = int(event.value.strip())
            slider = self.query_one("#slider-pdf-density-threshold", DensitySlider)
            if slider.value != val and slider.min_value <= val <= slider.max_value:
                slider.value = val
        except Exception:
            pass

    @on(Button.Pressed, "#btn-scrub-artifacts")
    def action_scrub_artifacts(self) -> None:
        """Open Scrub AI Artifacts confirmation modal."""
        db_inst = getattr(self.app, "db", None) if hasattr(self, "app") else None
        v_count = _get_vector_count(db_inst)
        footprint_mb = _get_model_cache_footprint_mb()

        def _on_scrub_modal_result(confirmed: bool | None) -> None:
            if confirmed:
                self._execute_scrub_and_purge()

        if hasattr(self, "app") and self.app:
            self.app.push_screen(
                ScrubArtifactsModal(v_count, footprint_mb), _on_scrub_modal_result
            )

    def _set_ai_assisted_switch_enabled(
        self, enabled: bool, value: bool | None = None
    ) -> None:
        """Synchronize disabled and value state on #switch-ai-assisted-naming and its row."""
        try:
            sw = self.query_one("#switch-ai-assisted-naming", Switch)
            self._syncing_ai_switches = True
            try:
                sw.disabled = not enabled
                if sw.parent is not None and isinstance(sw.parent, LabeledSwitch):
                    sw.parent.disabled = not enabled
                if value is not None:
                    sw.value = value
            finally:
                self._syncing_ai_switches = False
        except Exception:
            pass

    @on(Switch.Changed, "#switch-ai-assisted-naming")
    def on_ai_assisted_naming_changed(self, event: Switch.Changed) -> None:
        """Record explicit user toggles on AI Assisted Naming."""
        if self._syncing_ai_switches:
            return
        self._ai_assisted_naming_touched = True
        state_str = "enabled" if event.value else "disabled"
        if hasattr(self, "announce"):
            self.announce(f"AI assisted naming {state_str}.", priority="polite")

    @on(Select.Changed, "#select-strategy")
    def on_strategy_changed(self, event: Select.Changed) -> None:
        """Synchronize AI Assisted Naming switch when sorting strategy changes."""
        try:
            ai_consent_on = bool(self.query_one("#switch-ai-consent", Switch).value)
        except Exception:
            ai_consent_on = bool(getattr(self.settings, "AI_CONSENT_GRANTED", False))
        if not ai_consent_on:
            self._set_ai_assisted_switch_enabled(False, value=False)
            return
        if event.value == "generative":
            self._set_ai_assisted_switch_enabled(True, value=True)
        else:
            initial_strat = str(
                getattr(self.settings, "SORTING_STRATEGY", "default") or "default"
            )
            if initial_strat == "generative" and not self._ai_assisted_naming_touched:
                self._set_ai_assisted_switch_enabled(True, value=False)

    @on(Switch.Changed, "#switch-ai-consent")
    def on_ai_consent_changed(self, event: Switch.Changed) -> None:
        """Prompt user confirmation when AI consent switch is toggled off."""
        if event.value:
            self._staged_consent_revocation = False
            strat_now = "default"
            with contextlib.suppress(Exception):
                strat_now = str(
                    self.query_one("#select-strategy", Select).value or "default"
                )
            restore_val = True if strat_now == "generative" else None
            self._set_ai_assisted_switch_enabled(True, value=restore_val)
            return

        self._set_ai_assisted_switch_enabled(False, value=False)

        db_inst = getattr(self.app, "db", None) if hasattr(self, "app") else None
        v_count = _get_vector_count(db_inst)
        footprint_mb = _get_model_cache_footprint_mb()

        def _on_revoke_modal_result(confirmed: bool | None) -> None:
            if confirmed:
                self._staged_consent_revocation = True
                self._set_ai_assisted_switch_enabled(False, value=False)
                if hasattr(self, "announce"):
                    self.announce(
                        "AI consent revocation staged; save settings to apply.",
                        priority="polite",
                    )
            else:
                self._staged_consent_revocation = False
                event.switch.value = True

        if hasattr(self, "app") and self.app and self.is_current:
            self.app.push_screen(
                ConsentRevocationModal(v_count, footprint_mb),
                _on_revoke_modal_result,
            )

    @on(Switch.Changed, "#switch-character-key-shortcuts")
    def on_character_shortcuts_changed(self, event: Switch.Changed) -> None:
        """Announce shortcut status update when single-character shortcuts switch is toggled."""
        self._announce_switch_toggle(
            "#switch-character-key-shortcuts", bool(event.value)
        )

    @on(Switch.Changed, "#switch-quarantine-critical")
    def on_quarantine_critical_changed(self, event: Switch.Changed) -> None:
        """Announce status update when CRITICAL sensitivity auto-quarantine switch is toggled."""
        self._announce_switch_toggle("#switch-quarantine-critical", bool(event.value))

    @on(Switch.Changed, "#switch-quarantine-high")
    def on_quarantine_high_changed(self, event: Switch.Changed) -> None:
        """Announce status update when HIGH sensitivity auto-quarantine switch is toggled."""
        self._announce_switch_toggle("#switch-quarantine-high", bool(event.value))

    def _execute_scrub_and_purge(self) -> None:
        """Execute memory model unload, database vector clearing, and background model cache purging."""
        try:
            from app.core.shared_registry import SharedModelRegistry

            SharedModelRegistry.get_instance().unload_all_models()
        except Exception:
            pass

        try:
            app_db = getattr(self.app, "db", None) if hasattr(self, "app") else None
            if app_db is not None:
                app_db.clear_all_document_vectors()
        except Exception:
            pass

        try:
            from app.core.db import Database

            Database.scrub_all_vectors()
        except Exception:
            pass

        try:
            from app.config import get_app_dir
            from app.core.downloader import DownloadManager

            model_dir = str(get_app_dir() / "model")
            dm = DownloadManager.get_instance()

            def _on_purge_done(success: bool, err: Exception | None):
                if success:
                    if hasattr(self, "announce"):
                        self.announce(
                            "AI artifacts scrubbed and model cache purged successfully.",
                            priority="polite",
                        )
                else:
                    if hasattr(self, "announce"):
                        self.announce(
                            f"Model cache purge failed: {err}", priority="assertive"
                        )

            dm.delete_model_async(model_dir, on_done=_on_purge_done)
        except Exception as e:
            logger.error(f"Error purging model cache: {e}")

        try:
            from app.ui.notifications import notify

            notify(
                "AI artifacts scrubbed and model cache purging started.", type="info"
            )
        except Exception:
            pass
        if hasattr(self, "announce"):
            self.announce("AI artifacts scrubbed and model cache purging started.")

    def _report_settings_error(self, message: str) -> None:
        """Show contextual Help button and emit a settings validation error."""
        help_url = f"{TROUBLESHOOTING_URL}#settings-configuration"
        self._show_help_button(help_url)
        from app.ui.notifications import notify

        err_msg = f"Settings error: {message}"
        notify(err_msg, type="error", help_url=help_url)
        self.announce(err_msg, help_url=help_url)

    @on(Button.Pressed, "#btn-save")
    def action_save(self) -> None:
        """Save settings and dismiss modal."""
        from app.config import Settings

        p_str = self.query_one("#input-protected", Input).value
        raw_p_list = [p.strip() for p in p_str.split(",") if p.strip()]
        expanded_p_list = [
            os.path.expanduser(p) if p.startswith("~") else p for p in raw_p_list
        ]
        try:
            p_list = Settings.validate_protected_paths(expanded_p_list)
        except ValueError as e:
            self._report_settings_error(str(e))
            return

        i_str = self.query_one("#input-ignored", Input).value
        raw_i_list = [i.strip() for i in i_str.split(",") if i.strip()]
        try:
            i_list = Settings.validate_ignored_extensions(raw_i_list)
        except ValueError as e:
            self._report_settings_error(str(e))
            return

        try:
            conc_val = self.query_one("#input-concurrency", Input).value.strip()
            conc = int(conc_val)
            if not (1 <= conc <= 64):
                raise ValueError("Worker concurrency must be between 1 and 64.")
        except ValueError as e:
            self._report_settings_error(str(e))
            return

        try:
            max_f_val = self.query_one("#input-max-folders", Input).value.strip()
            max_f = int(max_f_val)
            if not (1 <= max_f <= 50):
                raise ValueError("Max folders must be between 1 and 50.")
        except ValueError as e:
            self._report_settings_error(str(e))
            return

        try:
            density_val_str = self.query_one(
                "#input-pdf-density-threshold", Input
            ).value.strip()
            pdf_density = int(density_val_str)
            if pdf_density < 0:
                raise ValueError("PDF text density threshold must be non-negative.")
        except ValueError as e:
            self._report_settings_error(str(e))
            return

        strat = self.query_one("#select-strategy", Select).value
        if strat == Select.BLANK:
            strat = "default"

        try:
            clin_renaming = self.query_one("#switch-clinical-renaming", Switch).value
        except Exception:
            clin_renaming = bool(
                getattr(self.settings, "CLINICAL_SMART_RENAMING", False)
            )
        try:
            ctx_renaming = self.query_one("#switch-contextual-renaming", Switch).value
        except Exception:
            ctx_renaming = bool(getattr(self.settings, "CONTEXTUAL_RENAMING", False))
        try:
            speak = self.query_one("#switch-speak-announcements", Switch).value
        except Exception:
            speak = bool(getattr(self.settings, "SPEAK_ANNOUNCEMENTS", False))
        try:
            char_shortcuts = self.query_one(
                "#switch-character-key-shortcuts", Switch
            ).value
        except Exception:
            char_shortcuts = bool(
                getattr(self.settings, "ENABLE_CHARACTER_KEY_SHORTCUTS", True)
            )
        try:
            ai_consent = self.query_one("#switch-ai-consent", Switch).value
        except Exception:
            ai_consent = bool(getattr(self.settings, "AI_CONSENT_GRANTED", False))

        try:
            conflict_pol = self.query_one("#select-conflict-policy", Select).value
            if conflict_pol == Select.BLANK:
                conflict_pol = "rename"
        except Exception:
            conflict_pol = getattr(self.settings, "CONFLICT_POLICY", "rename")

        try:
            dup_sel_val = self.query_one("#select-duplicate-action", Select).value
            duplicate_action = (
                str(dup_sel_val)
                if dup_sel_val is not None and dup_sel_val != Select.BLANK
                else str(getattr(self.settings, "DUPLICATE_ACTION", "off") or "off")
            )
        except Exception:
            duplicate_action = str(
                getattr(self.settings, "DUPLICATE_ACTION", "off") or "off"
            )

        try:
            det_sel_val = self.query_one("#select-detect-original-by", Select).value
            detect_original_by = (
                str(det_sel_val)
                if det_sel_val is not None and det_sel_val != Select.BLANK
                else str(
                    getattr(self.settings, "DETECT_ORIGINAL_BY", "first_seen")
                    or "first_seen"
                )
            )
        except Exception:
            detect_original_by = str(
                getattr(self.settings, "DETECT_ORIGINAL_BY", "first_seen")
                or "first_seen"
            )

        try:
            q_dir_raw = self.query_one("#input-quarantine-dir", Input).value.strip()
        except Exception:
            q_dir_raw = getattr(
                self.settings, "QUARANTINE_DIR_NAME", "_Compliance_Quarantine"
            )
        q_dir_str = q_dir_raw or "_Compliance_Quarantine"
        from app.core.path_utils import validate_target_path

        try:
            if any(ch in q_dir_str for ch in ("*", "?", "[", "]")):
                raise ValueError(
                    "Wildcard characters are not allowed in quarantine folder name."
                )
            validate_target_path(q_dir_str, keyword="QUARANTINE_DIR_NAME")
        except ValueError as e:
            self._report_settings_error(f"Invalid quarantine folder name: {e}")
            return

        existing_ratings = [
            str(r).upper()
            for r in (getattr(self.settings, "AUTO_QUARANTINE_RATINGS", []) or [])
            if r and str(r).upper() not in ("CRITICAL", "HIGH")
        ]
        try:
            q_crit = self.query_one("#switch-quarantine-critical", Switch).value
        except Exception:
            q_crit = "CRITICAL" in {
                str(r).upper()
                for r in (getattr(self.settings, "AUTO_QUARANTINE_RATINGS", []) or [])
            }
        try:
            q_high = self.query_one("#switch-quarantine-high", Switch).value
        except Exception:
            q_high = "HIGH" in {
                str(r).upper()
                for r in (getattr(self.settings, "AUTO_QUARANTINE_RATINGS", []) or [])
            }
        auto_quarantine_ratings: list[str] = []
        if q_crit:
            auto_quarantine_ratings.append("CRITICAL")
        if q_high:
            auto_quarantine_ratings.append("HIGH")
        for extra_r in existing_ratings:
            if extra_r not in auto_quarantine_ratings:
                auto_quarantine_ratings.append(extra_r)

        try:
            cleanup_empty = self.query_one(
                "#switch-cleanup-empty-folders", Switch
            ).value
        except Exception:
            cleanup_empty = bool(getattr(self.settings, "CLEANUP_EMPTY_FOLDERS", True))
        try:
            preserve_hier = self.query_one("#switch-preserve-hierarchy", Switch).value
        except Exception:
            preserve_hier = bool(getattr(self.settings, "PRESERVE_HIERARCHY", False))

        try:
            scan_depth_str = self.query_one(
                "#input-scan-max-depth", Input
            ).value.strip()
        except Exception:
            scan_depth_str = str(getattr(self.settings, "SCAN_MAX_DEPTH", 0))
        try:
            scan_max_depth = int(scan_depth_str)
            if not (0 <= scan_max_depth <= 20):
                raise ValueError("Max Scan Depth must be between 0 and 20.")
        except ValueError:
            self._report_settings_error(
                f"Invalid Max Scan Depth '{scan_depth_str}'. "
                "Expected an integer from 0 to 20."
            )
            return

        try:
            rename_pat_raw = self.query_one(
                "#input-rename-pattern", Input
            ).value.strip()
        except Exception:
            rename_pat_raw = str(getattr(self.settings, "RENAME_PATTERN", "") or "")
        if rename_pat_raw:
            from app.core.path_utils import ILLEGAL_PATH_CHARS_SET

            if (
                "/" in rename_pat_raw
                or "\\" in rename_pat_raw
                or ".." in rename_pat_raw
                or "\x00" in rename_pat_raw
                or any(ch in ILLEGAL_PATH_CHARS_SET for ch in rename_pat_raw)
            ):
                self._report_settings_error(
                    "Default rename template cannot contain path separators, '..', "
                    "null bytes, or illegal filename characters."
                )
                return

        if not ai_consent and (
            self._staged_consent_revocation
            or getattr(self.settings, "AI_CONSENT_GRANTED", None) is True
        ):
            self._staged_consent_revocation = False
            self._execute_scrub_and_purge()

        initial_strat = str(
            getattr(self.settings, "SORTING_STRATEGY", "default") or "default"
        )
        try:
            sw_ai_assisted_val = bool(
                self.query_one("#switch-ai-assisted-naming", Switch).value
            )
        except Exception:
            sw_ai_assisted_val = bool(
                getattr(self.settings, "AI_ASSISTED_NAMING", False)
            )

        if not ai_consent:
            ai_assisted_naming = False
        elif strat == "generative":
            ai_assisted_naming = True
        elif initial_strat == "generative" and not self._ai_assisted_naming_touched:
            ai_assisted_naming = False
        else:
            ai_assisted_naming = sw_ai_assisted_val

        res = {
            "PROTECTED_PATHS": p_list,
            "IGNORED_EXTENSIONS": i_list,
            "MAX_WORKERS": conc,
            "MAX_FOLDERS": max_f,
            "PDF_TEXT_DENSITY_THRESHOLD": pdf_density,
            "CONFLICT_POLICY": conflict_pol,
            "DUPLICATE_ACTION": duplicate_action,
            "DETECT_ORIGINAL_BY": detect_original_by,
            "SORTING_STRATEGY": strat,
            "AI_ASSISTED_NAMING": ai_assisted_naming,
            "CLINICAL_SMART_RENAMING": clin_renaming,
            "CONTEXTUAL_RENAMING": ctx_renaming,
            "RENAME_PATTERN": rename_pat_raw,
            "AI_CONSENT_GRANTED": ai_consent,
            "SPEAK_ANNOUNCEMENTS": speak,
            "ENABLE_CHARACTER_KEY_SHORTCUTS": char_shortcuts,
            "CLEANUP_EMPTY_FOLDERS": cleanup_empty,
            "PRESERVE_HIERARCHY": preserve_hier,
            "SCAN_MAX_DEPTH": scan_max_depth,
            "AUTO_QUARANTINE_RATINGS": auto_quarantine_ratings,
            "QUARANTINE_DIR_NAME": q_dir_str,
        }
        self.announce("Saved application settings.")
        self.dismiss(res)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        """Cancel settings modification."""
        self._staged_consent_revocation = False
        self.announce("Cancelled settings modification.")
        self.dismiss(None)

    @on(Button.Pressed, "#btn-model-mgr")
    def action_open_model_manager(self) -> None:
        """Open model manager modal."""
        if hasattr(self, "app") and self.app:
            self.app.push_screen(ModelManagerModal(self.settings))
