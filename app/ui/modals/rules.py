"""Rule template gallery and rule import dialogs."""

from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Select

from app.ui.a11y import A11yMixin
from app.ui.heading import Heading
from app.ui.layout import ResponsiveLayoutMixin


class TemplateGalleryModal(ResponsiveLayoutMixin, A11yMixin, ModalScreen[None]):
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
            yield Heading(
                "Starter Rule Template Gallery",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
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
                btn_import = Button(
                    "Import External Rules",
                    id="btn-import-rules",
                    variant="primary",
                )
                btn_import.tooltip = "Import Hazel (.hazelrules), DropIt XML/JSON, or TagSpaces taggroup.json rule files"
                yield btn_import
                btn_apply = Button(
                    "Apply Selected Templates",
                    id="btn-apply-templates",
                    variant="primary",
                )
                btn_apply.tooltip = "Merge selected template packs into settings"
                yield btn_apply

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

    @on(Checkbox.Changed)
    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Announce starter template gallery checkbox selection state changes."""
        chk_id = getattr(event.checkbox, "id", None) or getattr(
            event.control, "id", None
        )
        if not chk_id:
            return

        prefix = ""
        if chk_id.startswith("chk-gallery-"):
            prefix = "chk-gallery-"
        elif chk_id.startswith("chk-template-"):
            prefix = "chk-template-"
        else:
            return

        pack_id = chk_id[len(prefix) :]
        from app.core.rule_templates import get_starter_templates

        for pack in get_starter_templates():
            if pack.id == pack_id:
                state = "selected" if event.value else "deselected"
                self.announce(f"{pack.title} template pack {state}.")
                break

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

    @on(Button.Pressed, "#btn-import-rules")
    def action_import_rules(self) -> None:
        """Open external rule importer modal."""
        self.app.push_screen(RuleImportModal(self.settings))

    @on(Button.Pressed, "#btn-close-gallery")
    def action_close(self) -> None:
        """Close template gallery dialog."""
        self.announce("Closed starter rule template gallery.")
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Cancel on Escape key."""
        self.announce("Closed starter rule template gallery.")
        self.dismiss(None)


class RuleImportModal(ResponsiveLayoutMixin, A11yMixin, ModalScreen[Optional[Any]]):
    """Modal screen for selecting, inspecting, and merging external rules (Hazel, DropIt & TagSpaces)."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
    ]

    CSS = """
    RuleImportModal {
        align: center middle;
        background: rgba(0, 0, 0, 0.6);
    }
    .modal-box {
        padding: 1 2;
        background: $panel;
        border: thick $primary;
        width: 90%;
        max-width: 85;
        min-width: 35;
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
    .input-row {
        margin-top: 1;
        margin-bottom: 1;
        height: auto;
    }
    .preview-card {
        margin-top: 1;
        margin-bottom: 1;
        padding: 0 1;
        border: solid $accent;
        height: auto;
        max-height: 12;
        overflow-y: auto;
    }
    .warning-text {
        color: $warning;
        margin-top: 1;
    }
    .status-text {
        color: $accent;
        margin-top: 1;
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

    def __init__(self, settings: Any):
        super().__init__()
        self.settings = settings
        self.current_result: Optional[Any] = None
        self.conflicts: List[Any] = []
        self.conflict_resolutions: Dict[str, str] = {}

    def compose(self) -> ComposeResult:
        """Compose rule import modal children."""
        with Vertical(classes="modal-box"):
            yield Heading(
                "Import External Rules",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            yield Label(
                "Select a Hazel .hazelrules, DropIt XML/JSON profile, or TagSpaces taggroup.json file to import:"
            )

            with Horizontal(classes="input-row"):
                yield Input(
                    placeholder="/path/to/rules.hazelrules, profile.xml/json, or taggroup.json",
                    id="input-rule-file-path",
                )
                btn_parse = Button(
                    "Parse File", id="btn-parse-rule-file", variant="primary"
                )
                btn_parse.tooltip = "Parse selected rule file and display preview"
                yield btn_parse

            yield Label(
                "Enter file path and click 'Parse File' to preview extracted rules.",
                id="lbl-import-status",
                classes="status-text",
            )

            with Vertical(id="container-rule-preview", classes="preview-card"):
                yield Label(
                    "Imported Keywords:",
                    classes="status-text",
                    id="lbl-imported-keywords",
                )
                yield DataTable(id="dt-imported-keywords")
                yield Label(
                    "Imported Policies:",
                    classes="status-text",
                    id="lbl-imported-policies",
                )
                yield DataTable(id="dt-imported-policies")

                with Vertical(id="container-conflicts-section"):
                    yield Label(
                        "⚠️ Detected Rule Conflicts:",
                        classes="warning-text",
                        id="lbl-conflict-header",
                    )
                    with Horizontal(classes="input-row", id="row-bulk-strategy"):
                        yield Label("Bulk Resolution Strategy: ")
                        yield Select(
                            [
                                ("Keep Existing (Skip Conflicts)", "keep_existing"),
                                ("Overwrite Existing Rules", "overwrite"),
                                ("Skip Conflicting Rules", "skip_conflicting"),
                            ],
                            value="keep_existing",
                            id="select-bulk-conflict-strategy",
                            allow_blank=False,
                        )
                    yield Label(
                        "Conflict Resolution Table (Click or press Space/Enter on a row to cycle individual resolution):"
                    )
                    yield DataTable(id="dt-rule-conflicts")

                yield Vertical(id="container-import-warnings")

            with Horizontal(classes="button-row"):
                btn_close = Button("Cancel", id="btn-close-importer", variant="default")
                btn_close.tooltip = "Close import dialog"
                yield btn_close

                btn_merge = Button(
                    "Merge Rules into Settings",
                    id="btn-merge-imported-rules",
                    variant="primary",
                    disabled=True,
                )
                btn_merge.tooltip = "Safely merge extracted rules into settings according to resolution choices"
                yield btn_merge

    def on_mount(self) -> None:
        """Focus initial input field on mount and set up preview tables."""
        self._update_layout(self.size.width)
        self.query_one("#input-rule-file-path", Input).focus()
        try:
            dt_kw = self.query_one("#dt-imported-keywords", DataTable)
            dt_kw.add_columns("Keyword", "Target Path")

            dt_pol = self.query_one("#dt-imported-policies", DataTable)
            dt_pol.add_columns(
                "Type", "Expression / Pattern", "Target Path", "Priority"
            )

            dt_conf = self.query_one("#dt-rule-conflicts", DataTable)
            dt_conf.add_columns(
                "Rule Type",
                "Identifier",
                "Existing Path",
                "Imported Path",
                "Resolution",
            )
        except Exception:
            pass
        self.announce("Opened external rule importer modal.")

    def _format_resolution_label(self, strategy: str) -> str:
        """Format resolution strategy code for table display."""
        if strategy == "overwrite":
            return "Overwrite"
        elif strategy == "skip_conflicting":
            return "Skip Conflicting"
        return "Keep Existing"

    @on(Button.Pressed, "#btn-parse-rule-file")
    @on(Input.Submitted, "#input-rule-file-path")
    def action_parse_file(self) -> None:
        """Parse external rule package and render preview tables."""
        path_str = self.query_one("#input-rule-file-path", Input).value.strip()
        lbl_status = self.query_one("#lbl-import-status", Label)

        if not path_str:
            lbl_status.update("Please enter a valid rule file path.")
            self.announce("Please enter a valid rule file path.")
            return

        p = Path(path_str)
        if not p.exists():
            lbl_status.update(f"File not found: '{path_str}'")
            self.announce(f"File not found: '{path_str}'")
            return

        try:
            from app.core.rule_importers import RuleConflictAnalyzer, get_rule_importer

            importer = get_rule_importer(p)
            self.current_result = importer.parse_file(p)
            self.conflicts = RuleConflictAnalyzer.analyze(
                self.current_result, self.settings
            )
            self.conflict_resolutions = {
                c.identifier: c.resolution for c in self.conflicts
            }
        except Exception as exc:
            lbl_status.update(f"Error parsing rule package: {exc}")
            self.announce(f"Error parsing rule package: {exc}")
            return

        res = self.current_result
        dt_kw = self.query_one("#dt-imported-keywords", DataTable)
        dt_pol = self.query_one("#dt-imported-policies", DataTable)
        dt_conf = self.query_one("#dt-rule-conflicts", DataTable)
        btn_merge = self.query_one("#btn-merge-imported-rules", Button)

        dt_kw.clear()
        dt_pol.clear()
        dt_conf.clear()

        for kw, target_dir in res.keyword_rules.items():
            dt_kw.add_row(kw, target_dir)

        for pol in res.policies:
            dt_pol.add_row(
                pol.get("type", "keyword"),
                pol.get("expression", ""),
                pol.get("target_path", ""),
                str(pol.get("priority", 75)),
            )

        bulk_strategy = "keep_existing"
        try:
            val = self.query_one("#select-bulk-conflict-strategy", Select).value
            if val and val != Select.BLANK:
                bulk_strategy = str(val)
        except Exception:
            pass

        for conflict in self.conflicts:
            conflict.resolution = bulk_strategy
            self.conflict_resolutions[conflict.identifier] = bulk_strategy
            dt_conf.add_row(
                conflict.rule_type.capitalize(),
                conflict.identifier,
                conflict.existing_target,
                conflict.imported_target,
                self._format_resolution_label(bulk_strategy),
                key=conflict.identifier,
            )

        warn_container = self.query_one("#container-import-warnings", Vertical)
        warn_container.remove_children()
        for warn in res.warnings:
            warn_container.mount(Label(f"⚠️ {warn}", classes="warning-text"))

        total_rules = len(res.keyword_rules) + len(res.policies)
        lbl_status.update(
            f"Parsed {res.source_format}: {len(res.keyword_rules)} keyword rules, "
            f"{len(res.policies)} policies, {len(self.conflicts)} conflict(s), "
            f"{len(res.warnings)} warning(s)."
        )

        if total_rules > 0:
            btn_merge.disabled = False
            btn_merge.focus()
        else:
            btn_merge.disabled = True

        self.announce(
            f"Parsed {res.source_format}: {total_rules} rules extracted ({len(self.conflicts)} conflicts)."
        )

    @on(Select.Changed, "#select-bulk-conflict-strategy")
    def _on_bulk_strategy_changed(self, event: Select.Changed) -> None:
        """Apply bulk conflict resolution strategy across all detected conflicts."""
        if not event.value or event.value == Select.BLANK:
            return
        new_strategy = str(event.value)
        for conflict in self.conflicts:
            conflict.resolution = new_strategy
            self.conflict_resolutions[conflict.identifier] = new_strategy

        try:
            dt_conf = self.query_one("#dt-rule-conflicts", DataTable)
            dt_conf.clear()
            for conflict in self.conflicts:
                dt_conf.add_row(
                    conflict.rule_type.capitalize(),
                    conflict.identifier,
                    conflict.existing_target,
                    conflict.imported_target,
                    self._format_resolution_label(new_strategy),
                    key=conflict.identifier,
                )
        except Exception:
            pass
        self.announce(
            f"Updated bulk resolution strategy to: {self._format_resolution_label(new_strategy)}"
        )

    @on(DataTable.RowSelected, "#dt-rule-conflicts")
    @on(DataTable.CellSelected, "#dt-rule-conflicts")
    def _on_conflict_selected(
        self, event: Union[DataTable.RowSelected, DataTable.CellSelected]
    ) -> None:
        """Cycle resolution strategy for the selected conflict row."""
        row_key = None
        if hasattr(event, "row_key") and event.row_key and event.row_key.value:
            row_key = str(event.row_key.value)
        elif hasattr(event, "coordinate") and event.coordinate:
            try:
                dt_conf = self.query_one("#dt-rule-conflicts", DataTable)
                rk = dt_conf.coordinate_to_cell_key(event.coordinate).row_key
                if rk and rk.value:
                    row_key = str(rk.value)
            except Exception:
                pass

        if not row_key:
            return

        conflict = next((c for c in self.conflicts if c.identifier == row_key), None)
        if not conflict:
            return

        cycle_map = {
            "keep_existing": "overwrite",
            "overwrite": "skip_conflicting",
            "skip_conflicting": "keep_existing",
        }
        current_res = self.conflict_resolutions.get(
            conflict.identifier, conflict.resolution
        )
        new_res = cycle_map.get(current_res, "keep_existing")

        conflict.resolution = new_res
        self.conflict_resolutions[conflict.identifier] = new_res

        try:
            dt_conf = self.query_one("#dt-rule-conflicts", DataTable)
            dt_conf.clear()
            for c in self.conflicts:
                dt_conf.add_row(
                    c.rule_type.capitalize(),
                    c.identifier,
                    c.existing_target,
                    c.imported_target,
                    self._format_resolution_label(
                        self.conflict_resolutions.get(c.identifier, c.resolution)
                    ),
                    key=c.identifier,
                )
        except Exception:
            pass

        self.announce(
            f"Set resolution for '{conflict.identifier}' to {self._format_resolution_label(new_res)}."
        )

    @on(Button.Pressed, "#btn-merge-imported-rules")
    def action_merge_rules(self) -> None:
        """Merge parsed rules into settings and close modal."""
        if not self.current_result:
            try:
                self.dismiss(None)
            except Exception:
                pass
            return

        bulk_strategy = "keep_existing"
        try:
            val = self.query_one("#select-bulk-conflict-strategy", Select).value
            if val and val != Select.BLANK:
                bulk_strategy = str(val)
        except Exception:
            pass

        new_kw, new_pol = self.current_result.merge_into_settings(
            self.settings,
            resolutions=self.conflict_resolutions,
            default_strategy=bulk_strategy,
        )
        msg = f"Merged imported rules: {len(new_kw)} keyword rules and {len(new_pol)} policies."
        self.announce(msg)
        try:
            if hasattr(self.app, "notify"):
                self.app.notify(msg)
        except Exception:
            pass

        try:
            self.dismiss(self.current_result)
        except Exception:
            pass

    @on(Button.Pressed, "#btn-close-importer")
    def action_close(self) -> None:
        """Close import dialog."""
        self.announce("Closed external rule importer modal.")
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Cancel on Escape key."""
        self.announce("Closed external rule importer modal.")
        self.dismiss(None)
