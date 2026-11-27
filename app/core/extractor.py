"""Document extraction and processing module.

This module provides utilities to read text from various file formats.
"""

import concurrent.futures
import inspect
import logging
import os
import threading
from collections.abc import Callable
from enum import StrEnum
from typing import Any

import pypdf.errors
from pydantic import BaseModel, ConfigDict

from app.core.extractor_strategies import registry
from app.core.path_utils import safe_relpath
from app.core.progress import emit_progress
from app.core.text_utils import sanitize_text

logger = logging.getLogger(__name__)


class ExtractionStatus(StrEnum):
    """Enumeration of document extraction statuses."""

    SUCCESS = "SUCCESS"
    EMPTY = "EMPTY"
    UNSUPPORTED = "UNSUPPORTED"
    ENCRYPTED = "ENCRYPTED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"
    ERROR = "ERROR"
    PROVISIONAL = "PROVISIONAL"


class ExtractionResult(BaseModel):
    """Structured result model for document text extraction."""

    text: str = ""
    status: ExtractionStatus = ExtractionStatus.SUCCESS
    error_message: str | None = None
    truncated: bool = False
    truncation_reason: str | None = None
    truncated_bounds: dict[str, Any] | None = None

    model_config = ConfigDict(extra="allow")

    def dict(self, *args, **kwargs) -> dict[str, Any]:
        """Backward compatibility method for legacy Pydantic v1 callers."""
        return self.model_dump(*args, **kwargs)

    def __str__(self) -> str:
        """Return raw text if successful, or string status marker if non-successful."""
        if self.status == ExtractionStatus.SUCCESS:
            return self.text
        if self.error_message and self.status == ExtractionStatus.ERROR:
            return f"[STATUS:{self.error_message}]"
        return f"[STATUS:{self.status.value}]"

    def __len__(self) -> int:
        """Return length of string representation."""
        return len(str(self))

    def __contains__(self, item: Any) -> bool:
        """Check substring or attribute membership."""
        if isinstance(item, str):
            extra = getattr(self, "__pydantic_extra__", None)
            if hasattr(self, item) or (extra is not None and item in extra):
                return True
            return item in str(self)
        return False

    def __eq__(self, other: Any) -> bool:
        """Evaluate equality against ExtractionResult or string representation."""
        if isinstance(other, ExtractionResult):
            return self.text == other.text and self.status == other.status
        if isinstance(other, str):
            return str(self) == other or (
                self.status != ExtractionStatus.SUCCESS
                and other == f"[STATUS:{self.status.value}]"
            )
        return False

    def __getitem__(self, item: str) -> Any:
        """Support item lookup via bracket syntax for dictionary compatibility."""
        if item == "text":
            return self.text
        if item == "status":
            return self.status
        if item == "error_message":
            return self.error_message
        if item == "truncated":
            return self.truncated
        if item == "truncation_reason":
            return self.truncation_reason
        if item == "truncated_bounds":
            return self.truncated_bounds
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

    def startswith(self, prefix: str, *args, **kwargs) -> bool:
        """Check if string representation starts with prefix."""
        return str(self).startswith(prefix, *args, **kwargs)

    def __bool__(self) -> bool:
        """Evaluate truthiness based on text presence or success status."""
        return bool(self.text) or self.status == ExtractionStatus.SUCCESS


def _get_notification_manager() -> Any:
    """Retrieve the central NotificationManager instance dynamically without architectural layer violations."""
    try:
        import importlib

        mod = importlib.import_module("app.ui.notifications")
        return getattr(mod, "NotificationManager").get_instance()
    except (ImportError, AttributeError):
        return None


def __getattr__(name: str) -> Any:
    """Lazy module attribute resolver for NotificationManager to avoid layer violations."""
    if name == "NotificationManager":
        import importlib

        mod = importlib.import_module("app.ui.notifications")
        return getattr(mod, "NotificationManager")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def get_file_hash(file_path: str) -> str:
    """Calculate the SHA-256 hash of a file.

    For MP3 and M4A files, skips metadata headers and structural atoms to isolate
    the raw audio payload. For Word (.docx) and Excel (.xlsx) files, skips metadata
    stream headers (core document properties) to ensure stable payload digests.
    """
    from app.core.resilient_file_ops import resilient_file_hash

    return resilient_file_hash(file_path, skip_media_tags=True)


