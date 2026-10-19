"""Sessions read and write the one app store (ADR 0008)."""

import json
from pathlib import Path

import pytest

from app.config import AppSettings
from app.core.history import open_session_history
from app.core.pipeline import SortPipeline
from app.core.session import AppSession
from app.core.session_catalog import (
    find_abandoned_sessions,
    list_sessions,
    mark_session_abandoned,
)
from app.core.store import open_store


@pytest.fixture
def store(tmp_path):
    return open_store(tmp_path / "app", legacy_session_root=tmp_path / "none")


@pytest.fixture
def session(tmp_path, store):
    src = tmp_path / "src"
    src.mkdir()
    (src / "finance_doc.txt").write_text("Finance, investment, and banking report.")
    (src / "tech_doc.txt").write_text("Software engineering, Python, algorithms.")
    (src / "health_doc.txt").write_text("Medical science, clinical trial, patient.")
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    s = AppSession(settings, base_dir=str(src), store=store)
    try:
        yield s
    finally:
        s.close()


def test_session_managers_all_use_the_store(session, store):
    assert session.store is store
    assert (
        session.db.db_path
        == session.cache_manager.db_path
        == session.history_manager.db_path
        == str(store.path)
    )


def test_steps_are_logged_outside_any_sort_long_transaction(
    session, store, monkeypatch
):
    in_session = []
    log_step = session.history_manager.log_step

    def spy(*args, **kwargs):
        in_session.append(session.db_worker.is_in_session())
        return log_step(*args, **kwargs)

    monkeypatch.setattr(session.history_manager, "log_step", spy)

    plan, result = SortPipeline(session).run(dry_run=False)

    assert result.ok
    assert in_session and not any(in_session)
    sorted_dirs = [s for s in list_sessions(store) if s["base_dir"] == session.base_dir]
    assert len(sorted_dirs) == 1
    assert sorted_dirs[0]["history_db_path"] == str(store.path)


def test_undo_from_the_store_restores_a_sort(session, store):
    SortPipeline(session).run(dry_run=False)
    (sorted_session,) = list_sessions(store)

    with open_session_history(store) as history:
        history.rollback(sorted_session["session_id"])

    files = sorted(p.name for p in Path(session.base_dir).rglob("*") if p.is_file())
    assert files == ["finance_doc.txt", "health_doc.txt", "tech_doc.txt"]
    for name in files:
        assert (Path(session.base_dir) / name).is_file()


def _unfinished_session(store, base_dir):
    with open_session_history(store) as history:
        session_id = history.create_snapshot(str(base_dir))
        history.log_step(session_id, f"{base_dir}/a.txt", f"{base_dir}/Docs/a.txt")
    return session_id


def test_an_unfinished_session_with_steps_is_abandoned(tmp_path, store):
    session_id = _unfinished_session(store, tmp_path)

    (entry,) = find_abandoned_sessions(store)

    assert entry["session_id"] == session_id
    assert entry["step_count"] == 1


def test_a_session_marked_abandoned_is_not_offered_again(tmp_path, store):
    session_id = _unfinished_session(store, tmp_path)

    mark_session_abandoned(session_id, store)

    assert find_abandoned_sessions(store) == []


def test_an_interrupted_rollback_is_found_by_its_journal(store):
    journal = store.app_dir / "rollback_journal.json"
    journal.write_text(json.dumps({"session_id": "s1", "base_dir": "/x"}))

    (entry,) = find_abandoned_sessions(store)

    assert entry["session_id"] == "s1"
    assert entry["is_rollback_recovery"] is True
    assert entry["journal_path"] == str(journal)
    # The journal sits in the app directory, which recovery must never delete.
    assert "session_dir" not in entry
