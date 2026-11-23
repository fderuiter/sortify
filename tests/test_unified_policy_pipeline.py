"""End-to-end tests for unified policy actions across SortPipeline, MoveLedger, and ContinuousWatchdogDaemon."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import zipfile
from pathlib import Path

import docx
import openpyxl
import pypdf
import pytest
from openpyxl.comments import Comment

from app.config import AppSettings
from app.core.daemon import ContinuousWatchdogDaemon, FileChangeEvent
from app.core.db_conn import clear_connection_cache
from app.core.metadata import MetadataPass
from app.core.moves import VERIFIED
from app.core.pipeline import (
    IllegalStateTransitionError,
    PipelineLifecycleState,
    SortPipeline,
)
from app.core.plan_format import format_plan_tree
from app.core.quarantine_interceptor import QuarantineInterceptorService
from app.core.rule_templates import apply_starter_templates
from app.core.session import AppSession, split_plan_phases
from app.core.store import open_store


def _make_docx(
    path: Path, body_text: str, *, table_text: str = "", header_text: str = ""
) -> None:
    doc = docx.Document()
    doc.core_properties.author = "ssn 123-45-6789"
    doc.core_properties.comments = "API_KEY = 'super_secret_header_token_12345'"
    p = doc.add_paragraph()
    # Exercise split-run token scrubbing as well as single-run scrubbing
    p.add_run("Subject record: ")
    p.add_run(body_text)
    if table_text:
        tbl = doc.add_table(rows=1, cols=1)
        tbl.cell(0, 0).text = table_text
    if header_text:
        doc.sections[0].header.paragraphs[0].text = header_text
    doc.save(str(path))


def _make_xlsx(path: Path, cell_text: str, *, comment_text: str = "") -> None:
    wb = openpyxl.Workbook()
    wb.properties.creator = "patient@example.com"
    ws = wb.active
    assert ws is not None
    ws.title = "Payroll"
    ws["A1"] = "Employee Notes"
    ws["B1"] = cell_text
    if comment_text:
        ws["B1"].comment = Comment(comment_text, "auditor@example.com")
    wb.save(str(path))
    wb.close()


def _make_valid_pdf(path: Path) -> None:
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_metadata({"/Title": "Confidential Contract SSN 123-45-6789"})
    with open(path, "wb") as fh:
        writer.write(fh)


def _new_settings(tmp_path: Path) -> AppSettings:
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.STOP_WORDS = set()
    settings.CONFLICT_POLICY = "rename"
    return settings


def test_sort_pipeline_unified_policy_actions_and_full_undo(tmp_path: Path) -> None:
    """Criterion 1: SortPipeline.run unpacks & sorts .zip children, scrubs .txt/.docx, holds .pdf & quarantine, and passes undo."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    store = open_store(tmp_path / "store.db")

    # 1. Zip bundle containing two sortable child documents
    zip_path = ws / "incoming_bundle.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("invoice_q4.txt", "Vendor invoice total $4,200 due net 30")
        zf.writestr("nda_partner.txt", "Mutual NDA legal contract terms")
    orig_zip_bytes = zip_path.read_bytes()

    # 2. Sensitive .txt for redaction
    txt_path = ws / "patient_intake.txt"
    txt_path.write_text(
        "Patient intake SSN 123-45-6789 email jane.doe@example.com API_KEY='very_secret_value_987654'",
        encoding="utf-8",
    )
    orig_txt_bytes = txt_path.read_bytes()

    # 3. Sensitive .docx for redaction
    docx_path = ws / "patient_summary.docx"
    _make_docx(
        docx_path,
        "SSN 123-45-6789 and email john.roe@example.com",
        table_text="Token sk-abcdefghijklmnopqrstuvwxyz123456",
        header_text="Contact: ops@example.com",
    )
    orig_docx_bytes = docx_path.read_bytes()

    # 4. Sensitive .pdf under redact policy -> held intact in _Compliance_Quarantine
    pdf_path = ws / "patient_scan.pdf"
    _make_valid_pdf(pdf_path)
    orig_pdf_bytes = pdf_path.read_bytes()

    # 5. Explicit quarantine fixture
    quar_path = ws / "breach_dump.txt"
    quar_path.write_text("Quarantine hold incident dump", encoding="utf-8")
    orig_quar_bytes = quar_path.read_bytes()

    settings = _new_settings(tmp_path)
    settings.KEYWORD_RULES = {
        "invoice": "Finance/Invoices",
        "nda": "Legal/Contracts",
    }
    settings.POLICIES = [
        {
            "type": "unarchive",
            "expression": "*.zip",
            "action": "unarchive",
            "post_unarchive_action": "delete",
            "target_path": "",
            "priority": 100,
        },
        {
            "type": "keyword",
            "expression": "patient_",
            "action": "redact",
            "target_path": "Sanitized_Records",
            "priority": 95,
        },
        {
            "type": "keyword",
            "expression": "breach_dump",
            "action": "quarantine",
            "target_path": "_Compliance_Quarantine",
            "priority": 99,
        },
    ]

    try:
        session = AppSession(settings, base_dir=str(ws), store=store)
        try:
            pipeline = SortPipeline(session)
            plan, result = pipeline.run()
            assert result is not None
            assert result.ok
            assert result.state == PipelineLifecycleState.COMPLETED

            # CLI tree preview shows policy action badges
            tree_lines, _, _ = format_plan_tree(plan)
            tree_str = "\n".join(tree_lines)
            assert "[ACTION: unarchive]" in tree_str
            assert "[ACTION: redact]" in tree_str
            assert "[ACTION: quarantine]" in tree_str

            # split_plan_phases routes policy actions into Phase 1 fast_plan
            fast_plan, _slow_plan = split_plan_phases(plan)
            assert "Sanitized_Records" in fast_plan
            assert "_Compliance_Quarantine" in fast_plan

            # Zip unpacked & deleted from workspace root, children sorted to target folders
            assert not zip_path.exists()
            assert (ws / "Finance" / "Invoices" / "invoice_q4.txt").is_file()
            assert (ws / "Legal" / "Contracts" / "nda_partner.txt").is_file()

            # .txt scrubbed in Sanitized_Records
            out_txt = ws / "Sanitized_Records" / "patient_intake.txt"
            assert out_txt.is_file()
            scrubbed_txt = out_txt.read_text(encoding="utf-8")
            assert "123-45-6789" not in scrubbed_txt
            assert "jane.doe@example.com" not in scrubbed_txt
            assert "very_secret_value_987654" not in scrubbed_txt
            assert "[REDACTED" in scrubbed_txt

            # .docx scrubbed in Sanitized_Records and opens cleanly in python-docx
            out_docx = ws / "Sanitized_Records" / "patient_summary.docx"
            assert out_docx.is_file()
            parsed_docx = docx.Document(str(out_docx))
            full_docx_text = "\n".join(p.text for p in parsed_docx.paragraphs)
            assert "123-45-6789" not in full_docx_text
            assert "john.roe@example.com" not in full_docx_text
            assert (
                "sk-abcdefghijklmnopqrstuvwxyz123456"
                not in parsed_docx.tables[0].cell(0, 0).text
            )
            assert (
                "ops@example.com"
                not in parsed_docx.sections[0].header.paragraphs[0].text
            )

            # .pdf under redact is never corrupted or published to Sanitized_Records; held intact in _Compliance_Quarantine
            assert not (ws / "Sanitized_Records" / "patient_scan.pdf").exists()
            quar_pdf = ws / "_Compliance_Quarantine" / "patient_scan.pdf"
            assert quar_pdf.is_file()
            reader = pypdf.PdfReader(str(quar_pdf))
            assert len(reader.pages) == 1

            # Explicit quarantine fixture moved to _Compliance_Quarantine
            quar_dump = ws / "_Compliance_Quarantine" / "breach_dump.txt"
            assert quar_dump.is_file()

            # Quarantine records recorded for unarchive, redact, and quarantine
            q_records = session.db.get_quarantine_records_by_base_dir(str(ws))
            actions_recorded = {r["policy_action"] for r in q_records}
            assert {"unarchive", "redact", "quarantine"}.issubset(actions_recorded)

            # Full SortPipeline.undo restores workspace to exact initial state
            undo_pipeline = SortPipeline(session)
            undo_result = undo_pipeline.undo(result.session_id)
            assert undo_result.ok
            assert undo_result.rollback_completed is True
            assert undo_result.state == PipelineLifecycleState.ROLLED_BACK

            assert zip_path.read_bytes() == orig_zip_bytes
            assert txt_path.read_bytes() == orig_txt_bytes
            assert docx_path.read_bytes() == orig_docx_bytes
            assert pdf_path.read_bytes() == orig_pdf_bytes
            assert quar_path.read_bytes() == orig_quar_bytes
            assert not (ws / "Finance" / "Invoices" / "invoice_q4.txt").exists()
            assert not (ws / "Legal" / "Contracts" / "nda_partner.txt").exists()
            assert not out_txt.exists()
            assert not out_docx.exists()
            assert not (ws / ".sortify_overwritten" / result.session_id).exists()
        finally:
            session.close()
    finally:
        clear_connection_cache()