def evaluate_text_confidence(text: str, ext: str) -> float:
    """Evaluate extraction confidence for fast-path triage decisions.

    Returns a float score in [0.0, 1.0]. Scores >= 0.8 represent high confidence,
    allowing fast-path triage to bypass heavy models (Florence-2, EasyOCR, GGUF).
    Scores < 0.8 escalate documents to asynchronous background worker queues.
    """
    ext = ext.lower()
    if ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"):
        return 0.0

    if not text or not text.strip():
        return 0.0

    if text.startswith("[STATUS:"):
        return 0.0

    # Native text and tabular formats
    if ext in (
        ".txt",
        ".csv",
        ".md",
        ".json",
        ".log",
        ".tsv",
        ".xml",
        ".html",
        ".htm",
        ".rst",
        ".yaml",
        ".yml",
    ):
        stripped = text.strip()
        if len(stripped) > 0:
            return 1.0
        return 0.0

    # Complex document formats (PDF, DOCX, etc.)
    stripped = text.strip()
    words = [w for w in stripped.split() if any(c.isalnum() for c in w)]
    if len(words) < 3:
        return 0.3

    alphanumeric_chars = sum(1 for c in stripped if c.isalnum() or c.isspace())
    total_chars = len(stripped)
    ratio = alphanumeric_chars / max(1, total_chars)

    if ratio >= 0.75 and len(words) >= 3:
        return 0.9
    return 0.4


