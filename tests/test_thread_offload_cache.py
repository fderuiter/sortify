import asyncio
import os
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.session import AppSession
from app.ui.app import AutoSorterApp


@pytest.fixture
def test_env(tmp_path):
    db_worker = DBWorker()
    db_path = tmp_path / "test_cache_db.db"
    db = Database(db_path, worker=db_worker)

    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = True

    app = AutoSorterApp(settings)
    app.base_dir = str(tmp_path / "scan_dir")
    os.makedirs(app.base_dir, exist_ok=True)

    # Initialize mock session
    app_session = AppSession(settings, app.base_dir)
    app_session.db = db
    app.app_session = app_session

    yield app, db, db_worker

    app_session.close()
    db_worker.stop()


def test_load_ratings_from_db(test_env):
    app, db, _ = test_env

    # Insert rating in DB
    db.upsert_document(app.base_dir, "doc1.txt", "hash1", "content")
    db.set_document_rating(app.base_dir, "doc1.txt", "positive")

    # Run load_ratings_from_db
    app.load_ratings_from_db()

    # Verify in-memory ratings cache has the rating
    assert "doc1.txt" in app._ratings_cache
    assert app._ratings_cache["doc1.txt"] == "positive"


def test_load_locked_files_from_db(test_env):
    app, db, _ = test_env

    # Insert locked target path in DB
    db.upsert_document(app.base_dir, "doc1.txt", "hash1", "content")
    db.set_user_verified_target_path(app.base_dir, "doc1.txt", "TargetFolder")

    # Run load_locked_files_from_db
    app.load_locked_files_from_db()

    # Verify in-memory locked files cache has the target folder
    assert "doc1.txt" in app.locked_files
    assert app.locked_files["doc1.txt"] == "TargetFolder"


def test_render_tree_uses_in_memory_cache(test_env):
    app, db, _ = test_env

    # Populate the in-memory cache directly
    app._ratings_cache = {"doc1.txt": "positive"}
    app.locked_files = {"doc1.txt": "TargetFolder"}
    app.plan = {"doc1.txt": {"__type__": "file", "status": "Proposed"}}

    # Patch get_all_document_ratings to raise an error if called
    with patch.object(
        db,
        "get_all_document_ratings",
        side_effect=AssertionError("Should not query DB during render_tree"),
    ):
        app.render_tree()

    # Verify tree nodes correctly populated rating and icon from in-memory cache
    node = app.tree_nodes[0]
    assert node["id"] == "doc1.txt"
    assert node["rating"] == "positive"
    assert node["icon"] == "lock"  # because k in self.locked_files is True


def test_handle_node_drop_instantly_updates_cache(test_env):
    app, db, _ = test_env
    app._ratings_cache = {}
    app.plan = {"doc1.txt": {"__type__": "file", "status": "Proposed"}}

    # Mock node drop event args
    class MockEvent:
        def __init__(self):
            self.args = {"source": "doc1.txt", "target": "TargetFolder"}

    e = MockEvent()

    from nicegui import Client

    # Verify handle_node_drop instantly updates app.locked_files in-memory before/without blocking
    # We patch set_user_verified_target_path to make sure it's called
    with Client(None):
        with patch.object(
            db, "set_user_verified_target_path", wraps=db.set_user_verified_target_path
        ) as mock_write:
            app.handle_node_drop(e)
            assert mock_write.call_count == 1

    # Verify locked files cache updated instantly
    assert app.locked_files["doc1.txt"] == "TargetFolder"


def test_handle_node_rate_instantly_updates_cache(test_env):
    app, db, _ = test_env
    app.plan = {"doc1.txt": {"__type__": "file", "status": "Proposed"}}
    app._ratings_cache = {}

    # Mock rate event args
    class MockEvent:
        def __init__(self):
            self.args = {"file_id": "doc1.txt", "rating": "positive"}

    e = MockEvent()

    from nicegui import Client

    # Patch set_document_rating to track the write
    with Client(None):
        with patch.object(
            db, "set_document_rating", wraps=db.set_document_rating
        ) as mock_write:
            app.handle_node_rate(e)
            assert mock_write.call_count == 1

    # Verify in-memory ratings cache updated instantly
    assert app._ratings_cache["doc1.txt"] == "positive"


@pytest.mark.anyio
async def test_asynchronous_background_scan_loads(test_env):
    app, db, _ = test_env

    # Mock UI elements to prevent AttributeErrors
    app.status_label = MagicMock()
    app.cancel_btn = MagicMock()
    app.progress_bar = MagicMock()
    app.execute_btn = MagicMock()
    app.start_watcher = MagicMock()

    # Mock load_locked_files_from_db and load_ratings_from_db
    app.load_locked_files_from_db = MagicMock()
    app.load_ratings_from_db = MagicMock()
    app.render_tree = MagicMock()

    # Mock async method verify_current_plan
    async def mock_verify():
        pass

    app.verify_current_plan = mock_verify

    # Mock scanning methods to bypass heavy logic
    app.app_session.process_items_async = MagicMock()
    app.app_session.generate_sorting_plan = MagicMock(return_value={})

    with patch("app.core.scanner.get_files_recursively", return_value=[]):
        with patch("app.core.metadata.MetadataPass.run", return_value=[]):
            with patch("asyncio.to_thread", wraps=asyncio.to_thread) as mock_to_thread:
                await app._scan_and_process_worker()

                # Verify that load_locked_files_from_db and load_ratings_from_db are wrapped in asyncio.to_thread
                # for background thread execution
                mock_to_thread.assert_any_call(app.load_locked_files_from_db)
                mock_to_thread.assert_any_call(app.load_ratings_from_db)
