"""Local database management for autosorter."""

import logging
import os
import weakref
from pathlib import Path
from typing import Any, Optional

from app.core.db_conn import get_db_connection
from app.core.db_worker import DBWorker
from app.core.migrations import Migration, add_missing_columns, migrate

AUDIO_EXTENSIONS = (
    ".mp3",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".ogg",
    ".wma",
    ".aiff",
    ".alac",
    ".opus",
    ".mp2",
    ".amr",
    ".3gp",
)

ELIGIBLE_DOCUMENT_EXTENSIONS = (
    ".txt",
    ".docx",
    ".csv",
    ".xlsx",
    ".xls",
    ".pdf",
)


def _main_db_baseline(conn) -> None:
    """Bring a new or pre-migration main database to the schema of version 7.

    Versions 1 to 6 were set by the old ad hoc upgrade code, so this works on
    any of them: it creates missing tables and indexes and adds missing columns.
    """
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
    add_missing_columns(
        conn, "documents", {"user_verified_target_path": "TEXT", "rating": "TEXT"}
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_file_hash ON documents (base_dir, file_hash)"
    )

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tfidf_matrix_cache (
            base_dir TEXT,
            filepath TEXT,
            term TEXT,
            weight REAL,
            PRIMARY KEY (base_dir, filepath, term)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tfidf_matrix_cache_base ON tfidf_matrix_cache (base_dir)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_tfidf_matrix_cache_file ON tfidf_matrix_cache (base_dir, filepath)"
    )

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
            model_signature TEXT,
            PRIMARY KEY (base_dir, filepath),
            FOREIGN KEY (base_dir, filepath) REFERENCES documents(base_dir, filepath) ON DELETE CASCADE
        )
    """)
    add_missing_columns(conn, "document_vectors", {"model_signature": "TEXT"})

    conn.execute("""
        CREATE TABLE IF NOT EXISTS transaction_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            step_number INTEGER NOT NULL,
            base_dir TEXT NOT NULL,
            source_path TEXT NOT NULL,
            destination_path TEXT NOT NULL,
            file_hash TEXT,
            item_type TEXT DEFAULT 'file',
            metadata TEXT,
            timestamp REAL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_transaction_ledger_session ON transaction_ledger (session_id, step_number)"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS quarantine_records (
            job_id TEXT PRIMARY KEY,
            base_dir TEXT NOT NULL,
            original_filepath TEXT NOT NULL,
            staged_filepath TEXT NOT NULL,
            file_hash TEXT,
            status TEXT NOT NULL,
            policy_action TEXT,
            audit_log TEXT,
            created_at REAL,
            updated_at REAL,
            error_message TEXT
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quarantine_records_base_dir ON quarantine_records (base_dir, status)"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS jev_classification_cache (
            file_path TEXT PRIMARY KEY,
            mtime REAL NOT NULL,
            size INTEGER NOT NULL,
            category TEXT NOT NULL,
            sensitivity_rating TEXT NOT NULL,
            sensitivity_score REAL NOT NULL,
            archival_priority INTEGER NOT NULL,
            archival_priority_score REAL NOT NULL,
            confidence REAL NOT NULL,
            is_classified INTEGER NOT NULL,
            metadata TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_jev_cache_lookup ON jev_classification_cache (file_path, mtime, size)"
    )


# Ordered schema migrations for the main database. Never edit one after release;
# add a new entry instead. Numbering starts at 7 because the old upgrade code
# already used user_version 1 to 6.
MAIN_DB_MIGRATIONS = (Migration(7, "baseline", _main_db_baseline),)


