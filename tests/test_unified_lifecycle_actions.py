"""End-to-end tests for unified policy lifecycle actions, MoveLedger rollback/reconciliation, CLI --copy, TUI modals, and importers."""

import asyncio
import hashlib
import io
import os
import plistlib
import tarfile
import zipfile

import pytest

from app.config import AppSettings
from app.core.cache import CacheManager
from app.core.daemon import ContinuousWatchdogDaemon
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.exceptions import ArchiveSafetyError, UnsupportedArchiveFormatError
from app.core.forensic_scanner import ForensicScanner
from app.core.history import HistoryManager
from app.core.pipeline import SortPipeline
from app.core.quarantine_interceptor import QuarantineInterceptorService
from app.core.rule_importers import (
    DropItRuleImporter,
    HazelRuleImporter,
    TagSpacesRuleImporter,
    get_rule_importer,
)
from app.core.rule_templates import export_active_rules
from app.core.session import AppSession
from app.core.store import STORE_MIGRATIONS, open_store
from app.main import build_parser, handle_drop_command, handle_sort_command


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_path(path: os.PathLike | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _make_zip(path: os.PathLike | str, members: dict[str, bytes]) -> bytes:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    with open(path, "rb") as f:
        return f.read()


def _make_targz(path: os.PathLike | str, members: dict[str, bytes]) -> bytes:
    with tarfile.open(path, "w:gz") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    with open(path, "rb") as f:
        return f.read()


def _make_minimal_docx(path: os.PathLike | str, text: str) -> bytes:
    content_types = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        b'<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        b'<Default Extension="xml" ContentType="application/xml"/>'
        b'<Override PartName="/word/document.xml" '
        b'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        b"</Types>"
    )
    rels = (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" '
        b'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        b'Target="word/document.xml"/>'
        b"</Relationships>"
    )
    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>"
        "</w:document>"
    ).encode("utf-8")
    return _make_zip(
        path,
        {
            "[Content_Types].xml": content_types,
            "_rels/.rels": rels,
            "word/document.xml": doc_xml,
        },
    )


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def test_forensic_scanner_rejects_7z_rar_and_corrupt_archives_preserving_sources(
    tmp_path,
):
    """Criterion 2: ForensicScanner.unpack_archive raises UnsupportedArchiveFormatError on .7z/.rar and preserves source files."""
    scanner = ForensicScanner()
    extract_dir = tmp_path / "extracted"

    seven_z = tmp_path / "bundle.7z"
    seven_z_bytes = b"7z\xbc\xaf\x27\x1c\x00\x04payload-bytes-intact"
    seven_z.write_bytes(seven_z_bytes)

    rar_file = tmp_path / "release.rar"
    rar_bytes = b"Rar!\x1a\x07\x01\x00payload-rar-intact"
    rar_file.write_bytes(rar_bytes)

    corrupt_zip = tmp_path / "corrupt.zip"
    corrupt_bytes = b"PK\x03\x04\x14\x00\x00\x00truncated-corrupt-zip"
    corrupt_zip.write_bytes(corrupt_bytes)

    with pytest.raises(UnsupportedArchiveFormatError, match="7z"):
        scanner.unpack_archive(str(seven_z), str(extract_dir))
    assert seven_z.read_bytes() == seven_z_bytes

    with pytest.raises(UnsupportedArchiveFormatError, match="rar"):
        scanner.unpack_archive(str(rar_file), str(extract_dir))
    assert rar_file.read_bytes() == rar_bytes

    with pytest.raises(ArchiveSafetyError):
        scanner.unpack_archive(str(corrupt_zip), str(extract_dir))
    assert corrupt_zip.read_bytes() == corrupt_bytes


