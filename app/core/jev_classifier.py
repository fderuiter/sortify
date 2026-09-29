"""Jev Non-Generative Fast-Path Classifier Engine.

Provides ultra-fast, local, deterministic document classification, sensitivity rating,
and archival prioritization within a sub-150 ms SLA to bypass heavy generative AI processing.
"""

import json
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

from app.core.cache import BoundedMemoryCache

logger = logging.getLogger(__name__)


class JevClassificationResult(BaseModel):
    """Typed Pydantic schema for Jev non-generative classification results."""

    category: str = Field(default="Unclassified")
    sensitivity_rating: str = Field(default="LOW")
    sensitivity_score: float = Field(default=0.0)
    archival_priority: int = Field(default=3)
    archival_priority_score: float = Field(default=0.0)
    confidence: float = Field(default=0.0)
    is_classified: bool = Field(default=False)
    metadata: Dict[str, Any] = Field(default_factory=dict)

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def model_dump(self, *args, **kwargs) -> Dict[str, Any]:
        """Dump the result as a standard dictionary."""
        d = super().model_dump(*args, **kwargs)
        return d

    def dict(self, *args, **kwargs) -> Dict[str, Any]:
        """Backward compatibility method for dict serialization."""
        return self.model_dump(*args, **kwargs)

    def __getitem__(self, item: str) -> Any:
        """Support bracket access for dictionary compatibility."""
        if hasattr(self, item):
            return getattr(self, item)
        extra = getattr(self, "__pydantic_extra__", None)
        if extra and item in extra:
            return extra[item]
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        """Support dictionary get method."""
        try:
            return self[item]
        except KeyError:
            return default

    def __contains__(self, item: str) -> bool:
        """Support membership check for dictionary compatibility."""
        return hasattr(self, item) or (
            getattr(self, "__pydantic_extra__", None) is not None
            and item in self.__pydantic_extra__
        )


