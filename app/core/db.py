"""Local database management for autosorter."""

from pathlib import Path

from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker


class LazyDecryptedDoc:
    """A lazy-decrypted document that behaves exactly like a 4-tuple of (filepath, decrypted_text, file_hash, user_verified_target_path) but only performs decryption and SQLite fetching when accessed."""

    def __init__(self, db, base_dir, filepath, file_hash, user_verified_target_path):
        self.db = db
        self.base_dir = base_dir
        self.filepath = filepath
        self.file_hash = file_hash
        self.user_verified_target_path = user_verified_target_path
        self._decrypted_text = None
        self._is_decrypted = False

    def __getitem__(self, index):
        """Get item at index."""
        if index == 0:
            return self.filepath
        elif index == 1:
            if not self._is_decrypted:
                conn = get_db_connection(self.db.db_path)
                with conn:
                    cursor = conn.cursor()
                    cursor.execute(
                        "SELECT extracted_text FROM documents WHERE base_dir = ? AND filepath = ?",
                        (self.base_dir, self.filepath),
                    )
                    row = cursor.fetchone()
                    encrypted_text = row[0] if row else None

                self._decrypted_text = (
                    self.db.crypto.decrypt_text(encrypted_text)
                    if encrypted_text is not None
                    else None
                )
                self._is_decrypted = True
            return self._decrypted_text
        elif index == 2:
            return self.file_hash
        elif index == 3:
            return self.user_verified_target_path
        raise IndexError("Tuple index out of range")

    def __len__(self):
        """Get tuple length."""
        return 4

    def __iter__(self):
        """Iterate over elements."""
        yield self[0]
        yield self[1]
        yield self[2]
        yield self[3]