def test_mixed_archive_batch_extracts_zip_tar_and_preserves_7z_rar(tmp_path):
    """Scenario 2 & Criterion 2: Mixed archive batch extracts .zip and .tar.gz while leaving .7z and .rar intact."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    store = open_store(
        tmp_path / "mixed_store", legacy_session_root=tmp_path / "empty_sessions"
    )
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.POLICIES = [
        {
            "type": "unarchive",
            "expression": "*.*",
            "action": "unarchive",
            "post_unarchive_action": "delete",
            "target_path": "Unpacked",
            "priority": 100,
        }
    ]

    zip_path = ws / "valid.zip"
    _make_zip(zip_path, {"report.txt": b"Quarterly revenue numbers"})

    tar_path = ws / "logs.tar.gz"
    _make_targz(tar_path, {"server.log": b"2026-11-23 INFO boot ok\n"})

    seven_z = ws / "backup.7z"
    seven_z_bytes = b"7z\xbc\xaf\x27\x1c\x00\x04important-7z-archive"
    seven_z.write_bytes(seven_z_bytes)

    rar_path = ws / "photos.rar"
    rar_bytes = b"Rar!\x1a\x07\x00important-rar-archive"
    rar_path.write_bytes(rar_bytes)

    session = AppSession(settings, base_dir=str(ws), store=store)
    try:
        pipeline = SortPipeline(session)
        _, outcome = pipeline.run()
        assert outcome is not None
        assert (
            ws / "Unpacked" / "report.txt"
        ).read_bytes() == b"Quarterly revenue numbers"
        assert (
            ws / "Unpacked" / "server.log"
        ).read_bytes() == b"2026-11-23 INFO boot ok\n"
        assert not zip_path.exists()
        assert not tar_path.exists()

        # Unsupported .7z and .rar must remain 100% intact on disk!
        assert seven_z.exists()
        assert seven_z.read_bytes() == seven_z_bytes
        assert rar_path.exists()
        assert rar_path.read_bytes() == rar_bytes
        assert len(outcome.failed) == 2
    finally:
        session.close()


def test_redact_scrubs_utf8_text_and_holds_pdf_docx_in_quarantine_intact(tmp_path):
    """Scenario 3 & Criterion 3: Redact scrubs PII in UTF-8 text files and quarantines binary .pdf and .docx intact."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    store = open_store(
        tmp_path / "redact_store", legacy_session_root=tmp_path / "empty_sessions"
    )
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.POLICIES = [
        {
            "type": "compliance",
            "expression": "patient",
            "action": "redact",
            "target_path": "Sanitized",
            "priority": 100,
        }
    ]

    note_txt = ws / "patient_intake.txt"
    note_orig = "Patient SSN: 123-45-6789, Email: jane.doe@example.com\n"
    note_txt.write_text(note_orig, encoding="utf-8")

    pdf_file = ws / "patient_chart.pdf"
    pdf_bytes = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n<< /Type /Catalog >>\nendobj\n\x80\xff\xfe%%EOF\n"
    pdf_file.write_bytes(pdf_bytes)

    docx_file = ws / "patient_summary.docx"
    docx_bytes = _make_minimal_docx(docx_file, "Patient SSN 123-45-6789")

    session = AppSession(settings, base_dir=str(ws), store=store)
    try:
        pipeline = SortPipeline(session)
        _, outcome = pipeline.run()
        assert outcome is not None
        assert not outcome.failed

        sanitized = (ws / "Sanitized" / "patient_intake.txt").read_text(
            encoding="utf-8"
        )
        assert "123-45-6789" not in sanitized
        assert "jane.doe@example.com" not in sanitized
        assert "[REDACTED_SSN]" in sanitized
        assert "[REDACTED_EMAIL]" in sanitized

        quarantined_pdf = ws / "_Compliance_Quarantine" / "patient_chart.pdf"
        quarantined_docx = ws / "_Compliance_Quarantine" / "patient_summary.docx"
        assert quarantined_pdf.exists()
        assert quarantined_pdf.read_bytes() == pdf_bytes
        assert quarantined_pdf.read_bytes().startswith(b"%PDF-1.7")

        assert quarantined_docx.exists()
        assert quarantined_docx.read_bytes() == docx_bytes
        assert zipfile.is_zipfile(quarantined_docx)
    finally:
        session.close()

    # Also verify QuarantineInterceptorService preserves .pdf and .docx binary headers on redact
    worker = DBWorker()
    try:
        db_path = tmp_path / "q_redact.db"
        db = Database(db_path, worker, migrations=STORE_MIGRATIONS)
        cache_mgr = CacheManager(str(db_path), worker, migrations=STORE_MIGRATIONS)
        history = HistoryManager(
            db, cache_mgr, db_path=str(db_path), migrations=STORE_MIGRATIONS
        )
        ws_q = tmp_path / "quarantine_ws"
        ws_q.mkdir()
        q_pdf = ws_q / "patient_scan.pdf"
        q_pdf.write_bytes(pdf_bytes)
        q_docx = ws_q / "patient_form.docx"
        q_docx.write_bytes(docx_bytes)

        interceptor = QuarantineInterceptorService(
            db=db,
            policies=settings.POLICIES,
            runtime_settings=settings,
            history_manager=history,
        )
        res_pdf = interceptor.process_quarantine_job(
            interceptor.stage_incoming_file(str(q_pdf), str(ws_q))["job_id"]
        )
        res_docx = interceptor.process_quarantine_job(
            interceptor.stage_incoming_file(str(q_docx), str(ws_q))["job_id"]
        )
        assert res_pdf["status"] == "QUARANTINED"
        assert res_pdf["staged_filepath"] is not None
        assert _read_bytes(res_pdf["staged_filepath"]) == pdf_bytes
        assert res_docx["status"] == "QUARANTINED"
        assert res_docx["staged_filepath"] is not None
        assert _read_bytes(res_docx["staged_filepath"]) == docx_bytes
        assert len(history.get_sessions()) == 2
    finally:
        worker.stop()


