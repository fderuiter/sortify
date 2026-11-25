"""End-to-end and unit tests for folder anchoring, scan depth/filtering, learned rules, negative ratings, and generative naming."""

from __future__ import annotations

import os
from pathlib import Path
from unittest import mock

import numpy as np
import pytest
from pydantic import ValidationError
from textual.widgets import DataTable, Input, Select, Tree

from app.config import AppSettings, Settings
from app.core.analyzer_strategies import (
    GenerativeNamingStrategy,
    RecursiveKMeansStrategy,
    clustering_registry,
)
from app.core.daemon import ContinuousWatchdogDaemon
from app.core.db_conn import clear_connection_cache
from app.core.exceptions import HashVerificationError
from app.core.hashes_registry import HASHES
from app.core.mover import execute_moves
from app.core.pipeline import SortPipeline, reroot_plan
from app.core.scanner import (
    discover_existing_folders,
    get_files_recursively,
)
from app.core.session import AppSession
from app.core.shared_registry import SharedModelRegistry
from app.core.verifier import VerificationEngine
from app.ui.modals.edit import ReassignModal
from app.ui.modals.rules import ActiveRuleManagerModal
from app.ui.modals.settings import SettingsModal
from app.ui.tui import AutoSorterTUI


@pytest.fixture(autouse=True)
def _clean_env_and_sessions(monkeypatch, tmp_path):
    """Isolate AUTOSORTER_APP_DIR and suppress abandoned-session prompts."""
    app_dir = tmp_path / "_autosorter_app"
    app_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    monkeypatch.setattr(
        "app.core.session_catalog.find_abandoned_sessions", lambda *a, **k: []
    )
    yield
    clear_connection_cache(only_current_and_inactive=False)


def _extract_planned_destinations(plan: dict) -> dict[str, str]:
    """Return {source_rel_or_name: folder_path} across a nested sorting plan."""
    raw = plan.plan if hasattr(plan, "plan") and isinstance(plan.plan, dict) else plan
    out: dict[str, str] = {}

    def _walk(node: dict, prefix: str = "") -> None:
        for key, val in node.items():
            if str(key).startswith("__"):
                continue
            v = val.plan if hasattr(val, "plan") and isinstance(val.plan, dict) else val
            if not isinstance(v, dict):
                continue
            if v.get("__type__") == "file":
                src = str(v.get("source") or key).replace("\\", "/")
                out[src] = prefix.strip("/")
            else:
                next_prefix = f"{prefix}/{key}" if prefix else str(key)
                _walk(v, next_prefix)

    _walk(raw)
    return out


# ---------------------------------------------------------------------------
# AC 1 & Reqs 1-2: SCAN_MAX_DEPTH + Unified ScanFilter across scanner,
# pipeline, daemon, and mover + SettingsModal persistence
# ---------------------------------------------------------------------------


def test_scan_max_depth_config_validation_and_defaults(tmp_path):
    """SCAN_MAX_DEPTH defaults to 0 and enforces integer bounds 0..20."""
    default_cfg = Settings()
    assert default_cfg.SCAN_MAX_DEPTH == 0

    valid_cfg = Settings(SCAN_MAX_DEPTH=20)
    assert valid_cfg.SCAN_MAX_DEPTH == 20

    with pytest.raises(ValidationError):
        Settings(SCAN_MAX_DEPTH=-1)

    with pytest.raises(ValidationError):
        Settings(SCAN_MAX_DEPTH=21)

    settings_file = tmp_path / "settings.json"
    app_settings = AppSettings(filepath=str(settings_file))
    app_settings.SCAN_MAX_DEPTH = 3
    app_settings._save()
    app_settings.load()
    assert app_settings.SCAN_MAX_DEPTH == 3


@pytest.mark.anyio
async def test_settings_modal_validates_and_persists_scan_max_depth_and_generative(
    tmp_path,
):
    """SettingsModal validates SCAN_MAX_DEPTH bounds and AutoSorterTUI persists it."""
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))

    async with app.run_test(size=(100, 38)) as pilot:
        app.action_open_settings()
        await pilot.pause()

        modal = app.screen
        assert isinstance(modal, SettingsModal)

        depth_input = modal.query_one("#input-scan-max-depth", Input)
        assert depth_input.value == "0"

        # Out-of-range value rejected
        depth_input.value = "25"
        modal.action_save()
        await pilot.pause()
        assert app.screen is modal

        # Valid depth + generative strategy persisted through callback
        depth_input.value = "2"
        strat_select = modal.query_one("#select-strategy", Select)
        strat_select.value = "generative"
        modal.action_save()
        await pilot.pause()

        assert settings.SCAN_MAX_DEPTH == 2
        assert settings.SORTING_STRATEGY == "generative"
        assert settings.AI_ASSISTED_NAMING is True