@pytest.mark.parametrize("post_action", ["delete", "remove", "cleanup"])
def test_unarchive_backup_moves_and_rollback_restore_zip(
    tmp_path: Path, post_action: str
) -> None:
    """Criterion 2: Unarchived .zip with delete/remove/cleanup records verified MoveLedger backup and restores .zip on undo."""
    ws = tmp_path / f"ws_{post_action}"
    ws.mkdir()
    store = open_store(tmp_path / f"store_{post_action}.db")

    zip_path = ws / "bundle.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("report_a.txt", "Quarterly revenue invoice")
        zf.writestr("sub/notes_b.txt", "Project notes")
    expected_zip_bytes = zip_path.read_bytes()

    settings = _new_settings(tmp_path)
    settings.KEYWORD_RULES = {"invoice": "Invoices"}
    settings.POLICIES = [
        {
            "type": "unarchive",
            "expression": "*.zip",
            "action": "unarchive",
            "post_unarchive_action": post_action,
            "target_path": "Unpacked",
            "priority": 100,
        }
    ]

    try:
        session = AppSession(settings, base_dir=str(ws), store=store)
        try:
            _, res = SortPipeline(session).run()
            assert res is not None and res.ok
            assert not zip_path.exists()
            assert (ws / "Invoices" / "report_a.txt").is_file()
            notes_matches = [
                p
                for p in ws.rglob("notes_b.txt")
                if ".sortify_overwritten" not in p.parts
            ]
            assert len(notes_matches) == 1 and notes_matches[0].is_file()

            ledger_moves = session.history_manager.moves.list_moves(res.session_id)
            zip_backup_moves = [
                m
                for m in ledger_moves
                if os.path.abspath(m.src) == os.path.abspath(zip_path)
            ]
            assert len(zip_backup_moves) == 1
            assert zip_backup_moves[0].state == VERIFIED
            assert ".sortify_overwritten" in zip_backup_moves[0].dst
            assert os.path.isfile(zip_backup_moves[0].dst)

            # _replay_moves_backwards via history_manager.rollback removes unchanged children and restores .zip
            restored = session.history_manager.rollback(res.session_id)
            assert restored >= 1
            assert zip_path.is_file()
            assert zip_path.read_bytes() == expected_zip_bytes
            assert not (ws / "Invoices" / "report_a.txt").exists()
            assert not [
                p
                for p in ws.rglob("notes_b.txt")
                if ".sortify_overwritten" not in p.parts
            ]
        finally:
            session.close()
    finally:
        clear_connection_cache()


