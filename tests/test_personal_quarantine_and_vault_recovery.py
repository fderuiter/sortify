"""End-to-end tests for personal quarantine mode, starter-pack routing, store v4 release exemptions, and vault recovery."""

import argparse
import asyncio
import contextlib
import io
import json
import os

import pytest
from textual.widgets import Input, Select, Switch

from app.cli.quarantine_cli import handle_quarantine_command
from app.config import (
    AppSettings,
    Settings,
    resolve_effective_auto_quarantine_ratings,
)
from app.core.analyzer import FileAnalyzer
from app.core.daemon import ContinuousWatchdogDaemon
from app.core.db import Database
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.db_worker import DBWorker
from app.core.domain_contracts import VALID_QUARANTINE_STATES
from app.core.migrations import schema_version
from app.core.pipeline import SortPipeline
from app.core.quarantine_interceptor import QuarantineInterceptorService
from app.core.resilient_file_ops import resilient_file_hash
from app.core.rule_templates import apply_starter_templates
from app.core.scanner import get_files_recursively
from app.core.session import AppSession
from app.core.store import STORE_MIGRATIONS, open_store
from app.ui.a11y import inspect_tui_component
from app.ui.modals.quarantine import QuarantineVaultModal
from app.ui.modals.settings import SettingsModal
from app.ui.tui import AutoSorterTUI


@pytest.fixture
def isolated_env(tmp_path, monkeypatch):
    """Isolate AUTOSORTER_APP_DIR and AppSettings singleton state per test."""
    app_dir = tmp_path / "app_data"
    app_dir.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    AppSettings._instance = None
    try:
        yield app_dir
    finally:
        AppSettings._instance = None
        clear_connection_cache(only_current_and_inactive=False)


@pytest.fixture
def isolated_db(tmp_path):
    """Provide an isolated Database and DBWorker."""
    worker = DBWorker()
    db = Database(tmp_path / "vault_recovery.db", worker)
    try:
        yield db
    finally:
        worker.stop()
        clear_connection_cache(only_current_and_inactive=False)


def _create_personal_documents(ws):
    docs = {
        "electric_invoice_2026.txt": "Invoice #2026-881 Electric Utility Statement Balance Due $185.00",
        "contractor_1099_nec.txt": "Form 1099-NEC Nonemployee Compensation Tax Year 2025 Summary",
        "grocery_receipt_march.txt": "Store Receipt Subtotal $64.20 Visa Payment Approved",
        "alex_rivera_resume.txt": "Alex Rivera Resume Curriculum Vitae Senior Systems Engineer",
        "service_contract_2026.txt": "Master Services Contract Agreement Terms and Conditions",
        "pharmacy_prescription_rx.txt": "Patient Prescription Rx Amoxicillin 500mg Pharmacy Refill",
    }
    for filename, content in docs.items():
        (ws / filename).write_text(content, encoding="utf-8")
    return docs


def test_personal_mode_default_and_clinical_strategy_enforcement():
    """QUARANTINE_MODE defaults to personal while strict mode and clinical strategies enforce holds."""
    cfg = Settings()
    assert cfg.QUARANTINE_MODE == "personal"
    assert resolve_effective_auto_quarantine_ratings(cfg) == []

    # Explicit personal mode overrides any residual AUTO_QUARANTINE_RATINGS unless clinical strategy runs
    personal_with_ratings = Settings(
        QUARANTINE_MODE="personal",
        AUTO_QUARANTINE_RATINGS=["CRITICAL", "HIGH"],
    )
    assert resolve_effective_auto_quarantine_ratings(personal_with_ratings) == []

    # Clinical strategies always enforce CRITICAL/HIGH holds
    for strat in ("clinical_tmf", "clinical_isf"):
        clinical_cfg = Settings(QUARANTINE_MODE="personal", SORTING_STRATEGY=strat)
        assert resolve_effective_auto_quarantine_ratings(clinical_cfg) == [
            "CRITICAL",
            "HIGH",
        ]

    strict_cfg = Settings(QUARANTINE_MODE="strict")
    assert resolve_effective_auto_quarantine_ratings(strict_cfg) == [
        "CRITICAL",
        "HIGH",
    ]