def test_ac1_top_level_scan_keeps_subfolders_app_bundles_protected_and_crdownload_untouched(
    tmp_path,
):
    """AC 1: At SCAN_MAX_DEPTH=0, Invoices/, MyApp.app, ProtectedSub/, and partial.crdownload remain untouched."""
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Existing subfolder with nested document
    invoices_dir = ws / "Invoices"
    invoices_dir.mkdir()
    existing_invoice = invoices_dir / "jan_paid.pdf"
    existing_invoice.write_text("Paid January invoice")

    # macOS .app bundle directory
    app_bundle = ws / "MyApp.app" / "Contents"
    app_bundle.mkdir(parents=True)
    bundle_plist = app_bundle / "Info.plist"
    bundle_plist.write_text("<plist>MyApp</plist>")

    # Protected subdirectory
    protected_sub = ws / "ProtectedSub"
    protected_sub.mkdir()
    protected_doc = protected_sub / "secret_keys.txt"
    protected_doc.write_text("Top secret credentials")

    # In-flight download with ignored extension
    partial_dl = ws / "partial.crdownload"
    partial_dl.write_text("unfinished download bytes")

    # Legitimate top-level file to sort
    top_note = ws / "project_architecture_spec.txt"
    top_note.write_text(
        "Distributed systems architecture design document and microservice topology."
    )

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.SCAN_MAX_DEPTH = 0
    settings.PROTECTED_PATHS = [str(protected_sub)]

    # 1. Scanner check
    scanned = get_files_recursively(str(ws), settings=settings)
    assert scanned == ["project_architecture_spec.txt"]

    # 2. SortPipeline full run check
    session = AppSession(settings, base_dir=str(ws))
    try:
        plan, result = SortPipeline(session).run()
        assert result.ok
        assert [Path(p).name for p in result.moved] == ["project_architecture_spec.txt"]

        # Untouched assertions
        assert existing_invoice.exists()
        assert bundle_plist.exists()
        assert protected_doc.exists()
        assert partial_dl.exists()

        # 3. Mover direct defense-in-depth check against crafted plan targeting excluded items
        crafted_plan = {
            "Tampered": {
                "Info.plist": {
                    "__type__": "file",
                    "relative_source": "MyApp.app/Contents/Info.plist",
                },
                "secret_keys.txt": {
                    "__type__": "file",
                    "relative_source": "ProtectedSub/secret_keys.txt",
                },
                "partial.crdownload": {
                    "__type__": "file",
                    "relative_source": "partial.crdownload",
                },
            }
        }
        tamper_res = execute_moves(
            str(ws),
            crafted_plan,
            session.db,
            session.history_manager,
            settings,
        )
        assert tamper_res.get("moved", 0) == 0
        assert bundle_plist.exists()
        assert protected_doc.exists()
        assert partial_dl.exists()
    finally:
        session.close()

    # 4. Daemon ignore check
    daemon = ContinuousWatchdogDaemon(settings, str(ws))
    assert daemon.should_ignore_path(str(existing_invoice)) is True
    assert daemon.should_ignore_path(str(bundle_plist)) is True
    assert daemon.should_ignore_path(str(protected_doc)) is True
    assert daemon.should_ignore_path(str(partial_dl)) is True


# ---------------------------------------------------------------------------
# AC 2 & Reqs 3-4: Existing Folder Discovery + TF-IDF & ONNX Anchoring +
# --dest-dir + ReassignModal folder listing
# ---------------------------------------------------------------------------


