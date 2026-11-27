"""Comprehensive tests for byte-verified deduplication, reversible Trash staging, TUI modals, CLI flags, rule importers, and CRO suppression."""

from __future__ import annotations

import json
import logging
import os
import plistlib
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest import mock

import pytest

from app.config import AppSettings, PolicyRule
from app.core.extractor import get_file_hash
from app.core.ledger import KeyAuditLedger
from app.core.mover import DUPLICATE_STAGING_DIR, _stage_disposal_item
from app.core.moves import INTENT, MoveLedger
from app.core.pipeline import SortPipeline
from app.core.policy_engine import PolicyEngine
from app.core.resilient_file_ops import (
    DETECT_ORIGINAL_BY_MODES,
    group_byte_identical_files,
    raw_file_hash,
    same_file_content,
    select_original_from_cluster,
    send_to_os_trash,
)
from app.core.rule_importers import (
    DropItRuleImporter,
    HazelRuleImporter,
    SortifyRuleImporter,
)
from app.core.scanner import get_files_recursively
from app.core.session import AppSession
from app.core.verifier import VerificationEngine, VirtualFilesystemTracker
from app.main import apply_config_overrides, build_parser
from app.plugins.clinical_compliance.cro_multi_study_pipeline import (
    CROMultiStudyPipeline,
)
from app.ui.modals.edit import (
    DuplicateReviewModal,
    InteractiveCollisionResolverModal,
)
from app.ui.tui import AutoSorterTUI


def _write_docx_with_metadata(path: Path, body_xml: str, author: str) -> None:
    """Create a valid minimal .docx archive where only docProps/core.xml differs."""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="urn:schemas-microsoft-com:office:content-types"></Types>',
        )
        zf.writestr("word/document.xml", body_xml)
        zf.writestr(
            "docProps/core.xml",
            f"<cp:coreProperties><dc:creator>{author}</dc:creator></cp:coreProperties>",
        )


def _write_mp3_with_id3(path: Path, title_tag: bytes, audio_frames: bytes) -> None:
    """Create a synthetic MP3 with an ID3v2 header so get_file_hash strips the tag."""
    payload = title_tag.ljust(32, b"\x00")
    size = len(payload)
    synchsafe = bytes(
        [
            (size >> 21) & 0x7F,
            (size >> 14) & 0x7F,
            (size >> 7) & 0x7F,
            size & 0x7F,
        ]
    )
    header = b"ID3\x03\x00\x00" + synchsafe + payload
    path.write_bytes(header + audio_frames)


def test_exact_byte_grouping_ignores_metadata_stripped_hash_matches(tmp_path: Path):
    """Metadata-only digest matches (Office/MP3) are NOT grouped as duplicates, while byte-identical copies are."""
    docx_a = tmp_path / "contract_alice.docx"
    docx_b = tmp_path / "contract_bob.docx"
    _write_docx_with_metadata(docx_a, "<w:document>Same Clause</w:document>", "Alice")
    _write_docx_with_metadata(docx_b, "<w:document>Same Clause</w:document>", "Bob")

    mp3_a = tmp_path / "track_v1.mp3"
    mp3_b = tmp_path / "track_v2.mp3"
    frames = b"\xff\xfb\x90\x64" * 128
    _write_mp3_with_id3(mp3_a, b"TIT2Track One", frames)
    _write_mp3_with_id3(mp3_b, b"TIT2Track Two", frames)

    # Confirm get_file_hash strips Office/ID3 metadata and produces identical payload hashes
    assert get_file_hash(str(docx_a)) == get_file_hash(str(docx_b))
    assert get_file_hash(str(mp3_a)) == get_file_hash(str(mp3_b))
    # While exact byte verification rejects them
    assert not same_file_content(str(docx_a), str(docx_b))
    assert not same_file_content(str(mp3_a), str(mp3_b))
    assert raw_file_hash(docx_a) != raw_file_hash(docx_b)

    # Exact byte-identical copies with different names across folders
    inv_orig = tmp_path / "invoice.pdf"
    inv_copy1 = tmp_path / "invoice (1).pdf"
    sub = tmp_path / "downloads_sub"
    sub.mkdir()
    inv_copy2 = sub / "z_duplicate_invoice_final.pdf"
    exact_bytes = b"%PDF-1.4 exact invoice payload bytes 2026\n"
    inv_orig.write_bytes(exact_bytes)
    inv_copy1.write_bytes(exact_bytes)
    inv_copy2.write_bytes(exact_bytes)

    all_paths = [
        str(docx_a),
        str(docx_b),
        str(mp3_a),
        str(mp3_b),
        str(inv_orig),
        str(inv_copy1),
        str(inv_copy2),
    ]

    # VerificationEngine.get_duplicate_groups
    plan = {
        os.path.relpath(p, tmp_path): {
            "node_type": "file",
            "relative_source": os.path.relpath(p, tmp_path),
            "target_filename": os.path.basename(p),
            "is_checked": True,
        }
        for p in all_paths
    }
    groups = VerificationEngine.get_duplicate_groups(
        str(tmp_path), plan, detect_original_by="name"
    )
    assert len(groups) == 1
    clustered_sources = {item["source_path"] for item in groups[0]["items"]}
    assert clustered_sources == {
        os.path.normpath(str(inv_orig)),
        os.path.normpath(str(inv_copy1)),
        os.path.normpath(str(inv_copy2)),
    }


