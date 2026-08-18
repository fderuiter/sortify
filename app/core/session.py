"""Session manager module for encapsulating app state."""

import json
import os
import shutil

try:
    import sqlite3
except Exception:
    try:
        from sqlcipher3 import dbapi2 as sqlite3
    except Exception:
        sqlite3 = None

from app.config import get_app_dir
from app.core.analyzer import IncrementalAnalyzer
from app.core.cache import CacheManager
from app.core.db import Database
from app.core.history import HistoryManager


async def scan_abandoned_sessions_async():
    """Scan for unclosed session folders containing active session databases or failed/active sessions with trapped files."""
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

            journal_path = session_dir / "rollback_journal.json"
            if journal_path.exists():
                try:
                    with open(journal_path, "r") as f:
                        jdata = json.load(f)
                    abandoned.append(
                        {
                            "session_id": jdata.get("session_id"),
                            "safety_session_id": jdata.get("safety_session_id"),
                            "base_dir": jdata.get("base_dir"),
                            "session_dir": str(session_dir),
                            "journal_path": str(journal_path),
                            "is_rollback_recovery": True,
                            "status": "interrupted_rollback",
                        }
                    )
                    continue
                except Exception:
                    pass

            history_db = session_dir / "history.db"
            if not history_db.exists():
                continue

            try:
                from contextlib import closing

                with closing(sqlite3.connect(history_db, timeout=30.0)) as conn:
                    with closing(conn.cursor()) as cursor:
                        cursor.execute(
                            "SELECT session_id, base_dir, status FROM sessions ORDER BY timestamp DESC"
                        )
                        rows = cursor.fetchall()

                trapped_sessions = []
                for row in rows:
                    sid, base_dir, status = row
                    if status in ("active", "failed"):
                        branch_dir = os.path.join(base_dir, ".branches", sid)
                        has_files = False
                        if os.path.exists(branch_dir):
                            for root, dirs, files in os.walk(branch_dir):
                                if files:
                                    has_files = True
                                    break
                        if has_files:
                            trapped_sessions.append(
                                {
                                    "session_id": sid,
                                    "base_dir": base_dir,
                                    "session_dir": str(session_dir),
                                    "status": status,
                                    "has_trapped_files": True,
                                    "safety_folder": branch_dir,
                                }
                            )

                if trapped_sessions:
                    abandoned.extend(trapped_sessions)
                else:
                    plan_path = session_dir / "plan.json"
                    if plan_path.exists() and rows:
                        for row in rows:
                            sid, base_dir, status = row
                            if status == "active":
                                abandoned.append(
                                    {
                                        "session_id": sid,
                                        "base_dir": base_dir,
                                        "session_dir": str(session_dir),
                                        "plan_path": str(plan_path),
                                        "status": status,
                                        "has_trapped_files": False,
                                    }
                                )
                                break
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

        active_model_path = None
        if self.settings.AI_CONSENT_GRANTED:
            base_path = get_base_path(__file__)
            local_model_path = os.path.join(base_path, "offline_bundle", "model")
            try:
                user_model_path = str(get_app_dir() / "model")
            except Exception:
                user_model_path = None

            try:
                from app.core.offline_loader import OfflineModelLoader

                active_model_path = OfflineModelLoader.resolve_model_path("model")
            except Exception:
                active_model_path = None

            if not active_model_path:
                if os.path.exists(local_model_path):
                    active_model_path = local_model_path
                elif user_model_path and os.path.exists(user_model_path):
                    active_model_path = user_model_path

        model_path = active_model_path if self.settings.AI_CONSENT_GRANTED else None
        configured_strat = getattr(self.settings, "SORTING_STRATEGY", "default")
        if configured_strat in ("clinical_tmf", "clinical_isf"):
            strategy_name = configured_strat
            from app.core.analyzer_strategies import clustering_registry

            strat = clustering_registry.get_strategy(strategy_name)
            if strat and hasattr(strat, "smart_renaming"):
                strat.smart_renaming = getattr(
                    self.settings, "CLINICAL_SMART_RENAMING", False
                )
                strat.generate_audit_report = getattr(
                    self.settings, "CLINICAL_GENERATE_AUDIT_REPORT", True
                )
                strat.base_dir = self.base_dir
        else:
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

    async def process_items_async(
        self, items_to_sort, cancel_check, progress_callback=None
    ):
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
            progress_callback=progress_callback,
        ):
            yield item, text, file_hash, was_skipped

    def partial_fit(self, chunk):
        """Incrementally train the analyzer."""
        if not self.base_dir:
            return
        self.analyzer.partial_fit(self.base_dir, chunk, self.settings)

    def generate_sorting_plan(self, fast_path_only: bool = False):
        """Generate sorting plan from analyzer."""
        if not self.base_dir:
            return {}
        _, locked, _, _ = self.cache_manager.load_cache(self.base_dir)
        return self.analyzer.generate_sorting_plan(
            self.base_dir,
            self.settings,
            locked_files=locked,
            fast_path_only=fast_path_only,
        )

    def rollback(self, session_id, ignore_missing=False):
        """Rollback a past session."""
        self.history_manager.rollback(session_id, ignore_missing=ignore_missing)

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
