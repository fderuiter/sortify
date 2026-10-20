"""Roll back or forget an abandoned session, as the startup recovery prompt does."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.config import AppSettings
from app.core.session_catalog import (
    forget_abandoned_session,
    roll_back_abandoned_session,
)


def _session_info(tmp_path):
    session_dir = tmp_path / "sessions" / "sess_1"
    session_dir.mkdir(parents=True)
    return {
        "session_id": "sess_1",
        "base_dir": str(tmp_path),
        "session_dir": str(session_dir),
    }


def _undo_returning(result):
    pipeline = MagicMock()
    pipeline.return_value.undo.return_value = result
    return patch("app.core.pipeline.SortPipeline", pipeline)


def test_rollback_deletes_the_session_folder_once_every_move_is_undone(tmp_path):
    info = _session_info(tmp_path)
    with (
        patch("app.core.session.AppSession"),
        _undo_returning(SimpleNamespace(ok=True, failed=[])),
    ):
        assert roll_back_abandoned_session(AppSettings(), info) is True
    assert not (tmp_path / "sessions" / "sess_1").exists()


def test_partial_rollback_keeps_the_session_folder(tmp_path):
    info = _session_info(tmp_path)
    with (
        patch("app.core.session.AppSession"),
        _undo_returning(SimpleNamespace(ok=False, failed=["a.txt"])),
    ):
        assert roll_back_abandoned_session(AppSettings(), info) is False
    assert (tmp_path / "sessions" / "sess_1").exists()


def test_rollback_that_raises_keeps_the_session_folder_and_closes_the_session(
    tmp_path,
):
    info = _session_info(tmp_path)
    pipeline = MagicMock()
    pipeline.return_value.undo.side_effect = PermissionError("locked")
    with (
        patch("app.core.session.AppSession") as session_cls,
        patch("app.core.pipeline.SortPipeline", pipeline),
    ):
        assert roll_back_abandoned_session(AppSettings(), info) is False
    session_cls.return_value.close.assert_called_once()
    assert (tmp_path / "sessions" / "sess_1").exists()


def test_rollback_without_a_folder_or_session_id_does_nothing(tmp_path):
    info = _session_info(tmp_path)
    with patch("app.core.session.AppSession") as session_cls:
        assert (
            roll_back_abandoned_session(AppSettings(), {**info, "base_dir": None})
            is False
        )
        assert (
            roll_back_abandoned_session(AppSettings(), {**info, "session_id": None})
            is False
        )
    session_cls.assert_not_called()
    assert (tmp_path / "sessions" / "sess_1").exists()


def test_forget_removes_a_leftover_rollback_journal(tmp_path):
    journal = tmp_path / "rollback_journal.json"
    journal.write_text("{}")
    with patch("app.core.session_catalog.mark_session_abandoned") as mark:
        forget_abandoned_session({"session_id": "s", "journal_path": str(journal)})
    assert not journal.exists()
    mark.assert_not_called()


def test_forget_marks_a_store_session_abandoned():
    store = object()
    with patch("app.core.session_catalog.mark_session_abandoned") as mark:
        forget_abandoned_session({"session_id": "s"}, store=store)
    mark.assert_called_once_with("s", store=store)
