"""Local database management for autosorter."""

from pathlib import Path

from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker


class Database:
    """SQLite database abstraction for persistent storage of document state."""

    CURRENT_VERSION = 4

    def __init__(self, db_path: Path, worker: DBWorker):
        self.db_path = str(db_path)
        self.worker = worker
        from app.core.path_utils import resolve_db_crypto

        self.crypto = resolve_db_crypto(db_path)
        import threading

        self._cache_lock = threading.Lock()
        self._cached_base_dir = None
        self._cached_documents = None
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
                        user_verified_target_path TEXT,
                        PRIMARY KEY (base_dir, filepath)
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_documents_file_hash ON documents (base_dir, file_hash)"
                )
                conn.execute(f"PRAGMA user_version = {self.CURRENT_VERSION}")
            elif db_version < self.CURRENT_VERSION:
                if db_version == 1:
                    conn.execute(
                        "ALTER TABLE documents ADD COLUMN user_verified_target_path TEXT"
                    )
                if db_version <= 3:
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_documents_file_hash ON documents (base_dir, file_hash)"
                    )
                conn.execute(f"PRAGMA user_version = {self.CURRENT_VERSION}")

            # Initialize decoupled vector and metadata tables unconditionally
            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS document_vectors (
                    base_dir TEXT,
                    filepath TEXT,
                    vector TEXT,
                    PRIMARY KEY (base_dir, filepath),
                    FOREIGN KEY (base_dir, filepath) REFERENCES documents(base_dir, filepath) ON DELETE CASCADE
                )
            """)

            # Purge existing unencrypted vector cache on startup to prevent reading insecure data
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT base_dir, filepath, vector FROM document_vectors")
                rows = cursor.fetchall()
                unencrypted_keys = []
                for b_dir, f_path, vector in rows:
                    if vector:
                        is_unencrypted = False
                        if isinstance(vector, str):
                            stripped = vector.strip()
                            if stripped.startswith('[') and stripped.endswith(']'):
                                try:
                                    import json
                                    _ = json.loads(stripped)
                                    is_unencrypted = True
                                except Exception:
                                    pass
                        elif isinstance(vector, bytes):
                            try:
                                decoded = vector.decode('utf-8').strip()
                                if decoded.startswith('[') and decoded.endswith(']'):
                                    import json
                                    _ = json.loads(decoded)
                                    is_unencrypted = True
                            except Exception:
                                pass
                        
                        if is_unencrypted:
                            unencrypted_keys.append((b_dir, f_path))
                
                if unencrypted_keys:
                    for b_dir, f_path in unencrypted_keys:
                        conn.execute(
                            "DELETE FROM document_vectors WHERE base_dir = ? AND filepath = ?",
                            (b_dir, f_path)
                        )
            except Exception:
                pass

    def invalidate_cache(self):
        """Invalidate the in-memory decrypted documents cache."""
        with self._cache_lock:
            self._cached_base_dir = None
            self._cached_documents = None

    def _populate_cache_if_needed(self, base_dir):
        """Ensure the decrypted documents cache is populated for the base directory."""
        if self._cached_base_dir == base_dir and self._cached_documents is not None:
            return

        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents is not None:
                return

            self._cached_base_dir = None
            self._cached_documents = None

            conn = get_db_connection(self.db_path)
            with conn:
                cursor = conn.execute(
                    "SELECT filepath, extracted_text, file_hash, user_verified_target_path FROM documents WHERE base_dir = ?",
                    (base_dir,),
                )
                rows = cursor.fetchall()

            import concurrent.futures

            def _decrypt_row(row):
                decrypted_text = (
                    self.crypto.decrypt_text(row[1]) if row[1] is not None else None
                )
                return (row[0].replace("\\", "/"), decrypted_text, row[2], row[3])

            results = []
            if rows:
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    results = list(executor.map(_decrypt_row, rows))

            self._cached_base_dir = base_dir
            self._cached_documents = results

    def get_document(self, base_dir, filepath):
        """Retrieve a document by its base directory and filepath."""
        filepath = filepath.replace("\\", "/")
        self._populate_cache_if_needed(base_dir)
        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents is not None:
                for row in self._cached_documents:
                    if row[0] == filepath:
                        return {
                            "file_hash": row[2],
                            "extracted_text": row[1],
                        }
                return None

        # Fallback to DB
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                "SELECT file_hash, extracted_text FROM documents WHERE base_dir = ? AND filepath = ?",
                (base_dir, filepath),
            )
            row = cursor.fetchone()
            if row:
                decrypted_text = (
                    self.crypto.decrypt_text(row[1]) if row[1] is not None else None
                )
                return {
                    "file_hash": row[0],
                    "extracted_text": decrypted_text,
                }
            return None

    def upsert_document(self, base_dir, filepath, file_hash, extracted_text):
        """Insert or update a document in the database."""
        self.upsert_documents([(base_dir, filepath, file_hash, extracted_text)])

    def upsert_documents(self, documents):
        """Insert or update multiple documents in the database."""
        if not documents:
            return
        self.invalidate_cache()

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                rows_to_insert = []
                for doc in documents:
                    base_dir, filepath, file_hash, extracted_text = doc
                    filepath = filepath.replace("\\", "/")

                    enc_text = (
                        self.crypto.encrypt_text(extracted_text)
                        if extracted_text is not None
                        else None
                    )

                    rows_to_insert.append((base_dir, filepath, file_hash, enc_text))

                conn.executemany(
                    """
                    INSERT INTO documents (base_dir, filepath, file_hash, extracted_text)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(base_dir, filepath) DO UPDATE SET
                        file_hash = excluded.file_hash,
                        extracted_text = excluded.extracted_text
                """,
                    rows_to_insert,
                )
            self.invalidate_cache()

        self.worker.execute_write(_write)

    def get_all_documents(self, base_dir):
        """Retrieve all valid documents for a given base directory."""
        self._populate_cache_if_needed(base_dir)
        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents is not None:
                return list(self._cached_documents)

        # Fallback to DB query directly if cache was invalidated concurrently
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                "SELECT filepath, extracted_text, file_hash, user_verified_target_path FROM documents WHERE base_dir = ?",
                (base_dir,),
            )
            rows = cursor.fetchall()

            import concurrent.futures

            def _decrypt_row(row):
                decrypted_text = (
                    self.crypto.decrypt_text(row[1]) if row[1] is not None else None
                )
                return (row[0].replace("\\", "/"), decrypted_text, row[2], row[3])

            results = []
            if rows:
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    results = list(executor.map(_decrypt_row, rows))

            return results

    def set_user_verified_target(self, base_dir, file_hash, target_path):
        """Record the historical folder assignment for a specific document hash."""
        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents is not None:
                new_docs = []
                for row in self._cached_documents:
                    if row[2] == file_hash:
                        new_docs.append((row[0], row[1], row[2], target_path))
                    else:
                        new_docs.append(row)
                self._cached_documents = new_docs

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    "UPDATE documents SET user_verified_target_path = ? WHERE base_dir = ? AND file_hash = ?",
                    (target_path, base_dir, file_hash),
                )

        self.worker.execute_write_async(_write)

    def remove_document(self, base_dir, filepath):
        """Remove a document and its historical assignments when deleted."""
        filepath = filepath.replace("\\", "/")
        self.invalidate_cache()

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    "DELETE FROM documents WHERE base_dir = ? AND filepath = ?",
                    (base_dir, filepath),
                )
            self.invalidate_cache()

        self.worker.execute_write(_write)

    def update_document_path(self, base_dir, old_filepath, new_filepath):
        """Update a document's path and historical assignment when moved."""
        import os

        old_filepath = old_filepath.replace("\\", "/")
        new_filepath = new_filepath.replace("\\", "/")
        new_dir = os.path.dirname(new_filepath).replace("\\", "/")

        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents is not None:
                new_docs = []
                for row in self._cached_documents:
                    if row[0] == old_filepath:
                        new_docs.append((new_filepath, row[1], row[2], new_dir))
                    else:
                        new_docs.append(row)
                self._cached_documents = new_docs

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    "UPDATE documents SET filepath = ?, user_verified_target_path = ? WHERE base_dir = ? AND filepath = ?",
                    (new_filepath, new_dir, base_dir, old_filepath),
                )

        self.worker.execute_write_async(_write)

    def execute_batch_updates(self, updates):
        """Execute all collected database updates in a single unified database transaction."""
        if not updates:
            return
        self.invalidate_cache()

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                for item in updates:
                    if item["type"] == "verified_target":
                        base_dir, file_hash, target_path = item["args"]
                        conn.execute(
                            "UPDATE documents SET user_verified_target_path = ? WHERE base_dir = ? AND file_hash = ?",
                            (target_path, base_dir, file_hash),
                        )
                    elif item["type"] == "document_path":
                        base_dir, old_filepath, new_filepath = item["args"]
                        old_filepath = old_filepath.replace("\\", "/")
                        new_filepath = new_filepath.replace("\\", "/")
                        import os

                        old_filepath = old_filepath.replace("\\", "/")
                        new_filepath = new_filepath.replace("\\", "/")
                        new_dir = os.path.dirname(new_filepath).replace("\\", "/")
                        conn.execute(
                            "UPDATE documents SET filepath = ?, user_verified_target_path = ? WHERE base_dir = ? AND filepath = ?",
                            (new_filepath, new_dir, base_dir, old_filepath),
                        )
            self.invalidate_cache()

        self.worker.execute_write(_write)

    def clear(self, base_dir=None):
        """Clear documents from the database. If base_dir is provided, only clear those."""
        self.invalidate_cache()

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                if base_dir:
                    conn.execute(
                        "DELETE FROM documents WHERE base_dir = ?", (base_dir,)
                    )
                else:
                    conn.execute("DELETE FROM documents")
            self.invalidate_cache()

        self.worker.execute_write(_write)

    def get_model_metadata(self, key: str) -> str | None:
        """Get model metadata value for a given key."""
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                "SELECT value FROM model_metadata WHERE key = ?", (key,)
            )
            row = cursor.fetchone()
            return row[0] if row else None

    def set_model_metadata(self, key: str, value: str):
        """Set model metadata value for a given key."""

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    """
                    INSERT INTO model_metadata (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value
                    """,
                    (key, value),
                )

        self.worker.execute_write(_write)

    def get_document_vector(self, base_dir: str, filepath: str) -> list[float] | None:
        """Retrieve decoupled vector for a document."""
        filepath = filepath.replace("\\", "/")
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                "SELECT vector FROM document_vectors WHERE base_dir = ? AND filepath = ?",
                (base_dir, filepath),
            )
            row = cursor.fetchone()
            if row and row[0]:
                import json

                try:
                    decrypted = self.crypto.decrypt_vector(row[0])
                    return json.loads(decrypted)
                except Exception:
                    return None
            return None

    def upsert_document_vectors(
        self, base_dir: str, vectors_data: list[tuple[str, list[float]]]
    ):
        """Upsert document vector embeddings into the decoupled table."""
        if not vectors_data:
            return

        def _write():
            import json

            conn = get_db_connection(self.db_path)
            with conn:
                rows_to_insert = []
                for filepath, vector in vectors_data:
                    filepath = filepath.replace("\\", "/")
                    vector_str = json.dumps(vector)
                    enc_vector = self.crypto.encrypt_vector(vector_str).decode("utf-8")
                    rows_to_insert.append((base_dir, filepath, enc_vector))
                conn.executemany(
                    """
                    INSERT INTO document_vectors (base_dir, filepath, vector)
                    VALUES (?, ?, ?)
                    ON CONFLICT(base_dir, filepath) DO UPDATE SET
                        vector = excluded.vector
                    """,
                    rows_to_insert,
                )

        self.worker.execute_write(_write)

    def clear_all_document_vectors(self):
        """Delete all document vectors from the decoupled table."""

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute("DELETE FROM document_vectors")

        self.worker.execute_write(_write)

    def get_documents_missing_vectors(
        self, base_dir: str, limit: int = 50, offset: int = 0
    ) -> list[tuple[str, str]]:
        """Retrieve decrypted documents missing vector embeddings in batched format."""
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                """
                SELECT d.filepath, d.extracted_text 
                FROM documents d
                LEFT JOIN document_vectors v ON d.base_dir = v.base_dir AND d.filepath = v.filepath
                WHERE d.base_dir = ? AND v.vector IS NULL
                LIMIT ? OFFSET ?
                """,
                (base_dir, limit, offset),
            )
            rows = cursor.fetchall()
            results = []
            for filepath, enc_text in rows:
                dec_text = (
                    self.crypto.decrypt_text(enc_text) if enc_text is not None else None
                )
                results.append((filepath, dec_text))
            return results
