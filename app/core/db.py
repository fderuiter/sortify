import sqlite3
from contextlib import closing
import numpy as np
import threading
import queue
import os

from app.config import get_app_dir
from app.core.db_conn import get_db_connection

class Database:
    CURRENT_VERSION = 4

    def __init__(self, db_path=None):
        self.db_path = db_path or str(get_app_dir() / "autosorter.db")
        self._write_queue = queue.Queue()
        self._worker_thread = threading.Thread(target=self._write_worker, daemon=True)
        self._worker_thread.start()
        # Initialize DB synchronously to ensure it's ready
        self._execute_write(self._init_db_sync)

    def _write_worker(self):
        while True:
            task = self._write_queue.get()
            if task is None:
                break
            func, args, kwargs, res_q = task
            try:
                res = func(*args, **kwargs)
                res_q.put((True, res))
            except Exception as e:
                res_q.put((False, e))
            self._write_queue.task_done()

    def _execute_write(self, func, *args, **kwargs):
        res_q = queue.Queue()
        self._write_queue.put((func, args, kwargs, res_q))
        success, res = res_q.get()
        if not success:
            raise res
        return res

    def _init_db(self):
        self._execute_write(self._init_db_sync)

    def _init_db_sync(self):
        with closing(get_db_connection(self.db_path)) as conn, conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA user_version")
            db_version = cursor.fetchone()[0]

            if db_version == 0:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS documents (
                        base_dir TEXT,
                        filepath TEXT,
                        file_hash TEXT,
                        extracted_text TEXT,
                        embedding BLOB,
                        user_verified_target_path TEXT,
                        model_name TEXT,
                        vector_dimension INTEGER,
                        PRIMARY KEY (base_dir, filepath)
                    )
                """)
                conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_file_hash ON documents (base_dir, file_hash)")
                conn.execute(f"PRAGMA user_version = {self.CURRENT_VERSION}")
            elif db_version < self.CURRENT_VERSION:
                if db_version == 1:
                    conn.execute("ALTER TABLE documents ADD COLUMN user_verified_target_path TEXT")
                if db_version <= 2:
                    conn.execute("ALTER TABLE documents ADD COLUMN model_name TEXT")
                    conn.execute("ALTER TABLE documents ADD COLUMN vector_dimension INTEGER")
                if db_version <= 3:
                    conn.execute("CREATE INDEX IF NOT EXISTS idx_documents_file_hash ON documents (base_dir, file_hash)")
                conn.execute(f"PRAGMA user_version = {self.CURRENT_VERSION}")

    def get_document(self, base_dir, filepath):
        with closing(get_db_connection(self.db_path)) as conn, conn:
            cursor = conn.execute(
                "SELECT file_hash, extracted_text, embedding, model_name, vector_dimension FROM documents WHERE base_dir = ? AND filepath = ?",
                (base_dir, filepath),
            )
            row = cursor.fetchone()
            if row:
                text = row[1]
                emb_bytes = row[2]
                embedding = np.frombuffer(emb_bytes, dtype=np.float32) if emb_bytes else None
                return {
                    "file_hash": row[0],
                    "extracted_text": text,
                    "embedding": embedding,
                    "model_name": row[3],
                    "vector_dimension": row[4],
                }
            return None

    def upsert_document(self, base_dir, filepath, file_hash, extracted_text, embedding, model_name=None, vector_dimension=None):
        self.upsert_documents([(base_dir, filepath, file_hash, extracted_text, embedding, model_name, vector_dimension)])

    def _upsert_documents_sync(self, documents):
        if not documents:
            return
        with closing(get_db_connection(self.db_path)) as conn, conn:
            rows_to_insert = []
            for doc in documents:
                base_dir, filepath, file_hash, extracted_text, embedding, model_name, vector_dimension = doc
                embedding_blob = embedding.astype(np.float32).tobytes() if embedding is not None else None
                rows_to_insert.append((base_dir, filepath, file_hash, extracted_text, embedding_blob, model_name, vector_dimension))
                
            conn.executemany(
                """
                INSERT INTO documents (base_dir, filepath, file_hash, extracted_text, embedding, model_name, vector_dimension)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(base_dir, filepath) DO UPDATE SET
                    file_hash = excluded.file_hash,
                    extracted_text = excluded.extracted_text,
                    embedding = excluded.embedding,
                    model_name = excluded.model_name,
                    vector_dimension = excluded.vector_dimension
            """,
                rows_to_insert,
            )

    def upsert_documents(self, documents):
        self._execute_write(self._upsert_documents_sync, documents)

    def get_all_documents(self, base_dir):
        with closing(get_db_connection(self.db_path)) as conn, conn:
            cursor = conn.execute(
                "SELECT filepath, extracted_text, embedding, file_hash, user_verified_target_path, model_name, vector_dimension FROM documents WHERE base_dir = ?",
                (base_dir,),
            )
            rows = cursor.fetchall()

            import concurrent.futures
            def _map_row(row):
                text = row[1]
                emb_bytes = row[2]
                embedding = np.frombuffer(emb_bytes, dtype=np.float32) if emb_bytes is not None else None
                return (row[0], text, embedding, row[3], row[4], row[5], row[6])
                
            results = []
            if rows:
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    results = list(executor.map(_map_row, rows))
            return results

    def _set_user_verified_target_sync(self, base_dir, file_hash, target_path):
        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE documents SET user_verified_target_path = ? WHERE base_dir = ? AND file_hash = ?",
                (target_path, base_dir, file_hash),
            )

    def set_user_verified_target(self, base_dir, file_hash, target_path):
        self._execute_write(self._set_user_verified_target_sync, base_dir, file_hash, target_path)

    def _remove_document_sync(self, base_dir, filepath):
        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute("DELETE FROM documents WHERE base_dir = ? AND filepath = ?", (base_dir, filepath))

    def remove_document(self, base_dir, filepath):
        self._execute_write(self._remove_document_sync, base_dir, filepath)

    def _update_document_path_sync(self, base_dir, old_filepath, new_filepath):
        new_dir = os.path.dirname(new_filepath).replace("\\", "/")
        with closing(get_db_connection(self.db_path)) as conn, conn:
            conn.execute(
                "UPDATE documents SET filepath = ?, user_verified_target_path = ? WHERE base_dir = ? AND filepath = ?",
                (new_filepath, new_dir, base_dir, old_filepath)
            )

    def update_document_path(self, base_dir, old_filepath, new_filepath):
        self._execute_write(self._update_document_path_sync, base_dir, old_filepath, new_filepath)

    def _clear_sync(self, base_dir=None):
        with closing(get_db_connection(self.db_path)) as conn, conn:
            if base_dir:
                conn.execute("DELETE FROM documents WHERE base_dir = ?", (base_dir,))
            else:
                conn.execute("DELETE FROM documents")

    def clear(self, base_dir=None):
        self._execute_write(self._clear_sync, base_dir)


db = Database()
