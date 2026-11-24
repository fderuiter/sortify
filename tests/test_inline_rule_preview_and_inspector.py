"""Tests for inline rule preview, policy editing, masking warnings, auto-rescan, routing badges/inspector, and multi-format extraction preview."""

import io
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas
from textual.widgets import Checkbox, DataTable, Input, Label, Select, Static, Tree

from app.config import AppSettings, Settings
from app.core.analyzer import IncrementalAnalyzer
from app.core.analyzer_strategies import RecursiveKMeansStrategy
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.ui.modals.reports import ExtractionPreviewModal
from app.ui.modals.rules import ActiveRuleManagerModal, RuleEditModal
from app.ui.tui import AutoSorterTUI


@pytest.fixture(autouse=True)
def _no_abandoned_sessions(monkeypatch):
    """Prevent session recovery modals from interrupting TUI tests."""
    import app.core.session_catalog

    monkeypatch.setattr(
        app.core.session_catalog, "find_abandoned_sessions", lambda *a, **k: []
    )


def _make_sample_pdf(path: Path, text: str) -> str:
    """Create a 1-page PDF containing embedded text."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(50, 720, text)
    c.showPage()
    c.save()
    path.write_bytes(buf.getvalue())
    return str(path)


@pytest.mark.anyio
async def test_rule_editor_creates_and_edits_full_policies_and_validates_actions(
    tmp_path: Path,
):
    """Req 1 & AC 1: RuleEditModal saves keyword rules and full policies and rejects invalid paths or actions."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.KEYWORD_RULES = {}
    settings.POLICIES = []

    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
    modal = ActiveRuleManagerModal(settings)

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        # Open Add Rule modal
        modal.action_add_rule()
        await pilot.pause()
        assert isinstance(app.screen, RuleEditModal)
        editor = app.screen

        # Verify accessibility compliance on RuleEditModal
        audit = editor.audit_a11y_compliance()
        assert audit["violations"] == []

        # Switch category to Policy Rule
        editor.query_one("#select-rule-kind", Select).value = "policy"
        await pilot.pause()

        editor.query_one("#select-policy-type", Select).value = "pattern"
        editor.query_one("#input-rule-pattern", Input).value = r"^INV-\d+"
        editor.query_one("#input-rule-target", Input).value = "Finance/Invoices"
        editor.query_one("#input-policy-priority", Input).value = "25"
        editor.query_one("#chk-policy-halting", Checkbox).value = False

        # 1. Unsupported policy action should be rejected
        editor.query_one("#input-policy-action", Input).value = "vaporize_forever"
        editor.action_save()
        await pilot.pause()
        err_lbl = editor.query_one("#lbl-rule-error", Label)
        assert "unsupported policy action" in str(err_lbl.render()).lower()
        assert isinstance(app.screen, RuleEditModal)

        # 2. Invalid traversal path should be rejected
        editor.query_one("#input-policy-action", Input).value = "archive"
        editor.query_one("#input-rule-target", Input).value = "../EscapeWorkspace"
        editor.action_save()
        await pilot.pause()
        assert "traversal" in str(err_lbl.render()).lower()
        assert isinstance(app.screen, RuleEditModal)

        # 3. Non-integer priority should be rejected
        editor.query_one("#input-rule-target", Input).value = "Finance/Invoices"
        editor.query_one("#input-policy-priority", Input).value = "not-an-int"
        editor.action_save()
        await pilot.pause()
        assert "integer" in str(err_lbl.render()).lower()
        assert isinstance(app.screen, RuleEditModal)

        # 4. Valid full policy saves all attributes
        editor.query_one("#input-policy-priority", Input).value = "25"
        editor.action_save()
        await pilot.pause()
        assert isinstance(app.screen, ActiveRuleManagerModal)
        assert len(settings.POLICIES) == 1
        saved_pol = settings.POLICIES[0]
        assert saved_pol["type"] == "pattern"
        assert saved_pol["expression"] == r"^INV-\d+"
        assert saved_pol["target_path"] == "Finance/Invoices"
        assert saved_pol["priority"] == 25
        assert saved_pol["halting"] is False
        assert saved_pol["action"] == "archive"
        assert saved_pol["enabled"] is True

        # 5. Edit existing policy and update priority, action, and halting
        table = modal.query_one("#rules-table", DataTable)
        table.move_cursor(row=0)
        modal.action_edit_rule()
        await pilot.pause()
        assert isinstance(app.screen, RuleEditModal)
        edit_screen = app.screen
        assert edit_screen.query_one("#select-rule-kind", Select).value == "policy"
        assert edit_screen.query_one("#select-policy-type", Select).value == "pattern"
        assert edit_screen.query_one("#input-policy-priority", Input).value == "25"
        assert edit_screen.query_one("#input-policy-action", Input).value == "archive"
        assert edit_screen.query_one("#chk-policy-halting", Checkbox).value is False

        edit_screen.query_one("#input-policy-priority", Input).value = "40"
        edit_screen.query_one("#input-policy-action", Input).value = "quarantine"
        edit_screen.query_one("#chk-policy-halting", Checkbox).value = True
        edit_screen.action_save()
        await pilot.pause()

        updated_pol = settings.POLICIES[0]
        assert updated_pol["priority"] == 40
        assert updated_pol["action"] == "quarantine"
        assert updated_pol["halting"] is True


