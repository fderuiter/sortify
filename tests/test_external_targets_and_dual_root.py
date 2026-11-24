"""Tests for allowlisted external targets, dual-root sessions, rule import preservation, and multi-root locking."""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import AppSettings
from app.core.daemon import ContinuousWatchdogDaemon
from app.core.folder_lock import (
    FolderBusyError,
    FolderLock,
    canonical_folder_lock_key,
    collect_plan_external_roots,
    lock_multiple_folders,
)
from app.core.path_utils import (
    _lexical_abs_subpath,
    _sanitize_plan_with_base,
    expand_target_path,
    is_allowed_external_target,
    is_external_subpath,
    is_protected_target_path,
    is_system_protected_path,
    validate_target_path,
)
from app.core.pipeline import PipelineLifecycleState, SortPipeline
from app.core.rule_importers import DropItRuleImporter, HazelRuleImporter
from app.core.scanner import get_files_recursively
from app.core.session import AppSession
from app.core.verifier import VerificationEngine


def _isolated_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AppSettings:
    app_dir = tmp_path / ".autosorter"
    app_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SORTIFY_APP_DIR", str(app_dir))
    monkeypatch.setenv("NON_INTERACTIVE", "1")
    settings = AppSettings(filepath=str(app_dir / "settings.json"))
    settings.AI_CONSENT_GRANTED = False
    settings.CLEANUP_EMPTY_FOLDERS = True
    return settings


def test_tilde_expansion_and_allowlist_validation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify home shortcut expansion, allowlisted external roots, symlink escape blocking, and protected path rejection."""
    fake_home = tmp_path / "home_user"
    screenshots_root = fake_home / "Pictures" / "Screenshots"
    protected_sub = screenshots_root / "Vault"
    outside_dir = tmp_path / "outside_secret"
    screenshots_root.mkdir(parents=True)
    protected_sub.mkdir(parents=True)
    outside_dir.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    settings = _isolated_settings(monkeypatch, tmp_path)
    settings.PROTECTED_PATHS = [str(protected_sub)]
    settings.ALLOWED_EXTERNAL_ROOTS = ["~/Pictures/Screenshots"]

    assert settings.ALLOWED_EXTERNAL_ROOTS == [str(screenshots_root)]
    assert expand_target_path("~/Pictures/Screenshots/2026") == str(
        screenshots_root / "2026"
    )

    settings.KEYWORD_RULES = {"screenshot": "~/Pictures/Screenshots/2026"}
    assert settings.KEYWORD_RULES["screenshot"] == str(screenshots_root / "2026")

    # Reject unallowlisted external path and keep previous state intact
    with pytest.raises(ValidationError):
        settings.KEYWORD_RULES = {"bad": str(outside_dir)}
    assert settings.KEYWORD_RULES == {"screenshot": str(screenshots_root / "2026")}

    # Reject protected path even when nested inside an allowlisted external root
    assert is_protected_target_path(
        str(protected_sub / "sub"), settings.PROTECTED_PATHS
    )
    with pytest.raises(ValueError, match="protected"):
        validate_target_path(
            str(protected_sub / "sub"),
            allowed_external_roots=settings.ALLOWED_EXTERNAL_ROOTS,
            protected_paths=settings.PROTECTED_PATHS,
        )

    # Reject symlink inside allowlisted root pointing outside allowlist
    escape_link = screenshots_root / "escape_sym"
    escape_link.symlink_to(outside_dir, target_is_directory=True)
    assert not is_allowed_external_target(
        str(escape_link / "loot"), settings.ALLOWED_EXTERNAL_ROOTS
    )
    with pytest.raises(ValueError):
        validate_target_path(
            str(escape_link / "loot"),
            allowed_external_roots=settings.ALLOWED_EXTERNAL_ROOTS,
        )


def test_external_rule_cross_volume_move_and_undo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify external rule routing, simulated cross-volume verified move, and undo restoration + external dir pruning."""
    import app.core.mover as mover_mod

    settings = _isolated_settings(monkeypatch, tmp_path)
    inbox = tmp_path / "Inbox"
    inbox_sub = inbox / "Batch1"
    ext_root = tmp_path / "ExternalArchive"
    inbox_sub.mkdir(parents=True)
    ext_root.mkdir(parents=True)

    src_file = inbox_sub / "invoice_q4.txt"
    src_file.write_text("Quarterly invoice payment summary 2026", encoding="utf-8")

    settings.ALLOWED_EXTERNAL_ROOTS = [str(ext_root)]
    settings.KEYWORD_RULES = {"invoice": str(ext_root / "Finance" / "Invoices")}

    # Simulate cross-volume copy+fsync+SHA256 verification path
    monkeypatch.setattr(mover_mod, "_is_cross_volume", lambda _s, _d: True)

    session = AppSession(settings, base_dir=str(inbox))
    try:
        pipeline = SortPipeline(session)
        files = get_files_recursively(str(inbox))
        plan = pipeline.plan(files)
        res = pipeline.execute(plan)

        moved_target = ext_root / "Finance" / "Invoices" / "invoice_q4.txt"
        assert moved_target.is_file()
        assert not src_file.exists()
        assert not inbox_sub.exists()  # emptied source subfolder pruned
        assert inbox.is_dir()  # source root preserved

        # Undo restores file to original source subfolder and prunes emptied external folders
        assert res.session_id is not None
        undo_res = pipeline.undo(res.session_id)
        assert undo_res.rollback_completed is True
        assert len(undo_res.moved) == 1
        assert src_file.is_file()
        assert (
            src_file.read_text(encoding="utf-8")
            == "Quarterly invoice payment summary 2026"
        )
        assert not moved_target.exists()
        assert not (ext_root / "Finance").exists()
        assert ext_root.is_dir()  # allowlisted external root itself is never deleted
    finally:
        session.close()