def extract_file_text(
    file_path: str,
    settings=None,
    progress_callback=None,
    cancel_check=None,
    fast_triage: bool = False,
    db=None,
    base_dir: str | None = None,
) -> ExtractionResult:
    """Extract text content from a given file."""
    ext = os.path.splitext(file_path)[1].lower()

    if fast_triage and ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp"):
        if db and hasattr(db, "worker") and db.worker:

            def _bg_visual_job():
                full_text = str(
                    extract_file_text(file_path, settings=settings, fast_triage=False)
                )
                if base_dir:
                    rel_path = safe_relpath(file_path, base_dir).replace("\\", "/")
                    f_hash = get_file_hash(file_path)
                    db.upsert_document(base_dir, rel_path, f_hash, full_text)
                    db.update_tfidf_matrix_cache(base_dir)

            db.worker.submit_background_job(_bg_visual_job)
        return ExtractionResult(text="", status=ExtractionStatus.PROVISIONAL)

    try:
        extractor = registry.get_extractor(ext)
        if extractor:
            # Check the signature of extractor.extract to safely pass new args
            sig = inspect.signature(extractor.extract)
            kwargs = {}
            if "settings" in sig.parameters:
                kwargs["settings"] = settings
            if "progress_callback" in sig.parameters:
                kwargs["progress_callback"] = progress_callback
            if "cancel_check" in sig.parameters:
                kwargs["cancel_check"] = cancel_check

            raw_text = extractor.extract(file_path, **kwargs)
            text = sanitize_text(raw_text)

            truncated = bool(getattr(extractor, "truncated", False))
            truncation_reason = getattr(extractor, "truncation_reason", None)
            truncated_bounds = getattr(extractor, "truncated_bounds", None)

            if isinstance(raw_text, ExtractionResult):
                truncated = bool(raw_text.truncated or truncated)
                truncation_reason = raw_text.truncation_reason or truncation_reason
                truncated_bounds = raw_text.truncated_bounds or truncated_bounds

            if truncated:
                logger.warning(
                    f"Tabular extraction truncated for {file_path}: reason={truncation_reason}, bounds={truncated_bounds}",
                    extra={
                        "file_path": file_path,
                        "truncation_reason": truncation_reason,
                        "truncated_bounds": truncated_bounds,
                    },
                )
                try:
                    notif_mgr = _get_notification_manager()
                    if notif_mgr is not None:
                        notif_mgr.notify(
                            message=f"Tabular extraction reached boundary and truncated {os.path.basename(file_path)} ({truncation_reason})",
                            type="warning",
                            caption="Spreadsheet Truncation",
                            file_path=file_path,
                            truncation_reason=truncation_reason,
                            truncated_bounds=truncated_bounds,
                        )
                except Exception as notif_err:
                    logger.debug(
                        f"Failed to dispatch truncation notification: {notif_err}",
                        exc_info=True,
                    )

            if fast_triage:
                confidence = evaluate_text_confidence(text, ext)
                if confidence < 0.8:
                    if db and hasattr(db, "worker") and db.worker:

                        def _bg_escalation_job():
                            full_text = str(extractor.extract(file_path, **kwargs))
                            if base_dir:
                                rel_path = safe_relpath(file_path, base_dir).replace(
                                    "\\", "/"
                                )
                                f_hash = get_file_hash(file_path)
                                db.upsert_document(
                                    base_dir, rel_path, f_hash, full_text
                                )
                                db.update_tfidf_matrix_cache(base_dir)

                        db.worker.submit_background_job(_bg_escalation_job)
                    return ExtractionResult(
                        text="",
                        status=ExtractionStatus.PROVISIONAL,
                        truncated=truncated,
                        truncation_reason=truncation_reason,
                        truncated_bounds=truncated_bounds,
                    )

            if not text.strip():
                return ExtractionResult(
                    text="",
                    status=ExtractionStatus.EMPTY,
                    truncated=truncated,
                    truncation_reason=truncation_reason,
                    truncated_bounds=truncated_bounds,
                )

            if text.startswith("[STATUS:"):
                tag = text[8:-1] if text.endswith("]") else text[8:]
                status_enum = ExtractionStatus.FAILED
                if tag == "EMPTY":
                    status_enum = ExtractionStatus.EMPTY
                elif tag == "UNSUPPORTED":
                    status_enum = ExtractionStatus.UNSUPPORTED
                elif tag == "ENCRYPTED":
                    status_enum = ExtractionStatus.ENCRYPTED
                elif tag == "TIMEOUT":
                    status_enum = ExtractionStatus.TIMEOUT
                elif tag == "SKIPPED":
                    status_enum = ExtractionStatus.SKIPPED
                elif tag == "CANCELLED":
                    status_enum = ExtractionStatus.CANCELLED
                elif tag == "PROVISIONAL":
                    status_enum = ExtractionStatus.PROVISIONAL
                elif tag.startswith("ERROR"):
                    status_enum = ExtractionStatus.ERROR
                return ExtractionResult(
                    text="",
                    status=status_enum,
                    error_message=tag
                    if status_enum == ExtractionStatus.ERROR
                    else None,
                    truncated=truncated,
                    truncation_reason=truncation_reason,
                    truncated_bounds=truncated_bounds,
                )

            return ExtractionResult(
                text=text,
                status=ExtractionStatus.SUCCESS,
                truncated=truncated,
                truncation_reason=truncation_reason,
                truncated_bounds=truncated_bounds,
            )
        else:
            return ExtractionResult(text="", status=ExtractionStatus.UNSUPPORTED)
    except pypdf.errors.FileNotDecryptedError:
        return ExtractionResult(text="", status=ExtractionStatus.ENCRYPTED)
    except Exception as e:
        logging.error(
            f"Failed to extract text from {file_path}. Error: {str(e)}", exc_info=True
        )
        return ExtractionResult(
            text="", status=ExtractionStatus.FAILED, error_message=str(e)
        )