def test_ac2_anchoring_routes_billing_files_into_existing_invoices_in_base_and_dest_dir(
    tmp_path,
):
    """AC 2: Incoming billing files route into existing Invoices/ in base_dir or --dest-dir without duplicate folders."""
    # Case A: Existing Invoices/ in base_dir (TF-IDF similarity path)
    ws = tmp_path / "inbox"
    ws.mkdir()
    existing_invoices = ws / "Invoices"
    existing_invoices.mkdir()
    (existing_invoices / "acme_invoice_2025.txt").write_text(
        "Invoice billing statement payment due subtotal tax remittance."
    )

    incoming = ws / "vendor_billing_april.txt"
    incoming.write_text(
        "Monthly invoice billing statement payment due remittance for vendor."
    )

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.SCAN_MAX_DEPTH = 0
    settings.KEYWORD_RULES = {}
    settings.LEARNED_RULES = {}

    session = AppSession(settings, base_dir=str(ws))
    try:
        plan, result = SortPipeline(session).run()
        assert result.ok
        dest_map = _extract_planned_destinations(plan)
        assert dest_map.get("vendor_billing_april.txt") == "Invoices"
        assert (existing_invoices / "vendor_billing_april.txt").exists()
    finally:
        session.close()

    # Case B: Existing Invoices/ in external --dest-dir
    src_dir = tmp_path / "downloads"
    src_dir.mkdir()
    dest_dir = tmp_path / "archive_root"
    (dest_dir / "Invoices").mkdir(parents=True)
    (dest_dir / "Invoices" / "prior_bill.txt").write_text(
        "Invoice payment billing statement total amount due."
    )
    (src_dir / "q2_invoice_statement.txt").write_text(
        "Q2 billing invoice statement payment due."
    )

    session_dest = AppSession(settings, base_dir=str(src_dir))
    try:
        pipeline = SortPipeline(session_dest)
        raw_plan = pipeline.plan(dest_dir=str(dest_dir))
        dest_map2 = _extract_planned_destinations(raw_plan)
        assert dest_map2.get("q2_invoice_statement.txt") == "Invoices"

        rerooted = reroot_plan(raw_plan, str(dest_dir))
        session_dest.base_dir = str(dest_dir.resolve())
        exec_res = pipeline.execute(rerooted)
        assert exec_res.ok
        assert (dest_dir / "Invoices" / "q2_invoice_statement.txt").exists()
    finally:
        session_dest.close()


def test_onnx_semantic_anchoring_and_post_cluster_snapping(tmp_path):
    """Req 4: Semantic vector anchoring and post-clustering folder snapping route into existing folders."""
    ws = tmp_path / "semantic_ws"
    ws.mkdir()
    (ws / "Tax_Returns").mkdir()
    (ws / "unlabeled_01.txt").write_text("Form 1040 federal filing schedule.")
    (ws / "unlabeled_02.txt").write_text("Quarterly estimated deduction worksheet.")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = True
    settings.SCAN_MAX_DEPTH = 0
    settings.KEYWORD_RULES = {}

    session = AppSession(settings, base_dir=str(ws))
    try:
        # Simulate live local ONNX embedding vectors where files align closely with "Tax Returns"
        dim = session.analyzer.embedding_manager.dimensions
        tax_vec_arr = np.zeros(dim, dtype=np.float32)
        tax_vec_arr[0] = 0.95
        tax_vec_arr[1] = 0.05
        tax_vec_arr /= np.linalg.norm(tax_vec_arr)
        tax_vec = tax_vec_arr.tolist()

        with (
            mock.patch.object(
                type(session.analyzer.embedding_manager),
                "is_fallback",
                new_callable=mock.PropertyMock,
                return_value=False,
            ),
            mock.patch.object(
                session.analyzer.embedding_manager,
                "generate_embedding",
                side_effect=lambda _text: tax_vec,
            ),
            mock.patch.object(
                session.analyzer.embedding_manager,
                "get_vectors_batch",
                side_effect=lambda _base, fns, regenerate=False: dict.fromkeys(
                    fns, tax_vec
                ),
            ),
        ):
            plan = SortPipeline(session).plan()
        dest_map = _extract_planned_destinations(plan)
        assert dest_map.get("unlabeled_01.txt") == "Tax_Returns"
        assert dest_map.get("unlabeled_02.txt") == "Tax_Returns"
    finally:
        session.close()