def test_detect_original_by_all_modes(tmp_path: Path):
    """Verify all 8 Organize-compatible detect_original_by modes and validation."""
    f_alpha = tmp_path / "alpha.txt"
    f_beta = tmp_path / "beta.txt"
    f_gamma = tmp_path / "gamma.txt"
    for p in (f_alpha, f_beta, f_gamma):
        p.write_bytes(b"identical-cluster-bytes")

    os.utime(f_alpha, (1_700_000_200, 1_700_000_200))
    os.utime(f_beta, (1_700_000_100, 1_700_000_100))
    os.utime(f_gamma, (1_700_000_300, 1_700_000_300))

    # Order passed in: gamma, alpha, beta
    candidates = [str(f_gamma), str(f_alpha), str(f_beta)]

    orig, red = select_original_from_cluster(candidates, "first_seen")
    assert orig == os.path.normpath(str(f_gamma))
    assert len(red) == 2

    orig, _ = select_original_from_cluster(candidates, "-first_seen")
    assert orig == os.path.normpath(str(f_beta))

    orig, _ = select_original_from_cluster(candidates, "name")
    assert orig == os.path.normpath(str(f_alpha))

    orig, _ = select_original_from_cluster(candidates, "-name")
    assert orig == os.path.normpath(str(f_gamma))

    orig, _ = select_original_from_cluster(candidates, "lastmodified")
    assert orig == os.path.normpath(str(f_beta))

    orig, _ = select_original_from_cluster(candidates, "-lastmodified")
    assert orig == os.path.normpath(str(f_gamma))

    # Mock creation timestamp deterministically for created / -created
    created_map = {
        os.path.normpath(str(f_alpha)): 500.0,
        os.path.normpath(str(f_beta)): 900.0,
        os.path.normpath(str(f_gamma)): 100.0,
    }
    with mock.patch(
        "app.core.resilient_file_ops._file_created_timestamp",
        side_effect=lambda p: created_map[os.path.normpath(p)],
    ):
        orig_c, _ = select_original_from_cluster(candidates, "created")
        assert orig_c == os.path.normpath(str(f_gamma))
        orig_rc, _ = select_original_from_cluster(candidates, "-created")
        assert orig_rc == os.path.normpath(str(f_beta))

    assert len(DETECT_ORIGINAL_BY_MODES) == 8
    with pytest.raises(ValueError, match="Unsupported detect_original_by"):
        select_original_from_cluster(candidates, "random_mode")

    clusters = group_byte_identical_files(candidates, detect_original_by="name")
    assert len(clusters) == 1
    assert clusters[0]["original"] == os.path.normpath(str(f_alpha))


