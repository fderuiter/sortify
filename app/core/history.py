"""History management module for snapshotting and rollback."""

import logging
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List

from app.core.db_conn import get_db_connection
from app.core.path_utils import is_junction_path, safe_relpath

try:
    import pylnk3
except ImportError:
    pylnk3 = None


# Session status of the safety snapshot an undo takes before it touches files.
UNDO_SNAPSHOT_STATUS = "undo_snapshot"


def _robust_move(src, dst):
    """Safely move a file, retrying on temporary Windows sharing violations and file locks."""
    from app.core.resilient_file_ops import resilient_move

    resilient_move(src, dst)


def _remove_staging_leftovers(base_dir: str) -> None:
    """Delete transient ``.tmp_stage_*`` files left by interrupted moves, and nothing else."""
    from app.core.resilient_file_ops import resilient_remove

    for root, _dirs, files in os.walk(base_dir):
        for name in files:
            if name.startswith(".tmp_stage_"):
                try:
                    resilient_remove(os.path.join(root, name))
                except OSError as exc:
                    logging.warning(f"Could not remove staging file {name}: {exc}")


def _purge_overwrite_backups(base_dir: str, session_id: str) -> None:
    """Delete the files a pruned session's overwrites displaced (ADR 0005 point 3)."""
    from app.core.mover import OVERWRITE_BACKUP_DIR, overwrite_backup_dir
    from app.core.resilient_file_ops import resilient_rmtree

    backup_dir = overwrite_backup_dir(base_dir, session_id)
    if not os.path.isdir(backup_dir) or os.path.islink(backup_dir):
        return
    try:
        resilient_rmtree(backup_dir)
        root = os.path.join(base_dir, OVERWRITE_BACKUP_DIR)
        if not os.listdir(root):
            os.rmdir(root)
    except OSError as err:
        logging.warning(f"Could not purge overwrite backups in {backup_dir}: {err}")


def _is_same_restored_path(actual: str, expected: str) -> bool:
    return os.path.normcase(os.path.abspath(actual)) == os.path.normcase(
        os.path.abspath(expected)
    )


