"""Local database management for autosorter."""

import sqlite3
from contextlib import closing

import numpy as np

from app.config import get_app_dir


class Database:
    """SQLite database abstraction for persistent storage of document state."""

    CURRENT_VERSION = 1

    def __init__(self, db_path=None):
        self.db_path = db_path or str(get_app_dir() / "autosorter.db")
        self._init_db()

    def _init_db(self):
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
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
                        PRIMARY KEY (base_dir, filepath)
                    )
                """)
                conn.execute(f"PRAGMA user_version = {self.CURRENT_VERSION}")
            elif db_version < self.CURRENT_VERSION:
                pass

    def get_document(self, base_dir, filepath):
        """Retrieve a document by its base directory and filepath."""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            cursor = conn.execute(
                "SELECT file_hash, extracted_text, embedding FROM documents WHERE base_dir = ? AND filepath = ?",
                (base_dir, filepath),
            )
            row = cursor.fetchone()
            if row:
                embedding = np.frombuffer(row[2], dtype=np.float32) if row[2] else None
                return {
                    "file_hash": row[0],
                    "extracted_text": row[1],
                    "embedding": embedding,
                }
            return None

    def upsert_document(self, base_dir, filepath, file_hash, extracted_text, embedding):
        """Insert or update a document in the database."""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            if embedding is not None:
                embedding_blob = embedding.astype(np.float32).tobytes()
            else:
                embedding_blob = None
            conn.execute(
                """
                INSERT INTO documents (base_dir, filepath, file_hash, extracted_text, embedding)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(base_dir, filepath) DO UPDATE SET
                    file_hash = excluded.file_hash,
                    extracted_text = excluded.extracted_text,
                    embedding = excluded.embedding
            """,
                (base_dir, filepath, file_hash, extracted_text, embedding_blob),
            )

    def get_all_documents(self, base_dir):
        """Retrieve all valid documents for a given base directory."""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            cursor = conn.execute(
                "SELECT filepath, extracted_text, embedding FROM documents WHERE base_dir = ?",
                (base_dir,),
            )
            results = []
            for row in cursor.fetchall():
                embedding = np.frombuffer(row[2], dtype=np.float32) if row[2] is not None else None
                results.append((row[0], row[1], embedding))
            return results

    def clear(self, base_dir=None):
        """Clear documents from the database. If base_dir is provided, only clear those."""
        with closing(sqlite3.connect(self.db_path)) as conn, conn:
            if base_dir:
                conn.execute("DELETE FROM documents WHERE base_dir = ?", (base_dir,))
            else:
                conn.execute("DELETE FROM documents")


db = Database()
