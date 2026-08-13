import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from nicegui import Client

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
            with Client(None):
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
                # We want nested documents to check that ratings are mapped to their relative paths
                nested_filepath = "Folder/Subfolder/nested_file.txt"
                db.upsert_document(
                    temp_dir, nested_filepath, "hash123", "Content of nested file"
                )

                # Set document rating in the DB
                db.set_document_rating(temp_dir, nested_filepath, "positive")

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

                # Render tree which populates tree_nodes
                app.render_tree()

                # Check the generated tree nodes
                assert len(app.tree_nodes) == 1
                folder_node = app.tree_nodes[0]
                assert folder_node["id"] == "Folder"
                assert folder_node["text"] == "Folder"
                assert folder_node["is_file"] is False

                subfolder_node = folder_node["children"][0]
                assert subfolder_node["id"] == "Folder/Subfolder"
                assert subfolder_node["text"] == "Subfolder"
                assert subfolder_node["is_file"] is False

                file_node = subfolder_node["children"][0]
                assert file_node["id"] == "Folder/Subfolder/nested_file.txt"
                assert file_node["text"] == "nested_file.txt [Proposed]"
                assert file_node["is_file"] is True
                # Requirement 3: File tree must display only the leaf filename (no path prefix in the label/text)
                assert "Folder/Subfolder" not in file_node["text"]

                # Requirement 1 & 2: Set target/filepath payload to full relative path and align query
                assert file_node["filepath"] == "Folder/Subfolder/nested_file.txt"
                assert file_node["rating"] == "positive"

                # 3. Trigger handle_node_rate to clear the rating via full relative path
                class MockEvent:
                    def __init__(self, file_id, rating):
                        self.args = {"file_id": file_id, "rating": rating}

                event_clear = MockEvent("Folder/Subfolder/nested_file.txt", "positive")
                app.handle_node_rate(event_clear)

                # Verify database has cleared the rating
                db_rating = db.get_document_rating(temp_dir, nested_filepath)
                assert db_rating is None

                # 4. Trigger handle_node_rate to set a negative rating
                event_negative = MockEvent(
                    "Folder/Subfolder/nested_file.txt", "negative"
                )
                app.handle_node_rate(event_negative)

                # Verify database has recorded negative rating for the relative path
                db_rating = db.get_document_rating(temp_dir, nested_filepath)
                assert db_rating == "negative"

                # 5. Verify the clean up / removal of nested files from plan using relative path
                removed_info = find_and_remove_file(
                    app.plan, "Folder/Subfolder/nested_file.txt"
                )
                assert removed_info == {"__type__": "file", "status": "Proposed"}
                # The entire nested folder structure should also be cleaned up since it is empty now
                assert app.plan == {}

        finally:
            db_worker.stop()
            clear_connection_cache(only_current_and_inactive=False)