@pytest.mark.anyio
async def test_rule_editor_live_sample_file_preview_and_workspace_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Req 2, Constraint 1, & AC 2: Selecting a sample file updates match status and folder counts live without network or file moves."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    inv1 = ws / "invoice_march.txt"
    inv2 = ws / "vendor_bill.txt"
    notes = ws / "meeting_notes.txt"
    inv1.write_text(
        "Quarterly consulting invoice #1042 due in 30 days.", encoding="utf-8"
    )
    inv2.write_text("Please remit payment for invoice #1043.", encoding="utf-8")
    notes.write_text(
        "Team roadmap discussion and sprint retrospective.", encoding="utf-8"
    )

    # Guard socket connect to guarantee zero external network calls
    import socket

    orig_connect = socket.socket.connect

    def _forbid_external_connect(self_sock, address):
        if isinstance(address, tuple) and address[0] not in (
            "127.0.0.1",
            "::1",
            "localhost",
        ):
            raise AssertionError(f"Unexpected outbound network call to {address}")
        return orig_connect(self_sock, address)

    monkeypatch.setattr(socket.socket, "connect", _forbid_external_connect)

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    app = AutoSorterTUI(settings=settings, base_dir=str(ws))

    async with app.run_test(size=(100, 32)) as pilot:
        await pilot.pause()
        editor = RuleEditModal(
            title="Add Rule",
            settings=settings,
            base_dir=str(ws),
        )
        app.push_screen(editor)
        await pilot.pause()

        sel_sample = editor.query_one("#select-sample-file", Select)
        lbl_preview = editor.query_one("#lbl-rule-preview", Label)
        inp_pattern = editor.query_one("#input-rule-pattern", Input)

        # Select invoice_march.txt as sample file
        sel_sample.value = "invoice_march.txt"
        await pilot.pause()

        # Type 'invoice' -> matches both invoice_march.txt (filename+body) and vendor_bill.txt (body) = 2 of 3 files
        inp_pattern.value = "invoice"
        await pilot.pause()
        preview_text = str(lbl_preview.render())
        assert "MATCH" in preview_text
        assert "NO MATCH" not in preview_text
        assert "2 of 3" in preview_text
        assert editor.last_announcement is not None
        assert "2 of 3" in editor.last_announcement

        # Switch sample file to meeting_notes.txt -> NO MATCH on sample, still 2 of 3 workspace matches
        sel_sample.value = "meeting_notes.txt"
        await pilot.pause()
        preview_text = str(lbl_preview.render())
        assert "NO MATCH" in preview_text
        assert "2 of 3" in preview_text

        # Switch to policy pattern (filename glob) matching only meeting_notes.txt -> MATCH, 1 of 3 workspace matches
        editor.query_one("#select-rule-kind", Select).value = "policy"
        editor.query_one("#select-policy-type", Select).value = "pattern"
        inp_pattern.value = "meeting_*.txt"
        await pilot.pause()
        preview_text = str(lbl_preview.render())
        assert "✓ MATCH" in preview_text
        assert "1 of 3" in preview_text

        # Switch to compliance policy matching document body phrase -> MATCH, 1 of 3 workspace matches
        editor.query_one("#select-policy-type", Select).value = "compliance"
        inp_pattern.value = "sprint retrospective"
        await pilot.pause()
        preview_text = str(lbl_preview.render())
        assert "✓ MATCH" in preview_text
        assert "1 of 3" in preview_text

        # Verify zero files moved on disk
        assert inv1.exists()
        assert inv2.exists()
        assert notes.exists()