@pytest.mark.anyio
async def test_personal_mode_starter_packs_sort_and_daemon_zero_quarantine(
    tmp_path, isolated_env
):
    """Personal mode with starter packs routes invoices, 1099s, receipts, resumes, contracts, and prescriptions in sort and daemon runs with zero quarantine records."""
    sort_ws = tmp_path / "sort_ws"
    sort_ws.mkdir()
    _create_personal_documents(sort_ws)

    settings = Settings(QUARANTINE_MODE="personal")
    apply_starter_templates(
        settings,
        ["financial_tax", "medical_health", "legal_contracts", "office_hr"],
    )

    expected_targets = {
        "Financial/Invoices/electric_invoice_2026.txt",
        "Taxes/1099/contractor_1099_nec.txt",
        "Financial/Receipts/grocery_receipt_march.txt",
        "HR/Resumes/alex_rivera_resume.txt",
        "Legal/Contracts/service_contract_2026.txt",
        "Medical/Prescriptions/pharmacy_prescription_rx.txt",
    }

    with contextlib.closing(AppSession(settings, base_dir=str(sort_ws))) as session:
        pipeline = SortPipeline(session)
        plan = pipeline.plan()
        assert "_Compliance_Quarantine" not in plan
        outcome = pipeline.execute(plan)
        assert outcome.ok
        assert outcome.quarantined == []
        assert session.db.get_all_quarantine_records() == []

    for rel_target in expected_targets:
        assert (sort_ws / rel_target).exists(), f"Missing sorted target: {rel_target}"
    assert not (sort_ws / "_Compliance_Quarantine").exists()
    assert not (sort_ws / "_Quarantine_Staging").exists()

    # Verify daemon run also routes all 6 files to starter pack folders with zero quarantine records
    daemon_ws = tmp_path / "daemon_ws"
    daemon_ws.mkdir()
    docs = _create_personal_documents(daemon_ws)

    daemon = ContinuousWatchdogDaemon(settings, str(daemon_ws))
    daemon._is_running = True
    try:
        for fname in docs:
            await daemon._triage_file_path(str(daemon_ws / fname))
        daemon_session = daemon._get_or_create_session()
        assert daemon_session.db.get_all_quarantine_records() == []
    finally:
        daemon.stop()

    for rel_target in expected_targets:
        assert (daemon_ws / rel_target).exists(), (
            f"Missing daemon sorted target: {rel_target}"
        )
    assert not (daemon_ws / "_Compliance_Quarantine").exists()
    assert not (daemon_ws / "_Quarantine_Staging").exists()


def test_personal_mode_still_honors_explicit_quarantine_policy(tmp_path, isolated_db):
    """Explicit policy action='quarantine' still quarantines matching files in personal mode."""
    ws = tmp_path / "explicit_policy_ws"
    ws.mkdir()
    secret_doc = ws / "embargoed_filing.txt"
    secret_doc.write_text("EMBARGOED regulatory submission packet", encoding="utf-8")

    settings = Settings(
        QUARANTINE_MODE="personal",
        POLICIES=[
            {
                "name": "Hold Embargoed",
                "type": "keyword",
                "expression": "embargoed",
                "action": "quarantine",
                "target_path": "_Compliance_Quarantine",
                "priority": 100,
            }
        ],
    )
    service = QuarantineInterceptorService(
        db=isolated_db,
        policies=settings.POLICIES,
        runtime_settings=settings,
    )
    staged = service.stage_incoming_file(str(secret_doc), str(ws))
    result = service.process_quarantine_job(staged["job_id"])
    assert result["status"] == "QUARANTINED"
    assert result["policy_action"] == "quarantine"


