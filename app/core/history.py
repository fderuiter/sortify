"""History management module for snapshotting and rollback."""

import os
import shutil
import sqlite3
from app.core.db_conn import get_db_connection
from app.core.db_worker import worker
import time
import uuid
from contextlib import closing
from typing import Any, Dict, List

from app.config import get_app_dir
from app.core.db import db as db_instance


def init_history_db(db_path=None):
    db_path = db_path or str(get_app_dir() / "history.db")
    with closing(get_db_connection(db_path)) as conn, conn:
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
                FOREIGN KEY(session_id) REFERENCES sessions(session_id)
            )
        """)
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


class HistoryManager:
    """Manages full directory snapshots and rollback functionality."""
    
    def __init__(self, db_path=None):
        self.db_path = db_path or str(get_app_dir() / "history.db")

    def create_snapshot(self, base_dir: str) -> str:
        """Create a complete snapshot of the directory tree and its metadata."""
        def _write():
            session_id = str(uuid.uuid4())
            timestamp = time.time()

            from app.core.scanner import get_files_recursively
            files = get_files_recursively(base_dir)

            with closing(get_db_connection(self.db_path)) as conn, conn:
                conn.execute(
                    "INSERT INTO sessions (session_id, timestamp, base_dir, status) VALUES (?, ?, ?, ?)",
                    (session_id, timestamp, base_dir, "active")
                )
                
                # 1. Snapshot Files
                file_records = []
                for rel_path in files:
                    abs_path = os.path.join(base_dir, rel_path)
                    try:
                        st = os.stat(abs_path)
                        file_records.append((session_id, rel_path, st.st_ino, st.st_size, st.st_mtime))
                    except OSError:
                        continue
                if file_records:
                    conn.executemany(
                        "INSERT INTO snapshot_files (session_id, original_rel_path, inode, size, mtime) VALUES (?, ?, ?, ?, ?)",
                        file_records
                    )

                # 2. Snapshot Cache
                from app.core.cache import _get_conn as get_cache_conn
                with closing(get_cache_conn()) as cache_conn, cache_conn:
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
                with closing(get_db_connection(db_instance.db_path)) as db_conn, db_conn:
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
        return worker.execute_write(_write)

    def _prune_snapshots(self, conn, limit=10):
        cur = conn.execute("SELECT session_id FROM sessions ORDER BY timestamp DESC LIMIT -1 OFFSET ?", (limit,))
        old_sessions = [row[0] for row in cur.fetchall()]
        for sid in old_sessions:
            conn.execute("DELETE FROM snapshot_files WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM snapshot_cache WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM snapshot_documents WHERE session_id = ?", (sid,))
            conn.execute("DELETE FROM sessions WHERE session_id = ?", (sid,))

    def get_sessions(self) -> List[Dict[str, Any]]:
        """Retrieve a list of all historical sessions, ordered by time."""
        with closing(get_db_connection(self.db_path)) as conn, conn:
            cur = conn.execute("SELECT session_id, timestamp, base_dir, status FROM sessions ORDER BY timestamp DESC")
            return [{"session_id": r[0], "timestamp": r[1], "base_dir": r[2], "status": r[3]} for r in cur.fetchall()]

    def check_missing_files(self, session_id: str) -> List[str]:
        """Check if any files from the snapshot are missing from the disk."""
        with closing(get_db_connection(self.db_path)) as conn, conn:
            cur = conn.execute("SELECT base_dir FROM sessions WHERE session_id = ?", (session_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError("Session not found")
            base_dir = row[0]

            cur = conn.execute("SELECT original_rel_path, inode, size, mtime FROM snapshot_files WHERE session_id = ?", (session_id,))
            snapshot_files = cur.fetchall()

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
                st = os.stat(abs_path)
            except OSError:
                continue
                
            ino = st.st_ino
            size = st.st_size
            mtime = st.st_mtime
            
            inode_counts[ino] = inode_counts.get(ino, 0) + 1
            if ino == 0 or inode_counts[ino] > 1:
                inodes_reliable = False
                
            current_inodes[ino] = abs_path
            active_files_by_rel_path[rel_path] = (size, mtime)
            
            sig = (size, mtime)
            if sig not in active_files_by_sig:
                active_files_by_sig[sig] = []
            active_files_by_sig[sig].append(abs_path)

        missing = []
        for rel_path, inode, size, mtime in snapshot_files:
            found = False
            
            if inodes_reliable and inode in current_inodes:
                abs_path = current_inodes[inode]
                del current_inodes[inode] # consume it
                found = True
            else:
                # Fallback Step A
                curr_sig = active_files_by_rel_path.get(rel_path)
                if curr_sig == (size, mtime):
                    abs_path = os.path.join(base_dir, rel_path)
                    if curr_sig in active_files_by_sig and abs_path in active_files_by_sig[curr_sig]:
                        active_files_by_sig[curr_sig].remove(abs_path)
                    found = True
                else:
                    # Fallback Step B
                    sig = (size, mtime)
                    if sig in active_files_by_sig and active_files_by_sig[sig]:
                        active_files_by_sig[sig].pop(0)
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

            with closing(get_db_connection(self.db_path)) as conn, conn:
                cur = conn.execute("SELECT base_dir FROM sessions WHERE session_id = ?", (session_id,))
                row = cur.fetchone()
                if not row:
                    raise ValueError("Session not found")
                base_dir = row[0]

                cur = conn.execute("SELECT original_rel_path, inode, size, mtime FROM snapshot_files WHERE session_id = ?", (session_id,))
                snapshot_files = cur.fetchall()

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
                        st = os.stat(abs_path)
                    except OSError:
                        continue
                    
                    ino = st.st_ino
                    size = st.st_size
                    mtime = st.st_mtime
                
                    inode_counts[ino] = inode_counts.get(ino, 0) + 1
                    if ino == 0 or inode_counts[ino] > 1:
                        inodes_reliable = False
                    
                    current_inodes[ino] = abs_path
                    active_files_by_rel_path[rel_path] = (size, mtime)
                
                    sig = (size, mtime)
                    if sig not in active_files_by_sig:
                        active_files_by_sig[sig] = []
                    active_files_by_sig[sig].append(abs_path)

                # First compute all intended moves
                moves = []
                for rel_path, inode, size, mtime in snapshot_files:
                    target_abs = os.path.join(base_dir, rel_path)
                    current_abs = None
                
                    if inodes_reliable and inode in current_inodes:
                        current_abs = current_inodes[inode]
                        del current_inodes[inode]
                    else:
                        curr_sig = active_files_by_rel_path.get(rel_path)
                        if curr_sig == (size, mtime):
                            current_abs = target_abs
                            if curr_sig in active_files_by_sig and current_abs in active_files_by_sig[curr_sig]:
                                active_files_by_sig[curr_sig].remove(current_abs)
                        else:
                            sig = (size, mtime)
                            if sig in active_files_by_sig and active_files_by_sig[sig]:
                                current_abs = active_files_by_sig[sig].pop(0)

                    if current_abs:
                        if os.path.exists(current_abs) and not os.path.samefile(current_abs, target_abs) if os.path.exists(target_abs) else True:
                            if current_abs != target_abs:
                                moves.append((current_abs, target_abs))

                planned_source_rels = [os.path.relpath(m[0], base_dir) for m in moves]
                planned_target_rels = [os.path.relpath(m[1], base_dir) for m in moves]

                cur = conn.execute("SELECT filepath, file_hash, extracted_text, embedding FROM snapshot_documents WHERE session_id = ?", (session_id,))
                snapshot_docs = cur.fetchall()
                snapshot_docs_dict = {r[0]: r for r in snapshot_docs}
                snapshot_filepaths = set(snapshot_docs_dict.keys())

                # 1. Pre-Move Synchronization
                with closing(get_db_connection(db_instance.db_path)) as db_conn, db_conn:
                    cur_docs = db_conn.execute("SELECT filepath FROM documents WHERE base_dir = ?", (base_dir,))
                    current_filepaths = [row[0] for row in cur_docs.fetchall()]

                    to_delete = []
                    for fp in current_filepaths:
                        if fp not in snapshot_filepaths and fp not in planned_source_rels:
                            to_delete.append((base_dir, fp))
                    if to_delete:
                        db_conn.executemany("DELETE FROM documents WHERE base_dir = ? AND filepath = ?", to_delete)

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
                created_temps = []
                try:
                    for src, dst in moves:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        rel_src = os.path.relpath(src, base_dir)
                        rel_dst = os.path.relpath(dst, base_dir)

                        if os.path.exists(dst) and not os.path.samefile(src, dst):
                            # Collision: move existing target out of the way temporarily
                            temp_dst = dst + f".tmp.{uuid.uuid4().hex}"
                            shutil.move(dst, temp_dst)
                            created_temps.append(temp_dst)
                        
                            rel_temp_dst = os.path.relpath(temp_dst, base_dir)

                            with closing(get_db_connection(db_instance.db_path)) as db_conn, db_conn:
                                db_conn.execute("UPDATE documents SET filepath = ? WHERE base_dir = ? AND filepath = ?", (rel_temp_dst, base_dir, rel_dst))

                            # The file that was at dst is now at temp_dst. 
                            # If this file is also part of our 'moves', we need to update its src in the 'moves' list.
                            for i, (m_src, m_dst) in enumerate(moves):
                                if m_src == dst:
                                    moves[i] = (temp_dst, m_dst)
                            shutil.move(src, dst)
                        else:
                            if not os.path.exists(dst):
                                shutil.move(src, dst)

                        with closing(get_db_connection(db_instance.db_path)) as db_conn, db_conn:
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
                            
                    for tmp in created_temps:
                        if os.path.exists(tmp):
                            os.remove(tmp)
                        
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
                from app.core.cache import _get_conn as get_cache_conn
                with closing(get_cache_conn()) as cache_conn, cache_conn:
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
        return worker.execute_write(_write)

history_manager = HistoryManager()