def _read_preview_text_offline(
    file_path: str,
    settings: Any = None,
    db: Any = None,
    base_dir: str | None = None,
) -> tuple[str, str]:
    """Read document text for UI previews using DB cache first and offline local fallback.

    Enforces zero outbound network calls and never mutates database tables.
    Returns a ``(raw_text, source_kind)`` tuple where ``source_kind`` is one of
    ``'cache'``, ``'local'``, ``'provisional'``, or ``'missing'``.
    """
    from app.core.network_guard import block_external_network

    if not file_path:
        return "", "missing"

    with block_external_network("inline document preview"):
        abs_path = os.path.abspath(file_path)

        # 1. Prefer cached decrypted document text from Database.get_document
        if db is not None and hasattr(db, "get_document"):
            lookup_pairs: list[tuple[str, str]] = []
            if base_dir:
                abs_base = os.path.abspath(base_dir)
                rel_p = safe_relpath(abs_path, abs_base)
                for b_cand in (base_dir, abs_base):
                    lookup_pairs.append((b_cand, rel_p))
                    lookup_pairs.append((b_cand, rel_p.replace("\\", "/")))
                    lookup_pairs.append((b_cand, os.path.basename(abs_path)))
            parent_dir = os.path.dirname(abs_path)
            if parent_dir:
                lookup_pairs.append((parent_dir, os.path.basename(abs_path)))

            seen_pairs: set[tuple[str, str]] = set()
            for b_cand, r_cand in lookup_pairs:
                if not b_cand or not r_cand or (b_cand, r_cand) in seen_pairs:
                    continue
                seen_pairs.add((b_cand, r_cand))
                try:
                    cached = db.get_document(b_cand, r_cand)
                except (OSError, ValueError, RuntimeError, AttributeError, TypeError):
                    cached = None
                if (
                    isinstance(cached, dict)
                    and cached.get("extracted_text") is not None
                ):
                    cached_text = str(cached["extracted_text"])
                    if cached_text and not cached_text.startswith("[STATUS:"):
                        return cached_text, "cache"

        # 2. Local offline extraction fallback without heavy ML model downloads
        if not os.path.isfile(abs_path):
            return "", "missing"

        ext = os.path.splitext(abs_path)[1].lower()
        if ext == ".pdf":
            try:
                import pypdf

                pages_text: list[str] = []
                with open(abs_path, "rb") as f_pdf:
                    reader = pypdf.PdfReader(f_pdf)
                    for page in reader.pages:
                        try:
                            page_txt = page.extract_text() or ""
                        except (
                            pypdf.errors.PyPdfError,
                            OSError,
                            ValueError,
                            KeyError,
                            IndexError,
                            TypeError,
                            AttributeError,
                        ):
                            page_txt = ""
                        if page_txt.strip():
                            pages_text.append(page_txt.strip())
                return sanitize_text("\n\n".join(pages_text)), "local"
            except (
                pypdf.errors.PyPdfError,
                OSError,
                ValueError,
                KeyError,
                IndexError,
                TypeError,
            ) as exc:
                logger.debug(
                    "Offline PDF text preview failed for %s: %s", abs_path, exc
                )
                return "", "missing"

        if ext in (
            ".png",
            ".jpg",
            ".jpeg",
            ".bmp",
            ".tiff",
            ".webp",
            ".mp3",
            ".wav",
            ".m4a",
        ):
            return "", "provisional"

        if not registry.is_supported(ext):
            return "", "missing"

        res = extract_file_text(abs_path, settings=settings, fast_triage=False)
        if isinstance(res, ExtractionResult):
            return res.text or "", "local"
        return str(res or ""), "local"