def test_unarchive_rollback_preserves_user_modified_child_and_restores_zip(
    tmp_path: Path,
) -> None:
    """Modified extracted child files are never destroyed during undo conflict handling."""
    ws = tmp_path / "ws_conflict"
    ws.mkdir()
    store = open_store(tmp_path / "store_conflict.db")

    zip_path = ws / "docs.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("editable.txt", "Original extracted content")
        zf.writestr("untouched.txt", "Unchanged extracted content")

    settings = _new_settings(tmp_path)
    settings.POLICIES = [
        {
            "type": "unarchive",
            "expression": "*.zip",
            "action": "unarchive",
            "post_unarchive_action": "delete",
            "target_path": "Out",
            "priority": 100,
        }
    ]

    try:
        session = AppSession(settings, base_dir=str(ws), store=store)
        try:
            _, res = SortPipeline(session).run()
            assert res is not None and res.ok

            edited_matches = [
                p
                for p in ws.rglob("editable.txt")
                if ".sortify_overwritten" not in p.parts
            ]
            untouched_matches = [
                p
                for p in ws.rglob("untouched.txt")
                if ".sortify_overwritten" not in p.parts
            ]
            assert len(edited_matches) == 1 and len(untouched_matches) == 1
            edited_child = edited_matches[0]
            untouched_child = untouched_matches[0]
            edited_child.write_text("User edited after sort!", encoding="utf-8")

            undo_res = SortPipeline(session).undo(res.session_id)
            assert not undo_res.ok
            assert undo_res.state == PipelineLifecycleState.FAILED
            # User edit preserved, untouched sibling cleaned up, original zip restored
            assert edited_child.read_text(encoding="utf-8") == "User edited after sort!"
            assert not untouched_child.exists()
            assert zip_path.is_file()
        finally:
            session.close()
    finally:
        clear_connection_cache()


