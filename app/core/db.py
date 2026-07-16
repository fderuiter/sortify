"""Local database management for autosorter."""

import sys
from contextlib import closing

import numpy as np

from app.config import get_app_dir
from app.core.crypto import (
    decrypt_embedding,
    decrypt_text,
    encrypt_embedding,
    encrypt_text,
    get_cipher,
    get_raw_key
)

def get_sqlite_engine():
    import importlib
    
    if hasattr(sys, '_MEIPASS'):
        import os
        sys.path.insert(0, sys._MEIPASS)
        
    try:
        # Dynamically import to hide from PyInstaller, preventing standard compiler errors
        return importlib.import_module("sqlcipher3.dbapi2")
    except ImportError:
        import sqlite3
        return sqlite3

sqlite3 = get_sqlite_engine()

def get_connection(db_path):
    conn = sqlite3.connect(db_path, check_same_thread=False)
    try:
        # Use Fernet key as SQLCipher password since it is consistent and securely generated
        raw_key = get_raw_key()
        conn.execute(f"PRAGMA key = '{raw_key}'")
    except Exception as e:
        print('GET CONNECTION EXCEPTION:', e)
        import traceback; traceback.print_exc()
        # If cipher fails to load, just let it be unencrypted (e.g. before key is generated)
        pass
    return conn

class Database:
    """SQLite database abstraction for persistent storage of document state."""

    CURRENT_VERSION = 3

    def __init__(self, db_path=None):
        self.db_path = db_path or str(get_app_dir() / "autosorter.db")
        self._conn = None
        self._init_db()

    def _get_cached_conn(self):
        if self._conn is None:
            self._conn = get_connection(self.db_path)
        return self._conn

    def _init_db(self):
        conn = self._get_cached_conn()
        with conn:
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
                conn.execute(f"PRAGMA user_version = {self.CURRENT_VERSION}")
            elif db_version < self.CURRENT_VERSION:
                if db_version == 1:
                    conn.execute("ALTER TABLE documents ADD COLUMN user_verified_target_path TEXT")
                if db_version <= 2:
                    conn.execute("ALTER TABLE documents ADD COLUMN model_name TEXT")
                    conn.execute("ALTER TABLE documents ADD COLUMN vector_dimension INTEGER")
                conn.execute(f"PRAGMA user_version = {self.CURRENT_VERSION}")

    def get_document(self, base_dir, filepath):
        """Retrieve a document by its base directory and filepath."""
        conn = self._get_cached_conn()
        with conn:
            cursor = conn.execute(
                "SELECT file_hash, extracted_text, embedding, model_name, vector_dimension FROM documents WHERE base_dir = ? AND filepath = ?",
                (base_dir, filepath),
            )
            row = cursor.fetchone()
            if row:
                decrypted_text = decrypt_text(row[1]) if row[1] is not None else None
                decrypted_emb_bytes = decrypt_embedding(row[2]) if row[2] is not None else None
                embedding = np.frombuffer(decrypted_emb_bytes, dtype=np.float32) if decrypted_emb_bytes else None
                return {
                    "file_hash": row[0],
                    "extracted_text": decrypted_text,
                    "embedding": embedding,
                    "model_name": row[3],
                    "vector_dimension": row[4],
                }
            return None

    def upsert_document(self, base_dir, filepath, file_hash, extracted_text, embedding, model_name=None, vector_dimension=None):
        """Insert or update a document in the database."""
        conn = self._get_cached_conn()
        with conn:
            if embedding is not None:
                embedding_blob = encrypt_embedding(embedding.astype(np.float32).tobytes())
            else:
                embedding_blob = None
                
            enc_text = encrypt_text(extracted_text) if extracted_text is not None else None
            
            conn.execute(
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
                (base_dir, filepath, file_hash, enc_text, embedding_blob, model_name, vector_dimension),
            )

    def get_all_documents(self, base_dir):
        """Retrieve all valid documents for a given base directory."""
        conn = self._get_cached_conn()
        with conn:
            cursor = conn.execute(
                "SELECT filepath, extracted_text, embedding, file_hash, user_verified_target_path, model_name, vector_dimension FROM documents WHERE base_dir = ?",
                (base_dir,),
            )
            results = []
            for row in cursor.fetchall():
                decrypted_text = decrypt_text(row[1]) if row[1] is not None else None
                decrypted_emb_bytes = decrypt_embedding(row[2]) if row[2] is not None else None
                embedding = np.frombuffer(decrypted_emb_bytes, dtype=np.float32) if decrypted_emb_bytes is not None else None
                results.append((row[0], decrypted_text, embedding, row[3], row[4], row[5], row[6]))
            return results

    def set_user_verified_target(self, base_dir, file_hash, target_path):
        """Record the historical folder assignment for a specific document hash."""
        conn = self._get_cached_conn()
        with conn:
            conn.execute(
                "UPDATE documents SET user_verified_target_path = ? WHERE base_dir = ? AND file_hash = ?",
                (target_path, base_dir, file_hash),
            )

    def remove_document(self, base_dir, filepath):
        """Remove a document and its historical assignments when deleted."""
        conn = self._get_cached_conn()
        with conn:
            conn.execute("DELETE FROM documents WHERE base_dir = ? AND filepath = ?", (base_dir, filepath))

    def update_document_path(self, base_dir, old_filepath, new_filepath):
        """Update a document's path and historical assignment when moved."""
        import os
        new_dir = os.path.dirname(new_filepath).replace("\\", "/")
        conn = self._get_cached_conn()
        with conn:
            conn.execute(
                "UPDATE documents SET filepath = ?, user_verified_target_path = ? WHERE base_dir = ? AND filepath = ?",
                (new_filepath, new_dir, base_dir, old_filepath)
            )

    def clear(self, base_dir=None):
        """Clear documents from the database. If base_dir is provided, only clear those."""
        conn = self._get_cached_conn()
        with conn:
            if base_dir:
                conn.execute("DELETE FROM documents WHERE base_dir = ?", (base_dir,))
            else:
                conn.execute("DELETE FROM documents")


db = Database()
