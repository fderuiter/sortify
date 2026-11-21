"""Tests for opt-in compliance quarantine, token-boundary classification, full-path release, and content-hash exemptions."""

import asyncio
import contextlib
import json
import os

import pytest
from textual.widgets import Input, Switch

from app.cli.quarantine_cli import handle_quarantine_command
from app.config import AppSettings, Settings
from app.core.daemon import ContinuousWatchdogDaemon
from app.core.db import Database
from app.core.db_conn import clear_connection_cache
from app.core.db_worker import DBWorker
from app.core.jev_classifier import JevClassifierEngine
from app.core.mover import AsyncMoveEngine
from app.core.pipeline import SortPipeline
from app.core.quarantine_interceptor import QuarantineInterceptorService
from app.core.rule_templates import apply_starter_templates
from app.core.scanner import get_files_recursively
from app.core.session import AppSession
from app.ui.a11y import inspect_tui_component
from app.ui.modals.settings import SettingsModal
from app.ui.modals.wizard import WizardModal
from app.ui.tui import AutoSorterTUI


@pytest.fixture
def isolated_db(tmp_path):
    """Provide an isolated SQLite database and worker for test execution."""
    worker = DBWorker()
    db = Database(tmp_path / "test_compliance.db", worker)
    try:
        yield db
    finally:
        worker.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_personal_sort_default_config_and_starter_packs_zero_quarantine(
    tmp_path, monkeypatch
):
    """Fresh default config with starter packs sorts everyday sensitive & technical files without quarantining any."""
    app_dir = tmp_path / "app_data"
    app_dir.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    AppSettings._instance = None

    ws = tmp_path / "personal_workspace"
    ws.mkdir()

    files = {
        "invoice_2026_001.txt": "Invoice #2026-001 Total Due $450.00 Electric Utility Billing Statement",
        "tax_return_2025.txt": "2025 Individual Income Tax Return Form 1040 W-2 Summary",
        "nda_consulting_agreement.txt": "Mutual Non-Disclosure Agreement and Consulting Contract Terms",
        "clinical_lab_report.txt": "Patient Annual Physical Lab Report Blood Work Cholesterol Panel",
        "jane_doe_cv_resume.txt": "Jane Doe Curriculum Vitae Resume Work Experience Software Engineer",
        "opencv_tutorial_notes.txt": "OpenCV Python tutorial notes on Canny edge detection and Gaussian blur.",
    }
    for name, body in files.items():
        (ws / name).write_text(body, encoding="utf-8")

    settings = Settings()
    assert settings.AUTO_QUARANTINE_RATINGS == []
    apply_starter_templates(
        settings,
        [
            "financial_tax",
            "personal_admin",
            "legal_contracts",
            "medical_records",
            "developer_workspace",
        ],
    )

    classifier = JevClassifierEngine()
    opencv_res = classifier.classify(str(ws / "opencv_tutorial_notes.txt"))
    assert opencv_res.sensitivity_rating == "LOW"

    with contextlib.closing(AppSession(settings, base_dir=str(ws))) as session:
        pipeline = SortPipeline(session)
        plan = pipeline.plan()
        assert "_Compliance_Quarantine" not in plan
        result = pipeline.execute(plan)

    assert result.ok
    assert result.quarantined == []
    assert not (ws / "_Compliance_Quarantine").exists()
    assert not (ws / "_Quarantine_Staging").exists()
    assert len(result.moved) == len(files)
    clear_connection_cache(only_current_and_inactive=False)


def test_existing_settings_json_preserves_custom_auto_quarantine_ratings(tmp_path):
    """Upgrading with an existing settings.json keeps custom AUTO_QUARANTINE_RATINGS intact."""
    AppSettings._instance = None
    cfg_path = tmp_path / "settings.json"
    cfg_path.write_text(
        json.dumps(
            {
                "AUTO_QUARANTINE_RATINGS": ["CRITICAL", "HIGH"],
                "QUARANTINE_DIR_NAME": "_Vault_Hold",
            }
        ),
        encoding="utf-8",
    )

    try:
        loaded = AppSettings(filepath=str(cfg_path))
        assert loaded.AUTO_QUARANTINE_RATINGS == ["CRITICAL", "HIGH"]
        assert loaded.QUARANTINE_DIR_NAME == "_Vault_Hold"
    finally:
        AppSettings._instance = None


