"""Local database management for autosorter."""

from pathlib import Path

from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker


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
        self.corrupted_vectors = set()
        self._corrupted_vectors_lock = threading.Lock()
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
                        rating TEXT,
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

            try:
                conn.execute("ALTER TABLE documents ADD COLUMN rating TEXT")
            except Exception:
                pass

            # Initialize TF-IDF tables
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tfidf_vocab (
                    base_dir TEXT,
                    term TEXT,
                    df INTEGER,
                    PRIMARY KEY (base_dir, term)
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tfidf_doc_terms (
                    base_dir TEXT,
                    filepath TEXT,
                    term TEXT,
                    tf INTEGER,
                    PRIMARY KEY (base_dir, filepath, term)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tfidf_doc_terms_path ON tfidf_doc_terms (base_dir, filepath)"
            )

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
                cursor.execute(
                    "SELECT base_dir, filepath, vector FROM document_vectors"
                )
                rows = cursor.fetchall()
                unencrypted_keys = []
                for b_dir, f_path, vector in rows:
                    if vector:
                        is_unencrypted = False
                        if isinstance(vector, str):
                            stripped = vector.strip()
                            if stripped.startswith("[") and stripped.endswith("]"):
                                try:
                                    import json

                                    _ = json.loads(stripped)
                                    is_unencrypted = True
                                except Exception:
                                    pass
                        elif isinstance(vector, bytes):
                            try:
                                decoded = vector.decode("utf-8").strip()
                                if decoded.startswith("[") and decoded.endswith("]"):
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
                            (b_dir, f_path),
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

                for doc in documents:
                    base_dir, filepath, file_hash, extracted_text = doc
                    filepath = filepath.replace("\\", "/")
                    cursor = conn.execute(
                        "SELECT user_verified_target_path FROM documents WHERE base_dir = ? AND filepath = ?",
                        (base_dir, filepath),
                    )
                    row = cursor.fetchone()
                    target_path = row[0] if row else None
                    if target_path:
                        if self._is_tfidf_eligible(filepath, extracted_text):
                            self._update_tfidf_for_document_conn(
                                conn, base_dir, filepath, extracted_text, True
                            )
                        else:
                            cursor = conn.execute(
                                "SELECT 1 FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ? LIMIT 1",
                                (base_dir, filepath),
                            )
                            if cursor.fetchone() is not None:
                                self._update_tfidf_for_document_conn(
                                    conn, base_dir, filepath, None, False
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
                cursor = conn.execute(
                    "SELECT filepath FROM documents WHERE base_dir = ? AND file_hash = ?",
                    (base_dir, file_hash),
                )
                filepaths = [row[0] for row in cursor.fetchall()]

                conn.execute(
                    "UPDATE documents SET user_verified_target_path = ? WHERE base_dir = ? AND file_hash = ?",
                    (target_path, base_dir, file_hash),
                )
                for fp in filepaths:
                    self._update_tfidf_on_verified_target_change(
                        conn, base_dir, fp, target_path
                    )

        self.worker.execute_write_async(_write)

    def remove_document(self, base_dir, filepath):
        """Remove a document and its historical assignments when deleted."""
        filepath = filepath.replace("\\", "/")
        self.invalidate_cache()

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                cursor = conn.execute(
                    "SELECT 1 FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ? LIMIT 1",
                    (base_dir, filepath),
                )
                was_in = cursor.fetchone() is not None
                if was_in:
                    self._update_tfidf_for_document_conn(
                        conn, base_dir, filepath, None, False
                    )
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
                cursor = conn.execute(
                    "SELECT 1 FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ? LIMIT 1",
                    (base_dir, old_filepath),
                )
                was_in = cursor.fetchone() is not None
                if was_in:
                    conn.execute(
                        "UPDATE tfidf_doc_terms SET filepath = ? WHERE base_dir = ? AND filepath = ?",
                        (new_filepath, base_dir, old_filepath),
                    )
                else:
                    if new_dir:
                        cursor = conn.execute(
                            "SELECT extracted_text FROM documents WHERE base_dir = ? AND filepath = ?",
                            (base_dir, new_filepath),
                        )
                        row = cursor.fetchone()
                        if row and row[0]:
                            try:
                                decrypted_text = self.crypto.decrypt_text(row[0])
                            except Exception:
                                decrypted_text = None
                            if decrypted_text and self._is_tfidf_eligible(
                                new_filepath, decrypted_text
                            ):
                                self._update_tfidf_for_document_conn(
                                    conn, base_dir, new_filepath, decrypted_text, True
                                )

        self.worker.execute_write_async(_write)

    def set_user_verified_target_path(self, base_dir, filepath, target_path):
        """Record the historical folder assignment for a specific document path."""
        filepath = filepath.replace("\\", "/")
        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents is not None:
                new_docs = []
                for row in self._cached_documents:
                    if row[0] == filepath:
                        new_docs.append((row[0], row[1], row[2], target_path))
                    else:
                        new_docs.append(row)
                self._cached_documents = new_docs

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    "UPDATE documents SET user_verified_target_path = ? WHERE base_dir = ? AND filepath = ?",
                    (target_path, base_dir, filepath),
                )
                self._update_tfidf_on_verified_target_change(
                    conn, base_dir, filepath, target_path
                )

        self.worker.execute_write_async(_write)

    def _is_tfidf_eligible(self, filepath: str, extracted_text: str | None) -> bool:
        if not filepath or not extracted_text:
            return False
        if not filepath.lower().endswith(
            (".txt", ".docx", ".csv", ".xlsx", ".xls", ".pdf")
        ):
            return False
        if extracted_text.startswith("[STATUS:"):
            return False
        return True

    def _update_tfidf_on_verified_target_change(
        self, conn, base_dir, filepath, target_path
    ):
        filepath = filepath.replace("\\", "/")
        if target_path:
            cursor = conn.execute(
                "SELECT 1 FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ? LIMIT 1",
                (base_dir, filepath),
            )
            already_in = cursor.fetchone() is not None
            if not already_in:
                cursor = conn.execute(
                    "SELECT extracted_text FROM documents WHERE base_dir = ? AND filepath = ?",
                    (base_dir, filepath),
                )
                row = cursor.fetchone()
                if row and row[0]:
                    try:
                        decrypted_text = self.crypto.decrypt_text(row[0])
                    except Exception:
                        decrypted_text = None
                    if decrypted_text and self._is_tfidf_eligible(
                        filepath, decrypted_text
                    ):
                        self._update_tfidf_for_document_conn(
                            conn, base_dir, filepath, decrypted_text, True
                        )
        else:
            cursor = conn.execute(
                "SELECT 1 FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ? LIMIT 1",
                (base_dir, filepath),
            )
            was_in = cursor.fetchone() is not None
            if was_in:
                self._update_tfidf_for_document_conn(
                    conn, base_dir, filepath, None, False
                )

    def _update_tfidf_for_document_conn(
        self,
        conn,
        base_dir,
        filepath,
        text,
        is_added_or_updated: bool,
        stop_words_list=None,
    ):
        filepath = filepath.replace("\\", "/")
        cursor = conn.execute(
            "SELECT term, tf FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ?",
            (base_dir, filepath),
        )
        existing_terms = {row[0]: row[1] for row in cursor.fetchall()}

        if is_added_or_updated:
            from collections import Counter

            from sklearn.feature_extraction.text import TfidfVectorizer

            if stop_words_list is None:
                stop_words_list = "english"

            try:
                vectorizer = TfidfVectorizer(stop_words=stop_words_list)
                analyzer = vectorizer.build_analyzer()
                tokens = analyzer(text)
            except Exception:
                import re

                tokens = re.findall(r"\b\w\w+\b", text.lower())
                from sklearn.feature_extraction import text as sklearn_text

                stops = set(sklearn_text.ENGLISH_STOP_WORDS)
                if isinstance(stop_words_list, str) and stop_words_list == "english":
                    tokens = [t for t in tokens if t not in stops]
                elif stop_words_list:
                    tokens = [t for t in tokens if t not in stop_words_list]

            new_terms = Counter(tokens)

            terms_to_delete = []
            terms_to_insert = []
            terms_to_update = []

            for term, existing_tf in existing_terms.items():
                if term not in new_terms:
                    terms_to_delete.append(term)

            for term, new_tf in new_terms.items():
                if term in existing_terms:
                    if existing_tf != new_tf:
                        terms_to_update.append((new_tf, base_dir, filepath, term))
                else:
                    terms_to_insert.append((base_dir, filepath, term, new_tf))

            if terms_to_delete:
                conn.executemany(
                    "DELETE FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ? AND term = ?",
                    [(base_dir, filepath, t) for t in terms_to_delete],
                )
                for t in terms_to_delete:
                    conn.execute(
                        "UPDATE tfidf_vocab SET df = df - 1 WHERE base_dir = ? AND term = ?",
                        (base_dir, t),
                    )

            if terms_to_insert:
                conn.executemany(
                    "INSERT INTO tfidf_doc_terms (base_dir, filepath, term, tf) VALUES (?, ?, ?, ?)",
                    terms_to_insert,
                )
                for doc_row in terms_to_insert:
                    t = doc_row[2]
                    conn.execute(
                        """
                        INSERT INTO tfidf_vocab (base_dir, term, df)
                        VALUES (?, ?, 1)
                        ON CONFLICT(base_dir, term) DO UPDATE SET df = df + 1
                        """,
                        (base_dir, t),
                    )

            if terms_to_update:
                conn.executemany(
                    "UPDATE tfidf_doc_terms SET tf = ? WHERE base_dir = ? AND filepath = ? AND term = ?",
                    terms_to_update,
                )

            all_modified_terms = terms_to_delete
            if all_modified_terms:
                conn.executemany(
                    "DELETE FROM tfidf_vocab WHERE base_dir = ? AND term = ? AND df <= 0",
                    [(base_dir, t) for t in all_modified_terms],
                )

        else:
            if existing_terms:
                conn.execute(
                    "DELETE FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ?",
                    (base_dir, filepath),
                )
                for t in existing_terms:
                    conn.execute(
                        "UPDATE tfidf_vocab SET df = df - 1 WHERE base_dir = ? AND term = ?",
                        (base_dir, t),
                    )
                conn.executemany(
                    "DELETE FROM tfidf_vocab WHERE base_dir = ? AND term = ? AND df <= 0",
                    [(base_dir, t) for t in existing_terms],
                )

    def set_document_rating(self, base_dir: str, filepath: str, rating: str | None):
        """Record the quality feedback rating associated with a document path."""
        filepath = filepath.replace("\\", "/")

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    "UPDATE documents SET rating = ? WHERE base_dir = ? AND filepath = ?",
                    (rating, base_dir, filepath),
                )

        self.worker.execute_write_async(_write)

    def set_document_rating_by_hash(
        self, base_dir: str, file_hash: str, rating: str | None
    ):
        """Record the quality feedback rating associated with a document hash."""

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    "UPDATE documents SET rating = ? WHERE base_dir = ? AND file_hash = ?",
                    (rating, base_dir, file_hash),
                )

        self.worker.execute_write_async(_write)

    def get_all_document_ratings(self, base_dir: str) -> dict[str, str]:
        """Retrieve all document ratings for a given base directory."""
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                "SELECT filepath, rating FROM documents WHERE base_dir = ? AND rating IS NOT NULL",
                (base_dir,),
            )
            return {row[0].replace("\\", "/"): row[1] for row in cursor.fetchall()}

    def get_document_rating(self, base_dir: str, filepath: str) -> str | None:
        """Retrieve feedback rating for a specific document path."""
        filepath = filepath.replace("\\", "/")
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                "SELECT rating FROM documents WHERE base_dir = ? AND filepath = ?",
                (base_dir, filepath),
            )
            row = cursor.fetchone()
            return row[0] if row else None

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
                        cursor = conn.execute(
                            "SELECT filepath FROM documents WHERE base_dir = ? AND file_hash = ?",
                            (base_dir, file_hash),
                        )
                        filepaths = [row[0] for row in cursor.fetchall()]

                        conn.execute(
                            "UPDATE documents SET user_verified_target_path = ? WHERE base_dir = ? AND file_hash = ?",
                            (target_path, base_dir, file_hash),
                        )
                        for fp in filepaths:
                            self._update_tfidf_on_verified_target_change(
                                conn, base_dir, fp, target_path
                            )
                    elif item["type"] == "document_path":
                        base_dir, old_filepath, new_filepath = item["args"]
                        old_filepath = old_filepath.replace("\\", "/")
                        new_filepath = new_filepath.replace("\\", "/")
                        import os

                        new_dir = os.path.dirname(new_filepath).replace("\\", "/")
                        conn.execute(
                            "UPDATE documents SET filepath = ?, user_verified_target_path = ? WHERE base_dir = ? AND filepath = ?",
                            (new_filepath, new_dir, base_dir, old_filepath),
                        )
                        cursor = conn.execute(
                            "SELECT 1 FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ? LIMIT 1",
                            (base_dir, old_filepath),
                        )
                        was_in = cursor.fetchone() is not None
                        if was_in:
                            conn.execute(
                                "UPDATE tfidf_doc_terms SET filepath = ? WHERE base_dir = ? AND filepath = ?",
                                (new_filepath, base_dir, old_filepath),
                            )
                        else:
                            if new_dir:
                                cursor = conn.execute(
                                    "SELECT extracted_text FROM documents WHERE base_dir = ? AND filepath = ?",
                                    (base_dir, new_filepath),
                                )
                                row = cursor.fetchone()
                                if row and row[0]:
                                    try:
                                        decrypted_text = self.crypto.decrypt_text(
                                            row[0]
                                        )
                                    except Exception:
                                        decrypted_text = None
                                    if decrypted_text and self._is_tfidf_eligible(
                                        new_filepath, decrypted_text
                                    ):
                                        self._update_tfidf_for_document_conn(
                                            conn,
                                            base_dir,
                                            new_filepath,
                                            decrypted_text,
                                            True,
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
                        "DELETE FROM tfidf_vocab WHERE base_dir = ?", (base_dir,)
                    )
                    conn.execute(
                        "DELETE FROM tfidf_doc_terms WHERE base_dir = ?", (base_dir,)
                    )
                else:
                    conn.execute("DELETE FROM documents")
                    conn.execute("DELETE FROM tfidf_vocab")
                    conn.execute("DELETE FROM tfidf_doc_terms")
            self.invalidate_cache()

        self.worker.execute_write(_write)

    def get_tfidf_stats(self, base_dir: str):
        """Retrieve total document count, vocabulary and document-term frequencies for TF-IDF calculations."""
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                "SELECT COUNT(DISTINCT filepath) FROM tfidf_doc_terms WHERE base_dir = ?",
                (base_dir,),
            )
            N = cursor.fetchone()[0] or 0

            if N == 0:
                return 0, [], [], {}

            cursor = conn.execute(
                """
                SELECT t.term, v.df FROM (
                    SELECT term, SUM(tf) as total_tf FROM tfidf_doc_terms WHERE base_dir = ? GROUP BY term
                ) t INNER JOIN tfidf_vocab v ON t.term = v.term WHERE v.base_dir = ? ORDER BY t.total_tf DESC LIMIT 1000
                """,
                (base_dir, base_dir),
            )
            top_terms = cursor.fetchall()

            cursor = conn.execute(
                "SELECT filepath, term, tf FROM tfidf_doc_terms WHERE base_dir = ?",
                (base_dir,),
            )
            doc_terms = cursor.fetchall()

            cursor = conn.execute(
                "SELECT filepath, user_verified_target_path FROM documents WHERE base_dir = ? AND user_verified_target_path IS NOT NULL AND user_verified_target_path != ''",
                (base_dir,),
            )
            doc_metadata = {
                row[0].replace("\\", "/"): row[1] for row in cursor.fetchall()
            }

            return N, top_terms, doc_terms, doc_metadata

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

    def track_corrupted_vector(self, base_dir: str, filepath: str):
        """Track document file paths whose vectors failed to decrypt or deserialize in-memory."""
        if not base_dir or not filepath:
            return
        base_dir = base_dir.replace("\\", "/")
        filepath = filepath.replace("\\", "/")
        with self._corrupted_vectors_lock:
            if len(self.corrupted_vectors) < 1000:
                self.corrupted_vectors.add((base_dir, filepath))

    def get_corrupted_vectors_by_base_dir(self, base_dir: str) -> list[str]:
        """Retrieve in-memory tracked corrupted document file paths for a given base directory."""
        if not base_dir:
            return []
        base_dir_norm = base_dir.replace("\\", "/")
        with self._corrupted_vectors_lock:
            return [
                filepath for b_dir, filepath in self.corrupted_vectors
                if b_dir.replace("\\", "/").lower() == base_dir_norm.lower()
            ]

    def clear_corrupted_vectors(self, base_dir: str, filepaths: list[str]):
        """Clear successfully reconstructed document file paths from the in-memory failure tracker."""
        if not base_dir or not filepaths:
            return
        base_dir_norm = base_dir.replace("\\", "/")
        filepaths_norm = {f.replace("\\", "/") for f in filepaths}
        with self._corrupted_vectors_lock:
            self.corrupted_vectors = {
                (b_dir, filepath) for b_dir, filepath in self.corrupted_vectors
                if not (
                    b_dir.replace("\\", "/").lower() == base_dir_norm.lower()
                    and filepath.replace("\\", "/") in filepaths_norm
                )
            }

    def get_documents_by_filepaths(self, base_dir: str, filepaths: list[str]) -> list[tuple[str, str]]:
        """Retrieve filepath and decrypted text for specific files."""
        if not base_dir or not filepaths:
            return []
        
        conn = get_db_connection(self.db_path)
        results = []
        with conn:
            # Process in small chunks to avoid sqlite query argument limits
            chunk_size = 50
            for i in range(0, len(filepaths), chunk_size):
                chunk = filepaths[i:i+chunk_size]
                placeholders = ",".join(["?"] * len(chunk))
                cursor = conn.execute(
                    f"""
                    SELECT filepath, extracted_text
                    FROM documents
                    WHERE base_dir = ? AND filepath IN ({placeholders})
                    """,
                    (base_dir, *chunk),
                )
                rows = cursor.fetchall()
                for filepath, enc_text in rows:
                    dec_text = (
                        self.crypto.decrypt_text(enc_text) if enc_text is not None else None
                    )
                    results.append((filepath, dec_text))
        return results

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
                    self.track_corrupted_vector(base_dir, filepath)
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
            filepaths_to_clear = [filepath for filepath, _ in vectors_data]
            self.clear_corrupted_vectors(base_dir, filepaths_to_clear)

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
