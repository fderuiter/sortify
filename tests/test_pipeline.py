"""Tests for the shared sort pipeline (issue #708)."""

from pathlib import Path

import pytest

from app.config import AppSettings
from app.core.pipeline import SortPipeline, drop_scope
from app.core.session import AppSession


def _write_corpus(base: Path) -> None:
    (base / "finance_doc.txt").write_text("Finance, investment, and banking report.")
    (base / "tech_doc.txt").write_text(
        "Software engineering, Python, algorithms, and computers."
    )
    (base / "health_doc.txt").write_text(
        "Medical science, clinical trial, doctor, and patient data."
    )


@pytest.fixture
def session(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _write_corpus(src)
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    s = AppSession(settings, base_dir=str(src))
    try:
        yield s
    finally:
        s.close()


def _planned_sources(plan) -> set[str]:
    """File names anywhere in the plan's folder tree."""
    names: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for child in node.values():
                walk(child)
        elif isinstance(node, (list, tuple, set)):
            for child in node:
                walk(child)
        elif isinstance(node, str):
            names.add(Path(node).name)

    walk(plan.plan if hasattr(plan, "plan") else plan)
    return names


def test_drop_scope_sorts_a_folder_whole(tmp_path):
    assert drop_scope(str(tmp_path)) == (str(tmp_path), None)


def test_drop_scope_sorts_a_file_inside_its_parent(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert drop_scope(str(f)) == (str(tmp_path), [str(f)])


def test_plan_discovers_every_file_and_moves_nothing(session):
    plan = SortPipeline(session).plan()

    assert {"finance_doc.txt", "tech_doc.txt", "health_doc.txt"} <= _planned_sources(
        plan
    )
    for name in ("finance_doc.txt", "tech_doc.txt", "health_doc.txt"):
        assert (Path(session.base_dir) / name).exists()


def test_plan_limits_to_the_items_given(session):
    plan = SortPipeline(session).plan(["tech_doc.txt"])

    assert "tech_doc.txt" in _planned_sources(plan)
    assert not _planned_sources(plan) & {"finance_doc.txt", "health_doc.txt"}


def test_dry_run_returns_the_plan_and_no_summary(session):
    plan, summary = SortPipeline(session).run(dry_run=True)

    assert plan
    assert summary is None
    assert (Path(session.base_dir) / "finance_doc.txt").exists()


def test_run_moves_the_planned_files(session):
    plan, result = SortPipeline(session).run()

    assert result.ok
    assert sorted(Path(p).name for p in result.moved) == [
        "finance_doc.txt",
        "health_doc.txt",
        "tech_doc.txt",
    ]
    assert result.describe() == "3 moved."
    remaining = {p.name for p in Path(session.base_dir).iterdir() if p.is_file()}
    assert not remaining & {"finance_doc.txt", "tech_doc.txt", "health_doc.txt"}


def test_result_sorts_audit_records_into_buckets():
    from app.core.pipeline import SortResult

    summary = {
        "cancelled": False,
        "audit_report": {
            "records": [
                {
                    "relative_source": "a.txt",
                    "relative_destination": "Fin/a.txt",
                    "status": "VERIFIED_PASS",
                },
                {
                    "relative_source": "b.txt",
                    "relative_destination": "_Compliance_Quarantine/b.txt",
                    "status": "VERIFIED_PASS",
                },
                {
                    "relative_source": "c.txt",
                    "relative_destination": "c.txt",
                    "status": "SKIPPED",
                },
                {
                    "relative_source": "d.txt",
                    "relative_destination": "X/d.txt",
                    "status": "MOVE_FAIL",
                    "error": "File relocation operation failed",
                },
            ]
        },
    }

    result = SortResult.from_summary(summary, "_Compliance_Quarantine")

    assert result.moved == ["a.txt"]
    assert result.quarantined == ["b.txt"]
    assert result.skipped == ["c.txt"]
    assert result.failed == [("d.txt", "File relocation operation failed")]
    assert not result.ok
    assert result.describe() == "1 moved, 1 quarantined, 1 skipped, 1 failed."


def test_cancelled_result_says_so():
    from app.core.pipeline import SortResult

    result = SortResult.from_summary({"cancelled": True}, "_Compliance_Quarantine")

    assert not result.ok
    assert result.describe() == "Cancelled: 0 moved."


def _files_in(folder) -> list[str]:
    return sorted(p.name for p in Path(folder).rglob("*") if p.is_file())


def test_execute_reports_the_session_it_recorded(session):
    _plan, result = SortPipeline(session).run()

    assert result.session_id


def test_undo_puts_every_sorted_file_back(session):
    pipeline = SortPipeline(session)
    _plan, sorted_result = pipeline.run()

    result = pipeline.undo(sorted_result.session_id)

    assert result.ok
    assert result.session_id == sorted_result.session_id
    assert sorted(Path(p).name for p in result.moved) == [
        "finance_doc.txt",
        "health_doc.txt",
        "tech_doc.txt",
    ]
    assert result.describe() == "3 moved."
    for name in ("finance_doc.txt", "health_doc.txt", "tech_doc.txt"):
        assert (Path(session.base_dir) / name).is_file()


def test_undo_reports_a_file_changed_since_the_sort_as_failed(session):
    pipeline = SortPipeline(session)
    _plan, sorted_result = pipeline.run()
    base = Path(session.base_dir)
    (changed,) = list(base.rglob("tech_doc.txt"))
    changed.write_text("Edited after the sort.")

    result = pipeline.undo(sorted_result.session_id)

    assert not result.ok
    assert [Path(p).name for p, _reason in result.failed] == ["tech_doc.txt"]
    assert sorted(Path(p).name for p in result.moved) == [
        "finance_doc.txt",
        "health_doc.txt",
    ]
    assert changed.is_file()
    assert not (base / "tech_doc.txt").exists()


def test_undo_of_an_unknown_session_raises(session):
    with pytest.raises(ValueError):
        SortPipeline(session).undo("no-such-session")


def test_plan_fast_path_and_bypassed_items_options(tmp_path):
    """SortPipeline.plan supports fast_path_only, target_paths, run_metadata_pass, and bypassed_items."""
    from unittest import mock

    from app.core.pipeline import PipelineLifecycleState

    src = tmp_path / "src"
    src.mkdir()
    (src / "invoice_q1.txt").write_text("Quarterly invoice details.")
    (src / "notes.txt").write_text("General research notes.")

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    settings.KEYWORD_RULES = {"invoice": "Invoices"}

    s = AppSession(settings, base_dir=str(src))
    try:
        pipeline = SortPipeline(s)
        states: list[PipelineLifecycleState] = []
        pipeline.add_state_change_hook(lambda _old, new, _ctx=None: states.append(new))

        with mock.patch(
            "app.core.extractor.build_corpus_generator"
        ) as mock_build_corpus:
            fast_plan = pipeline.plan(
                ["invoice_q1.txt", "notes.txt"],
                fast_path_only=True,
                target_paths=["invoice_q1.txt"],
                run_metadata_pass=True,
            )
            mock_build_corpus.assert_not_called()

        assert "invoice_q1.txt" in _planned_sources(fast_plan)
        assert "notes.txt" not in _planned_sources(fast_plan)
        assert states == [
            PipelineLifecycleState.PLANNING,
            PipelineLifecycleState.PLANNED,
        ]

        partial_fit_spy = mock.MagicMock(wraps=s.partial_fit)
        s.partial_fit = partial_fit_spy
        slow_plan = pipeline.plan(
            ["invoice_q1.txt", "notes.txt"],
            fast_path_only=False,
            run_metadata_pass=True,
            bypassed_items={"invoice_q1.txt"},
        )
        assert partial_fit_spy.called
        fitted_keys = {
            k for call in partial_fit_spy.call_args_list for k in call.args[0].keys()
        }
        assert fitted_keys == {"notes.txt"}
        assert "notes.txt" in _planned_sources(slow_plan)
    finally:
        s.close()


def test_execute_move_context_wraps_execute_moves(session):
    """SortPipeline.execute enters move_context around session.execute_moves."""
    from contextlib import contextmanager

    events: list[str] = []

    @contextmanager
    def record_context(plan):
        assert plan
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    orig_execute = session.execute_moves

    def wrapped_execute(plan, **kwargs):
        assert events == ["enter"]
        events.append("moves")
        return orig_execute(plan, **kwargs)

    session.execute_moves = wrapped_execute
    pipeline = SortPipeline(session)
    plan = pipeline.plan(["finance_doc.txt"])
    result = pipeline.execute(plan, move_context=record_context)

    assert result.ok
    assert events == ["enter", "moves", "exit"]


def test_reroot_plan_and_validate_drop_paths(tmp_path):
    """reroot_plan and validate_drop_paths share consistent path handling."""
    from app.core.pipeline import reroot_plan, validate_drop_paths

    valid_file = tmp_path / "valid.txt"
    valid_file.write_text("hello")
    ignored_file = tmp_path / ".autosorter.tmp"
    ignored_file.write_text("ignored")
    protected_dir = tmp_path / "protected"
    protected_dir.mkdir()
    protected_file = protected_dir / "secret.txt"
    protected_file.write_text("secret")

    settings = AppSettings()
    settings.PROTECTED_PATHS = [str(protected_dir)]

    validated, issues = validate_drop_paths(
        [
            f"'{valid_file}'",
            str(tmp_path / "missing.txt"),
            str(ignored_file),
            str(protected_file),
            "   ",
        ],
        settings=settings,
        ignore_predicate=lambda p: p.endswith(".autosorter.tmp"),
    )

    assert validated == [str(valid_file.resolve())]
    assert [i.reason for i in issues] == ["missing", "ignored", "protected"]

    dest_dir = tmp_path / "out_dest"
    abs_target = str((tmp_path / "already_abs").resolve())
    rerooted = reroot_plan(
        {"Docs": ["valid.txt"], abs_target: ["other.txt"]},
        str(dest_dir),
    )
    assert dest_dir.is_dir()
    assert str(dest_dir.resolve() / "Docs") in rerooted
    assert abs_target in rerooted


@pytest.mark.anyio
async def test_ipc_drop_validates_paths_once_and_daemon_emits_pipeline_telemetry(
    tmp_path, monkeypatch
):
    """DaemonIPCServer._handle_drop_rpc validates paths once, and daemon sorting emits pipeline telemetry."""
    import threading
    from unittest import mock

    import app.core.pipeline as pipeline_mod
    from app.core.daemon import ContinuousWatchdogDaemon
    from app.core.db_conn import clear_connection_cache
    from app.core.ipc import DaemonIPCServer

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_dir"))
    src = tmp_path / "watched"
    src.mkdir()
    dropped = tmp_path / "dropped_doc.txt"
    dropped.write_text("Finance report and budget summary.")

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    daemon = ContinuousWatchdogDaemon(settings, str(src))
    ipc_server = DaemonIPCServer(daemon)

    try:
        with mock.patch.object(
            pipeline_mod,
            "validate_drop_paths",
            wraps=pipeline_mod.validate_drop_paths,
        ) as spy_validate:
            res = await ipc_server._handle_drop_rpc(
                {"paths": [str(dropped)], "dry_run": True}
            )
            assert spy_validate.call_count == 1
            assert res["status"] == "success"
            assert res["processed_count"] == 1
            assert res["validated_paths"] == [str(dropped.resolve())]

        (src / "invoice_100.txt").write_text("Invoice total 100")
        settings.KEYWORD_RULES = {"invoice": "Invoices"}
        daemon._is_running = True
        telemetry_events: list[dict] = []
        daemon.add_telemetry_hook(
            lambda evt, payload: (
                telemetry_events.append(payload)
                if evt == "pipeline_state_change"
                else None
            )
        )
        with mock.patch.object(AppSettings, "load", lambda _self: None):
            daemon._run_sorting_sync(threading.Event())

        observed_states = [e["new_state"] for e in telemetry_events]
        assert "planning" in observed_states
        assert "planned" in observed_states
        assert "executing" in observed_states
        assert "completed" in observed_states
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_handle_scan_command_applies_config_overrides_once(
    tmp_path, monkeypatch, capsys
):
    """handle_scan_command applies CLI overrides once and exports simulation reports via shared helper."""
    import argparse

    import app.main as main_mod

    target = tmp_path / "scan_dir"
    target.mkdir()
    (target / "doc.txt").write_text("Software python engineering")
    report_file = tmp_path / "sim_report.json"

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    override_calls: list[int] = []
    orig_apply = main_mod.apply_config_overrides

    def tracked_apply(s, a):
        override_calls.append(1)
        return orig_apply(s, a)

    monkeypatch.setattr(main_mod, "apply_config_overrides", tracked_apply)

    args = argparse.Namespace(
        directory=str(target),
        preset=None,
        tui=False,
        interactive=False,
        json=True,
        quiet=True,
        export_report=str(report_file),
        report_format="json",
    )

    with pytest.raises(SystemExit) as exc_info:
        main_mod.handle_scan_command(args, settings)

    assert exc_info.value.code == 0
    assert len(override_calls) == 1
    assert report_file.is_file()
    captured = capsys.readouterr()
    assert '"status": "success"' in captured.out


def test_daemon_process_dropped_items_and_reload_settings(tmp_path, monkeypatch):
    """ContinuousWatchdogDaemon.process_dropped_items and _reload_settings cover shared validation and timer flushing."""
    from unittest import mock

    from app.core.daemon import ContinuousWatchdogDaemon
    from app.core.db_conn import clear_connection_cache

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_dir"))
    watched = tmp_path / "watched"
    watched.mkdir()
    drop_dir = tmp_path / "drop_src"
    drop_dir.mkdir()
    valid_doc = drop_dir / "invoice_doc.txt"
    valid_doc.write_text("Invoice banking financial report")
    ignored_doc = drop_dir / "sortify.db"
    ignored_doc.write_text("db")
    prot_dir = tmp_path / "protected"
    prot_dir.mkdir()
    prot_doc = prot_dir / "secret.txt"
    prot_doc.write_text("secret")

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    settings.PROTECTED_PATHS = [str(prot_dir)]
    daemon = ContinuousWatchdogDaemon(settings, str(watched))

    try:
        mock_timer = mock.MagicMock()
        object.__setattr__(settings, "_save_timer", mock_timer)
        with (
            mock.patch.object(AppSettings, "_save") as mock_save,
            mock.patch.object(AppSettings, "load") as mock_load,
        ):
            daemon._reload_settings()
            mock_timer.cancel.assert_called_once()
            mock_save.assert_called_once()
            mock_load.assert_called_once()

        object.__setattr__(settings, "_save_timer", None)
        with mock.patch.object(AppSettings, "load", side_effect=OSError("disk busy")):
            daemon._reload_settings(log_error=False)
            daemon._reload_settings(log_error=True)

        result = daemon.process_dropped_items(
            [
                str(valid_doc),
                str(tmp_path / "missing_doc.txt"),
                str(ignored_doc),
                str(prot_doc),
            ],
            dry_run=False,
        )
        assert result["status"] == "success"
        assert result["processed_count"] == 1
        assert len(result["errors"]) == 2
        assert any("Path does not exist:" in e for e in result["errors"])
        assert any("Protected path blocked:" in e for e in result["errors"])
        moved_files = list(drop_dir.rglob("invoice_doc.txt"))
        assert len(moved_files) == 1
        assert len(moved_files[0].relative_to(drop_dir).parts) == 2
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)


def test_plan_relativizes_dropped_file_items_under_base_dir(tmp_path, monkeypatch):
    """SortPipeline.plan relativizes absolute dropped file paths (POSIX and Windows drive paths) under base_dir."""
    from app.core.pipeline import _relativize_items_to_base

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_dir"))
    drop_dir = tmp_path / "drop_src"
    drop_dir.mkdir()
    dropped_file = drop_dir / "invoice_doc.txt"
    dropped_file.write_text("Invoice banking financial report")

    base, files = drop_scope(str(dropped_file))
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    s = AppSession(settings, base_dir=base)
    try:
        plan, result = SortPipeline(s).run(files)
        assert plan
        assert result.ok
        moved_files = list(drop_dir.rglob("invoice_doc.txt"))
        assert len(moved_files) == 1
        assert len(moved_files[0].relative_to(drop_dir).parts) == 2
    finally:
        s.close()

    win_base = r"C:\Users\runneradmin\AppData\Local\Temp\drop_src"
    win_items = [
        r"C:\Users\runneradmin\AppData\Local\Temp\drop_src\invoice_doc.txt",
        r"C:\Users\runneradmin\AppData\Local\Temp\drop_src\sub\report.txt",
        r"C:\Users\runneradmin\Other\external.txt",
        r"C:\Users\runneradmin\Other::ns_doc.txt",
        "already_rel.txt",
    ]
    assert _relativize_items_to_base(win_items, win_base) == [
        "invoice_doc.txt",
        r"sub\report.txt",
        r"C:\Users\runneradmin\Other\external.txt",
        r"C:\Users\runneradmin\Other::ns_doc.txt",
        "already_rel.txt",
    ]


def test_reroot_plan_binds_sources_and_executes_nested_drop_into_dest(
    tmp_path, monkeypatch
):
    """reroot_plan binds absolute source_path on nested leaves and executes into dest_dir."""
    from app.core.pipeline import reroot_plan
    from app.main import _reroot_plan

    assert _reroot_plan is reroot_plan

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_dir"))
    src = tmp_path / "incoming"
    (src / "sub1").mkdir(parents=True)
    (src / "sub2").mkdir(parents=True)
    (src / "sub1" / "bundle.zip").write_bytes(b"PK\x03\x04")
    (src / "sub2" / "invoice_doc.txt").write_text(
        "Finance, investment, and banking invoice report."
    )
    dest = tmp_path / "sorted_dest"
    dest.mkdir()

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    s = AppSession(settings, base_dir=str(src))
    try:
        pipe = SortPipeline(s)
        plan = pipe.plan(["sub1/bundle.zip", "sub2/invoice_doc.txt"])
        rerooted = reroot_plan(plan, str(dest), source_dir=str(src))

        assert s.source_dirs == [str(src)]
        s.base_dir = str(dest)
        assert s.source_dirs == [str(src)]

        outcome = pipe.execute(rerooted)
        assert outcome.ok
        dest_names = {p.name for p in dest.rglob("*") if p.is_file()}
        assert dest_names == {"bundle.zip", "invoice_doc.txt"}
        assert not [p for p in src.rglob("*") if p.is_file()]
    finally:
        s.close()


def test_plan_scoped_single_file_does_not_sweep_cached_siblings(tmp_path, monkeypatch):
    """Dropping one file into an already-scanned folder plans and moves only that file."""
    from app.core.pipeline import DropBatchCoordinator

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_dir"))
    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    sibling_a = downloads / "sibling_invoice.txt"
    sibling_a.write_text("Annual accounting tax invoice statement")
    sibling_b = downloads / "sibling_archive.zip"
    sibling_b.write_bytes(b"PK\x03\x04")

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False

    # Pre-scan the folder so sibling rows exist in SQLite documents cache
    s_warm = AppSession(settings, base_dir=str(downloads))
    try:
        warm_plan = SortPipeline(s_warm).plan()
        assert warm_plan
    finally:
        s_warm.close()

    # Now drop a single new file into the populated folder
    dropped_receipt = downloads / "coffee_receipt.txt"
    dropped_receipt.write_text("Bank payment receipt total USD 14.50")

    coord = DropBatchCoordinator(settings)
    batch_plan = coord.plan_batch([str(dropped_receipt)])
    assert [it.source_path for it in batch_plan.items] == [
        str(dropped_receipt.resolve())
    ]
    res = coord.execute_batch(batch_plan)
    assert res.moved_count == 1
    assert res.failed_count == 0

    # Siblings remain untouched in Downloads root
    assert sibling_a.exists()
    assert sibling_b.exists()
    assert not dropped_receipt.exists()
    moved_receipts = list(downloads.rglob("coffee_receipt.txt"))
    assert len(moved_receipts) == 1
    assert moved_receipts[0].parent != downloads


def test_scan_filter_root_segments_and_drop_coordinator_bundle_quarantine_misc(
    tmp_path, monkeypatch
):
    """Root .app bundles and quarantine folders are blocked while .sketch/.fig route to Miscellaneous."""
    import os

    from app.core.pipeline import DropBatchCoordinator, validate_drop_paths
    from app.core.scanner import ScanFilter

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_dir"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    calc_app = workspace / "Calculator.app"
    (calc_app / "Contents" / "MacOS").mkdir(parents=True)
    app_bin = calc_app / "Contents" / "MacOS" / "Calculator"
    app_bin.write_text("mach-o binary")

    quarantine_dir = workspace / "_Compliance_Quarantine"
    quarantine_dir.mkdir()
    quarantine_doc = quarantine_dir / "flagged.txt"
    quarantine_doc.write_text("quarantined pii")

    protected_dir = workspace / "ProtectedVault"
    protected_dir.mkdir()
    vault_doc = protected_dir / "secret.txt"
    vault_doc.write_text("top secret")

    ignored_tmp = workspace / "download.tmp"
    ignored_tmp.write_text("partial")

    sketch_file = workspace / "hero_banner.sketch"
    sketch_file.write_bytes(b"\x89SKETCH\x00\x01")
    fig_file = workspace / "wireframe.fig"
    fig_file.write_bytes(b"\x89FIGMA\x00\x01")

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    settings.PROTECTED_PATHS = [str(protected_dir)]

    # Root-level ScanFilter checks must detect bundle and quarantine even when base_dir == target
    sf_bundle = ScanFilter.from_settings(settings=settings, base_dir=str(calc_app))
    assert sf_bundle.is_app_bundle(str(calc_app)) is True
    sf_quar = ScanFilter.from_settings(settings=settings, base_dir=str(quarantine_dir))
    assert sf_quar.is_quarantine(str(quarantine_dir)) is True

    valid, issues = validate_drop_paths(
        [
            str(calc_app),
            str(quarantine_dir),
            str(protected_dir),
            str(ignored_tmp),
            str(sketch_file),
            str(fig_file),
        ],
        settings=settings,
    )
    assert valid == [str(sketch_file.resolve()), str(fig_file.resolve())]
    issue_details = {os.path.basename(i.abs_path): (i.reason, i.detail) for i in issues}
    assert issue_details == {
        "Calculator.app": ("ignored", "app_bundle"),
        "_Compliance_Quarantine": ("ignored", "quarantine"),
        "ProtectedVault": ("protected", "protected"),
        "download.tmp": ("ignored", "ignored_extension"),
    }

    coord = DropBatchCoordinator(settings)
    bplan = coord.plan_batch(valid)
    bres = coord.execute_batch(bplan)
    assert bres.moved_count == 2
    assert (workspace / "Miscellaneous" / "hero_banner.sketch").exists()
    assert (workspace / "Miscellaneous" / "wireframe.fig").exists()
    assert app_bin.exists()
    assert quarantine_doc.exists()
    assert vault_doc.exists()
    assert ignored_tmp.exists()


def test_drop_batch_coordinator_priority_overrides_overwrite_and_multi_folder_undo(
    tmp_path, monkeypatch
):
    """Queue priority, folder/exclusion/conflict overrides, displaced overwrite restore, and single-session undo."""
    from app.core.pipeline import DropBatchCoordinator

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_dir"))
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    coord = DropBatchCoordinator(settings)

    # 1. Queue priority: second folder passed first wins clean filename on collision
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dest = tmp_path / "dest_priority"
    dir_a.mkdir()
    dir_b.mkdir()
    dest.mkdir()
    file_a = dir_a / "deck.sketch"
    file_b = dir_b / "deck.sketch"
    file_extra = dir_b / "skip_me.fig"
    file_custom = dir_a / "custom_route.fig"
    file_a.write_text("from_A")
    file_b.write_text("from_B_higher_priority")
    file_extra.write_text("excluded_payload")
    file_custom.write_text("custom_folder_payload")

    # Pass file_b BEFORE file_a so file_b has higher queue priority (0 < 1)
    bplan = coord.plan_batch(
        [str(file_b), str(file_a), str(file_extra), str(file_custom)],
        dest_dir=str(dest),
    )
    assert bplan.warnings
    assert any("deck.sketch" in w for w in bplan.warnings)

    # Verify traversal escape in folder override is rejected
    try:
        coord.apply_user_overrides(
            bplan,
            item_overrides={
                str(file_custom.resolve()): {"target_folder": "../outside"}
            },
        )
        raise AssertionError("Expected ValueError for parent traversal")
    except ValueError:
        pass

    coord.apply_user_overrides(
        bplan,
        conflict_policy="rename",
        item_overrides={
            str(file_extra.resolve()): {"is_excluded": True},
            str(file_custom.resolve()): {"target_folder": "DesignOverrides/Sub"},
        },
    )
    bres = coord.execute_batch(bplan)
    assert bres.session_id is not None
    assert bres.moved_count == 3
    assert (
        dest / "Miscellaneous" / "deck.sketch"
    ).read_text() == "from_B_higher_priority"
    assert (dest / "Miscellaneous" / "deck_1.sketch").read_text() == "from_A"
    assert (
        dest / "DesignOverrides" / "Sub" / "custom_route.fig"
    ).read_text() == "custom_folder_payload"
    assert file_extra.exists()

    # Single undo restores both dir_a and dir_b sources
    undo_res = SortPipeline(None, settings=settings).undo(bres.session_id)
    assert undo_res.ok
    assert file_a.read_text() == "from_A"
    assert file_b.read_text() == "from_B_higher_priority"
    assert file_custom.read_text() == "custom_folder_payload"

    # 2. Overwrite policy records displaced target in MoveLedger and restores it on undo
    dest_ow = tmp_path / "dest_overwrite"
    (dest_ow / "Miscellaneous").mkdir(parents=True)
    existing_target = dest_ow / "Miscellaneous" / "deck.sketch"
    existing_target.write_text("preexisting_on_disk")

    ow_plan = coord.plan_batch([str(file_b)], dest_dir=str(dest_ow))
    assert any("already exists" in w or "deck.sketch" in w for w in ow_plan.warnings)
    coord.apply_user_overrides(ow_plan, conflict_policy="overwrite")
    ow_res = coord.execute_batch(ow_plan)
    assert ow_res.session_id is not None
    assert existing_target.read_text() == "from_B_higher_priority"
    assert not file_b.exists()

    ow_undo = SortPipeline(None, settings=settings).undo(ow_res.session_id)
    assert ow_undo.ok
    assert file_b.read_text() == "from_B_higher_priority"
    assert existing_target.read_text() == "preexisting_on_disk"

    # 3. Multi-folder in-place drop (no dest_dir) shares one session_id and rolls back all folders
    multi_1 = tmp_path / "multi_1"
    multi_2 = tmp_path / "multi_2"
    multi_1.mkdir()
    multi_2.mkdir()
    m1_doc = multi_1 / "alpha.sketch"
    m2_doc = multi_2 / "beta.fig"
    m1_doc.write_text("alpha")
    m2_doc.write_text("beta")

    in_place_plan = coord.plan_batch([str(m1_doc), str(m2_doc)], dest_dir=None)
    in_place_res = coord.execute_batch(in_place_plan)
    assert in_place_res.session_id is not None
    assert (multi_1 / "Miscellaneous" / "alpha.sketch").exists()
    assert (multi_2 / "Miscellaneous" / "beta.fig").exists()

    in_place_undo = SortPipeline(None, settings=settings).undo(in_place_res.session_id)
    assert in_place_undo.ok
    assert m1_doc.exists()
    assert m2_doc.exists()
    assert not (multi_1 / "Miscellaneous").exists()
    assert not (multi_2 / "Miscellaneous").exists()


def test_app_session_lock_folders_sorted_canonical_order(tmp_path, monkeypatch):
    """AppSession.lock_folders acquires unique canonical FolderLocks in ascending order."""
    import os

    from app.core.folder_lock import FolderLock

    z_dir = tmp_path / "zzz_folder"
    a_dir = tmp_path / "aaa_folder"
    z_dir.mkdir()
    a_dir.mkdir()

    entered: list[str] = []
    orig_enter = FolderLock.__enter__

    def tracking_enter(self):
        entered.append(self.folder)
        return orig_enter(self)

    monkeypatch.setattr(FolderLock, "__enter__", tracking_enter)
    with AppSession.lock_folders([str(z_dir), str(a_dir), str(z_dir)]):
        assert entered == sorted(
            [os.path.realpath(str(a_dir)), os.path.realpath(str(z_dir))]
        )


def test_drop_batch_unified_single_plan_cross_folder_clustering_and_pruning(
    tmp_path, monkeypatch
):
    """DropBatchCoordinator plans and executes multi-folder drops in one SortPipeline pass, clusters .md/.html across folders, prunes nested dirs, recreates source roots, and undoes in one step."""
    import os

    from app.core.pipeline import DropBatchCoordinator
    from app.core.services import acquire_services

    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_unified_drop"))
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    settings.CONTEXTUAL_RENAMING = False

    folder_a = tmp_path / "source_alpha"
    folder_b = tmp_path / "source_beta"
    sub_a = folder_a / "nested_a"
    sub_b = folder_b / "nested_b"
    dest_dir = tmp_path / "unified_dest"
    sub_a.mkdir(parents=True)
    sub_b.mkdir(parents=True)
    dest_dir.mkdir()

    # Split two distinct topics across two separate folders using .md and .html files
    quantum_md = sub_a / "quantum_circuit_gates.md"
    quantum_html = sub_b / "quantum_qubit_entanglement.html"
    bakery_md = folder_a / "sourdough_fermentation_recipe.md"
    bakery_html = folder_b / "sourdough_starter_hydration.html"

    quantum_md.write_text(
        "# Quantum Computing\nQuantum qubit entanglement superconducting circuit gates hamiltonian coherence.\n",
        encoding="utf-8",
    )
    quantum_html.write_text(
        "<html><body><h1>Quantum Entanglement</h1><p>Quantum qubit entanglement superconducting circuit gates hamiltonian coherence.</p></body></html>",
        encoding="utf-8",
    )
    bakery_md.write_text(
        "# Artisan Bakery\nSourdough fermentation levain bread flour hydration crust crumb baking oven.\n",
        encoding="utf-8",
    )
    bakery_html.write_text(
        "<html><body><h1>Sourdough Starter</h1><p>Sourdough fermentation levain bread flour hydration crust crumb baking oven.</p></body></html>",
        encoding="utf-8",
    )

    plan_calls = 0
    exec_calls = 0
    orig_plan = SortPipeline.plan
    orig_execute = SortPipeline.execute

    def spy_plan(self, *args, **kwargs):
        nonlocal plan_calls
        plan_calls += 1
        return orig_plan(self, *args, **kwargs)

    def spy_execute(self, *args, **kwargs):
        nonlocal exec_calls
        exec_calls += 1
        return orig_execute(self, *args, **kwargs)

    monkeypatch.setattr(SortPipeline, "plan", spy_plan)
    monkeypatch.setattr(SortPipeline, "execute", spy_execute)

    coord = DropBatchCoordinator(settings)
    bplan = coord.plan_batch([str(folder_a), str(folder_b)], dest_dir=str(dest_dir))
    assert plan_calls == 1
    assert len(bplan.groups) == 2
    assert bplan.groups[0].session is bplan.groups[1].session

    # Cross-folder topic clustering puts the two quantum files in one shared folder
    # and the two sourdough files in another shared folder.
    by_name = {os.path.basename(it.source_path): it for it in bplan.items}
    assert (
        by_name["quantum_circuit_gates.md"].target_folder
        == by_name["quantum_qubit_entanglement.html"].target_folder
    )
    assert (
        by_name["sourdough_fermentation_recipe.md"].target_folder
        == by_name["sourdough_starter_hydration.html"].target_folder
    )
    assert (
        by_name["quantum_circuit_gates.md"].target_folder
        != by_name["sourdough_fermentation_recipe.md"].target_folder
    )

    bres = coord.execute_batch(bplan)
    assert exec_calls == 1
    assert bres.session_id is not None
    assert bres.moved_count == 4

    # Emptied nested subfolders are pruned, while emptied dropped source roots are recreated
    assert not sub_a.exists()
    assert not sub_b.exists()
    assert folder_a.is_dir()
    assert folder_b.is_dir()

    # Exactly one session recorded in history
    with acquire_services() as svc:
        sessions = svc.history.get_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == bres.session_id

    # Single undo restores all 4 files across both folders and prunes created dest folders
    undo_res = SortPipeline(None, settings=settings).undo(bres.session_id)
    assert undo_res.ok
    assert quantum_md.exists()
    assert quantum_html.exists()
    assert bakery_md.exists()
    assert bakery_html.exists()
    assert [p for p in dest_dir.iterdir() if not p.name.startswith(".")] == []

    # Also verify in-place multi-folder drop uses 1 plan pass + 1 execute pass + 1 undo
    plan_calls = 0
    exec_calls = 0
    in_place_plan = coord.plan_batch([str(folder_a), str(folder_b)], dest_dir=None)
    assert plan_calls == 1
    in_place_by_name = {
        os.path.basename(it.source_path): it for it in in_place_plan.items
    }
    assert (
        in_place_by_name["quantum_circuit_gates.md"].target_folder
        == in_place_by_name["quantum_qubit_entanglement.html"].target_folder
    )
    in_place_res = coord.execute_batch(in_place_plan)
    assert exec_calls == 1
    assert in_place_res.moved_count == 4
    assert not sub_a.exists()
    assert not sub_b.exists()

    assert in_place_res.session_id is not None
    in_place_undo = SortPipeline(None, settings=settings).undo(in_place_res.session_id)
    assert in_place_undo.ok
    assert quantum_md.exists()
    assert quantum_html.exists()
    assert bakery_md.exists()
    assert bakery_html.exists()