@pytest.mark.anyio
async def test_ctrl_e_chains_duplicate_modal_and_collision_modal_and_skips_excluded_renames(
    tmp_path: Path,
):
    """Ctrl+E opens DuplicateReviewModal for byte duplicates and InteractiveCollisionResolverModal for distinct rename collisions."""
    dup1 = tmp_path / "invoice.pdf"
    dup2 = tmp_path / "invoice (1).pdf"
    dup1.write_bytes(b"same-invoice-bytes")
    dup2.write_bytes(b"same-invoice-bytes")

    distinct1 = tmp_path / "scan_001.txt"
    distinct2 = tmp_path / "scan_002.txt"
    distinct1.write_text("Quarterly revenue report Q1")
    distinct2.write_text("Quarterly revenue report Q2")

    # FileRenamerEngine assigned identical target_filename ("Financial_Report.txt") to distinct files
    plan = {
        "Invoices": {
            "invoice.pdf": {
                "node_type": "file",
                "relative_source": "invoice.pdf",
                "target_filename": "invoice.pdf",
                "is_checked": True,
            },
            "invoice (1).pdf": {
                "node_type": "file",
                "relative_source": "invoice (1).pdf",
                "target_filename": "invoice (1).pdf",
                "is_checked": True,
            },
        },
        "Reports": {
            "scan_001.txt": {
                "node_type": "file",
                "relative_source": "scan_001.txt",
                "target_filename": "Financial_Report.txt",
                "is_checked": True,
            },
            "scan_002.txt": {
                "node_type": "file",
                "relative_source": "scan_002.txt",
                "target_filename": "Financial_Report.txt",
                "is_checked": True,
            },
            "excluded_note.txt": {
                "node_type": "file",
                "relative_source": "excluded_note.txt",
                "target_filename": "bad/slash*name?.txt",
                "is_excluded": True,
                "is_checked": False,
            },
        },
    }

    # Verify check_rename_proposals ignores excluded nodes completely
    invalid_renames, unconfirmed_renames = (
        VirtualFilesystemTracker().check_rename_proposals(str(tmp_path), plan)
    )
    assert all(
        "excluded_note" not in str(err)
        for err in [*invalid_renames, *unconfirmed_renames]
    )

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    settings.override("DUPLICATE_ACTION", "review")
    app = AutoSorterTUI(settings=settings)
    mock_session = mock.MagicMock()
    mock_session.base_dir = str(tmp_path)
    app.app_session = mock_session
    app.plan = plan

    async with app.run_test() as pilot:
        with mock.patch.object(app, "run_execute_worker") as mock_worker:
            app.action_execute_sort()
            await pilot.pause()

            # Step 1: DuplicateReviewModal opens for byte-identical invoice cluster
            assert isinstance(app.screen, DuplicateReviewModal)
            dup_modal = app.screen
            assert len(dup_modal.duplicate_groups) == 1
            dup_modal.action_confirm()
            await pilot.pause()

            # Step 2: InteractiveCollisionResolverModal opens next for distinct scan_001/scan_002 collision
            assert isinstance(app.screen, InteractiveCollisionResolverModal)
            assert not isinstance(app.screen, DuplicateReviewModal)
            col_modal = app.screen
            assert len(col_modal.collision_groups) == 1
            col_modal.action_confirm()
            await pilot.pause()

            assert plan["Invoices"]["invoice (1).pdf"]["duplicate_action"] == "stage"
            assert (
                plan["Reports"]["scan_001.txt"]["target_filename"]
                == "Financial_Report.txt"
            )
            assert (
                plan["Reports"]["scan_002.txt"]["target_filename"]
                == "Financial_Report_1.txt"
            )
            mock_worker.assert_called_once()