def test_office_and_pdf_redaction_valid_containers_and_vector_purge(
    tmp_path: Path,
) -> None:
    """Criterion 3: Redacted .docx/.xlsx open cleanly in python-docx/openpyxl; .pdf held intact in quarantine opens in pypdf; vectors purged."""
    ws = tmp_path / "ws_office"
    ws.mkdir()
    store = open_store(tmp_path / "store_office.db")

    docx_file = ws / "hr_confidential.docx"
    _make_docx(
        docx_file,
        "Employee SSN 123-45-6789 phone (555) 234-5678",
        table_text="Email hr.lead@example.com",
        header_text="Card 4111-1111-1111-1111",
    )

    xlsx_file = ws / "hr_confidential.xlsx"
    _make_xlsx(
        xlsx_file,
        "SSN 123-45-6789 API_KEY='secret_excel_token_99999'",
        comment_text="Reviewer email audit@example.com",
    )

    pdf_file = ws / "hr_confidential.pdf"
    _make_valid_pdf(pdf_file)

    settings = _new_settings(tmp_path)
    settings.POLICIES = [
        {
            "type": "keyword",
            "expression": "hr_confidential",
            "action": "redact",
            "target_path": "HR_Redacted",
            "priority": 100,
        }
    ]

    try:
        session = AppSession(settings, base_dir=str(ws), store=store)
        try:
            # Seed pre-redaction vector embeddings to verify _purge_document_vector clears them
            session.db.upsert_document_vectors(
                str(ws),
                [
                    ("hr_confidential.docx", [1.0, 2.0, 3.0, 4.0]),
                    ("hr_confidential.xlsx", [5.0, 6.0, 7.0, 8.0]),
                ],
                model_signature="test-model",
            )
            assert session.db.get_document_vectors_count() == 2

            _, res = SortPipeline(session).run()
            assert res is not None and res.ok

            # Cached pre-redaction vectors purged
            assert session.db.get_document_vectors_count() == 0

            # Verify .docx opens cleanly in python-docx and tokens are scrubbed
            redacted_docx = ws / "HR_Redacted" / "hr_confidential.docx"
            assert redacted_docx.is_file()
            doc_loaded = docx.Document(str(redacted_docx))
            para_blob = "\n".join(p.text for p in doc_loaded.paragraphs)
            assert "123-45-6789" not in para_blob
            assert "555" not in para_blob or "[REDACTED_PHONE]" in para_blob
            assert "hr.lead@example.com" not in doc_loaded.tables[0].cell(0, 0).text
            assert "123-45-6789" not in (doc_loaded.core_properties.author or "")
            assert "super_secret_header_token_12345" not in (
                doc_loaded.core_properties.comments or ""
            )

            # Verify .xlsx opens cleanly in openpyxl and tokens are scrubbed
            redacted_xlsx = ws / "HR_Redacted" / "hr_confidential.xlsx"
            assert redacted_xlsx.is_file()
            wb_loaded = openpyxl.load_workbook(str(redacted_xlsx))
            try:
                ws_loaded = wb_loaded["Payroll"]
                cell_b1 = str(ws_loaded["B1"].value)
                assert "123-45-6789" not in cell_b1
                assert "secret_excel_token_99999" not in cell_b1
                assert "[REDACTED" in cell_b1
                assert ws_loaded["B1"].comment is not None
                assert "audit@example.com" not in ws_loaded["B1"].comment.text
                assert "patient@example.com" not in (wb_loaded.properties.creator or "")
            finally:
                wb_loaded.close()

            # Verify .pdf is held intact in _Compliance_Quarantine and opens cleanly in pypdf without PdfStreamError
            assert not (ws / "HR_Redacted" / "hr_confidential.pdf").exists()
            quar_pdf = ws / "_Compliance_Quarantine" / "hr_confidential.pdf"
            assert quar_pdf.is_file()
            pdf_reader = pypdf.PdfReader(str(quar_pdf))
            assert len(pdf_reader.pages) == 1

            # Also verify standalone QuarantineInterceptorService holds .pdf intact under redact
            pdf_direct = ws / "hr_confidential_direct.pdf"
            _make_valid_pdf(pdf_direct)
            interceptor = QuarantineInterceptorService(
                db=session.db, policies=settings.POLICIES, runtime_settings=settings
            )
            staged = interceptor.stage_incoming_file(str(pdf_direct), str(ws))
            q_rec = interceptor.process_quarantine_job(staged["job_id"])
            assert q_rec.status == "QUARANTINED"
            assert q_rec.staged_filepath is not None
            assert pypdf.PdfReader(q_rec.staged_filepath).pages
        finally:
            session.close()
    finally:
        clear_connection_cache()