@pytest.mark.anyio
async def test_reassign_modal_discovers_existing_folders_on_disk(tmp_path):
    """AC 2 & Req 3: ReassignModal lists existing folders discovered in base_dir and dest_dir."""
    base = tmp_path / "base"
    dest = tmp_path / "dest"
    (base / "Invoices").mkdir(parents=True)
    (base / "MyApp.app").mkdir()
    (base / ".hidden_folder").mkdir()
    (base / "ProtectedVault").mkdir()
    (dest / "Receipts_2026").mkdir(parents=True)

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.PROTECTED_PATHS = [str(base / "ProtectedVault")]

    folders = discover_existing_folders(
        str(base), dest_dir=str(dest), settings=settings
    )
    assert "Invoices" in folders
    assert "Receipts_2026" in folders
    assert "MyApp.app" not in folders
    assert ".hidden_folder" not in folders
    assert "ProtectedVault" not in folders

    modal = ReassignModal(
        existing_folders=["Misc"],
        count=1,
        base_dir=str(base),
        dest_dir=str(dest),
        settings=settings,
    )
    assert "Invoices" in modal.existing_folders
    assert "Receipts_2026" in modal.existing_folders
    assert "ProtectedVault" not in modal.existing_folders


# ---------------------------------------------------------------------------
# AC 3 & Req 5: SCAN_MAX_DEPTH=2, PRESERVE_HIERARCHY flatten vs single-pass
# nesting, and subfolder rule/cluster execution with zero silent skips
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("preserve_hierarchy", [False, True])
def test_ac3_depth_2_preserve_hierarchy_flatten_vs_nest_once_and_moves_cleanly(
    tmp_path, preserve_hierarchy
):
    """AC 3: With SCAN_MAX_DEPTH=2, PRESERVE_HIERARCHY=False flattens, True nests once, and moves succeed."""
    ws = tmp_path / f"ws_hier_{preserve_hierarchy}"
    (ws / "dept" / "q1").mkdir(parents=True)
    (ws / "dept" / "q1" / "deep3").mkdir(parents=True)

    rule_file = ws / "dept" / "invoice_march.txt"
    rule_file.write_text("March consulting invoice #402.")

    cluster_file = ws / "dept" / "q1" / "engineering_roadmap.txt"
    cluster_file.write_text(
        "Kubernetes cluster autoscaling architecture and Rust telemetry pipeline."
    )

    beyond_depth_file = ws / "dept" / "q1" / "deep3" / "ignored_depth3.txt"
    beyond_depth_file.write_text(
        "Depth 3 should remain untouched when SCAN_MAX_DEPTH=2."
    )

    settings = AppSettings(
        filepath=str(tmp_path / f"settings_{preserve_hierarchy}.json")
    )
    settings.AI_CONSENT_GRANTED = False
    settings.SCAN_MAX_DEPTH = 2
    settings.PRESERVE_HIERARCHY = preserve_hierarchy
    settings.KEYWORD_RULES = {"invoice": "Invoices"}

    scanned = get_files_recursively(str(ws), settings=settings)
    norm_scanned = {p.replace("\\", "/") for p in scanned}
    assert "dept/invoice_march.txt" in norm_scanned
    assert "dept/q1/engineering_roadmap.txt" in norm_scanned
    assert "dept/q1/deep3/ignored_depth3.txt" not in norm_scanned

    session = AppSession(settings, base_dir=str(ws))
    try:
        pipeline = SortPipeline(session)
        plan = pipeline.plan()
        dest_map = _extract_planned_destinations(plan)

        assert "dept/invoice_march.txt" in dest_map
        assert "dept/q1/engineering_roadmap.txt" in dest_map

        inv_folder = dest_map["dept/invoice_march.txt"]
        eng_folder = dest_map["dept/q1/engineering_roadmap.txt"]

        if not preserve_hierarchy:
            # Flattened: no "dept" or "dept/q1" subfolder segments injected
            assert inv_folder == "Invoices"
            assert "dept" not in eng_folder.split("/")
        else:
            # Nested ONCE: never "dept/dept" or "q1/q1"
            assert inv_folder == "Invoices/dept"
            assert eng_folder.endswith("/dept/q1")
            assert "dept/dept" not in eng_folder
            assert "q1/q1" not in eng_folder

        # VerificationEngine resolves real source paths without skipping
        moves = VerificationEngine.get_moves(str(ws), plan)
        assert len(moves) == 2

        result = pipeline.execute(plan)
        assert result.ok
        assert len(result.moved) == 2
        assert result.skipped == []
        assert not rule_file.exists()
        assert not cluster_file.exists()
        assert beyond_depth_file.exists()

        if not preserve_hierarchy:
            assert (ws / "Invoices" / "invoice_march.txt").exists()
        else:
            assert (ws / "Invoices" / "dept" / "invoice_march.txt").exists()
    finally:
        session.close()


