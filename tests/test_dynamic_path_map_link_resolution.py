import os
import shutil
import tempfile
import unittest
from pathlib import Path

from app.core.cache import CacheManager
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.history import HistoryManager
from app.core.mover import _update_path_map_entry, execute_moves, resolve_new_target
from app.core.verifier import VerificationEngine, VirtualFilesystemTracker


class TestDynamicPathMapLinkResolution(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.base_dir = os.path.join(self.test_dir, "base")
        self.dest_dir = os.path.join(self.base_dir, "Documents")
        os.makedirs(self.base_dir, exist_ok=True)
        os.makedirs(self.dest_dir, exist_ok=True)

        self.db_worker = DBWorker()
        db_path = Path(self.test_dir) / "test.db"
        self.db = Database(db_path, self.db_worker)
        self.cache_manager = CacheManager(str(Path(self.test_dir) / "cache.db"), self.db_worker)
        self.history_manager = HistoryManager(
            self.db, self.cache_manager, str(Path(self.test_dir) / "history.db")
        )

    def tearDown(self):
        self.db_worker.stop()
        from app.core.db_conn import clear_connection_cache
        clear_connection_cache(only_current_and_inactive=False)
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_verifier_check_collisions_rename_policy(self):
        """Test that check_collisions allows duplicate target filenames without raising duplicate_target when conflict_policy='rename'."""
        src1 = os.path.join(self.base_dir, "dir1", "report.pdf")
        src2 = os.path.join(self.base_dir, "dir2", "report.pdf")
        plan = {
            "Documents": {
                "file1.pdf": {
                    "__type__": "file",
                    "source_path": src1,
                    "relative_source": "dir1/report.pdf",
                    "target_filename": "report.pdf",
                },
                "file2.pdf": {
                    "__type__": "file",
                    "source_path": src2,
                    "relative_source": "dir2/report.pdf",
                    "target_filename": "report.pdf",
                },
            }
        }
        tracker = VirtualFilesystemTracker()
        moves_list = VerificationEngine.get_moves(self.base_dir, plan)
        tracker.populate_from_moves(moves_list)

        collisions_rename = tracker.check_collisions(moves_list, self.base_dir, conflict_policy="rename")
        dup_targets_rename = [c for c in collisions_rename if c.get("type") == "duplicate_target"]
        self.assertEqual(dup_targets_rename, [])

        collisions_skip = tracker.check_collisions(moves_list, self.base_dir, conflict_policy="skip")
        dup_targets_skip = [c for c in collisions_skip if c.get("type") == "duplicate_target"]
        self.assertEqual(len(dup_targets_skip), 1)

    def test_update_path_map_entry_and_case_normalization(self):
        """Test path_map dynamic update and case-insensitive resolution."""
        path_map = {
            os.path.normcase(os.path.abspath("/base/src/doc.pdf")): os.path.abspath("/base/Documents/doc.pdf")
        }
        src = os.path.abspath("/base/src/doc.pdf")
        new_dest = os.path.abspath("/base/Documents/doc_1.pdf")
        _update_path_map_entry(path_map, src, new_dest)

        # Look up with different casing
        resolved = resolve_new_target(src.upper(), path_map)
        self.assertEqual(os.path.normcase(resolved), os.path.normcase(new_dest))

    def test_dynamic_rename_link_resolution_execution(self):
        """Test that a collision rename updates path_map dynamically and points symlink to suffixed destination."""
        # Pre-create an existing file at destination to cause collision
        existing_dest_file = os.path.join(self.dest_dir, "report.pdf")
        with open(existing_dest_file, "w") as f:
            f.write("Existing file in dest")

        # Create source file
        src_file = os.path.join(self.base_dir, "report.pdf")
        with open(src_file, "w") as f:
            f.write("Source file content")

        # Create source symlink pointing to source file
        symlink_path = os.path.join(self.base_dir, "report_link.pdf")
        try:
            os.symlink(src_file, symlink_path)
        except (OSError, NotImplementedError):
            self.skipTest("Symlinks not supported on this platform/privilege level")

        plan = {
            "Documents": {
                "report.pdf": {
                    "__type__": "file",
                    "source_path": src_file,
                    "relative_source": "report.pdf",
                    "target_filename": "report.pdf",
                },
                "report_link.pdf": {
                    "__type__": "file",
                    "source_path": symlink_path,
                    "relative_source": "report_link.pdf",
                    "target_filename": "report_link.pdf",
                },
            }
        }

        # Verify plan integrity passes
        integrity = VerificationEngine.verify_plan_integrity(self.base_dir, plan)
        self.assertEqual(integrity.get("invalid_renames") or [], [])

        # Execute moves
        summary = execute_moves(
            self.base_dir, plan, db=self.db, history_manager=self.history_manager
        )
        self.assertFalse(summary.get("cancelled", False))

        # Destination report_1.pdf should exist
        expected_suffixed_target = os.path.join(self.dest_dir, "report_1.pdf")
        self.assertTrue(os.path.exists(expected_suffixed_target))

        # Destination symlink should exist and readlink should point to report_1.pdf
        dest_symlink = os.path.join(self.dest_dir, "report_link.pdf")
        self.assertTrue(os.path.lexists(dest_symlink))
        symlink_target = os.readlink(dest_symlink)
        self.assertIn("report_1.pdf", symlink_target)

    def test_step_ledger_and_rollback_from_dynamically_suffixed_target(self):
        """Test step ledger logging and rollback from dynamically suffixed target paths."""
        session_id = self.history_manager.create_snapshot(self.base_dir)
        src_file = os.path.join(self.base_dir, "data.txt")
        with open(src_file, "w") as f:
            f.write("source data")

        suffixed_target = os.path.join(self.dest_dir, "data_1.txt")
        # Perform move
        shutil.move(src_file, suffixed_target)

        # Log step into step ledger
        self.history_manager.log_step(
            session_id=session_id,
            source_path=src_file,
            target_path=suffixed_target,
            original_filename="data.txt",
            is_collision_renamed=True,
        )

        # Unwind session
        unwound = self.history_manager.unwind_session(session_id, db=self.db)
        self.assertEqual(unwound, 1)
        self.assertTrue(os.path.exists(src_file))
        self.assertFalse(os.path.exists(suffixed_target))


if __name__ == "__main__":
    unittest.main()