class JevClassifierEngine:
    """Thread-safe, non-generative document classification engine."""

    CATEGORY_RULES = {
        "Financial": {
            "keywords": [
                "invoice", "receipt", "tax", "statement", "w2", "financial",
                "payroll", "accounting", "balance", "ledger", "audit",
                "billing", "expense", "revenue", "invoice_", "receipt_"
            ],
            "extensions": [".csv", ".xlsx", ".xls"],
            "sensitivity_rating": "HIGH",
            "sensitivity_score": 0.85,
            "archival_priority": 1,
            "archival_priority_score": 0.90,
            "target_category": "Financial Reports",
        },
        "Legal": {
            "keywords": [
                "contract", "nda", "agreement", "patent", "compliance",
                "legal", "terms", "license", "privacy", "lawsuit", "court",
                "bylaws", "affidavit", "subpoena"
            ],
            "extensions": [".pdf", ".docx"],
            "sensitivity_rating": "HIGH",
            "sensitivity_score": 0.80,
            "archival_priority": 1,
            "archival_priority_score": 0.85,
            "target_category": "Legal Documents",
        },
        "Medical": {
            "keywords": [
                "patient", "clinical", "medical", "lab_report", "prescription",
                "health", "doctor", "hipaa", "diagnosis", "hospital", "pharma",
                "pathology", "radiology", "blood_work"
            ],
            "extensions": [],
            "sensitivity_rating": "CRITICAL",
            "sensitivity_score": 0.95,
            "archival_priority": 1,
            "archival_priority_score": 0.95,
            "target_category": "Medical & Clinical Records",
        },
        "Personal": {
            "keywords": [
                "passport", "id_card", "driver_license", "resume", "cv",
                "ssn", "tax_return", "personal", "birth_certificate"
            ],
            "extensions": [],
            "sensitivity_rating": "HIGH",
            "sensitivity_score": 0.90,
            "archival_priority": 2,
            "archival_priority_score": 0.70,
            "target_category": "Personal Records",
        },
        "Technical": {
            "keywords": [
                "log", "config", "spec", "build", "code", "readme",
                "script", "source", "repo", "database", "schema", "api_spec"
            ],
            "extensions": [".py", ".js", ".json", ".yaml", ".yml", ".xml", ".log", ".sql", ".sh", ".bat"],
            "sensitivity_rating": "LOW",
            "sensitivity_score": 0.20,
            "archival_priority": 4,
            "archival_priority_score": 0.30,
            "target_category": "Technical & Data Assets",
        },
        "Administrative": {
            "keywords": [
                "memo", "agenda", "minutes", "report", "presentation",
                "deck", "slides", "meeting", "notice", "schedule", "plan"
            ],
            "extensions": [".pptx", ".ppt", ".key"],
            "sensitivity_rating": "MEDIUM",
            "sensitivity_score": 0.50,
            "archival_priority": 3,
            "archival_priority_score": 0.50,
            "target_category": "Administrative Records",
        },
    }

    def __init__(
        self,
        confidence_threshold: float = 0.5,
        db_path: Optional[str] = None,
        worker: Any = None,
        max_cache_size: int = 1000,
    ):
        """Initialize JevClassifierEngine with confidence threshold and two-tier caching."""
        self.confidence_threshold = confidence_threshold
        self.db_path = db_path
        self.worker = worker
        self.memory_cache = BoundedMemoryCache[Tuple[str, float, int], JevClassificationResult](
            max_size=max_cache_size
        )
        if self.db_path:
            self._init_db()

    def set_database(self, db_path: str, worker: Any = None) -> None:
        """Bind or update the persistent SQLite database and worker for caching."""
        self.db_path = db_path
        if worker is not None:
            self.worker = worker
        self._init_db()

    def _init_db(self) -> None:
        """Ensure the jev_classification_cache table exists in the database."""
        if not self.db_path:
            return
        try:
            from app.core.db_conn import get_db_connection

            conn = get_db_connection(self.db_path)
            with conn:
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
        except Exception as e:
            logger.warning(f"Failed to initialize jev_classification_cache DB table: {e}")

    def _get_file_stat_key(self, file_path: str) -> Optional[Tuple[str, float, int]]:
        """Calculate a fast file stat key (abs_path, mtime, size) before inspecting file contents."""
        try:
            if not file_path or not os.path.exists(file_path) or not os.path.isfile(file_path):
                return None
            abs_path = os.path.abspath(file_path).replace("\\", "/")
            stat = os.stat(file_path)
            return (abs_path, float(stat.st_mtime), int(stat.st_size))
        except Exception as e:
            logger.debug(f"Could not calculate stat key for {file_path}: {e}")
            return None

    def invalidate(self, file_path: str) -> None:
        """Invalidate both in-memory and persistent database cache entries for a file."""
        try:
            abs_path = os.path.abspath(file_path).replace("\\", "/")
            keys_to_purge = [
                k for k in self.memory_cache.keys()
                if (isinstance(k, tuple) and k[0] == abs_path) or k == abs_path
            ]
            for k in keys_to_purge:
                self.memory_cache.invalidate(k)

            if self.db_path:
                def _write_delete():
                    from app.core.db_conn import get_db_connection

                    conn = get_db_connection(self.db_path)
                    with conn:
                        conn.execute(
                            "DELETE FROM jev_classification_cache WHERE file_path = ?",
                            (abs_path,),
                        )

                if self.worker and hasattr(self.worker, "execute_write_async"):
                    self.worker.execute_write_async(_write_delete)
                else:
                    _write_delete()
        except Exception as e:
            logger.warning(f"Failed to invalidate cache for {file_path}: {e}")

    def _get_from_db(self, stat_key: Tuple[str, float, int]) -> Optional[JevClassificationResult]:
        """Query persistent SQLite database table for a cached result matching file path, mtime, and size."""
        if not self.db_path:
            return None
        abs_path, mtime, size = stat_key
        try:
            from app.core.db_conn import get_db_connection

            conn = get_db_connection(self.db_path)
            with conn:
                cursor = conn.execute(
                    """SELECT category, sensitivity_rating, sensitivity_score,
                              archival_priority, archival_priority_score, confidence,
                              is_classified, metadata
                       FROM jev_classification_cache
                       WHERE file_path = ? AND mtime = ? AND size = ?""",
                    (abs_path, mtime, size),
                )
                row = cursor.fetchone()

            if row:
                category, sens_rat, sens_score, arch_prio, arch_score, conf, is_class, meta_json = row
                try:
                    metadata = json.loads(meta_json) if meta_json else {}
                except Exception:
                    metadata = {}
                return JevClassificationResult(
                    category=category,
                    sensitivity_rating=sens_rat,
                    sensitivity_score=sens_score,
                    archival_priority=arch_prio,
                    archival_priority_score=arch_score,
                    confidence=conf,
                    is_classified=bool(is_class),
                    metadata=metadata,
                )
            else:
                # Check if there is a stale entry for abs_path with old mtime/size
                with conn:
                    cursor = conn.execute(
                        "SELECT 1 FROM jev_classification_cache WHERE file_path = ? LIMIT 1",
                        (abs_path,),
                    )
                    if cursor.fetchone():
                        self.invalidate(abs_path)
        except Exception as e:
            logger.warning(f"Database cache query failed for {abs_path}: {e}")
        return None

    def _save_to_db(self, stat_key: Tuple[str, float, int], result: JevClassificationResult) -> None:
        """Persist a classification result into SQLite database via DBWorker."""
        if not self.db_path:
            return
        abs_path, mtime, size = stat_key
        try:
            meta_str = json.dumps(result.metadata or {})
            now = time.time()

            def _write_upsert():
                from app.core.db_conn import get_db_connection

                conn = get_db_connection(self.db_path)
                with conn:
                    conn.execute(
                        """INSERT INTO jev_classification_cache (
                            file_path, mtime, size, category, sensitivity_rating,
                            sensitivity_score, archival_priority, archival_priority_score,
                            confidence, is_classified, metadata, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(file_path) DO UPDATE SET
                            mtime = excluded.mtime,
                            size = excluded.size,
                            category = excluded.category,
                            sensitivity_rating = excluded.sensitivity_rating,
                            sensitivity_score = excluded.sensitivity_score,
                            archival_priority = excluded.archival_priority,
                            archival_priority_score = excluded.archival_priority_score,
                            confidence = excluded.confidence,
                            is_classified = excluded.is_classified,
                            metadata = excluded.metadata,
                            updated_at = excluded.updated_at
                        """,
                        (
                            abs_path,
                            mtime,
                            size,
                            result.category,
                            result.sensitivity_rating,
                            result.sensitivity_score,
                            result.archival_priority,
                            result.archival_priority_score,
                            result.confidence,
                            1 if result.is_classified else 0,
                            meta_str,
                            now,
                        ),
                    )

            if self.worker and hasattr(self.worker, "execute_write_async"):
                self.worker.execute_write_async(_write_upsert)
            else:
                _write_upsert()
        except Exception as e:
            logger.warning(f"Failed to persist classification result for {abs_path}: {e}")

    def classify(
        self, file_path: str, text_content: Optional[str] = None
    ) -> JevClassificationResult:
        """Classify a file using deterministic non-generative fast-path rules with two-tier caching.

        Executes locally within sub-150 ms SLA (<10 ms for cached hits).
        """
        start_time = time.perf_counter()
        try:
            if not file_path:
                return JevClassificationResult()

            stat_key = self._get_file_stat_key(file_path)

            # Tier 1: In-memory LRU Cache Check
            if stat_key is not None:
                mem_res = self.memory_cache.get(stat_key)
                if mem_res is not None:
                    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                    if isinstance(mem_res.metadata, dict):
                        mem_res.metadata["latency_ms"] = elapsed_ms
                    return mem_res

                # Tier 2: Persistent SQLite DB Cache Check
                db_res = self._get_from_db(stat_key)
                if db_res is not None:
                    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                    if isinstance(db_res.metadata, dict):
                        db_res.metadata["latency_ms"] = elapsed_ms
                    self.memory_cache.set(stat_key, db_res)
                    return db_res

            fn_lower = os.path.basename(file_path).lower()
            ext = os.path.splitext(file_path)[1].lower()

            # Fast snippet read if text content not provided
            snippet = ""
            if text_content is not None:
                snippet = str(text_content)[:4096].lower()
            elif os.path.exists(file_path) and os.path.isfile(file_path):
                # Read at most 4KB snippet if readable file and size < 10MB
                try:
                    if os.path.getsize(file_path) <= 10 * 1024 * 1024 and ext in {
                        ".txt", ".csv", ".json", ".xml", ".yaml", ".yml",
                        ".log", ".md", ".py", ".js", ".html", ".htm"
                    }:
                        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                            snippet = f.read(4096).lower()
                except Exception:
                    pass

            text_to_check = f"{fn_lower} {snippet}"

            best_match = None
            best_score = 0.0

            for cat_name, rules in self.CATEGORY_RULES.items():
                match_score = 0.0
                # Check keyword matches
                kw_matches = sum(1 for kw in rules["keywords"] if kw in text_to_check)
                if kw_matches > 0:
                    match_score += min(0.6 + (kw_matches - 1) * 0.15, 0.95)

                # Check extension matches
                if ext in rules["extensions"]:
                    match_score += 0.35

                if match_score > best_score:
                    best_score = min(match_score, 1.0)
                    best_match = (cat_name, rules)

            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            if best_match and best_score >= self.confidence_threshold:
                cat_name, rules = best_match
                target_cat = rules.get("target_category", cat_name)
                result = JevClassificationResult(
                    category=target_cat,
                    sensitivity_rating=rules["sensitivity_rating"],
                    sensitivity_score=rules["sensitivity_score"],
                    archival_priority=rules["archival_priority"],
                    archival_priority_score=rules["archival_priority_score"],
                    confidence=best_score,
                    is_classified=True,
                    metadata={
                        "raw_category": cat_name,
                        "latency_ms": elapsed_ms,
                        "file_path": file_path,
                    },
                )
            else:
                result = JevClassificationResult(
                    category="Unclassified",
                    sensitivity_rating="LOW",
                    sensitivity_score=0.0,
                    archival_priority=5,
                    archival_priority_score=0.0,
                    confidence=best_score,
                    is_classified=False,
                    metadata={
                        "latency_ms": elapsed_ms,
                        "file_path": file_path,
                    },
                )

            # Store in Tier 1 (Memory) and Tier 2 (SQLite DB)
            if stat_key is not None:
                self.memory_cache.set(stat_key, result)
                self._save_to_db(stat_key, result)

            return result

        except Exception as e:
            try:
                path_str = str(file_path)
            except Exception:
                path_str = "<unprintable file_path>"
            logger.warning(
                f"Jev classification exception for {path_str}: {e}. Returning unclassified fallback."
            )
            return JevClassificationResult(
                category="Unclassified",
                confidence=0.0,
                is_classified=False,
                metadata={"error": str(e)},
            )

    def classify_file(
        self, file_path: str, text_content: Optional[str] = None
    ) -> JevClassificationResult:
        """Alias for classify method for API consistency."""
        return self.classify(file_path, text_content=text_content)