@pytest.mark.anyio
async def test_active_rule_manager_flags_shadowed_lower_priority_policies(
    tmp_path: Path,
):
    """Req 3 & AC 3: ActiveRuleManagerModal marks shadowed lower-priority policies with visible masked status."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.KEYWORD_RULES = {}
    settings.POLICIES = [
        {
            "type": "keyword",
            "expression": "inv",
            "target_path": "Finance/General",
            "priority": 50,
            "halting": False,
            "enabled": True,
        },
        {
            "type": "keyword",
            "expression": "invoice_2026",
            "target_path": "Finance/2026",
            "priority": 10,
            "halting": False,
            "enabled": True,
        },
        {
            "type": "pattern",
            "expression": ".*",
            "target_path": "CatchAll",
            "priority": 100,
            "halting": True,
            "enabled": False,  # Disabled rule must NOT mask active rules!
        },
    ]

    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
    modal = ActiveRuleManagerModal(settings)

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        app.push_screen(modal)
        await pilot.pause()

        by_pattern = {r["pattern"]: r for r in modal._rules_data}
        assert by_pattern["inv"]["status"] == "Active"
        assert by_pattern[".*"]["status"] == "Disabled"

        # 'invoice_2026' contains 'inv' and has lower priority (10 < 50), so 'inv' masks it
        masked_row = by_pattern["invoice_2026"]
        assert "Masked" in masked_row["status"]
        assert "inv" in masked_row["status"]
        assert masked_row["status"] != "Active"

        # Screen reader announcement includes masking warning
        joined_ann = " ".join(a["message"] for a in modal.announcements)
        assert "masked" in joined_ann.lower()


@pytest.mark.anyio
async def test_rule_manager_close_triggers_rescan_only_after_mutations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Req 4 & AC 4: Closing rule manager triggers directory scan after mutations, while read-only visits skip rescans."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "doc.txt").write_text("invoice payment", encoding="utf-8")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings._settings_model.AI_CONSENT_GRANTED = True
    settings.KEYWORD_RULES = {"invoice": "Invoices"}

    app = AutoSorterTUI(settings=settings, base_dir=str(ws))
    scan_calls: list[str] = []

    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        # Replace action_scan_directory after initial mount so we count post-modal rescans
        monkeypatch.setattr(
            app, "action_scan_directory", lambda: scan_calls.append("scanned")
        )

        # 1. Read-only visit: open and close without modifying rules -> NO rescan
        app.action_open_active_rules()
        await pilot.pause()
        assert isinstance(app.screen, ActiveRuleManagerModal)
        app.screen.action_close()
        await pilot.pause()
        assert scan_calls == []

        # 2. Mutating visit (toggle rule): open, toggle, close -> triggers rescan
        app.action_open_active_rules()
        await pilot.pause()
        assert isinstance(app.screen, ActiveRuleManagerModal)
        app.screen.action_toggle_rule()
        await pilot.pause()
        app.screen.action_close()
        await pilot.pause()
        assert len(scan_calls) == 1


@pytest.mark.anyio
async def test_plan_tree_routing_badges_and_inspector_details(tmp_path: Path):
    """Req 5, Req 6, Constraint 5, & AC 5: Plan tree displays routing badges and inspector shows matched expression, priority, or cluster keywords."""
    # 1. Verify RecursiveKMeansStrategy populates top cluster keywords in leaf match metadata
    strategy = RecursiveKMeansStrategy()
    cluster_plan, _err = strategy._generate_plan_inline(
        filenames=["q1.txt", "q2.txt"],
        documents=[
            "quarterly revenue forecast budget spreadsheet",
            "annual revenue budget financial forecast",
        ],
        max_folders=2,
        stop_words={"the", "and"},
        max_depth=2,
        max_features=100,
        pre_fetched_vectors=None,
    )
    misc_leaves = cluster_plan["Miscellaneous"]
    assert misc_leaves["q1.txt"]["routed_by"] == "clustering"
    assert (
        "revenue" in misc_leaves["q1.txt"]["match"]
        or "budget" in misc_leaves["q1.txt"]["match"]
    )

    # 2. Verify analyzer sets policy_priority on policy files and routed_by='clustering' on None leaves
    ws = tmp_path / "sort_ws"
    ws.mkdir()
    (ws / "contract.txt").write_text(
        "NDA confidential legal agreement", encoding="utf-8"
    )
    (ws / "receipt.txt").write_text("coffee shop receipt", encoding="utf-8")

    cfg = Settings(
        AI_CONSENT_GRANTED=True,
        KEYWORD_RULES={"receipt": "Expenses/Receipts"},
        POLICIES=[
            {
                "type": "compliance",
                "expression": "confidential",
                "target_path": "Legal/Confidential",
                "priority": 75,
                "action": "retain",
                "halting": False,
                "enabled": True,
            }
        ],
    )
    worker = DBWorker()
    db = Database(tmp_path / "analyzer.db", worker)
    analyzer = IncrementalAnalyzer(
        max_folders=3,
        stop_words=set(),
        db=db,
        strategy_name="recursive_kmeans",
        model_path=None,
    )
    try:
        analyzer.partial_fit(
            str(ws),
            {
                "contract.txt": "NDA confidential legal agreement",
                "receipt.txt": "coffee shop receipt",
            },
            runtime_settings=cfg,
        )
        generated = analyzer.generate_sorting_plan(
            base_dir=str(ws),
            runtime_settings=cfg,
        )
    finally:
        analyzer.close()
        worker.stop(stop_embeddings=False)

    pol_node = generated["Legal"]["Confidential"]["contract.txt"]
    assert pol_node["routed_by"] == "compliance"
    assert pol_node["match"] == "confidential"
    assert pol_node["policy_priority"] == 75
    assert pol_node["policy_action"] == "retain"

    kw_node = generated["Expenses"]["Receipts"]["receipt.txt"]
    assert kw_node["routed_by"] == "keyword"
    assert kw_node["match"] == "receipt"

    # 3. Verify TUI plan tree badges and inspector output across routing types
    tui_settings = AppSettings(filepath=str(tmp_path / "tui_settings.json"))
    tui_settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=tui_settings, base_dir=str(ws))
    app.plan = {
        "Legal": {
            "contract.txt": {
                "__type__": "file",
                "filepath": str(ws / "contract.txt"),
                "routed_by": "compliance",
                "match": "confidential",
                "policy_priority": 75,
                "policy_action": "retain",
            }
        },
        "Expenses": {
            "receipt.txt": {
                "__type__": "file",
                "filepath": str(ws / "receipt.txt"),
                "routed_by": "keyword",
                "match": "receipt",
            }
        },
        "HistoryFolder": {
            "old_tax.pdf": {
                "__type__": "file",
                "filepath": str(ws / "old_tax.pdf"),
                "routed_by": "historical",
                "match": "user assignment",
            }
        },
        "ClusterTopic": {
            "q1_budget.txt": {
                "__type__": "file",
                "filepath": str(ws / "q1_budget.txt"),
                "routed_by": "clustering",
                "match": "revenue, budget, forecast",
            }
        },
    }

    async with app.run_test(size=(110, 35)) as pilot:
        app.rebuild_tree()
        await pilot.pause()

        tree = app.query_one("#plan-tree", Tree)
        file_nodes = []

        def _walk(n):
            if n.data and n.data.get("is_file"):
                file_nodes.append(n)
            for ch in n.children:
                _walk(ch)

        _walk(tree.root)
        assert len(file_nodes) == 4
        labels_by_key = {n.data["key"]: n.label.plain for n in file_nodes}

        assert "[POLICY]" in labels_by_key["contract.txt"]
        assert "[ACTION: RETAIN]" in labels_by_key["contract.txt"]
        assert "[KEYWORD]" in labels_by_key["receipt.txt"]
        assert "[HIST]" in labels_by_key["old_tax.pdf"]
        assert "[CLUSTER]" in labels_by_key["q1_budget.txt"]

        nodes_by_key = {n.data["key"]: n for n in file_nodes}

        # Inspect policy-routed file
        pol_inspector = app._update_inspector(
            nodes_by_key["contract.txt"], announce=True
        )
        assert "Matched Rule:" in pol_inspector
        assert "confidential" in pol_inspector
        assert "Policy Priority:" in pol_inspector
        assert "75" in pol_inspector
        assert "Policy Action:" in pol_inspector
        assert "retain" in pol_inspector

        # Inspect cluster-routed file
        cluster_inspector = app._update_inspector(
            nodes_by_key["q1_budget.txt"], announce=True
        )
        assert "Cluster Keywords:" in cluster_inspector
        assert "revenue, budget, forecast" in cluster_inspector


@pytest.mark.anyio
async def test_extraction_preview_supports_non_pdf_and_pdf_with_cached_sanitized_text(
    tmp_path: Path,
):
    """Req 7, Constraints 2 & 3, & AC 6: Ctrl+X opens preview on non-PDF and PDF files, reads cached DB text, and scrubs secrets/home paths."""
    ws = tmp_path / "docs_ws"
    ws.mkdir()
    home_str = str(Path.home())

    txt_file = ws / "deployment_notes.txt"
    txt_file.write_text(
        f"Config stored at {home_str}/.config/app and token sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890",
        encoding="utf-8",
    )

    cached_md = ws / "cached_summary.md"
    cached_md.write_text("On-disk placeholder", encoding="utf-8")

    pdf_file = Path(
        _make_sample_pdf(ws / "statement.pdf", "Embedded PDF Invoice Total $450.00")
    )

    # Store decrypted text in encrypted SQLite cache via Database API without modifying schema
    worker = DBWorker()
    db = Database(tmp_path / "preview_cache.db", worker)
    try:
        db.upsert_document(
            str(ws),
            "cached_summary.md",
            "hash123",
            f"Cached DB Document at {home_str}/vault with AWS key AKIAIOSFODNN7EXAMPLE",
        )

        settings = AppSettings(filepath=str(tmp_path / "settings.json"))
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(ws), db=db)
        app.plan = {
            "Docs": {
                "deployment_notes.txt": {
                    "__type__": "file",
                    "filepath": str(txt_file),
                    "routed_by": "keyword",
                    "match": "deployment",
                },
                "cached_summary.md": {
                    "__type__": "file",
                    "filepath": str(cached_md),
                    "routed_by": "clustering",
                    "match": "summary, vault",
                },
                "statement.pdf": {
                    "__type__": "file",
                    "filepath": str(pdf_file),
                    "routed_by": "keyword",
                    "match": "invoice",
                },
            }
        }

        async with app.run_test(size=(105, 34)) as pilot:
            app.rebuild_tree()
            await pilot.pause()

            tree = app.query_one("#plan-tree", Tree)
            nodes_by_key = {}

            def _collect(n):
                if n.data and n.data.get("is_file"):
                    nodes_by_key[n.data["key"]] = n
                for ch in n.children:
                    _collect(ch)

            _collect(tree.root)

            # 1. Non-PDF local file preview via action_open_extraction_preview ([Ctrl+X])
            app.active_tree_node = nodes_by_key["deployment_notes.txt"]
            app.action_open_extraction_preview()
            await pilot.pause()
            assert isinstance(app.screen, ExtractionPreviewModal)
            txt_modal = app.screen
            assert txt_modal.audit_a11y_compliance()["violations"] == []

            rendered_txt = str(
                txt_modal.query_one("#preview-extracted-text", Static).render()
            )
            assert "Config stored at" in rendered_txt
            assert "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ1234567890" not in rendered_txt
            assert "REDACTED" in rendered_txt
            assert home_str not in rendered_txt
            txt_modal.action_cancel()
            await pilot.pause()

            # 2. Cached non-PDF document read via Database.get_document
            app.active_tree_node = nodes_by_key["cached_summary.md"]
            app.action_open_extraction_preview()
            await pilot.pause()
            assert isinstance(app.screen, ExtractionPreviewModal)
            md_modal = app.screen
            assert md_modal.text_source == "cache"
            rendered_md = str(
                md_modal.query_one("#preview-extracted-text", Static).render()
            )
            assert "Cached DB Document" in rendered_md
            assert "AKIAIOSFODNN7EXAMPLE" not in rendered_md
            assert home_str not in rendered_md
            md_modal.action_cancel()
            await pilot.pause()

            # 3. PDF file preview shows BOTH density table AND sanitized extracted text
            app.active_tree_node = nodes_by_key["statement.pdf"]
            app.action_open_extraction_preview()
            await pilot.pause()
            assert isinstance(app.screen, ExtractionPreviewModal)
            pdf_modal = app.screen
            assert pdf_modal.audit_a11y_compliance()["violations"] == []
            dt = pdf_modal.query_one("#preview-table", DataTable)
            assert dt.row_count == 1
            rendered_pdf = str(
                pdf_modal.query_one("#preview-extracted-text", Static).render()
            )
            assert "Embedded PDF Invoice Total $450.00" in rendered_pdf
            pdf_modal.action_cancel()
            await pilot.pause()
    finally:
        worker.stop(stop_embeddings=False)