@pytest.mark.anyio
async def test_daemon_phase0_starter_packs_route_three_files_with_zero_duplicates(
    tmp_path: Path,
) -> None:
    """Criterion 4: Restricting daemon Phase 0 routes 3 starter-pack files to 3 output files with zero _1 copies."""
    ws = tmp_path / "daemon_starter_ws"
    ws.mkdir()

    settings = _new_settings(tmp_path)
    apply_starter_templates(
        settings, ["financial_tax", "medical_health", "legal_contracts"]
    )

    f1 = ws / "q1_tax_filing.txt"
    f1.write_text("2026 confidential tax schedule and deductions", encoding="utf-8")
    f2 = ws / "hospital_visit.txt"
    f2.write_text("Patient insurance claim #88412 summary", encoding="utf-8")
    f3 = ws / "vendor_terms.txt"
    f3.write_text("Signed confidential agreement with supplier", encoding="utf-8")

    try:
        daemon = ContinuousWatchdogDaemon(settings, str(ws))
        daemon._is_running = True
        try:
            for fp in (f1, f2, f3):
                await daemon._triage_file_path(str(fp))

            all_outputs = sorted(
                p.relative_to(ws).as_posix()
                for p in ws.rglob("*")
                if p.is_file()
                and not any(
                    part.startswith(".") or part.startswith("_Quarantine")
                    for part in p.relative_to(ws).parts
                )
            )
            assert len(all_outputs) == 3
            assert all("_1" not in os.path.basename(p) for p in all_outputs)
            assert set(all_outputs) == {
                "Taxes/Confidential/q1_tax_filing.txt",
                "Medical/Insurance_Claims/hospital_visit.txt",
                "Legal/Confidential/vendor_terms.txt",
            }
            assert not (ws / "_Quarantine_Staging").exists() or not os.listdir(
                ws / "_Quarantine_Staging"
            )
        finally:
            daemon.stop()
    finally:
        clear_connection_cache()


