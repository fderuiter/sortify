"""History management module for snapshotting and rollback."""

import os
import shutil
import time
import uuid
from typing import Any, Dict, List

from app.core.db_conn import get_db_connection


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
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)
            try:
                conn.execute("ALTER TABLE snapshot_files ADD COLUMN is_symlink INTEGER DEFAULT 0")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE snapshot_files ADD COLUMN symlink_target TEXT")
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
                    embedding BLOB,
                    FOREIGN KEY(session_id) REFERENCES sessions(session_id)
                )
            """)

    def _create_snapshot_internal(self, base_dir: str) -> str:
        session_id = str(uuid.uuid4())
        timestamp = time.time()

        from app.core.scanner import get_files_recursively
        files = get_files_recursively(base_dir)

        conn = get_db_connection(self.db_path)
        with conn:
            conn.execute(
                "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
                (session_id, timestamp, base_dir, "active")
            )
            
            # 1. Snapshot Files
            file_records = []
            for rel_path in files:
                abs_path = os.path.join(base_dir, rel_path)
                try:
                    st = os.lstat(abs_path)
                    is_symlink = 1 if os.path.islink(abs_path) else 0
                    symlink_target = os.readlink(abs_path) if is_symlink else None
                    file_records.append((session_id, rel_path, st.st_ino, st.st_size, st.st_mtime, is_symlink, symlink_target))
                except OSError:
                    continue
            if file_records:
                conn.executemany(
                    "INSERT INTO snapshot_files (session_id, original_rel_path, inode, size, mtime, is_symlink, symlink_target) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    file_records
                )

            # 2. Snapshot Cache
            cache_conn = self.cache_manager._get_conn()
            with cache_conn:
                cur = cache_conn.execute(
                    "SELECT corpus, locked_files, index_to_word, manual_folders FROM directory_cache WHERE source_directory = ?",
                    (base_dir,)
                )
                row = cur.fetchone()
            if row:
                conn.execute(
                    "INSERT INTO snapshot_cache (session_id, corpus, locked_files, index_to_word, manual_folders) VALUES (?, ?, ?, ?, ?)",
                    (session_id, row[0], row[1], row[2], row[3])
                )

            # 3. Snapshot DB
            docs = []
            db_conn = get_db_connection(self.db.db_path)
            with db_conn:
                cur = db_conn.execute(
                    "SELECT filepath, file_hash, extracted_text, embedding FROM documents WHERE base_dir = ?",
                    (base_dir,)
                )
                for r in cur.fetchall():
                    docs.append((session_id, r[0], r[1], r[2], r[3]))
            if docs:
                conn.executemany(
                    "INSERT INTO snapshot_documents (session_id, filepath, file_hash, extracted_text, embedding) VALUES (?, ?, ?, ?, ?)",
                    docs
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
        cur = conn.execute("SELECT session_id, base_dir FROM sessions ORDER BY timestamp DESC LIMIT -1 OFFSET ?", (limit,))
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
            cur = conn.execute("SELECT session_id, timestamp, base_dir, status FROM sessions ORDER BY timestamp DESC")
            return [{"session_id": r[0], "timestamp": r[1], "base_dir": r[2], "status": r[3]} for r in cur.fetchall()]

    def _build_current_file_state(self, base_dir: str):
        from app.core.scanner import get_files_recursively
        current_files = get_files_recursively(base_dir)
        
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
            active_files_by_rel_path[rel_path] = sig
            
            if sig not in active_files_by_sig:
                active_files_by_sig[sig] = []
            active_files_by_sig[sig].append(abs_path)
            
        return current_inodes, active_files_by_rel_path, active_files_by_sig, inodes_reliable

    def check_missing_files(self, session_id: str) -> List[str]:
        """Check if any files from the snapshot are missing from the disk."""
        conn = get_db_connection(self.db_path)
        with conn:
            cur = conn.execute("SELECT base_dir FROM sessions WHERE session_id = ?", (session_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("Session not found")
            base_dir = row[0]

            cur = conn.execute("SELECT original_rel_path, inode, size, mtime, is_symlink, symlink_target FROM snapshot_files WHERE session_id = ?", (session_id,))
            snapshot_files = cur.fetchall()

        from app.core.scanner import get_files_recursively
        current_inodes, active_files_by_rel_path, active_files_by_sig, inodes_reliable = self._build_current_file_state(base_dir)

        missing = []
        for rel_path, inode, size, mtime, is_symlink, symlink_target in snapshot_files:
            found = False
            target_sig = (size, mtime, is_symlink, symlink_target)
            
            if inodes_reliable and inode in current_inodes:
                abs_path, current_sig = current_inodes[inode]
                if current_sig[2] == is_symlink:
                    if not is_symlink or current_sig[3] == symlink_target:
                        del current_inodes[inode]
                        found = True
            
            if not found:
                # Fallback Step A
                curr_sig = active_files_by_rel_path.get(rel_path)
                if curr_sig == target_sig:
                    abs_path = os.path.join(base_dir, rel_path)
                    if curr_sig in active_files_by_sig and abs_path in active_files_by_sig[curr_sig]:
                        active_files_by_sig[curr_sig].remove(abs_path)
                    found = True
                else:
                    # Fallback Step B
                    if target_sig in active_files_by_sig and active_files_by_sig[target_sig]:
                        active_files_by_sig[target_sig].pop(0)
                        found = True
            
            if not found:
                missing.append(rel_path)

        return missing

    def rollback(self, session_id: str, ignore_missing: bool = False):
        """Revert directory and metadata state to the snapshot."""
        def _write():
            missing = self.check_missing_files(session_id)
            if missing and not ignore_missing:
                raise ValueError(f"Cannot rollback: {len(missing)} files from the snapshot are missing from the disk (e.g., {missing[0]}).")

            conn = get_db_connection(self.db_path)
            with conn:
                cur = conn.execute("SELECT base_dir FROM sessions WHERE session_id = ?", (session_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError("Session not found")
                base_dir = row[0]

            # Generate safety backup snapshot before proceeding
            safety_session_id = self._create_snapshot_internal(base_dir)

            with conn:
                cur = conn.execute("SELECT original_rel_path, inode, size, mtime, is_symlink, symlink_target FROM snapshot_files WHERE session_id = ?", (session_id,))
                snapshot_files = cur.fetchall()

                current_inodes, active_files_by_rel_path, active_files_by_sig, inodes_reliable = self._build_current_file_state(base_dir)

                # First compute all intended moves
                moves = []
                symlinks_to_restore = []
                for rel_path, inode, size, mtime, is_symlink, symlink_target in snapshot_files:
                    target_abs = os.path.join(base_dir, rel_path)
                    current_abs = None
                    target_sig = (size, mtime, is_symlink, symlink_target)
                
                    if inodes_reliable and inode in current_inodes:
                        abs_path, current_sig = current_inodes[inode]
                        if current_sig[2] == is_symlink:
                            if not is_symlink or current_sig[3] == symlink_target:
                                current_abs = abs_path
                                del current_inodes[inode]
                    
                    if not current_abs:
                        curr_sig = active_files_by_rel_path.get(rel_path)
                        if curr_sig == target_sig:
                            current_abs = target_abs
                            if curr_sig in active_files_by_sig and current_abs in active_files_by_sig[curr_sig]:
                                active_files_by_sig[curr_sig].remove(current_abs)
                        else:
                            if target_sig in active_files_by_sig and active_files_by_sig[target_sig]:
                                current_abs = active_files_by_sig[target_sig].pop(0)

                    if current_abs:
                        if is_symlink:
                            if current_abs != target_abs:
                                try:
                                    os.remove(current_abs)
                                except OSError:
                                    pass
                            symlinks_to_restore.append((target_abs, symlink_target))
                        else:
                            same_file = False
                            if os.path.exists(current_abs) and os.path.exists(target_abs):
                                try:
                                    same_file = os.path.samefile(current_abs, target_abs)
                                except OSError:
                                    pass
                            if not same_file:
                                if current_abs != target_abs:
                                    moves.append((current_abs, target_abs))
                    else:
                        if is_symlink:
                            symlinks_to_restore.append((target_abs, symlink_target))

                planned_target_rels = [os.path.relpath(m[1], base_dir) for m in moves]

                cur = conn.execute("SELECT filepath, file_hash, extracted_text, embedding FROM snapshot_documents WHERE session_id = ?", (session_id,))
                snapshot_docs = cur.fetchall()
                snapshot_docs_dict = {r[0]: r for r in snapshot_docs}

                # 1. Pre-Move Synchronization
                db_conn = get_db_connection(self.db.db_path)
                with db_conn:
                    docs_to_upsert = []
                    for fp, r in snapshot_docs_dict.items():
                        if fp not in planned_target_rels:
                            docs_to_upsert.append((base_dir, r[0], r[1], r[2], r[3]))
                    if docs_to_upsert:
                        db_conn.executemany(
                            """
                            INSERT INTO documents (base_dir, filepath, file_hash, extracted_text, embedding)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(base_dir, filepath) DO UPDATE SET
                                file_hash=excluded.file_hash,
                                extracted_text=excluded.extracted_text,
                                embedding=excluded.embedding
                            """,
                            docs_to_upsert
                        )

                # Execute moves safely to avoid overwriting during cyclic renames
                try:
                    for src, dst in moves:
                        from app.core.mover import get_safe_path
                        db_conn = get_db_connection(self.db.db_path)
                        
                        # Fix parent directory collisions
                        parts = os.path.relpath(dst, base_dir).split(os.sep)
                        current = base_dir
                        for part in parts[:-1]:
                            current = os.path.join(current, part)
                            if os.path.exists(current) and not os.path.isdir(current):
                                safe_current = get_safe_path(os.path.dirname(current), os.path.basename(current))
                                shutil.move(current, safe_current)
                                rel_current = os.path.relpath(current, base_dir)
                                rel_safe = os.path.relpath(safe_current, base_dir)
                                with db_conn:
                                    db_conn.execute("UPDATE documents SET filepath = ? WHERE base_dir = ? AND filepath = ?", (rel_safe, base_dir, rel_current))
                                    db_conn.execute("UPDATE documents SET filepath = ? || SUBSTR(filepath, ?) WHERE base_dir = ? AND filepath LIKE ?", (rel_safe, len(rel_current) + 1, base_dir, rel_current + os.sep + '%'))
                                    
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        if os.path.islink(dst):
                            try:
                                os.remove(dst)
                            except OSError:
                                pass
                        
                        rel_src = os.path.relpath(src, base_dir)
                        rel_dst = os.path.relpath(dst, base_dir)

                        if os.path.exists(dst) and not os.path.samefile(src, dst):
                            is_cyclic = False
                            for m_src, m_dst in moves:
                                if m_src == dst:
                                    is_cyclic = True
                                    break
                                    
                            if is_cyclic:
                                # Cyclic rename conflict -> isolate to safe transient path
                                branch_rel_temp = os.path.join(".branches", safety_session_id, rel_dst)
                                temp_dst = os.path.join(base_dir, branch_rel_temp)
                                os.makedirs(os.path.dirname(temp_dst), exist_ok=True)
                                shutil.move(dst, temp_dst)
                                
                                with db_conn:
                                    db_conn.execute("UPDATE documents SET filepath = ? WHERE base_dir = ? AND filepath = ?", (branch_rel_temp, base_dir, rel_dst))
    
                                for i, (m_src, m_dst) in enumerate(moves):
                                    if m_src == dst:
                                        moves[i] = (temp_dst, m_dst)
                            else:
                                # Non-cyclic inline rename (Requirement 2)
                                safe_dst = get_safe_path(os.path.dirname(dst), os.path.basename(dst))
                                shutil.move(dst, safe_dst)
                                
                                safe_rel = os.path.relpath(safe_dst, base_dir)
                                with db_conn:
                                    db_conn.execute("UPDATE documents SET filepath = ? WHERE base_dir = ? AND filepath = ?", (safe_rel, base_dir, rel_dst))
                                    db_conn.execute("UPDATE documents SET filepath = ? || SUBSTR(filepath, ?) WHERE base_dir = ? AND filepath LIKE ?", (safe_rel, len(rel_dst) + 1, base_dir, rel_dst + os.sep + '%'))
                                
                            shutil.move(src, dst)
                        else:
                            if not os.path.exists(dst):
                                shutil.move(src, dst)

                        with db_conn:
                            db_conn.execute("DELETE FROM documents WHERE base_dir = ? AND filepath = ?", (base_dir, rel_src))
                            snapshot_doc = snapshot_docs_dict.get(rel_dst)
                            if snapshot_doc:
                                db_conn.execute(
                                    """
                                    INSERT INTO documents (base_dir, filepath, file_hash, extracted_text, embedding)
                                    VALUES (?, ?, ?, ?, ?)
                                    ON CONFLICT(base_dir, filepath) DO UPDATE SET
                                        file_hash=excluded.file_hash,
                                        extracted_text=excluded.extracted_text,
                                        embedding=excluded.embedding
                                    """,
                                    (base_dir, rel_dst, snapshot_doc[1], snapshot_doc[2], snapshot_doc[3])
                                )

                    # Restore symlinks after standard files
                    for target_abs, symlink_target in symlinks_to_restore:
                        if os.path.exists(target_abs) or os.path.islink(target_abs):
                            if os.path.islink(target_abs):
                                try:
                                    os.remove(target_abs)
                                except OSError:
                                    pass
                            else:
                                from app.core.mover import get_safe_path
                                safe_path = get_safe_path(os.path.dirname(target_abs), os.path.basename(target_abs))
                                shutil.move(target_abs, safe_path)
                                
                                rel_target = os.path.relpath(target_abs, base_dir)
                                rel_safe = os.path.relpath(safe_path, base_dir)
                                db_conn = get_db_connection(self.db.db_path)
                                with db_conn:
                                    db_conn.execute("UPDATE documents SET filepath = ? WHERE base_dir = ? AND filepath = ?", (rel_safe, base_dir, rel_target))
                                    db_conn.execute("UPDATE documents SET filepath = ? || SUBSTR(filepath, ?) WHERE base_dir = ? AND filepath LIKE ?", (rel_safe, len(rel_target) + 1, base_dir, rel_target + os.sep + '%'))
                        try:
                            os.symlink(symlink_target, target_abs)
                        except OSError as e:
                            import logging
                            logging.warning(f"Failed to recreate symbolic link at {target_abs}: {e}")
                        
                except Exception as e:
                    conn.execute("UPDATE sessions SET status = 'failed' WHERE session_id = ?", (session_id,))
                    conn.commit()
                    raise e

                # Clean empty directories
                from app.core.mover import _remove_empty_dirs
                for entry in os.listdir(base_dir):
                    entry_path = os.path.join(base_dir, entry)
                    if os.path.isdir(entry_path):
                        _remove_empty_dirs(entry_path)

                # Restore Cache
                cur = conn.execute("SELECT corpus, locked_files, index_to_word, manual_folders FROM snapshot_cache WHERE session_id = ?", (session_id,))
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
                            (base_dir, row[0], row[1], row[2], row[3])
                        )
                    else:
                        cache_conn.execute("DELETE FROM directory_cache WHERE source_directory = ?", (base_dir,))



                conn.execute("UPDATE sessions SET status = 'rolled_back' WHERE session_id = ?", (session_id,))

        return self.db.worker.execute_write(_write)