def test_rule_precedence_learned_and_keyword_before_jev_and_strict_plan_matches_mover(
    tmp_path, isolated_db
):
    """KEYWORD_RULES and LEARNED_RULES evaluate before JEV categories, and strict mode routes holds to QUARANTINE_DIR_NAME."""
    ws = tmp_path / "precedence_ws"
    ws.mkdir()
    learned_file = ws / "acme_vendor_bill.txt"
    learned_file.write_text(
        "Acme Vendor Invoice #912 Payment Due $320", encoding="utf-8"
    )

    isolated_db.upsert_documents(
        [
            (
                str(ws),
                "acme_vendor_bill.txt",
                "hash_acme",
                "Acme Vendor Invoice #912 Payment Due $320",
            )
        ]
    )

    personal_settings = Settings(
        QUARANTINE_MODE="personal",
        LEARNED_RULES={"acme_vendor": "Vendors/Acme"},
    )
    analyzer = FileAnalyzer(max_folders=10, stop_words=set(), db=isolated_db)
    personal_plan = analyzer.generate_sorting_plan(
        base_dir=str(ws),
        runtime_settings=personal_settings,
        jev_results={
            "acme_vendor_bill.txt": {
                "category": "Financial & Tax",
                "confidence": 0.92,
                "is_classified": True,
                "sensitivity_rating": "HIGH",
                "sensitivity_score": 0.85,
            }
        },
    )
    assert "Vendors" in personal_plan
    assert "acme_vendor_bill.txt" in personal_plan["Vendors"]["Acme"]
    assert "_Compliance_Quarantine" not in personal_plan

    strict_settings = Settings(
        QUARANTINE_MODE="strict",
        QUARANTINE_DIR_NAME="_Regulated_Hold",
    )
    strict_plan = analyzer.generate_sorting_plan(
        base_dir=str(ws),
        runtime_settings=strict_settings,
    )
    assert "_Regulated_Hold" in strict_plan
    assert "acme_vendor_bill.txt" in strict_plan["_Regulated_Hold"]

    # Custom QUARANTINE_DIR_NAME is excluded from scanner, pipeline, and daemon
    hold_dir = ws / "_Regulated_Hold"
    hold_dir.mkdir()
    (hold_dir / "held_item.txt").write_text("Held sensitive payload", encoding="utf-8")
    active_app_settings = AppSettings(filepath=str(tmp_path / "strict_settings.json"))
    active_app_settings.QUARANTINE_DIR_NAME = "_Regulated_Hold"
    scanned = get_files_recursively(str(ws))
    assert not any("_Regulated_Hold" in item for item in scanned)

    daemon = ContinuousWatchdogDaemon(strict_settings, str(ws))
    assert daemon.should_ignore_path(str(hold_dir / "held_item.txt"))