# ---------------------------------------------------------------------------
# AC 4 & Reqs 6-7: TUI Reassignment -> LEARNED_RULES + ActiveRuleManagerModal
# + SortPipeline.plan MetadataPass & LEARNED_RULES on text AND filenames
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ac4_tui_reassignment_persists_learned_rule_and_pipeline_applies_to_text_and_filename(
    tmp_path,
):
    """AC 4: Reassigning in TUI writes to LEARNED_RULES, ActiveRuleManagerModal manages it, and SortPipeline applies it to text & filenames."""
    ws = tmp_path / "learned_ws"
    ws.mkdir()
    reassigned_src = ws / " reimbursement_voucher_2026.txt".strip()
    reassigned_src.write_text("Employee travel reimbursement voucher.")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.SCAN_MAX_DEPTH = 0
    settings.KEYWORD_RULES = {}
    settings.LEARNED_RULES = {}

    app = AutoSorterTUI(settings=settings, base_dir=str(ws))
    app.plan = {
        "Uncategorized": {
            "reimbursement_voucher_2026.txt": {
                "__type__": "file",
                "filepath": str(reassigned_src),
                "source": "reimbursement_voucher_2026.txt",
                "target_filename": "reimbursement_voucher_2026.txt",
                "is_checked": True,
                "is_excluded": False,
                "is_locked": False,
                "folder": "Uncategorized",
            }
        }
    }

    async with app.run_test(size=(100, 35)) as pilot:
        await pilot.pause()
        app.render_tree()
        await pilot.pause()
        tree = app.query_one("#plan-tree", Tree)
        # Expand and move cursor to the leaf file node
        cat_node = tree.root.children[0]
        cat_node.expand()
        await pilot.pause()
        leaf_node = cat_node.children[0]
        tree.select_node(leaf_node)
        await pilot.pause()

        app.action_reassign_selected()
        await pilot.pause()
        modal = app.screen
        assert isinstance(modal, ReassignModal)

        folder_input = modal.query_one("#input-target-folder", Input)
        folder_input.value = "Expense_Claims"
        modal.action_confirm()
        await pilot.pause()

        # Verify a pattern was persisted to folder-scoped LEARNED_RULES -> "Expense_Claims"
        scoped_settings = app.get_active_settings()
        assert scoped_settings.LEARNED_RULES
        learned_patterns = [
            pat
            for pat, target in scoped_settings.LEARNED_RULES.items()
            if target == "Expense_Claims"
        ]
        assert len(learned_patterns) == 1
        learned_pat = learned_patterns[0]
        assert "reimbursement" in learned_pat

        # Verify ActiveRuleManagerModal lists the learned rule and supports editing/toggling
        rule_modal = ActiveRuleManagerModal(scoped_settings)
        app.push_screen(rule_modal)
        await pilot.pause()
        table = rule_modal.query_one("#rules-table", DataTable)
        assert table.row_count == 1
        assert rule_modal._rules_data[0]["kind"] == "learned"
        assert rule_modal._rules_data[0]["pattern"] == learned_pat
        assert rule_modal._rules_data[0]["target_folder"] == "Expense_Claims"

        # Toggle learned rule off -> moves to DISABLED_LEARNED_RULES, then toggle back on
        rule_modal.action_toggle_rule()
        await pilot.pause()
        assert learned_pat in scoped_settings.DISABLED_LEARNED_RULES
        assert learned_pat not in scoped_settings.LEARNED_RULES
        rule_modal.action_toggle_rule()
        await pilot.pause()
        assert scoped_settings.LEARNED_RULES.get(learned_pat) == "Expense_Claims"
        # Ensure global LEARNED_RULES also carries Expense_Claims for the next_batch workspace test below
        settings.LEARNED_RULES = {learned_pat: "Expense_Claims"}
        settings.KEYWORD_RULES = {}
        app.pop_screen()
        await pilot.pause()

    # Now verify SortPipeline.plan applies LEARNED_RULES to BOTH filename matches AND extracted-text matches!
    batch_ws = tmp_path / "next_batch"
    batch_ws.mkdir()
    # 1) Filename match (empty/minimal body)
    fn_match = batch_ws / f"april_{learned_pat}_99.txt"
    fn_match.write_text("Short note.")
    # 2) Extracted-text match with opaque filename (status_match is None!)
    opaque_text_match = batch_ws / "scan_0042.txt"
    opaque_text_match.write_text(
        f"Scanned receipt containing {learned_pat} approved by finance."
    )

    session = AppSession(settings, base_dir=str(batch_ws))
    try:
        plan = SortPipeline(session).plan()
        dest_map = _extract_planned_destinations(plan)
        assert dest_map.get(fn_match.name) == "Expense_Claims"
        assert dest_map.get(opaque_text_match.name) == "Expense_Claims"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# AC 5 & Req 7: Marking a file negative ([-]) stops IncrementalAnalyzer from
