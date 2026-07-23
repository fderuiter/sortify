"""Session manager module for encapsulating app state."""

import json
import os
import shutil
import sqlite3

from app.config import get_app_dir
from app.core.analyzer import IncrementalAnalyzer
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.history import HistoryManager


async def scan_abandoned_sessions_async():
    """Scan for unclosed session folders containing active session databases."""
    import asyncio

    def _scan():
        from app.core.path_utils import get_session_base_dir

        session_base = get_session_base_dir()
        abandoned = []
        if not session_base.exists():
            return abandoned

        for session_dir in session_base.iterdir():
            if not session_dir.is_dir():
                continue

            plan_path = session_dir / "plan.json"
            if not plan_path.exists():
                continue

            history_db = session_dir / "history.db"
            if not history_db.exists():
                continue

            try:
                conn = sqlite3.connect(history_db)
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT session_id, base_dir, status FROM sessions ORDER BY timestamp DESC LIMIT 1"
                )
                row = cursor.fetchone()
                conn.close()

                if row and row[2] == "active":
                    abandoned.append(
                        {
                            "session_id": row[0],
                            "base_dir": row[1],
                            "session_dir": str(session_dir),
                            "plan_path": str(plan_path),
                        }
                    )
            except Exception:
                pass

        return abandoned

    return await asyncio.to_thread(_scan)


class AppSession:
    """Encapsulates the core business logic, analytics, and database services for a single application run."""

    def __init__(self, settings, base_dir=None, session_id=None):
        self.settings = settings
        self.base_dir = base_dir

        from app.core.path_utils import get_base_path, setup_session_directory

        self.session_id, self.session_dir = setup_session_directory(session_id)

        from app.core.db_worker import DBWorker

        self.db_worker = DBWorker()
        self.db = Database(self.session_dir / "autosorter.db", self.db_worker)
        self.cache_manager = CacheManager(
            str(self.session_dir / "cache.db"), self.db_worker
        )
        self.history_manager = HistoryManager(
            self.db, self.cache_manager, str(self.session_dir / "history.db")
        )

        base_path = get_base_path(__file__)

        local_model_path = os.path.join(base_path, "offline_bundle", "model")
        user_model_path = str(get_app_dir() / "model")

        active_model_path = None
        if os.path.exists(local_model_path):
            active_model_path = local_model_path
        elif os.path.exists(user_model_path):
            active_model_path = user_model_path

        model_path = active_model_path if self.settings.AI_CONSENT_GRANTED else None
        strategy_name = (
            "generative"
            if getattr(self.settings, "AI_ASSISTED_NAMING", False)
            else "default"
        )

        self.analyzer = IncrementalAnalyzer(
            self.settings.MAX_FOLDERS,
            self.settings.STOP_WORDS,
            self.db,
            strategy_name=strategy_name,
            model_path=model_path,
        )

    def save_cache_sync(self, locked_files, manual_folders):
        """Save the cache synchronously."""
        if not self.base_dir:
            return
        self.cache_manager.save_cache_sync(
            self.base_dir, self.analyzer.corpus, locked_files, {}, manual_folders
        )

    def process_items(self, items_to_sort, callback, cancel_check):
        """Build corpus generator for files."""
        if not self.base_dir:
            return
        from app.core.extractor import build_corpus_generator

        for chunk in build_corpus_generator(
            self.base_dir,
            items_to_sort,
            callback,
            max_workers=self.settings.MAX_WORKERS,
            db=self.db,
            chunk_size=50,
            cancel_check=cancel_check,
            settings=self.settings,
        ):
            yield chunk

    async def process_items_async(self, items_to_sort, cancel_check):
        """Build corpus asynchronous generator for files, yielded file-by-file sequentially."""
        if not self.base_dir:
            return
        from app.core.extractor import build_corpus_generator_async

        async for item, text, file_hash, was_skipped in build_corpus_generator_async(
            self.base_dir,
            items_to_sort,
            db=self.db,
            cancel_check=cancel_check,
            settings=self.settings,
        ):
            yield item, text, file_hash, was_skipped

    def partial_fit(self, chunk):
        """Incrementally train the analyzer."""
        if not self.base_dir:
            return
        self.analyzer.partial_fit(self.base_dir, chunk, self.settings)

    def generate_sorting_plan(self):
        """Generate sorting plan from analyzer."""
        if not self.base_dir:
            return {}
        _, locked, _, _ = self.cache_manager.load_cache(self.base_dir)
        return self.analyzer.generate_sorting_plan(
            self.base_dir, self.settings, locked_files=locked
        )

    def load_cache(self):
        """Load the cache for the current session."""
        if not self.base_dir:
            return None, None, None, None
        return self.cache_manager.load_cache(self.base_dir)

    def get_sessions(self):
        """Get history sessions."""
        return self.history_manager.get_sessions()

    def check_missing_files(self, session_id):
        """Check if files are missing from a session."""
        return self.history_manager.check_missing_files(session_id)

    def rollback(self, session_id, ignore_missing=False):
        """Rollback a past session."""
        self.history_manager.rollback(session_id, ignore_missing=ignore_missing)

    def update_document_path(self, old_path, new_path):
        """Update document paths within session bounds."""
        if not self.base_dir:
            return
        if old_path in self.analyzer.corpus:
            self.analyzer.corpus[new_path] = self.analyzer.corpus.pop(old_path)
        self.db.update_document_path(self.base_dir, old_path, new_path)

    def remove_document(self, path):
        """Remove document from session and db."""
        if not self.base_dir:
            return
        if path in self.analyzer.corpus:
            del self.analyzer.corpus[path]
        self.db.remove_document(self.base_dir, path)

    def execute_moves(self, plan, resume=False):
        """Execute move operations."""
        if not self.base_dir:
            return {}

        plan_path = self.session_dir / "plan.json"
        with open(plan_path, "w") as f:
            json.dump(plan, f)

        from app.core.mover import execute_moves

        return execute_moves(
            self.base_dir,
            plan,
            self.db,
            self.history_manager,
            self.settings,
            resume=resume,
        )

    def close(self):
        """Cleanup session directory."""
        if hasattr(self, "analyzer") and self.analyzer:
            self.analyzer.terminate()
        if hasattr(self, "db_worker") and self.db_worker:
            self.db_worker.stop()
        if self.session_dir and os.path.exists(self.session_dir):
            shutil.rmtree(self.session_dir, ignore_errors=True)