class Database:
    """SQLite database abstraction for persistent storage of document state."""

    CURRENT_VERSION = 5

    def __init__(self, db_path: Path, worker: DBWorker):
        self.db_path = str(db_path)
        self.worker = worker
        from app.core.path_utils import resolve_db_crypto

        self.crypto = resolve_db_crypto(db_path)
        import threading
        self._cache_lock = threading.Lock()
        self._cached_base_dir = None
        self._cached_documents = None
        self._cached_term_frequencies = None
        self._cached_documents_lazy = None
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
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS term_frequencies (
                        base_dir TEXT,
                        filepath TEXT,
                        term TEXT,
                        frequency INTEGER,
                        PRIMARY KEY (base_dir, filepath, term)
                    )
                """)
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_term_frequencies_doc ON term_frequencies (base_dir, filepath)"
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
                if db_version <= 4:
                    conn.execute("""
                        CREATE TABLE IF NOT EXISTS term_frequencies (
                            base_dir TEXT,
                            filepath TEXT,
                            term TEXT,
                            frequency INTEGER,
                            PRIMARY KEY (base_dir, filepath, term)
                        )
                    """)
                    conn.execute(
                        "CREATE INDEX IF NOT EXISTS idx_term_frequencies_doc ON term_frequencies (base_dir, filepath)"
                    )
                conn.execute(f"PRAGMA user_version = {self.CURRENT_VERSION}")

    def invalidate_cache(self):
        """Invalidate the in-memory decrypted documents cache."""
        with self._cache_lock:
            self._cached_base_dir = None
            self._cached_documents = None
            self._cached_term_frequencies = None
            self._cached_documents_lazy = None

    def _populate_cache_if_needed(self, base_dir):
        """Ensure the decrypted documents cache is populated for the base directory."""
        if self._cached_base_dir == base_dir and self._cached_documents is not None:
            return

        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents is not None:
                return

            if self._cached_base_dir != base_dir:
                self._cached_base_dir = None
                self._cached_documents = None
                self._cached_term_frequencies = None
                self._cached_documents_lazy = None

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
                return (row[0], decrypted_text, row[2], row[3])

            results = []
            if rows:
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    results = list(executor.map(_decrypt_row, rows))

            self._cached_base_dir = base_dir
            self._cached_documents = results

    def get_document(self, base_dir, filepath):
        """Retrieve a document by its base directory and filepath."""
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
                import re
                from collections import Counter

                rows_to_insert = []
                freq_rows = []
                for doc in documents:
                    base_dir, filepath, file_hash, extracted_text = doc

                    enc_text = (
                        self.crypto.encrypt_text(extracted_text)
                        if extracted_text is not None
                        else None
                    )

                    rows_to_insert.append((base_dir, filepath, file_hash, enc_text))

                    # Clean up old term frequencies before inserting new ones
                    conn.execute(
                        "DELETE FROM term_frequencies WHERE base_dir = ? AND filepath = ?",
                        (base_dir, filepath),
                    )

                    if extracted_text:
                        tokens = re.findall(r'\b\w+\b', extracted_text.lower())
                        counts = Counter(tokens)
                        for term, freq in counts.items():
                            freq_rows.append((base_dir, filepath, term, freq))

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

                if freq_rows:
                    conn.executemany(
                        """
                        INSERT INTO term_frequencies (base_dir, filepath, term, frequency)
                        VALUES (?, ?, ?, ?)
                    """,
                        freq_rows,
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
                return (row[0], decrypted_text, row[2], row[3])

            results = []
            if rows:
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    results = list(executor.map(_decrypt_row, rows))

            return results

    def get_all_documents_lazy(self, base_dir):
        """Retrieve all documents for a given base directory as lazy-decrypted tuples."""
        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents_lazy is not None:
                return list(self._cached_documents_lazy)

            if self._cached_base_dir != base_dir:
                self._cached_base_dir = None
                self._cached_documents = None
                self._cached_term_frequencies = None
                self._cached_documents_lazy = None

        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                "SELECT filepath, file_hash, user_verified_target_path FROM documents WHERE base_dir = ?",
                (base_dir,),
            )
            rows = cursor.fetchall()
        
        lazy_docs = [
            LazyDecryptedDoc(self, base_dir, row[0], row[1], row[2])
            for row in rows
        ]

        with self._cache_lock:
            self._cached_base_dir = base_dir
            self._cached_documents_lazy = lazy_docs

        return lazy_docs

    def set_user_verified_target(self, base_dir, file_hash, target_path):
        """Record the historical folder assignment for a specific document hash."""
        self.invalidate_cache()

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    "UPDATE documents SET user_verified_target_path = ? WHERE base_dir = ? AND file_hash = ?",
                    (target_path, base_dir, file_hash),
                )
            self.invalidate_cache()

        self.worker.execute_write(_write)

    def remove_document(self, base_dir, filepath):
        """Remove a document and its historical assignments when deleted."""
        self.invalidate_cache()

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    "DELETE FROM documents WHERE base_dir = ? AND filepath = ?",
                    (base_dir, filepath),
                )
                conn.execute(
                    "DELETE FROM term_frequencies WHERE base_dir = ? AND filepath = ?",
                    (base_dir, filepath),
                )
            self.invalidate_cache()

        self.worker.execute_write(_write)

    def update_document_path(self, base_dir, old_filepath, new_filepath):
        """Update a document's path and historical assignment when moved."""
        self.invalidate_cache()
        import os

        new_dir = os.path.dirname(new_filepath).replace("\\", "/")

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    "UPDATE documents SET filepath = ?, user_verified_target_path = ? WHERE base_dir = ? AND filepath = ?",
                    (new_filepath, new_dir, base_dir, old_filepath),
                )
                conn.execute(
                    "UPDATE term_frequencies SET filepath = ? WHERE base_dir = ? AND filepath = ?",
                    (new_filepath, base_dir, old_filepath),
                )
            self.invalidate_cache()

        self.worker.execute_write(_write)

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
                        import os

                        new_dir = os.path.dirname(new_filepath).replace("\\", "/")
                        conn.execute(
                            "UPDATE documents SET filepath = ?, user_verified_target_path = ? WHERE base_dir = ? AND filepath = ?",
                            (new_filepath, new_dir, base_dir, old_filepath),
                        )
                        conn.execute(
                            "UPDATE term_frequencies SET filepath = ? WHERE base_dir = ? AND filepath = ?",
                            (new_filepath, base_dir, old_filepath),
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
                    conn.execute(
                        "DELETE FROM term_frequencies WHERE base_dir = ?", (base_dir,)
                    )
                else:
                    conn.execute("DELETE FROM documents")
                    conn.execute("DELETE FROM term_frequencies")
            self.invalidate_cache()

        self.worker.execute_write(_write)

    def get_term_frequencies(self, base_dir):
        """Retrieve all term frequencies for documents in a given base directory."""
        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_term_frequencies is not None:
                return list(self._cached_term_frequencies)

            if self._cached_base_dir != base_dir:
                self._cached_base_dir = None
                self._cached_documents = None
                self._cached_term_frequencies = None
                self._cached_documents_lazy = None

        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                "SELECT filepath, term, frequency FROM term_frequencies WHERE base_dir = ?",
                (base_dir,),
            )
            rows = cursor.fetchall()

        with self._cache_lock:
            self._cached_base_dir = base_dir
            self._cached_term_frequencies = rows

        return rows