def test_pipeline_stage_undo_trash_flush_and_headless_fallback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """End-to-end ADR 0005 & ADR 0006 verification: stage after primary verified, undo restores all copies, flush exports to OS Trash."""
    store_dir = tmp_path / "_app_store"
    work_dir = tmp_path / "downloads"
    work_dir.mkdir()

    payload = b"Contract Agreement v3 signed bytes 99887766"
    orig_file = work_dir / "agreement.txt"
    dup_file_1 = work_dir / "agreement (1).txt"
    sub_dir = work_dir / "old_copies"
    sub_dir.mkdir()
    dup_file_2 = sub_dir / "agreement_backup.txt"
    unique_file = work_dir / "budget_2026.txt"

    orig_file.write_bytes(payload)
    dup_file_1.write_bytes(payload)
    dup_file_2.write_bytes(payload)
    unique_file.write_text("Annual engineering budget spreadsheet summary")

    with mock.patch.dict(os.environ, {"AUTOSORTER_APP_DIR": str(store_dir)}):
        settings = AppSettings()
        settings.override("AI_CONSENT_GRANTED", False)
        settings.override("DUPLICATE_ACTION", "stage")
        settings.override("DETECT_ORIGINAL_BY", "name")
        settings.override("SCAN_MAX_DEPTH", 3)

        session = AppSession(settings, base_dir=str(work_dir))
        try:
            pipeline = SortPipeline(session)
            plan, result = pipeline.run()
            assert result is not None
            assert result.ok
            assert result.session_id

            # Redundant copies removed from original paths and staged under .sortify_duplicates/<session_id>/
            assert not dup_file_1.exists()
            assert not dup_file_2.exists()

            staging_session_dir = work_dir / DUPLICATE_STAGING_DIR / result.session_id
            assert staging_session_dir.is_dir()
            staged_files = [p for p in staging_session_dir.rglob("*") if p.is_file()]
            assert len(staged_files) == 2
            for sf in staged_files:
                assert sf.read_bytes() == payload

            # Re-scanning work_dir ignores .sortify_duplicates hidden staging directory
            rescanned = get_files_recursively(str(work_dir), settings=settings)
            assert not any(DUPLICATE_STAGING_DIR in p for p in rescanned)

            # Undo restores EVERY staged duplicate and the primary back to exact source paths (ADR 0006)
            undo_res = pipeline.undo(result.session_id)
            assert undo_res.ok
            assert orig_file.read_bytes() == payload
            assert dup_file_1.read_bytes() == payload
            assert dup_file_2.read_bytes() == payload

            # Re-run sort to stage again, test headless OS Trash failure keeps staged files intact
            session2 = AppSession(settings, base_dir=str(work_dir))
            try:
                pipeline2 = SortPipeline(session2)
                _, res2 = pipeline2.run()
                assert res2 is not None and res2.ok

                def failing_trash(_path: str) -> None:
                    raise OSError("Headless environment lacks desktop trash")

                failed_report = pipeline2.flush_trash(
                    res2.session_id, trash_fn=failing_trash
                )
                assert failed_report["exported"] == []
                assert len(failed_report["kept_staged"]) == 2
                # Staged files remain recoverable via undo after failed OS Trash handoff!
                for kept_path in failed_report["kept_staged"]:
                    assert os.path.isfile(kept_path)

                # Now flush with working OS Trash callback and verify audit trail
                trashed_paths: list[str] = []

                def fake_os_trash(target: str) -> None:
                    trashed_paths.append(target)
                    os.remove(target)

                with caplog.at_level(logging.INFO):
                    ok_report = pipeline2.flush_trash(
                        res2.session_id, trash_fn=fake_os_trash
                    )
                assert len(ok_report["exported"]) == 2
                assert len(trashed_paths) == 2
                assert ok_report["kept_staged"] == []
                assert any(
                    "Irreversible OS Trash handoff:" in rec.message
                    for rec in caplog.records
                )

                audit_events = KeyAuditLedger().get_key_audit_events(limit=10)
                trash_events = [
                    ev
                    for ev in audit_events
                    if ev["event_type"] == "OS_TRASH_HANDOFF"
                    and ev["status"] == "EXPORTED_TO_OS_TRASH"
                ]
                assert len(trash_events) == 2
            finally:
                session2.close()
        finally:
            session.close()