def test_jev_classifier_word_and_token_boundaries(tmp_path):
    """Partial substrings inside technical identifiers do not trigger false HIGH/CRITICAL ratings."""
    engine = JevClassifierEngine()

    false_positive_cases = {
        "opencv_tutorial_notes.txt": "Installing opencv contrib modules for image capture",
        "standard_operating_notes.txt": "Standard markdown formatting conventions for monday releases",
        "syntax_highlighting_guide.txt": "Tree-sitter syntax and taxonomy overview",
        "unicode_normalization_notes.txt": "NFC unicode string handling in terminal widgets",
    }
    for fname, content in false_positive_cases.items():
        fpath = tmp_path / fname
        fpath.write_text(content, encoding="utf-8")
        res = engine.classify(str(fpath))
        assert res.sensitivity_rating == "LOW", (
            f"Expected LOW for {fname}, got {res.sensitivity_rating} ({res.category})"
        )

    true_positive_cases = {
        "john_cv_2026.txt": ("HIGH", "Curriculum Vitae John Smith"),
        "vendor-nda-draft.docx.txt": (
            "HIGH",
            "Mutual NDA and confidentiality contract",
        ),
        "tax_2025_summary.txt": ("HIGH", "Annual federal taxes and W-2 statement"),
        "invoice_0042.txt": ("HIGH", "Invoice_0042 billing receipt for consulting"),
        "patient_clinical_diagnosis.txt": (
            "CRITICAL",
            "Patient clinical diagnosis and medical prescription",
        ),
    }
    for fname, (expected_rating, content) in true_positive_cases.items():
        fpath = tmp_path / fname
        fpath.write_text(content, encoding="utf-8")
        res = engine.classify(str(fpath))
        assert res.sensitivity_rating == expected_rating, (
            f"Expected {expected_rating} for {fname}, got {res.sensitivity_rating}"
        )


def test_wizard_and_settings_modals_toggle_auto_quarantine(tmp_path):
    """WizardModal and SettingsModal expose accessible HIGH/CRITICAL quarantine switches and persist changes."""
    AppSettings._instance = None
    settings_path = tmp_path / "settings.json"
    ws = tmp_path / "ws"
    ws.mkdir()

    try:
        settings = AppSettings(filepath=str(settings_path))
        assert settings.AUTO_QUARANTINE_RATINGS == []

        async def _exercise_wizard():
            app = AutoSorterTUI(settings=settings, base_dir=ws, skip_wizard=True)
            async with app.run_test(size=(110, 55)) as pilot:
                wiz = WizardModal(settings, current_dir=str(ws))
                app.push_screen(wiz)
                await pilot.pause(0.1)

                a11y_issues = inspect_tui_component(wiz)
                assert a11y_issues == []

                sw_crit = wiz.query_one("#switch-wizard-quarantine-critical", Switch)
                sw_high = wiz.query_one("#switch-wizard-quarantine-high", Switch)
                assert sw_crit.value is False
                assert sw_high.value is False

                sw_crit.value = True
                sw_high.value = True
                await pilot.pause(0.05)

                wiz.action_finish()
                await pilot.pause(0.1)

        asyncio.run(_exercise_wizard())
        assert settings.AUTO_QUARANTINE_RATINGS == ["CRITICAL", "HIGH"]

        async def _exercise_settings():
            app = AutoSorterTUI(settings=settings, base_dir=ws, skip_wizard=True)
            async with app.run_test(size=(110, 55)) as pilot:
                app.action_open_settings()
                await pilot.pause(0.1)
                modal = app.screen
                assert isinstance(modal, SettingsModal)

                a11y_issues = inspect_tui_component(modal)
                assert a11y_issues == []

                sw_crit = modal.query_one("#switch-quarantine-critical", Switch)
                sw_high = modal.query_one("#switch-quarantine-high", Switch)
                inp_qdir = modal.query_one("#input-quarantine-dir", Input)

                assert sw_crit.value is True
                assert sw_high.value is True
                assert inp_qdir.value == "_Compliance_Quarantine"

                # Invalid traversal folder name is rejected without closing modal
                inp_qdir.value = "../escape"
                modal.action_save()
                await pilot.pause(0.05)
                assert app.screen is modal

                # Valid custom folder name and CRITICAL-only toggle save cleanly
                sw_high.value = False
                inp_qdir.value = "_Custom_Compliance_Hold"
                modal.action_save()
                await pilot.pause(0.1)

        asyncio.run(_exercise_settings())
        assert settings.AUTO_QUARANTINE_RATINGS == ["CRITICAL"]
        assert settings.QUARANTINE_DIR_NAME == "_Custom_Compliance_Hold"

        persisted = json.loads(settings_path.read_text(encoding="utf-8"))
        assert persisted["AUTO_QUARANTINE_RATINGS"] == ["CRITICAL"]
        assert persisted["QUARANTINE_DIR_NAME"] == "_Custom_Compliance_Hold"
    finally:
        AppSettings._instance = None
        clear_connection_cache(only_current_and_inactive=False)