class Database:
    """SQLite database abstraction for persistent storage of document state."""

    CURRENT_VERSION = MAIN_DB_MIGRATIONS[-1].version
    _active_instances = weakref.WeakSet()

    def __init__(
        self,
        db_path: Path,
        worker: DBWorker,
        migrations: tuple[Migration, ...] = MAIN_DB_MIGRATIONS,
    ):
        self.db_path = str(db_path)
        self.worker = worker
        self.migrations = migrations
        Database._active_instances.add(self)
        from app.core.path_utils import resolve_db_crypto

        self.crypto = resolve_db_crypto(db_path)
        import threading

        self._cache_lock = threading.Lock()
        self._cached_base_dir = None
        self._cached_documents: Any = None
        self.corrupted_vectors = set()
        self._corrupted_vectors_lock = threading.Lock()
        from app.core.cache import SparseMatrixLRUCache

        self.matrix_lru_cache = SparseMatrixLRUCache(max_documents=500)
        self.init_db()

    def init_db(self):
        """Initialize the core database and create tables if they do not exist."""
        conn = get_db_connection(self.db_path)
        with conn:
            migrate(conn, self.migrations, label=f"Database {self.db_path}")

            # Purge existing audio-derived or non-eligible index terms from legacy databases
            try:
                purge_cursor = conn.cursor()
                purge_cursor.execute(
                    "SELECT DISTINCT base_dir, filepath FROM tfidf_doc_terms"
                )
                indexed_docs = purge_cursor.fetchall()
                for b_dir, f_path in indexed_docs:
                    if not self._is_tfidf_eligible_extension(f_path):
                        self._update_tfidf_for_document_conn(
                            conn, b_dir, f_path, None, False
                        )
                conn.execute("DELETE FROM tfidf_vocab WHERE df <= 0")
            except Exception:
                pass

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

    def invalidate_cache(self, base_dir: Optional[str] = None):
        """Invalidate the in-memory decrypted documents cache and matrix LRU cache."""
        with self._cache_lock:
            self._cached_base_dir = None
            if self._cached_documents is not None and hasattr(
                self._cached_documents, "clear"
            ):
                self._cached_documents.clear()
            self._cached_documents = None
        if hasattr(self, "matrix_lru_cache") and self.matrix_lru_cache:
            try:
                self.matrix_lru_cache.invalidate(base_dir)
            except Exception:
                pass

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

            from app.core.shared_registry import SharedWorkerPool

            def _decrypt_row(row):
                decrypted_text = (
                    self.crypto.decrypt_text(row[1]) if row[1] is not None else None
                )
                return (row[0].replace("\\", "/"), decrypted_text, row[2], row[3])

            results = []
            if rows:
                pool = SharedWorkerPool.get_instance()
                results = list(pool.map(_decrypt_row, rows))

            from app.core.cache import BoundedMemoryCache

            doc_cache = BoundedMemoryCache(max_size=10000)
            for row in results:
                doc_cache[row[0]] = row

            self._cached_base_dir = base_dir
            self._cached_documents = doc_cache

    def get_document(self, base_dir, filepath):
        """Retrieve a document by its base directory and filepath."""
        if "::" in filepath:
            s_root, rel_p = filepath.split("::", 1)
            base_dir = s_root
            filepath = rel_p

        filepath = filepath.replace("\\", "/")
        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents is not None:
                if hasattr(self._cached_documents, "get") and not isinstance(
                    self._cached_documents, list
                ):
                    row = self._cached_documents.get(filepath)
                    if row is not None:
                        return {
                            "file_hash": row[2],
                            "extracted_text": row[1],
                        }
                else:
                    for row in self._cached_documents:
                        if row[0] == filepath:
                            return {
                                "file_hash": row[2],
                                "extracted_text": row[1],
                            }

        # Fallback to DB
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                "SELECT file_hash, extracted_text, user_verified_target_path FROM documents WHERE base_dir = ? AND filepath = ?",
                (base_dir, filepath),
            )
            row = cursor.fetchone()
            if row:
                file_hash, raw_text, user_verified_target = row[0], row[1], row[2]
                decrypted_text = (
                    self.crypto.decrypt_text(raw_text) if raw_text is not None else None
                )
                with self._cache_lock:
                    if (
                        self._cached_base_dir == base_dir
                        and self._cached_documents is not None
                    ):
                        doc_tuple = (
                            filepath,
                            decrypted_text,
                            file_hash,
                            user_verified_target,
                        )
                        if hasattr(
                            self._cached_documents, "__setitem__"
                        ) and not isinstance(self._cached_documents, list):
                            self._cached_documents[filepath] = doc_tuple
                        elif isinstance(self._cached_documents, list):
                            for i, r in enumerate(self._cached_documents):
                                if r[0] == filepath:
                                    self._cached_documents[i] = doc_tuple
                                    break
                            else:
                                self._cached_documents.append(doc_tuple)
                return {
                    "file_hash": file_hash,
                    "extracted_text": decrypted_text,
                }
            return None

    def get_document_hashes(
        self, base_dir: str, filepaths: list[str]
    ) -> dict[str, str]:
        """Retrieve a mapping of filepath to file_hash for given filepaths in a base directory.

        Queries only filepath and file_hash columns with an SQL IN filter to bypass
        AES text decryption. Missing document records map to an empty string.
        """
        if not filepaths:
            return {}

        norm_map = {fp: fp.replace("\\", "/") for fp in filepaths}
        result = {orig_fp: "" for orig_fp in filepaths}

        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents is not None:
                if hasattr(self._cached_documents, "get") and not isinstance(
                    self._cached_documents, list
                ):
                    for orig_fp, norm_fp in norm_map.items():
                        row = self._cached_documents.get(norm_fp)
                        if row is not None and row[2]:
                            result[orig_fp] = row[2]
                else:
                    for row in self._cached_documents:
                        for orig_fp, norm_fp in norm_map.items():
                            if row[0] == norm_fp and row[2]:
                                result[orig_fp] = row[2]

        missing_norm = {
            norm_fp: orig_fp
            for orig_fp, norm_fp in norm_map.items()
            if not result[orig_fp]
        }
        if missing_norm:
            conn = get_db_connection(self.db_path)
            with conn:
                unique_missing = list(missing_norm.keys())
                batch_size = 500
                for i in range(0, len(unique_missing), batch_size):
                    batch = unique_missing[i : i + batch_size]
                    placeholders = ",".join("?" for _ in batch)
                    cursor = conn.execute(
                        f"SELECT filepath, file_hash FROM documents WHERE base_dir = ? AND filepath IN ({placeholders})",
                        [base_dir, *batch],
                    )
                    for row in cursor.fetchall():
                        db_fp, db_hash = row[0], row[1]
                        for orig_fp, norm_fp in norm_map.items():
                            if norm_fp == db_fp:
                                result[orig_fp] = db_hash or ""

        for orig_fp, norm_fp in norm_map.items():
            if norm_fp not in result:
                result[norm_fp] = result[orig_fp]

        return result

    def upsert_document(self, base_dir, filepath, file_hash, extracted_text):
        """Insert or update a document in the database."""
        if "::" in filepath:
            s_root, rel_p = filepath.split("::", 1)
            base_dir = s_root
            filepath = rel_p
        self.upsert_documents([(base_dir, filepath, file_hash, extracted_text)])

    def _compute_term_frequencies(
        self, text: str | None, stop_words_list=None
    ) -> dict[str, int]:
        """Tokenize text and calculate term frequencies off-lock on concurrent threads."""
        if not text:
            return {}
        from collections import Counter

        from sklearn.feature_extraction.text import TfidfVectorizer

        from app.core.text_utils import sanitize_text

        clean_text = sanitize_text(text or "")
        if not clean_text:
            return {}

        if stop_words_list is None:
            stop_words_list = "english"

        try:
            vectorizer = TfidfVectorizer(stop_words=stop_words_list)
            analyzer = vectorizer.build_analyzer()
            tokens = analyzer(clean_text)
        except Exception:
            import re

            tokens = re.findall(r"\b\w\w+\b", clean_text.lower())
            from sklearn.feature_extraction import text as sklearn_text

            stops = set(sklearn_text.ENGLISH_STOP_WORDS)
            if isinstance(stop_words_list, str) and stop_words_list == "english":
                tokens = [t for t in tokens if t not in stops]
            elif stop_words_list:
                tokens = [t for t in tokens if t not in stop_words_list]

        return dict(Counter(tokens))

    compute_term_frequencies = _compute_term_frequencies

    def upsert_documents(self, documents):
        """Insert or update multiple documents in the database."""
        if not documents:
            return
        self.invalidate_cache()

        precomputed_term_counts = {}

        def _process_doc(doc):
            b_dir, f_path, _, ext_text = doc[:4]
            if "::" in f_path:
                s_root, rel_p = f_path.split("::", 1)
                b_dir = s_root
                f_path = rel_p
            f_path = f_path.replace("\\", "/")
            if self._is_tfidf_eligible(f_path, ext_text):
                try:
                    counts = self.compute_term_frequencies(ext_text)
                    return (b_dir, f_path), counts
                except Exception:
                    return (b_dir, f_path), {}
            return (b_dir, f_path), None

        if len(documents) > 1:
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as executor:
                results = list(executor.map(_process_doc, documents))
                for key, counts in results:
                    precomputed_term_counts[key] = counts
        else:
            key, counts = _process_doc(documents[0])
            precomputed_term_counts[key] = counts

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                from app.core.text_utils import sanitize_text

                rows_to_insert = []
                for doc in documents:
                    base_dir, filepath, file_hash, extracted_text = doc[:4]
                    if "::" in filepath:
                        s_root, rel_p = filepath.split("::", 1)
                        base_dir = s_root
                        filepath = rel_p
                    filepath = filepath.replace("\\", "/")

                    sanitized_text = (
                        sanitize_text(extracted_text)
                        if extracted_text is not None
                        else None
                    )

                    enc_text = (
                        self.crypto.encrypt_text(sanitized_text)
                        if sanitized_text is not None
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
                    base_dir, filepath, file_hash, extracted_text = doc[:4]
                    if "::" in filepath:
                        s_root, rel_p = filepath.split("::", 1)
                        base_dir = s_root
                        filepath = rel_p
                    filepath = filepath.replace("\\", "/")
                    cursor = conn.execute(
                        "SELECT user_verified_target_path FROM documents WHERE base_dir = ? AND filepath = ?",
                        (base_dir, filepath),
                    )
                    row = cursor.fetchone()
                    target_path = row[0] if row else None
                    if target_path:
                        key = (base_dir, filepath)
                        counts = precomputed_term_counts.get(key)
                        if counts is not None:
                            self._update_tfidf_for_document_conn(
                                conn,
                                base_dir,
                                filepath,
                                None,
                                True,
                                term_counts=counts,
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

    def get_all_documents(self, base_dir, target_paths=None, source_dirs=None):
        """Retrieve valid documents for a given base directory or list of source_dirs, optionally restricted by target_paths."""
        multi_root = bool(source_dirs and len(source_dirs) > 1)

        sources = []
        if source_dirs:
            for s in source_dirs:
                if s:
                    if s not in sources:
                        sources.append(s)
                    norm_s = os.path.normpath(os.path.abspath(s))
                    if norm_s not in sources:
                        sources.append(norm_s)
        elif base_dir:
            sources.append(base_dir)
            norm_b = os.path.normpath(os.path.abspath(base_dir))
            if norm_b not in sources:
                sources.append(norm_b)

        if not sources:
            return []

        norm_targets = None
        candidate_filepaths = []
        if target_paths is not None:
            norm_targets = set()
            cand_set = set()

            for p in target_paths:
                if not p:
                    continue
                if "::" in p:
                    s_root, rel = p.split("::", 1)
                    norm_s = os.path.normpath(os.path.abspath(s_root))
                    norm_rel = rel.replace("\\", "/")
                    norm_targets.add(p)
                    norm_targets.add(f"{s_root}::{norm_rel}")
                    norm_targets.add(f"{norm_s}::{norm_rel}")
                    norm_targets.add(
                        f"{s_root}::{os.path.normpath(norm_rel).replace('\\', '/')}"
                    )
                    norm_targets.add(
                        f"{norm_s}::{os.path.normpath(norm_rel).replace('\\', '/')}"
                    )

                    for fp_variant in (
                        rel,
                        norm_rel,
                        os.path.normpath(norm_rel).replace("\\", "/"),
                        rel.replace("/", "\\"),
                        os.path.normpath(rel).replace("/", "\\"),
                    ):
                        if fp_variant and fp_variant not in cand_set:
                            cand_set.add(fp_variant)
                            candidate_filepaths.append(fp_variant)
                else:
                    norm_p = p.replace("\\", "/")
                    norm_targets.add(p)
                    norm_targets.add(norm_p)
                    norm_targets.add(os.path.normpath(norm_p).replace("\\", "/"))

                    if os.path.isabs(p):
                        for s in sources:
                            if s and p.startswith(s):
                                try:
                                    from app.core.path_utils import safe_relpath

                                    rel_abs = safe_relpath(p, s).replace("\\", "/")
                                    norm_targets.add(rel_abs)
                                    norm_targets.add(
                                        os.path.normpath(rel_abs).replace("\\", "/")
                                    )
                                    if multi_root:
                                        norm_targets.add(f"{s}::{rel_abs}")
                                        norm_s = os.path.normpath(os.path.abspath(s))
                                        norm_targets.add(f"{norm_s}::{rel_abs}")
                                    for v in (
                                        rel_abs,
                                        os.path.normpath(rel_abs).replace("\\", "/"),
                                    ):
                                        if v and v not in cand_set:
                                            cand_set.add(v)
                                            candidate_filepaths.append(v)
                                except Exception:
                                    pass

                    if multi_root:
                        for s in sources:
                            norm_targets.add(f"{s}::{norm_p}")
                            norm_targets.add(
                                f"{s}::{os.path.normpath(norm_p).replace('\\', '/')}"
                            )
                            norm_s = os.path.normpath(os.path.abspath(s))
                            norm_targets.add(f"{norm_s}::{norm_p}")
                            norm_targets.add(
                                f"{norm_s}::{os.path.normpath(norm_p).replace('\\', '/')}"
                            )

                    for fp_variant in (
                        p,
                        norm_p,
                        os.path.normpath(norm_p).replace("\\", "/"),
                        p.replace("/", "\\"),
                        os.path.normpath(p).replace("/", "\\"),
                    ):
                        if fp_variant and fp_variant not in cand_set:
                            cand_set.add(fp_variant)
                            candidate_filepaths.append(fp_variant)

            if not norm_targets:
                return []

        if not multi_root and base_dir:
            with self._cache_lock:
                if (
                    self._cached_base_dir == base_dir
                    and self._cached_documents is not None
                ):
                    cached = (
                        self._cached_documents.values()
                        if hasattr(self._cached_documents, "values")
                        else self._cached_documents
                    )
                    if norm_targets is not None:
                        return [
                            d
                            for d in cached
                            if d[0] in norm_targets
                            or os.path.normpath(d[0]).replace("\\", "/") in norm_targets
                        ]
                    return list(cached)

            if norm_targets is None:
                self._populate_cache_if_needed(base_dir)
                with self._cache_lock:
                    if (
                        self._cached_base_dir == base_dir
                        and self._cached_documents is not None
                    ):
                        cached = (
                            self._cached_documents.values()
                            if hasattr(self._cached_documents, "values")
                            else self._cached_documents
                        )
                        return list(cached)

        def _row_matches_guard(row):
            if norm_targets is None:
                return True
            doc_base, fp = row[0], row[1]
            rel_p = fp.replace("\\", "/")
            item_key = f"{doc_base}::{rel_p}" if multi_root else rel_p
            if item_key in norm_targets:
                return True
            if os.path.normpath(item_key).replace("\\", "/") in norm_targets:
                return True
            if multi_root:
                norm_doc_base = os.path.normpath(doc_base)
                if f"{norm_doc_base}::{rel_p}" in norm_targets:
                    return True
                if (
                    f"{norm_doc_base}::{os.path.normpath(rel_p).replace('\\', '/')}"
                    in norm_targets
                ):
                    return True
                norm_abs_base = os.path.normpath(os.path.abspath(doc_base))
                if f"{norm_abs_base}::{rel_p}" in norm_targets:
                    return True
                if (
                    f"{norm_abs_base}::{os.path.normpath(rel_p).replace('\\', '/')}"
                    in norm_targets
                ):
                    return True
            else:
                if (
                    rel_p in norm_targets
                    or os.path.normpath(rel_p).replace("\\", "/") in norm_targets
                ):
                    return True
            return False

        rows = []
        conn = get_db_connection(self.db_path)
        with conn:
            if norm_targets is not None:
                CHUNK_SIZE = 500
                seen_row_keys = set()
                placeholders_sources = ",".join(["?"] * len(sources))
                for i in range(0, len(candidate_filepaths), CHUNK_SIZE):
                    chunk = candidate_filepaths[i : i + CHUNK_SIZE]
                    placeholders_fps = ",".join(["?"] * len(chunk))
                    query = (
                        f"SELECT base_dir, filepath, extracted_text, file_hash, user_verified_target_path "
                        f"FROM documents "
                        f"WHERE base_dir IN ({placeholders_sources}) AND filepath IN ({placeholders_fps})"
                    )
                    cursor = conn.execute(query, tuple(sources) + tuple(chunk))
                    for r in cursor.fetchall():
                        rk = (r[0], r[1])
                        if rk not in seen_row_keys:
                            seen_row_keys.add(rk)
                            rows.append(r)
            else:
                placeholders = ",".join(["?"] * len(sources))
                query = (
                    f"SELECT base_dir, filepath, extracted_text, file_hash, user_verified_target_path "
                    f"FROM documents WHERE base_dir IN ({placeholders})"
                )
                cursor = conn.execute(query, tuple(sources))
                rows = cursor.fetchall()

        # In-memory pre-decryption guard: decrypt only rows confirmed to match target criteria
        confirmed_rows = [row for row in rows if _row_matches_guard(row)]

        from app.core.shared_registry import SharedWorkerPool

        def _decrypt_row(row):
            doc_base, fp, text_enc, f_hash, verified_target = row
            decrypted_text = (
                self.crypto.decrypt_text(text_enc) if text_enc is not None else None
            )
            rel_p = fp.replace("\\", "/")
            item_key = f"{doc_base}::{rel_p}" if multi_root else rel_p
            return (item_key, decrypted_text, f_hash, verified_target, doc_base)

        results = []
        if confirmed_rows:
            pool = SharedWorkerPool.get_instance()
            results = list(pool.map(_decrypt_row, confirmed_rows))

        if norm_targets is not None:
            results = [
                r
                for r in results
                if r[0] in norm_targets
                or os.path.normpath(r[0]).replace("\\", "/") in norm_targets
            ]

        return results

    def set_user_verified_target(self, base_dir, file_hash, target_path):
        """Record the historical folder assignment for a specific document hash."""
        precomputed_counts_map = {}
        cached_hit = False
        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents is not None:
                cached_hit = True
                if hasattr(self._cached_documents, "items"):
                    for fp, row in list(self._cached_documents.items()):
                        if row[2] == file_hash:
                            self._cached_documents[fp] = (
                                row[0],
                                row[1],
                                row[2],
                                target_path,
                            )
                            dec_text = row[1]
                            if (
                                target_path
                                and dec_text
                                and self._is_tfidf_eligible(fp, dec_text)
                            ):
                                precomputed_counts_map[fp] = (
                                    self.compute_term_frequencies(dec_text)
                                )
                elif isinstance(self._cached_documents, list):
                    new_docs = []
                    for row in self._cached_documents:
                        if row[2] == file_hash:
                            new_docs.append((row[0], row[1], row[2], target_path))
                            dec_text = row[1]
                            if (
                                target_path
                                and dec_text
                                and self._is_tfidf_eligible(row[0], dec_text)
                            ):
                                precomputed_counts_map[row[0]] = (
                                    self.compute_term_frequencies(dec_text)
                                )
                        else:
                            new_docs.append(row)
                    self._cached_documents = new_docs

        if target_path and not cached_hit:
            try:
                conn_read = get_db_connection(self.db_path)
                cursor = conn_read.execute(
                    "SELECT filepath, extracted_text FROM documents WHERE base_dir = ? AND file_hash = ?",
                    (base_dir, file_hash),
                )
                rows = cursor.fetchall()
                for fp, enc_text in rows:
                    if enc_text:
                        try:
                            dec_text = self.crypto.decrypt_text(enc_text)
                        except Exception:
                            dec_text = None
                        if dec_text and self._is_tfidf_eligible(fp, dec_text):
                            precomputed_counts_map[fp] = self.compute_term_frequencies(
                                dec_text
                            )
            except Exception:
                pass

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
                        conn,
                        base_dir,
                        fp,
                        target_path,
                        term_counts=precomputed_counts_map.get(fp),
                    )

        self.worker.execute_write_async(_write)

    def _drop_stale_destination_row_conn(
        self, conn, base_dir, old_filepath, new_filepath
    ):
        """Delete a leftover row at a move's destination before renaming onto it.

        The file has already been moved there, so any existing row for the
        destination is stale (deleted or moved outside the app). Leaving it
        makes the rename hit the UNIQUE (base_dir, filepath) constraint.
        Nothing is dropped when there is no source row to rename, so a
        replayed or rolled-back update can't delete a row it doesn't replace.
        """
        if new_filepath == old_filepath:
            return
        cursor = conn.execute(
            "SELECT 1 FROM documents WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?) LIMIT 1",
            (base_dir, old_filepath, old_filepath),
        )
        if cursor.fetchone() is None:
            return
        cursor = conn.execute(
            "SELECT 1 FROM documents WHERE base_dir = ? AND filepath = ? LIMIT 1",
            (base_dir, new_filepath),
        )
        if cursor.fetchone() is None:
            return
        cursor = conn.execute(
            "SELECT 1 FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ? LIMIT 1",
            (base_dir, new_filepath),
        )
        if cursor.fetchone() is not None:
            self._update_tfidf_for_document_conn(
                conn, base_dir, new_filepath, None, False
            )
        conn.execute(
            "DELETE FROM documents WHERE base_dir = ? AND filepath = ?",
            (base_dir, new_filepath),
        )

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

        ext_text = None
        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents is not None:
                if (
                    hasattr(self._cached_documents, "pop")
                    and hasattr(self._cached_documents, "__setitem__")
                    and not isinstance(self._cached_documents, list)
                ):
                    row = self._cached_documents.pop(old_filepath, None)
                    if row is not None:
                        ext_text = row[1]
                        self._cached_documents[new_filepath] = (
                            new_filepath,
                            row[1],
                            row[2],
                            new_dir,
                        )
                elif isinstance(self._cached_documents, list):
                    new_docs = []
                    for row in self._cached_documents:
                        if row[0] == old_filepath:
                            ext_text = row[1]
                            new_docs.append((new_filepath, row[1], row[2], new_dir))
                        else:
                            new_docs.append(row)
                    self._cached_documents = new_docs

        precomputed_counts = None
        if new_dir:
            try:
                if not ext_text:
                    doc = self.get_document(base_dir, old_filepath)
                    if doc and doc.get("extracted_text"):
                        ext_text = doc["extracted_text"]
                if ext_text and self._is_tfidf_eligible(new_filepath, ext_text):
                    precomputed_counts = self.compute_term_frequencies(ext_text)
            except Exception:
                pass

        def _write():
            counts = precomputed_counts
            conn = get_db_connection(self.db_path)
            with conn:
                self._drop_stale_destination_row_conn(
                    conn, base_dir, old_filepath, new_filepath
                )
                conn.execute(
                    "UPDATE documents SET filepath = ?, user_verified_target_path = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                    (new_filepath, new_dir, base_dir, old_filepath, old_filepath),
                )
                cursor = conn.execute(
                    "SELECT 1 FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ? LIMIT 1",
                    (base_dir, old_filepath),
                )
                was_in = cursor.fetchone() is not None
                if was_in:
                    cursor = conn.execute(
                        "SELECT extracted_text FROM documents WHERE base_dir = ? AND filepath = ?",
                        (base_dir, new_filepath),
                    )
                    row = cursor.fetchone()
                    decrypted_text = None
                    if row and row[0]:
                        try:
                            decrypted_text = self.crypto.decrypt_text(row[0])
                        except Exception:
                            decrypted_text = None
                    if self._is_tfidf_eligible(new_filepath, decrypted_text):
                        conn.execute(
                            "UPDATE tfidf_doc_terms SET filepath = ? WHERE base_dir = ? AND filepath = ?",
                            (new_filepath, base_dir, old_filepath),
                        )
                    else:
                        self._update_tfidf_for_document_conn(
                            conn, base_dir, old_filepath, None, False
                        )
                else:
                    if new_dir:
                        if counts is None:
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
                                    counts = self.compute_term_frequencies(
                                        decrypted_text
                                    )
                        if counts is not None:
                            self._update_tfidf_for_document_conn(
                                conn,
                                base_dir,
                                new_filepath,
                                None,
                                True,
                                term_counts=counts,
                            )

        self.worker.execute_write_async(_write)

    def get_user_verified_target_path(self, base_dir, filepath) -> Optional[str]:
        """Retrieve the historical user-verified target path for a specific document."""
        filepath = filepath.replace("\\", "/")
        self._populate_cache_if_needed(base_dir)
        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents is not None:
                if hasattr(self._cached_documents, "get") and not isinstance(
                    self._cached_documents, list
                ):
                    row = self._cached_documents.get(
                        filepath
                    ) or self._cached_documents.get(filepath.replace("/", "\\"))
                    if row is not None and len(row) > 3:
                        return row[3]
                elif isinstance(self._cached_documents, list):
                    for row in self._cached_documents:
                        if (
                            row[0] == filepath or row[0].replace("\\", "/") == filepath
                        ) and len(row) > 3:
                            return row[3]

        conn = get_db_connection(self.db_path)
        with conn:
            cur = conn.execute(
                "SELECT user_verified_target_path FROM documents WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                (base_dir, filepath, filepath),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def set_user_verified_target_path(self, base_dir, filepath, target_path):
        """Record the historical folder assignment for a specific document path."""
        filepath = filepath.replace("\\", "/")
        self._populate_cache_if_needed(base_dir)
        with self._cache_lock:
            if self._cached_base_dir == base_dir and self._cached_documents is not None:
                if hasattr(self._cached_documents, "get") and not isinstance(
                    self._cached_documents, list
                ):
                    row = self._cached_documents.get(
                        filepath
                    ) or self._cached_documents.get(filepath.replace("/", "\\"))
                    if row is not None:
                        self._cached_documents[filepath] = (
                            row[0],
                            row[1],
                            row[2],
                            target_path,
                        )
                elif isinstance(self._cached_documents, list):
                    new_docs = []
                    for row in self._cached_documents:
                        if row[0] == filepath or row[0].replace("\\", "/") == filepath:
                            new_docs.append((row[0], row[1], row[2], target_path))
                        else:
                            new_docs.append(row)
                    self._cached_documents = new_docs

        precomputed_counts = None
        if target_path:
            try:
                doc = self.get_document(base_dir, filepath)
                if doc and doc.get("extracted_text"):
                    ext_text = doc["extracted_text"]
                    if self._is_tfidf_eligible(filepath, ext_text):
                        precomputed_counts = self.compute_term_frequencies(ext_text)
            except Exception:
                pass

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    "UPDATE documents SET user_verified_target_path = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                    (target_path, base_dir, filepath, filepath),
                )
                self._update_tfidf_on_verified_target_change(
                    conn,
                    base_dir,
                    filepath,
                    target_path,
                    term_counts=precomputed_counts,
                )

        self.worker.execute_write(_write)

    @staticmethod
    def _is_tfidf_eligible_extension(filepath: str) -> bool:
        if not filepath:
            return False
        fp_lower = filepath.replace("\\", "/").lower()
        if any(fp_lower.endswith(ext) for ext in AUDIO_EXTENSIONS):
            return False
        if not fp_lower.endswith(ELIGIBLE_DOCUMENT_EXTENSIONS):
            return False
        return True

    def _is_tfidf_eligible(self, filepath: str, extracted_text: str | None) -> bool:
        if not self._is_tfidf_eligible_extension(filepath):
            return False
        if not extracted_text:
            return False
        if extracted_text.startswith("[STATUS:"):
            return False
        return True

    def _update_tfidf_on_verified_target_change(
        self,
        conn,
        base_dir,
        filepath,
        target_path,
        term_counts: dict[str, int] | None = None,
    ):
        filepath = filepath.replace("\\", "/")
        if target_path:
            cursor = conn.execute(
                "SELECT 1 FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ? LIMIT 1",
                (base_dir, filepath),
            )
            already_in = cursor.fetchone() is not None
            if not already_in:
                if term_counts is None:
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
                            term_counts = self.compute_term_frequencies(decrypted_text)
                if term_counts is not None:
                    self._update_tfidf_for_document_conn(
                        conn, base_dir, filepath, None, True, term_counts=term_counts
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
        term_counts: dict[str, int] | None = None,
    ):
        orig_filepath = filepath
        filepath = filepath.replace("\\", "/")
        cursor = conn.execute(
            "SELECT term, tf FROM tfidf_doc_terms WHERE base_dir = ? AND (filepath = ? OR filepath = ?)",
            (base_dir, filepath, orig_filepath),
        )
        existing_terms = {row[0]: row[1] for row in cursor.fetchall()}

        if is_added_or_updated:
            if term_counts is None:
                if text and self._is_tfidf_eligible(filepath, text):
                    term_counts = self.compute_term_frequencies(text, stop_words_list)
                else:
                    term_counts = {}

            new_terms = term_counts or {}

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
                conn.executemany(
                    "UPDATE tfidf_vocab SET df = df - 1 WHERE base_dir = ? AND term = ?",
                    [(base_dir, t) for t in terms_to_delete],
                )

            if terms_to_insert:
                conn.executemany(
                    "INSERT INTO tfidf_doc_terms (base_dir, filepath, term, tf) VALUES (?, ?, ?, ?)",
                    terms_to_insert,
                )
                conn.executemany(
                    """
                    INSERT INTO tfidf_vocab (base_dir, term, df)
                    VALUES (?, ?, 1)
                    ON CONFLICT(base_dir, term) DO UPDATE SET df = df + 1
                    """,
                    [(base_dir, doc_row[2]) for doc_row in terms_to_insert],
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
                    "DELETE FROM tfidf_doc_terms WHERE base_dir = ? AND (filepath = ? OR filepath = ?)",
                    (base_dir, filepath, orig_filepath),
                )
                conn.executemany(
                    "UPDATE tfidf_vocab SET df = df - 1 WHERE base_dir = ? AND term = ?",
                    [(base_dir, t) for t in existing_terms],
                )
                conn.executemany(
                    "DELETE FROM tfidf_vocab WHERE base_dir = ? AND term = ? AND df <= 0",
                    [(base_dir, t) for t in existing_terms],
                )

        self._update_tfidf_matrix_cache_conn(conn, base_dir)

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

    def _apply_batch_updates_on_conn(self, conn, updates, precomputed_updates_map=None):
        if precomputed_updates_map is None:
            precomputed_updates_map = {}
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
                    counts = precomputed_updates_map.get((base_dir, fp))
                    self._update_tfidf_on_verified_target_change(
                        conn,
                        base_dir,
                        fp,
                        target_path,
                        term_counts=counts,
                    )
            elif item["type"] == "document_path":
                base_dir, old_filepath, new_filepath = item["args"]
                old_filepath = old_filepath.replace("\\", "/")
                new_filepath = new_filepath.replace("\\", "/")
                import os

                new_dir = os.path.dirname(new_filepath).replace("\\", "/")
                self._drop_stale_destination_row_conn(
                    conn, base_dir, old_filepath, new_filepath
                )
                conn.execute(
                    "UPDATE documents SET filepath = ?, user_verified_target_path = ? WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                    (new_filepath, new_dir, base_dir, old_filepath, old_filepath),
                )
                cursor = conn.execute(
                    "SELECT 1 FROM tfidf_doc_terms WHERE base_dir = ? AND filepath = ? LIMIT 1",
                    (base_dir, old_filepath),
                )
                was_in = cursor.fetchone() is not None
                if was_in:
                    cursor = conn.execute(
                        "SELECT extracted_text FROM documents WHERE base_dir = ? AND filepath = ?",
                        (base_dir, new_filepath),
                    )
                    row = cursor.fetchone()
                    decrypted_text = None
                    if row and row[0]:
                        try:
                            decrypted_text = self.crypto.decrypt_text(row[0])
                        except Exception:
                            decrypted_text = None
                    if self._is_tfidf_eligible(new_filepath, decrypted_text):
                        conn.execute(
                            "UPDATE tfidf_doc_terms SET filepath = ? WHERE base_dir = ? AND filepath = ?",
                            (new_filepath, base_dir, old_filepath),
                        )
                    else:
                        self._update_tfidf_for_document_conn(
                            conn, base_dir, old_filepath, None, False
                        )
                else:
                    if new_dir:
                        counts = precomputed_updates_map.get((base_dir, new_filepath))
                        if counts is None:
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
                                    counts = self.compute_term_frequencies(
                                        decrypted_text
                                    )
                        if counts is not None:
                            self._update_tfidf_for_document_conn(
                                conn,
                                base_dir,
                                new_filepath,
                                None,
                                True,
                                term_counts=counts,
                            )
            elif item["type"] == "transaction_step":
                (
                    session_id,
                    step_number,
                    base_dir,
                    source_path,
                    destination_path,
                    file_hash,
                    item_type,
                    metadata,
                ) = item["args"]
                session_id = str(session_id)
                import time

                conn.execute(
                    """
                    INSERT INTO transaction_ledger (
                        session_id, step_number, base_dir, source_path, destination_path,
                        file_hash, item_type, metadata, timestamp
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        step_number,
                        base_dir,
                        source_path.replace("\\", "/"),
                        destination_path.replace("\\", "/"),
                        file_hash,
                        item_type,
                        metadata,
                        time.time(),
                    ),
                )

    def execute_batch_updates(self, updates):
        """Execute all collected database updates in a single unified database transaction."""
        if not updates:
            return
        self.invalidate_cache()

        precomputed_updates_map = {}
        for item in updates:
            if item["type"] == "verified_target":
                base_dir, file_hash, target_path = item["args"]
                if target_path:
                    try:
                        conn_read = get_db_connection(self.db_path)
                        cursor = conn_read.execute(
                            "SELECT filepath, extracted_text FROM documents WHERE base_dir = ? AND file_hash = ?",
                            (base_dir, file_hash),
                        )
                        rows = cursor.fetchall()
                        for fp, enc_text in rows:
                            if enc_text:
                                try:
                                    dec_text = self.crypto.decrypt_text(enc_text)
                                except Exception:
                                    dec_text = None
                                if dec_text and self._is_tfidf_eligible(fp, dec_text):
                                    precomputed_updates_map[(base_dir, fp)] = (
                                        self.compute_term_frequencies(dec_text)
                                    )
                    except Exception:
                        pass
            elif item["type"] == "document_path":
                base_dir, old_filepath, new_filepath = item["args"]
                old_filepath = old_filepath.replace("\\", "/")
                new_filepath = new_filepath.replace("\\", "/")
                import os

                new_dir = os.path.dirname(new_filepath).replace("\\", "/")
                if new_dir:
                    try:
                        doc = self.get_document(base_dir, old_filepath)
                        if doc and doc.get("extracted_text"):
                            ext_text = doc["extracted_text"]
                            if self._is_tfidf_eligible(new_filepath, ext_text):
                                precomputed_updates_map[(base_dir, new_filepath)] = (
                                    self.compute_term_frequencies(ext_text)
                                )
                    except Exception:
                        pass

        def _write():
            conn = get_db_connection(self.db_path)
            if self.worker and self.worker.is_in_session():
                if (
                    self.worker._active_session
                    and self.worker._active_session.get("conn") is None
                ):
                    self.worker._active_session["conn"] = conn
                self._apply_batch_updates_on_conn(
                    conn, updates, precomputed_updates_map
                )
            else:
                with conn:
                    self._apply_batch_updates_on_conn(
                        conn, updates, precomputed_updates_map
                    )
            self.invalidate_cache()

        self.worker.execute_write(_write)

    def get_transaction_steps(self, session_id: str) -> list:
        """Retrieve recorded transaction steps for a given session, ordered by step_number ASC."""
        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                """
                SELECT id, session_id, step_number, base_dir, source_path, destination_path,
                       file_hash, item_type, metadata, timestamp
                FROM transaction_ledger
                WHERE session_id = ?
                ORDER BY step_number ASC
                """,
                (session_id,),
            )
            rows = cursor.fetchall()
            return [
                {
                    "id": r[0],
                    "session_id": r[1],
                    "step_number": r[2],
                    "base_dir": r[3],
                    "source_path": r[4],
                    "destination_path": r[5],
                    "file_hash": r[6],
                    "item_type": r[7],
                    "metadata": r[8],
                    "timestamp": r[9],
                }
                for r in rows
            ]

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
                    conn.execute(
                        "DELETE FROM tfidf_matrix_cache WHERE base_dir = ?", (base_dir,)
                    )
                    self.matrix_lru_cache.invalidate(base_dir)
                else:
                    conn.execute("DELETE FROM documents")
                    conn.execute("DELETE FROM tfidf_vocab")
                    conn.execute("DELETE FROM tfidf_doc_terms")
                    conn.execute("DELETE FROM tfidf_matrix_cache")
                    self.matrix_lru_cache.invalidate(None)
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

    def get_tfidf_matrix_cache(self, base_dir: str):
        """Retrieve pre-computed TF-IDF term weights directly from the in-memory LRU cache or cache table in under 5ms."""
        if not base_dir:
            return []
        cached = self.matrix_lru_cache.get(base_dir)
        if cached is not None:
            return cached

        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                "SELECT filepath, term, weight FROM tfidf_matrix_cache WHERE base_dir = ?",
                (base_dir,),
            )
            rows = cursor.fetchall()
            if rows:
                self.matrix_lru_cache.put(base_dir, rows)
            return rows

    def update_tfidf_matrix_cache(self, base_dir: str):
        """Recompute and refresh pre-computed TF-IDF matrix weights for a workspace base directory."""

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                self._update_tfidf_matrix_cache_conn(conn, base_dir)

        self.worker.execute_write(_write)

    def _update_tfidf_matrix_cache_conn(self, conn, base_dir: str):
        if not base_dir:
            return
        import math

        cursor = conn.execute(
            "SELECT COUNT(DISTINCT filepath) FROM tfidf_doc_terms WHERE base_dir = ?",
            (base_dir,),
        )
        N = cursor.fetchone()[0] or 0
        conn.execute("DELETE FROM tfidf_matrix_cache WHERE base_dir = ?", (base_dir,))
        self.matrix_lru_cache.invalidate(base_dir)
        if N == 0:
            return

        cursor = conn.execute(
            """
            SELECT t.term, v.df FROM (
                SELECT term, SUM(tf) as total_tf FROM tfidf_doc_terms WHERE base_dir = ? GROUP BY term
            ) t INNER JOIN tfidf_vocab v ON t.term = v.term WHERE v.base_dir = ? ORDER BY t.total_tf DESC LIMIT 1000
            """,
            (base_dir, base_dir),
        )
        top_terms = cursor.fetchall()
        if not top_terms:
            return

        vocab_terms = {term for term, df in top_terms}
        idf_weights = {term: math.log((1 + N) / (1 + df)) + 1 for term, df in top_terms}

        cursor = conn.execute(
            "SELECT filepath, term, tf FROM tfidf_doc_terms WHERE base_dir = ?",
            (base_dir,),
        )
        doc_terms = cursor.fetchall()

        cache_rows = []
        for filepath, term, tf in doc_terms:
            if term in vocab_terms:
                tf_weight = 1.0 + math.log(max(1, tf))
                weight = tf_weight * idf_weights[term]
                cache_rows.append((base_dir, filepath, term, weight))

        if cache_rows:
            conn.executemany(
                "INSERT OR REPLACE INTO tfidf_matrix_cache (base_dir, filepath, term, weight) VALUES (?, ?, ?, ?)",
                cache_rows,
            )
            formatted_rows = [(row[1], row[2], row[3]) for row in cache_rows]
            self.matrix_lru_cache.put(base_dir, formatted_rows)

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
                filepath
                for b_dir, filepath in self.corrupted_vectors
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
                (b_dir, filepath)
                for b_dir, filepath in self.corrupted_vectors
                if not (
                    b_dir.replace("\\", "/").lower() == base_dir_norm.lower()
                    and filepath.replace("\\", "/") in filepaths_norm
                )
            }

    def get_documents_by_filepaths(
        self, base_dir: str, filepaths: list[str]
    ) -> list[tuple[str, str]]:
        """Retrieve filepath and decrypted text for specific files."""
        if not base_dir or not filepaths:
            return []

        conn = get_db_connection(self.db_path)
        results: list[tuple[str, str]] = []
        with conn:
            # Process in small chunks to avoid sqlite query argument limits
            chunk_size = 50
            for i in range(0, len(filepaths), chunk_size):
                chunk = filepaths[i : i + chunk_size]
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
                        self.crypto.decrypt_text(enc_text)
                        if enc_text is not None
                        else ""
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

    def get_document_vectors_batch(
        self, base_dir: str, filepaths: list[str]
    ) -> dict[str, list[float]]:
        """Retrieve decoupled vectors for a list of document filepaths in batched format."""
        if not base_dir or not filepaths:
            return {}

        filepaths_norm = [fp.replace("\\", "/") for fp in filepaths]
        conn = get_db_connection(self.db_path)
        results = {}
        with conn:
            import json

            chunk_size = 50
            for i in range(0, len(filepaths_norm), chunk_size):
                chunk = filepaths_norm[i : i + chunk_size]
                placeholders = ",".join(["?"] * len(chunk))
                cursor = conn.execute(
                    f"""
                    SELECT filepath, vector
                    FROM document_vectors
                    WHERE base_dir = ? AND filepath IN ({placeholders})
                    """,
                    (base_dir, *chunk),
                )
                rows = cursor.fetchall()
                for filepath, enc_vector in rows:
                    if enc_vector:
                        try:
                            decrypted = self.crypto.decrypt_vector(enc_vector)
                            results[filepath] = json.loads(decrypted)
                        except Exception:
                            self.track_corrupted_vector(base_dir, filepath)
        return results

    def upsert_document_vectors(
        self,
        base_dir: str,
        vectors_data: list[tuple[str, list[float]]],
        model_signature: str | None = None,
    ):
        """Upsert document vector embeddings into the decoupled table."""
        if not vectors_data:
            return

        def _write():
            import json

            conn = get_db_connection(self.db_path)
            with conn:
                sig = model_signature
                if sig is None:
                    cursor = conn.execute(
                        "SELECT value FROM model_metadata WHERE key = ?",
                        ("active_model_signature",),
                    )
                    row = cursor.fetchone()
                    if row:
                        sig = row[0]

                rows_to_insert = []
                for filepath, vector in vectors_data:
                    filepath = filepath.replace("\\", "/")
                    vector_str = json.dumps(vector)
                    enc_vector = self.crypto.encrypt_vector(vector_str).decode("utf-8")
                    rows_to_insert.append((base_dir, filepath, enc_vector, sig))
                conn.executemany(
                    """
                    INSERT INTO document_vectors (base_dir, filepath, vector, model_signature)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(base_dir, filepath) DO UPDATE SET
                        vector = excluded.vector,
                        model_signature = excluded.model_signature
                    """,
                    rows_to_insert,
                )
            filepaths_to_clear = [filepath for filepath, _ in vectors_data]
            self.clear_corrupted_vectors(base_dir, filepaths_to_clear)

        self.worker.execute_write(_write)

    def get_document_vectors_count(self) -> int:
        """Return total count of stored document vectors in document_vectors table."""
        try:
            conn = get_db_connection(self.db_path)
            with conn:
                cursor = conn.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='document_vectors'"
                )
                if not cursor.fetchone()[0]:
                    return 0
                cursor = conn.execute("SELECT COUNT(*) FROM document_vectors")
                row = cursor.fetchone()
                return row[0] if row else 0
        except Exception as e:
            logging.debug(f"Error fetching document vectors count: {e}")
            return 0

    def clear_all_document_vectors(self):
        """Delete all document vectors from the decoupled table."""

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                cursor = conn.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='document_vectors'"
                )
                if cursor.fetchone()[0]:
                    conn.execute("DELETE FROM document_vectors")

        with self._corrupted_vectors_lock:
            self.corrupted_vectors.clear()
        if hasattr(self, "matrix_lru_cache") and self.matrix_lru_cache:
            try:
                self.matrix_lru_cache.invalidate(None)
            except Exception:
                pass

        self.worker.execute_write(_write)

    @classmethod
    def scrub_all_vectors(cls):
        """Scrub document vectors across all active database instances and default database."""
        active_instances = list(cls._active_instances)
        scrubbed_paths = set()
        for instance in active_instances:
            try:
                instance.clear_all_document_vectors()
                scrubbed_paths.add(os.path.abspath(instance.db_path))
            except Exception as e:
                logging.debug(
                    f"Failed to clear document vectors on instance {instance}: {e}"
                )

        try:
            from app.config import get_app_dir
            from app.core.services import acquire_services
            from app.core.store import STORE_FILENAME

            if (
                os.path.abspath(str(get_app_dir() / STORE_FILENAME))
                not in scrubbed_paths
            ):
                with acquire_services() as services:
                    services.db.clear_all_document_vectors()
        except Exception as e:
            logging.debug(f"Failed to scrub default database vectors: {e}")

    def get_documents_missing_vectors(
        self,
        base_dir: str,
        limit: int = 50,
        offset: int = 0,
        active_model_signature: str | None = None,
    ) -> list[tuple[str, str]]:
        """Retrieve decrypted documents missing vector embeddings in batched format."""
        conn = get_db_connection(self.db_path)
        with conn:
            sig = active_model_signature
            if sig is None:
                cursor = conn.execute(
                    "SELECT value FROM model_metadata WHERE key = ?",
                    ("active_model_signature",),
                )
                row = cursor.fetchone()
                if row:
                    sig = row[0]

            cursor = conn.execute(
                """
                SELECT d.filepath, d.extracted_text 
                FROM documents d
                LEFT JOIN document_vectors v ON d.base_dir = v.base_dir AND d.filepath = v.filepath
                WHERE d.base_dir = ? AND (v.vector IS NULL OR v.model_signature IS NULL OR v.model_signature != ?)
                LIMIT ? OFFSET ?
                """,
                (base_dir, sig, limit, offset),
            )
            rows = cursor.fetchall()
            results: list[tuple[str, str]] = []
            for filepath, enc_text in rows:
                dec_text = (
                    self.crypto.decrypt_text(enc_text) if enc_text is not None else ""
                )
                results.append((filepath, dec_text))
            return results

    def stage_quarantine_record(
        self,
        job_id: str,
        base_dir: str,
        original_filepath: str,
        staged_filepath: str,
        file_hash: str | None = None,
        policy_action: str | None = None,
    ):
        """Record an incoming document in quarantine staging with status STAGED."""
        import json
        import time

        from app.core.domain_contracts import validate_quarantine_transition

        validate_quarantine_transition(None, "STAGED", job_id=job_id)

        now = time.time()
        audit_log = json.dumps(
            [
                {
                    "timestamp": now,
                    "status": "STAGED",
                    "details": "Initial document placement in quarantine staging",
                }
            ]
        )

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                conn.execute(
                    """
                    INSERT INTO quarantine_records (
                        job_id, base_dir, original_filepath, staged_filepath,
                        file_hash, status, policy_action, audit_log, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'STAGED', ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        base_dir,
                        original_filepath.replace("\\", "/"),
                        staged_filepath.replace("\\", "/"),
                        file_hash,
                        policy_action,
                        audit_log,
                        now,
                        now,
                    ),
                )

        return self.worker.execute_write(_write)

    def update_quarantine_status(
        self,
        job_id: str,
        status: str,
        policy_action: str | None = None,
        error_message: str | None = None,
        audit_entry: dict | str | None = None,
    ):
        """Atomically transition quarantine job state and append structured audit record."""
        import json
        import time

        from app.core.domain_contracts import validate_quarantine_transition
        from app.core.exceptions import EntryNotFoundError

        now = time.time()

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                cursor = conn.execute(
                    "SELECT status, audit_log, policy_action FROM quarantine_records WHERE job_id = ?",
                    (job_id,),
                )
                row = cursor.fetchone()
                if not row:
                    raise EntryNotFoundError(
                        f"Quarantine record not found for job ID '{job_id}'"
                    )

                current_status = row[0]
                validate_quarantine_transition(current_status, status, job_id=job_id)

                current_log = []
                if row[1]:
                    try:
                        current_log = json.loads(row[1])
                    except Exception:
                        current_log = []

                existing_action = row[2]
                act_to_save = (
                    policy_action if policy_action is not None else existing_action
                )

                if audit_entry:
                    if isinstance(audit_entry, dict):
                        entry = dict(audit_entry)
                        if "timestamp" not in entry:
                            entry["timestamp"] = now
                        if "previous_status" not in entry and current_status:
                            entry["previous_status"] = current_status
                        if "status" not in entry:
                            entry["status"] = status
                        current_log.append(entry)
                    else:
                        current_log.append(
                            {
                                "timestamp": now,
                                "status": status,
                                "previous_status": current_status,
                                "details": str(audit_entry),
                            }
                        )
                else:
                    current_log.append(
                        {
                            "timestamp": now,
                            "status": status,
                            "previous_status": current_status,
                            "details": f"Transitioned quarantine state from {current_status} to {status}",
                        }
                    )

                conn.execute(
                    """
                    UPDATE quarantine_records
                    SET status = ?, policy_action = ?, audit_log = ?, updated_at = ?, error_message = ?
                    WHERE job_id = ?
                    """,
                    (
                        status,
                        act_to_save,
                        json.dumps(current_log),
                        now,
                        error_message,
                        job_id,
                    ),
                )

        return self.worker.execute_write(_write)

    def recover_orphaned_quarantine_jobs(
        self, timeout_seconds: float = 300.0
    ) -> list[str]:
        """Detect and reset orphaned inspection jobs exceeding configured timeouts back to STAGED."""
        import json
        import time

        now = time.time()
        cutoff_time = now - timeout_seconds

        def _write():
            conn = get_db_connection(self.db_path)
            with conn:
                cursor = conn.execute(
                    """
                    SELECT job_id, updated_at, audit_log, policy_action
                    FROM quarantine_records
                    WHERE status = 'IN_INSPECTION' AND updated_at <= ?
                    """,
                    (cutoff_time,),
                )
                orphaned_rows = cursor.fetchall()
                recovered_ids = []
                for j_id, upd_at, a_log, pol_act in orphaned_rows:
                    log_entries = []
                    if a_log:
                        try:
                            log_entries = json.loads(a_log)
                        except Exception:
                            log_entries = []
                    log_entries.append(
                        {
                            "timestamp": now,
                            "status": "STAGED",
                            "previous_status": "IN_INSPECTION",
                            "details": (
                                f"Orphaned inspection job reset to STAGED after exceeding "
                                f"timeout of {timeout_seconds}s (elapsed: {now - (upd_at or now):.1f}s)"
                            ),
                        }
                    )
                    conn.execute(
                        """
                        UPDATE quarantine_records
                        SET status = 'STAGED', updated_at = ?, audit_log = ?
                        WHERE job_id = ?
                        """,
                        (now, json.dumps(log_entries), j_id),
                    )
                    recovered_ids.append(j_id)
                return recovered_ids

        return self.worker.execute_write(_write)

    def get_quarantine_record(self, job_id: str) -> dict | None:
        """Retrieve a quarantine staging record by job_id."""
        import json

        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                """
                SELECT job_id, base_dir, original_filepath, staged_filepath, file_hash,
                       status, policy_action, audit_log, created_at, updated_at, error_message
                FROM quarantine_records
                WHERE job_id = ?
                """,
                (job_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            audit = []
            if row[7]:
                try:
                    audit = json.loads(row[7])
                except Exception:
                    audit = []
            return {
                "job_id": row[0],
                "base_dir": row[1],
                "original_filepath": row[2],
                "staged_filepath": row[3],
                "file_hash": row[4],
                "status": row[5],
                "policy_action": row[6],
                "audit_log": audit,
                "created_at": row[8],
                "updated_at": row[9],
                "error_message": row[10],
            }

    def get_quarantine_records_by_base_dir(
        self, base_dir: str, status: str | None = None
    ) -> list[dict]:
        """Retrieve quarantine staging records for a base directory."""
        import json

        if not base_dir:
            all_recs = self.get_all_quarantine_records()
            return (
                [
                    r
                    for r in all_recs
                    if (r.get("status") or "").upper() == status.upper()
                ]
                if status
                else all_recs
            )

        conn = get_db_connection(self.db_path)
        with conn:
            b_norm = os.path.normpath(base_dir)
            b_slash = base_dir.replace("\\", "/")
            b_backslash = base_dir.replace("/", "\\")
            try:
                b_abs = os.path.normpath(os.path.abspath(base_dir))
                b_abs_slash = b_abs.replace("\\", "/")
                b_abs_backslash = b_abs.replace("/", "\\")
            except Exception:
                b_abs = b_norm
                b_abs_slash = b_slash
                b_abs_backslash = b_backslash

            candidates = [
                base_dir,
                b_norm,
                b_slash,
                b_backslash,
                b_abs,
                b_abs_slash,
                b_abs_backslash,
            ]

            if status:
                cursor = conn.execute(
                    """
                    SELECT job_id, base_dir, original_filepath, staged_filepath, file_hash,
                           status, policy_action, audit_log, created_at, updated_at, error_message
                    FROM quarantine_records
                    WHERE (
                        LOWER(base_dir) = LOWER(?) OR
                        LOWER(base_dir) = LOWER(?) OR
                        LOWER(base_dir) = LOWER(?) OR
                        LOWER(base_dir) = LOWER(?) OR
                        LOWER(base_dir) = LOWER(?) OR
                        LOWER(base_dir) = LOWER(?) OR
                        LOWER(base_dir) = LOWER(?)
                    ) AND (status = ? OR UPPER(status) = UPPER(?))
                    ORDER BY created_at ASC
                    """,
                    (*candidates, status, status),
                )

            else:
                cursor = conn.execute(
                    """
                    SELECT job_id, base_dir, original_filepath, staged_filepath, file_hash,
                           status, policy_action, audit_log, created_at, updated_at, error_message
                    FROM quarantine_records
                    WHERE (
                        LOWER(base_dir) = LOWER(?) OR
                        LOWER(base_dir) = LOWER(?) OR
                        LOWER(base_dir) = LOWER(?) OR
                        LOWER(base_dir) = LOWER(?) OR
                        LOWER(base_dir) = LOWER(?) OR
                        LOWER(base_dir) = LOWER(?) OR
                        LOWER(base_dir) = LOWER(?)
                    )
                    ORDER BY created_at ASC
                    """,
                    candidates,
                )
            rows = cursor.fetchall()
            results = []
            for row in rows:
                audit = []
                if row[7]:
                    try:
                        audit = json.loads(row[7])
                    except Exception:
                        audit = []
                results.append(
                    {
                        "job_id": row[0],
                        "base_dir": row[1],
                        "original_filepath": row[2],
                        "staged_filepath": row[3],
                        "file_hash": row[4],
                        "status": row[5],
                        "policy_action": row[6],
                        "audit_log": audit,
                        "created_at": row[8],
                        "updated_at": row[9],
                        "error_message": row[10],
                    }
                )

            if not results:
                # Fallback matching for unusual or relative path representations
                try:
                    target_norm = os.path.normcase(os.path.abspath(base_dir)).rstrip(
                        "\\/"
                    )
                    for r in self.get_all_quarantine_records():
                        rec_base = r.get("base_dir")
                        if rec_base:
                            try:
                                rec_norm = os.path.normcase(
                                    os.path.abspath(rec_base)
                                ).rstrip("\\/")
                                if rec_norm == target_norm:
                                    if status is None or r["status"] == status:
                                        results.append(r)
                            except Exception:
                                pass
                except Exception:
                    pass

            return results

    def get_all_quarantine_records(self) -> list[dict]:
        """Retrieve all quarantine staging records across all sessions."""
        import json

        conn = get_db_connection(self.db_path)
        with conn:
            cursor = conn.execute(
                """
                SELECT job_id, base_dir, original_filepath, staged_filepath, file_hash,
                       status, policy_action, audit_log, created_at, updated_at, error_message
                FROM quarantine_records
                ORDER BY created_at ASC
                """
            )
            rows = cursor.fetchall()
            results = []
            for row in rows:
                audit = []
                if row[7]:
                    try:
                        audit = json.loads(row[7])
                    except Exception:
                        audit = []
                results.append(
                    {
                        "job_id": row[0],
                        "base_dir": row[1],
                        "original_filepath": row[2],
                        "staged_filepath": row[3],
                        "file_hash": row[4],
                        "status": row[5],
                        "policy_action": row[6],
                        "audit_log": audit,
                        "created_at": row[8],
                        "updated_at": row[9],
                        "error_message": row[10],
                    }
                )
            return results
