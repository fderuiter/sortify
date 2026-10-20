"""Settings dialog and its scrub-artifacts and consent-revocation confirmations."""

import logging
import os
from typing import Any, Dict, Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Switch, TabbedContent, TabPane

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin
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
        from app.core.db_worker import DBWorker
        from app.core.store import open_store

        return open_store().database(DBWorker()).get_document_vectors_count()
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


class ScrubArtifactsModal(A11yMixin, ModalScreen[bool]):
    """Modal dialog for confirming manual scrubbing of AI vector database rows and model cache files."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    ScrubArtifactsModal {
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
    .modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
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
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    """

    def __init__(self, vector_count: int, cache_size_mb: float):
        super().__init__()
        self.vector_count = vector_count
        self.cache_size_mb = cache_size_mb

    def compose(self) -> ComposeResult:
        """Compose modal layout with warning labels and action buttons."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Scrub AI Artifacts Confirmation", level=1, classes="modal-title"
            )
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
            yield Label(
                "Warning: This operation will permanently remove all stored document vector embeddings "
                "from the SQLite database and purge local model cache files from disk. "
                "Are you sure you want to scrub AI artifacts?",
                classes="warning-label",
            )
            with Horizontal(classes="button-row"):
                yield Button("Cancel", id="btn-cancel-scrub", variant="default")
                yield Button(
                    "Scrub AI Artifacts", id="btn-confirm-scrub", variant="error"
                )

    @on(Button.Pressed, "#btn-confirm-scrub")
    def action_confirm(self) -> None:
        """Confirm artifact scrubbing and dismiss modal with True."""
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel-scrub")
    def action_cancel(self) -> None:
        """Cancel artifact scrubbing and dismiss modal with False."""
        self.dismiss(False)


class ConsentRevocationModal(A11yMixin, ModalScreen[bool]):
    """Modal dialog for confirming revocation of AI consent and cascading artifact scrubbing."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    ConsentRevocationModal {
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
    .modal-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
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
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    """

    def __init__(self, vector_count: int, cache_size_mb: float):
        super().__init__()
        self.vector_count = vector_count
        self.cache_size_mb = cache_size_mb

    def compose(self) -> ComposeResult:
        """Compose modal layout with warning labels and consent revocation action buttons."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Confirm AI Consent Revocation", level=1, classes="modal-title"
            )
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
            yield Label(
                "Revoking AI consent will disable AI features, unload models from memory, "
                "scrub stored document vectors from SQLite tables, and purge model cache files from disk. "
                "Do you wish to proceed?",
                classes="warning-label",
            )
            with Horizontal(classes="button-row"):
                yield Button(
                    "Keep Consent Active", id="btn-cancel-revoke", variant="default"
                )
                yield Button(
                    "Revoke Consent & Scrub Data",
                    id="btn-confirm-revoke",
                    variant="error",
                )

    @on(Button.Pressed, "#btn-confirm-revoke")
    def action_confirm(self) -> None:
        """Confirm consent revocation and dismiss modal with True."""
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel-revoke")
    def action_cancel(self) -> None:
        """Cancel consent revocation and dismiss modal with False."""
        self.dismiss(False)