def test_plan_preview_displays_quarantine_folder_and_subsequent_scans_skip_it(
    tmp_path, monkeypatch
):
    """Enabling AUTO_QUARANTINE_RATINGS surfaces _Compliance_Quarantine in plan preview and subsequent scans ignore it."""
    app_dir = tmp_path / "app_data"
    app_dir.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    AppSettings._instance = None

    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "invoice_confidential.txt").write_text(
        "Invoice #8821 Billing Statement Balance Due $9,200.00", encoding="utf-8"
    )
    (ws / "project_readme.txt").write_text(
        "Project build instructions and configuration readme", encoding="utf-8"
    )

    settings = Settings()
    settings.AUTO_QUARANTINE_RATINGS = ["CRITICAL", "HIGH"]
    settings.KEYWORD_RULES = {"invoice": "Invoices", "readme": "Documentation"}

    with contextlib.closing(AppSession(settings, base_dir=str(ws))) as session:
        pipeline = SortPipeline(session)
        plan = pipeline.plan()

        # Requirement 4: Plan preview shows _Compliance_Quarantine BEFORE any move executes
        assert "_Compliance_Quarantine" in plan
        assert "invoice_confidential.txt" in plan["_Compliance_Quarantine"]
        assert "Invoices" not in plan
        assert not (ws / "_Compliance_Quarantine").exists()

        res1 = pipeline.execute(plan)
        assert res1.ok
        assert res1.quarantined == ["invoice_confidential.txt"]
        assert (ws / "_Compliance_Quarantine" / "invoice_confidential.txt").exists()

        # Requirement 5: Directory scanner and subsequent sort skip _Compliance_Quarantine
        scanned = get_files_recursively(str(ws))
        assert not any("_Compliance_Quarantine" in p for p in scanned)

        plan2 = pipeline.plan()
        assert "_Compliance_Quarantine" not in plan2

    daemon = ContinuousWatchdogDaemon(settings, str(ws))
    assert daemon.should_ignore_path(
        str(ws / "_Compliance_Quarantine" / "invoice_confidential.txt")
    )
    assert daemon.should_ignore_path(str(ws / "_Quarantine_Staging" / "staged.txt"))
    clear_connection_cache(only_current_and_inactive=False)


