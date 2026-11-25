"""Tests for integrated undo subcommand and preset CLI flags."""

import json
from pathlib import Path

import pytest

from tests.test_cli_subcommands import create_sample_corpus, run_cli


@pytest.mark.xdist_group(name="cli_undo_presets")
def test_undo_default_to_latest(tmp_path, monkeypatch):
    """Verify executing 'app/main.py undo' without flags defaults to rolling back --latest."""
    app_dir = tmp_path / "app_dir"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    src_dir = tmp_path / "workspace"
    create_sample_corpus(src_dir)

    # Execute sort
    code_sort, stdout_sort, stderr_sort = run_cli(["sort", str(src_dir), "--json"])
    assert code_sort == 0, f"Sort failed: {stderr_sort}"

    # Execute undo with no subcommand flags (should default to --latest)
    code_undo, stdout_undo, stderr_undo = run_cli(["undo", "--json"])
    assert code_undo == 0, f"Undo default failed: {stderr_undo}"
    data = json.loads(stdout_undo)
    assert data["status"] == "success"


@pytest.mark.xdist_group(name="cli_undo_presets")
def test_undo_latest_explicit_flag(tmp_path, monkeypatch):
    """Verify executing 'app/main.py undo --latest' rolls back the last sorting session."""
    app_dir = tmp_path / "app_dir"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    src_dir = tmp_path / "workspace"
    create_sample_corpus(src_dir)

    # Execute sort
    code_sort, stdout_sort, stderr_sort = run_cli(["sort", str(src_dir), "--json"])
    assert code_sort == 0, f"Sort failed: {stderr_sort}"

    # Execute undo --latest --json
    code_undo, stdout_undo, stderr_undo = run_cli(["undo", "--latest", "--json"])
    assert code_undo == 0, f"Undo latest failed: {stderr_undo}"
    data = json.loads(stdout_undo)
    assert data["status"] == "success"


@pytest.mark.xdist_group(name="cli_undo_presets")
def test_undo_no_history_error(tmp_path, monkeypatch):
    """Verify undo when no history sessions exist returns non-zero code and error message."""
    app_dir = tmp_path / "empty_app_dir"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    code, stdout, stderr = run_cli(["undo", "--latest", "--json"])
    assert code == 1
    data = json.loads(stdout)
    assert data["status"] == "error"
    assert "No historical sorting sessions" in data["message"]


@pytest.mark.xdist_group(name="cli_undo_presets")
def test_preset_documents_resolution(tmp_path, monkeypatch):
    """Verify --preset documents resolves to ~/Documents."""
    fake_home = tmp_path / "user_home"
    docs_dir = fake_home / "Documents"
    create_sample_corpus(docs_dir)
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    code, stdout, stderr = run_cli(["scan", "--preset", "documents", "--json"])
    assert code == 0, f"Expected 0 exit code, got {code}. Stderr: {stderr}"
    data = json.loads(stdout)
    assert data["status"] == "success"
    assert Path(data["target_directory"]).resolve() == docs_dir.resolve()


@pytest.mark.xdist_group(name="cli_undo_presets")
def test_undo_latest_twice_reaches_the_previous_sort(tmp_path, monkeypatch):
    """A second `undo --latest` undoes the sort before, not the first undo's safety snapshot."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_dir"))

    undone = []
    for name in ("first", "second"):
        workspace = tmp_path / name
        create_sample_corpus(workspace)
        code, _out, err = run_cli(["sort", str(workspace), "--json"])
        assert code == 0, f"Sort of {name} failed: {err}"

    for _ in range(2):
        code, out, err = run_cli(["undo", "--latest", "--json"])
        assert code == 0, f"Undo failed: {err}"
        undone.append(json.loads(out)["session_id"])

    code, out, _err = run_cli(["undo", "--list", "--json"])
    sessions = {s["session_id"]: s for s in json.loads(out)}
    first_undo, second_undo = (sessions[sid] for sid in undone)

    assert Path(first_undo["base_dir"]).name == "second"
    assert Path(second_undo["base_dir"]).name == "first"
    assert first_undo["status"] == second_undo["status"] == "rolled_back"


@pytest.mark.xdist_group(name="cli_undo_presets")
def test_undo_list_prints_move_and_file_counts_and_guards_non_undoable_sessions(
    tmp_path, monkeypatch
):
    """Plain-text `undo --list` prints move/file counts, hides undo_snapshot rows, and `undo` guards non-undoable sessions without InvalidStateTransitionError."""
    from app.core.db_conn import get_db_connection
    from app.core.store import open_store

    app_dir = tmp_path / "app_dir"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    workspace = tmp_path / "workspace"
    create_sample_corpus(workspace)
    code_sort, _out_sort, err_sort = run_cli(["sort", str(workspace), "--json"])
    assert code_sort == 0, f"Sort failed: {err_sort}"

    store = open_store(app_dir, legacy_session_root=tmp_path / "none")
    conn = get_db_connection(str(store.path))
    with conn:
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
            ("legacy_snap_id", 1999999999.0, str(workspace), "undo_snapshot"),
        )
        conn.execute(
            "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
            ("zero_move_id", 1999999998.0, str(workspace), "completed"),
        )

    code_json, out_json, _ = run_cli(["undo", "--list", "--json"])
    assert code_json == 0
    sessions_json = json.loads(out_json)
    listed_ids = {s["session_id"] for s in sessions_json}
    assert "legacy_snap_id" not in listed_ids
    sorted_entry = next(s for s in sessions_json if s["session_id"] != "zero_move_id")
    assert sorted_entry["step_count"] > 0
    assert sorted_entry["file_count"] > 0

    code_txt, out_txt, _ = run_cli(["undo", "--list"])
    assert code_txt == 0
    assert "legacy_snap_id" not in out_txt
    assert (
        f"Moves: {sorted_entry['step_count']} | Files: {sorted_entry['file_count']}"
        in out_txt
    )

    # Targeting legacy undo_snapshot or 0-move session returns clean error (never InvalidStateTransitionError)
    for bad_id in ("legacy_snap_id", "zero_move_id"):
        c_bad, o_bad, e_bad = run_cli(["undo", "--session-id", bad_id, "--json"])
        assert c_bad == 1
        assert "InvalidStateTransitionError" not in (o_bad + e_bad)
        payload = json.loads(o_bad)
        assert payload["status"] == "error"

    # Despite zero_move_id and legacy_snap_id having newer timestamps, `undo --latest` skips them and undoes the real sort!
    c_latest, o_latest, e_latest = run_cli(["undo", "--latest", "--json"])
    assert c_latest == 0, f"undo --latest failed: {e_latest}"
    assert json.loads(o_latest)["session_id"] == sorted_entry["session_id"]