def test_adr_0005_refuses_staging_when_primary_not_verified(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    """ADR 0005 guard: _stage_disposal_item refuses to displace a duplicate if primary move is unverified."""
    import threading

    db_path = tmp_path / "ledger.db"
    base_dir = tmp_path / "workspace"
    base_dir.mkdir()

    primary_src = base_dir / "orig.txt"
    dup_src = base_dir / "copy.txt"
    primary_src.write_bytes(b"important-bytes")
    dup_src.write_bytes(b"important-bytes")

    moves = MoveLedger(str(db_path), auto_init=True)
    # Record primary move in INTENT state only (NOT verified!)
    dest_primary = base_dir / "Sorted" / "orig.txt"
    moves.record_intent(
        "sess_unverified",
        str(primary_src),
        str(dest_primary),
        raw_file_hash(primary_src),
        primary_src.stat().st_size,
    )
    row = moves.list_moves("sess_unverified")[0]
    assert row.state == INTENT

    fake_history = mock.MagicMock()
    fake_history.moves = moves
    audit_records: list = []

    with caplog.at_level(logging.WARNING):
        res = _stage_disposal_item(
            base_dir=str(base_dir),
            item={
                "source_path": str(dup_src),
                "content": {
                    "is_duplicate": True,
                    "duplicate_of": str(primary_src),
                    "duplicate_action": "stage",
                },
            },
            db=None,
            path_map={
                os.path.normcase(os.path.abspath(str(primary_src))): str(dest_primary)
            },
            session_id="sess_unverified",
            history_manager=fake_history,
            db_lock=threading.Lock(),
            db_updates_batch=[],
            audit_records=audit_records,
        )
    assert res is None
    assert any("ADR 0005" in rec.message for rec in caplog.records)
    # Duplicate source file was NOT touched!
    assert dup_src.exists()
    assert dup_src.read_bytes() == b"important-bytes"


def test_policy_engine_and_hazel_dropit_trash_rule_importers(tmp_path: Path):
    """Verify PolicyEngine duplicate/trash evaluation and warning-free Hazel & DropIt Trash imports."""
    # 1. PolicyEngine duplicate & trash evaluation
    dup_rule = PolicyRule(
        type="duplicate",
        expression="*.pdf",
        target_path="",
        priority=5,
        action="trash",
        detect_original_by="created",
    )
    dup_rule_dict: dict[str, object] = dict(dup_rule)
    matched = PolicyEngine.evaluate_policies(
        "invoice (1).pdf",
        "Invoice text",
        None,
        [dup_rule_dict],
        is_duplicate=True,
    )
    assert matched is not None and not isinstance(matched, tuple)
    assert matched["action"] == "trash"

    not_matched = PolicyEngine.evaluate_policies(
        "invoice.pdf",
        "Invoice text",
        None,
        [dup_rule_dict],
        is_duplicate=False,
    )
    assert not_matched is None

    # 2. HazelRuleImporter: Trash action, Move w/ ThrowAwayDuplicate, and folder RemoveDuplicates
    hazel_plist = tmp_path / "downloads.hazelrules"
    hazel_payload = {
        "RemoveDuplicates": True,
        "Rules": [
            {
                "Name": "Trash Old DMG Installers",
                "Enabled": True,
                "Conditions": [
                    {"Attribute": "Extension", "Operator": "is", "Value": "dmg"}
                ],
                "Actions": [{"Type": "Trash"}],
            },
            {
                "Name": "Move PDFs & Throw Away Duplicates",
                "Enabled": True,
                "Conditions": [
                    {"Attribute": "Extension", "Operator": "is", "Value": "pdf"}
                ],
                "Actions": [
                    {
                        "Type": "Move",
                        "Destination": "Documents/PDFs",
                        "ThrowAwayDuplicate": True,
                    }
                ],
            },
        ],
    }
    hazel_plist.write_bytes(plistlib.dumps(hazel_payload))

    hazel_res = HazelRuleImporter().parse_file(hazel_plist)
    assert hazel_res.warnings == []
    assert hazel_res.unsupported == []
    actions = {
        (r["type"], r.get("action"), r["expression"]) for r in hazel_res.policies
    }
    assert ("pattern", "trash", "*.dmg") in actions
    assert ("duplicate", "trash", "*.pdf") in actions
    assert ("duplicate", "trash", "*") in actions

    # Merge into Settings and roundtrip via SortifyRuleImporter
    target_settings = AppSettings()
    hazel_res.merge_into_settings(target_settings)
    assert target_settings.DUPLICATE_ACTION == "trash"

    exported_json = tmp_path / "exported_rules.json"
    exported_json.write_text(
        json.dumps(
            {
                "POLICIES": [
                    p.model_dump() if hasattr(p, "model_dump") else dict(p)
                    for p in target_settings.POLICIES
                ]
            }
        ),
        encoding="utf-8",
    )
    reimported = SortifyRuleImporter().parse_file(exported_json)
    assert reimported.warnings == []
    assert len(reimported.policies) == len(target_settings.POLICIES)

    # 3. DropItRuleImporter: Recycle Bin action imports cleanly without warnings
    dropit_ini = tmp_path / "profile.ini"
    dropit_ini.write_text(
        "[DropItTempClean]\n"
        "Rules=*.tmp;*.bak\n"
        "Action=Recycle Bin\n"
        "Destination=\n"
        "Enabled=True\n",
        encoding="utf-8",
    )
    dropit_res = DropItRuleImporter().parse_file(dropit_ini)
    assert dropit_res.warnings == []
    assert dropit_res.unsupported == []
    assert len(dropit_res.policies) >= 1
    assert all(p.get("action") == "trash" for p in dropit_res.policies)


def test_cli_scan_and_sort_duplicates_flags(tmp_path: Path):
    """Verify CLI parser and scan/sort execution with --duplicates, --detect-original-by, and --flush-trash."""
    parser = build_parser()

    # Both space-separated '-created' and '=-created' work cleanly on scan and sort
    scan_args = parser.parse_args(
        [
            "scan",
            str(tmp_path),
            "--duplicates",
            "skip",
            "--detect-original-by",
            "-created",
            "--json",
        ]
    )
    assert scan_args.duplicates == "skip"
    assert scan_args.detect_original_by == "-created"

    sort_args = parser.parse_args(
        [
            "sort",
            str(tmp_path),
            "--duplicates",
            "trash",
            "--detect-original-by=-lastmodified",
            "--flush-trash",
            "--json",
        ]
    )
    assert sort_args.duplicates == "trash"
    assert sort_args.detect_original_by == "-lastmodified"
    assert sort_args.flush_trash is True

    settings = AppSettings()
    apply_config_overrides(settings, sort_args)
    assert settings.DUPLICATE_ACTION == "trash"
    assert settings.DETECT_ORIGINAL_BY == "-lastmodified"

    # Subprocess CLI verification for `scan` and `sort --duplicates stage`
    target_dir = tmp_path / "cli_sort_dir"
    target_dir.mkdir()
    (target_dir / "receipt_a.txt").write_text("Hotel receipt total $250.00")
    (target_dir / "receipt_b.txt").write_text("Hotel receipt total $250.00")

    env = os.environ.copy()
    env["AUTOSORTER_APP_DIR"] = str(tmp_path / "cli_store")
    env["PYTHON_KEYRING_BACKEND"] = "keyring.backends.fail.Keyring"
    env["SORTIFY_AI_CONSENT"] = "false"
    env["NON_INTERACTIVE"] = "1"

    repo_root = Path(__file__).resolve().parent.parent
    scan_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "app.main",
            "scan",
            str(target_dir),
            "--duplicates",
            "stage",
            "--detect-original-by",
            "-name",
            "--json",
        ],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert scan_proc.returncode == 0, scan_proc.stderr
    scan_json = json.loads(scan_proc.stdout)
    assert scan_json["status"] == "success"
    assert scan_json["files_scanned"] == 2

    # Test CLI sort --duplicates trash --flush-trash calls send2trash and logs handoff
    from app.main import handle_sort_command

    cli_trash_args = parser.parse_args(
        [
            "sort",
            str(target_dir),
            "--duplicates",
            "trash",
            "--detect-original-by",
            "name",
            "--flush-trash",
            "--json",
            "--decline-ai-consent",
        ]
    )
    with (
        mock.patch.dict(
            os.environ, {"AUTOSORTER_APP_DIR": str(tmp_path / "cli_store2")}
        ),
        mock.patch(
            "send2trash.send2trash", side_effect=lambda p: os.remove(p)
        ) as mock_cli_s2t,
        pytest.raises(SystemExit) as exc_info,
    ):
        cli_settings = AppSettings()
        handle_sort_command(cli_trash_args, cli_settings)
    assert exc_info.value.code == 0
    assert mock_cli_s2t.call_count == 1

    # Test send_to_os_trash invokes send2trash when trash_fn is omitted
    sample = tmp_path / "trash_me.txt"
    sample.write_text("temp")
    with mock.patch("send2trash.send2trash") as mock_s2t:
        ok, err = send_to_os_trash(sample)
        assert ok is True
        assert err is None
        mock_s2t.assert_called_once_with(os.path.abspath(str(sample)))


