"""Interactive Onboarding Sample Playground Modal with Live Rule Matrix."""

import logging
import os
from typing import Any, Dict, List, Optional

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Label, OptionList, Static
from textual.widgets.option_list import Option

from app.core.rule_templates import apply_starter_templates, get_starter_templates
from app.core.sample_corpus import (
    generate_sample_corpus,
    get_sample_corpus_items,
    simulate_corpus_classification,
)
from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin

logger = logging.getLogger(__name__)


class SamplePlaygroundModal(ResponsiveLayoutMixin, A11yMixin, ModalScreen[None]):
    """Modal screen for interactive onboarding sample playground with live rule simulation."""

    BINDINGS = [
        Binding("escape", "cancel", "Close playground", show=True),
    ]

    CSS = """
    SamplePlaygroundModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 92%;
        max-width: 100;
        min-width: 40;
        height: auto;
        max-height: 92%;
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
    .playground-panes {
        width: 100%;
        height: auto;
        min-height: 16;
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
        border: solid $accent;
        margin-bottom: 1;
    }
    #playground-doc-preview {
        height: 6;
        border: solid $primary;
        padding: 0 1;
        background: $surface;
        overflow-y: auto;
    }
    #playground-prediction-matrix {
        height: 10;
        border: solid $accent;
        margin-top: 1;
        margin-bottom: 1;
    }
    .button-row {
        margin-top: 1;
        height: 3;
        align: right middle;
    }
    Button:focus, Checkbox:focus, OptionList:focus, DataTable:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, settings: Any, target_dir: Optional[str] = None):
        super().__init__()
        self.settings = settings
        self.target_dir = target_dir or getattr(self.app, "base_dir", "") or os.getcwd()
        self.sample_items = get_sample_corpus_items()
        self.selected_file: str = "demo_finance.txt"
        self._predictions: Dict[str, Dict[str, Any]] = {}

    def compose(self) -> ComposeResult:
        """Compose playground modal dialog UI widgets."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Interactive Sample Playground [Ctrl+G]",
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
                    yield OptionList(*opts, id="playground-doc-list")

                    yield Label("Document Text Preview:", classes="field-label")
                    yield Static("", id="playground-doc-preview")

                # Right Pane: Starter Template Packs & Live Prediction Matrix
                with Vertical(classes="right-pane"):
                    yield Label("Starter Rule Template Packs:", classes="field-label")
                    for pack in get_starter_templates():
                        is_default = pack.id in ("financial_tax", "personal_admin")
                        chk = Checkbox(
                            f"{pack.icon} {pack.title}",
                            value=is_default,
                            id=f"chk-playground-pack-{pack.id}",
                        )
                        chk.tooltip = pack.description
                        yield chk

                    yield Label(
                        "Live Classification Prediction Matrix:", classes="field-label"
                    )
                    yield DataTable(id="playground-prediction-matrix")

            with Horizontal(classes="button-row"):
                yield Button(
                    "Commit Sandbox to Workspace",
                    id="btn-commit-playground",
                    variant="primary",
                )
                yield Button(
                    "Close Playground",
                    id="btn-close-playground",
                    variant="default",
                )

    def on_mount(self) -> None:
        """Set up prediction matrix columns, focus doc list, and evaluate predictions."""
        self._update_layout(self.size.width)

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

    def _get_selected_pack_ids(self) -> List[str]:
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
        selected_packs = self._get_selected_pack_ids()
        self._predictions = simulate_corpus_classification(
            selected_packs=selected_packs,
            settings=self.settings,
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

    @on(Checkbox.Changed)
    def on_pack_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Refresh predictions live when starter rule template pack checkbox is toggled."""
        chk_id = getattr(event.checkbox, "id", None) or getattr(
            event.control, "id", None
        )
        if chk_id and chk_id.startswith("chk-playground-pack-"):
            pack_id = chk_id[len("chk-playground-pack-") :]
            self._refresh_simulation()
            state = "enabled" if event.value else "disabled"
            self.announce(f"Starter template pack {pack_id} {state}.")

    @on(Button.Pressed, "#btn-commit-playground")
    def action_commit(self) -> None:
        """Apply chosen starter template packs and commit sample documents to workspace disk."""
        selected_packs = self._get_selected_pack_ids()
        if selected_packs:
            try:
                apply_starter_templates(self.settings, selected_packs)
            except Exception as exc:
                logger.error(f"Failed to apply starter templates on commit: {exc}")

        try:
            generate_sample_corpus(self.target_dir, overwrite=True)
            self.announce(
                f"Committed sample documents to workspace '{self.target_dir}'."
            )
            if hasattr(self.app, "action_scan_directory"):
                self.app.action_scan_directory()
            if hasattr(self.app, "notify"):
                self.app.notify(
                    "Sample corpus committed to workspace.", severity="information"
                )
        except Exception as exc:
            logger.error(f"Failed to commit sample corpus: {exc}")
            self.announce(f"Failed to commit sample documents: {exc}")

        self.dismiss(None)

    @on(Button.Pressed, "#btn-close-playground")
    def action_cancel(self) -> None:
        """Close interactive playground without modifying disk files."""
        self.announce("Closed interactive sample playground modal.")
        self.dismiss(None)