def test_daemon_four_workers_concurrent_burst_logs_zero_illegal_state_errors(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Criterion 5: Isolating SortPipeline._state across 4 daemon workers logs zero IllegalStateTransitionError errors."""
    ws = tmp_path / "burst_ws"
    ws.mkdir()

    settings = _new_settings(tmp_path)
    settings.MAX_WORKERS = 4
    settings.DEDUP_WINDOW = 0.01
    settings.RECONCILIATION_INTERVAL = 60.0
    settings.KEYWORD_RULES = {
        "invoice": "Invoices",
        "contract": "Contracts",
        "memo": "Memos",
    }
    settings.POLICIES = [
        {
            "type": "keyword",
            "expression": "redact_me",
            "action": "redact",
            "target_path": "Redacted",
            "priority": 100,
        }
    ]

    caplog.set_level(logging.DEBUG)
    daemon = ContinuousWatchdogDaemon(settings, str(ws))
    try:
        daemon.start()
        created_files: list[Path] = []
        for idx in range(12):
            if idx % 4 == 0:
                p = ws / f"invoice_{idx}.txt"
                p.write_text(f"Invoice #{idx} payment due", encoding="utf-8")
            elif idx % 4 == 1:
                p = ws / f"contract_{idx}.txt"
                p.write_text(f"Contract #{idx} legal clause", encoding="utf-8")
            elif idx % 4 == 2:
                p = ws / f"redact_me_{idx}.txt"
                p.write_text(f"Sensitive SSN 123-45-6789 item {idx}", encoding="utf-8")
            else:
                p = ws / f"memo_{idx}.txt"
                p.write_text(f"Internal team memo {idx}", encoding="utf-8")
            created_files.append(p)
            daemon.enqueue_event(
                FileChangeEvent(
                    event_type="created", file_path=str(p), timestamp=time.time()
                )
            )

        deadline = time.time() + 25.0
        while time.time() < deadline:
            remaining_root = [p for p in created_files if p.exists()]
            if (
                not remaining_root
                and daemon.get_metrics_telemetry()["events_processed"] >= 12
            ):
                break
            time.sleep(0.1)

        assert not [p for p in created_files if p.exists()]
        illegal_logs = [
            rec
            for rec in caplog.records
            if "IllegalStateTransitionError" in rec.getMessage()
            or "Illegal pipeline state transition" in rec.getMessage()
            or (
                rec.exc_info
                and rec.exc_info[0] is not None
                and issubclass(rec.exc_info[0], IllegalStateTransitionError)
            )
        ]
        assert illegal_logs == []
    finally:
        daemon.stop()
        clear_connection_cache()


@pytest.mark.anyio
async def test_daemon_started_outside_base_dir_reconciles_all_preexisting_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 6: Joining relative scanner paths with self.base_dir reconciles pre-existing files when cwd != base_dir."""
    watched_dir = tmp_path / "watched_target"
    watched_dir.mkdir()
    unrelated_cwd = tmp_path / "unrelated_working_dir"
    unrelated_cwd.mkdir()

    (watched_dir / "invoice_preexisting_1.txt").write_text(
        "Invoice #101", encoding="utf-8"
    )
    (watched_dir / "contract_preexisting_2.txt").write_text(
        "Legal contract #202", encoding="utf-8"
    )

    monkeypatch.chdir(unrelated_cwd)

    settings = _new_settings(tmp_path)
    settings.DEDUP_WINDOW = 0.01
    settings.KEYWORD_RULES = {
        "invoice": "Invoices",
        "contract": "Contracts",
    }

    try:
        daemon = ContinuousWatchdogDaemon(settings, str(watched_dir))
        daemon._is_running = True
        daemon._event_queue = asyncio.PriorityQueue()
        try:
            await daemon._run_reconciliation_audit()
            assert daemon._event_queue.qsize() == 2

            while not daemon._event_queue.empty():
                _, _, ev = await daemon._event_queue.get()
                assert os.path.isabs(ev.file_path)
                assert ev.file_path.startswith(str(watched_dir))
                await daemon._process_single_event(ev)
                daemon._event_queue.task_done()

            assert (watched_dir / "Invoices" / "invoice_preexisting_1.txt").is_file()
            assert (watched_dir / "Contracts" / "contract_preexisting_2.txt").is_file()
        finally:
            daemon.stop()
    finally:
        clear_connection_cache()


def test_metadata_pass_and_locked_override_preserve_policy_action(
    tmp_path: Path,
) -> None:
    """MetadataPass does not bypass supported files under content compliance policies, and conflict overrides keep policy_action."""
    ws = tmp_path / "meta_ws"
    ws.mkdir()
    store = open_store(tmp_path / "meta_store.db")

    doc_path = ws / "invoice_secret.txt"
    doc_path.write_text(
        "Invoice with SSN 123-45-6789 and secret_trigger", encoding="utf-8"
    )

    settings = _new_settings(tmp_path)
    settings.KEYWORD_RULES = {"invoice": "Invoices"}
    settings.POLICIES = [
        {
            "type": "compliance",
            "expression": "secret_trigger",
            "action": "redact",
            "target_path": "Safe_Invoices",
            "priority": 100,
        }
    ]

    try:
        session = AppSession(settings, base_dir=str(ws), store=store)
        try:
            bypassed = MetadataPass.run(
                str(ws), ["invoice_secret.txt"], settings, session.db
            )
            assert bypassed == []

            # Lock invoice_secret.txt to a custom folder; compliance policy_action='redact' must still survive
            def _write_lock():
                conn = session.cache_manager._get_conn()
                with conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO directory_cache (source_directory, corpus, locked_files, index_to_word, manual_folders) VALUES (?, ?, ?, ?, ?)",
                        (
                            str(ws),
                            "{}",
                            json.dumps({"invoice_secret.txt": "Locked_Invoices"}),
                            "{}",
                            "[]",
                        ),
                    )

            session.db_worker.execute_write(_write_lock)
            plan, res = SortPipeline(session).run()
            assert res is not None and res.ok
            node = plan["Safe_Invoices"]["invoice_secret.txt"]
            assert node["policy_action"] == "redact"
            out_file = ws / "Safe_Invoices" / "invoice_secret.txt"
            assert out_file.is_file()
            assert "123-45-6789" not in out_file.read_text(encoding="utf-8")
        finally:
            session.close()
    finally:
        clear_connection_cache()