def test_cro_multi_study_pipeline_duplicate_suppression_and_archive(
    tmp_path: Path,
):
    """CROMultiStudyPipeline skips or archives verified duplicates flagged by ForensicScanner while preserving manifest lineage."""
    source_dir = tmp_path / "cro_source"
    source_dir.mkdir()

    protocol_text = (
        "Clinical Study Protocol Version 1.0 Protocol ID: PROTO-777 "
        "Principal Investigator: Dr. Elena Rostova Inclusion and exclusion criteria"
    )
    (source_dir / "protocol_master.txt").write_text(protocol_text, encoding="utf-8")
    (source_dir / "protocol_copy_email.txt").write_text(protocol_text, encoding="utf-8")
    with zipfile.ZipFile(source_dir / "site_bundle.zip", "w") as zf:
        zf.writestr("protocol_archive_dup.txt", protocol_text)

    # 1. Mode: suppress_duplicates=True (skip)
    target_skip = tmp_path / "cro_target_skip"
    pipeline_skip = CROMultiStudyPipeline(
        mode="tmf", smart_renaming=True, suppress_duplicates=True
    )
    res_skip = pipeline_skip.run_pipeline(str(source_dir), str(target_skip))

    assert res_skip.total_scanned_files == 3
    assert res_skip.total_unique_documents == 1
    assert res_skip.total_duplicates_detected == 2

    study_dir = target_skip / "PROTO_777"
    copied_docs = [
        p
        for p in study_dir.rglob("*")
        if p.is_file() and not p.name.startswith("compliance_audit_report.")
    ]
    # Exactly 1 protocol file in binder — zero '_1' duplicate suffix clutter!
    assert len(copied_docs) == 1
    assert "_1" not in copied_docs[0].name

    manifest_skip = json.loads(
        Path(res_skip.chain_of_custody_manifest_path).read_text(encoding="utf-8")
    )
    assert manifest_skip["summary_metrics"]["total_duplicates_suppressed"] == 2
    assert len(manifest_skip["document_manifest"]) == 3
    skipped_entries = [
        r
        for r in manifest_skip["document_manifest"]
        if r["duplicate_status"] == "skipped"
    ]
    assert len(skipped_entries) == 2
    for entry in skipped_entries:
        assert entry["is_duplicate"] is True
        assert entry["duplicate_of"] is not None
        assert entry["destination_path"] is None
        assert entry["primary_destination_path"] is not None

    # 2. Mode: duplicate_action="archive"
    target_arch = tmp_path / "cro_target_archive"
    pipeline_arch = CROMultiStudyPipeline(
        mode="tmf", smart_renaming=True, duplicate_action="archive"
    )
    res_arch = pipeline_arch.run_pipeline(str(source_dir), str(target_arch))
    manifest_arch = json.loads(
        Path(res_arch.chain_of_custody_manifest_path).read_text(encoding="utf-8")
    )
    assert manifest_arch["summary_metrics"]["total_duplicates_archived"] == 2
    archive_dir = target_arch / "PROTO_777" / "_Duplicate_Archive"
    assert archive_dir.is_dir()
    assert len(list(archive_dir.iterdir())) == 2