# repeating the rejected folder target
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_ac5_negative_rating_stops_analyzer_from_repeating_rejected_folder(
    tmp_path,
):
    """AC 5: Marking a file negative via SelectionActionsMixin prevents IncrementalAnalyzer from repeating that folder."""
    ws = tmp_path / "rating_ws"
    ws.mkdir()
    (ws / "Invoices").mkdir()
    (ws / "Archive").mkdir()

    target_doc = ws / "ambiguous_statement.txt"
    target_doc.write_text("Invoice billing statement summary for Q1.")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.SCAN_MAX_DEPTH = 0
    settings.KEYWORD_RULES = {"invoice": "Invoices"}
    settings.AUTO_QUARANTINE_RATINGS = []

    session = AppSession(settings, base_dir=str(ws))
    try:
        initial_plan = SortPipeline(session).plan()
        initial_dest = _extract_planned_destinations(initial_plan)
        assert initial_dest.get("ambiguous_statement.txt") == "Invoices"

        # Launch TUI with active session & mark file negative ([-])
        app = AutoSorterTUI(settings=settings, base_dir=str(ws))
        app.app_session = session
        app.plan = {
            "Invoices": {
                "ambiguous_statement.txt": {
                    "__type__": "file",
                    "filepath": str(target_doc),
                    "source": "ambiguous_statement.txt",
                    "target_filename": "ambiguous_statement.txt",
                    "is_checked": True,
                    "is_excluded": False,
                    "is_locked": False,
                    "folder": "Invoices",
                }
            }
        }

        async with app.run_test(size=(100, 35)) as pilot:
            await pilot.pause()
            app.render_tree()
            await pilot.pause()
            tree = app.query_one("#plan-tree", Tree)
            cat_node = tree.root.children[0]
            cat_node.expand()
            await pilot.pause()
            leaf_node = cat_node.children[0]
            tree.select_node(leaf_node)
            await pilot.pause()

            app.action_rate_negative()
            await pilot.pause()

        # Confirm DB persisted rating="negative" and "!Invoices" rejection target on relative path
        rating_val = session.db.get_document_rating(str(ws), "ambiguous_statement.txt")
        assert rating_val == "negative"
        verified_docs = {
            str(d[0]).replace("\\", "/"): d[3]
            for d in session.db.get_all_documents(str(ws))
            if len(d) > 3
        }
        assert verified_docs.get("ambiguous_statement.txt") == "!Invoices"

        # Re-plan: IncrementalAnalyzer must NOT route ambiguous_statement.txt back into "Invoices"
        next_plan = SortPipeline(session).plan()
        next_dest = _extract_planned_destinations(next_plan)
        assert next_dest.get("ambiguous_statement.txt") != "Invoices"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# AC 6 & Reqs 8-9: --strategy generative wiring, distinct hashes in
# hashes_registry.py, Folder/Folder deduplication, and DEGRADED_FALLBACK
# ---------------------------------------------------------------------------


def test_ac6_generative_hashes_are_distinct_and_verified_in_model_registry(tmp_path):
    """AC 6: HASHES['generative_naming'] has distinct digests from model_download and verifies via ModelRegistry."""
    gen_hashes = HASHES["generative_naming"]
    emb_hashes = HASHES["model_download"]

    assert set(gen_hashes.keys()) != set(emb_hashes.keys())
    assert set(gen_hashes.values()).isdisjoint(set(emb_hashes.values()))
    assert "model.gguf" in gen_hashes

    registry = SharedModelRegistry.get_instance()

    # Tampered weights fail verification with HashVerificationError and record FAILED health
    bad_dir = tmp_path / "bad_gen_model"
    bad_dir.mkdir()
    (bad_dir / "model.gguf").write_bytes(b"corrupted-gguf-payload")
    (bad_dir / "config.json").write_text("{}")
    with pytest.raises(HashVerificationError):
        registry.verify_integrity("generative_naming", str(bad_dir))