@pytest.mark.anyio
async def test_store_migration_v4_release_exemptions_and_dismiss_restore_or_purge(
    tmp_path, isolated_env, monkeypatch
):
    """Store migration v4 records SHA-256 exemptions + verified targets, resolves relative paths from any CWD, and restores or purges dismissed items."""
    store = open_store(isolated_env)
    store_path = store.path
    assert store_path == isolated_env / "sortify.db"
    conn = get_db_connection(store_path)
    assert schema_version(conn) == STORE_MIGRATIONS[-1].version
    assert STORE_MIGRATIONS[-1].version >= 4
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "quarantine_release_exemptions" in tables
    cols = {row[1] for row in conn.execute("PRAGMA table_info(quarantine_records)")}
    assert "verified_target_path" in cols
    assert "released_file_hash" in cols

    ws = tmp_path / "regulated_ws"
    ws.mkdir()
    outside_cwd = tmp_path / "outside_cwd"
    outside_cwd.mkdir()

    strict_settings = Settings(QUARANTINE_MODE="strict")
    held_doc = ws / "patient_discharge_summary.txt"
    held_doc.write_text(
        "Patient Discharge Summary Clinical Diagnosis Prescription Confidential",
        encoding="utf-8",
    )
    expected_sha256 = resilient_file_hash(str(held_doc))

    with contextlib.closing(AppSession(strict_settings, base_dir=str(ws))) as session:
        # Insert relative _Compliance_Quarantine path to verify outside-base_dir release
        rel_hold_dir = ws / "_Compliance_Quarantine"
        rel_hold_dir.mkdir()
        rel_held_path = rel_hold_dir / "patient_discharge_summary.txt"
        held_doc.replace(rel_held_path)

        session.db.stage_quarantine_record(
            job_id="qjob_rel_hold",
            base_dir=str(ws),
            original_filepath="patient_discharge_summary.txt",
            staged_filepath="_Compliance_Quarantine/patient_discharge_summary.txt",
            file_hash=expected_sha256,
            policy_action="sensitivity_hold",
        )
        session.db.update_quarantine_status(
            job_id="qjob_rel_hold", status="IN_INSPECTION"
        )
        session.db.update_quarantine_status(
            job_id="qjob_rel_hold",
            status="QUARANTINED",
            policy_action="sensitivity_hold",
        )

        monkeypatch.chdir(outside_cwd)
        service = QuarantineInterceptorService(
            db=session.db, runtime_settings=strict_settings
        )
        rel_out = service.release_quarantine_batch(["qjob_rel_hold"])
        assert rel_out[0]["status"] == "RELEASED"
        assert held_doc.exists()
        assert not rel_held_path.exists()

        rec = session.db.get_quarantine_record("qjob_rel_hold")
        assert rec["status"] == "RELEASED"
        assert rec["released_file_hash"] == expected_sha256
        assert rec["verified_target_path"] == "patient_discharge_summary.txt"

        exemption = session.db.get_quarantine_release_exemption(
            expected_sha256, base_dir=str(ws)
        )
        assert exemption is not None
        assert exemption["file_hash"] == expected_sha256
        assert exemption["verified_target_path"] == "patient_discharge_summary.txt"

        # Later pipeline scan + mover must not re-quarantine the released SHA-256 hash
        strict_settings.KEYWORD_RULES = {"discharge": "Medical/Discharge"}
        pipeline = SortPipeline(session, settings=strict_settings)
        plan = pipeline.plan()
        assert "_Compliance_Quarantine" not in plan
        assert "Medical" in plan
        move_res = pipeline.execute(plan)
        assert move_res.quarantined == []
        sorted_target = ws / "Medical" / "Discharge" / "patient_discharge_summary.txt"
        assert sorted_target.exists()

        # Move back to root and verify daemon event also skips sensitivity hold for released SHA-256
        sorted_target.replace(held_doc)

    daemon = ContinuousWatchdogDaemon(strict_settings, str(ws))
    daemon._is_running = True
    try:
        await daemon._triage_file_path(str(held_doc))
        assert (ws / "Medical" / "Discharge" / "patient_discharge_summary.txt").exists()
        d_sess = daemon._get_or_create_session()
        active_q = d_sess.db.get_quarantine_records_by_base_dir(
            str(ws), status="QUARANTINED"
        )
        assert active_q == []
    finally:
        daemon.stop()

    # Verify dismiss_quarantine_batch restores staged file to original_filepath, and purge requires confirmation
    check_conn = get_db_connection(store_path)
    ex_rows = check_conn.execute(
        "SELECT file_hash, verified_target_path FROM quarantine_release_exemptions"
    ).fetchall()
    assert any(r[0] == expected_sha256 for r in ex_rows)

    with contextlib.closing(AppSession(strict_settings, base_dir=str(ws))) as session2:
        svc2 = QuarantineInterceptorService(
            db=session2.db, runtime_settings=strict_settings
        )
        dismiss_doc = ws / "staged_for_dismiss.txt"
        dismiss_doc.write_text("Restore me on dismiss", encoding="utf-8")
        staged_d = svc2.stage_incoming_file(str(dismiss_doc), str(ws))
        dismiss_doc.unlink()
        assert not dismiss_doc.exists()

        d_res = svc2.dismiss_quarantine_batch([staged_d["job_id"]])
        assert d_res[0]["status"] == "DISMISSED"
        assert d_res[0]["action"] == "restored"
        assert dismiss_doc.exists()
        assert not os.path.exists(staged_d["staged_filepath"])

        # Unconfirmed purge raises ValueError; confirmed purge deletes staged file
        purge_doc = ws / "staged_for_purge.txt"
        purge_doc.write_text("Purge me after confirmation", encoding="utf-8")
        staged_p = svc2.stage_incoming_file(str(purge_doc), str(ws))
        purge_doc.unlink()

        with pytest.raises(ValueError, match="confirm_purge=True"):
            svc2.dismiss_quarantine_batch([staged_p["job_id"]], purge=True)
        assert os.path.exists(staged_p["staged_filepath"])

        p_res = svc2.dismiss_quarantine_batch(
            [staged_p["job_id"]], purge=True, confirm_purge=True
        )
        assert p_res[0]["status"] == "DISMISSED"
        assert p_res[0]["action"] == "purged"
        assert not os.path.exists(staged_p["staged_filepath"])
        assert not purge_doc.exists()


