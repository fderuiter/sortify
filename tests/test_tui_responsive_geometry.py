"""Automated geometry and mouse hit-test suite for responsive TUI scroll containers and sticky footers."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.geometry import Spacing
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    Log,
    Select,
    TabbedContent,
    Tree,
)

from app.config import AppSettings
from app.plugins.clinical_compliance.tui_views import CROForensicModal
from app.ui.catalog import get_catalog_registry
from app.ui.layout import ModalBox
from app.ui.modals.drop_zone import DropPlanConfirmModal
from app.ui.modals.edit import (
    DirectorySelectModal,
    InteractiveCollisionResolverModal,
    ManualPathFallbackModal,
)
from app.ui.modals.reports import (
    AuditReportModal,
    ExtractionPreviewModal,
)
from app.ui.modals.rules import (
    DeleteRuleConfirmModal,
    RuleImportModal,
)
from app.ui.modals.sample_playground import SamplePlaygroundModal
from app.ui.modals.session import (
    HistoryModal,
    RollbackConfirmModal,
    RollbackErrorModal,
)
from app.ui.modals.settings import (
    SettingsModal,
)
from app.ui.modals.wizard import WizardModal
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")


class _ModalHostApp(App[None]):
    """Lightweight host application for mounting modals under controlled viewport sizes."""

    def __init__(self, settings: Any, base_dir: str) -> None:
        super().__init__()
        self.settings = settings
        self.base_dir = base_dir


@pytest.fixture(autouse=True)
def isolated_tui_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate AppSettings and keyring side effects during headless geometry tests."""
    import app.config
    import app.core.session_catalog
    from app.core.plugin_registry import PluginRegistry

    PluginRegistry.reset_instance()
    app.config.AppSettings.clear_observers()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path))
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.fail.Keyring")
    monkeypatch.setattr(app.config, "get_app_dir", lambda: tmp_path)
    monkeypatch.setattr(
        app.config.AppSettings, "_trigger_save", lambda self: self._save()
    )
    monkeypatch.setattr(app.core.session_catalog, "find_abandoned_sessions", lambda: [])
    monkeypatch.setattr(Input, "cursor_blink", False)

    for key in list(os.environ.keys()):
        if key.startswith("AUTOSORTER_") and key != "AUTOSORTER_APP_DIR":
            monkeypatch.delenv(key, raising=False)

    if hasattr(app.config, "settings"):
        monkeypatch.delattr(app.config, "settings", raising=False)


def _is_visible(widget: Any) -> bool:
    """Return True if widget and all its ancestors are displayed and not marked hidden."""
    return all(
        anc.display and "hidden" not in anc.classes
        for anc in widget.ancestors_with_self
    )


def _find_modal_box(modal: Any) -> Any:
    """Return the outer bordered container of a modal screen."""
    for selector in (
        ".modal-frame",
        ".modal-box",
        ".audit-modal-box",
        ".quarantine-modal-box",
        ".dropzone-box",
        ".preview-modal-box",
        ".error-box",
        ".palette-box",
    ):
        matches = modal.query(selector)
        if matches:
            return matches.first()
    raise AssertionError(f"No outer modal box found for {type(modal).__name__}")


def _assert_button_visible_and_hittestable(
    screen: Any,
    btn: Button,
    container_region: Any,
    *,
    check_hit_test: bool = True,
) -> None:
    """Assert button renders its full label inside container_region and passes center-point hit-test."""
    label_text = btn.label.plain
    assert btn.content_region.width >= len(label_text), (
        f"Button #{btn.id} ({label_text!r}) clipped: "
        f"content_width={btn.content_region.width} < {len(label_text)} (region={btn.region})"
    )
    r = btn.region
    assert r.width > 0 and r.height > 0, f"Button #{btn.id} has empty region {r}"
    assert (
        r.x >= container_region.x
        and r.right <= container_region.right
        and r.y >= container_region.y
        and r.bottom <= container_region.bottom
    ), f"Button #{btn.id} region {r} overflows container {container_region}"

    if check_hit_test:
        cx = r.x + r.width // 2
        cy = r.y + r.height // 2
        hit, _ = screen.get_widget_at(cx, cy)
        assert hit is btn or btn in hit.ancestors_with_self, (
            f"Center-point hit-test failed for #{btn.id} at ({cx}, {cy}): hit {hit!r}"
        )