def process_item_worker(
    base_dir: str, item: str, progress_callback: Callable, db, settings=None
) -> tuple[str, str, str]:
    """Process a single item, checking hash first, and extract its text content."""
    try:
        from app.core.scanner import parse_item_identifier

        src_root, rel_p = parse_item_identifier(item, default_base_dir=base_dir)
        item_path = os.path.join(src_root, rel_p) if src_root else item
        if os.path.isfile(item_path):
            _, ext = os.path.splitext(item_path)
            file_hash = get_file_hash(item_path)
            if not registry.is_supported(ext):
                return item, "[STATUS:UNSUPPORTED]", file_hash
            doc = db.get_document(src_root, rel_p) if db else None
            if doc and doc["file_hash"] == file_hash:
                # Skip extraction if unchanged
                return item, doc["extracted_text"], file_hash

            res = extract_file_text(
                item_path, settings=settings, progress_callback=progress_callback
            )
            text = str(res)
            if db:
                db.upsert_document(src_root, rel_p, file_hash, text)
            return item, text, file_hash
        elif os.path.isdir(item_path):
            return item, item, ""
    except Exception as e:
        logging.error(
            f"General worker failure processing item: {item}. Error: {str(e)}",
            exc_info=True,
        )

    finally:
        emit_progress(
            progress_callback,
            progress_or_update=1.0,
            stage=f"Completed extraction for {item}",
            unit_count=1,
            unit_type="files",
        )

    return item, "", ""


class _ChunkExtractionBatch:
    """Per-chunk database adapter that prefetches cached hashes/texts and flushes upserts once."""

    def __init__(self, db: Any, base_dir: str, items: list[Any]) -> None:
        self._db = db
        self._lock = threading.Lock()
        self._pending: list[tuple[str, str, str, str]] = []
        self._docs_by_root: dict[str, dict[str, dict[str, Any]]] = {}
        self._prefetched_roots: set[str] = set()
        self._prefetch(base_dir, items)

    def _prefetch(self, base_dir: str, items: list[Any]) -> None:
        if not self._db or not items:
            return
        from app.core.scanner import parse_item_identifier

        paths_by_root: dict[str, list[str]] = {}
        for item in items:
            try:
                src_root, rel_p = parse_item_identifier(item, default_base_dir=base_dir)
            except (OSError, ValueError, TypeError):
                continue
            if src_root and rel_p:
                paths_by_root.setdefault(src_root, []).append(rel_p)

        for src_root, rel_paths in paths_by_root.items():
            cached_hashes: dict[str, str] | None = None
            if hasattr(self._db, "get_document_hashes"):
                try:
                    raw_hashes = self._db.get_document_hashes(src_root, rel_paths)
                    if isinstance(raw_hashes, dict):
                        cached_hashes = raw_hashes
                except Exception:  # noqa: BLE001 - custom db doubles or driver errors fall back to per-item lookup
                    cached_hashes = None

            if cached_hashes is None:
                continue

            matched_paths = [p for p, h in cached_hashes.items() if h]
            if not matched_paths:
                self._docs_by_root[src_root] = {}
                self._prefetched_roots.add(src_root)
                continue

            if hasattr(self._db, "get_documents_map"):
                try:
                    docs_map = self._db.get_documents_map(src_root, matched_paths)
                    if isinstance(docs_map, dict):
                        self._docs_by_root[src_root] = docs_map
                        self._prefetched_roots.add(src_root)
                except Exception as exc:  # noqa: BLE001 - fall back to get_document on error
                    logger.debug("Batch document prefetch skipped: %s", exc)

    def get_document(self, base_dir: str, filepath: str) -> dict[str, Any] | None:
        """Return prefetched cached document metadata if available, else fall back to ``db``."""
        if base_dir in self._prefetched_roots:
            root_map = self._docs_by_root.get(base_dir, {})
            norm_fp = filepath.replace("\\", "/")
            return root_map.get(filepath) or root_map.get(norm_fp)
        if self._db and hasattr(self._db, "get_document"):
            return self._db.get_document(base_dir, filepath)
        return None

    def upsert_document(
        self, base_dir: str, filepath: str, file_hash: str, extracted_text: str
    ) -> None:
        """Buffer an extracted document row to be persisted once at chunk completion."""
        with self._lock:
            self._pending.append((base_dir, filepath, file_hash, extracted_text))

    def flush(self) -> None:
        """Persist all buffered extraction rows in a single batch transaction."""
        with self._lock:
            if not self._pending:
                return
            rows = list(self._pending)
            self._pending.clear()
        if not self._db:
            return
        if hasattr(self._db, "upsert_documents"):
            self._db.upsert_documents(rows)
        elif hasattr(self._db, "upsert_document"):
            for row in rows:
                self._db.upsert_document(*row)


