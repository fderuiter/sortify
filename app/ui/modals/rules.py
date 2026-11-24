"""Rule template gallery and rule import dialogs."""

from pathlib import Path
from typing import Any, Optional

from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Checkbox, DataTable, Input, Label, Select

from app.ui.heading import Heading
from app.ui.layout import ModalBox, ResponsiveModalScreen


class _CancelToCloseMixin:
    """Mixin delegating Escape `action_cancel` to `action_close`."""

    def action_cancel(self) -> None:
        """Cancel on Escape key."""
        close_fn = getattr(self, "action_close", None)
        if callable(close_fn):
            close_fn()


class TemplateGalleryModal(_CancelToCloseMixin, ResponsiveModalScreen[None]):
    """Modal screen for browsing and applying starter rule template packs."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-box {
        max-width: 88;
        max-height: 92%;
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
        layout: grid;
        grid-size: 2;
        grid-columns: 1fr 1fr;
        grid-gutter: 0 1;
    }
    .button-row Button {
        width: 100%;
        min-width: 0;
        margin: 0;
    }
    Button:focus, Checkbox:focus {
        border: heavy $accent;
        text-style: bold;
    }
    TemplateGalleryModal {
        &.compact .modal-title {
            margin-bottom: 0;
        }
        &.compact .template-card {
            margin-top: 0;
            margin-bottom: 0;
        }
        &.compact .button-row {
            margin-top: 0;
        }
        &.compact .button-row Button {
            height: 1;
            min-height: 1;
            border: none;
            margin-bottom: 0;
        }
        &.narrow .button-row Button {
            padding: 0;
        }
    }
    """

    def __init__(self, settings, defer_rescan: bool = False):
        super().__init__()
        self.settings = settings
        self.defer_rescan = defer_rescan

    def compose(self) -> ComposeResult:
        """Compose template gallery children."""
        with ModalBox(classes="modal-box"):
            yield Heading(
                "Starter Rule Template Gallery",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            with Vertical(classes="modal-body"):
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
                btn_import.tooltip = "Import Hazel (.hazelrules/.plist), DropIt (.ini/.xml/.json/.dropit), or TagSpaces (taggroup.json) rule files"
                yield btn_import
                btn_export = Button(
                    "Export Rules",
                    id="btn-export-rules",
                    variant="primary",
                )
                btn_export.tooltip = "Export active rules to Sortify JSON, Hazel (.hazelrules), or DropIt (.ini)"
                yield btn_export
                btn_apply = Button(
                    "Apply Selected Templates",
                    id="btn-apply-templates",
                    variant="primary",
                )
                btn_apply.tooltip = "Merge selected template packs into settings"
                yield btn_apply

    def on_mount(self) -> None:
        """Focus initial checkbox on mount and emit announcement."""
        super().on_mount()
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
            try:
                apply_starter_templates(self.settings, selected_pack_ids)
                msg = f"Applied {len(selected_pack_ids)} starter rule template pack(s) to settings."
                self.announce(msg)
                if hasattr(self.app, "notify"):
                    self.app.notify(msg)
                if not self.defer_rescan:
                    scan_fn = getattr(self.app, "action_scan_directory", None)
                    if callable(scan_fn):
                        scan_fn()
            except Exception as exc:
                err_msg = f"Failed to apply starter rule template pack(s): {exc}"
                self.announce(err_msg)
                if hasattr(self.app, "notify"):
                    self.app.notify(err_msg, severity="error")
        else:
            self.announce("No starter template packs were selected.")

        self.dismiss(None)

    @on(Button.Pressed, "#btn-import-rules")
    def action_import_rules(self) -> None:
        """Open external rule importer modal."""
        self.app.push_screen(RuleImportModal(self.settings))

    @on(Button.Pressed, "#btn-export-rules")
    def action_export_rules(self) -> None:
        """Open export rules modal."""
        self.app.push_screen(ExportRulesModal(self.settings))

    @on(Button.Pressed, "#btn-close-gallery")
    def action_close(self) -> None:
        """Close template gallery dialog."""
        self.announce("Closed starter rule template gallery.")
        self.dismiss(None)


class ExportRulesModal(_CancelToCloseMixin, ResponsiveModalScreen[Optional[Path]]):
    """Modal screen for exporting active keyword rules and policies to external formats."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-box {
        min-width: 35;
    }
    .field-label {
        margin-top: 1;
        color: $text;
        text-style: bold;
    }
    .status-text {
        color: $accent;
        margin-top: 1;
        text-style: bold;
    }
    .error-text {
        color: $error;
        margin-top: 1;
        text-style: bold;
    }
    .summary-text {
        color: $text-muted;
        margin-top: 1;
    }
    Button:focus, Input:focus, Select:focus {
        border: heavy $accent;
        text-style: bold;
    }
    ExportRulesModal {
        &.compact .modal-box, &.narrow .modal-box {
            width: 95%;
        }
        &.compact .field-label,
        &.compact .status-text,
        &.compact .error-text,
        &.compact .summary-text {
            margin-top: 0;
        }
    }
    """

    def __init__(self, settings: Any):
        super().__init__()
        self.settings = settings

    def compose(self) -> ComposeResult:
        """Compose export rules modal children."""
        kw_count = len(getattr(self.settings, "KEYWORD_RULES", {}) or {})
        pol_count = len(getattr(self.settings, "POLICIES", []) or [])

        with ModalBox(classes="modal-box"):
            yield Heading(
                "Export Active Rules",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            with Vertical(classes="modal-body"):
                yield Label(
                    f"Export {kw_count} active keyword rule(s) and {pol_count} policy rule(s) to disk:",
                    id="lbl-export-summary",
                    classes="summary-text",
                )

                yield Label("Export Format:", classes="field-label")
                yield Select(
                    [
                        ("Sortify JSON (*.json)", "sortify_json"),
                        ("Hazel Rules (*.hazelrules)", "hazel"),
                        ("DropIt Configuration (*.ini)", "dropit_ini"),
                    ],
                    value="sortify_json",
                    id="select-export-format",
                    allow_blank=False,
                )

                yield Label("Destination File Path:", classes="field-label")
                yield Input(
                    placeholder="/path/to/exported_rules.json",
                    value="exported_rules.json",
                    id="input-export-path",
                )

                yield Label(
                    "",
                    id="lbl-export-status",
                    classes="status-text",
                )

            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel-export", variant="default")
                btn_cancel.tooltip = "Close export dialog without saving"
                yield btn_cancel

                btn_export = Button(
                    "Export Rules",
                    id="btn-do-export",
                    variant="primary",
                )
                btn_export.tooltip = "Export active rules to destination path"
                yield btn_export

    def on_mount(self) -> None:
        """Focus initial widget on mount and emit accessibility announcement."""
        super().on_mount()
        try:
            self.query_one("#input-export-path", Input).focus()
        except Exception:
            pass
        self.announce("Opened export rules modal.")

    @on(Select.Changed, "#select-export-format")
    def on_format_changed(self, event: Select.Changed) -> None:
        """Update destination filename placeholder and value when format changes."""
        if not event.value or event.value == Select.BLANK:
            return
        fmt = str(event.value)
        ext_map = {
            "sortify_json": ".json",
            "hazel": ".hazelrules",
            "dropit_ini": ".ini",
        }
        name_map = {
            "sortify_json": "Sortify JSON",
            "hazel": "Hazel (.hazelrules)",
            "dropit_ini": "DropIt (.ini)",
        }
        new_ext = ext_map.get(fmt, ".json")
        fmt_label = name_map.get(fmt, fmt)
        self.announce(f"Selected export format: {fmt_label}")

        try:
            inp = self.query_one("#input-export-path", Input)
            val = inp.value.strip()
            if val:
                p = Path(val)
                if p.suffix.lower() in (".json", ".hazelrules", ".ini"):
                    inp.value = str(p.with_suffix(new_ext))
                else:
                    inp.value = f"{val}{new_ext}"
            else:
                inp.value = f"exported_rules{new_ext}"
            inp.placeholder = f"/path/to/exported_rules{new_ext}"
        except Exception:
            pass

    @on(Button.Pressed, "#btn-do-export")
    @on(Input.Submitted, "#input-export-path")
    def action_export(self) -> None:
        """Export active rules to selected format and path."""
        from app.core.rule_templates import export_active_rules

        path_str = self.query_one("#input-export-path", Input).value.strip()
        lbl_status = self.query_one("#lbl-export-status", Label)

        if not path_str:
            err_msg = "Please provide a valid destination file path."
            lbl_status.update(err_msg)
            lbl_status.remove_class("status-text")
            lbl_status.add_class("error-text")
            self.announce(err_msg)
            if hasattr(self.app, "notify"):
                self.app.notify(err_msg, severity="error")
            return

        fmt = "sortify_json"
        try:
            sel_val = self.query_one("#select-export-format", Select).value
            if sel_val and sel_val != Select.BLANK:
                fmt = str(sel_val)
        except Exception:
            pass

        try:
            saved_path = export_active_rules(self.settings, fmt, path_str)
            success_msg = f"Successfully exported active rules to {saved_path}"
            lbl_status.update(success_msg)
            lbl_status.remove_class("error-text")
            lbl_status.add_class("status-text")
            self.announce(success_msg)
            if hasattr(self.app, "notify"):
                self.app.notify(success_msg)
            self.dismiss(saved_path)
        except Exception as exc:
            err_msg = f"Failed to export rules: {exc}"
            lbl_status.update(err_msg)
            lbl_status.remove_class("status-text")
            lbl_status.add_class("error-text")
            self.announce(err_msg)
            if hasattr(self.app, "notify"):
                self.app.notify(err_msg, severity="error")

    @on(Button.Pressed, "#btn-cancel-export")
    def action_close(self) -> None:
        """Close export modal without changes."""
        self.announce("Closed export rules modal.")
        self.dismiss(None)


class RuleImportModal(_CancelToCloseMixin, ResponsiveModalScreen[Optional[Any]]):
    """Modal screen for selecting, inspecting, and merging external rules (Hazel, DropIt & TagSpaces)."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-box {
        max-width: 88;
        min-width: 35;
        max-height: 92%;
    }
    .input-row {
        width: 100%;
        margin-top: 1;
        margin-bottom: 1;
        height: auto;
        align: left middle;
    }
    #input-rule-file-path {
        width: 1fr;
        margin-right: 1;
    }
    #btn-browse-rule-file {
        width: auto;
        min-width: 11;
        margin-right: 1;
    }
    #btn-parse-rule-file {
        width: auto;
        min-width: 12;
    }
    #chk-import-disabled {
        margin-top: 0;
        margin-bottom: 0;
    }
    #select-bulk-conflict-strategy {
        width: 1fr;
    }
    .preview-card {
        margin-top: 1;
        margin-bottom: 1;
        padding: 0 1;
        border: solid $accent;
        height: auto;
    }
    #dt-imported-keywords, #dt-imported-policies, #dt-rule-conflicts {
        width: 100%;
        height: auto;
        min-height: 5;
        max-height: 10;
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
    .button-row Button {
        min-width: 10;
        margin-left: 1;
    }
    Button:focus, Input:focus, Checkbox:focus {
        border: heavy $accent;
        text-style: bold;
    }
    RuleImportModal {
        &.compact .modal-title {
            margin-bottom: 0;
        }
        &.compact .input-row {
            margin-top: 0;
            margin-bottom: 0;
        }
        &.compact .preview-card {
            margin-top: 0;
            margin-bottom: 0;
        }
        &.compact .status-text, &.compact .warning-text {
            margin-top: 0;
        }
        &.compact .button-row {
            margin-top: 0;
        }
        &.narrow #btn-browse-rule-file,
        &.narrow #btn-parse-rule-file {
            min-width: 0;
            padding: 0 1;
        }
        &.narrow .button-row Button {
            min-width: 0;
            padding: 0 1;
        }
    }
    """

    def __init__(self, settings: Any):
        super().__init__()
        self.settings = settings
        self.current_result: Optional[Any] = None
        self.conflicts: list[Any] = []
        self.conflict_resolutions: dict[str, str] = {}
        self.import_disabled: bool = True

    def compose(self) -> ComposeResult:
        """Compose rule import modal children."""
        with ModalBox(classes="modal-box"):
            yield Heading(
                "Import External Rules",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            with Vertical(classes="modal-body"):
                yield Label(
                    "Select a Hazel (.hazelrules/.plist), DropIt (.ini/.xml/.json/.dropit), TagSpaces (taggroup.json), or Sortify JSON file to import:"
                )

                with Horizontal(classes="input-row"):
                    yield Input(
                        placeholder="/path/to/rules.hazelrules, profile.ini/xml/json/dropit, or taggroup.json",
                        id="input-rule-file-path",
                    )
                    btn_browse = Button(
                        "Browse...", id="btn-browse-rule-file", variant="default"
                    )
                    btn_browse.tooltip = "Select a rule file (.hazelrules, .plist, .ini, .xml, .json, .dropit) using the native file dialog"
                    yield btn_browse
                    btn_parse = Button(
                        "Parse File", id="btn-parse-rule-file", variant="primary"
                    )
                    btn_parse.tooltip = "Parse selected rule file and display preview"
                    yield btn_parse

                chk_disabled = Checkbox(
                    "Import new rules disabled for review",
                    value=True,
                    id="chk-import-disabled",
                )
                chk_disabled.tooltip = (
                    "Stage newly imported rules disabled until reviewed and enabled"
                )
                yield chk_disabled

                yield Label(
                    "Browse or enter a file path and click 'Parse File' to preview extracted rules.",
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
        super().on_mount()
        try:
            self.query_one("#input-rule-file-path", Input).focus()
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

    @on(Checkbox.Changed, "#chk-import-disabled")
    def _on_import_disabled_changed(self, event: Checkbox.Changed) -> None:
        """Toggle whether newly imported rules are staged disabled for review."""
        self.import_disabled = bool(event.value)
        mode_text = (
            "disabled for review" if self.import_disabled else "enabled immediately"
        )
        self.announce(f"New imported rules will be staged {mode_text}.")

    @on(Button.Pressed, "#btn-browse-rule-file")
    def action_browse_file(self) -> None:
        """Open native OS file picker to select an external rule package."""
        initial_dir: str | None = None
        try:
            raw_val = self.query_one("#input-rule-file-path", Input).value.strip()
            if raw_val:
                cand = Path(raw_val).expanduser()
                parent = cand if cand.is_dir() else cand.parent
                if parent.exists():
                    initial_dir = str(parent)
        except Exception:
            pass
        if not initial_dir:
            base_dir = getattr(self.app, "base_dir", None)
            if base_dir and Path(str(base_dir)).exists():
                initial_dir = str(base_dir)
        self._browse_rule_file_worker(initial_dir)

    @work(thread=True)
    def _browse_rule_file_worker(self, initial_dir: str | None) -> None:
        """Launch native file picker on a worker thread without blocking the TUI."""
        from app.core.native_dialog import (
            RULE_IMPORT_EXTENSIONS,
            NativeDialogError,
            select_native_file,
        )

        try:
            selected = select_native_file(
                initial_dir=initial_dir,
                title="Select Rule File to Import",
                extensions=RULE_IMPORT_EXTENSIONS,
            )
        except NativeDialogError as exc:
            self.app.call_from_thread(self._handle_browse_fallback, str(exc))
            return
        except Exception as exc:
            self.app.call_from_thread(self._handle_browse_fallback, str(exc))
            return

        if selected:
            self.app.call_from_thread(self._apply_selected_rule_file, selected)
        else:
            self.app.call_from_thread(self.announce, "Rule file selection cancelled.")

    def _apply_selected_rule_file(self, selected_path: str) -> None:
        """Populate selected rule file path into input and parse preview."""
        try:
            inp = self.query_one("#input-rule-file-path", Input)
            inp.value = selected_path
        except Exception:
            return
        self.announce(f"Selected rule file: {selected_path}")
        self.action_parse_file()

    def _handle_browse_fallback(self, reason: str) -> None:
        """Fall back to manual path input when native file dialog is unavailable."""
        msg = (
            f"Native file picker unavailable ({reason}). "
            "Please enter the rule file path manually."
        )
        try:
            self.query_one("#lbl-import-status", Label).update(msg)
            self.query_one("#input-rule-file-path", Input).focus()
        except Exception:
            pass
        self.announce(msg)

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
            pol_type_label = str(pol.get("type", "keyword"))
            if (
                str(pol.get("action", "")).lower() == "unarchive"
                and pol_type_label.lower() != "unarchive"
            ):
                pol_type_label = f"{pol_type_label} [unarchive]"
            dt_pol.add_row(
                pol_type_label,
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
        self, event: DataTable.RowSelected | DataTable.CellSelected | Any
    ) -> None:
        """Cycle resolution strategy for the selected conflict row."""
        row_key = None
        rk_attr = getattr(event, "row_key", None)
        if rk_attr is not None and getattr(rk_attr, "value", None):
            row_key = str(rk_attr.value)
        elif (coord := getattr(event, "coordinate", None)) is not None:
            try:
                dt_conf = self.query_one("#dt-rule-conflicts", DataTable)
                rk = dt_conf.coordinate_to_cell_key(coord).row_key
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

        stage_disabled = self.import_disabled
        try:
            stage_disabled = bool(
                self.query_one("#chk-import-disabled", Checkbox).value
            )
            self.import_disabled = stage_disabled
        except Exception:
            pass

        try:
            new_kw, new_pol = self.current_result.merge_into_settings(
                self.settings,
                resolutions=self.conflict_resolutions,
                default_strategy=bulk_strategy,
                import_disabled=stage_disabled,
            )
        except Exception as exc:
            err_msg = f"Failed to save imported rules: {exc}"
            self.announce(err_msg)
            try:
                from app.ui.notifications import notify

                notify(err_msg, type="error")
            except Exception:
                pass
            try:
                if hasattr(self.app, "notify"):
                    self.app.notify(err_msg, severity="error")
            except Exception:
                pass
            return

        dis_kw = getattr(self.settings, "DISABLED_KEYWORD_RULES", {}) or {}
        if stage_disabled:
            msg = (
                f"Merged imported rules (new rules staged disabled for review): "
                f"{len(new_kw)} active keyword(s), {len(dis_kw)} disabled keyword(s), "
                f"and {len(new_pol)} policies."
            )
        else:
            msg = (
                f"Merged imported rules: {len(new_kw)} keyword rules and "
                f"{len(new_pol)} policies."
            )
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


class RuleEditModal(ResponsiveModalScreen[Optional[dict]]):
    """Modal screen for adding or editing keyword rules and policies with live sample preview."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .rule-edit-box {
        width: 86%;
        max-width: 76;
        max-height: 92%;
    }
    .hidden {
        display: none;
    }
    .field-label {
        margin-top: 1;
        color: $text;
        text-style: bold;
    }
    .policy-grid {
        height: auto;
        width: 100%;
    }
    .preview-box {
        margin-top: 1;
        padding: 0 1;
        border: solid $accent;
        height: auto;
    }
    .preview-status {
        color: $text;
        margin-top: 0;
    }
    .preview-status.match {
        color: $success;
        text-style: bold;
    }
    .preview-status.no-match {
        color: $warning;
        text-style: bold;
    }
    .error-label {
        color: $error;
        text-style: bold;
        margin-top: 1;
        display: none;
    }
    .error-label.visible {
        display: block;
    }
    Button:focus, Input:focus, Select:focus, Checkbox:focus {
        border: heavy $accent;
        text-style: bold;
    }
    RuleEditModal {
        &.compact .rule-edit-box, &.narrow .rule-edit-box {
            width: 96%;
        }
        &.compact .modal-title {
            margin-bottom: 0;
        }
        &.compact .field-label {
            margin-top: 0;
        }
        &.compact .preview-box {
            margin-top: 0;
        }
        &.compact .button-row {
            margin-top: 0;
        }
        &.compact .button-row Button {
            height: 1;
            min-height: 1;
            border: none;
            margin: 0;
        }
    }
    """

    RULE_TYPE_OPTIONS: list[tuple[str, str]] = [
        ("Keyword (substring)", "keyword"),
        ("Pattern (regex / glob)", "pattern"),
        ("Override", "override"),
        ("Action", "action"),
        ("File Age (e.g. > 30d)", "age"),
        ("Modified Date (e.g. < 2025-01-01)", "modified_date"),
        ("Created Date (e.g. > 2024-01-01)", "created_date"),
        ("EXIF / PDF Date (e.g. < 2023-06-01)", "exif_date"),
        ("File Size (e.g. > 10MB)", "size"),
        ("Compliance", "compliance"),
        ("Unarchive", "unarchive"),
        ("Duplicate", "duplicate"),
    ]
    MAX_WORKSPACE_PREVIEW_FILES: int = 200

    def __init__(
        self,
        title: str = "Add Keyword Rule",
        pattern: str = "",
        target_folder: str = "",
        rule_kind: str = "keyword",
        policy_type: str = "keyword",
        priority: int = 0,
        halting: bool = False,
        action: str = "",
        enabled: bool = True,
        settings: Any = None,
        base_dir: Optional[str] = None,
        source_dirs: Optional[list[str]] = None,
        db: Any = None,
        *,
        rename_pattern: str = "",
        rule_type: Optional[str] = None,
        rule_action: Optional[str] = None,
    ):
        super().__init__()
        self.title_text = title
        self.initial_pattern = pattern
        self.initial_target = target_folder
        norm_kind = str(rule_kind or "keyword").strip().lower()
        if rule_type is not None:
            legacy_rt = str(rule_type).strip().lower()
            if norm_kind == "keyword" and legacy_rt and legacy_rt != "keyword":
                norm_kind = "policy"
            if policy_type == "keyword" and legacy_rt:
                policy_type = legacy_rt
        self.initial_kind = (
            norm_kind if norm_kind in ("keyword", "policy", "learned") else "keyword"
        )
        self.initial_policy_type = str(policy_type or "keyword").strip().lower()
        self.initial_priority = int(priority) if priority is not None else 0
        self.initial_halting = bool(halting)
        eff_action = action if action else (rule_action or "")
        self.initial_action = str(eff_action).strip()
        self.initial_rename_pattern = str(rename_pattern or "").strip()
        self.initial_enabled = bool(enabled)
        self.settings = settings
        self.base_dir = base_dir
        self.source_dirs = source_dirs
        self.db = db
        self._workspace_items: list[tuple[str, str, str]] = []
        self._text_cache: dict[str, str] = {}
        self._mounted_ready: bool = False

    def compose(self) -> ComposeResult:
        """Compose rule editor inputs, policy attributes, live sample preview, and action buttons."""
        kind_options: list[tuple[str, str]] = [
            ("Keyword Rule", "keyword"),
            ("Policy Rule", "policy"),
        ]
        if self.initial_kind == "learned":
            kind_options.insert(0, ("Learned Rule", "learned"))

        policy_types: list[tuple[str, str]] = list(self.RULE_TYPE_OPTIONS)
        known_pol_vals = {val for _lbl, val in policy_types}
        if self.initial_policy_type and self.initial_policy_type not in known_pol_vals:
            policy_types.append(
                (self.initial_policy_type.title(), self.initial_policy_type)
            )

        pol_cls = (
            "policy-grid" if self.initial_kind == "policy" else "policy-grid hidden"
        )

        with ModalBox(classes="rule-edit-box modal-box"):
            yield Heading(
                self.title_text, level=1, classes="modal-title", id="modal-title"
            )
            with Vertical(classes="modal-body"):
                yield Label("Rule Category:", classes="field-label")
                sel_kind = Select(
                    kind_options,
                    value=self.initial_kind,
                    allow_blank=False,
                    id="select-rule-kind",
                )
                sel_kind.tooltip = (
                    "Select whether to save a keyword rule or a prioritized policy"
                )
                yield sel_kind

                yield Label("Pattern / Expression:", classes="field-label")
                inp_pattern = Input(
                    value=self.initial_pattern,
                    placeholder="e.g. invoice, ^INV-\\d+, > 30d, > 10MB, < 2025-01-01",
                    id="input-rule-pattern",
                )
                inp_pattern.tooltip = "Keyword substring or policy expression (regex, glob, age, date, or size)"
                yield inp_pattern

                yield Label("Target Folder:", classes="field-label")
                inp_target = Input(
                    value=self.initial_target,
                    placeholder="e.g. Finances/Invoices or Archive/{year}/{month}",
                    id="input-rule-target",
                )
                inp_target.tooltip = (
                    "Relative destination folder inside the active workspace"
                )
                yield inp_target

                yield Label("Rename Template (optional):", classes="field-label")
                inp_rename = Input(
                    value=self.initial_rename_pattern,
                    placeholder="e.g. {date}_{stem}{extension}",
                    id="input-rule-rename-pattern",
                )
                inp_rename.tooltip = (
                    "Optional filename rename template (e.g. {date}_{stem}{extension})"
                )
                yield inp_rename

                with Vertical(id="policy-fields-container", classes=pol_cls):
                    yield Label("Policy Type:", classes="field-label")
                    sel_ptype = Select(
                        policy_types,
                        value=self.initial_policy_type,
                        allow_blank=False,
                        id="select-policy-type",
                    )
                    sel_ptype.tooltip = "Select policy evaluation strategy"
                    yield sel_ptype

                    yield Label("Priority (integer):", classes="field-label")
                    inp_prio = Input(
                        value=str(self.initial_priority),
                        placeholder="e.g. 10 (higher runs first)",
                        id="input-policy-priority",
                    )
                    inp_prio.tooltip = (
                        "Integer priority; higher-priority policies evaluate first"
                    )
                    yield inp_prio

                    yield Label("Policy Action (optional):", classes="field-label")
                    inp_action = Input(
                        value=self.initial_action,
                        placeholder="archive, retain, quarantine, redact, unarchive, trash",
                        id="input-policy-action",
                    )
                    inp_action.tooltip = "Optional policy action (archive, retain, quarantine, redact, unarchive, trash)"
                    yield inp_action

                    chk_halt = Checkbox(
                        "Halting (stop evaluating lower-priority rules on match)",
                        value=self.initial_halting,
                        id="chk-policy-halting",
                    )
                    chk_halt.tooltip = (
                        "Stop evaluating lower-priority policies when this rule matches"
                    )
                    yield chk_halt

                with Vertical(id="rule-preview-box", classes="preview-box"):
                    yield Label("Sample File Preview:", classes="field-label")
                    sel_sample = Select(
                        [],
                        allow_blank=True,
                        prompt="Select workspace sample file...",
                        id="select-sample-file",
                    )
                    sel_sample.tooltip = "Choose a workspace file to live-test this rule without moving files"
                    yield sel_sample
                    yield Label(
                        "Enter a pattern to preview workspace matches.",
                        classes="preview-status",
                        id="lbl-rule-preview",
                    )
                yield Label("", classes="error-label", id="lbl-rule-error")
            with Horizontal(classes="button-row"):
                btn_cancel = Button(
                    "Cancel", id="btn-cancel-edit-rule", variant="default"
                )
                btn_cancel.tooltip = "Cancel and discard rule changes"
                yield btn_cancel
                btn_save = Button("Save Rule", id="btn-save-rule", variant="primary")
                btn_save.tooltip = "Validate rule definition and save"
                yield btn_save

    def _discover_workspace_files(self) -> list[tuple[str, str, str]]:
        """Discover candidate workspace files as (item_id, src_root, rel_path) tuples."""
        import os

        from app.core.scanner import (
            get_files_from_sources,
            get_files_recursively,
            parse_item_identifier,
        )

        eff_settings = self.settings or getattr(self.app, "settings", None)
        eff_base = self.base_dir or getattr(self.app, "base_dir", None)
        eff_sources = self.source_dirs or getattr(self.app, "source_dirs", None)

        raw_items: list[str] = []
        if eff_sources and isinstance(eff_sources, list) and len(eff_sources) > 0:
            valid_sources = [s for s in eff_sources if s and os.path.isdir(str(s))]
            if valid_sources:
                raw_items = get_files_from_sources(valid_sources, settings=eff_settings)
        if not raw_items and eff_base and os.path.isdir(str(eff_base)):
            raw_items = get_files_recursively(str(eff_base), settings=eff_settings)

        discovered: list[tuple[str, str, str]] = []
        for item in raw_items[: self.MAX_WORKSPACE_PREVIEW_FILES]:
            s_root, rel_p = parse_item_identifier(
                item, default_base_dir=str(eff_base or "")
            )
            discovered.append((item, s_root, rel_p))
        return discovered

    def _get_item_text(self, src_root: str, rel_path: str) -> str:
        """Load cached or local offline text for a workspace item."""
        import os

        from app.core.extractor import _read_preview_text_offline

        abs_p = (
            os.path.abspath(os.path.join(src_root, rel_path))
            if src_root
            else os.path.abspath(rel_path)
        )
        if abs_p in self._text_cache:
            return self._text_cache[abs_p]

        eff_settings = self.settings or getattr(self.app, "settings", None)
        eff_db = self.db if self.db is not None else getattr(self.app, "db", None)
        raw_text, _src = _read_preview_text_offline(
            abs_p,
            settings=eff_settings,
            db=eff_db,
            base_dir=src_root or self.base_dir or getattr(self.app, "base_dir", None),
        )
        self._text_cache[abs_p] = raw_text or ""
        return self._text_cache[abs_p]

    def _read_draft_state(self) -> dict[str, Any]:
        """Collect current draft input values from form widgets."""
        kind_val = self.initial_kind
        try:
            sel_k = self.query_one("#select-rule-kind", Select).value
            if sel_k and sel_k != Select.BLANK:
                kind_val = str(sel_k)
        except Exception:
            pass

        pattern_val = self.query_one("#input-rule-pattern", Input).value.strip()
        target_val = self.query_one("#input-rule-target", Input).value.strip()

        rename_val = self.initial_rename_pattern
        try:
            rename_val = self.query_one(
                "#input-rule-rename-pattern", Input
            ).value.strip()
        except Exception:
            pass

        pol_type = self.initial_policy_type
        try:
            sel_pt = self.query_one("#select-policy-type", Select).value
            if sel_pt and sel_pt != Select.BLANK:
                pol_type = str(sel_pt)
        except Exception:
            pass

        prio_raw = str(self.initial_priority)
        try:
            prio_raw = self.query_one("#input-policy-priority", Input).value.strip()
        except Exception:
            pass

        action_val = self.initial_action
        try:
            action_val = self.query_one("#input-policy-action", Input).value.strip()
        except Exception:
            pass

        halting_val = self.initial_halting
        try:
            halting_val = bool(self.query_one("#chk-policy-halting", Checkbox).value)
        except Exception:
            pass

        return {
            "kind": kind_val,
            "pattern": pattern_val,
            "target": target_val,
            "rename_pattern": rename_val,
            "policy_type": pol_type,
            "priority_raw": prio_raw,
            "action": action_val,
            "halting": halting_val,
        }

    def _matches_draft(
        self, draft: dict[str, Any], src_root: str, rel_path: str
    ) -> bool:
        """Evaluate whether a workspace file matches the current draft rule locally."""
        import os
        import re

        from app.core.network_guard import block_external_network
        from app.core.policy_engine import METADATA_POLICY_TYPES, PolicyEngine

        pattern = draft["pattern"]
        kind = draft["kind"]
        pol_type = draft["policy_type"]
        eff_pattern = pattern
        if not eff_pattern and kind == "policy" and pol_type == "duplicate":
            eff_pattern = "*"
        if not eff_pattern and not (
            kind == "policy" and pol_type in ("unarchive", "duplicate")
        ):
            return False

        with block_external_network("inline rule preview"):
            abs_p = (
                os.path.abspath(os.path.join(src_root, rel_path))
                if src_root
                else os.path.abspath(rel_path)
            )
            if kind == "policy" and pol_type in METADATA_POLICY_TYPES:
                doc_text = ""
                status_tag: Optional[str] = None
            else:
                doc_text = self._get_item_text(src_root, rel_path)
                status_re = (
                    re.match(r"^\[STATUS:(.*?)\]$", doc_text.strip())
                    if doc_text
                    else None
                )
                status_tag = status_re.group(1) if status_re else None

            if kind == "policy":
                try:
                    prio_int = int(draft["priority_raw"] or "0")
                except ValueError:
                    prio_int = 0
                pol_dict: dict[str, Any] = {
                    "type": pol_type,
                    "expression": eff_pattern,
                    "target_path": draft["target"],
                    "priority": prio_int,
                    "halting": draft["halting"],
                    "enabled": True,
                }
                if draft["action"]:
                    pol_dict["action"] = draft["action"]
                try:
                    return bool(
                        PolicyEngine.match_policy(
                            pol_dict,
                            rel_path,
                            doc_text,
                            status_tag,
                            abs_file_path=abs_p,
                        )
                    )
                except Exception:
                    return False

            filename_only = os.path.basename(rel_path).lower()
            doc_lower = (doc_text or "").lower()
            text_to_search = (
                filename_only if status_tag else f"{filename_only} {doc_lower}"
            )
            if kind == "learned":
                try:
                    return bool(re.search(pattern, text_to_search, re.IGNORECASE))
                except re.error:
                    return pattern.lower() in text_to_search
            return pattern.lower() in text_to_search

    def _refresh_preview(self, announce_change: bool = False) -> None:
        """Recompute sample file match status and total workspace match count."""
        from app.core.policy_engine import (
            METADATA_POLICY_TYPES,
            validate_metadata_expression,
        )

        try:
            lbl = self.query_one("#lbl-rule-preview", Label)
        except Exception:
            return

        draft = self._read_draft_state()
        pattern = draft["pattern"]
        kind = draft["kind"]
        pol_type = draft["policy_type"]

        # Validate regex or metadata expression early so user gets live syntax feedback
        if kind == "policy" and pol_type == "pattern" and pattern:
            import re

            try:
                re.compile(pattern)
            except re.error as exc:
                msg = f"Invalid regex expression: {exc}"
                lbl.update(msg)
                lbl.remove_class("match")
                lbl.add_class("no-match")
                if announce_change:
                    self.announce(msg)
                return
        elif kind == "policy" and pol_type in METADATA_POLICY_TYPES and pattern:
            try:
                validate_metadata_expression(pol_type, pattern)
            except ValueError as exc:
                msg = f"Invalid {pol_type} expression: {exc}"
                lbl.update(msg)
                lbl.remove_class("match")
                lbl.add_class("no-match")
                if announce_change:
                    self.announce(msg)
                return

        selected_item: Optional[str] = None
        try:
            sel_val = self.query_one("#select-sample-file", Select).value
            if sel_val and sel_val != Select.BLANK:
                selected_item = str(sel_val)
        except Exception:
            selected_item = None

        total_files = len(self._workspace_items)
        if not pattern and not (
            kind == "policy" and pol_type in ("unarchive", "duplicate")
        ):
            if selected_item:
                disp = selected_item.split("::", 1)[-1]
                msg = f"Enter a pattern to test against '{disp}' ({total_files} workspace file(s))."
            elif total_files > 0:
                msg = f"Select a sample file or enter a pattern ({total_files} workspace file(s))."
            else:
                msg = "No active workspace folder loaded for sample preview."
            lbl.update(msg)
            lbl.remove_class("match")
            lbl.remove_class("no-match")
            if announce_change:
                self.announce(msg)
            return

        match_count = sum(
            1
            for _item_id, s_root, rel_p in self._workspace_items
            if self._matches_draft(draft, s_root, rel_p)
        )

        if selected_item:
            sample_tuple = next(
                (t for t in self._workspace_items if t[0] == selected_item), None
            )
            if sample_tuple is not None:
                _id, s_root, rel_p = sample_tuple
                is_hit = self._matches_draft(draft, s_root, rel_p)
                if is_hit:
                    msg = f"✓ MATCH on '{rel_p}' | Workspace matches: {match_count} of {total_files} file(s)"
                    lbl.remove_class("no-match")
                    lbl.add_class("match")
                else:
                    msg = f"✗ NO MATCH on '{rel_p}' | Workspace matches: {match_count} of {total_files} file(s)"
                    lbl.remove_class("match")
                    lbl.add_class("no-match")
            else:
                msg = f"Workspace matches: {match_count} of {total_files} file(s)"
                lbl.remove_class("match")
                lbl.remove_class("no-match")
        elif total_files > 0:
            msg = f"Workspace matches: {match_count} of {total_files} file(s)"
            lbl.remove_class("match")
            lbl.remove_class("no-match")
        else:
            msg = "No workspace files available to preview."
            lbl.remove_class("match")
            lbl.remove_class("no-match")

        lbl.update(msg)
        if announce_change:
            self.announce(msg)

    def on_mount(self) -> None:
        """Focus initial input on mount and load workspace sample file options."""
        super().on_mount()
        self._workspace_items = self._discover_workspace_files()
        try:
            sel_sample = self.query_one("#select-sample-file", Select)
            options = [(rel_p, item_id) for item_id, _s, rel_p in self._workspace_items]
            sel_sample.set_options(options)
        except Exception:
            pass

        self._mounted_ready = True
        self._refresh_preview(announce_change=False)
        self.query_one("#input-rule-pattern", Input).focus()

    @on(Select.Changed, "#select-rule-kind")
    def _on_rule_kind_changed(self, event: Select.Changed) -> None:
        """Show or hide policy-specific fields when switching rule category."""
        if not self._mounted_ready:
            return
        kind_str = (
            str(event.value)
            if event.value and event.value != Select.BLANK
            else "keyword"
        )
        try:
            pol_box = self.query_one("#policy-fields-container", Vertical)
            if kind_str == "policy":
                pol_box.remove_class("hidden")
            else:
                pol_box.add_class("hidden")
        except Exception:
            pass
        self._refresh_preview(announce_change=True)

    @on(Select.Changed, "#select-policy-type")
    @on(Select.Changed, "#select-sample-file")
    def _on_preview_select_changed(self, event: Select.Changed) -> None:
        """Re-evaluate sample preview when policy type or sample file selection changes."""
        if not self._mounted_ready:
            return
        self._refresh_preview(announce_change=True)

    @on(Input.Changed, "#input-rule-pattern")
    @on(Input.Changed, "#input-rule-target")
    @on(Input.Changed, "#input-policy-priority")
    @on(Input.Changed, "#input-policy-action")
    def _on_preview_input_changed(self, event: Input.Changed) -> None:
        """Re-evaluate sample preview immediately as draft rule inputs change."""
        if not self._mounted_ready:
            return
        self._refresh_preview(announce_change=True)

    @on(Checkbox.Changed, "#chk-policy-halting")
    def _on_preview_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Re-evaluate preview when halting checkbox toggles."""
        if not self._mounted_ready:
            return
        self._refresh_preview(announce_change=False)

    @on(Button.Pressed, "#btn-save-rule")
    def action_save(self) -> None:
        """Validate inputs and dismiss modal with keyword rule or policy definition."""
        from app.config import Settings
        from app.core.path_utils import ILLEGAL_PATH_CHARS_SET, validate_target_path
        from app.core.pattern_formatter import PatternTokenFormatter
        from app.core.policy_engine import PolicyEngine

        draft = self._read_draft_state()
        kind = draft["kind"]
        pattern = draft["pattern"]
        target = draft["target"]
        rename_pat = draft["rename_pattern"]
        err_lbl = self.query_one("#lbl-rule-error", Label)

        if rename_pat:
            if (
                "/" in rename_pat
                or "\\" in rename_pat
                or ".." in rename_pat
                or "\x00" in rename_pat
                or any(ch in ILLEGAL_PATH_CHARS_SET for ch in rename_pat)
            ):
                msg = (
                    "Rename template cannot contain path separators, '..', null bytes, "
                    "or illegal filename characters."
                )
                err_lbl.update(msg)
                err_lbl.add_class("visible")
                self.announce(msg, priority="assertive")
                if hasattr(self.app, "notify"):
                    self.app.notify(msg, severity="error")
                return
            preview_fn = PatternTokenFormatter.format_pattern(
                rename_pat,
                metadata={
                    "date": "2026-01-15",
                    "category": "sample",
                    "original": "document",
                    "stem": "document",
                    "extension": ".pdf",
                },
                seq=1,
                fallback_original="",
            )
            if not preview_fn or "/" in preview_fn or "\\" in preview_fn:
                msg = (
                    f"Rename template '{rename_pat}' does not produce a valid filename."
                )
                err_lbl.update(msg)
                err_lbl.add_class("visible")
                self.announce(msg, priority="assertive")
                if hasattr(self.app, "notify"):
                    self.app.notify(msg, severity="error")
                return

        if kind == "policy" or (kind in ("keyword", "learned") and rename_pat):
            pol_type = draft["policy_type"] if kind == "policy" else "keyword"
            action_raw = draft["action"] if kind == "policy" else ""

            if not pattern and pol_type == "duplicate":
                pattern = "*"

            if not pattern and pol_type not in ("unarchive", "duplicate"):
                err_lbl.update("Policy expression cannot be empty.")
                err_lbl.add_class("visible")
                self.announce(
                    "Policy expression cannot be empty.", priority="assertive"
                )
                return

            try:
                prio_int = int(draft["priority_raw"] or "0")
                if kind != "policy" and prio_int == 0:
                    prio_int = 75
            except ValueError:
                err_lbl.update("Policy priority must be a valid integer.")
                err_lbl.add_class("visible")
                self.announce(
                    "Policy priority must be a valid integer.", priority="assertive"
                )
                return

            normalized_action: Optional[str] = None
            if action_raw:
                if not PolicyEngine.validate_policy_action(action_raw):
                    supported = ", ".join(sorted(PolicyEngine.SUPPORTED_ACTIONS))
                    msg = f"Unsupported policy action '{action_raw}'. Supported actions: {supported}."
                    err_lbl.update(msg)
                    err_lbl.add_class("visible")
                    self.announce(msg, priority="assertive")
                    if hasattr(self.app, "notify"):
                        self.app.notify(msg, severity="error")
                    return
                normalized_action = str(action_raw).strip().lower()
            elif pol_type == "unarchive":
                normalized_action = "unarchive"
            elif pol_type == "duplicate" and not target:
                normalized_action = "trash"

            candidate_pol: dict[str, Any] = {
                "type": pol_type,
                "expression": pattern,
                "target_path": target,
                "priority": prio_int,
                "halting": draft["halting"] if kind == "policy" else False,
                "enabled": self.initial_enabled,
            }
            if normalized_action:
                candidate_pol["action"] = normalized_action
            if rename_pat:
                candidate_pol["rename_pattern"] = rename_pat

            try:
                validated_list = Settings.validate_policies([candidate_pol])
                validated_pol = dict(validated_list[0])
            except (ValueError, Exception) as exc:
                err_lbl.update(f"Invalid policy: {exc}")
                err_lbl.add_class("visible")
                self.announce(f"Invalid policy: {exc}", priority="assertive")
                if hasattr(self.app, "notify"):
                    self.app.notify(str(exc), severity="error")
                return

            self.dismiss(
                {
                    "kind": "policy",
                    "type": str(validated_pol.get("type", pol_type)),
                    "pattern": str(validated_pol.get("expression", pattern)),
                    "target": str(validated_pol.get("target_path", target)),
                    "rename_pattern": str(
                        validated_pol.get("rename_pattern", "") or ""
                    ),
                    "priority": int(validated_pol.get("priority", prio_int)),
                    "policy": validated_pol,
                }
            )
            return

        if not pattern:
            err_lbl.update("Rule pattern/keyword cannot be empty.")
            err_lbl.add_class("visible")
            self.announce("Rule pattern/keyword cannot be empty.", priority="assertive")
            return

        if not target:
            err_lbl.update("Target folder cannot be empty.")
            err_lbl.add_class("visible")
            self.announce("Target folder cannot be empty.", priority="assertive")
            return

        try:
            if "{" in target and "}" in target:
                PatternTokenFormatter.format_target_path(
                    target,
                    metadata={
                        "date": "2026-01-15",
                        "category": "sample",
                        "original": "document",
                        "stem": "document",
                        "extension": ".pdf",
                    },
                    seq=1,
                )
            else:
                validate_target_path(target, keyword=pattern)
        except (ValueError, Exception) as exc:
            err_lbl.update(f"Invalid path: {exc}")
            err_lbl.add_class("visible")
            self.announce(f"Invalid target path: {exc}", priority="assertive")
            if hasattr(self.app, "notify"):
                self.app.notify(str(exc), severity="error")
            return

        self.dismiss(
            {
                "kind": kind,
                "type": kind,
                "pattern": pattern,
                "target": target,
                "rename_pattern": "",
            }
        )

    @on(Button.Pressed, "#btn-cancel-edit-rule")
    def action_cancel_button(self) -> None:
        """Cancel on Cancel button."""
        self.announce("Cancelled rule editing.")
        self.dismiss(None)

    def action_cancel(self) -> None:
        """Cancel on Escape."""
        self.announce("Cancelled rule editing dialog.")
        self.dismiss(None)


class DeleteRuleConfirmModal(ResponsiveModalScreen[bool]):
    """Confirmation modal screen for deleting a rule."""

    BINDINGS = [
        Binding("escape", "cancel", "Cancel deletion", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .delete-confirm-box {
        width: 80%;
        max-width: 60;
    }
    Button:focus {
        border: heavy $accent;
        text-style: bold;
    }
    """

    def __init__(self, rule_pattern: str):
        super().__init__()
        self.rule_pattern = rule_pattern

    def compose(self) -> ComposeResult:
        """Compose confirmation dialog children."""
        with ModalBox(classes="delete-confirm-box modal-box"):
            yield Heading(
                "Confirm Delete Rule",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            with Vertical(classes="modal-body"):
                yield Label(
                    f"Are you sure you want to delete rule '{self.rule_pattern}'?\n"
                    "This action cannot be undone.",
                    id="lbl-delete-confirm-text",
                )
            with Horizontal(classes="button-row"):
                btn_cancel = Button("Cancel", id="btn-cancel-delete", variant="default")
                btn_cancel.tooltip = "Cancel and keep this rule"
                yield btn_cancel
                btn_delete = Button("Delete", id="btn-confirm-delete", variant="error")
                btn_delete.tooltip = "Permanently remove this rule"
                yield btn_delete

    def on_mount(self) -> None:
        """Focus cancel button by default to avoid accidental deletion."""
        super().on_mount()
        self.query_one("#btn-cancel-delete", Button).focus()

    @on(Button.Pressed, "#btn-confirm-delete")
    def action_confirm_delete(self) -> None:
        """Confirm deletion."""
        self.announce(f"Confirmed deletion of rule '{self.rule_pattern}'.")
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel-delete")
    def action_cancel_button(self) -> None:
        """Cancel deletion."""
        self.announce(f"Cancelled deletion of rule '{self.rule_pattern}'.")
        self.dismiss(False)

    def action_cancel(self) -> None:
        """Cancel on Escape."""
        self.announce("Cancelled delete rule dialog.")
        self.dismiss(False)


class ActiveRuleManagerModal(_CancelToCloseMixin, ResponsiveModalScreen[bool]):
    """Dedicated modal screen for viewing, adding, editing, toggling, and deleting rules."""

    BINDINGS = [
        Binding("escape", "cancel", "Close manager", show=True),
        Binding("space", "toggle_rule", "Toggle Rule", show=False),
        Binding("enter", "edit_rule", "Edit Rule", show=False),
        Binding("delete", "delete_rule", "Delete Rule", show=False),
        Binding("ctrl+i", "import_rules", "Import Rules", show=True),
        Binding("i", "import_rules", "Import Rules", show=False),
        Binding("ctrl+e", "export_rules", "Export Rules", show=True),
        Binding("e", "export_rules", "Export Rules", show=False),
        Binding("ctrl+b", "browse_templates", "Browse Templates", show=True),
        Binding("b", "browse_templates", "Browse Templates", show=False),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .rule-manager-box {
        max-width: 88;
        min-width: 35;
        max-height: 92%;
    }
    #rules-table {
        height: auto;
        max-height: 15;
        min-height: 6;
        margin-top: 1;
        margin-bottom: 1;
    }
    .button-row {
        layout: grid;
        grid-size: 4;
        grid-columns: 1fr 1fr 1fr 1fr;
        grid-gutter: 0 1;
    }
    .button-row Button {
        width: 100%;
        min-width: 0;
        padding: 0 1;
        margin: 0;
    }
    Button:focus, DataTable:focus {
        border: heavy $accent;
        text-style: bold;
    }
    ActiveRuleManagerModal {
        &.compact .modal-title {
            margin-bottom: 0;
        }
        &.compact #rules-table {
            margin-top: 0;
            margin-bottom: 0;
        }
        &.compact .button-row, &.narrow .button-row {
            margin-top: 0;
            grid-size: 2;
            grid-columns: 1fr 1fr;
        }
        &.compact .button-row Button, &.narrow .button-row Button {
            height: 1;
            min-height: 1;
            border: none;
            padding: 0;
            margin: 0;
        }
    }
    """

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self._rules_data: list[dict] = []
        self._mutated: bool = False

    def compose(self) -> ComposeResult:
        """Compose active rule manager children."""
        with ModalBox(classes="rule-manager-box modal-box"):
            yield Heading(
                "Active Rule Manager",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            with Vertical(classes="modal-body"):
                yield Label(
                    "Manage keyword rules and policies. Select a rule to edit, toggle, or delete:",
                    id="lbl-rules-desc",
                )
                yield DataTable(id="rules-table")
            with Vertical(classes="modal-footer"):
                with Horizontal(classes="button-row"):
                    btn_toggle = Button(
                        "Toggle Status", id="btn-toggle-rule", variant="default"
                    )
                    btn_toggle.tooltip = (
                        "Toggle active/disabled status of selected rule"
                    )
                    yield btn_toggle
                    btn_delete = Button(
                        "Delete Rule", id="btn-delete-rule", variant="error"
                    )
                    btn_delete.tooltip = "Delete selected rule after confirmation"
                    yield btn_delete
                    btn_edit = Button(
                        "Edit Rule", id="btn-edit-rule", variant="default"
                    )
                    btn_edit.tooltip = (
                        "Edit pattern or destination folder of selected rule"
                    )
                    yield btn_edit
                    btn_add = Button("Add Rule", id="btn-add-rule", variant="primary")
                    btn_add.tooltip = "Add a new keyword rule or policy"
                    yield btn_add

                with Horizontal(classes="button-row"):
                    btn_close = Button("Close", id="btn-close-rules", variant="default")
                    btn_close.tooltip = "Close active rule manager"
                    yield btn_close
                    btn_import = Button(
                        "Import Rules", id="btn-import-rules", variant="default"
                    )
                    btn_import.tooltip = "Import rules from Hazel, DropIt, or TagSpaces"
                    yield btn_import
                    btn_export = Button(
                        "Export Rules", id="btn-export-rules", variant="default"
                    )
                    btn_export.tooltip = (
                        "Export active rules to Sortify JSON, Hazel, or DropIt"
                    )
                    yield btn_export
                    btn_browse = Button(
                        "Browse Templates",
                        id="btn-browse-templates",
                        variant="primary",
                    )
                    btn_browse.tooltip = "Browse and apply starter rule templates"
                    yield btn_browse

    def on_mount(self) -> None:
        """Set up columns and load rules on mount."""
        super().on_mount()
        table = self.query_one("#rules-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Status", "Type", "Pattern", "Target Folder", "Priority")
        self._refresh_table()
        table.focus()
        masked_rows = [r for r in self._rules_data if r.get("masked_by")]
        if masked_rows:
            self.announce(
                f"Opened active rule manager. Warning: {len(masked_rows)} policy rule(s) masked by higher-priority rules."
            )
        else:
            self.announce("Opened active rule manager.")

    def _load_rules_data(self) -> list[dict]:
        """Aggregate active keyword rules, disabled keyword rules, learned rules, and policies with masking detection."""
        from app.core.policy_engine import is_masked_by

        rules = []
        kw_rules = getattr(self.settings, "KEYWORD_RULES", {}) or {}
        for kw, target in sorted(kw_rules.items()):
            rules.append(
                {
                    "kind": "keyword",
                    "status": "Active",
                    "enabled": True,
                    "type": "keyword",
                    "pattern": str(kw),
                    "target_folder": str(target),
                    "priority": 0,
                    "original_key": kw,
                }
            )

        dis_rules = getattr(self.settings, "DISABLED_KEYWORD_RULES", {}) or {}
        for kw, target in sorted(dis_rules.items()):
            rules.append(
                {
                    "kind": "keyword",
                    "status": "Disabled",
                    "enabled": False,
                    "type": "keyword",
                    "pattern": str(kw),
                    "target_folder": str(target),
                    "priority": 0,
                    "original_key": kw,
                }
            )

        learned_rules = getattr(self.settings, "LEARNED_RULES", {}) or {}
        for kw, target in sorted(learned_rules.items()):
            rules.append(
                {
                    "kind": "learned",
                    "status": "Active",
                    "enabled": True,
                    "type": "learned",
                    "pattern": str(kw),
                    "target_folder": str(target),
                    "priority": 0,
                    "original_key": kw,
                }
            )

        policies = getattr(self.settings, "POLICIES", []) or []
        enabled_indexed = [
            (idx, pol)
            for idx, pol in enumerate(policies)
            if isinstance(pol, dict) and pol.get("enabled", True)
        ]
        enabled_sorted = sorted(
            enabled_indexed,
            key=lambda pair: pair[1].get("priority", 0),
            reverse=True,
        )
        masked_by_idx: dict[int, str] = {}
        for pos, (low_idx, low_pol) in enumerate(enabled_sorted):
            for _high_idx, high_pol in enabled_sorted[:pos]:
                if is_masked_by(high_pol, low_pol) or bool(
                    high_pol.get("halting", False)
                ):
                    masked_by_idx[low_idx] = str(high_pol.get("expression", ""))
                    break

        for idx, pol in enumerate(policies):
            if isinstance(pol, dict):
                is_enabled = bool(pol.get("enabled", True))
                masker = masked_by_idx.get(idx) if is_enabled else None
                if not is_enabled:
                    status_str = "Disabled"
                elif masker is not None:
                    status_str = f"⚠ Masked by '{masker}'"
                else:
                    status_str = "Active"
                rules.append(
                    {
                        "kind": "policy",
                        "status": status_str,
                        "enabled": is_enabled,
                        "masked_by": masker,
                        "type": str(pol.get("type", "policy")),
                        "pattern": str(pol.get("expression", "")),
                        "target_folder": str(pol.get("target_path", "")),
                        "rename_pattern": str(pol.get("rename_pattern", "") or ""),
                        "priority": pol.get("priority", 0),
                        "halting": bool(pol.get("halting", False)),
                        "action": str(pol.get("action") or ""),
                        "policy_idx": idx,
                        "original": pol,
                    }
                )
        return rules

    def _refresh_table(self, preserve_focus: bool = True) -> None:
        """Re-populate rules table from settings while preserving cursor and focus."""
        table = self.query_one("#rules-table", DataTable)
        focused = self.focused
        had_table_focus = focused is table
        prev_cursor = table.cursor_row if table.row_count > 0 else 0

        self._rules_data = self._load_rules_data()
        table.clear()
        for idx, r in enumerate(self._rules_data):
            table.add_row(
                r["status"],
                r["type"],
                r["pattern"],
                r["target_folder"],
                str(r["priority"]),
                key=f"rule_row_{idx}",
            )

        if self._rules_data:
            target_row = max(0, min(prev_cursor, len(self._rules_data) - 1))
            table.move_cursor(row=target_row)

        if preserve_focus:
            if had_table_focus:
                table.focus()
            elif focused and getattr(focused, "id", None):
                try:
                    self.query_one(f"#{focused.id}").focus()
                except Exception:
                    table.focus()

    def _get_selected_rule(self) -> Optional[dict]:
        """Return the currently highlighted rule dictionary."""
        table = self.query_one("#rules-table", DataTable)
        if not self._rules_data or table.row_count == 0:
            return None
        row_idx = table.cursor_row
        if 0 <= row_idx < len(self._rules_data):
            return self._rules_data[row_idx]
        return None

    @on(DataTable.RowSelected, "#rules-table")
    def on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Trigger editing when pressing Enter on a rule row."""
        self.action_edit_rule()

    @on(Button.Pressed, "#btn-toggle-rule")
    def action_toggle_rule(self) -> None:
        """Toggle active/disabled status of selected rule."""
        rule = self._get_selected_rule()
        if not rule:
            self.announce("No rule selected to toggle.")
            return

        if rule["kind"] == "keyword":
            kw = rule["original_key"]
            kw_rules = dict(getattr(self.settings, "KEYWORD_RULES", {}) or {})
            dis_rules = dict(getattr(self.settings, "DISABLED_KEYWORD_RULES", {}) or {})

            if rule.get("enabled", rule["status"] == "Active"):
                target = kw_rules.pop(kw, rule["target_folder"])
                dis_rules[kw] = target
                new_status = "Disabled"
            else:
                target = dis_rules.pop(kw, rule["target_folder"])
                kw_rules[kw] = target
                new_status = "Active"

            self.settings.KEYWORD_RULES = kw_rules
            self.settings.DISABLED_KEYWORD_RULES = dis_rules
            if hasattr(self.settings, "flush"):
                self.settings.flush()
            msg = f"Toggled keyword rule '{kw}' to {new_status}."
        elif rule["kind"] == "learned":
            kw = rule["original_key"]
            learned_rules = dict(getattr(self.settings, "LEARNED_RULES", {}) or {})
            dis_rules = dict(getattr(self.settings, "DISABLED_KEYWORD_RULES", {}) or {})
            target = learned_rules.pop(kw, rule["target_folder"])
            dis_rules[kw] = target
            self.settings.LEARNED_RULES = learned_rules
            self.settings.DISABLED_KEYWORD_RULES = dis_rules
            if hasattr(self.settings, "flush"):
                self.settings.flush()
            msg = f"Toggled learned rule '{kw}' to Disabled."
        elif rule["kind"] == "policy":
            policies = [dict(p) for p in getattr(self.settings, "POLICIES", []) or []]
            idx = rule["policy_idx"]
            if 0 <= idx < len(policies):
                curr = policies[idx].get("enabled", True)
                policies[idx]["enabled"] = not curr
                new_status = "Active" if not curr else "Disabled"
                self.settings.POLICIES = policies
                if hasattr(self.settings, "flush"):
                    self.settings.flush()
                msg = f"Toggled policy '{rule['pattern']}' to {new_status}."
            else:
                return
        else:
            return

        self._mutated = True
        self._refresh_table(preserve_focus=True)
        self.announce(msg)
        if hasattr(self.app, "notify"):
            self.app.notify(msg)

    @on(Button.Pressed, "#btn-add-rule")
    def action_add_rule(self) -> None:
        """Open editor modal to add a new keyword rule or policy."""

        def on_rule_added(result: Optional[dict]) -> None:
            if not result:
                return
            res_kind = result.get("kind", "keyword")
            pattern = result["pattern"]
            target = result["target"]

            if res_kind == "policy":
                pol_payload = dict(
                    result.get("policy")
                    or {
                        "type": "keyword",
                        "expression": pattern,
                        "target_path": target,
                        "priority": 0,
                        "halting": False,
                        "enabled": True,
                    }
                )
                policies = [
                    dict(p) for p in getattr(self.settings, "POLICIES", []) or []
                ]
                policies.append(pol_payload)
                self.settings.POLICIES = policies
                if hasattr(self.settings, "flush"):
                    self.settings.flush()
                msg = f"Added policy '{pattern}' (priority {pol_payload.get('priority', 0)})."
            else:
                kw_rules = dict(getattr(self.settings, "KEYWORD_RULES", {}) or {})
                kw_rules[pattern] = target
                self.settings.KEYWORD_RULES = kw_rules
                if hasattr(self.settings, "flush"):
                    self.settings.flush()
                msg = f"Added rule '{pattern}' routing to '{target}'."

            self._mutated = True
            self._refresh_table(preserve_focus=False)
            masked_now = [
                r
                for r in self._rules_data
                if r.get("masked_by") and r.get("pattern") == pattern
            ]
            if masked_now:
                msg = f"{msg} Warning: masked by higher-priority rule '{masked_now[0]['masked_by']}'."
            self.announce(msg)
            if hasattr(self.app, "notify"):
                self.app.notify(msg)

        self.app.push_screen(
            RuleEditModal(
                title="Add Rule",
                settings=self.settings,
                base_dir=getattr(self.app, "base_dir", None),
                source_dirs=getattr(self.app, "source_dirs", None),
                db=getattr(self.app, "db", None),
            ),
            on_rule_added,
        )

    @on(Button.Pressed, "#btn-edit-rule")
    def action_edit_rule(self) -> None:
        """Open editor modal to edit the selected keyword rule or policy."""
        rule = self._get_selected_rule()
        if not rule:
            self.announce("No rule selected to edit.")
            return

        def on_rule_edited(result: Optional[dict]) -> None:
            if not result:
                return
            res_kind = result.get("kind", rule["kind"])
            new_pattern = result["pattern"]
            new_target = result["target"]

            if rule["kind"] == "policy" and res_kind == "policy":
                policies = [
                    dict(p) for p in getattr(self.settings, "POLICIES", []) or []
                ]
                idx = rule["policy_idx"]
                if 0 <= idx < len(policies):
                    if "policy" in result and isinstance(result["policy"], dict):
                        updated_pol = dict(policies[idx])
                        updated_pol.update(result["policy"])
                        if "action" not in result["policy"]:
                            updated_pol.pop("action", None)
                        if "rename_pattern" not in result["policy"]:
                            updated_pol.pop("rename_pattern", None)
                        policies[idx] = updated_pol
                    else:
                        policies[idx]["expression"] = new_pattern
                        policies[idx]["target_path"] = new_target
                    self.settings.POLICIES = policies
                    if hasattr(self.settings, "flush"):
                        self.settings.flush()
                    msg = f"Updated policy '{new_pattern}'."
                else:
                    return
            elif rule["kind"] in ("keyword", "learned") and res_kind == "policy":
                old_key = rule["original_key"]
                if rule["kind"] == "learned":
                    l_dict = dict(getattr(self.settings, "LEARNED_RULES", {}) or {})
                    l_dict.pop(old_key, None)
                    self.settings.LEARNED_RULES = l_dict
                else:
                    is_active = rule.get("enabled", rule["status"] == "Active")
                    d_name = "KEYWORD_RULES" if is_active else "DISABLED_KEYWORD_RULES"
                    r_dict = dict(getattr(self.settings, d_name, {}) or {})
                    r_dict.pop(old_key, None)
                    setattr(self.settings, d_name, r_dict)
                policies = [
                    dict(p) for p in getattr(self.settings, "POLICIES", []) or []
                ]
                policies.append(dict(result["policy"]))
                self.settings.POLICIES = policies
                if hasattr(self.settings, "flush"):
                    self.settings.flush()
                msg = f"Converted rule '{new_pattern}' into a policy."
            elif rule["kind"] == "policy" and res_kind == "keyword":
                policies = [
                    dict(p) for p in getattr(self.settings, "POLICIES", []) or []
                ]
                idx = rule["policy_idx"]
                if 0 <= idx < len(policies):
                    was_enabled = bool(policies[idx].get("enabled", True))
                    policies.pop(idx)
                    self.settings.POLICIES = policies
                    d_name = (
                        "KEYWORD_RULES" if was_enabled else "DISABLED_KEYWORD_RULES"
                    )
                    r_dict = dict(getattr(self.settings, d_name, {}) or {})
                    r_dict[new_pattern] = new_target
                    setattr(self.settings, d_name, r_dict)
                    if hasattr(self.settings, "flush"):
                        self.settings.flush()
                    msg = f"Converted policy '{new_pattern}' into a keyword rule."
                else:
                    return
            elif rule["kind"] == "keyword":
                old_key = rule["original_key"]
                is_active = rule.get("enabled", rule["status"] == "Active")
                dict_name = "KEYWORD_RULES" if is_active else "DISABLED_KEYWORD_RULES"
                rules_dict = dict(getattr(self.settings, dict_name, {}) or {})
                if old_key in rules_dict and old_key != new_pattern:
                    del rules_dict[old_key]
                rules_dict[new_pattern] = new_target
                setattr(self.settings, dict_name, rules_dict)
                if hasattr(self.settings, "flush"):
                    self.settings.flush()
                msg = f"Updated rule '{new_pattern}' target folder to '{new_target}'."
            elif rule["kind"] == "learned":
                old_key = rule["original_key"]
                learned_dict = dict(getattr(self.settings, "LEARNED_RULES", {}) or {})
                if old_key in learned_dict and old_key != new_pattern:
                    del learned_dict[old_key]
                learned_dict[new_pattern] = new_target
                self.settings.LEARNED_RULES = learned_dict
                if hasattr(self.settings, "flush"):
                    self.settings.flush()
                msg = f"Updated learned rule '{new_pattern}' target folder to '{new_target}'."
            else:
                return

            self._mutated = True
            self._refresh_table(preserve_focus=True)
            self.announce(msg)
            if hasattr(self.app, "notify"):
                self.app.notify(msg)

        orig_raw = rule.get("original")
        orig_pol: dict[str, Any] = dict(orig_raw) if isinstance(orig_raw, dict) else {}
        self.app.push_screen(
            RuleEditModal(
                title=f"Edit Rule: {rule['pattern']}",
                pattern=rule["pattern"],
                target_folder=rule["target_folder"],
                rule_kind=rule["kind"],
                policy_type=str(orig_pol.get("type", rule.get("type", "keyword"))),
                priority=int(orig_pol.get("priority", rule.get("priority", 0)) or 0),
                halting=bool(orig_pol.get("halting", False)),
                action=str(orig_pol.get("action") or ""),
                enabled=bool(rule.get("enabled", True)),
                settings=self.settings,
                base_dir=getattr(self.app, "base_dir", None),
                source_dirs=getattr(self.app, "source_dirs", None),
                db=getattr(self.app, "db", None),
                rename_pattern=str(
                    orig_pol.get("rename_pattern") or rule.get("rename_pattern") or ""
                ),
            ),
            on_rule_edited,
        )

    @on(Button.Pressed, "#btn-delete-rule")
    def action_delete_rule(self) -> None:
        """Delete selected rule after user confirmation."""
        rule = self._get_selected_rule()
        if not rule:
            self.announce("No rule selected to delete.")
            return

        pattern = rule["pattern"]

        def on_confirmed(confirmed: Optional[bool]) -> None:
            if not confirmed:
                return

            if rule["kind"] == "keyword":
                key = rule["original_key"]
                is_active = rule.get("enabled", rule["status"] == "Active")
                dict_name = "KEYWORD_RULES" if is_active else "DISABLED_KEYWORD_RULES"
                rules_dict = dict(getattr(self.settings, dict_name, {}) or {})
                rules_dict.pop(key, None)
                setattr(self.settings, dict_name, rules_dict)
                if hasattr(self.settings, "flush"):
                    self.settings.flush()
                msg = f"Deleted rule '{pattern}' from settings."
            elif rule["kind"] == "learned":
                key = rule["original_key"]
                learned_dict = dict(getattr(self.settings, "LEARNED_RULES", {}) or {})
                learned_dict.pop(key, None)
                self.settings.LEARNED_RULES = learned_dict
                if hasattr(self.settings, "flush"):
                    self.settings.flush()
                msg = f"Deleted learned rule '{pattern}' from settings."
            elif rule["kind"] == "policy":
                policies = [
                    dict(p) for p in getattr(self.settings, "POLICIES", []) or []
                ]
                idx = rule["policy_idx"]
                if 0 <= idx < len(policies):
                    policies.pop(idx)
                    self.settings.POLICIES = policies
                    if hasattr(self.settings, "flush"):
                        self.settings.flush()
                    msg = f"Deleted policy '{pattern}' from settings."
                else:
                    return
            else:
                return

            self._mutated = True
            self._refresh_table(preserve_focus=True)
            self.announce(msg)
            if hasattr(self.app, "notify"):
                self.app.notify(msg)

        self.app.push_screen(
            DeleteRuleConfirmModal(rule_pattern=pattern),
            on_confirmed,
        )

    @on(Button.Pressed, "#btn-close-rules")
    def action_close(self) -> None:
        """Close active rule manager dialog and report whether rules changed."""
        self.announce("Closed active rule manager.")
        self.dismiss(self._mutated)

    def action_close_rules(self) -> None:
        """Close active rule manager (legacy alias)."""
        self.action_close()

    @on(events.DescendantFocus)
    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """Announce focused controls for screen reader accessibility."""
        widget_id = getattr(event.widget, "id", None)
        if not widget_id:
            return

        focus_announcements = {
            "btn-import-rules": "Import Rules button focused. Import rules from Hazel, DropIt, or TagSpaces.",
            "btn-export-rules": "Export Rules button focused. Export active rules to Sortify JSON, Hazel, or DropIt.",
            "btn-browse-templates": "Browse Templates button focused. Browse and apply starter rule templates.",
            "btn-toggle-rule": "Toggle Status button focused. Toggle active or disabled status of selected rule.",
            "btn-delete-rule": "Delete Rule button focused. Delete selected rule after confirmation.",
            "btn-edit-rule": "Edit Rule button focused. Edit pattern or destination folder of selected rule.",
            "btn-add-rule": "Add Rule button focused. Add a new keyword rule.",
            "btn-close-rules": "Close button focused. Close active rule manager.",
            "rules-table": "Rules table focused. Select a rule to edit, toggle, or delete.",
        }
        msg = focus_announcements.get(widget_id)
        if msg:
            self.announce(msg)

    @on(Button.Pressed, "#btn-import-rules")
    def action_import_rules(self) -> None:
        """Open external rule importer modal."""
        self.announce("Opening external rule importer modal.")

        def on_import_closed(result: Optional[Any] = None) -> None:
            if result is not None:
                self._mutated = True
                self._refresh_table(preserve_focus=True)
                msg = f"Imported rules merged into settings. Active rules count: {len(self._rules_data)}."
                self.announce(msg)
                if hasattr(self.app, "notify"):
                    self.app.notify(msg)
            else:
                self.announce("Returned to active rule manager.")

        self.app.push_screen(RuleImportModal(self.settings), on_import_closed)

    @on(Button.Pressed, "#btn-export-rules")
    def action_export_rules(self) -> None:
        """Open export rules modal."""
        self.announce("Opening export rules modal.")

        def on_export_closed(result: Optional[Path] = None) -> None:
            self.announce("Returned to active rule manager.")

        self.app.push_screen(ExportRulesModal(self.settings), on_export_closed)

    @on(Button.Pressed, "#btn-browse-templates")
    def action_browse_templates(self) -> None:
        """Open starter rule template gallery modal."""
        self.announce("Opening starter rule template gallery modal.")
        before_snapshot = (
            dict(getattr(self.settings, "KEYWORD_RULES", {}) or {}),
            dict(getattr(self.settings, "DISABLED_KEYWORD_RULES", {}) or {}),
            [dict(p) for p in (getattr(self.settings, "POLICIES", []) or [])],
        )

        def on_gallery_closed(result: Any = None) -> None:
            after_snapshot = (
                dict(getattr(self.settings, "KEYWORD_RULES", {}) or {}),
                dict(getattr(self.settings, "DISABLED_KEYWORD_RULES", {}) or {}),
                [dict(p) for p in (getattr(self.settings, "POLICIES", []) or [])],
            )
            if after_snapshot != before_snapshot:
                self._mutated = True
            self._refresh_table(preserve_focus=True)
            msg = f"Updated rules table from settings. Total rules: {len(self._rules_data)}."
            self.announce(msg)

        self.app.push_screen(
            TemplateGalleryModal(self.settings, defer_rescan=True), on_gallery_closed
        )

    action_browse_rule_templates = action_browse_templates
    action_open_templates = action_browse_templates
    action_open_export_rules = action_export_rules
    action_open_rule_import = action_import_rules
