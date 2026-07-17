"""Local database management for autosorter."""

from contextlib import closing
from pathlib import Path

import numpy as np

from app.core.crypto import SessionCrypto
from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker


class Database:
    """SQLite database abstraction for persistent storage of document state."""

    CURRENT_VERSION = 4

    def __init__(self, db_path: Path, worker: DBWorker):
        self.db_path = str(db_path)
        self.worker = worker
        key_path = Path(db_path).parent / "secret.key"
        self.crypto = SessionCrypto(key_path, Path(db_path))
        self.init_db()

    def init_db(self):
        """Initialize the core database and create tables if they do not exist."""
        conn = get_db_connection(self.db_path)
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
        """Retrieve a document by its base directory and filepath."""
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                "SELECT file_hash, extracted_text, embedding, model_name, vector_dimension FROM documents WHERE base_dir = ? AND filepath = ?",
                (base_dir, filepath),
            )
            row = cursor.fetchone()
            if row:
                decrypted_text = self.crypto.decrypt_text(row[1]) if row[1] is not None else None
                decrypted_emb_bytes = self.crypto.decrypt_embedding(row[2]) if row[2] is not None else None
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
        self.upsert_documents([(base_dir, filepath, file_hash, extracted_text, embedding, model_name, vector_dimension)])

    def upsert_documents(self, documents):
        """Insert or update multiple documents in the database."""
        if not documents:
            return
            
        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                rows_to_insert = []
                for doc in documents:
                    base_dir, filepath, file_hash, extracted_text, embedding, model_name, vector_dimension = doc
                    
                    if embedding is not None:
                        embedding_blob = self.crypto.encrypt_embedding(embedding.astype(np.float32).tobytes())
                    else:
                        embedding_blob = None
                        
                    enc_text = self.crypto.encrypt_text(extracted_text) if extracted_text is not None else None
                    
                    rows_to_insert.append(
                        (base_dir, filepath, file_hash, enc_text, embedding_blob, model_name, vector_dimension)
                    )
                    
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
        self.worker.execute_write(_write)

    def get_all_documents(self, base_dir):
        """Retrieve all valid documents for a given base directory."""
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                "SELECT filepath, extracted_text, embedding, file_hash, user_verified_target_path, model_name, vector_dimension FROM documents WHERE base_dir = ?",
                (base_dir,),
            )
            rows = cursor.fetchall()

            import concurrent.futures
            
            def _decrypt_row(row):
                decrypted_text = self.crypto.decrypt_text(row[1]) if row[1] is not None else None
                decrypted_emb_bytes = self.crypto.decrypt_embedding(row[2]) if row[2] is not None else None
                embedding = np.frombuffer(decrypted_emb_bytes, dtype=np.float32) if decrypted_emb_bytes is not None else None
                return (row[0], decrypted_text, embedding, row[3], row[4], row[5], row[6])
                
            results = []
            if rows:
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    results = list(executor.map(_decrypt_row, rows))
                    
            return results

    def set_user_verified_target(self, base_dir, file_hash, target_path):
        """Record the historical folder assignment for a specific document hash."""
        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    "UPDATE documents SET user_verified_target_path = ? WHERE base_dir = ? AND file_hash = ?",
                    (target_path, base_dir, file_hash),
                )
        self.worker.execute_write(_write)

    def remove_document(self, base_dir, filepath):
        """Remove a document and its historical assignments when deleted."""
        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute("DELETE FROM documents WHERE base_dir = ? AND filepath = ?", (base_dir, filepath))
        self.worker.execute_write(_write)

    def update_document_path(self, base_dir, old_filepath, new_filepath):
        """Update a document's path and historical assignment when moved."""
        import os
        new_dir = os.path.dirname(new_filepath).replace("\\", "/")
        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    "UPDATE documents SET filepath = ?, user_verified_target_path = ? WHERE base_dir = ? AND filepath = ?",
                    (new_filepath, new_dir, base_dir, old_filepath)
                )
        self.worker.execute_write(_write)

    def clear(self, base_dir=None):
        """Clear documents from the database. If base_dir is provided, only clear those."""
        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                if base_dir:
                    conn.execute("DELETE FROM documents WHERE base_dir = ?", (base_dir,))
                else:
                    conn.execute("DELETE FROM documents")
        self.worker.execute_write(_write)