def build_corpus_generator(
    base_dir: str,
    items_to_sort: list,
    progress_callback: Callable,
    max_workers: int,
    db,
    chunk_size: int = 50,
    sequential: bool = False,
    cancel_check: Callable | None = None,
    settings=None,
):
    """Map every item to its text payload asynchronously in fixed batches and yield chunks.

    Parameters
    ----------
    base_dir : str
        The base directory containing the items.
    items_to_sort : list
        A list of item names to process.
    progress_callback : Callable
        A callback function to execute after each item is processed.
    max_workers : int
        The maximum number of parallel workers.
    db : Any
        Database connection or instance used for document lookups.
    chunk_size : int
        The number of items to yield in each chunk (defaults to fixed 50).
    sequential : bool
        If True, items are processed iteratively in exact order to eliminate ingestion noise.
    cancel_check : Callable | None
        A callback to check if the process should be cancelled.
    settings : Any | None
        Optional settings object.

    Yields
    ------
    dict
        A mapping of item names to their text payloads for a chunk of items.
    """
    if settings is None:
        from app.config import AppSettings

        try:
            settings = AppSettings()
        except Exception:
            logger.warning(
                "Could not load AppSettings; extracting without settings",
                exc_info=True,
            )

    items_to_sort = sorted(items_to_sort)
    try:
        for i in range(0, len(items_to_sort), chunk_size):
            if cancel_check and cancel_check():
                break

            batch = items_to_sort[i : i + chunk_size]
            chunk = {}
            chunk_db = (
                _ChunkExtractionBatch(db, base_dir, batch) if db is not None else None
            )
            effective_db = chunk_db if chunk_db is not None else db

            try:
                if sequential:
                    for item in batch:
                        if cancel_check and cancel_check():
                            break
                        item_name, item_text, file_hash = process_item_worker(
                            base_dir,
                            item,
                            progress_callback,
                            effective_db,
                            settings=settings,
                        )

                        item_text_str = str(item_text)
                        chunk[item_name] = {
                            "text": item_text_str
                            if item_text_str.startswith(("[STATUS:", item_name + " "))
                            else item_name + " " + item_text_str,
                            "hash": file_hash,
                        }
                else:
                    from app.core.shared_registry import SharedWorkerPool

                    pool = SharedWorkerPool.get_instance(max_workers=max_workers)
                    item_to_future = {
                        item: pool.submit(
                            process_item_worker,
                            base_dir,
                            item,
                            progress_callback,
                            effective_db,
                            settings,
                        )
                        for item in batch
                    }
                    timeout = (
                        getattr(settings, "VISUAL_TIMEOUT", None) if settings else None
                    )
                    for item in batch:
                        if cancel_check and cancel_check():
                            # Attempt to cancel remaining futures in this batch
                            for fut in item_to_future.values():
                                fut.cancel()
                            break
                        future = item_to_future[item]
                        try:
                            item_name, item_text, file_hash = future.result(
                                timeout=timeout
                            )
                        except concurrent.futures.TimeoutError:
                            logging.warning(
                                f"Extraction of '{item}' timed out after {timeout} seconds."
                            )
                            item_name = item
                            item_text = "[STATUS:TIMEOUT]"
                            file_hash = ""
                            future.cancel()

                        item_text_str = str(item_text)
                        chunk[item_name] = {
                            "text": item_text_str
                            if item_text_str.startswith(("[STATUS:", item_name + " "))
                            else item_name + " " + item_text_str,
                            "hash": file_hash,
                        }

                    item_to_future.clear()
                    del item_to_future
            finally:
                if chunk_db is not None:
                    chunk_db.flush()

            if chunk:
                yield chunk
                chunk = {}
    finally:
        from app.core.shared_registry import SharedModelRegistry

        registry = SharedModelRegistry.get_instance()
        registry.unload_model("easyocr")
        registry.unload_model("florence-2")
