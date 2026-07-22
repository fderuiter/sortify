"""Session manager module for encapsulating app state."""

import os
import shutil
import tempfile
import uuid
from pathlib import Path

from app.config import get_app_dir
from app.core.analyzer import IncrementalAnalyzer
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.history import HistoryManager


class AppSession:
    """Encapsulates the core business logic, analytics, and database services for a single application run."""
    
    def __init__(self, settings, base_dir=None):
        self.settings = settings
        self.base_dir = base_dir
        self.session_id = str(uuid.uuid4())
        self.session_dir = Path(tempfile.gettempdir()) / "autosorter_sessions" / self.session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        
        from app.core.db_worker import DBWorker
        self.db_worker = DBWorker()
        self.db = Database(self.session_dir / "autosorter.db", self.db_worker)
        self.cache_manager = CacheManager(str(self.session_dir / "cache.db"), self.db_worker)
        self.history_manager = HistoryManager(self.db, self.cache_manager, str(self.session_dir / "history.db"))
        
        user_model_path = get_app_dir() / "model"
        model_path = str(user_model_path) if self.settings.AI_CONSENT_GRANTED else None
        
        self.analyzer = IncrementalAnalyzer(
            self.settings.MAX_FOLDERS,
            self.settings.STOP_WORDS,
            self.db,
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
            cancel_check=cancel_check
        ):
            yield chunk

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
        return self.analyzer.generate_sorting_plan(self.base_dir, self.settings, locked_files=locked)

    def save_cache_async(self, locked_files, manual_folders):
        """Save the cache asynchronously."""
        if not self.base_dir:
            return
        self.cache_manager.save_cache_async(
            self.base_dir, self.analyzer.corpus, locked_files, {}, manual_folders
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

    def execute_moves(self, plan):
        """Execute move operations."""
        if not self.base_dir:
            return {}
        from app.core.mover import execute_moves
        return execute_moves(self.base_dir, plan, self.db, self.history_manager, self.settings)

    def close(self):
        """Cleanup session directory."""
        if hasattr(self, "analyzer") and self.analyzer:
            self.analyzer.terminate()
        if hasattr(self, "db_worker") and self.db_worker:
            self.db_worker.stop()
        if self.session_dir and os.path.exists(self.session_dir):
            shutil.rmtree(self.session_dir, ignore_errors=True)
