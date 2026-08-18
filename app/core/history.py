"""History management module for snapshotting and rollback."""

import os
import time
import uuid
from typing import Any, Dict, List

from app.core.db_conn import get_db_connection
from app.core.path_utils import is_junction_path

try:
    import pylnk3
except ImportError:
    pylnk3 = None


def _robust_move(src, dst):
    """Safely move a file, retrying on temporary Windows sharing violations and file locks."""
    from app.core.resilient_file_ops import resilient_move

    resilient_move(src, dst)


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
            conn.execute("""
                CREATE TABLE IF NOT EXISTS snapshot_documents (
                    session_id TEXT,
                    filepath TEXT,
                    file_hash TEXT,
                    extracted_text TEXT,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)

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
                cur = db_conn.execute(
                    "SELECT filepath, file_hash, extracted_text FROM documents WHERE base_dir = ?",
                    (base_dir,),
                )
                for r in cur.fetchall():
                    docs.append((session_id, r[0], r[1], r[2]))
            if docs:
                conn.executemany(
                    "INSERT INTO snapshot_documents (session_id, filepath, file_hash, extracted_text) VALUES (?, ?, ?, ?)",
                    docs,
                )

            # Prune old snapshots to prevent excessive growth (keep last 10)
            self._prune_snapshots(conn, limit=10)

        return session_id

    def create_snapshot(self, base_dir: str) -> str:
        """Create a complete snapshot of the directory tree and its metadata."""

        def _write():
            return self._create_snapshot_internal(base_dir)

        return self.db.worker.execute_write(_write)

    def _prune_snapshots(self, conn, limit=10):
        cur = conn.execute(
            "SELECT session_id, base_dir FROM sessions ORDER BY timestamp DESC LIMIT -1 OFFSET ?",
            (limit,),
        )
        old_sessions = cur.fetchall()
        for sid, base_dir in old_sessions:
            # Never prune divergent history branches that contain unmerged user data
            branch_dir = os.path.join(base_dir, ".branches", sid)
            if os.path.exists(branch_dir) and os.listdir(branch_dir):
                continue

            conn.execute("DELETE FROM snapshot_files WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM snapshot_cache WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM snapshot_documents WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))

    def get_sessions(self) -> List[Dict[str, Any]]:
        """Retrieve a list of all historical sessions, ordered by time."""
        conn = get_db_connection(self.db_path)
        with conn:
            cur = conn.execute(
                "SELECT session_id, timestamp, base_dir, status FROM sessions ORDER BY timestamp DESC"
            )
            return [
                {
                    "session_id": r[0],
                    "timestamp": r[1],
                    "base_dir": r[2],
                    "status": r[3],
                }
                for r in cur.fetchall()
            ]

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

        inode_counts = {}
        current_inodes = {}
        active_files_by_rel_path = {}
        active_files_by_sig = {}
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

    def rollback(self, session_id: str, ignore_missing: bool = False):
        """Revert directory and metadata state to the snapshot."""
        self.db.invalidate_cache()

        def _write():
            missing = self.check_missing_files(session_id)
            if missing and not ignore_missing:
                raise ValueError(
                    f"Cannot rollback: {len(missing)} files from the snapshot are missing from the disk (e.g., {missing[0]})."
                )

            conn = get_db_connection(self.db_path)
            with conn:
                cur = conn.execute(
                    "SELECT base_dir FROM sessions WHERE session_id = ?", (session_id,)
                )
                row = cur.fetchone()
                if not row:
                    raise ValueError("Session not found")
                base_dir = row[0]

            # Generate safety backup snapshot before proceeding
            safety_session_id = self._create_snapshot_internal(base_dir)

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

                inode_counts = {}
                current_inodes = {}
                active_files_by_rel_path = {}
                active_files_by_sig = {}
                active_files_by_size = {}
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

                print("DEBUG ROLLBACK: inodes_reliable =", inodes_reliable, flush=True)
                print("DEBUG ROLLBACK: current_files =", current_files, flush=True)
                print(
                    "DEBUG ROLLBACK: snapshot_files =",
                    [(r[0], r[1], r[2], r[3], r[6]) for r in snapshot_files],
                    flush=True,
                )
                print(
                    "DEBUG ROLLBACK: active_files_by_size =",
                    active_files_by_size,
                    flush=True,
                )
                print(
                    "DEBUG ROLLBACK: active_files_by_sig =",
                    active_files_by_sig,
                    flush=True,
                )

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

                    print(
                        f"DEBUG ROLLBACK FILE {rel_path}: current_abs resolved to = {current_abs}",
                        flush=True,
                    )
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
                            symlinks_to_restore.append((target_abs, symlink_target, link_type))
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
                    os.path.relpath(m[1], base_dir).lower().replace("\\", "/")
                    for m in moves
                }

                cur = conn.execute(
                    "SELECT filepath, file_hash, extracted_text FROM snapshot_documents WHERE session_id = ?",
                    (session_id,),
                )
                snapshot_docs = cur.fetchall()
                snapshot_docs_dict = {r[0]: r for r in snapshot_docs}

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
                            docs_to_upsert.append((base_dir, r[0], r[1], r[2]))
                    if docs_to_upsert:
                        db_conn.executemany(
                            """
                            INSERT INTO documents (base_dir, filepath, file_hash, extracted_text)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(base_dir, filepath) DO UPDATE SET
                             file_hash=excluded.file_hash,
                             extracted_text=excluded.extracted_text
                            """,
                            docs_to_upsert,
                        )

                # Execute moves safely to avoid overwriting during cyclic renames
                try:
                    for src, dst in moves:
                        from app.core.mover import get_safe_path

                        db_conn = get_db_connection(self.db.db_path)

                        # Fix parent directory collisions
                        parts = (
                            os.path.relpath(dst, base_dir).replace("\\", "/").split("/")
                        )
                        current = base_dir
                        for part in parts[:-1]:
                            current = os.path.join(current, part)
                            if os.path.exists(current) and not os.path.isdir(current):
                                safe_current = get_safe_path(
                                    os.path.dirname(current), os.path.basename(current)
                                )
                                _robust_move(current, safe_current)
                                rel_current = os.path.relpath(
                                    current, base_dir
                                ).replace("\\", "/")
                                rel_safe = os.path.relpath(
                                    safe_current, base_dir
                                ).replace("\\", "/")
                                with db_conn:
                                    db_conn.execute(
                                        "UPDATE documents SET filepath = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                                        (rel_safe, base_dir, rel_current, rel_current),
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
                                from app.core.resilient_file_ops import resilient_remove

                                resilient_remove(dst)
                            except OSError:
                                pass

                        rel_src = os.path.relpath(src, base_dir).replace("\\", "/")
                        rel_dst = os.path.relpath(dst, base_dir).replace("\\", "/")

                        if os.path.exists(dst) and not os.path.samefile(src, dst):
                            is_cyclic = False
                            for m_src, m_dst in moves:
                                if m_src == dst:
                                    is_cyclic = True
                                    break

                            if is_cyclic:
                                # Cyclic rename conflict -> isolate to safe transient path
                                branch_rel_temp = os.path.join(
                                    ".branches", safety_session_id, rel_dst
                                ).replace("\\", "/")
                                temp_dst = os.path.join(base_dir, branch_rel_temp)
                                os.makedirs(os.path.dirname(temp_dst), exist_ok=True)
                                _robust_move(dst, temp_dst)

                                with db_conn:
                                    db_conn.execute(
                                        "UPDATE documents SET filepath = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                                        (branch_rel_temp, base_dir, rel_dst, rel_dst),
                                    )

                                for i, (m_src, m_dst) in enumerate(moves):
                                    if m_src == dst:
                                        moves[i] = (temp_dst, m_dst)
                            else:
                                # Non-cyclic inline rename (Requirement 2)
                                safe_dst = get_safe_path(
                                    os.path.dirname(dst), os.path.basename(dst)
                                )
                                _robust_move(dst, safe_dst)

                                safe_rel = os.path.relpath(safe_dst, base_dir).replace(
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
                            snapshot_doc = snapshot_docs_dict.get(rel_dst)
                            if snapshot_doc:
                                db_conn.execute(
                                    """
                                    INSERT INTO documents (base_dir, filepath, file_hash, extracted_text)
                        VALUES (?, ?, ?, ?)
                        ON CONFLICT(base_dir, filepath) DO UPDATE SET
                            file_hash=excluded.file_hash,
                            extracted_text=excluded.extracted_text
                                    """,
                                    (
                                        base_dir,
                                        rel_dst,
                                        snapshot_doc[1],
                                        snapshot_doc[2],
                                    ),
                                )

                    # Restore symlinks and junctions after standard files
                    for target_abs, symlink_target, l_type in symlinks_to_restore:
                        if os.path.exists(target_abs) or os.path.islink(target_abs) or is_junction_path(target_abs):
                            if os.path.islink(target_abs) or is_junction_path(target_abs):
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

                                rel_target = os.path.relpath(
                                    target_abs, base_dir
                                ).replace("\\", "/")
                                rel_safe = os.path.relpath(safe_path, base_dir).replace(
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

                                rel_target = os.path.relpath(
                                    target_abs, base_dir
                                ).replace("\\", "/")
                                rel_safe = os.path.relpath(safe_path, base_dir).replace(
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

                # Clean up any leftover/orphaned files that were copied during the failed transfer
                # but are not in the snapshot files.
                from app.core.scanner import get_files_recursively

                try:
                    import gc
                    import time

                    gc.collect()
                    time.sleep(0.1)
                    current_files_after_restore = get_files_recursively(
                        base_dir, include_hidden=True
                    )
                    snapshot_rel_paths = {
                        r[0].lower().replace("\\", "/") for r in snapshot_files
                    }
                    for rel_path in current_files_after_restore:
                        norm_rel_path = rel_path.lower().replace("\\", "/")
                        if norm_rel_path not in snapshot_rel_paths:
                            abs_path = os.path.join(base_dir, rel_path)
                            if os.path.lexists(abs_path) and not os.path.isdir(
                                abs_path
                            ):
                                try:
                                    from app.core.resilient_file_ops import (
                                        resilient_remove,
                                    )

                                    resilient_remove(abs_path)
                                except OSError:
                                    pass
                except Exception:
                    pass

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
                self.db.invalidate_cache()

                # Clean delete of rollback journal file
                try:
                    from pathlib import Path

                    journal_path = Path(self.db_path).parent / "rollback_journal.json"
                    if journal_path.exists():
                        journal_path.unlink()
                except Exception as ex:
                    import logging

                    logging.warning(f"Failed to delete rollback journal: {ex}")

        return self.db.worker.execute_write(_write)

    def resume_rollback(self, session_id: str):
        """Resume and complete an interrupted rollback session."""
        self.rollback(session_id, ignore_missing=True)

    def revert_rollback(self, safety_session_id: str):
        """Revert an interrupted rollback session back to its original state using the safety snapshot."""
        self.rollback(safety_session_id, ignore_missing=True)
