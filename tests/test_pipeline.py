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
    (changed,) = [p for p in base.rglob("tech_doc.txt")]
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
    tmp_path,
):
    """DaemonIPCServer._handle_drop_rpc validates paths once, and daemon sorting emits pipeline telemetry."""
    import threading
    from unittest import mock

    import app.core.pipeline as pipeline_mod
    from app.core.daemon import ContinuousWatchdogDaemon
    from app.core.db_conn import clear_connection_cache
    from app.core.ipc import DaemonIPCServer

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


def test_daemon_process_dropped_items_and_reload_settings(tmp_path):
    """ContinuousWatchdogDaemon.process_dropped_items and _reload_settings cover shared validation and timer flushing."""
    from unittest import mock

    from app.core.daemon import ContinuousWatchdogDaemon
    from app.core.db_conn import clear_connection_cache

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
    finally:
        daemon.stop()
        clear_connection_cache(only_current_and_inactive=False)