class SettingsModal(
    ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[Dict[str, Any]]]
):
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
    SettingsModal TabbedContent {
        height: auto;
    }
    SettingsModal TabPane {
        height: auto;
        padding: 0;
    }
    """

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.help_url = f"{TROUBLESHOOTING_URL}#settings-configuration"

    def compose(self) -> ComposeResult:
        """Compose modal dialog children."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Application Settings [Ctrl+O]",
                level=1,
                classes="modal-title",
                id="modal-title",
            )

            with TabbedContent(initial="tab-general", id="settings-tabbed-content"):
                with TabPane("General Settings", id="tab-general"):
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
                        "Ignored Extensions (comma-separated):", classes="field-label"
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

                with TabPane("AI & Privacy Settings", id="tab-ai-privacy"):
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
                        "Compliance & Renaming Toggles:", level=2, classes="field-label"
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

                    yield LabeledSwitch(
                        label="AI Consent Granted",
                        value=bool(getattr(self.settings, "AI_CONSENT_GRANTED", False)),
                        switch_id="switch-ai-consent",
                        tooltip="Toggle AI consent for semantic document classification and processing",
                    )

                    yield LabeledSwitch(
                        label="AI Contextual Renaming",
                        value=bool(
                            getattr(self.settings, "CONTEXTUAL_RENAMING", False)
                        ),
                        switch_id="switch-contextual-renaming",
                        tooltip="Toggle AI contextual file renaming",
                    )

                    yield LabeledSwitch(
                        label="Speak Status Messages Aloud",
                        value=bool(
                            getattr(self.settings, "SPEAK_ANNOUNCEMENTS", False)
                        ),
                        switch_id="switch-speak-announcements",
                        tooltip="Read each status message aloud with the system speech command (say / spd-say)",
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

                with TabPane("Extraction & OCR", id="tab-extraction"):
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
        self._update_layout(self.size.width)
        self.query_one("#input-protected", Input).focus()
        self.announce("Opened application settings dialog.")

    @on(Button.Pressed, "#btn-open-templates")
    def action_open_templates(self) -> None:
        """Open starter rule template gallery modal."""
        self.app.push_screen(TemplateGalleryModal(self.settings))

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

        def _on_scrub_modal_result(confirmed: bool) -> None:
            if confirmed:
                self._execute_scrub_and_purge()

        if hasattr(self, "app") and self.app:
            self.app.push_screen(
                ScrubArtifactsModal(v_count, footprint_mb), _on_scrub_modal_result
            )

    @on(Switch.Changed, "#switch-ai-consent")
    def on_ai_consent_changed(self, event: Switch.Changed) -> None:
        """Prompt user confirmation when AI consent switch is toggled off."""
        if not event.value:
            db_inst = getattr(self.app, "db", None) if hasattr(self, "app") else None
            v_count = _get_vector_count(db_inst)
            footprint_mb = _get_model_cache_footprint_mb()

            def _on_revoke_modal_result(confirmed: bool) -> None:
                if confirmed:
                    if self.settings:
                        self.settings.AI_CONSENT_GRANTED = False
                    if (
                        hasattr(self, "app")
                        and hasattr(self.app, "settings")
                        and self.app.settings
                    ):
                        self.app.settings.AI_CONSENT_GRANTED = False
                    self._execute_scrub_and_purge()
                    if hasattr(self, "announce"):
                        self.announce(
                            "AI consent revoked and artifacts scrubbed.",
                            priority="polite",
                        )
                else:
                    event.switch.value = True

            if hasattr(self, "app") and self.app:
                self.app.push_screen(
                    ConsentRevocationModal(v_count, footprint_mb),
                    _on_revoke_modal_result,
                )

    def _execute_scrub_and_purge(self) -> None:
        """Execute memory model unload, database vector clearing, and background model cache purging."""
        try:
            from app.core.shared_registry import SharedModelRegistry

            SharedModelRegistry.get_instance().unload_all_models()
        except Exception:
            pass

        try:
            if hasattr(self, "app") and hasattr(self.app, "db") and self.app.db:
                self.app.db.clear_all_document_vectors()
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

            def _on_purge_done(success: bool, err: Optional[Exception]):
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

        help_url = f"{TROUBLESHOOTING_URL}#settings-configuration"
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

        try:
            density_val_str = self.query_one(
                "#input-pdf-density-threshold", Input
            ).value.strip()
            pdf_density = int(density_val_str)
            if pdf_density < 0:
                raise ValueError("PDF text density threshold must be non-negative.")
        except ValueError as e:
            self._show_help_button(help_url)
            from app.ui.notifications import notify

            notify(f"Settings error: {e}", type="error", help_url=help_url)
            self.announce(f"Settings error: {e}", help_url=help_url)
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
            ai_consent = self.query_one("#switch-ai-consent", Switch).value
        except Exception:
            ai_consent = bool(getattr(self.settings, "AI_CONSENT_GRANTED", False))

        if (
            ai_consent is False
            and getattr(self.settings, "AI_CONSENT_GRANTED", None) is True
        ):
            self._execute_scrub_and_purge()

        try:
            conflict_pol = self.query_one("#select-conflict-policy", Select).value
            if conflict_pol == Select.BLANK:
                conflict_pol = "rename"
        except Exception:
            conflict_pol = getattr(self.settings, "CONFLICT_POLICY", "rename")

        res = {
            "PROTECTED_PATHS": p_list,
            "IGNORED_EXTENSIONS": i_list,
            "MAX_WORKERS": conc,
            "MAX_FOLDERS": max_f,
            "PDF_TEXT_DENSITY_THRESHOLD": pdf_density,
            "CONFLICT_POLICY": conflict_pol,
            "SORTING_STRATEGY": strat,
            "CLINICAL_SMART_RENAMING": clin_renaming,
            "CONTEXTUAL_RENAMING": ctx_renaming,
            "AI_CONSENT_GRANTED": ai_consent,
            "SPEAK_ANNOUNCEMENTS": speak,
        }
        self.announce("Saved application settings.")
        self.dismiss(res)

    @on(Button.Pressed, "#btn-help")
    def action_help(self) -> None:
        """Open settings troubleshooting documentation."""
        url = getattr(
            self,
            "help_url",
            f"{TROUBLESHOOTING_URL}#settings-configuration",
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
