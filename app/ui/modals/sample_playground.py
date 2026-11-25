"""Interactive Onboarding Sample Playground Modal with Live Rule Matrix."""

import logging
import os
from collections.abc import Callable
from typing import Any, Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, DataTable, Label, OptionList, Static
from textual.widgets.option_list import Option

from app.core.rule_templates import apply_starter_templates, get_starter_templates
from app.core.sample_corpus import (
    generate_sample_corpus,
    get_sample_corpus_items,
    simulate_corpus_classification,
)
from app.ui.heading import Heading
from app.ui.layout import ModalBox, ResponsiveModalScreen

logger = logging.getLogger(__name__)


class SamplePlaygroundModal(ResponsiveModalScreen[None]):
    """Modal screen for interactive onboarding sample playground with live rule simulation."""

    BINDINGS = [
        Binding("escape", "cancel", "Close playground", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-box {
        width: 92%;
        max-width: 100;
        min-width: 40;
        max-height: 92%;
    }
    .field-label {
        margin-top: 1;
        color: $text;
        text-style: bold;
    }
    .playground-panes {
        width: 100%;
        height: auto;
        min-height: 12;
        margin-top: 1;
        margin-bottom: 1;
    }
    .left-pane {
        width: 45%;
        height: auto;
        padding-right: 1;
    }
    .right-pane {
        width: 55%;
        height: auto;
        padding-left: 1;
    }
    #playground-doc-list {
        height: 8;
        min-height: 5;
        border: solid $accent;
        margin-bottom: 1;
    }
    #playground-doc-preview {
        height: 6;
        min-height: 4;
        border: solid $primary;
        padding: 0 1;
        background: $surface;
        overflow-y: auto;
    }
    #playground-prediction-matrix {
        height: 10;
        min-height: 7;
        border: solid $accent;
        margin-top: 1;
        margin-bottom: 1;
    }
    .button-row {
        min-height: 3;
    }
    .button-row Button {
        margin-left: 1;
    }
    Button:focus, Checkbox:focus, OptionList:focus, DataTable:focus {
        border: heavy $accent;
        text-style: bold;
    }
    SamplePlaygroundModal {
        &.compact .field-label {
            margin-top: 0;
        }
        &.compact .playground-panes {
            margin-top: 0;
            margin-bottom: 0;
            min-height: 0;
        }
        &.narrow .playground-panes {
            layout: vertical;
        }
        &.narrow .left-pane, &.narrow .right-pane {
            width: 100%;
            padding: 0;
        }
        &.compact #playground-doc-list {
            height: 6;
            margin-bottom: 0;
        }
        &.compact #playground-doc-preview {
            height: 4;
            min-height: 3;
        }
        &.compact #playground-prediction-matrix {
            height: 6;
            min-height: 6;
            margin-top: 0;
            margin-bottom: 0;
        }
        &.narrow .button-row Button {
            min-width: 0;
            padding: 0 1;
        }
    }
    """

    def __init__(
        self,
        settings: Any,
        target_dir: Optional[str] = None,
        initial_selected_packs: Optional[list[str]] = None,
        on_sync: Optional[Callable[[dict[str, Any]], None]] = None,
    ):
        super().__init__()
        self.settings = settings
        self.target_dir = target_dir or getattr(self.app, "base_dir", "") or os.getcwd()
        self.sample_items = get_sample_corpus_items()
        self.selected_file: str = "demo_finance.txt"
        self._predictions: dict[str, dict[str, Any]] = {}
        self._initial_selected_packs = (
            list(initial_selected_packs)
            if initial_selected_packs is not None
            else ["financial_tax", "personal_admin"]
        )
        self._on_sync = on_sync

    def compose(self) -> ComposeResult:
        """Compose playground modal dialog UI widgets."""
        with ModalBox(classes="modal-box"):
            with Vertical(classes="modal-body"):
                yield Heading(
                    "Interactive Sample Playground",
                    level=1,
                    classes="modal-title",
                    id="modal-title",
                )
                yield Label(
                    "Select a sample document and toggle starter rule packs to preview live classification prediction matrix without altering disk files."
                )

                with Horizontal(classes="playground-panes"):
                    # Left Pane: Sample Document Selection & Content Preview
                    with Vertical(classes="left-pane"):
                        yield Label("Sample Corpus Documents:", classes="field-label")
                        opts = [Option(fn, id=fn) for fn in self.sample_items.keys()]
                        doc_list = OptionList(*opts, id="playground-doc-list")
                        doc_list.tooltip = (
                            "Select a sample document to inspect its text content"
                        )
                        yield doc_list

                        yield Label("Document Text Preview:", classes="field-label")
                        yield Static("", id="playground-doc-preview")

                    # Right Pane: Starter Template Packs & Live Prediction Matrix
                    with Vertical(classes="right-pane"):
                        yield Label(
                            "Starter Rule Template Packs:", classes="field-label"
                        )
                        initial_set = set(self._initial_selected_packs)
                        for pack in get_starter_templates():
                            is_default = pack.id in initial_set
                            chk = Checkbox(
                                f"{pack.icon} {pack.title}",
                                value=is_default,
                                id=f"chk-playground-pack-{pack.id}",
                            )
                            chk.tooltip = pack.description
                            yield chk

                        yield Label(
                            "Live Classification Prediction Matrix:",
                            classes="field-label",
                        )
                        matrix = DataTable(id="playground-prediction-matrix")
                        matrix.tooltip = "Live classification results for each sample document across selected starter packs"
                        yield matrix

            with Horizontal(classes="button-row"):
                btn_commit = Button(
                    "Commit Sandbox to Workspace",
                    id="btn-commit-playground",
                    variant="primary",
                )
                btn_commit.tooltip = "Save selected starter packs and write sample documents into the target workspace"
                yield btn_commit
                btn_close = Button(
                    "Close Playground",
                    id="btn-close-playground",
                    variant="default",
                )
                btn_close.tooltip = (
                    "Close sample playground without writing sample files to disk"
                )
                yield btn_close

    def on_mount(self) -> None:
        """Set up prediction matrix columns, focus doc list, and evaluate predictions."""
        super().on_mount()

        matrix = self.query_one("#playground-prediction-matrix", DataTable)
        matrix.cursor_type = "row"
        matrix.add_column("Document", key="doc")
        matrix.add_column("Category", key="cat")
        matrix.add_column("Target Path", key="target")
        matrix.add_column("Confidence", key="conf")
        matrix.add_column("Matched Rule", key="rule")

        self._refresh_simulation()
        self._update_doc_preview(self.selected_file)

        try:
            self.query_one("#playground-doc-list", OptionList).focus()
        except Exception:
            pass

        self.announce("Opened interactive sample playground modal.")

    def _get_selected_pack_ids(self) -> list[str]:
        """Return list of pack IDs currently checked in the playground."""
        selected_packs = []
        for pack in get_starter_templates():
            try:
                chk = self.query_one(f"#chk-playground-pack-{pack.id}", Checkbox)
                if chk.value:
                    selected_packs.append(pack.id)
            except Exception:
                pass
        return selected_packs

    def _refresh_simulation(self) -> None:
        """Re-evaluate rule simulation and update prediction matrix table."""
        from app.config import resolve_scoped_settings

        selected_packs = self._get_selected_pack_ids()
        scoped_settings = resolve_scoped_settings(
            self.settings, self.target_dir, fallback_to_global=True
        )
        self._predictions = simulate_corpus_classification(
            selected_packs=selected_packs,
            settings=scoped_settings,
        )

        matrix = self.query_one("#playground-prediction-matrix", DataTable)
        matrix.clear()

        for fn, pred in self._predictions.items():
            conf_str = f"{int(pred.get('confidence', 0) * 100)}%"
            matrix.add_row(
                pred.get("filename", fn),
                pred.get("category", "Uncategorized"),
                pred.get("target_path", ""),
                conf_str,
                pred.get("matched_rule", "None"),
                key=fn,
            )

    def _update_doc_preview(self, filename: str) -> None:
        """Update text preview box for selected sample document."""
        self.selected_file = filename
        preview = self.query_one("#playground-doc-preview", Static)
        content = self.sample_items.get(filename, "")
        if not content:
            preview.update("[italic gray](Empty document)[/italic gray]")
        else:
            preview.update(content)

    @on(OptionList.OptionHighlighted, "#playground-doc-list")
    @on(OptionList.OptionSelected, "#playground-doc-list")
    def on_doc_option_event(
        self, event: OptionList.OptionHighlighted | OptionList.OptionSelected
    ) -> None:
        """Update document preview on OptionList highlight or selection."""
        if event.option and event.option.id:
            fn = str(event.option.id)
            self._update_doc_preview(fn)
            self.announce(f"Selected sample document {fn}.")

    def _notify_sync(self, *, committed: bool) -> None:
        """Invoke optional synchronization callback with current playground state."""
        if callable(self._on_sync):
            try:
                self._on_sync(
                    {
                        "selected_packs": self._get_selected_pack_ids(),
                        "committed": committed,
                        "target_dir": self.target_dir,
                    }
                )
            except Exception as exc:
                logger.debug(f"SamplePlaygroundModal sync callback error: {exc}")

    @on(Checkbox.Changed)
    def on_pack_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Refresh predictions live when starter rule template pack checkbox is toggled."""
        chk_id = getattr(event.checkbox, "id", None) or getattr(
            event.control, "id", None
        )
        if chk_id and chk_id.startswith("chk-playground-pack-"):
            pack_id = chk_id[len("chk-playground-pack-") :]
            self._refresh_simulation()
            self._notify_sync(committed=False)
            state = "enabled" if event.value else "disabled"
            self.announce(f"Starter template pack {pack_id} {state}.")

    @on(Button.Pressed, "#btn-commit-playground")
    def action_commit(self) -> None:
        """Apply chosen starter template packs and commit sample documents to workspace disk."""
        from app.config import resolve_scoped_settings

        selected_packs = self._get_selected_pack_ids()
        if selected_packs:
            try:
                scoped_settings = resolve_scoped_settings(
                    self.settings, self.target_dir, fallback_to_global=True
                )
                apply_starter_templates(scoped_settings, selected_packs)
                if hasattr(self.settings, "_save"):
                    self.settings._save()
            except Exception as exc:
                logger.error(f"Failed to apply starter templates on commit: {exc}")

        try:
            try:
                generate_sample_corpus(self.target_dir, overwrite=False)
                self.announce(
                    f"Committed sample documents to workspace '{self.target_dir}'."
                )
            except FileExistsError:
                self.announce(
                    f"Using the existing sample dataset in {self.target_dir}."
                )
            if hasattr(self.app, "base_dir"):
                setattr(self.app, "base_dir", self.target_dir)
            if hasattr(self.app, "source_dirs"):
                setattr(self.app, "source_dirs", [self.target_dir])
            update_cta = getattr(self.app, "_update_onboarding_cta_visibility", None)
            if callable(update_cta):
                try:
                    update_cta()
                except Exception:
                    pass
            try:
                ws_state = self.app.query_one("#onboarding-workspace-state", Static)
                ws_state.update(f"Active Workspace: {self.target_dir}")
            except Exception:
                pass
            self._notify_sync(committed=True)
            if hasattr(self.app, "notify"):
                self.app.notify(
                    "Sample corpus committed to workspace.", severity="information"
                )
        except Exception as exc:
            logger.error(f"Failed to commit sample corpus: {exc}")
            self.announce(f"Failed to commit sample documents: {exc}")

        app_ref = self.app
        was_wizard_underneath = len(app_ref.screen_stack) > 2
        self.dismiss(None)
        scan_fn = getattr(app_ref, "action_scan_directory", None)
        if not was_wizard_underneath and callable(scan_fn):
            scan_fn()

    @on(Button.Pressed, "#btn-close-playground")
    def action_cancel(self) -> None:
        """Close interactive playground without modifying disk files."""
        self.announce("Closed interactive sample playground modal.")
        self.dismiss(None)
