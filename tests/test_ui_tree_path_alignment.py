import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from app.config import AppSettings
from app.core.db import Database
from app.core.db_conn import clear_connection_cache
from app.core.db_worker import DBWorker
from app.ui.app import AutoSorterApp, find_and_remove_file


def test_ui_tree_path_alignment_and_nested_ratings():
    # Set up temp dir and database
    with tempfile.TemporaryDirectory() as temp_dir:
        db_worker = DBWorker()
        db_path = Path(temp_dir) / "test_ratings.db"
        db = Database(db_path, db_worker)

        try:
            # Create a mock AppSession
            app_session = MagicMock()
            app_session.db = db
            app_session.base_dir = temp_dir

            # Setup dummy settings
            settings = AppSettings()

            # Initialize AutoSorterApp
            app = AutoSorterApp(settings)
            app.base_dir = temp_dir
            app.app_session = app_session

            # 1. Populate some documents in the DB
            nested_filepath = "Folder/Subfolder/nested_file.txt"
            db.upsert_document(
                temp_dir, nested_filepath, "hash123", "Content of nested file"
            )

            # Set document rating in the DB
            db.set_document_rating(temp_dir, nested_filepath, "positive")

            # Wait for asynchronous DB writes to complete
            db_worker.q.join()

            # Ensure DB returns the rating keyed by nested_filepath
            all_ratings = db.get_all_document_ratings(temp_dir)
            assert nested_filepath in all_ratings
            assert all_ratings[nested_filepath] == "positive"

            # Load ratings from DB to populate in-memory cache
            app.load_ratings_from_db()

            # 2. Render the tree using a nested plan
            app.plan = {
                "Folder": {
                    "Subfolder": {
                        "nested_file.txt": {
                            "__type__": "file",
                            "status": "Proposed",
                        }
                    }
                }
            }

            app.load_ratings_from_db()
            app.render_tree()

            assert len(app.tree_nodes) == 1
            folder_node = app.tree_nodes[0]
            assert folder_node["id"] == "Folder"

            subfolder_node = folder_node["children"][0]
            assert subfolder_node["id"] == "Folder/Subfolder"

            file_node = subfolder_node["children"][0]
            assert file_node["id"] == "Folder/Subfolder/nested_file.txt"

            assert file_node["filepath"] == "Folder/Subfolder/nested_file.txt"
            assert file_node["rating"] == "positive"

            class MockEvent:
                def __init__(self, file_id, rating):
                    self.args = {"file_id": file_id, "rating": rating}

            event_clear = MockEvent("Folder/Subfolder/nested_file.txt", "positive")
            app.handle_node_rate(event_clear)

            db_worker.q.join()

            db_rating = db.get_document_rating(temp_dir, nested_filepath)
            assert db_rating is None

            removed_info = find_and_remove_file(
                app.plan, "Folder/Subfolder/nested_file.txt"
            )
            assert removed_info == {"__type__": "file", "status": "Proposed"}
            assert app.plan == {}

        finally:
            db_worker.stop()
            clear_connection_cache(only_current_and_inactive=False)