def test_batch_release_resolves_full_workspace_paths_from_any_cwd(
    tmp_path, isolated_db, monkeypatch
):
    """Batch release succeeds when os.getcwd() is an unrelated directory for both mover and legacy relative records."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    other_cwd = tmp_path / "unrelated_cwd"
    other_cwd.mkdir()

    med_file = ws / "patient_chart.txt"
    med_file.write_text(
        "Patient Medical Record Clinical Diagnosis Prescription", encoding="utf-8"
    )

    settings = Settings()
    settings.AUTO_QUARANTINE_RATINGS = ["CRITICAL", "HIGH"]

    plan = {
        "Medical": {
            "patient_chart.txt": {
                "__type__": "file",
                "relative_source": "patient_chart.txt",
                "sensitivity_rating": "CRITICAL",
                "sensitivity_score": 0.95,
            }
        }
    }

    # Execute mover while CWD is tmp_path, passing a relative base_dir to verify normalization
    monkeypatch.chdir(tmp_path)
    engine = AsyncMoveEngine()
    engine.execute(
        base_dir="workspace",
        plan=plan,
        db=isolated_db,
        history_manager=None,
        runtime_settings=settings,
    )

    records = isolated_db.get_all_quarantine_records()
    assert len(records) == 1
    mover_job_id = records[0]["job_id"]
    assert os.path.isabs(records[0]["base_dir"])
    assert os.path.isabs(records[0]["staged_filepath"])
    assert records[0]["file_hash"]

    # Also insert a legacy relative staged_filepath record to verify backward-compatible resolution
    legacy_file = ws / "_Compliance_Quarantine" / "legacy_held.txt"
    legacy_file.write_text("Legacy held tax statement", encoding="utf-8")
    isolated_db.stage_quarantine_record(
        job_id="qjob_legacy_rel",
        base_dir=str(ws),
        original_filepath="legacy_held.txt",
        staged_filepath="_Compliance_Quarantine/legacy_held.txt",
        policy_action="sensitivity_hold",
    )
    isolated_db.update_quarantine_status(
        job_id="qjob_legacy_rel", status="IN_INSPECTION"
    )
    isolated_db.update_quarantine_status(
        job_id="qjob_legacy_rel",
        status="QUARANTINED",
        policy_action="sensitivity_hold",
    )

    # Switch to a completely unrelated working directory before releasing
    monkeypatch.chdir(other_cwd)
    service = QuarantineInterceptorService(db=isolated_db, runtime_settings=settings)
    release_results = service.release_quarantine_batch(
        [mover_job_id, "qjob_legacy_rel"]
    )

    assert [r["status"] for r in release_results] == ["RELEASED", "RELEASED"]
    assert (ws / "patient_chart.txt").exists()
    assert (ws / "legacy_held.txt").exists()

    # Verify CLI `quarantine release` subcommand also resolves full workspace paths from other_cwd
    cli_doc = ws / "cli_patient_chart.txt"
    cli_doc.write_text(
        "Confidential Clinical Patient Diagnosis Chart #902", encoding="utf-8"
    )
    cli_staged = service.stage_incoming_file(str(cli_doc), str(ws))
    cli_job_id = cli_staged["job_id"]
    cli_doc.unlink()
    service.process_quarantine_job(cli_job_id)
    assert isolated_db.get_quarantine_record(cli_job_id)["status"] == "QUARANTINED"

    import argparse
    import io

    cli_args = argparse.Namespace(
        subcommand="quarantine",
        quarantine_command="release",
        job_ids_pos=[cli_job_id],
        job_id=None,
        job_ids=None,
        dest_dir=None,
        conflict_policy="rename",
        allow_external_dest=False,
        db_path=str(isolated_db.db_path),
        json=True,
        quiet=True,
    )
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)
    with pytest.raises(SystemExit) as exc_info:
        handle_quarantine_command(cli_args, settings)
    assert exc_info.value.code == 0
    payload = json.loads(buf.getvalue())
    assert payload["status"] == "success"
    assert cli_doc.exists()


def test_release_records_content_hash_exemption_for_mover_and_reevaluates_on_edit(
    tmp_path, monkeypatch
):
    """Released unchanged files sort normally without repeat quarantine; editing the file triggers re-evaluation."""
    app_dir = tmp_path / "app_data"
    app_dir.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    AppSettings._instance = None

    ws = tmp_path / "workspace"
    ws.mkdir()
    doc_path = ws / "invoice_vip.txt"
    doc_path.write_text(
        "Invoice #5500 Confidential Billing Statement Total $12,000", encoding="utf-8"
    )

    settings = Settings()
    settings.AUTO_QUARANTINE_RATINGS = ["CRITICAL", "HIGH"]
    settings.KEYWORD_RULES = {"invoice": "Invoices"}

    with contextlib.closing(AppSession(settings, base_dir=str(ws))) as session:
        pipeline = SortPipeline(session)
        plan1 = pipeline.plan()
        assert "_Compliance_Quarantine" in plan1
        res1 = pipeline.execute(plan1)
        assert res1.quarantined == ["invoice_vip.txt"]

        records = session.db.get_quarantine_records_by_base_dir(
            str(ws), status="QUARANTINED"
        )
        assert len(records) == 1
        service = QuarantineInterceptorService(db=session.db, runtime_settings=settings)
        rel_res = service.release_quarantine_batch([records[0]["job_id"]])
        assert rel_res[0]["status"] == "RELEASED"
        assert doc_path.exists()

        # Re-sorting the UNCHANGED released file routes to 'Invoices' instead of re-quarantining!
        plan2 = pipeline.plan()
        assert "_Compliance_Quarantine" not in plan2
        assert "Invoices" in plan2
        res2 = pipeline.execute(plan2)
        assert res2.quarantined == []
        sorted_invoice = ws / "Invoices" / "invoice_vip.txt"
        assert sorted_invoice.exists()

        # Modifying the released file invalidates its content-hash approval exemption!
        moved_back = ws / "invoice_vip.txt"
        moved_back.write_text(
            "Invoice #5500 UPDATED Balance $19,500 New Line Item Added",
            encoding="utf-8",
        )
        sorted_invoice.unlink()

        plan3 = pipeline.plan()
        assert "_Compliance_Quarantine" in plan3
        res3 = pipeline.execute(plan3)
        assert res3.quarantined == ["invoice_vip.txt"]
        assert (ws / "_Compliance_Quarantine" / "invoice_vip.txt").exists()

    clear_connection_cache(only_current_and_inactive=False)


@pytest.mark.anyio
async def test_daemon_skips_unchanged_released_file_and_reholds_when_modified(
    tmp_path, monkeypatch
):
    """ContinuousWatchdogDaemon does not re-quarantine unchanged released files, but re-evaluates modified content."""
    app_dir = tmp_path / "app_data"
    app_dir.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))
    ws = tmp_path / "watched_ws"
    ws.mkdir()

    chart = ws / "patient_lab_report.txt"
    chart.write_text(
        "Patient Clinical Lab Report Blood Work Diagnosis Confidential",
        encoding="utf-8",
    )

    settings = Settings()
    settings.AUTO_QUARANTINE_RATINGS = ["CRITICAL", "HIGH"]
    daemon = ContinuousWatchdogDaemon(settings, str(ws))
    daemon._is_running = True

    try:
        # 1. Initial arrival holds the file in quarantine
        await daemon._triage_file_path(str(chart))
        assert not chart.exists()

        session = daemon._get_or_create_session()
        q_recs = session.db.get_quarantine_records_by_base_dir(
            str(ws), status="QUARANTINED"
        )
        assert len(q_recs) == 1
        first_job_id = q_recs[0]["job_id"]

        # 2. User approves and releases the file back to the watched workspace
        service = QuarantineInterceptorService(db=session.db, runtime_settings=settings)
        rel = service.release_quarantine_batch([first_job_id], dest_dir=str(ws))
        assert rel[0]["status"] == "RELEASED"
        assert chart.exists()

        # 3. Watchdog triage of the unchanged released file does NOT trap it in quarantine again
        await daemon._triage_file_path(str(chart))
        active_holds = session.db.get_quarantine_records_by_base_dir(
            str(ws), status="QUARANTINED"
        )
        assert active_holds == []

        # 4. User modifies the released file -> compliance scanner evaluates updated content and holds it
        edited_chart = ws / "patient_lab_report_updated.txt"
        edited_chart.write_text(
            "Patient Clinical Lab Report UPDATED Critical Pathology Findings",
            encoding="utf-8",
        )
        await daemon._triage_file_path(str(edited_chart))
        active_holds_after_edit = session.db.get_quarantine_records_by_base_dir(
            str(ws), status="QUARANTINED"
        )
        assert len(active_holds_after_edit) == 1
        assert active_holds_after_edit[0]["policy_action"] == "sensitivity_hold"
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_explicit_compliance_policies_still_run_when_auto_quarantine_disabled(
    tmp_path, isolated_db
):
    """Explicit quarantine, redact, and archive policies continue executing when AUTO_QUARANTINE_RATINGS is empty."""
    ws = tmp_path / "policy_ws"
    ws.mkdir()

    q_file = ws / "secret_key_dump.txt"
    q_file.write_text("TOP_SECRET production token dump", encoding="utf-8")

    r_file = ws / "customer_note.txt"
    r_file.write_text(
        "Customer SSN 123-45-6789 contact alice@example.com", encoding="utf-8"
    )

    a_file = ws / "legacy_ledger.txt"
    a_file.write_text("FY2020 closed ledger snapshot", encoding="utf-8")

    settings = Settings()
    assert settings.AUTO_QUARANTINE_RATINGS == []
    policies = [
        {
            "name": "Hold Top Secret",
            "type": "keyword",
            "expression": "top_secret",
            "action": "quarantine",
            "target_path": "_Compliance_Quarantine",
            "priority": 1,
        },
        {
            "name": "Redact Customer Notes",
            "type": "keyword",
            "expression": "customer ssn",
            "action": "redact",
            "target_path": "Sanitized",
            "priority": 2,
        },
        {
            "name": "Archive FY2020",
            "type": "keyword",
            "expression": "fy2020",
            "action": "archive",
            "target_path": "Archived_2020",
            "priority": 3,
        },
    ]

    service = QuarantineInterceptorService(
        db=isolated_db, policies=policies, runtime_settings=settings
    )

    j_q = service.stage_incoming_file(str(q_file), str(ws))["job_id"]
    res_q = service.process_quarantine_job(j_q)
    assert res_q["status"] == "QUARANTINED"
    assert res_q["policy_action"] == "quarantine"

    j_r = service.stage_incoming_file(str(r_file), str(ws))["job_id"]
    res_r = service.process_quarantine_job(j_r)
    assert res_r["status"] == "RELEASED"
    assert res_r["policy_action"] == "redact"
    sanitized_text = (ws / "Sanitized" / "customer_note.txt").read_text(
        encoding="utf-8"
    )
    assert "123-45-6789" not in sanitized_text

    j_a = service.stage_incoming_file(str(a_file), str(ws))["job_id"]
    res_a = service.process_quarantine_job(j_a)
    assert res_a["status"] == "ARCHIVED"
    assert (ws / "Archived_2020" / "legacy_ledger.txt").exists()


def test_tui_partial_settings_without_consent_attr_does_not_schedule_deferred_wizard(
    tmp_path, monkeypatch
):
    """Partial settings stubs without AI_CONSENT_GRANTED do not push WizardModal on deferred frame refreshes."""
    from unittest.mock import patch

    import textual.timer as ttimer

    app_dir = tmp_path / "app_data"
    app_dir.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    class PartialSettingsStub:
        def __init__(self):
            self.language = "en"
            self.ai_status_badge = "ON"

    orig_sleep = ttimer.sleep

    async def delayed_frame_sleep(delay):
        if delay < 0.1:
            delay = 0.25
        return await orig_sleep(delay)

    async def _run_partial():
        app = AutoSorterTUI(settings=PartialSettingsStub(), base_dir=str(tmp_path))
        pushed = []
        orig_push = app.push_screen

        def spy_push(screen, callback=None, *args, **kwargs):
            pushed.append(screen)
            return orig_push(screen, callback, *args, **kwargs)

        with patch.object(ttimer, "sleep", delayed_frame_sleep):
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen = spy_push
                await pilot.pause()
                assert not any(isinstance(s, WizardModal) for s in pushed)
                assert not isinstance(app.screen, WizardModal)

    asyncio.run(_run_partial())