def test_ac6_folder_folder_deduplication_in_recursive_kmeans():
    """AC 6 & Req 9: RecursiveKMeansStrategy deduplicates identical parent/child labels (Folder/Folder)."""
    strategy = RecursiveKMeansStrategy()
    strategy.max_folders = 2
    strategy.max_depth = 3
    strategy.max_features = 3
    strategy.stop_words = set()
    strategy._error = 0.0

    # Two distinct clusters at depth 1 and depth 2 whose keyword label repeats "Invoices"
    docs = [
        "alpha invoice billing doc 1",
        "alpha invoice billing doc 2",
        "alpha invoice billing doc 3",
        "alpha invoice billing doc 4",
        "beta payment remittance doc 1",
        "beta payment remittance doc 2",
        "beta payment remittance doc 3",
        "beta payment remittance doc 4",
    ]
    fns = [f"inv_{i}.txt" for i in range(8)]

    with mock.patch.object(strategy, "_get_cluster_keywords", return_value="Invoices"):
        nested = strategy._cluster_recursive(fns, docs, depth=1)

    assert "Invoices" in nested
    # Under nested["Invoices"], there must NEVER be a duplicate nested["Invoices"]["Invoices"]!
    assert "Invoices" not in nested["Invoices"]
    leaves = [
        k
        for k, v in nested["Invoices"].items()
        if isinstance(v, dict) and v.get("__type__") == "file"
    ]
    assert sorted(leaves) == sorted(fns)


def test_ac6_generative_strategy_session_wiring_and_degraded_fallback_health(tmp_path):
    """AC 6 & Reqs 8-9: SORTING_STRATEGY='generative' invokes GenerativeNamingStrategy and reports DEGRADED_FALLBACK when weights fail."""
    ws = tmp_path / "gen_ws"
    ws.mkdir()
    for idx in range(4):
        (ws / f"research_note_{idx}.txt").write_text(
            f"Quantum error correction stabilizer lattice experiment {idx}."
        )

    # Place invalid/corrupt weights in generative_naming directory so integrity check fails
    gen_model_dir = (
        Path(os.environ["AUTOSORTER_APP_DIR"]) / "models" / "generative_naming"
    )
    gen_model_dir.mkdir(parents=True, exist_ok=True)
    (gen_model_dir / "model.gguf").write_bytes(b"invalid-weights")
    (gen_model_dir / "config.json").write_text("{}")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = True
    settings.SORTING_STRATEGY = "generative"
    settings.SCAN_MAX_DEPTH = 0
    settings.KEYWORD_RULES = {}

    registry = SharedModelRegistry.get_instance()
    with registry._lock:
        registry._model_health.clear()

    gen_strategy = clustering_registry.get_strategy("generative")
    assert isinstance(gen_strategy, GenerativeNamingStrategy)

    session = AppSession(settings, base_dir=str(ws))
    try:
        assert session.analyzer.strategy_name == "generative"

        with mock.patch.object(
            gen_strategy, "generate_plan", wraps=gen_strategy.generate_plan
        ) as spy_gen:
            plan = SortPipeline(session).plan()
            assert spy_gen.call_count >= 1

        # Verify plan succeeded via fallback, has no Folder/Folder nesting, and surfaces DEGRADED_FALLBACK
        dest_map = _extract_planned_destinations(plan)
        assert len(dest_map) == 4
        for folder_path in dest_map.values():
            segs = [s.lower() for s in folder_path.split("/") if s]
            for i in range(1, len(segs)):
                assert segs[i] != segs[i - 1]

        model_status = (
            plan.model_status
            if hasattr(plan, "model_status")
            else plan.get("model_status")
        )
        assert model_status == "DEGRADED_FALLBACK"

        gen_health = registry.get_model_health("generative_naming")
        assert gen_health is not None
        health_status = (
            gen_health.get("status")
            if isinstance(gen_health, dict)
            else getattr(gen_health, "status", None)
        )
        assert health_status == "DEGRADED_FALLBACK"
    finally:
        session.close()
        with registry._lock:
            registry._model_health.clear()