def test_ignore_and_halting_policies_leave_files_untouched_at_origin(tmp_path):
    """Criterion 4: Files matching ignore or failing halting stay untouched at origin."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    store = open_store(
        tmp_path / "ignore_halt_store", legacy_session_root=tmp_path / "empty_sessions"
    )
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.POLICIES = [
        {
            "type": "ignore",
            "expression": "*.part",
            "action": "ignore",
            "target_path": "",
            "priority": 200,
        },
        {
            "type": "compliance",
            "expression": "APPROVED-RELEASE",
            "target_path": "Approved",
            "priority": 100,
            "halting": True,
        },
        {
            "type": "keyword",
            "expression": "invoice",
            "target_path": "LowerPriorityInvoices",
            "priority": 50,
        },
    ]

    part_file = ws / "ubuntu.iso.part"
    part_file.write_bytes(b"partial-download-stream")

    halted_file = ws / "unapproved_invoice.txt"
    halted_file.write_text("Invoice #991 without release stamp", encoding="utf-8")

    approved_file = ws / "approved_doc.txt"
    approved_file.write_text("APPROVED-RELEASE: ready for filing", encoding="utf-8")

    session = AppSession(settings, base_dir=str(ws), store=store)
    try:
        pipeline = SortPipeline(session)
        _, outcome = pipeline.run()
        assert outcome is not None
        assert not outcome.failed

        # Approved file moves to Approved; .part and halted_file stay untouched at origin!
        assert (ws / "Approved" / "approved_doc.txt").exists()
        assert part_file.exists()
        assert part_file.read_bytes() == b"partial-download-stream"
        assert halted_file.exists()
        assert (
            halted_file.read_text(encoding="utf-8")
            == "Invoice #991 without release stamp"
        )
        assert not (ws / "LowerPriorityInvoices").exists()
        assert not (ws / "Miscellaneous").exists()
        assert len(outcome.moved) == 1
    finally:
        session.close()


def test_cli_copy_flag_and_policy_copy_archive_retain_actions(tmp_path, monkeypatch):
    """Scenario 1 & Criterion 1, 5: CLI --copy flag and ignore policies work end-to-end."""
    app_dir = tmp_path / "app_home"
    app_dir.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    src = tmp_path / "inbox"
    dest = tmp_path / "sorted"
    src.mkdir()
    dest.mkdir()

    f1 = src / "budget_2026.txt"
    f1.write_text("Annual finance budget forecast 2026", encoding="utf-8")
    f2 = src / "draft.part"
    f2.write_bytes(b"in-progress-download")

    settings = AppSettings(filepath=str(app_dir / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.KEYWORD_RULES = {"budget": "Finance"}
    settings.POLICIES = [
        {
            "type": "ignore",
            "expression": "*.part",
            "action": "ignore",
            "target_path": "",
            "priority": 100,
        }
    ]
    settings._save()

    parser = build_parser()
    args = parser.parse_args(
        ["sort", str(src), "--dest-dir", str(dest), "--copy", "--quiet"]
    )
    assert args.copy is True

    with pytest.raises(SystemExit) as exc:
        handle_sort_command(args, settings)
    assert exc.value.code == 0

    # Original stays in inbox AND verified copy exists in sorted/Finance
    assert f1.exists()
    assert (dest / "Finance" / "budget_2026.txt").exists()
    assert _sha256_path(f1) == _sha256_path(dest / "Finance" / "budget_2026.txt")
    # Ignored .part file untouched in inbox and absent from dest
    assert f2.exists()
    assert not list(dest.rglob("*.part"))

    # Also test `drop --copy`
    drop_src = tmp_path / "drop_item.txt"
    drop_src.write_text("budget summary quick drop", encoding="utf-8")
    drop_args = parser.parse_args(
        ["drop", str(drop_src), "--dest-dir", str(dest), "--copy", "--quiet"]
    )
    assert drop_args.copy is True
    with pytest.raises(SystemExit) as drop_exc:
        handle_drop_command(drop_args, settings)
    assert drop_exc.value.code == 0
    assert drop_src.exists()
    assert (dest / "Finance" / "drop_item.txt").exists()


def test_ledger_rollback_and_crash_reconciliation_for_all_lifecycle_ops(tmp_path):
    """Criterion 6: MoveLedger rolls back and crash-reconciles move, copy, archive, unarchive, and redact losslessly."""
    ws = tmp_path / "workspace"
    ws.mkdir()
    store = open_store(
        tmp_path / "ledger_store", legacy_session_root=tmp_path / "empty_sessions"
    )
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.POLICIES = [
        {
            "type": "pattern",
            "expression": "copy_*.txt",
            "action": "copy",
            "target_path": "Copied",
            "priority": 100,
        },
        {
            "type": "pattern",
            "expression": "arch_*.txt",
            "action": "archive",
            "target_path": "Vault",
            "priority": 95,
        },
        {
            "type": "unarchive",
            "expression": "bundle_*.zip",
            "action": "unarchive",
            "post_unarchive_action": "delete",
            "target_path": "Extracted",
            "priority": 90,
        },
        {
            "type": "compliance",
            "expression": "ssn_record",
            "action": "redact",
            "target_path": "Redacted",
            "priority": 85,
        },
    ]
    settings.KEYWORD_RULES = {"plainmove": "Moved"}

    f_move = ws / "plainmove_doc.txt"
    move_bytes = b"Plain move document bytes"
    f_move.write_bytes(move_bytes)

    f_copy = ws / "copy_note.txt"
    copy_bytes = b"Copy me while keeping source"
    f_copy.write_bytes(copy_bytes)

    f_arch = ws / "arch_ledger.txt"
    arch_bytes = b"Archive me into verified zip"
    f_arch.write_bytes(arch_bytes)

    f_zip = ws / "bundle_release.zip"
    zip_bytes = _make_zip(
        f_zip,
        {
            "inner_a.txt": b"Extracted child A",
            "sub/inner_b.txt": b"Extracted child B",
        },
    )

    f_redact = ws / "ssn_record.txt"
    redact_bytes = b"ssn_record for 123-45-6789 confidential\n"
    f_redact.write_bytes(redact_bytes)

    session = AppSession(settings, base_dir=str(ws), store=store)
    try:
        pipeline = SortPipeline(session)
        _, outcome = pipeline.run()
        assert outcome is not None
        assert not outcome.failed

        # Verify post-execution state
        assert not f_move.exists()
        assert (ws / "Moved" / "plainmove_doc.txt").read_bytes() == move_bytes

        assert f_copy.read_bytes() == copy_bytes
        assert (ws / "Copied" / "copy_note.txt").read_bytes() == copy_bytes

        assert not f_arch.exists()
        zip_out = ws / "Vault" / "arch_ledger.txt.zip"
        assert zip_out.exists()
        with zipfile.ZipFile(zip_out, "r") as zf:
            assert zf.read("arch_ledger.txt") == arch_bytes

        assert not f_zip.exists()
        assert (ws / "Extracted" / "inner_a.txt").read_bytes() == b"Extracted child A"
        assert (
            ws / "Extracted" / "sub" / "inner_b.txt"
        ).read_bytes() == b"Extracted child B"

        assert not f_redact.exists()
        redacted_out = (ws / "Redacted" / "ssn_record.txt").read_text(encoding="utf-8")
        assert "123-45-6789" not in redacted_out

        # Preview rollback and then roll back the entire session
        history = session.history_manager
        sessions = history.get_sessions()
        assert len(sessions) == 1
        sid = sessions[0]["session_id"]

        preview = history.preview_rollback(sid)
        assert preview["summary"]["clean"] >= 5

        rb = history.rollback(sid)
        assert rb >= 5

        # Every original file and byte is restored!
        assert f_move.read_bytes() == move_bytes
        assert f_copy.read_bytes() == copy_bytes
        assert not (ws / "Copied" / "copy_note.txt").exists()
        assert f_arch.read_bytes() == arch_bytes
        assert not zip_out.exists()
        assert f_zip.read_bytes() == zip_bytes
        assert not (ws / "Extracted" / "inner_a.txt").exists()
        assert not (ws / "Extracted" / "sub" / "inner_b.txt").exists()
        assert f_redact.read_bytes() == redact_bytes
        assert not (ws / "Redacted" / "ssn_record.txt").exists()
    finally:
        session.close()

    # Now test crash reconciliation (`IN_PROGRESS` entries) for copy and archive
    worker = DBWorker()
    try:
        crash_db_path = tmp_path / "crash_reconcile.db"
        db = Database(crash_db_path, worker, migrations=STORE_MIGRATIONS)
        cache_mgr = CacheManager(
            str(crash_db_path), worker, migrations=STORE_MIGRATIONS
        )
        history_crash = HistoryManager(
            db, cache_mgr, db_path=str(crash_db_path), migrations=STORE_MIGRATIONS
        )
        crash_sid = history_crash.start_session(str(ws))
        # 1) Copy completed on disk before crash -> rolls forward to verified, preserving both src and dst
        c_copy_src = ws / "crash_copy_done.txt"
        c_copy_dst = ws / "CrashCopied" / "crash_copy_done.txt"
        c_copy_dst.parent.mkdir(parents=True, exist_ok=True)
        c_copy_src.write_bytes(b"copy-crash-bytes")
        c_copy_dst.write_bytes(b"copy-crash-bytes")
        seq_copy_done = history_crash.moves.record_intent(
            crash_sid,
            str(c_copy_src),
            str(c_copy_dst),
            _sha256_bytes(b"copy-crash-bytes"),
            len(b"copy-crash-bytes"),
            op_kind="copy",
            dst_sha256=_sha256_bytes(b"copy-crash-bytes"),
        )

        # 2) Copy aborted before dst was written -> marks failed/aborted while keeping src intact
        c_copy_abort_src = ws / "crash_copy_abort.txt"
        c_copy_abort_dst = ws / "CrashCopied" / "crash_copy_abort.txt"
        c_copy_abort_src.write_bytes(b"copy-abort-bytes")
        seq_copy_abort = history_crash.moves.record_intent(
            crash_sid,
            str(c_copy_abort_src),
            str(c_copy_abort_dst),
            _sha256_bytes(b"copy-abort-bytes"),
            len(b"copy-abort-bytes"),
            op_kind="copy",
            dst_sha256=_sha256_bytes(b"copy-abort-bytes"),
        )

        # 3) Archive completed on disk before crash -> rolls forward to verified
        c_arch_src = ws / "crash_arch_done.txt"
        c_arch_dst = ws / "CrashVault" / "crash_arch_done.txt.zip"
        c_arch_dst.parent.mkdir(parents=True, exist_ok=True)
        _make_zip(c_arch_dst, {"crash_arch_done.txt": b"archived-before-crash"})
        seq_arch_done = history_crash.moves.record_intent(
            crash_sid,
            str(c_arch_src),
            str(c_arch_dst),
            _sha256_bytes(b"archived-before-crash"),
            len(b"archived-before-crash"),
            op_kind="archive",
            dst_sha256=_sha256_path(c_arch_dst),
        )

        # 4) Archive crashed after parking source in backup dir before zip completed -> restores parked source!
        c_arch_abort_src = ws / "crash_arch_abort.txt"
        c_arch_abort_dst = ws / "CrashVault" / "crash_arch_abort.txt.zip"
        c_arch_parked = ws / "_parked_backup" / "crash_arch_abort.txt"
        c_arch_parked.parent.mkdir(parents=True, exist_ok=True)
        c_arch_parked.write_bytes(b"parked-original-bytes")
        seq_arch_abort = history_crash.moves.record_intent(
            crash_sid,
            str(c_arch_abort_src),
            str(c_arch_abort_dst),
            _sha256_bytes(b"parked-original-bytes"),
            len(b"parked-original-bytes"),
            op_kind="archive",
        )
        history_crash.moves.record_displacement(
            crash_sid, seq_arch_abort, str(c_arch_parked)
        )

        reconciled = history_crash.moves.reconcile_incomplete_transactions(db=db)
        assert reconciled == 4

        by_seq = {m.seq: m for m in history_crash.moves.list_moves(crash_sid)}
        assert by_seq[seq_copy_done].state == "verified"
        assert c_copy_src.read_bytes() == b"copy-crash-bytes"
        assert c_copy_dst.read_bytes() == b"copy-crash-bytes"

        assert by_seq[seq_copy_abort].state == "failed"
        assert c_copy_abort_src.read_bytes() == b"copy-abort-bytes"
        assert not c_copy_abort_dst.exists()

        assert by_seq[seq_arch_done].state == "verified"
        assert c_arch_dst.exists()

        assert by_seq[seq_arch_abort].state == "failed"
        assert c_arch_abort_src.read_bytes() == b"parked-original-bytes"
    finally:
        worker.stop()


def test_daemon_handles_copy_ignore_and_unsupported_archives_safely(
    tmp_path, monkeypatch
):
    """Criterion 1, 2, 4: Daemon executes copy/ignore/unarchive and leaves .7z/.rar intact."""
    app_dir = tmp_path / "daemon_home"
    app_dir.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    ws = tmp_path / "watch_dir"
    ws.mkdir()
    settings = AppSettings(filepath=str(app_dir / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.POLICIES = [
        {
            "type": "ignore",
            "expression": "*.part",
            "action": "ignore",
            "target_path": "",
            "priority": 120,
        },
        {
            "type": "copy",
            "expression": "*.pdf",
            "action": "copy",
            "target_path": "PDF_Backups",
            "priority": 110,
        },
        {
            "type": "unarchive",
            "expression": "*.7z",
            "action": "unarchive",
            "post_unarchive_action": "delete",
            "target_path": "Unpacked",
            "priority": 100,
        },
    ]
    settings._save()

    part_file = ws / "video.mp4.part"
    part_file.write_bytes(b"streaming-bytes")

    pdf_file = ws / "invoice.pdf"
    pdf_bytes = b"%PDF-1.4 sample invoice bytes"
    pdf_file.write_bytes(pdf_bytes)

    seven_z = ws / "archive.7z"
    seven_z_bytes = b"7z\xbc\xaf\x27\x1c\x00\x04daemon-7z-data"
    seven_z.write_bytes(seven_z_bytes)

    daemon = ContinuousWatchdogDaemon(settings, str(ws))
    daemon._is_running = True

    async def _triage_all():
        try:
            await daemon._triage_file_path(str(part_file))
            await daemon._triage_file_path(str(pdf_file))
            await daemon._triage_file_path(str(seven_z))
        finally:
            daemon._is_running = False
            if daemon._sort_executor is not None:
                daemon._sort_executor.shutdown(wait=True)
            if daemon._app_session is not None:
                daemon._app_session.close()

    asyncio.run(_triage_all())

    # .part stayed untouched
    assert part_file.exists()
    assert part_file.read_bytes() == b"streaming-bytes"

    # .pdf was copied while keeping source intact
    assert pdf_file.exists()
    assert pdf_file.read_bytes() == pdf_bytes
    assert (ws / "PDF_Backups" / "invoice.pdf").read_bytes() == pdf_bytes

    # .7z failed unarchive explicitly and stayed 100% intact on disk!
    assert seven_z.exists()
    assert seven_z.read_bytes() == seven_z_bytes


def test_importers_and_exporters_roundtrip_copy_ignore_archive_unarchive(tmp_path):
    """Criterion 5: Hazel, DropIt, TagSpaces, and Sortify JSON import & export copy and ignore."""
    ini_path = tmp_path / "rules.ini"
    ini_path.write_text(
        "[DropIt]\n"
        "[Association_1]\n"
        "Name = Copy PDFs\n"
        "Rules = *.pdf\n"
        "Action = Copy\n"
        "Destination = Backups/PDFs\n\n"
        "[Association_2]\n"
        "Name = Ignore Temp Downloads\n"
        "Rules = *.crdownload;*.part\n"
        "Action = Ignore\n"
        "Destination = \n\n"
        "[Association_3]\n"
        "Name = Compress Logs\n"
        "Rules = *.log\n"
        "Action = Compress\n"
        "Destination = Archive/Logs\n",
        encoding="utf-8",
    )
    res_ini = DropItRuleImporter().parse_file(ini_path)
    by_expr = {p["expression"]: p for p in res_ini.policies}
    assert by_expr["*.pdf"]["action"] == "copy"
    assert by_expr["*.crdownload"]["action"] == "ignore"
    assert by_expr["*.crdownload"]["target_path"] == ""
    assert by_expr["*.part"]["action"] == "ignore"
    assert by_expr["*.log"]["action"] == "archive"

    # Merge into AppSettings and round-trip through Sortify JSON, Hazel, and DropIt exporters
    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    res_ini.merge_into_settings(settings, import_disabled=False)

    out_json = tmp_path / "export.json"
    out_hazel = tmp_path / "export.hazelrules"
    out_dropit = tmp_path / "export.ini"

    export_active_rules(settings, "sortify_json", out_json)
    export_active_rules(settings, "hazel", out_hazel)
    export_active_rules(settings, "dropit_ini", out_dropit)

    re_json = get_rule_importer(out_json).parse_file(out_json)
    assert {p["action"] for p in re_json.policies} >= {"copy", "ignore", "archive"}

    with open(out_hazel, "rb") as f:
        hazel_plist = plistlib.load(f)
    action_types = {
        r["NSRuleActions"][0]["NSRuleActionType"] for r in hazel_plist["Rules"]
    }
    assert "NSRuleActionCopy" in action_types
    assert "NSRuleActionIgnore" in action_types
    assert "NSRuleActionArchive" in action_types

    re_hazel = HazelRuleImporter().parse_file(out_hazel)
    hazel_actions = {p["expression"]: p["action"] for p in re_hazel.policies}
    assert hazel_actions["*.pdf"] == "copy"
    assert hazel_actions["*.crdownload"] == "ignore"
    assert hazel_actions["*.log"] == "archive"

    re_dropit = DropItRuleImporter().parse_file(out_dropit)
    dropit_actions = {p["expression"]: p["action"] for p in re_dropit.policies}
    assert dropit_actions["*.pdf"] == "copy"
    assert dropit_actions["*.crdownload"] == "ignore"
    assert dropit_actions["*.log"] == "archive"

    # TagSpaces smart rule with copy & ignore
    ts_path = tmp_path / "taggroups.json"
    ts_path.write_text(
        '{"rules": ['
        '{"pattern": "*.bak", "action": "copy", "destination": "Backups"},'
        '{"pattern": "*.tmp", "action": "ignore"}'
        "]}",
        encoding="utf-8",
    )
    re_ts = TagSpacesRuleImporter().parse_file(ts_path)
    ts_actions = {p["expression"]: p["action"] for p in re_ts.policies}
    assert ts_actions["*.bak"] == "copy"
    assert ts_actions["*.tmp"] == "ignore"


def test_tui_rule_modals_create_and_edit_copy_and_ignore_rules(tmp_path):
    """Criterion 5: Users can create and edit copy and ignore rules in app/ui/modals/rules.py."""
    from textual.widgets import DataTable, Input, Select

    from app.ui.modals.rules import ActiveRuleManagerModal, RuleEditModal
    from app.ui.tui import AutoSorterTUI

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.AI_CONSENT_GRANTED = False

    async def _run():
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause(0.1)
            mgr = ActiveRuleManagerModal(settings)
            app.push_screen(mgr)
            await pilot.pause(0.1)

            # 1. Add an `ignore` rule with an empty target folder
            mgr.action_add_rule()
            await pilot.pause(0.1)
            assert isinstance(app.screen, RuleEditModal)
            edit_modal = app.screen
            edit_modal.query_one("#input-rule-pattern", Input).value = "*.part"
            edit_modal.query_one("#select-rule-action", Select).value = "ignore"
            edit_modal.query_one("#input-rule-target", Input).value = ""
            edit_modal.action_save()
            await pilot.pause(0.15)

            assert any(
                p.get("expression") == "*.part" and p.get("action") == "ignore"
                for p in settings.POLICIES
            )

            # 2. Add a `copy` rule targeting Backups/PDFs
            mgr.action_add_rule()
            await pilot.pause(0.1)
            edit_modal2 = app.screen
            assert isinstance(edit_modal2, RuleEditModal)
            edit_modal2.query_one("#input-rule-pattern", Input).value = "*.pdf"
            edit_modal2.query_one("#select-rule-action", Select).value = "copy"
            edit_modal2.query_one("#input-rule-target", Input).value = "Backups/PDFs"
            edit_modal2.action_save()
            await pilot.pause(0.15)

            assert any(
                p.get("expression") == "*.pdf"
                and p.get("action") == "copy"
                and p.get("target_path") == "Backups/PDFs"
                for p in settings.POLICIES
            )

            table = mgr.query_one("#rules-table", DataTable)
            rendered_types = [
                str(table.get_row_at(i)[1]) for i in range(table.row_count)
            ]
            assert any("[ignore]" in t for t in rendered_types)
            assert any("[copy]" in t for t in rendered_types)

    asyncio.run(_run())


def test_drop_zone_modal_executes_unified_policy_actions(tmp_path, monkeypatch):
    """Criterion 1: DropZoneModal executes copy, ignore, archive, unarchive, and redact policies."""
    from app.ui.modals.drop_zone import DropPlanConfirmModal, DropZoneModal
    from app.ui.tui import AutoSorterTUI

    app_dir = tmp_path / "dropzone_app"
    app_dir.mkdir()
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    ws = tmp_path / "drop_ws"
    ws.mkdir()

    f_part = ws / "incomplete.part"
    f_part.write_bytes(b"still-downloading")

    f_copy = ws / "receipt.pdf"
    pdf_bytes = b"%PDF-1.4 receipt bytes"
    f_copy.write_bytes(pdf_bytes)

    f_arch = ws / "audit.log"
    f_arch.write_bytes(b"2026-11-23 audit trail")

    f_zip = ws / "package.zip"
    _make_zip(f_zip, {"readme.txt": b"Unpacked from DropZone"})

    f_redact = ws / "clinical_note.txt"
    f_redact.write_text("Clinical SSN 123-45-6789\n", encoding="utf-8")

    settings = AppSettings(filepath=str(app_dir / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.POLICIES = [
        {
            "type": "ignore",
            "expression": "*.part",
            "action": "ignore",
            "target_path": "",
            "priority": 120,
        },
        {
            "type": "copy",
            "expression": "*.pdf",
            "action": "copy",
            "target_path": "Copied_PDFs",
            "priority": 110,
        },
        {
            "type": "pattern",
            "expression": "*.log",
            "action": "archive",
            "target_path": "Archived_Logs",
            "priority": 105,
        },
        {
            "type": "unarchive",
            "expression": "*.zip",
            "action": "unarchive",
            "post_unarchive_action": "delete",
            "target_path": "Unpacked_Zip",
            "priority": 100,
        },
        {
            "type": "compliance",
            "expression": "clinical",
            "action": "redact",
            "target_path": "Sanitized_Notes",
            "priority": 95,
        },
    ]
    settings._save()

    async def _run_dropzone():
        app = AutoSorterTUI(settings=settings, base_dir=str(ws))
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause(0.1)
            dz = DropZoneModal(
                settings=settings,
                base_dir=str(ws),
                initial_paths=[
                    str(f_part),
                    str(f_copy),
                    str(f_arch),
                    str(f_zip),
                    str(f_redact),
                ],
            )
            app.push_screen(dz)
            await pilot.pause(0.2)
            dz.trigger_drop_triage()

            for _ in range(60):
                await pilot.pause(0.1)
                if isinstance(app.screen, DropPlanConfirmModal):
                    break
            assert isinstance(app.screen, DropPlanConfirmModal)
            app.screen.action_confirm()

            for _ in range(60):
                await pilot.pause(0.1)
                if dz.processed_result is not None:
                    break
            assert dz.processed_result is not None
            assert dz.processed_result["status"] == "success"

    asyncio.run(_run_dropzone())

    assert f_part.exists()
    assert f_part.read_bytes() == b"still-downloading"

    assert f_copy.exists()
    assert (ws / "Copied_PDFs" / "receipt.pdf").read_bytes() == pdf_bytes

    assert not f_arch.exists()
    assert zipfile.is_zipfile(ws / "Archived_Logs" / "audit.log.zip")

    assert not f_zip.exists()
    assert (
        ws / "Unpacked_Zip" / "readme.txt"
    ).read_bytes() == b"Unpacked from DropZone"

    assert not f_redact.exists()
    scrubbed = (ws / "Sanitized_Notes" / "clinical_note.txt").read_text(
        encoding="utf-8"
    )
    assert "123-45-6789" not in scrubbed
    assert "[REDACTED_SSN]" in scrubbed