def _assert_datatable_min_rows(table: DataTable, min_rows: int = 3) -> None:
    """Assert DataTable reserves space for its column header plus at least min_rows data rows."""
    header_rows = 1 if table.show_header else 0
    avail_rows = table.content_region.height - header_rows
    assert avail_rows >= min_rows, (
        f"DataTable #{table.id} only shows {avail_rows} rows "
        f"(expected >= {min_rows}, content_region={table.content_region})"
    )


def test_responsive_compact_and_narrow_breakpoints(tmp_path: Path) -> None:
    """Verify compact (width <= 100 or height <= 30) and dual-pane narrow (width < 100) breakpoints."""

    async def _run() -> None:
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            dual_pane = app.query_one("#main-dual-pane")
            left_pane = app.query_one("#left-tree-pane")
            right_pane = app.query_one("#right-meta-pane")

            # 80x24 -> compact active, dual-pane stacked vertically (narrow)
            assert "compact" in app.classes
            assert "narrow" in dual_pane.classes
            assert left_pane.region.y < right_pane.region.y

            # 100x30 -> compact active, but width >= 100 preserves side-by-side workspace!
            await pilot.resize_terminal(100, 30)
            await pilot.pause()
            assert "compact" in app.classes
            assert "narrow" not in dual_pane.classes
            assert left_pane.region.x < right_pane.region.x
            assert left_pane.region.y == right_pane.region.y

            # 90x40 -> compact active via width <= 100, dual-pane stacked vertically
            await pilot.resize_terminal(90, 40)
            await pilot.pause()
            assert "compact" in app.classes
            assert "narrow" in dual_pane.classes

            # 120x28 -> compact active via height <= 30, dual-pane side-by-side
            await pilot.resize_terminal(120, 28)
            await pilot.pause()
            assert "compact" in app.classes
            assert "narrow" not in dual_pane.classes

            # 120x35 -> neither compact nor narrow
            await pilot.resize_terminal(120, 35)
            await pilot.pause()
            assert "compact" not in app.classes
            assert "narrow" not in dual_pane.classes

    asyncio.run(_run())


def test_modal_box_scroll_delegation() -> None:
    """Verify ModalBox delegates scroll_to_widget and scroll_home to inner .modal-body."""

    class _ScrollProbeApp(App[None]):
        CSS = """
        .modal-box { height: 8; overflow-y: hidden; border: solid green; }
        .modal-body { height: 100%; overflow-y: auto; }
        .button-row { dock: bottom; height: 1; }
        """

        def compose(self) -> ComposeResult:
            with ModalBox(classes="modal-box", id="probe-box"):
                with Vertical(classes="modal-body", id="probe-body"):
                    for idx in range(15):
                        yield Label(f"Item line {idx}", id=f"lbl-{idx}")
                with Horizontal(classes="button-row"):
                    yield Button("OK", id="btn-ok")

    async def _run() -> None:
        app = _ScrollProbeApp()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            box = app.query_one("#probe-box", ModalBox)
            body = app.query_one("#probe-body", Vertical)
            target = app.query_one("#lbl-14", Label)

            box.scroll_to_widget(target, animate=False)
            assert body.scroll_y > 0
            await pilot.pause()
            assert body.scroll_y > 0

            box.scroll_home(animate=False)
            assert body.scroll_y == 0
            await pilot.pause()
            assert body.scroll_y == 0

    asyncio.run(_run())