def test_tui_vault_statuses_settings_persistence_and_reinspect_runtime_settings(
    tmp_path, isolated_env, isolated_db, monkeypatch
):
    """QuarantineVaultModal exposes all 10 VALID_QUARANTINE_STATES, SettingsModal persists QUARANTINE_MODE, and TUI/CLI re-inspection uses updated runtime_settings."""
    settings_file = tmp_path / "settings.json"
    ws = tmp_path / "tui_ws"
    ws.mkdir()

    # Stage and hold a document in strict mode first
    strict_cfg = AppSettings(filepath=str(settings_file))
    strict_cfg.AI_CONSENT_GRANTED = True
    strict_cfg.QUARANTINE_MODE = "strict"
    strict_cfg.AUTO_QUARANTINE_RATINGS = ["CRITICAL", "HIGH"]
    strict_cfg._save()

    inv_file = ws / "consulting_invoice_99.txt"
    inv_file.write_text(
        "Invoice #99 Consulting Billing Statement Amount Due $4,500",
        encoding="utf-8",
    )
    strict_svc = QuarantineInterceptorService(
        db=isolated_db, runtime_settings=strict_cfg
    )
    staged = strict_svc.stage_incoming_file(str(inv_file), str(ws))
    inv_file.unlink()
    held_rec = strict_svc.process_quarantine_job(staged["job_id"])
    assert held_rec["status"] == "QUARANTINED"

    async def _exercise_tui():
        app = AutoSorterTUI(settings=strict_cfg, base_dir=str(ws), skip_wizard=True)
        async with app.run_test(size=(115, 55)) as pilot:
            # 1. Open SettingsModal, verify QUARANTINE_MODE select, switch to personal mode & save
            app.action_open_settings()
            await pilot.pause(0.1)
            s_modal = app.screen
            assert isinstance(s_modal, SettingsModal)
            assert inspect_tui_component(s_modal) == []

            sel_mode = s_modal.query_one("#select-quarantine-mode", Select)
            assert sel_mode.value == "strict"
            sel_mode.value = "personal"
            sw_crit = s_modal.query_one("#switch-quarantine-critical", Switch)
            sw_high = s_modal.query_one("#switch-quarantine-high", Switch)
            sw_crit.value = False
            sw_high.value = False
            inp_qdir = s_modal.query_one("#input-quarantine-dir", Input)
            inp_qdir.value = "_Vault_Compliance"
            s_modal.action_save()
            await pilot.pause(0.1)
            await app.workers.wait_for_complete()

            assert strict_cfg.QUARANTINE_MODE == "personal"
            assert strict_cfg.AUTO_QUARANTINE_RATINGS == []
            assert strict_cfg.QUARANTINE_DIR_NAME == "_Vault_Compliance"

            # Apply starter template rule and re-inspect in QuarantineVaultModal
            strict_cfg.KEYWORD_RULES = {"invoice": "Financial/Invoices"}

            vault_modal = QuarantineVaultModal(
                db=isolated_db,
                base_dir=str(ws),
                runtime_settings=strict_cfg,
            )
            await app.push_screen(vault_modal)
            await pilot.pause(0.1)
            await app.workers.wait_for_complete()

            status_select = vault_modal.query_one("#quarantine-status-filter", Select)
            option_values = {str(val) for _, val in status_select._options}
            assert VALID_QUARANTINE_STATES.issubset(option_values)
            assert len(VALID_QUARANTINE_STATES) == 10

            vault_modal.execute_reinspect_async(staged["job_id"])
            await app.workers.wait_for_complete()
            await pilot.pause(0.1)

    asyncio.run(_exercise_tui())

    persisted_json = json.loads(settings_file.read_text(encoding="utf-8"))
    assert persisted_json["QUARANTINE_MODE"] == "personal"
    assert persisted_json["AUTO_QUARANTINE_RATINGS"] == []
    assert persisted_json["QUARANTINE_DIR_NAME"] == "_Vault_Compliance"

    updated_rec = isolated_db.get_quarantine_record(staged["job_id"])
    assert updated_rec["status"] == "RELEASED"
    assert (ws / "Financial" / "Invoices" / "consulting_invoice_99.txt").exists()

    # Verify CLI `quarantine process` also passes active runtime_settings when re-inspecting
    cli_doc = ws / "tax_1099_cli.txt"
    cli_doc.write_text("Form 1099 Tax Statement 2025", encoding="utf-8")
    strict_only = Settings(QUARANTINE_MODE="strict")
    cli_staged = QuarantineInterceptorService(
        db=isolated_db, runtime_settings=strict_only
    ).stage_incoming_file(str(cli_doc), str(ws))
    cli_doc.unlink()
    QuarantineInterceptorService(
        db=isolated_db, runtime_settings=strict_only
    ).process_quarantine_job(cli_staged["job_id"])
    assert (
        isolated_db.get_quarantine_record(cli_staged["job_id"])["status"]
        == "QUARANTINED"
    )

    personal_cli_settings = Settings(
        QUARANTINE_MODE="personal",
        KEYWORD_RULES={"1099": "Taxes/1099"},
    )
    cli_args = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="process",
        job_ids_pos=[cli_staged["job_id"]],
        job_id=None,
        job_ids=None,
        timeout=30.0,
        db_path=str(isolated_db.db_path),
        json=True,
        quiet=True,
    )
    out_buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", out_buf)
    with pytest.raises(SystemExit) as exc_info:
        handle_quarantine_command(cli_args, personal_cli_settings)
    assert exc_info.value.code == 0
    payload = json.loads(out_buf.getvalue())
    assert payload["status"] == "success"
    assert (ws / "Taxes" / "1099" / "tax_1099_cli.txt").exists()