def test_verifier_blocks_unapproved_external_paths_and_allows_approved(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify VerificationEngine flags unallowlisted external targets as critical errors and permits allowlisted ones."""
    settings = _isolated_settings(monkeypatch, tmp_path)
    inbox = tmp_path / "Inbox"
    approved_ext = tmp_path / "ApprovedExt"
    unapproved_ext = tmp_path / "UnapprovedExt"
    inbox.mkdir()
    approved_ext.mkdir()
    unapproved_ext.mkdir()
    (inbox / "doc.txt").write_text("hello world", encoding="utf-8")

    settings.ALLOWED_EXTERNAL_ROOTS = [str(approved_ext)]

    bad_plan = {
        str(unapproved_ext / "Sub"): {
            "doc.txt": {"__type__": "file", "source_path": str(inbox / "doc.txt")}
        }
    }
    bad_report = VerificationEngine.verify_plan_integrity(
        str(inbox), bad_plan, settings=settings
    )
    assert bad_report["success"] is False
    assert bad_report["has_critical_errors"] is True
    assert len(bad_report["escaping_paths"]) == 1
    assert "ALLOWED_EXTERNAL_ROOTS" in bad_report["escaping_paths"][0]["message"]

    good_plan = {
        str(approved_ext / "Sub"): {
            "doc.txt": {"__type__": "file", "source_path": str(inbox / "doc.txt")}
        }
    }
    good_report = VerificationEngine.verify_plan_integrity(
        str(inbox), good_plan, settings=settings
    )
    assert good_report["escaping_paths"] == []
    assert good_report["has_critical_errors"] is False

    # Sanitizer expands tilde and preserves trusted external roots
    monkeypatch.setenv("HOME", str(approved_ext))
    monkeypatch.setenv("USERPROFILE", str(approved_ext))
    sanitized, _warns = _sanitize_plan_with_base(
        {
            "~/Sub": {
                "doc.txt": {"__type__": "file", "source_path": str(inbox / "doc.txt")}
            }
        },
        base_dir=str(inbox),
        trusted_roots=settings.ALLOWED_EXTERNAL_ROOTS,
    )
    assert str(approved_ext / "Sub") in sanitized


def test_importers_preserve_external_targets_opt_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify Hazel and DropIt importers strip external paths by default and preserve allowlisted roots on opt-in."""
    settings = _isolated_settings(monkeypatch, tmp_path)
    allowed_drive = tmp_path / "AllowedDrive"
    unallowed_drive = tmp_path / "OtherDrive"
    allowed_drive.mkdir()
    unallowed_drive.mkdir()
    settings.ALLOWED_EXTERNAL_ROOTS = [str(allowed_drive)]

    # DropIt INI
    ini_file = tmp_path / "rules.ini"
    ini_file.write_text(
        "[DropIt]\n"
        "[Association_1]\n"
        "Name=Invoices\n"
        "Rules=*invoice*\n"
        "Action=Move\n"
        f"Destination={allowed_drive / 'Billing'}\n"
        "[Association_2]\n"
        "Name=Other\n"
        "Rules=*receipt*\n"
        "Action=Move\n"
        f"Destination={unallowed_drive / 'Receipts'}\n",
        encoding="utf-8",
    )

    dropit = DropItRuleImporter()
    res_default = dropit.parse_file(ini_file, settings=settings)
    pol_targets_default = {
        p["expression"]: p["target_path"] for p in res_default.policies
    }
    assert not pol_targets_default["*invoice*"].startswith("/")
    assert pol_targets_default["*invoice*"].endswith("AllowedDrive/Billing")

    res_preserve = dropit.parse_file(
        ini_file, preserve_external_targets=True, settings=settings
    )
    pol_targets_preserve = {
        p["expression"]: p["target_path"] for p in res_preserve.policies
    }
    assert pol_targets_preserve["*invoice*"] == str(allowed_drive / "Billing")
    assert "*receipt*" not in pol_targets_preserve
    assert any("ALLOWED_EXTERNAL_ROOTS" in w for w in res_preserve.warnings)

    # Hazel plist
    hazel_file = tmp_path / "sample.hazelrules"
    hazel_payload = [
        {
            "Name": "Screenshots",
            "Conditions": [
                {"Attribute": "Name", "Operator": "contains", "Value": "screenshot"}
            ],
            "Actions": [
                {
                    "Action": "NSRuleActionMove",
                    "Destination": str(allowed_drive / "Screenshots"),
                }
            ],
        }
    ]
    hazel_file.write_bytes(plistlib.dumps(hazel_payload))
    hazel = HazelRuleImporter()
    hz_res = hazel.parse_file(
        hazel_file, preserve_external_targets=True, settings=settings
    )
    assert hz_res.keyword_rules["screenshot"] == str(allowed_drive / "Screenshots")


def test_dual_root_session_nested_dest_dir_and_undo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify dual-root session keeps session.base_dir intact, excludes nested dest_dir, and supports full undo."""
    settings = _isolated_settings(monkeypatch, tmp_path)
    inbox = tmp_path / "Downloads"
    nested_dest = inbox / "SortedOutput"
    inbox_sub = inbox / "Incoming"
    inbox_sub.mkdir(parents=True)
    nested_dest.mkdir(parents=True)

    existing_sorted = nested_dest / "already_sorted.txt"
    existing_sorted.write_text("do not rescan", encoding="utf-8")
    incoming_doc = inbox_sub / "budget_report.txt"
    incoming_doc.write_text("annual finance budget forecast report", encoding="utf-8")

    settings.SCAN_MAX_DEPTH = 5
    settings.KEYWORD_RULES = {"budget": "Finance"}

    # Scanner excludes nested dest_dir to prevent feedback loops
    scanned = get_files_recursively(str(inbox), dest_dir=str(nested_dest))
    assert any("budget_report.txt" in f for f in scanned)
    assert not any("already_sorted.txt" in f for f in scanned)

    # Daemon ignores events inside nested dest_dir
    daemon = ContinuousWatchdogDaemon(
        settings=settings, base_dir=str(inbox), dest_dir=str(nested_dest)
    )
    assert daemon.should_ignore_path(str(existing_sorted)) is True
    assert daemon.should_ignore_path(str(incoming_doc)) is False

    session = AppSession(settings, base_dir=str(inbox), dest_dir=str(nested_dest))
    try:
        assert session.base_dir == str(inbox)
        assert session.dest_dir == str(nested_dest)
        pipeline = SortPipeline(session)
        plan = pipeline.plan(scanned)
        assert session.base_dir == str(inbox)
        result = pipeline.execute(plan)
        assert session.base_dir == str(inbox)

        landed = nested_dest / "Finance" / "budget_report.txt"
        assert landed.is_file()
        assert not incoming_doc.exists()
        assert not inbox_sub.exists()

        assert result.session_id is not None
        pipeline.undo(result.session_id)
        assert incoming_doc.is_file()
        assert not landed.exists()
        assert not (nested_dest / "Finance").exists()
        assert nested_dest.is_dir()
        assert existing_sorted.is_file()
    finally:
        session.close()


def test_multi_root_folder_lock_canonical_order_and_contention(tmp_path: Path) -> None:
    """Verify lock_multiple_folders deduplicates symlink aliases, orders canonically, and blocks concurrent contention."""
    root_b = tmp_path / "z_source"
    root_a = tmp_path / "a_dest"
    root_ext = tmp_path / "m_external"
    lock_dir = tmp_path / "locks"
    for d in (root_a, root_b, root_ext, lock_dir):
        d.mkdir()
    alias_to_a = tmp_path / "alias_dest"
    alias_to_a.symlink_to(root_a, target_is_directory=True)

    assert canonical_folder_lock_key(str(alias_to_a)) == canonical_folder_lock_key(
        str(root_a)
    )

    plan = {
        str(root_ext / "Invoices"): {
            "a.txt": {"__type__": "file", "source_path": str(root_b / "a.txt")}
        }
    }
    ext_roots = collect_plan_external_roots(
        plan, allowed_external_roots=[str(root_ext)]
    )
    assert ext_roots == [str(root_ext)]

    # Acquiring reversed roots + symlink alias succeeds without self-deadlock
    with lock_multiple_folders(
        [str(root_b), str(alias_to_a), str(root_a), *ext_roots],
        timeout=0.5,
        lock_dir=lock_dir,
    ):
        # Contending lock on external root fails immediately
        with pytest.raises(FolderBusyError):
            with FolderLock(str(root_ext), timeout=0.05, lock_dir=lock_dir):
                pass


def test_sensitivity_hold_stays_in_source_quarantine_and_undo_verifies_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify sensitivity holds remain in source workspace quarantine and undo refuses tampered external files."""
    settings = _isolated_settings(monkeypatch, tmp_path)
    inbox = tmp_path / "SourceWorkspace"
    dest_root = tmp_path / "DestRoot"
    ext_root = tmp_path / "ExternalVault"
    inbox.mkdir()
    dest_root.mkdir()
    ext_root.mkdir()

    settings.ALLOWED_EXTERNAL_ROOTS = [str(ext_root)]
    settings.AUTO_QUARANTINE_RATINGS = ["RESTRICTED"]

    sensitive_file = inbox / "payroll_restricted.txt"
    sensitive_file.write_text("Restricted payroll data", encoding="utf-8")
    normal_file = inbox / "memo.txt"
    normal_file.write_text("General office memo", encoding="utf-8")

    session = AppSession(settings, base_dir=str(inbox), dest_dir=str(dest_root))
    try:
        pipeline = SortPipeline(session)
        plan = {
            str(ext_root / "Payroll"): {
                "payroll_restricted.txt": {
                    "__type__": "file",
                    "source_path": str(sensitive_file),
                    "sensitivity_rating": "RESTRICTED",
                }
            },
            str(ext_root / "Memos"): {
                "memo.txt": {
                    "__type__": "file",
                    "source_path": str(normal_file),
                }
            },
        }
        session.transition_to(PipelineLifecycleState.PLANNED)
        pipeline.transition_to(PipelineLifecycleState.PLANNED)
        res = pipeline.execute(plan)
        # Sensitive file stayed inside source workspace quarantine, NOT external root or dest_dir
        quarantined = inbox / "_Compliance_Quarantine" / "payroll_restricted.txt"
        assert quarantined.is_file()
        assert not (ext_root / "Payroll" / "payroll_restricted.txt").exists()
        assert not (
            dest_root / "_Compliance_Quarantine" / "payroll_restricted.txt"
        ).exists()

        moved_memo = ext_root / "Memos" / "memo.txt"
        assert moved_memo.is_file()

        # Tamper with external file bytes -> undo detects hash mismatch and preserves tampered file
        moved_memo.write_text("Tampered external content!", encoding="utf-8")
        assert res.session_id is not None
        undo_res = pipeline.undo(res.session_id)
        assert undo_res.rollback_completed is False
        assert moved_memo.is_file()
        assert moved_memo.read_text(encoding="utf-8") == "Tampered external content!"
    finally:
        session.close()


def test_tui_modals_and_unapproved_external_ctrl_e_blocked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify TUI SettingsModal/RuleImportModal expose external controls and Ctrl+E blocks unapproved external targets cleanly."""
    import asyncio

    from textual.widgets import Checkbox, Input, Static

    from app.ui.modals.rules import RuleImportModal
    from app.ui.modals.settings import SettingsModal
    from app.ui.tui import AutoSorterTUI

    settings = _isolated_settings(monkeypatch, tmp_path)
    inbox = tmp_path / "TuiInbox"
    unapproved_ext = tmp_path / "UnapprovedBackup"
    inbox.mkdir()
    unapproved_ext.mkdir()
    doc = inbox / "notes.txt"
    doc.write_text("project meeting notes", encoding="utf-8")

    async def _exercise_tui() -> None:
        app = AutoSorterTUI(
            base_dir=str(inbox),
            settings=settings,
            skip_wizard=True,
            non_interactive=True,
        )
        async with app.run_test(size=(110, 36)) as pilot:
            # Stage an unapproved external destination in plan and run validation update
            app.plan = {
                str(unapproved_ext / "Docs"): {
                    "notes.txt": {
                        "__type__": "file",
                        "source_path": str(doc),
                        "relative_source": "notes.txt",
                    }
                }
            }
            report = VerificationEngine.verify_plan_integrity(
                str(inbox), app.plan, settings=settings
            )
            app._update_validation_state(report)
            await pilot.pause()

            banner_text = str(app.query_one("#alert-banner", Static).render())
            assert (
                "UNAPPROVED EXTERNAL" in banner_text
                or "ALLOWED_EXTERNAL_ROOTS" in banner_text
            )

            # Pressing Ctrl+E (action_execute_sort) blocks cleanly without raising ValueError
            app.action_execute_sort()
            await pilot.pause()
            assert doc.is_file()
            assert not (unapproved_ext / "Docs" / "notes.txt").exists()

            # Verify SettingsModal renders #input-dest-dir and #input-external-roots
            settings_modal = SettingsModal(settings)
            app.push_screen(settings_modal)
            await pilot.pause()
            assert settings_modal.query_one("#input-dest-dir", Input) is not None
            assert settings_modal.query_one("#input-external-roots", Input) is not None
            app.pop_screen()
            await pilot.pause()

            # Verify RuleImportModal renders #chk-preserve-external-targets (unchecked by default)
            import_modal = RuleImportModal(settings)
            app.push_screen(import_modal)
            await pilot.pause()
            chk = import_modal.query_one("#chk-preserve-external-targets", Checkbox)
            assert chk.value is False
            app.pop_screen()
            await pilot.pause()

    asyncio.run(_exercise_tui())


def test_cross_platform_path_utils_and_protected_guards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercise POSIX, Windows drive, UNC, tilde, reserved-name, traversal, and system-protected branches."""
    fake_home = tmp_path / "win_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    assert expand_target_path("~") == str(fake_home)
    assert expand_target_path("~\\Pictures\\Snaps").startswith(str(fake_home))
    assert expand_target_path("relative/sub") == "relative/sub"

    # Cross-platform lexical & external subpath checks (Windows drive & UNC)
    assert _lexical_abs_subpath(
        r"D:\Archive\Invoices\2026", r"D:\Archive", allow_equal=False
    )
    assert not _lexical_abs_subpath(r"D:\Archive", r"D:\Archive", allow_equal=False)
    assert not _lexical_abs_subpath(r"D:\Archive\..\Secret", r"D:\Archive")
    assert not _lexical_abs_subpath(r"D:\Archive\.\Sub", r"D:\Archive")
    assert not _lexical_abs_subpath("\\\\", r"\\server\share")
    assert is_external_subpath(
        r"\\nas01\finance\Invoices", r"\\NAS01\Finance", allow_equal=False
    )
    assert not is_external_subpath("relative/path", r"D:\Archive")
    assert not is_allowed_external_target("relative/path", [r"D:\Archive"])
    assert not is_allowed_external_target(r"D:\Archive\Sub", None)
    assert is_allowed_external_target(
        r"D:\Archive\Sub", ["", r"D:\Archive", r"\\nas01\share"]
    )
    assert is_allowed_external_target(r"\\nas01\share\Reports", [r"\\nas01\share"])

    # Built-in system protected paths and relative + base_dir protected checks
    assert is_system_protected_path("/")
    assert is_system_protected_path(r"C:\\")
    assert is_system_protected_path("\\\\")
    assert is_system_protected_path("/etc/ssh")
    assert is_system_protected_path(r"C:\Windows\System32\drivers")
    assert not is_system_protected_path("")
    assert not is_system_protected_path("relative/folder")
    assert is_protected_target_path(
        "Vault/Sub",
        protected_paths=[str(tmp_path / "ws" / "Vault")],
        base_dir=str(tmp_path / "ws"),
    )

    # Validation rejections across malformed / unsafe targets
    for bad_target in (
        "bad\x00null",
        "../escape",
        "sub/~/tilde",
        "C:",
        "D:relative",
        r"D:\Bad<Char>",
        "/Volumes/Archive/./Dot",
        "\\\\onlyserver",
        "/",
        "/Volumes/Archive/Trailing. ",
        "/Volumes/Archive/CON",
        "/etc/passwd",
        "Relative/Bad?Char",
        "Relative/Trailing.",
        "Relative/NUL.txt",
    ):
        with pytest.raises(ValueError):
            validate_target_path(
                bad_target,
                allowed_external_roots=[
                    "/Volumes/Archive",
                    r"D:\Archive",
                    r"\\nas01\share",
                ],
            )

    # Valid Windows drive & UNC targets pass when allowlisted
    validate_target_path(
        r"D:\Archive\Invoices",
        allowed_external_roots=[r"D:\Archive"],
    )
    validate_target_path(
        r"\\nas01\share\Invoices",
        allowed_external_roots=[r"\\nas01\share"],
    )


def test_mover_expands_raw_tilde_without_literal_tilde_subfolder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify AsyncMoveEngine expands raw '~/Pictures/Screenshots' in plans without creating 'Downloads/~/...'."""
    from app.core.mover import AsyncMoveEngine

    fake_home = tmp_path / "user_home"
    screenshots = fake_home / "Pictures" / "Screenshots"
    downloads = tmp_path / "Downloads"
    screenshots.mkdir(parents=True)
    downloads.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    settings = _isolated_settings(monkeypatch, tmp_path)
    settings.ALLOWED_EXTERNAL_ROOTS = [str(screenshots)]

    snap = downloads / "screen_01.png"
    snap.write_bytes(b"\x89PNG\r\n\x1a\nfake_png_payload")

    raw_tilde_plan = {
        "~/Pictures/Screenshots": {
            "screen_01.png": {
                "__type__": "file",
                "source_path": str(snap),
                "relative_source": "screen_01.png",
            }
        }
    }

    session = AppSession(settings, base_dir=str(downloads))
    try:
        summary = AsyncMoveEngine().execute(
            str(downloads),
            raw_tilde_plan,
            session.db,
            session.history_manager,
            runtime_settings=settings,
        )
        assert not summary.get("error")
        assert (screenshots / "screen_01.png").is_file()
        assert not (downloads / "~").exists()
        assert not snap.exists()
    finally:
        session.close()


def test_dual_root_multi_source_prunes_updates_documents_and_records_history(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify dual-root multi-source sort prunes source_dirs, updates documents in DB, records source base_dir, and undoes."""
    from app.core.scanner import get_files_from_sources

    settings = _isolated_settings(monkeypatch, tmp_path)
    src_a = tmp_path / "SourceA"
    src_b = tmp_path / "SourceB"
    sub_a = src_a / "NestedA"
    sub_b = src_b / "NestedB"
    dest_root = tmp_path / "CentralDest"
    for d in (sub_a, sub_b, dest_root):
        d.mkdir(parents=True)

    file_a = sub_a / "invoice_alpha.txt"
    file_b = sub_b / "invoice_beta.txt"
    file_a.write_text("Alpha invoice 2026", encoding="utf-8")
    file_b.write_text("Beta invoice 2026", encoding="utf-8")

    settings.SCAN_MAX_DEPTH = 4
    settings.KEYWORD_RULES = {"invoice": "Billing"}

    session = AppSession(
        settings,
        base_dir=str(src_a),
        source_dirs=[str(src_a), str(src_b)],
        dest_dir=str(dest_root),
    )
    try:
        pipeline = SortPipeline(session, dest_dir=str(dest_root))
        items = get_files_from_sources(
            [str(src_a), str(src_b)], settings=settings, dest_dir=str(dest_root)
        )
        plan = pipeline.plan(items, dest_dir=str(dest_root))
        res = pipeline.execute(plan)
        assert res.ok
        assert res.session_id is not None

        # Both files moved to dest_root/Billing, and both emptied source subfolders pruned
        dst_a = dest_root / "Billing" / "invoice_alpha.txt"
        dst_b = dest_root / "Billing" / "invoice_beta.txt"
        assert dst_a.is_file()
        assert dst_b.is_file()
        assert not sub_a.exists()
        assert not sub_b.exists()
        assert src_a.is_dir()
        assert src_b.is_dir()

        # History records source root (src_a) in sessions.base_dir, NOT dest_root
        sessions = session.history_manager.get_sessions()
        matching = [s for s in sessions if s["session_id"] == res.session_id]
        assert len(matching) == 1
        assert matching[0]["base_dir"] == str(src_a)

        # Database documents table updated for moved files under their respective source roots
        doc_a_moved = session.db.get_document(str(src_a), str(dst_a).replace("\\", "/"))
        doc_b_moved = session.db.get_document(str(src_b), str(dst_b).replace("\\", "/"))
        assert doc_a_moved is not None
        assert doc_b_moved is not None

        # Undo restores both files to their original source subfolders and updates DB back
        undo_res = pipeline.undo(res.session_id)
        assert undo_res.rollback_completed is True
        assert file_a.is_file()
        assert file_b.is_file()
        assert not dst_a.exists()
        assert not dst_b.exists()
        assert (
            session.db.get_document(str(src_a), "NestedA/invoice_alpha.txt") is not None
        )
    finally:
        session.close()


def test_cli_dest_dir_and_rules_import_preserve_external(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify --dest-dir across sort --tui, scan, daemon, and rules import --preserve-external-targets."""
    import json

    import app.core.daemon as daemon_mod
    import app.main as main_mod
    import app.ui.tui as tui_mod
    from app.cli.rules_cli import handle_rules_command

    settings = _isolated_settings(monkeypatch, tmp_path)
    inbox = tmp_path / "CliInbox"
    dest_root = tmp_path / "CliDest"
    ext_drive = tmp_path / "CliExternalDrive"
    for d in (inbox, dest_root, ext_drive):
        d.mkdir()
    (inbox / "tax_2026.txt").write_text("annual tax filing receipt", encoding="utf-8")
    settings.ALLOWED_EXTERNAL_ROOTS = [str(ext_drive)]
    settings.KEYWORD_RULES = {"tax": "Taxes"}

    parser = main_mod.build_parser()

    # 1. sort --tui --dest-dir forwards dest_dir to run_tui
    captured_tui: dict[str, object] = {}

    def _fake_run_tui(s: object, initial_dir: object = None, **kwargs: object) -> None:
        captured_tui["initial_dir"] = initial_dir
        captured_tui.update(kwargs)

    monkeypatch.setattr(tui_mod, "run_tui", _fake_run_tui)
    args_sort_tui = parser.parse_args(
        ["sort", str(inbox), "--tui", "--dest-dir", str(dest_root)]
    )
    with pytest.raises(SystemExit) as exc_info:
        main_mod.handle_sort_command(args_sort_tui, settings)
    assert exc_info.value.code == 0
    assert captured_tui.get("dest_dir") == str(dest_root)

    # 2. scan --dest-dir --json includes destination_directory
    args_scan = parser.parse_args(
        ["scan", str(inbox), "--dest-dir", str(dest_root), "--json"]
    )
    with pytest.raises(SystemExit) as scan_exc:
        main_mod.handle_scan_command(args_scan, settings)
    assert scan_exc.value.code == 0
    scan_out = json.loads(capsys.readouterr().out)
    assert scan_out["destination_directory"] == str(dest_root)

    # 3. daemon start --dest-dir forwards dest_dir to start_daemon
    captured_daemon: dict[str, object] = {}

    def _fake_start_daemon(
        s: object, base_dir: str | None = None, dest_dir: str | None = None
    ) -> None:
        captured_daemon["base_dir"] = base_dir
        captured_daemon["dest_dir"] = dest_dir

    monkeypatch.setattr(daemon_mod, "start_daemon", _fake_start_daemon)
    args_daemon = parser.parse_args(
        ["daemon", "start", str(inbox), "--dest-dir", str(dest_root)]
    )
    main_mod.handle_daemon_command(args_daemon, settings)
    assert captured_daemon["dest_dir"] == str(dest_root)

    # 4. rules import --preserve-external-targets --enable --json preserves allowlisted external target
    ini_path = tmp_path / "cli_import.ini"
    ini_path.write_text(
        "[DropIt]\n"
        "[Association_1]\n"
        "Name=ExternalContracts\n"
        "Rules=*contract*\n"
        "Action=Move\n"
        f"Destination={ext_drive / 'Contracts'}\n",
        encoding="utf-8",
    )
    args_rules = parser.parse_args(
        [
            "rules",
            "import",
            str(ini_path),
            "--preserve-external-targets",
            "--enable",
            "--json",
        ]
    )
    with pytest.raises(SystemExit) as rules_exc:
        handle_rules_command(args_rules, settings)
    assert rules_exc.value.code == 0
    rules_out = json.loads(capsys.readouterr().out)
    assert rules_out["preserve_external_targets"] is True
    assert any(
        p["target_path"] == str(ext_drive / "Contracts") for p in settings.POLICIES
    )


def test_interactive_rule_edit_and_reassign_modals(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Verify RuleEditModal, ReassignModal, and RenameModal handle tilde expansion, allowlists, and protected paths."""
    import asyncio

    from textual.widgets import Input

    from app.ui.modals.edit import ReassignModal, RenameModal
    from app.ui.modals.rules import RuleEditModal
    from app.ui.tui import AutoSorterTUI

    fake_home = tmp_path / "modal_home"
    allowed_pics = fake_home / "Pictures" / "Screenshots"
    protected_dir = tmp_path / "ProtectedVault"
    inbox = tmp_path / "ModalInbox"
    for d in (allowed_pics, protected_dir, inbox):
        d.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    settings = _isolated_settings(monkeypatch, tmp_path)
    settings.PROTECTED_PATHS = [str(protected_dir)]
    settings.ALLOWED_EXTERNAL_ROOTS = [str(allowed_pics)]

    async def _exercise_modals() -> None:
        app = AutoSorterTUI(
            base_dir=str(inbox),
            settings=settings,
            skip_wizard=True,
            non_interactive=True,
        )
        async with app.run_test(size=(110, 36)) as pilot:
            saved_rules: list[object] = []

            # RuleEditModal rejects protected path and traversal, then accepts '~/Pictures/Screenshots/UI'
            rule_modal = RuleEditModal(title="Add Rule", settings=settings)
            app.push_screen(rule_modal, saved_rules.append)
            await pilot.pause()

            rule_modal.query_one("#input-rule-pattern", Input).value = "screen"
            rule_modal.query_one("#input-rule-target", Input).value = str(protected_dir)
            rule_modal.action_save()
            await pilot.pause()
            assert saved_rules == []  # still open, rejected protected path

            rule_modal.query_one("#input-rule-target", Input).value = "../escape"
            rule_modal.action_save()
            await pilot.pause()
            assert saved_rules == []  # rejected traversal

            rule_modal.query_one(
                "#input-rule-target", Input
            ).value = "~/Pictures/Screenshots/UI"
            rule_modal.action_save()
            await pilot.pause()
            assert len(saved_rules) == 1
            assert saved_rules[0] == {
                "pattern": "screen",
                "target": str(allowed_pics / "UI"),
            }

            # ReassignModal blocks protected paths, expands tilde on confirm
            reassigned: list[object] = []
            reassign_modal = ReassignModal(
                existing_folders=[],
                count=1,
                base_dir=str(inbox),
                settings=settings,
            )
            app.push_screen(reassign_modal, reassigned.append)
            await pilot.pause()

            reassign_modal.query_one("#input-target-folder", Input).value = str(
                protected_dir
            )
            reassign_modal.action_confirm()
            await pilot.pause()
            assert reassigned == []

            reassign_modal.query_one(
                "#input-target-folder", Input
            ).value = "~/Pictures/Screenshots/Batch"
            reassign_modal.action_confirm()
            await pilot.pause()
            assert reassigned == [str(allowed_pics / "Batch")]

            # RenameModal expands tilde destination override cleanly
            renamed: list[object] = []
            rename_modal = RenameModal(
                title="Rename File",
                current_name="clip",
                extension=".png",
                base_dir=str(inbox),
                settings=settings,
            )
            app.push_screen(rename_modal, renamed.append)
            await pilot.pause()
            rename_modal.query_one(
                "#input-destination", Input
            ).value = "~/Pictures/Screenshots"
            rename_modal.action_confirm()
            await pilot.pause()
            assert len(renamed) == 1
            res_obj = renamed[0]
            assert getattr(res_obj, "destination_override", None) == str(allowed_pics)

    asyncio.run(_exercise_modals())


def test_settings_modal_save_rejects_traversal_and_expands_tilde_and_drop_preserves_base_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Verify SettingsModal rejects '..' traversal, expands '~', and drop --dest-dir records source base_dir."""
    import asyncio
    import json

    from textual.widgets import Input

    import app.main as main_mod
    from app.ui.modals.settings import SettingsModal
    from app.ui.tui import AutoSorterTUI

    fake_home = tmp_path / "settings_home"
    sorted_root = fake_home / "SortedRoot"
    ext_archive = fake_home / "ExtArchive"
    inbox = tmp_path / "DropInbox"
    nested = inbox / "Sub"
    for d in (sorted_root, ext_archive, nested):
        d.mkdir(parents=True)

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    settings = _isolated_settings(monkeypatch, tmp_path)

    async def _exercise_settings_modal() -> None:
        app = AutoSorterTUI(
            base_dir=str(inbox),
            settings=settings,
            skip_wizard=True,
            non_interactive=True,
        )
        async with app.run_test(size=(110, 36)) as pilot:
            saved: list[object] = []
            modal = SettingsModal(settings)
            app.push_screen(modal, saved.append)
            await pilot.pause()

            # Reject '..' traversal in ALLOWED_EXTERNAL_ROOTS
            modal.query_one(
                "#input-external-roots", Input
            ).value = f"{ext_archive}/../Secret"
            modal.action_save()
            await pilot.pause()
            assert saved == []

            # Accept '~' in DEST_DIR and ALLOWED_EXTERNAL_ROOTS and expand both
            modal.query_one("#input-dest-dir", Input).value = "~/SortedRoot"
            modal.query_one("#input-external-roots", Input).value = "~/ExtArchive"
            modal.action_save()
            await pilot.pause()
            assert len(saved) == 1
            res = saved[0]
            assert isinstance(res, dict)
            assert res["DEST_DIR"] == str(sorted_root)
            assert res["ALLOWED_EXTERNAL_ROOTS"] == [str(ext_archive)]

    asyncio.run(_exercise_settings_modal())

    # Verify CLI drop --dest-dir keeps source root in sessions.base_dir and supports undo
    drop_file = nested / "invoice_drop.txt"
    drop_file.write_text("Drop invoice document 2026", encoding="utf-8")
    settings.KEYWORD_RULES = {"invoice": "Invoices"}

    parser = main_mod.build_parser()
    args_drop = parser.parse_args(
        ["drop", str(inbox), "--dest-dir", str(sorted_root), "--json"]
    )
    with pytest.raises(SystemExit) as drop_exc:
        main_mod.handle_drop_command(args_drop, settings)
    assert drop_exc.value.code == 0
    drop_out = json.loads(capsys.readouterr().out)
    assert drop_out["status"] == "success"
    assert (sorted_root / "Invoices" / "invoice_drop.txt").is_file()
    assert not drop_file.exists()
    assert not nested.exists()  # emptied source subfolder pruned
    assert inbox.is_dir()  # source root preserved without needing recreation

    check_session = AppSession(settings, base_dir=str(inbox))
    try:
        sessions = check_session.history_manager.get_sessions()
        assert any(s["base_dir"] == str(inbox) for s in sessions)
        assert not any(s["base_dir"] == str(sorted_root) for s in sessions)
    finally:
        check_session.close()