@pytest.mark.parametrize("viewport", [(80, 24), (100, 30)])
def test_main_screen_onboarding_and_sticky_execute_geometry(
    viewport: tuple[int, int], tmp_path: Path
) -> None:
    """Verify onboarding CTA buttons and sticky Execute button stay visible and hit-testable."""

    async def _run() -> None:
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings)

        async with app.run_test(size=viewport) as pilot:
            await pilot.pause()
            right_pane = app.query_one("#right-meta-pane")
            meta_scroll = app.query_one("#right-meta-scroll")
            btn_select = app.query_one("#btn-cta-select-dir", Button)
            btn_scan = app.query_one("#btn-cta-scan-dir", Button)
            btn_sample = app.query_one("#btn-cta-sample-data", Button)
            btn_exec = app.query_one("#btn-execute", Button)

            assert btn_sample.label.plain == "Load Sample Dataset [G]"
            assert btn_exec.label.plain == "Execute Plan [Ctrl+E]"

            for btn in (btn_select, btn_scan, btn_sample, btn_exec):
                _assert_button_visible_and_hittestable(
                    app.screen, btn, right_pane.region
                )
                _assert_button_visible_and_hittestable(
                    app.screen, btn, app.screen.region
                )

            exec_region_before = btn_exec.region

            # Flood log widget inside #right-meta-scroll and scroll down; #btn-execute must stay pinned
            log_widget = app.query_one("#tui-log", Log)
            for idx in range(30):
                log_widget.write_line(
                    f"Log message {idx}: verifying sticky footer behavior"
                )
            await pilot.pause()

            meta_scroll.scroll_end(animate=False)
            await pilot.pause()

            assert btn_exec.region == exec_region_before
            _assert_button_visible_and_hittestable(
                app.screen, btn_exec, right_pane.region
            )

    asyncio.run(_run())


def test_main_screen_alert_banner_scroll_cap_at_80x24(tmp_path: Path) -> None:
    """Verify multi-line #alert-banner caps height and scrolls at 80x24 while preserving >= 4 tree rows."""

    async def _run() -> None:
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.plan = {
                "Finance": {
                    f"invoice_{i}.pdf": {
                        "__type__": "file",
                        "filepath": f"{tmp_path}/invoice_{i}.pdf",
                        "target_filename": "duplicate.pdf",
                        "confidence": 0.91,
                        "category": "Finance",
                    }
                    for i in range(6)
                }
            }
            app.rebuild_tree()
            validation_result = {
                "success": False,
                "has_critical_errors": True,
                "collisions": [
                    {
                        "type": "duplicate_destination",
                        "target": f"{tmp_path}/Finance/duplicate_{i}.pdf",
                        "sources": [
                            f"{tmp_path}/src_a_{i}.pdf",
                            f"{tmp_path}/src_b_{i}.pdf",
                        ],
                        "message": f"Multiple files collide at destination Finance/duplicate_{i}.pdf",
                    }
                    for i in range(6)
                ],
                "circular_renames": [],
                "broken_links": [],
                "unreadable_files": [],
            }
            app._update_validation_state(validation_result)
            await pilot.pause()

            banner = app.query_one("#alert-banner")
            tree = app.query_one("#plan-tree", Tree)
            btn_exec = app.query_one("#btn-execute", Button)

            assert banner.display is True
            assert banner.region.height <= 3
            assert banner.max_scroll_y > 0
            assert tree.content_region.height >= 4
            _assert_button_visible_and_hittestable(
                app.screen, btn_exec, app.screen.region
            )

    asyncio.run(_run())