def test_resilient_file_ops_verifier_and_history_edge_branches(tmp_path: Path):
    """Cover edge branches in resilient_file_ops, VerificationEngine, and HistoryManager."""
    from app.core.resilient_file_ops import duplicate_staging_relpath

    # 1. raw_file_hash non-sha256, real _file_created_timestamp, empty cluster, symlinks & duplicate refs
    f1 = tmp_path / "doc1.txt"
    f2 = tmp_path / "doc2.txt"
    f1.write_bytes(b"branch-coverage-payload")
    f2.write_bytes(b"branch-coverage-payload")
    assert len(raw_file_hash(f1, algorithm="md5")) == 32

    orig_real, rem_real = select_original_from_cluster(
        [str(f1), str(f2)], mode="created"
    )
    assert orig_real in {str(f1), str(f2)}
    assert len(rem_real) == 1
    orig_rev, _ = select_original_from_cluster([str(f1), str(f2)], mode="-created")
    assert orig_rev in {str(f1), str(f2)}

    with pytest.raises(ValueError, match="empty cluster"):
        select_original_from_cluster([], mode="first_seen")
    with pytest.raises(ValueError, match="Unsupported detect_original_by"):
        group_byte_identical_files([str(f1)], detect_original_by="bad_selector")

    symlink_p = tmp_path / "doc_link.txt"
    symlink_p.symlink_to(f1)
    clusters = group_byte_identical_files(
        ["", str(f1), str(f1), str(symlink_p), str(tmp_path / "missing.txt"), str(f2)],
        detect_original_by="first_seen",
    )
    assert len(clusters) == 1
    assert len(clusters[0]["paths"]) == 2

    # 2. duplicate_staging_relpath external path & distinct source_root
    base_dir = tmp_path / "session_base"
    base_dir.mkdir()
    ext_root = tmp_path / "external_root"
    ext_root.mkdir()
    ext_file = ext_root / "sub" / "ext.txt"
    ext_file.parent.mkdir()
    ext_file.write_text("external")

    rel_escaped = duplicate_staging_relpath(ext_file, base_dir)
    assert rel_escaped.startswith("_ext_")
    rel_multi_root = duplicate_staging_relpath(ext_file, base_dir, source_root=ext_root)
    assert rel_multi_root.startswith("_root_")

    # 3. send_to_os_trash nonexistent path
    ok_missing, err_missing = send_to_os_trash(tmp_path / "does_not_exist.bin")
    assert ok_missing is False
    assert err_missing is not None and "does not exist" in err_missing

    # 4. Stationary byte-identical destination file + apply_deduplication_selections modes
    stationary_dir = base_dir / "Archive"
    stationary_dir.mkdir()
    stationary_target = stationary_dir / "master.bin"
    stationary_target.write_bytes(b"stationary-identical-bytes")

    incoming_dup = base_dir / "incoming_copy.bin"
    incoming_dup.write_bytes(b"stationary-identical-bytes")

    pair_a = base_dir / "pair_a.bin"
    pair_b = base_dir / "pair_b.bin"
    pair_a.write_bytes(b"pair-identical-bytes")
    pair_b.write_bytes(b"pair-identical-bytes")

    plan: dict[str, dict] = {
        "Archive": {
            "incoming_copy.bin": {
                "node_type": "file",
                "relative_source": "incoming_copy.bin",
                "target_filename": "master.bin",
                "is_checked": True,
            },
            "pair_a.bin": {
                "node_type": "file",
                "relative_source": "pair_a.bin",
                "target_filename": "pair_a.bin",
                "is_checked": True,
            },
            "pair_b.bin": {
                "node_type": "file",
                "relative_source": "pair_b.bin",
                "target_filename": "pair_b.bin",
                "is_checked": True,
            },
        }
    }

    dup_groups = VerificationEngine.get_duplicate_groups(
        str(base_dir), plan, detect_original_by="name"
    )
    assert len(dup_groups) == 2
    stationary_group = next(g for g in dup_groups if g["existing_file_on_disk"])
    cluster_group = next(g for g in dup_groups if not g["existing_file_on_disk"])

    for action_mode in ("trash", "keep", "rename", "keep_both", "overwrite", "skip"):
        VerificationEngine.apply_deduplication_selections(
            str(base_dir),
            plan,
            {
                stationary_group["group_id"]: action_mode,
                cluster_group["group_id"]: action_mode,
            },
            groups=dup_groups,
        )
    assert plan["Archive"]["incoming_copy.bin"]["is_excluded"] is True

    # 5. HistoryManager.flush_staged_duplicates with max_age_days and base_dir lookup from DB
    store_dir = tmp_path / "_hist_store"
    with mock.patch.dict(os.environ, {"AUTOSORTER_APP_DIR": str(store_dir)}):
        settings = AppSettings()
        settings.override("AI_CONSENT_GRANTED", False)
        settings.override("DUPLICATE_ACTION", "stage")
        sort_root = tmp_path / "hist_sort_root"
        sort_root.mkdir()
        (sort_root / "x1.txt").write_bytes(b"hist-dup-bytes")
        (sort_root / "x2.txt").write_bytes(b"hist-dup-bytes")

        sess = AppSession(settings, base_dir=str(sort_root))
        try:
            pipe = SortPipeline(sess)
            _, m_res = pipe.run()
            assert m_res is not None and m_res.ok

            # max_age_days=30 keeps freshly staged duplicates untouched
            retained = sess.history_manager.flush_staged_duplicates(
                session_id=None,
                base_dir=str(sort_root),
                max_age_days=30.0,
                trash_fn=lambda p: os.remove(p),
            )
            assert retained["exported"] == []
            assert len(retained["kept_staged"]) == 1

            # Resolving base_dir from SQLite when base_dir=None exports cleanly
            flushed = sess.history_manager.flush_staged_duplicates(
                session_id=m_res.session_id,
                base_dir=None,
                trash_fn=lambda p: os.remove(p),
            )
            assert len(flushed["exported"]) == 1
        finally:
            sess.close()