def test_unarchive_safety_rejection_and_move_retain_post_actions(
    tmp_path: Path,
) -> None:
    """Zip-bomb archives quarantine safely, path-traversal entries are stripped, and move/retain post_unarchive_actions undo cleanly."""
    ws = tmp_path / "unarchive_modes_ws"
    ws.mkdir()
    store = open_store(tmp_path / "unarchive_modes_store.db")

    # 1. Zip-bomb archive (>100:1 compression ratio) -> ArchiveSafetyError -> held intact in _Compliance_Quarantine with MANUAL_REVIEW_REQUIRED
    bomb_zip = ws / "bomb_ratio.zip"
    with zipfile.ZipFile(bomb_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.bin", b"\x00" * (2 * 1024 * 1024))
    bomb_bytes = bomb_zip.read_bytes()

    # 2. Safe archive with a Zip-Slip traversal entry + valid child and post_unarchive_action="archive" -> moved to Processed_Archives, traversal stripped
    move_zip = ws / "move_bundle.zip"
    with zipfile.ZipFile(move_zip, "w") as zf:
        zf.writestr("../../escaped_payload.txt", "should never escape")
        zf.writestr("invoice_march.txt", "March vendor invoice #300")
    move_bytes = move_zip.read_bytes()

    # 3. Safe archive with post_unarchive_action="retain" -> kept in place, children sorted
    keep_zip = ws / "retain_bundle.zip"
    with zipfile.ZipFile(keep_zip, "w") as zf:
        zf.writestr("nda_april.txt", "April NDA legal agreement")
    keep_bytes = keep_zip.read_bytes()

    settings = _new_settings(tmp_path)
    settings.KEYWORD_RULES = {
        "invoice": "Invoices",
        "nda": "Legal",
    }
    settings.POLICIES = [
        {
            "type": "unarchive",
            "expression": "bomb_*.zip",
            "action": "unarchive",
            "post_unarchive_action": "delete",
            "target_path": "",
            "priority": 100,
        },
        {
            "type": "unarchive",
            "expression": "move_*.zip",
            "action": "unarchive",
            "post_unarchive_action": "archive",
            "post_unarchive_target": "Processed_Archives",
            "target_path": "",
            "priority": 95,
        },
        {
            "type": "unarchive",
            "expression": "retain_*.zip",
            "action": "unarchive",
            "post_unarchive_action": "retain",
            "target_path": "",
            "priority": 90,
        },
    ]

    try:
        session = AppSession(settings, base_dir=str(ws), store=store)
        try:
            _, res = SortPipeline(session).run()
            assert res is not None and res.ok

            # Zip-bomb quarantined intact; path-traversal payload never written outside workspace
            assert not bomb_zip.exists()
            assert not (tmp_path / "escaped_payload.txt").exists()
            quar_bomb = ws / "_Compliance_Quarantine" / "bomb_ratio.zip"
            assert quar_bomb.is_file()
            assert quar_bomb.read_bytes() == bomb_bytes

            # Move-mode zip relocated to Processed_Archives and valid child sorted
            assert not move_zip.exists()
            assert (
                ws / "Processed_Archives" / "move_bundle.zip"
            ).read_bytes() == move_bytes
            assert (ws / "Invoices" / "invoice_march.txt").is_file()

            # Retain-mode zip kept in place and child sorted
            assert keep_zip.is_file()
            assert keep_zip.read_bytes() == keep_bytes
            assert (ws / "Legal" / "nda_april.txt").is_file()

            q_records = session.db.get_quarantine_records_by_base_dir(str(ws))
            statuses = {r["original_filepath"]: r["status"] for r in q_records}
            assert statuses.get("bomb_ratio.zip") == "MANUAL_REVIEW_REQUIRED"
            assert statuses.get("move_bundle.zip") == "UNARCHIVED"
            assert statuses.get("retain_bundle.zip") == "UNARCHIVED"

            # Undo restores bomb_zip & move_zip, leaves retain_zip intact, and removes sorted children
            undo_res = SortPipeline(session).undo(res.session_id)
            assert undo_res.ok
            assert bomb_zip.read_bytes() == bomb_bytes
            assert move_zip.read_bytes() == move_bytes
            assert keep_zip.read_bytes() == keep_bytes
            assert not (ws / "Invoices" / "invoice_march.txt").exists()
            assert not (ws / "Legal" / "nda_april.txt").exists()
        finally:
            session.close()
    finally:
        clear_connection_cache()


def test_archive_and_retain_policy_actions_and_undo(tmp_path: Path) -> None:
    """Archive and retain policy directives move or retain files in MoveLedger, log quarantine_records, and undo cleanly."""
    ws = tmp_path / "archive_retain_ws"
    ws.mkdir()
    (ws / "Retained_Documents").mkdir()
    store = open_store(tmp_path / "archive_retain_store.db")

    arch_file = ws / "legacy_ledger_2021.txt"
    arch_file.write_text("Closed fiscal year 2021 ledger", encoding="utf-8")

    retain_move_file = ws / "board_minutes_hold.txt"
    retain_move_file.write_text("7-year retention hold board minutes", encoding="utf-8")

    retain_inplace_file = ws / "Retained_Documents" / "already_retained_hold.txt"
    retain_inplace_file.write_text("Already in Retained_Documents", encoding="utf-8")

    settings = _new_settings(tmp_path)
    settings.SCAN_MAX_DEPTH = 2
    settings.POLICIES = [
        {
            "type": "keyword",
            "expression": "legacy_ledger",
            "action": "archive",
            "target_path": "",
            "priority": 100,
        },
        {
            "type": "keyword",
            "expression": "_hold",
            "action": "retain",
            "target_path": "Retained_Documents",
            "priority": 95,
        },
    ]

    try:
        session = AppSession(settings, base_dir=str(ws), store=store)
        try:
            _, res = SortPipeline(session).run()
            assert res is not None and res.ok

            assert not arch_file.exists()
            assert (ws / "Archive" / "legacy_ledger_2021.txt").is_file()
            assert not retain_move_file.exists()
            assert (ws / "Retained_Documents" / "board_minutes_hold.txt").is_file()
            assert retain_inplace_file.is_file()

            q_records = session.db.get_quarantine_records_by_base_dir(str(ws))
            by_orig = {r["original_filepath"]: r for r in q_records}
            assert by_orig["legacy_ledger_2021.txt"]["status"] == "ARCHIVED"
            assert by_orig["board_minutes_hold.txt"]["status"] == "RELEASED"
            assert (
                by_orig["Retained_Documents/already_retained_hold.txt"]["status"]
                == "RELEASED"
            )

            undo_res = SortPipeline(session).undo(res.session_id)
            assert undo_res.ok
            assert arch_file.is_file()
            assert retain_move_file.is_file()
            assert retain_inplace_file.is_file()
        finally:
            session.close()
    finally:
        clear_connection_cache()