@pytest.mark.parametrize("viewport", [(80, 24), (100, 30)])
def test_rule_import_modal_geometry_and_sticky_footer(
    viewport: tuple[int, int], tmp_path: Path
) -> None:
    """Verify RuleImportModal Parse File button, >= 3 preview rows, and sticky footer hit-tests."""

    async def _run() -> None:
        import json

        settings = AppSettings()
        settings._settings_model.KEYWORD_RULES = {"invoice": "OldFinance"}
        rules_json = tmp_path / "import_rules.json"
        rules_json.write_text(
            json.dumps(
                {
                    "source": "Sortify",
                    "keyword_rules": {
                        "invoice": "Finance/Invoices",
                        "receipt": "Finance/Receipts",
                        "contract": "Legal/Contracts",
                        "nda": "Legal/NDA",
                    },
                    "policies": [
                        {
                            "type": "keyword",
                            "expression": "tax",
                            "target_path": "Finance/Tax",
                            "priority": 80,
                        },
                        {
                            "type": "keyword",
                            "expression": "audit",
                            "target_path": "Compliance/Audit",
                            "priority": 85,
                        },
                        {
                            "type": "keyword",
                            "expression": "payroll",
                            "target_path": "HR/Payroll",
                            "priority": 75,
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        host = _ModalHostApp(settings=settings, base_dir=str(tmp_path))
        async with host.run_test(size=viewport) as pilot:
            modal = RuleImportModal(settings=settings)
            host.push_screen(modal)
            await pilot.pause()

            box = _find_modal_box(modal)
            inp_path = modal.query_one("#input-rule-file-path", Input)
            btn_parse = modal.query_one("#btn-parse-rule-file", Button)

            # Parse File button sits horizontally beside input with full label
            assert btn_parse.label.plain == "Parse File"
            assert inp_path.region.y == btn_parse.region.y
            assert btn_parse.region.x >= inp_path.region.right
            _assert_button_visible_and_hittestable(modal, btn_parse, box.region)

            # Trigger real rule parsing to populate all 3 preview DataTables
            inp_path.value = str(rules_json)
            modal.action_parse_file()
            await pilot.pause()

            for dt_id in (
                "#dt-imported-keywords",
                "#dt-imported-policies",
                "#dt-rule-conflicts",
            ):
                dt = modal.query_one(dt_id, DataTable)
                _assert_datatable_min_rows(dt, min_rows=3)

            btn_close = modal.query_one("#btn-close-importer", Button)
            btn_merge = modal.query_one("#btn-merge-imported-rules", Button)
            _assert_button_visible_and_hittestable(modal, btn_close, box.region)
            _assert_button_visible_and_hittestable(modal, btn_merge, box.region)

            # Clicking Cancel/Close button via mouse center dismisses without scrolling
            await pilot.click(
                offset=(
                    btn_close.region.x + btn_close.region.width // 2,
                    btn_close.region.y + btn_close.region.height // 2,
                )
            )
            await pilot.pause()
            assert host.screen is not modal

    asyncio.run(_run())


@pytest.mark.parametrize("viewport", [(80, 24), (100, 30)])
def test_rollback_and_audit_report_modals_geometry(
    viewport: tuple[int, int], tmp_path: Path
) -> None:
    """Verify RollbackConfirmModal (Stage 1 & 2) and AuditReportModal summary cards, tables, and footers."""

    async def _run() -> None:
        settings = AppSettings()
        session_info = {
            "session_id": "sess-geo-01",
            "items": [
                {
                    "step_id": i,
                    "source_path": f"{tmp_path}/src_{i}.pdf",
                    "target_path": f"{tmp_path}/dst_{i}.pdf",
                    "operation_type": "move",
                    "status": "completed",
                }
                for i in range(1, 8)
            ],
        }

        host = _ModalHostApp(settings=settings, base_dir=str(tmp_path))
        async with host.run_test(size=viewport) as pilot:
            # 1. RollbackConfirmModal Stage 1 & Stage 2
            rb_modal = RollbackConfirmModal(session_info)
            await host.push_screen(rb_modal)
            await pilot.pause()

            rb_box = _find_modal_box(rb_modal)
            lbl_sel = rb_modal.query_one("#lbl-selection-count", Label)
            assert (
                lbl_sel.region.x >= rb_box.region.x
                and lbl_sel.region.right <= rb_box.region.right
            )

            _assert_datatable_min_rows(
                rb_modal.query_one("#rollback-items-table", DataTable), min_rows=3
            )
            for btn_id in (
                "#btn-cancel-rollback",
                "#btn-preview-dryrun",
                "#btn-confirm-rollback",
            ):
                _assert_button_visible_and_hittestable(
                    rb_modal, rb_modal.query_one(btn_id, Button), rb_box.region
                )

            # Transition to Stage 2
            rb_modal.action_preview_dryrun()
            await pilot.pause()
            for stat_id in (
                "#lbl-summary-total",
                "#lbl-summary-clean",
                "#lbl-summary-collisions",
                "#lbl-summary-missing",
            ):
                stat_lbl = rb_modal.query_one(stat_id, Label)
                assert stat_lbl.region.width > 0
                assert (
                    stat_lbl.region.x >= rb_box.region.x
                    and stat_lbl.region.right <= rb_box.region.right
                )
            _assert_datatable_min_rows(
                rb_modal.query_one("#dryrun-diff-table", DataTable), min_rows=3
            )
            for btn_id in (
                "#btn-back-to-selection",
                "#btn-cancel-rollback-2",
                "#btn-execute-rollback",
            ):
                _assert_button_visible_and_hittestable(
                    rb_modal, rb_modal.query_one(btn_id, Button), rb_box.region
                )

            btn_cancel_rb2 = rb_modal.query_one("#btn-cancel-rollback-2", Button)
            await pilot.click(
                offset=(
                    btn_cancel_rb2.region.x + btn_cancel_rb2.region.width // 2,
                    btn_cancel_rb2.region.y + btn_cancel_rb2.region.height // 2,
                )
            )
            await pilot.pause()
            assert host.screen is not rb_modal

            # 2. AuditReportModal across both tabs
            report_data = {
                "base_dir": str(tmp_path),
                "total_files": 14,
                "verified_pass_count": 11,
                "mismatch_fail_count": 2,
                "skipped_count": 1,
                "unverified_count": 0,
                "records": [
                    {
                        "filename": f"doc_{i}.pdf",
                        "source_path": f"{tmp_path}/src/doc_{i}.pdf",
                        "target_path": f"{tmp_path}/dst/doc_{i}.pdf",
                        "status": "VERIFIED_PASS" if i < 5 else "MISMATCH_FAIL",
                    }
                    for i in range(7)
                ],
            }
            audit_modal = AuditReportModal(report_data=report_data)
            await host.push_screen(audit_modal)
            await pilot.pause()

            audit_box = _find_modal_box(audit_modal)
            for card in audit_modal.query(".stat-card"):
                assert card.region.width > 0
                assert (
                    card.region.x >= audit_box.region.x
                    and card.region.right <= audit_box.region.right
                )

            _assert_datatable_min_rows(
                audit_modal.query_one("#audit-table", DataTable), min_rows=3
            )

            tabs = audit_modal.query_one("#audit-tabs", TabbedContent)
            tabs.query_one("Tabs").focus()
            await pilot.press("right")
            await pilot.pause(0.2)
            _assert_datatable_min_rows(
                audit_modal.query_one("#failed-items-table", DataTable), min_rows=3
            )

            for btn_id in (
                "#btn-retry-selected",
                "#btn-retry-all",
                "#btn-export-json",
                "#btn-export-csv",
                "#btn-close",
            ):
                _assert_button_visible_and_hittestable(
                    audit_modal, audit_modal.query_one(btn_id, Button), audit_box.region
                )

            btn_close_audit = audit_modal.query_one("#btn-close", Button)
            await pilot.click(
                offset=(
                    btn_close_audit.region.x + btn_close_audit.region.width // 2,
                    btn_close_audit.region.y + btn_close_audit.region.height // 2,
                )
            )
            await pilot.pause()
            assert host.screen is not audit_modal

    asyncio.run(_run())


@pytest.mark.parametrize("viewport", [(80, 24), (58, 24)])
def test_all_modals_button_labels_tables_and_sticky_footers(
    viewport: tuple[int, int], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify all 32 catalog modals at 80x24 and 58x24 for compact padding, button hit-visibility, narrow stacking, and ModalBox._body_scroll_target()."""
    from app.core.extractor_strategies import PdfExtractor

    monkeypatch.setattr(
        PdfExtractor,
        "preview_pdf_density",
        lambda self, path, density_threshold=50: [
            {"page_index": i, "char_count": 120 * i, "has_vector": i % 2 == 0}
            for i in range(1, 6)
        ],
    )

    async def _run() -> None:
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        sample_pdf = tmp_path / "sample.pdf"
        sample_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")

        catalog_modals = [
            entry for entry in get_catalog_registry() if entry["category"] == "modal"
        ]
        assert len(catalog_modals) == 32

        rich_overrides: dict[str, Any] = {
            "extraction_preview_modal": lambda: ExtractionPreviewModal(
                str(sample_pdf), settings=settings
            ),
            "rollback_confirm_modal": lambda: RollbackConfirmModal(
                {
                    "session_id": "sess-geo-01",
                    "items": [
                        {
                            "step_id": i,
                            "source_path": f"{tmp_path}/src_{i}.pdf",
                            "target_path": f"{tmp_path}/dst_{i}.pdf",
                            "operation_type": "move",
                            "status": "completed",
                        }
                        for i in range(1, 6)
                    ],
                }
            ),
            "rollback_error_modal": lambda: RollbackErrorModal(
                "Rollback Failed", "Error detail line\n" * 16
            ),
            "history_modal": lambda: HistoryModal(
                sessions=[
                    {
                        "session_id": f"h-{i}",
                        "created_at": "2026-01-01",
                        "base_dir": str(tmp_path),
                        "total_files": 5,
                        "status": "completed",
                    }
                    for i in range(5)
                ],
                base_dir=str(tmp_path),
            ),
            "audit_report_modal": lambda: AuditReportModal(
                report_data={
                    "base_dir": str(tmp_path),
                    "total_files": 7,
                    "verified_pass_count": 5,
                    "mismatch_fail_count": 2,
                    "skipped_count": 0,
                    "unverified_count": 0,
                    "records": [
                        {
                            "filename": f"doc_{i}.pdf",
                            "source_path": f"{tmp_path}/src/doc_{i}.pdf",
                            "target_path": f"{tmp_path}/dst/doc_{i}.pdf",
                            "status": "VERIFIED_PASS" if i < 5 else "MISMATCH_FAIL",
                        }
                        for i in range(7)
                    ],
                }
            ),
            "drop_plan_confirm_modal": lambda: DropPlanConfirmModal(
                "Move 6 files", [f"doc_{i}.pdf -> Finance/" for i in range(14)]
            ),
            "directory_select_modal": lambda: DirectorySelectModal(
                current_dir=str(tmp_path)
            ),
            "manual_path_fallback_modal": lambda: ManualPathFallbackModal(
                current_dir=str(tmp_path), reason="Native dialog unavailable"
            ),
            "wizard_modal": lambda: WizardModal(
                settings=settings, current_dir=str(tmp_path)
            ),
            "sample_playground_modal": lambda: SamplePlaygroundModal(
                settings=settings, target_dir=str(tmp_path)
            ),
            "cro_forensic_modal": lambda: CROForensicModal(
                settings=settings, base_dir=str(tmp_path)
            ),
        }

        modal_factories: list[tuple[str, Any]] = [
            (
                entry["id"],
                rich_overrides.get(
                    entry["id"],
                    lambda e=entry: e["factory"](settings=settings),
                ),
            )
            for entry in catalog_modals
        ]
        modal_factories.append(
            ("delete_rule_confirm_modal", lambda: DeleteRuleConfirmModal("invoice.*"))
        )

        for modal_id, factory in modal_factories:
            host = _ModalHostApp(settings=settings, base_dir=str(tmp_path))
            async with host.run_test(size=viewport) as pilot:
                modal = factory()
                name = type(modal).__name__
                await host.push_screen(modal)
                await pilot.pause()

                if name in ("DropZoneModal", "CROForensicModal"):
                    help_btns = modal.query("#btn-help")
                    if help_btns:
                        help_btns.first().remove_class("hidden")
                        await pilot.pause()

                box = _find_modal_box(modal)
                assert box.styles.padding == Spacing(0, 1, 0, 1), (
                    f"{viewport} {name} ({modal_id}) expected compact padding Spacing(0, 1, 0, 1), "
                    f"got {box.styles.padding}"
                )

                bodies = list(modal.query(".modal-body"))
                if bodies:
                    assert isinstance(box, ModalBox), (
                        f"{viewport} {name} expected ModalBox outer container, got {box!r}"
                    )
                    assert box.max_scroll_y == 0, (
                        f"{viewport} {name} ModalBox overflowed with max_scroll_y={box.max_scroll_y}"
                    )
                    scroll_target = box._body_scroll_target()
                    assert scroll_target is not None, (
                        f"{viewport} {name} ModalBox._body_scroll_target() returned None"
                    )
                else:
                    assert modal_id == "command_palette_modal"

                # DuplicateReviewModal & InteractiveCollisionResolverModal: dropdown must stay above pinned footer
                if modal_id in ("duplicate_review_modal", "collision_resolver_modal"):
                    sel_id = (
                        "#select-action-dup_1"
                        if modal_id == "duplicate_review_modal"
                        else "#select-action-group_1"
                    )
                    action_sel = modal.query_one(sel_id, Select)
                    button_row = modal.query_one(".button-row")
                    assert action_sel.region.bottom <= button_row.region.y, (
                        f"{viewport} {name} {sel_id} ({action_sel.region}) overlaps .button-row ({button_row.region})"
                    )

                # First check pinned footer buttons (and all DropZoneModal buttons) WITHOUT scrolling
                for btn in modal.query(Button):
                    if not _is_visible(btn):
                        continue
                    in_pinned_footer = any(
                        "button-row" in anc.classes
                        or "modal-footer" in anc.classes
                        or "quarantine-button-row" in anc.classes
                        for anc in btn.ancestors_with_self
                    ) and not any("modal-body" in anc.classes for anc in btn.ancestors)
                    if in_pinned_footer or name == "DropZoneModal":
                        _assert_button_visible_and_hittestable(modal, btn, box.region)

                # Next check EVERY visible button across the modal (scrolling scrollable .modal-body controls into view)
                for btn in modal.query(Button):
                    if not _is_visible(btn):
                        continue
                    in_body = any("modal-body" in anc.classes for anc in btn.ancestors)
                    if (
                        in_body
                        and name != "DropZoneModal"
                        and isinstance(box, ModalBox)
                    ):
                        box.scroll_to_widget(btn, animate=False)
                        await pilot.pause()
                    _assert_button_visible_and_hittestable(modal, btn, box.region)

                if bodies and isinstance(box, ModalBox):
                    box.scroll_home(animate=False)
                    await pilot.pause()
                    target_after = box._body_scroll_target()
                    assert target_after is not None and target_after.scroll_y == 0

                # Narrow (58x24) vertical split-pane stacking and 3->2 column button grid reflow
                if viewport == (58, 24):
                    if name == "DropZoneModal":
                        dt_stage = modal.query_one("#dropzone-staging-table", DataTable)
                        meta_prev = modal.query_one("#dropzone-metadata-preview")
                        assert dt_stage.region.y < meta_prev.region.y
                        q_btns = [
                            b
                            for b in modal.query(".queue-row Button")
                            if _is_visible(b)
                        ]
                        assert len(q_btns) == 5
                        assert q_btns[0].region.x < q_btns[1].region.x
                        assert q_btns[2].region.y > q_btns[0].region.y
                    elif name == "QuarantineVaultModal":
                        t_pane = modal.query_one("#quarantine-table-pane")
                        d_pane = modal.query_one("#quarantine-detail-pane")
                        assert t_pane.region.y < d_pane.region.y
                        q_cells = [
                            w
                            for w in modal.query_one(".quarantine-button-row").children
                            if _is_visible(w)
                        ]
                        assert len(q_cells) == 6
                        assert q_cells[0].region.x < q_cells[1].region.x
                        assert q_cells[2].region.y > q_cells[0].region.y
                    elif name == "SamplePlaygroundModal":
                        l_pane = modal.query_one(".left-pane")
                        r_pane = modal.query_one(".right-pane")
                        assert l_pane.region.y < r_pane.region.y
                    elif name in ("SettingsModal", "ModelManagerModal"):
                        f_btns = [
                            b
                            for b in modal.query(".button-row Button")
                            if _is_visible(b)
                        ]
                        assert len(f_btns) >= 3
                        assert f_btns[0].region.x < f_btns[1].region.x
                        assert f_btns[2].region.y > f_btns[0].region.y

                for dt in modal.query(DataTable):
                    if not _is_visible(dt):
                        continue
                    _assert_datatable_min_rows(dt, min_rows=3)

    asyncio.run(_run())


def test_settings_modal_tab_scroll_home_forwarding(tmp_path: Path) -> None:
    """Verify SettingsModal keeps ModalBox.max_scroll_y == 0 and forwards scroll_home across TabPanes at 80x24 and 58x24."""

    async def _run() -> None:
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        for viewport in ((80, 24), (58, 24)):
            host = _ModalHostApp(settings=settings, base_dir=str(tmp_path))
            async with host.run_test(size=viewport) as pilot:
                modal = SettingsModal(settings=settings)
                await host.push_screen(modal)
                await pilot.pause()

                box = modal.query_one(".modal-box", ModalBox)
                assert box.max_scroll_y == 0
                active_pane = box._body_scroll_target()
                assert active_pane is not None
                assert active_pane.id == "tab-general"

                # Scroll down the active TabPane and verify ModalBox.scroll_home resets it to 0
                active_pane.scroll_end(animate=False)
                await pilot.pause()
                assert active_pane.max_scroll_y > 0
                assert active_pane.scroll_y > 0
                assert box.max_scroll_y == 0

                box.scroll_home(animate=False)
                await pilot.pause()
                assert active_pane.scroll_y == 0

    asyncio.run(_run())


def test_cro_forensic_and_migrated_modals_resize_breakpoints(tmp_path: Path) -> None:
    """Verify CROForensicModal preserves compact on 120x28 resize and migrated modals apply breakpoints."""

    async def _run() -> None:
        settings = AppSettings()
        host = _ModalHostApp(settings=settings, base_dir=str(tmp_path))
        async with host.run_test(size=(120, 40)) as pilot:
            cro_modal = CROForensicModal(settings=settings, base_dir=str(tmp_path))
            await host.push_screen(cro_modal)
            await pilot.pause()
            assert "compact" not in cro_modal.classes
            assert "narrow" not in cro_modal.classes

            # Height-only resize to 120x28 must set compact without self.size.height overwriting it
            await pilot.resize_terminal(120, 28)
            await pilot.pause()
            assert "compact" in cro_modal.classes
            assert "narrow" not in cro_modal.classes

            await pilot.resize_terminal(76, 24)
            await pilot.pause()
            assert "compact" in cro_modal.classes
            assert "narrow" in cro_modal.classes
            host.pop_screen()
            await pilot.pause()

            # DropPlanConfirmModal and RollbackErrorModal gain ResponsiveModalScreen classes
            for modal in (
                DropPlanConfirmModal("Move 2 files", ["a.pdf -> Finance/"]),
                RollbackErrorModal("Rollback Failed", "Details"),
            ):
                await host.push_screen(modal)
                await pilot.pause()
                assert "compact" in modal.classes
                assert "narrow" in modal.classes
                host.pop_screen()
                await pilot.pause()

            # InteractiveCollisionResolverModal uses NARROW_WIDTH_BREAKPOINT = 60
            await pilot.resize_terminal(70, 40)
            await pilot.pause()
            collision_modal = InteractiveCollisionResolverModal(duplicate_groups=[])
            await host.push_screen(collision_modal)
            await pilot.pause()
            assert "narrow" not in collision_modal.classes
            assert "compact" in collision_modal.classes

            await pilot.resize_terminal(58, 40)
            await pilot.pause()
            assert "narrow" in collision_modal.classes

    asyncio.run(_run())


def test_modal_box_scroll_helpers_update_regions_immediately_when_screen_dirty(
    tmp_path: Path,
) -> None:
    """Verify ModalBox.scroll_to_widget(..., animate=False) and scroll_home(animate=False) update regions synchronously even when Screen has dirty widgets."""

    async def _run() -> None:
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        for modal_factory, target_btn_id, switch_tab in (
            (
                lambda: WizardModal(settings=settings, current_dir=str(tmp_path)),
                "#preset-demo",
                None,
            ),
            (
                lambda: SettingsModal(settings=settings),
                "#btn-open-rules",
                "tab-ai-privacy",
            ),
        ):
            host = _ModalHostApp(settings=settings, base_dir=str(tmp_path))
            async with host.run_test(size=(58, 24)) as pilot:
                modal = modal_factory()
                await host.push_screen(modal)
                await pilot.pause()

                if switch_tab is not None:
                    modal.query_one("Tabs").focus()
                    await pilot.press("right")
                    await pilot.pause(0.2)

                box = _find_modal_box(modal)
                assert isinstance(box, ModalBox)
                btn = modal.query_one(target_btn_id, Button)
                modal._dirty_widgets.add(modal)
                modal._repaint_required = True

                # Must update compositor regions synchronously with zero pilot.pause()
                box.scroll_to_widget(btn, animate=False)
                _assert_button_visible_and_hittestable(modal, btn, box.region)

                modal._dirty_widgets.add(modal)
                modal._repaint_required = True
                box.scroll_home(animate=False)
                body_target = box._body_scroll_target()
                assert body_target is not None and body_target.scroll_y == 0

    asyncio.run(_run())