class HistoryManager:
    """Manages full directory snapshots and rollback functionality."""

    def __init__(self, db, cache_manager, db_path=None):
        from pathlib import Path

        self.db = db
        self.cache_manager = cache_manager
        self.db_path = db_path or str(Path(db.db_path).parent / "history.db")
        self._init_db()

    def _init_db(self):
        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    timestamp REAL,
                    base_dir TEXT,
                    status TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshot_files (
                    session_id TEXT,
                    original_rel_path TEXT,
                    inode INTEGER,
                    size INTEGER,
                    mtime REAL,
                    is_symlink INTEGER DEFAULT 0,
                    symlink_target TEXT,
                    link_type TEXT,
                    arguments TEXT,
                    description TEXT,
                    icon_file TEXT,
                    icon_index INTEGER,
                    work_dir TEXT,
                    window_mode INTEGER,
                    file_hash TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)
            try:
                conn.execute(
                    "ALTER TABLE snapshot_files ADD COLUMN is_symlink INTEGER DEFAULT 0"
                )
            except Exception:
                pass
            try:
                conn.execute(
                    "ALTER TABLE snapshot_files ADD COLUMN symlink_target TEXT"
                )
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE snapshot_files ADD COLUMN file_hash TEXT")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE snapshot_files ADD COLUMN link_type TEXT")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE snapshot_files ADD COLUMN arguments TEXT")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE snapshot_files ADD COLUMN description TEXT")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE snapshot_files ADD COLUMN icon_file TEXT")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE snapshot_files ADD COLUMN icon_index INTEGER")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE snapshot_files ADD COLUMN work_dir TEXT")
            except Exception:
                pass
            try:
                conn.execute(
                    "ALTER TABLE snapshot_files ADD COLUMN window_mode INTEGER"
                )
            except Exception:
                pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshot_cache (
                    session_id TEXT PRIMARY KEY,
                    corpus TEXT,
                    locked_files TEXT,
                    index_to_word TEXT,
                    manual_folders TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)
            try:
                conn.execute(
                    "ALTER TABLE snapshot_documents ADD COLUMN user_verified_target_path TEXT"
                )
            except Exception:
                pass
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshot_documents (
                    session_id TEXT,
                    filepath TEXT,
                    file_hash TEXT,
                    extracted_text TEXT,
                    user_verified_target_path TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS step_ledger (
                    step_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT,
                    source_path TEXT,
                    target_path TEXT,
                    original_path TEXT,
                    original_filename TEXT,
                    is_cross_volume INTEGER DEFAULT 0,
                    is_collision_renamed INTEGER DEFAULT 0,
                    file_hash TEXT,
                    timestamp REAL,
                    status TEXT DEFAULT 'completed',
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_step_ledger_session ON step_ledger(session_id)"
            )

    def _create_snapshot_internal(self, base_dir: str) -> str:
        session_id = str(uuid.uuid4())
        timestamp = time.time()

        from app.core.link_manager import LinkManager
        from app.core.scanner import get_files_recursively

        files = get_files_recursively(base_dir)

        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
                (session_id, timestamp, base_dir, "active"),
            )

            # 1. Snapshot Files
            file_records = []
            for rel_path in files:
                abs_path = os.path.join(base_dir, rel_path)
                try:
                    st = os.lstat(abs_path)
                    is_symlink = 1 if os.path.islink(abs_path) else 0
                    is_junc = 1 if is_junction_path(abs_path) else 0
                    is_lnk = 1 if abs_path.lower().endswith(".lnk") else 0
                    is_link_entity = bool(is_symlink or is_junc or is_lnk)

                    link_type = None
                    link_target = None
                    arguments = None
                    description = None
                    icon_file = None
                    icon_index = 0
                    work_dir = None
                    window_mode = None

                    if is_junc:
                        link_type = "junction"
                        try:
                            link_target = os.readlink(abs_path)
                        except OSError:
                            info = LinkManager.get_link_info(abs_path)
                            if info:
                                link_target = info.get("target")
                    elif is_symlink:
                        link_type = "symlink"
                        try:
                            link_target = os.readlink(abs_path)
                        except OSError:
                            pass
                    elif is_lnk:
                        link_type = "lnk"
                        if pylnk3:
                            try:
                                parsed = pylnk3.parse(abs_path)
                                link_target = parsed.path
                                arguments = parsed.arguments
                                description = parsed.description
                                icon_file = parsed.icon
                                icon_index = getattr(parsed, "icon_index", 0)
                                work_dir = parsed.work_dir
                                window_mode = parsed.window_mode
                            except Exception:
                                info = LinkManager.get_link_info(abs_path)
                                if info:
                                    link_target = info.get("target")
                        else:
                            info = LinkManager.get_link_info(abs_path)
                            if info:
                                link_target = info.get("target")

                    file_hash = None
                    if not is_link_entity and st.st_size > 0:
                        try:
                            from app.core.extractor import get_file_hash

                            file_hash = get_file_hash(abs_path)
                        except Exception:
                            pass

                    file_records.append(
                        (
                            session_id,
                            rel_path.replace("\\", "/"),
                            st.st_ino,
                            st.st_size,
                            st.st_mtime,
                            is_symlink,
                            is_symlink
                            and (
                                link_target or os.readlink(abs_path)
                                if is_symlink
                                else None
                            )
                            or link_target,
                            link_type,
                            arguments,
                            description,
                            icon_file,
                            icon_index,
                            work_dir,
                            window_mode,
                            file_hash,
                        )
                    )
                except OSError:
                    continue
            if file_records:
                conn.executemany(
                    """
                    INSERT INTO snapshot_files (
                        session_id, original_rel_path, inode, size, mtime, 
                        is_symlink, symlink_target, link_type, arguments, 
                        description, icon_file, icon_index, work_dir, 
                        window_mode, file_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    file_records,
                )

            # 2. Snapshot Cache
            cache_conn = self.cache_manager._get_conn()
            with cache_conn:
                cur = cache_conn.execute(
                    "SELECT corpus, locked_files, index_to_word, manual_folders FROM directory_cache WHERE source_directory = ?",
                    (base_dir,),
                )
                row = cur.fetchone()
            if row:
                conn.execute(
                    "INSERT INTO snapshot_cache (session_id, corpus, locked_files, index_to_word, manual_folders) VALUES (?, ?, ?, ?, ?)",
                    (session_id, row[0], row[1], row[2], row[3]),
                )

            # 3. Snapshot DB
            docs = []
            db_conn = get_db_connection(self.db.db_path)
            with db_conn:
                try:
                    cur = db_conn.execute(
                        "SELECT filepath, file_hash, extracted_text, user_verified_target_path FROM documents WHERE base_dir = ?",
                        (base_dir,),
                    )
                    for r in cur.fetchall():
                        docs.append(
                            (
                                session_id,
                                r[0].replace("\\", "/"),
                                r[1],
                                r[2],
                                r[3] if len(r) > 3 else None,
                            )
                        )
                except Exception:
                    cur = db_conn.execute(
                        "SELECT filepath, file_hash, extracted_text FROM documents WHERE base_dir = ?",
                        (base_dir,),
                    )
                    for r in cur.fetchall():
                        docs.append(
                            (session_id, r[0].replace("\\", "/"), r[1], r[2], None)
                        )
            if docs:
                try:
                    conn.executemany(
                        "INSERT INTO snapshot_documents (session_id, filepath, file_hash, extracted_text, user_verified_target_path) VALUES (?, ?, ?, ?, ?)",
                        docs,
                    )
                except Exception:
                    conn.executemany(
                        "INSERT INTO snapshot_documents (session_id, filepath, file_hash, extracted_text) VALUES (?, ?, ?, ?)",
                        [d[:4] for d in docs],
                    )

            # Prune expired snapshots based on configured age retention limit
            policies = None
            try:
                from app.config import AppSettings

                app_settings = AppSettings()
                policies = getattr(app_settings, "UNIFIED_POLICIES", None) or getattr(
                    app_settings, "POLICIES", None
                )
            except Exception:
                pass
            self._prune_snapshots(conn, policies=policies)

        return session_id

    def create_snapshot(self, base_dir: str) -> str:
        """Create a complete snapshot of the directory tree and its metadata."""

        def _write():
            return self._create_snapshot_internal(base_dir)

        return self.db.worker.execute_write(_write)

    def _prune_snapshots(self, conn, retention_days=None, limit=None, policies=None):
        if retention_days is None:
            try:
                from app.config import AppSettings

                app_settings = AppSettings()
                retention_days = getattr(app_settings, "SNAPSHOT_RETENTION_DAYS", 30)
            except Exception:
                retention_days = 30

        cutoff_timestamp = time.time() - (float(retention_days) * 86400.0)
        cur = conn.execute(
            "SELECT session_id, base_dir, status FROM sessions WHERE timestamp IS NOT NULL AND timestamp < ?",
            (cutoff_timestamp,),
        )
        old_sessions = cur.fetchall()
        for sid, base_dir, status in old_sessions:
            if status and str(status).lower() in (
                "active",
                "retain",
                "retained",
                "archive",
                "archived",
            ):
                continue

            # Never prune divergent history branches that contain unmerged user data
            branch_dir = os.path.join(base_dir, ".branches", sid)
            if os.path.exists(branch_dir) and os.listdir(branch_dir):
                continue

            if policies:
                retained_by_policy = False
                try:
                    from app.core.policy_engine import PolicyEngine

                    cur_files = conn.execute(
                        "SELECT original_rel_path FROM snapshot_files WHERE session_id = ?",
                        (sid,),
                    ).fetchall()
                    file_paths = [r[0] for r in cur_files] or [base_dir]

                    for rule in policies:
                        act = (
                            rule.get("action", "").lower()
                            if isinstance(rule, dict)
                            else getattr(rule, "action", "").lower()
                        )
                        if act in ("retain", "archive"):
                            expr = (
                                rule.get("expression", "")
                                if isinstance(rule, dict)
                                else getattr(rule, "expression", "")
                            )
                            if not expr:
                                continue
                            for fp in file_paths:
                                if PolicyEngine.match_policy(rule, fp, "", ""):
                                    retained_by_policy = True
                                    break
                        if retained_by_policy:
                            break
                except Exception as ex:
                    logging.warning(
                        f"Error evaluating policies during snapshot pruning: {ex}"
                    )
                if retained_by_policy:
                    continue

            conn.execute("DELETE FROM snapshot_files WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM snapshot_cache WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM snapshot_documents WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM step_ledger WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))
            _purge_overwrite_backups(base_dir, sid)

    def get_sessions(self) -> List[Dict[str, Any]]:
        """Retrieve a list of all historical sessions, ordered by time."""
        conn = get_db_connection(self.db_path)
        with conn:
            cur = conn.execute(
                "SELECT session_id, timestamp, base_dir, status FROM sessions ORDER BY timestamp DESC"
            )
            sessions = []
            for r in cur.fetchall():
                sid, ts, b_dir, status = r
                step_count = 0
                try:
                    c = conn.execute(
                        "SELECT COUNT(*) FROM step_ledger WHERE session_id = ?",
                        (sid,),
                    )
                    step_count = c.fetchone()[0]
                except Exception:
                    pass

                file_count = 0
                try:
                    c = conn.execute(
                        "SELECT COUNT(*) FROM snapshot_files WHERE session_id = ?",
                        (sid,),
                    )
                    file_count = c.fetchone()[0]
                except Exception:
                    pass

                sessions.append(
                    {
                        "session_id": sid,
                        "timestamp": ts,
                        "base_dir": b_dir,
                        "status": status,
                        "step_count": step_count,
                        "file_count": file_count,
                    }
                )
            return sessions

    def check_missing_files(self, session_id: str) -> List[str]:
        """Check if any files from the snapshot are missing from the disk."""
        conn = get_db_connection(self.db_path)
        with conn:
            cur = conn.execute(
                "SELECT base_dir FROM sessions WHERE session_id = ?", (session_id,)
            )
            row = cur.fetchone()
            if not row:
                raise ValueError("Session not found")
            base_dir = row[0]

            try:
                cur = conn.execute(
                    "SELECT original_rel_path, inode, size, mtime, is_symlink, symlink_target, file_hash, link_type FROM snapshot_files WHERE session_id = ?",
                    (session_id,),
                )
                snapshot_files = cur.fetchall()
            except Exception:
                try:
                    cur = conn.execute(
                        "SELECT original_rel_path, inode, size, mtime, is_symlink, symlink_target, file_hash FROM snapshot_files WHERE session_id = ?",
                        (session_id,),
                    )
                    snapshot_files = [(*row, None) for row in cur.fetchall()]
                except Exception:
                    cur = conn.execute(
                        "SELECT original_rel_path, inode, size, mtime, is_symlink, symlink_target FROM snapshot_files WHERE session_id = ?",
                        (session_id,),
                    )
                    snapshot_files = [(*row, None, None) for row in cur.fetchall()]

        from app.core.scanner import get_files_recursively

        current_files = get_files_recursively(base_dir, include_hidden=True)

        inode_counts: dict[Any, Any] = {}
        current_inodes: dict[Any, Any] = {}
        active_files_by_rel_path: dict[Any, Any] = {}
        active_files_by_sig: dict[Any, Any] = {}
        inodes_reliable = True

        for rel_path in current_files:
            abs_path = os.path.join(base_dir, rel_path)
            try:
                st = os.lstat(abs_path)
            except OSError:
                continue

            ino = st.st_ino
            size = st.st_size
            mtime = st.st_mtime
            is_symlink = 1 if os.path.islink(abs_path) else 0
            symlink_target = os.readlink(abs_path) if is_symlink else None

            inode_counts[ino] = inode_counts.get(ino, 0) + 1
            if ino == 0 or inode_counts[ino] > 1:
                inodes_reliable = False

            sig = (size, mtime, is_symlink, symlink_target)
            current_inodes[ino] = (abs_path, sig)
            active_files_by_rel_path[rel_path.lower().replace("\\", "/")] = sig

            if sig not in active_files_by_sig:
                active_files_by_sig[sig] = []
            active_files_by_sig[sig].append(abs_path)

        def verify_hash(abs_path, expected_hash, expected_size=0):
            if not expected_hash:
                return True

            import gc
            import sys
            import time

            from app.core.extractor import get_file_hash
            from app.core.resilient_file_ops import MAX_ATTEMPTS, RETRY_DELAY

            for attempt in range(MAX_ATTEMPTS):
                try:
                    h = get_file_hash(abs_path)
                    if h == expected_hash:
                        return True

                    # On Windows, always retry if the hash doesn't match yet
                    if sys.platform != "win32":
                        return False
                except Exception:
                    if sys.platform != "win32":
                        return False

                if attempt == MAX_ATTEMPTS - 1:
                    break

                gc.collect()
                if RETRY_DELAY > 0:
                    time.sleep(RETRY_DELAY)

            return False

        missing = []
        for (
            rel_path,
            inode,
            size,
            mtime,
            is_symlink,
            symlink_target,
            file_hash,
            link_type,
        ) in snapshot_files:
            is_lnk = (
                1 if (link_type == "lnk" or rel_path.lower().endswith(".lnk")) else 0
            )
            is_link_entity = bool(is_symlink or is_lnk)

            found = False
            target_sig = (size, mtime, is_symlink, symlink_target)

            if inodes_reliable and inode in current_inodes:
                abs_path, current_sig = current_inodes[inode]
                if is_link_entity:
                    del current_inodes[inode]
                    found = True
                else:
                    if current_sig[2] == is_symlink:
                        if not is_symlink or current_sig[3] == symlink_target:
                            if verify_hash(abs_path, file_hash, size):
                                del current_inodes[inode]
                                found = True

            if not found:
                # Fallback Step A
                curr_sig = active_files_by_rel_path.get(
                    rel_path.lower().replace("\\", "/")
                )
                if is_link_entity:
                    if curr_sig is not None:
                        found = True
                else:
                    if curr_sig == target_sig:
                        abs_path = os.path.join(base_dir, rel_path)
                        if verify_hash(abs_path, file_hash, size):
                            if (
                                curr_sig in active_files_by_sig
                                and abs_path in active_files_by_sig[curr_sig]
                            ):
                                active_files_by_sig[curr_sig].remove(abs_path)
                            found = True
                if not found:
                    # Fallback Step B
                    if not is_link_entity:
                        if (
                            target_sig in active_files_by_sig
                            and active_files_by_sig[target_sig]
                        ):
                            for idx, cand_path in enumerate(
                                active_files_by_sig[target_sig]
                            ):
                                if verify_hash(cand_path, file_hash, size):
                                    active_files_by_sig[target_sig].pop(idx)
                                    found = True
                                    break

            if not found:
                if not is_link_entity:
                    missing.append(rel_path)

        return missing

    def _rollback_from_steps(
        self,
        session_id: str,
        steps: list,
        base_dir: str,
        safety_session_id: str,
        ignore_missing: bool = False,
    ):
        """Replay recorded transaction ledger step entries in reverse order."""
        import json
        import logging
        import sys
        from pathlib import Path

        from app.core.extractor import get_file_hash
        from app.core.resilient_file_ops import resilient_remove

        def verify_hash(abs_path, expected_hash, expected_size=0):
            if not expected_hash:
                return True
            EMPTY_SHA256 = (
                "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
            )
            max_attempts = 30 if sys.platform == "win32" else 10
            sleep_time = 0.1 if sys.platform == "win32" else 0.05

            for attempt in range(max_attempts):
                try:
                    h = get_file_hash(abs_path)
                    if h == expected_hash:
                        return True
                    if sys.platform == "win32":
                        import gc

                        gc.collect()
                        time.sleep(sleep_time)
                        continue
                    if (
                        h == EMPTY_SHA256
                        and expected_hash != EMPTY_SHA256
                        and expected_size > 0
                    ):
                        import gc

                        gc.collect()
                        time.sleep(sleep_time)
                        continue
                    return False
                except Exception:
                    import gc

                    gc.collect()
                    time.sleep(sleep_time)
            return False

        reverse_steps = sorted(steps, key=lambda s: s["step_number"], reverse=True)

        # Write rollback journal before executing file reversals
        try:
            journal_path = Path(self.db_path).parent / "rollback_journal.json"
            journal_data = {
                "session_id": session_id,
                "safety_session_id": safety_session_id,
                "base_dir": base_dir,
                "steps": reverse_steps,
            }
            with open(journal_path, "w") as f:
                json.dump(journal_data, f, indent=2)
        except Exception as ex:
            logging.warning(f"Failed to write rollback journal: {ex}")

        db_conn = get_db_connection(self.db.db_path)

        failed_steps = []
        for step in reverse_steps:
            source_path = step["source_path"]
            destination_path = step["destination_path"]
            file_hash = step["file_hash"]
            item_type = step.get("item_type") or "file"
            meta = json.loads(step["metadata"]) if step.get("metadata") else {}

            curr_abs = os.path.normpath(os.path.join(base_dir, destination_path))
            orig_abs = os.path.normpath(os.path.join(base_dir, source_path))

            try:
                # Pre-step content integrity check
                if item_type == "file" and file_hash:
                    if os.path.exists(curr_abs):
                        if not verify_hash(curr_abs, file_hash):
                            logging.error(
                                f"Pre-step file hash mismatch for {destination_path}. Halting step replay and restoring state from safety snapshot."
                            )
                            self._rollback_from_snapshot_internal(
                                safety_session_id, ignore_missing=True
                            )
                            raise ValueError(
                                f"Rollback halted: file hash mismatch detected for {destination_path}. Disk state restored from safety snapshot."
                            )
                    elif not ignore_missing:
                        logging.error(
                            f"Pre-step file missing at {destination_path}. Halting step replay and restoring state from safety snapshot."
                        )
                        self._rollback_from_snapshot_internal(
                            safety_session_id, ignore_missing=True
                        )
                        raise ValueError(
                            f"Rollback halted: file missing at {destination_path}. Disk state restored from safety snapshot."
                        )

                # Replay step reversal
                if item_type == "file":
                    if curr_abs != orig_abs and os.path.exists(curr_abs):
                        os.makedirs(os.path.dirname(orig_abs), exist_ok=True)
                        if os.path.exists(orig_abs) and not os.path.samefile(
                            curr_abs, orig_abs
                        ):
                            from app.core.mover import get_safe_path

                            safe_orig = get_safe_path(
                                os.path.dirname(orig_abs), os.path.basename(orig_abs)
                            )
                            _robust_move(orig_abs, safe_orig)
                        _robust_move(curr_abs, orig_abs)

                elif item_type in ("symlink", "junction"):
                    if os.path.lexists(curr_abs) or is_junction_path(curr_abs):
                        try:
                            resilient_remove(curr_abs)
                        except OSError:
                            pass
                    target_link = meta.get("target") if meta else None
                    if target_link:
                        os.makedirs(os.path.dirname(orig_abs), exist_ok=True)
                        if item_type == "junction":
                            from app.core.mover import _create_junction

                            _create_junction(target_link, orig_abs)
                        else:
                            os.symlink(target_link, orig_abs)

                elif item_type == "lnk":
                    if os.path.lexists(curr_abs):
                        try:
                            resilient_remove(curr_abs)
                        except OSError:
                            pass
                    os.makedirs(os.path.dirname(orig_abs), exist_ok=True)
                    target_link = meta.get("target") if meta else None
                    if pylnk3 and target_link:
                        kwargs = {
                            k: v
                            for k, v in meta.items()
                            if k
                            in (
                                "arguments",
                                "description",
                                "icon_file",
                                "icon_index",
                                "work_dir",
                                "window_mode",
                            )
                        }
                        pylnk3.for_file(target_link, lnk_name=orig_abs, **kwargs)

                # Post-step content integrity check
                if item_type == "file" and file_hash:
                    if not verify_hash(orig_abs, file_hash):
                        logging.error(
                            f"Post-step file hash mismatch for {source_path}. Halting step replay and restoring state from safety snapshot."
                        )
                        self._rollback_from_snapshot_internal(
                            safety_session_id, ignore_missing=True
                        )
                        raise ValueError(
                            f"Rollback halted: post-step file hash mismatch for {source_path}. Disk state restored from safety snapshot."
                        )

                # Update core database
                with db_conn:
                    db_conn.execute(
                        "UPDATE documents SET filepath = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                        (source_path, base_dir, destination_path, destination_path),
                    )
            except Exception as step_err:
                logging.error(f"Failed step reversal: {step_err}")
                failed_steps.append((step, step_err))

        if failed_steps:
            first_err = failed_steps[0][1]
            if isinstance(first_err, ValueError):
                raise first_err
            raise PermissionError(
                f"Recovery incomplete: {len(failed_steps)} step(s) failed during rollback. Error: {first_err}"
            )

        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "UPDATE sessions SET status = 'rolled_back' WHERE session_id = ?",
                (session_id,),
            )

        # Every step reversed, so the journal no longer describes unfinished work.
        try:
            journal_path = Path(self.db_path).parent / "rollback_journal.json"
            if journal_path.exists():
                journal_path.unlink()
        except Exception as ex:
            logging.warning(f"Failed to delete rollback journal: {ex}")

    def _mark_undo_snapshot(self, session_id: str) -> None:
        """Label a pre-undo safety snapshot so it is never mistaken for a sort.

        Left "active", the startup scan reports it as interrupted work; marked
        "completed", ``undo --latest`` picks it instead of the previous sort.
        """
        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "UPDATE sessions SET status = ? WHERE session_id = ?",
                (UNDO_SNAPSHOT_STATUS, session_id),
            )

    def log_step(
        self,
        session_id: str,
        source_path: str,
        target_path: str,
        original_path: str = None,
        original_filename: str = None,
        is_cross_volume: bool = False,
        is_collision_renamed: bool = False,
        file_hash: str = None,
    ) -> int:
        """Log a single file relocation step in real time into the persistent history database."""
        if not session_id:
            return 0
        norm_src = os.path.normpath(source_path)
        norm_dst = os.path.normpath(target_path)
        orig_path = os.path.normpath(original_path or source_path)
        orig_name = original_filename or os.path.basename(orig_path)
        ts = time.time()

        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO step_ledger (
                    session_id, source_path, target_path, original_path,
                    original_filename, is_cross_volume, is_collision_renamed,
                    file_hash, timestamp, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'completed')
                """,
                (
                    session_id,
                    norm_src,
                    norm_dst,
                    orig_path,
                    orig_name,
                    1 if is_cross_volume else 0,
                    1 if is_collision_renamed else 0,
                    file_hash,
                    ts,
                ),
            )
            return cursor.lastrowid

    def get_step_ledger(self, session_id: str) -> List[Dict[str, Any]]:
        """Retrieve all recorded relocation steps for a session in chronological order."""
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                """
                SELECT step_id, session_id, source_path, target_path, original_path,
                       original_filename, is_cross_volume, is_collision_renamed,
                       file_hash, timestamp, status
                FROM step_ledger
                WHERE session_id = ?
                ORDER BY step_id ASC
                """,
                (session_id,),
            )
            rows = cursor.fetchall()
            return [
                {
                    "step_id": r[0],
                    "session_id": r[1],
                    "source_path": r[2],
                    "target_path": r[3],
                    "original_path": r[4],
                    "original_filename": r[5],
                    "is_cross_volume": bool(r[6]),
                    "is_collision_renamed": bool(r[7]),
                    "file_hash": r[8],
                    "timestamp": r[9],
                    "status": r[10],
                }
                for r in rows
            ]

    def clear_step_ledger(self, session_id: str):
        """Clean up temporary ledger records upon successful completion of a batch move operation."""
        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute("DELETE FROM step_ledger WHERE session_id = ?", (session_id,))
            conn.execute(
                "UPDATE sessions SET status = 'completed' WHERE session_id = ?",
                (session_id,),
            )

    def export_audit_log(
        self,
        output_path: str,
        session_id: Any = None,
        format: Any = None,
    ) -> Dict[str, Any]:
        """Export session history records into standard CSV or JSON audit log formats."""
        import csv
        import json
        from datetime import datetime, timezone

        db_paths: List[str] = []
        if self.db_path:
            p_abs = str(Path(self.db_path).resolve())
            if os.path.exists(p_abs):
                db_paths.append(p_abs)

        try:
            from app.core.session_catalog import find_all_history_sessions

            discovered_sessions = find_all_history_sessions()
            for s in discovered_sessions:
                hist_path = s.get("history_db_path")
                if hist_path:
                    abs_hist = str(Path(hist_path).resolve())
                    if abs_hist not in db_paths and os.path.exists(abs_hist):
                        db_paths.append(abs_hist)
        except Exception:
            pass

        if not db_paths and self.db_path:
            db_paths.append(str(Path(self.db_path).resolve()))

        sessions_map: Dict[str, Dict[str, Any]] = {}
        steps: List[Dict[str, Any]] = []
        seen_step_keys = set()

        for db_p in db_paths:
            try:
                conn = get_db_connection(db_p)
                with conn:
                    if session_id:
                        cur_sess = conn.execute(
                            "SELECT session_id, timestamp, base_dir, status FROM sessions WHERE session_id = ?",
                            (session_id,),
                        )
                    else:
                        cur_sess = conn.execute(
                            "SELECT session_id, timestamp, base_dir, status FROM sessions ORDER BY timestamp DESC"
                        )
                    for row in cur_sess.fetchall():
                        sid = row[0]
                        if sid and sid not in sessions_map:
                            sessions_map[sid] = {
                                "session_id": sid,
                                "timestamp": row[1],
                                "base_dir": row[2],
                                "status": row[3],
                            }

                    if session_id:
                        cur_steps = conn.execute(
                            """
                            SELECT step_id, session_id, source_path, target_path, original_path,
                                   original_filename, is_cross_volume, is_collision_renamed,
                                   file_hash, timestamp, status
                            FROM step_ledger
                            WHERE session_id = ?
                            ORDER BY step_id ASC
                            """,
                            (session_id,),
                        )
                    else:
                        cur_steps = conn.execute(
                            """
                            SELECT step_id, session_id, source_path, target_path, original_path,
                                   original_filename, is_cross_volume, is_collision_renamed,
                                   file_hash, timestamp, status
                            FROM step_ledger
                            ORDER BY session_id ASC, step_id ASC
                            """
                        )
                    for r in cur_steps.fetchall():
                        step_key = (r[1], r[0], r[2], r[3])
                        if step_key in seen_step_keys:
                            continue
                        seen_step_keys.add(step_key)

                        ts = r[9]
                        ts_iso = ""
                        if ts:
                            try:
                                ts_iso = datetime.fromtimestamp(
                                    ts, tz=timezone.utc
                                ).isoformat()
                            except Exception:
                                pass
                        steps.append(
                            {
                                "step_id": r[0],
                                "session_id": r[1],
                                "source_path": r[2] or "",
                                "target_path": r[3] or "",
                                "original_path": r[4] or "",
                                "original_filename": r[5] or "",
                                "is_cross_volume": bool(r[6]),
                                "is_collision_renamed": bool(r[7]),
                                "file_hash": r[8] or "",
                                "timestamp": r[9] or 0.0,
                                "timestamp_iso": ts_iso,
                                "status": r[10] or "completed",
                            }
                        )
            except Exception:
                pass

        sessions = sorted(
            sessions_map.values(), key=lambda s: s.get("timestamp") or 0.0, reverse=True
        )

        steps.sort(key=lambda s: (s.get("timestamp") or 0.0, s.get("step_id") or 0))

        if not steps:
            try:
                from app.core.ledger import TransactionLedger

                tx_ledger = TransactionLedger()
                pending = tx_ledger.get_pending_entries(session_id=session_id)
                for idx, entry in enumerate(pending, 1):
                    ts = entry.get("updated_at") or entry.get("created_at") or 0.0
                    ts_iso = ""
                    if ts:
                        try:
                            ts_iso = datetime.fromtimestamp(
                                ts, tz=timezone.utc
                            ).isoformat()
                        except Exception:
                            pass
                    steps.append(
                        {
                            "step_id": idx,
                            "session_id": entry.get("session_id")
                            or session_id
                            or "unknown",
                            "source_path": entry.get("source_path") or "",
                            "target_path": entry.get("dest_path") or "",
                            "original_path": entry.get("source_path") or "",
                            "original_filename": os.path.basename(
                                entry.get("source_path") or ""
                            ),
                            "is_cross_volume": False,
                            "is_collision_renamed": False,
                            "file_hash": entry.get("file_hash") or "",
                            "timestamp": ts,
                            "timestamp_iso": ts_iso,
                            "status": entry.get("status") or "pending",
                        }
                    )
            except Exception:
                pass

        if session_id and not sessions and not steps:
            raise ValueError(f"Session '{session_id}' not found in history database.")

        fmt = (format or "").lower().strip()
        if not fmt:
            if output_path.lower().endswith(".csv"):
                fmt = "csv"
            else:
                fmt = "json"

        if fmt not in ("csv", "json"):
            raise ValueError(
                f"Unsupported export format '{fmt}'. Supported formats are 'csv' and 'json'."
            )

        out_dir = os.path.dirname(os.path.abspath(output_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)

        if fmt == "csv":
            fieldnames = [
                "session_id",
                "step_id",
                "timestamp",
                "timestamp_iso",
                "source_path",
                "target_path",
                "original_path",
                "original_filename",
                "file_hash",
                "status",
            ]
            with open(output_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                for step in steps:
                    writer.writerow(step)
        else:
            export_payload = {
                "export_timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": session_id,
                "sessions": sessions,
                "total_steps": len(steps),
                "steps": steps,
            }
            with open(output_path, "w", encoding="utf-8") as f:
                json.dump(export_payload, f, indent=2)

        return {
            "status": "success",
            "session_id": session_id,
            "output_path": output_path,
            "format": fmt,
            "count": len(steps),
        }

    def unwind_session(self, session_id: str, db=None) -> int:
        """Unwind completed relocation steps in exact reverse chronological order using step ledger."""
        return self._unwind_session_internal(session_id, db=db)

    def _unwind_session_internal(self, session_id: str, db=None) -> int:
        if db:
            db.invalidate_cache()
        steps = self.get_step_ledger(session_id)

        conn = get_db_connection(self.db_path)
        snapshot_files = set()
        base_dir = None
        with conn:
            cur = conn.execute(
                "SELECT base_dir FROM sessions WHERE session_id = ?", (session_id,)
            )
            row = cur.fetchone()
            base_dir = row[0] if row else None

            cur = conn.execute(
                "SELECT original_rel_path FROM snapshot_files WHERE session_id = ?",
                (session_id,),
            )
            snapshot_files = {r[0] for r in cur.fetchall()}

        unwound_count = 0
        from app.core.resilient_file_ops import resilient_move

        target_dirs_to_clean = set()

        failed_unwinds = []
        if not steps:
            return self._rollback_from_snapshot_internal(
                session_id, base_dir=base_dir, ignore_missing=True
            )

        if steps:
            # Exact reverse chronological order
            steps_reverse = sorted(steps, key=lambda s: s["step_id"], reverse=True)

            for step in steps_reverse:
                target_path = step["target_path"]
                source_path = step["source_path"]

                if os.path.lexists(target_path):
                    target_dirs_to_clean.add(os.path.dirname(target_path))
                    os.makedirs(os.path.dirname(source_path), exist_ok=True)

                    try:
                        from app.core.resilient_file_ops import same_file_content

                        if os.path.lexists(source_path) and same_file_content(
                            source_path, target_path
                        ):
                            # An identical copy is already back in place.
                            from app.core.resilient_file_ops import resilient_remove

                            resilient_remove(target_path)
                        else:
                            # resilient_move never replaces an existing file: if
                            # something else now occupies source_path, the moved
                            # file is restored next to it under a suffixed name.
                            restored_path = resilient_move(target_path, source_path)
                            if not _is_same_restored_path(restored_path, source_path):
                                logging.warning(
                                    f"Undo conflict: {source_path} is occupied by a different file; "
                                    f"restored {target_path} to {restored_path} instead."
                                )
                                source_path = restored_path
                        unwound_count += 1

                        if db and base_dir:
                            try:
                                rel_target = safe_relpath(
                                    target_path, base_dir
                                ).replace("\\", "/")
                                rel_source = safe_relpath(
                                    source_path, base_dir
                                ).replace("\\", "/")
                                db.update_document_path(
                                    base_dir, rel_target, rel_source
                                )
                            except Exception:
                                pass
                    except Exception as move_err:
                        logging.error(
                            f"Failed to unwind step {step['step_id']}: {move_err}"
                        )
                        failed_unwinds.append((step, move_err))
        elif snapshot_files:
            return self._rollback_from_snapshot_internal(session_id, base_dir=base_dir)

        if failed_unwinds:
            first_err = failed_unwinds[0][1]
            raise PermissionError(
                f"Recovery incomplete: {len(failed_unwinds)} step(s) failed to unwind. Error: {first_err}"
            )

        if base_dir and os.path.exists(base_dir):
            _remove_staging_leftovers(base_dir)

        if base_dir:
            from app.core.mover import _prune_emptied_dirs

            _prune_emptied_dirs(target_dirs_to_clean, base_dir)

        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "UPDATE sessions SET status = 'rolled_back' WHERE session_id = ?",
                (session_id,),
            )
            conn.execute("DELETE FROM step_ledger WHERE session_id = ?", (session_id,))

        if db:
            db.invalidate_cache()

        return unwound_count

    def _rollback_from_snapshot_internal(
        self,
        session_id: str,
        base_dir: str = None,
        safety_session_id: str = None,
        ignore_missing: bool = False,
    ):
        conn = get_db_connection(self.db_path)
        if not base_dir:
            with conn:
                cur = conn.execute(
                    "SELECT base_dir FROM sessions WHERE session_id = ?", (session_id,)
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError("Session not found")
                base_dir = row[0]

        if not safety_session_id:
            safety_session_id = self._create_snapshot_internal(base_dir)
            self._mark_undo_snapshot(safety_session_id)

        with conn:
            try:
                cur = conn.execute(
                    """
                    SELECT original_rel_path, inode, size, mtime, is_symlink, symlink_target, file_hash,
                           link_type, arguments, description, icon_file, icon_index, work_dir, window_mode
                    FROM snapshot_files WHERE session_id = ?
                    """,
                    (session_id,),
                )
                snapshot_files = cur.fetchall()
            except Exception:
                try:
                    cur = conn.execute(
                        "SELECT original_rel_path, inode, size, mtime, is_symlink, symlink_target, file_hash FROM snapshot_files WHERE session_id = ?",
                        (session_id,),
                    )
                    snapshot_files = [
                        (*row, None, None, None, None, 0, None, None)
                        for row in cur.fetchall()
                    ]
                except Exception:
                    cur = conn.execute(
                        "SELECT original_rel_path, inode, size, mtime, is_symlink, symlink_target FROM snapshot_files WHERE session_id = ?",
                        (session_id,),
                    )
                    snapshot_files = [
                        (*row, None, None, None, None, None, 0, None, None)
                        for row in cur.fetchall()
                    ]

            from app.core.scanner import get_files_recursively

            current_files = get_files_recursively(base_dir, include_hidden=True)

            with conn:
                inode_counts: dict[Any, Any] = {}
                current_inodes: dict[Any, Any] = {}
                active_files_by_rel_path: dict[Any, Any] = {}
                active_files_by_sig: dict[Any, Any] = {}
                active_files_by_size: dict[Any, Any] = {}
                inodes_reliable = True

                for rel_path in current_files:
                    abs_path = os.path.join(base_dir, rel_path)
                    try:
                        st = os.lstat(abs_path)
                    except OSError:
                        continue

                    ino = st.st_ino
                    size = st.st_size
                    mtime = st.st_mtime
                    is_symlink = 1 if os.path.islink(abs_path) else 0
                    symlink_target = os.readlink(abs_path) if is_symlink else None

                    inode_counts[ino] = inode_counts.get(ino, 0) + 1
                    if ino == 0 or inode_counts[ino] > 1:
                        inodes_reliable = False

                    sig = (size, mtime, is_symlink, symlink_target)
                    current_inodes[ino] = (abs_path, sig)
                    active_files_by_rel_path[rel_path.lower().replace("\\", "/")] = sig

                    if sig not in active_files_by_sig:
                        active_files_by_sig[sig] = []
                    active_files_by_sig[sig].append(abs_path)

                    if size not in active_files_by_size:
                        active_files_by_size[size] = []
                    active_files_by_size[size].append(abs_path)

                # First compute all intended moves
                moves = []

                def verify_hash(abs_path, expected_hash, expected_size=0):
                    if not expected_hash:
                        return True
                    EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
                    import sys

                    # On Windows, we allow up to 30 attempts (with 0.1s sleep, up to 3 seconds total)
                    # to let NTFS flushes, sharing violations, and anti-virus locks settle.
                    max_attempts = 30 if sys.platform == "win32" else 10
                    sleep_time = 0.1 if sys.platform == "win32" else 0.05

                    for attempt in range(max_attempts):
                        try:
                            from app.core.extractor import get_file_hash

                            h = get_file_hash(abs_path)
                            if h == expected_hash:
                                return True

                            # On Windows, always retry if the hash doesn't match yet
                            if sys.platform == "win32":
                                import gc

                                gc.collect()
                                time.sleep(sleep_time)
                                continue

                            if (
                                h == EMPTY_SHA256
                                and expected_hash != EMPTY_SHA256
                                and expected_size > 0
                            ):
                                import gc

                                gc.collect()
                                time.sleep(sleep_time)
                                continue
                            return False
                        except Exception:
                            import gc

                            gc.collect()
                            time.sleep(sleep_time)
                    return False

                symlinks_to_restore = []
                shortcuts_to_restore = []
                for (
                    rel_path,
                    inode,
                    size,
                    mtime,
                    is_symlink,
                    symlink_target,
                    file_hash,
                    link_type,
                    arguments,
                    description,
                    icon_file,
                    icon_index,
                    work_dir,
                    window_mode,
                ) in snapshot_files:
                    target_abs = os.path.join(base_dir, rel_path)
                    current_abs = None
                    target_sig = (size, mtime, is_symlink, symlink_target)

                    is_lnk = (
                        1
                        if (link_type == "lnk" or rel_path.lower().endswith(".lnk"))
                        else 0
                    )
                    is_link_entity = bool(is_symlink or is_lnk)

                    if inodes_reliable and inode in current_inodes:
                        abs_path, current_sig = current_inodes[inode]
                        if is_link_entity:
                            current_abs = abs_path
                            del current_inodes[inode]
                        else:
                            if current_sig[2] == is_symlink:
                                if not is_symlink or current_sig[3] == symlink_target:
                                    if verify_hash(abs_path, file_hash, size):
                                        current_abs = abs_path
                                        del current_inodes[inode]

                    if not current_abs:
                        curr_sig = active_files_by_rel_path.get(
                            rel_path.lower().replace("\\", "/")
                        )
                        if is_link_entity:
                            if curr_sig is not None:
                                current_abs = target_abs
                        else:
                            if curr_sig == target_sig:
                                candidate_abs = target_abs
                                if verify_hash(candidate_abs, file_hash, size):
                                    current_abs = candidate_abs
                                    if (
                                        curr_sig in active_files_by_sig
                                        and current_abs in active_files_by_sig[curr_sig]
                                    ):
                                        active_files_by_sig[curr_sig].remove(
                                            current_abs
                                        )
                        if not current_abs:
                            if not is_link_entity:
                                if (
                                    target_sig in active_files_by_sig
                                    and active_files_by_sig[target_sig]
                                ):
                                    for idx, cand_path in enumerate(
                                        active_files_by_sig[target_sig]
                                    ):
                                        if verify_hash(cand_path, file_hash, size):
                                            current_abs = active_files_by_sig[
                                                target_sig
                                            ].pop(idx)
                                            break
                                if not current_abs:
                                    if (
                                        size in active_files_by_size
                                        and active_files_by_size[size]
                                    ):
                                        for idx, cand_path in enumerate(
                                            active_files_by_size[size]
                                        ):
                                            if verify_hash(cand_path, file_hash, size):
                                                current_abs = active_files_by_size[
                                                    size
                                                ].pop(idx)
                                                break

                    if not current_abs:
                        if not is_link_entity and not ignore_missing:
                            raise ValueError(
                                f"Rollback validation failed: file hash mismatch or missing for {rel_path}"
                            )

                    if is_link_entity:
                        if current_abs and current_abs != target_abs:
                            try:
                                from app.core.resilient_file_ops import resilient_remove

                                resilient_remove(current_abs)
                            except OSError:
                                pass
                        if is_symlink or link_type == "junction":
                            symlinks_to_restore.append(
                                (target_abs, symlink_target, link_type)
                            )
                        elif is_lnk:
                            shortcuts_to_restore.append(
                                (
                                    target_abs,
                                    symlink_target,
                                    arguments,
                                    description,
                                    icon_file,
                                    icon_index,
                                    work_dir,
                                    window_mode,
                                )
                            )
                    else:
                        if current_abs:
                            same_file = False
                            if os.path.exists(current_abs) and os.path.exists(
                                target_abs
                            ):
                                try:
                                    same_file = os.path.samefile(
                                        current_abs, target_abs
                                    )
                                except OSError:
                                    pass
                            if not same_file:
                                if current_abs != target_abs:
                                    moves.append((current_abs, target_abs))

                planned_target_rels = {
                    safe_relpath(m[1], base_dir).lower().replace("\\", "/")
                    for m in moves
                }

                try:
                    cur = conn.execute(
                        "SELECT filepath, file_hash, extracted_text, user_verified_target_path FROM snapshot_documents WHERE session_id = ?",
                        (session_id,),
                    )
                    snapshot_docs = cur.fetchall()
                    snapshot_docs_dict = {
                        r[0].replace("\\", "/"): (
                            r[0].replace("\\", "/"),
                            r[1],
                            r[2],
                            r[3] if len(r) > 3 else None,
                        )
                        for r in snapshot_docs
                    }
                except Exception:
                    cur = conn.execute(
                        "SELECT filepath, file_hash, extracted_text FROM snapshot_documents WHERE session_id = ?",
                        (session_id,),
                    )
                    snapshot_docs = cur.fetchall()
                    snapshot_docs_dict = {
                        r[0].replace("\\", "/"): (
                            r[0].replace("\\", "/"),
                            r[1],
                            r[2],
                            None,
                        )
                        for r in snapshot_docs
                    }

                # Write rollback journal before any modifications or relocations
                try:
                    import json
                    from pathlib import Path

                    journal_path = Path(self.db_path).parent / "rollback_journal.json"
                    journal_data = {
                        "session_id": session_id,
                        "safety_session_id": safety_session_id,
                        "base_dir": base_dir,
                        "moves": moves,
                        "symlinks": symlinks_to_restore,
                        "shortcuts": shortcuts_to_restore,
                    }
                    with open(journal_path, "w") as f:
                        json.dump(journal_data, f, indent=2)
                except Exception as ex:
                    import logging

                    logging.warning(f"Failed to write rollback journal: {ex}")

                # 1. Pre-Move Synchronization
                db_conn = get_db_connection(self.db.db_path)
                with db_conn:
                    docs_to_upsert = []
                    for fp, r in snapshot_docs_dict.items():
                        if fp.lower().replace("\\", "/") not in planned_target_rels:
                            verified_target = r[3] if len(r) > 3 else None
                            docs_to_upsert.append(
                                (base_dir, r[0], r[1], r[2], verified_target)
                            )
                    if docs_to_upsert:
                        db_conn.executemany(
                            """
                            INSERT INTO documents (base_dir, filepath, file_hash, extracted_text, user_verified_target_path)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(base_dir, filepath) DO UPDATE SET
                             file_hash=excluded.file_hash,
                             extracted_text=excluded.extracted_text,
                             user_verified_target_path=excluded.user_verified_target_path
                            """,
                            docs_to_upsert,
                        )

                # Execute moves safely to avoid overwriting during cyclic renames
                try:
                    failed_moves = []
                    for src, dst in moves:
                        try:
                            from app.core.mover import get_safe_path

                            db_conn = get_db_connection(self.db.db_path)

                            # Fix parent directory collisions
                            parts = (
                                safe_relpath(dst, base_dir)
                                .replace("\\", "/")
                                .split("/")
                            )
                            current = base_dir
                            for part in parts[:-1]:
                                current = os.path.join(current, part)
                                if os.path.exists(current) and not os.path.isdir(
                                    current
                                ):
                                    safe_current = get_safe_path(
                                        os.path.dirname(current),
                                        os.path.basename(current),
                                    )
                                    _robust_move(current, safe_current)
                                    rel_current = safe_relpath(
                                        current, base_dir
                                    ).replace("\\", "/")
                                    rel_safe = safe_relpath(
                                        safe_current, base_dir
                                    ).replace("\\", "/")
                                    with db_conn:
                                        db_conn.execute(
                                            "UPDATE documents SET filepath = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                                            (
                                                rel_safe,
                                                base_dir,
                                                rel_current,
                                                rel_current,
                                            ),
                                        )
                                        db_conn.execute(
                                            "UPDATE documents SET filepath = ? || SUBSTR(filepath, ?) WHERE base_dir = ? AND (filepath LIKE ? OR REPLACE(filepath, '\\', '/') LIKE ?)",
                                            (
                                                rel_safe,
                                                len(rel_current) + 1,
                                                base_dir,
                                                rel_current + "/%",
                                                rel_current + "/%",
                                            ),
                                        )

                            os.makedirs(os.path.dirname(dst), exist_ok=True)
                            if os.path.islink(dst):
                                try:
                                    from app.core.resilient_file_ops import (
                                        resilient_remove,
                                    )

                                    resilient_remove(dst)
                                except OSError:
                                    pass

                            rel_src = safe_relpath(src, base_dir).replace("\\", "/")
                            rel_dst = safe_relpath(dst, base_dir).replace("\\", "/")

                            if os.path.exists(dst) and not os.path.samefile(src, dst):
                                is_cyclic = False
                                for m_src, m_dst in moves:
                                    if _is_same_restored_path(m_src, dst):
                                        is_cyclic = True
                                        break

                                if is_cyclic:
                                    # Cyclic rename conflict -> isolate to safe transient path
                                    branch_rel_temp = os.path.join(
                                        ".branches", safety_session_id, rel_dst
                                    ).replace("\\", "/")
                                    temp_dst = os.path.join(base_dir, branch_rel_temp)
                                    os.makedirs(
                                        os.path.dirname(temp_dst), exist_ok=True
                                    )
                                    _robust_move(dst, temp_dst)

                                    with db_conn:
                                        db_conn.execute(
                                            "UPDATE documents SET filepath = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                                            (
                                                branch_rel_temp,
                                                base_dir,
                                                rel_dst,
                                                rel_dst,
                                            ),
                                        )

                                    for i, (m_src, m_dst) in enumerate(moves):
                                        if _is_same_restored_path(m_src, dst):
                                            moves[i] = (temp_dst, m_dst)
                                else:
                                    # Non-cyclic inline rename (Requirement 2)
                                    safe_dst = get_safe_path(
                                        os.path.dirname(dst), os.path.basename(dst)
                                    )
                                    _robust_move(dst, safe_dst)

                                    safe_rel = safe_relpath(safe_dst, base_dir).replace(
                                        "\\", "/"
                                    )
                                    with db_conn:
                                        db_conn.execute(
                                            "UPDATE documents SET filepath = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                                            (safe_rel, base_dir, rel_dst, rel_dst),
                                        )
                                        db_conn.execute(
                                            "UPDATE documents SET filepath = ? || SUBSTR(filepath, ?) WHERE base_dir = ? AND (filepath LIKE ? OR REPLACE(filepath, '\\', '/') LIKE ?)",
                                            (
                                                safe_rel,
                                                len(rel_dst) + 1,
                                                base_dir,
                                                rel_dst + "/%",
                                                rel_dst + "/%",
                                            ),
                                        )

                                _robust_move(src, dst)
                            else:
                                if not os.path.exists(dst):
                                    _robust_move(src, dst)

                            with db_conn:
                                db_conn.execute(
                                    "DELETE FROM documents WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                                    (base_dir, rel_src, rel_src),
                                )
                                rel_dst_norm = rel_dst.replace("\\", "/")
                                snapshot_doc = (
                                    snapshot_docs_dict.get(rel_dst_norm)
                                    or snapshot_docs_dict.get(rel_dst)
                                    or snapshot_docs_dict.get(
                                        rel_dst.replace("/", "\\")
                                    )
                                )
                                if snapshot_doc:
                                    verified_target = (
                                        snapshot_doc[3]
                                        if len(snapshot_doc) > 3
                                        else None
                                    )
                                    db_conn.execute(
                                        """
                                        INSERT INTO documents (base_dir, filepath, file_hash, extracted_text, user_verified_target_path)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(base_dir, filepath) DO UPDATE SET
                                file_hash=excluded.file_hash,
                                extracted_text=excluded.extracted_text,
                                user_verified_target_path=excluded.user_verified_target_path
                                        """,
                                        (
                                            base_dir,
                                            rel_dst,
                                            snapshot_doc[1],
                                            snapshot_doc[2],
                                            verified_target,
                                        ),
                                    )
                        except Exception as move_err:
                            import logging

                            logging.error(
                                f"Failed to move {src} to {dst} during recovery: {move_err}"
                            )
                            failed_moves.append((src, dst, move_err))

                    if failed_moves:
                        first_err = failed_moves[0][2]
                        raise PermissionError(
                            f"Recovery incomplete: {len(failed_moves)} file(s) failed to restore. Safety storage preserved. Error: {first_err}"
                        )

                    # Restore symlinks and junctions after standard files
                    for target_abs, symlink_target, l_type in symlinks_to_restore:
                        if (
                            os.path.exists(target_abs)
                            or os.path.islink(target_abs)
                            or is_junction_path(target_abs)
                        ):
                            if os.path.islink(target_abs) or is_junction_path(
                                target_abs
                            ):
                                try:
                                    from app.core.resilient_file_ops import (
                                        resilient_remove,
                                    )

                                    resilient_remove(target_abs)
                                except OSError:
                                    pass
                            else:
                                from app.core.mover import get_safe_path

                                safe_path = get_safe_path(
                                    os.path.dirname(target_abs),
                                    os.path.basename(target_abs),
                                )
                                _robust_move(target_abs, safe_path)

                                rel_target = safe_relpath(target_abs, base_dir).replace(
                                    "\\", "/"
                                )
                                rel_safe = safe_relpath(safe_path, base_dir).replace(
                                    "\\", "/"
                                )
                                db_conn = get_db_connection(self.db.db_path)
                                with db_conn:
                                    db_conn.execute(
                                        "UPDATE documents SET filepath = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                                        (rel_safe, base_dir, rel_target, rel_target),
                                    )
                                    db_conn.execute(
                                        "UPDATE documents SET filepath = ? || SUBSTR(filepath, ?) WHERE base_dir = ? AND (filepath LIKE ? OR REPLACE(filepath, '\\', '/') LIKE ?)",
                                        (
                                            rel_safe,
                                            len(rel_target) + 1,
                                            base_dir,
                                            rel_target + "/%",
                                            rel_target + "/%",
                                        ),
                                    )
                        try:
                            os.makedirs(os.path.dirname(target_abs), exist_ok=True)
                            if l_type == "junction":
                                from app.core.mover import _create_junction

                                _create_junction(symlink_target, target_abs)
                            else:
                                os.symlink(symlink_target, target_abs)
                        except OSError as e:
                            import logging

                            logging.warning(
                                f"Failed to recreate link at {target_abs}: {e}"
                            )

                    # Restore Windows shortcuts after standard files and symlinks
                    for (
                        target_abs,
                        lnk_target,
                        arguments,
                        description,
                        icon_file,
                        icon_index,
                        work_dir,
                        window_mode,
                    ) in shortcuts_to_restore:
                        if os.path.exists(target_abs) or os.path.islink(target_abs):
                            if os.path.islink(
                                target_abs
                            ) or target_abs.lower().endswith(".lnk"):
                                try:
                                    from app.core.resilient_file_ops import (
                                        resilient_remove,
                                    )

                                    resilient_remove(target_abs)
                                except OSError:
                                    pass
                            else:
                                from app.core.mover import get_safe_path

                                safe_path = get_safe_path(
                                    os.path.dirname(target_abs),
                                    os.path.basename(target_abs),
                                )
                                _robust_move(target_abs, safe_path)

                                rel_target = safe_relpath(target_abs, base_dir).replace(
                                    "\\", "/"
                                )
                                rel_safe = safe_relpath(safe_path, base_dir).replace(
                                    "\\", "/"
                                )
                                db_conn = get_db_connection(self.db.db_path)
                                with db_conn:
                                    db_conn.execute(
                                        "UPDATE documents SET filepath = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                                        (rel_safe, base_dir, rel_target, rel_target),
                                    )
                                    db_conn.execute(
                                        "UPDATE documents SET filepath = ? || SUBSTR(filepath, ?) WHERE base_dir = ? AND (filepath LIKE ? OR REPLACE(filepath, '\\', '/') LIKE ?)",
                                        (
                                            rel_safe,
                                            len(rel_target) + 1,
                                            base_dir,
                                            rel_target + "/%",
                                            rel_target + "/%",
                                        ),
                                    )
                        try:
                            if pylnk3 and lnk_target:
                                os.makedirs(os.path.dirname(target_abs), exist_ok=True)
                                kwargs = {
                                    "arguments": arguments,
                                    "description": description,
                                    "icon_file": icon_file,
                                    "icon_index": icon_index or 0,
                                    "work_dir": work_dir,
                                    "window_mode": window_mode,
                                }
                                pylnk3.for_file(
                                    lnk_target, lnk_name=target_abs, **kwargs
                                )
                            else:
                                os.makedirs(os.path.dirname(target_abs), exist_ok=True)
                                with open(target_abs, "w") as f:
                                    f.write("")
                        except Exception as e:
                            import logging

                            logging.warning(
                                f"Failed to recreate Windows shortcut at {target_abs}: {e}"
                            )

                except Exception as e:
                    conn.execute(
                        "UPDATE sessions SET status = 'failed' WHERE session_id = ?",
                        (session_id,),
                    )
                    conn.commit()
                    raise e

                # Only remove staging files this tool created; never files that
                # merely weren't in the snapshot (hidden files, new downloads).
                _remove_staging_leftovers(base_dir)

                # Clean empty directories
                from app.config import AppSettings
                from app.core.mover import _remove_empty_dirs

                try:
                    app_settings = AppSettings()
                    protected_paths = getattr(app_settings, "PROTECTED_PATHS", [])
                    protected_paths = [os.path.normpath(p) for p in protected_paths]
                except Exception:
                    protected_paths = None

                for entry in os.listdir(base_dir):
                    entry_path = os.path.join(base_dir, entry)
                    if os.path.isdir(entry_path):
                        try:
                            _remove_empty_dirs(entry_path, protected_paths)
                        except Exception:
                            pass

                # Restore Cache
                cur = conn.execute(
                    "SELECT corpus, locked_files, index_to_word, manual_folders FROM snapshot_cache WHERE session_id = ?",
                    (session_id,),
                )
                row = cur.fetchone()
                cache_conn = self.cache_manager._get_conn()
                with cache_conn:
                    if row:
                        cache_conn.execute(
                            """
                            INSERT INTO directory_cache (source_directory, corpus, locked_files, index_to_word, manual_folders)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(source_directory) DO UPDATE SET
                                corpus=excluded.corpus,
                                locked_files=excluded.locked_files,
                                index_to_word=excluded.index_to_word,
                                manual_folders=excluded.manual_folders
                            """,
                            (base_dir, row[0], row[1], row[2], row[3]),
                        )
                    else:
                        cache_conn.execute(
                            "DELETE FROM directory_cache WHERE source_directory = ?",
                            (base_dir,),
                        )

                conn.execute(
                    "UPDATE sessions SET status = 'rolled_back' WHERE session_id = ?",
                    (session_id,),
                )
                self.db.invalidate_cache(base_dir)
                try:
                    from app.core.shared_registry import SharedModelRegistry

                    jev_engine = SharedModelRegistry.get_instance().get_jev_classifier()
                    if jev_engine and hasattr(jev_engine, "clear_cache"):
                        jev_engine.clear_cache()
                except Exception:
                    pass

                # Clean delete of rollback journal file
                try:
                    from pathlib import Path

                    journal_path = Path(self.db_path).parent / "rollback_journal.json"
                    if journal_path.exists():
                        journal_path.unlink()
                except Exception as ex:
                    import logging

                    logging.warning(f"Failed to delete rollback journal: {ex}")

        return len(moves)

    def rollback(self, session_id: str, ignore_missing: bool = False):
        """Revert directory and metadata state using recorded transaction ledger steps or snapshot."""
        self.db.invalidate_cache()

        def _write():
            conn = get_db_connection(self.db_path)
            step_count = 0
            base_dir = None
            with conn:
                try:
                    cur = conn.execute(
                        "SELECT COUNT(*) FROM step_ledger WHERE session_id = ?",
                        (session_id,),
                    )
                    step_count = cur.fetchone()[0]
                except Exception:
                    step_count = 0

                cur = conn.execute(
                    "SELECT base_dir FROM sessions WHERE session_id = ?", (session_id,)
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError("Session not found")
                base_dir = row[0]

            # Pre-rollback safety snapshot created BEFORE any file system write operation
            safety_session_id = None
            if os.path.exists(base_dir):
                safety_session_id = self._create_snapshot_internal(base_dir)
                self._mark_undo_snapshot(safety_session_id)

            if step_count > 0:
                result = self._unwind_session_internal(session_id, db=self.db)
            elif steps := self.db.get_transaction_steps(session_id):
                result = self._rollback_from_steps(
                    session_id,
                    steps,
                    base_dir,
                    safety_session_id,
                    ignore_missing=ignore_missing,
                )
            else:
                missing = self.check_missing_files(session_id)
                if missing and not ignore_missing:
                    raise ValueError(
                        f"Cannot rollback: {len(missing)} files from the snapshot are missing from the disk (e.g., {missing[0]})."
                    )
                result = self._rollback_from_snapshot_internal(
                    session_id,
                    base_dir=base_dir,
                    safety_session_id=safety_session_id,
                    ignore_missing=ignore_missing,
                )

            if safety_session_id:
                self._mark_undo_snapshot(safety_session_id)
            return result

        return self.db.worker.execute_write(_write)


@contextmanager
def open_session_history(
    history_db_path: "str | os.PathLike[str] | None" = None,
) -> Iterator[HistoryManager]:
    """Yield a HistoryManager bound to the databases of one sort session.

    A session keeps its ``autosorter.db`` and ``cache.db`` next to its
    ``history.db``, so a rollback must update those rather than the global
    databases in the app dir. Without a path, the app dir's ``history.db`` is used.
    The DBWorker opened here is stopped on exit.
    """
    from app.config import get_app_dir
    from app.core.cache import CacheManager
    from app.core.db import Database
    from app.core.db_worker import DBWorker

    history_db = (
        Path(history_db_path) if history_db_path else get_app_dir() / "history.db"
    )
    session_dir = history_db.parent
    db_worker = DBWorker()
    try:
        db = Database(session_dir / "autosorter.db", db_worker)
        cache_mgr = CacheManager(str(session_dir / "cache.db"), db_worker)
        yield HistoryManager(db, cache_mgr, str(history_db))
    finally:
        db_worker.stop(stop_embeddings=False)
