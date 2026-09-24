"""Document extraction and processing module.

This module provides utilities to read text from various file formats.
"""

import concurrent.futures
import inspect
import logging
import os
from enum import Enum
from typing import Any, Callable, Dict, Optional, Tuple

import pypdf.errors
from pydantic import BaseModel, ConfigDict

from app.core.extractor_strategies import registry
from app.core.progress import emit_progress
from app.core.text_utils import sanitize_text


class ExtractionStatus(str, Enum):
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
    error_message: Optional[str] = None

    model_config = ConfigDict(extra="allow")

    def dict(self, *args, **kwargs) -> Dict[str, Any]:
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
            if hasattr(self, item) or (
                getattr(self, "__pydantic_extra__", None) is not None
                and item in self.__pydantic_extra__
            ):
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


def get_file_hash(file_path: str) -> str:
    """Calculate the SHA-256 hash of a file.

    For MP3 and M4A files, skips metadata headers and structural atoms
    to isolate the raw audio payload, ensuring stable hashes after tag edits.
    """
    from app.core.resilient_file_ops import resilient_file_hash

    return resilient_file_hash(file_path, skip_media_tags=True)


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

    if fast_triage and ext in (".png", ".jpg", ".jpeg", ".bmp", ".tiff"):
        if db and hasattr(db, "worker") and db.worker:
            def _bg_visual_job():
                full_text = str(extract_file_text(file_path, settings=settings, fast_triage=False))
                if base_dir:
                    rel_path = os.path.relpath(file_path, base_dir).replace("\\", "/")
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
            if fast_triage and ext == ".pdf" and not raw_text.strip():
                if db and hasattr(db, "worker") and db.worker:
                    def _bg_pdf_job():
                        full_text = str(extractor.extract(file_path, **kwargs))
                        if base_dir:
                            rel_path = os.path.relpath(file_path, base_dir).replace("\\", "/")
                            f_hash = get_file_hash(file_path)
                            db.upsert_document(base_dir, rel_path, f_hash, full_text)
                            db.update_tfidf_matrix_cache(base_dir)
                    db.worker.submit_background_job(_bg_pdf_job)
                return ExtractionResult(text="", status=ExtractionStatus.PROVISIONAL)
            text = sanitize_text(raw_text)
            if not text.strip():
                return ExtractionResult(text="", status=ExtractionStatus.EMPTY)

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
                    error_message=tag if status_enum == ExtractionStatus.ERROR else None,
                )

            return ExtractionResult(text=text, status=ExtractionStatus.SUCCESS)
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


def fast_triage_extract(
    file_path: str, settings=None, db=None, base_dir: str | None = None
) -> str:
    """Fast-tier synchronous extractor for native text and metadata (<50ms)."""
    return extract_file_text(
        file_path, settings=settings, fast_triage=True, db=db, base_dir=base_dir
    )


def process_item_worker(
    base_dir: str, item: str, progress_callback: Callable, db, settings=None
) -> Tuple[str, str, str]:
    """Process a single item, checking hash first, and extract its text content."""
    try:
        item_path = os.path.join(base_dir, item)
        if os.path.isfile(item_path):
            _, ext = os.path.splitext(item_path)
            if not registry.is_supported(ext):
                return item, "[STATUS:UNSUPPORTED]", ""

            file_hash = get_file_hash(item_path)
            doc = db.get_document(base_dir, item)
            if doc and doc["file_hash"] == file_hash:
                # Skip extraction if unchanged
                return item, doc["extracted_text"], file_hash

            res = extract_file_text(
                item_path, settings=settings, progress_callback=progress_callback
            )
            text = str(res)
            return item, text, file_hash
        elif os.path.isdir(item_path):
            return item, item, ""
    except Exception as e:
        logging.error(
            f"General worker failure processing item: {item}. Error: {str(e)}"
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


async def build_corpus_generator_async(
    base_dir: str,
    items_to_sort: list,
    db,
    cancel_check: Callable | None = None,
    settings=None,
    progress_callback: Callable | None = None,
    batch_size: int = 50,
):
    """Asynchronously map every item to its text payload sequentially in fixed batches and yield file-by-file.

    Parameters
    ----------
    base_dir : str
        The base directory containing the items.
    items_to_sort : list
        A list of item names to process.
    db : Any
        Database connection or instance used for document lookups.
    cancel_check : Callable | None
        A callback to check if the process should be cancelled.
    settings : Any | None
        Optional settings object.
    progress_callback : Callable | None
        Optional callback for intra-file progress updates.
    batch_size : int
        Fixed batch size for ingestion (default 50).

    Yields
    ------
    tuple of (str, str, str, bool)
        (item_name, item_text, file_hash, was_skipped)
    """
    import asyncio

    if settings is None:
        from app.config import AppSettings

        try:
            settings = AppSettings()
        except Exception:
            pass

    items_to_sort = sorted(items_to_sort)
    try:
        for i in range(0, len(items_to_sort), batch_size):
            if cancel_check and cancel_check():
                break

            batch = items_to_sort[i : i + batch_size]
            for item in batch:
                if cancel_check and cancel_check():
                    break

                item_path = os.path.join(base_dir, item)

                # 1. Run file hashing in background thread to protect event loop
                file_hash = await asyncio.to_thread(get_file_hash, item_path)

                # 2. Check cache database
                doc = await asyncio.to_thread(db.get_document, base_dir, item)
                if doc and doc["file_hash"] == file_hash:
                    # Skip extraction and yield immediately
                    yield item, doc["extracted_text"], file_hash, True
                    continue

                # 3. Process/extract file content in background thread
                text = await asyncio.to_thread(
                    extract_file_text,
                    item_path,
                    settings=settings,
                    progress_callback=progress_callback,
                    cancel_check=cancel_check,
                )

                yield item, text, file_hash, False
    finally:
        from app.core.shared_registry import SharedModelRegistry

        registry = SharedModelRegistry.get_instance()
        registry.unload_model("easyocr")
        registry.unload_model("florence-2")


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
            pass

    items_to_sort = sorted(items_to_sort)
    try:
        for i in range(0, len(items_to_sort), chunk_size):
            if cancel_check and cancel_check():
                break

            batch = items_to_sort[i : i + chunk_size]
            chunk = {}

            if sequential:
                for item in batch:
                    if cancel_check and cancel_check():
                        break
                    item_name, item_text, file_hash = process_item_worker(
                        base_dir, item, progress_callback, db, settings=settings
                    )

                    doc = db.get_document(base_dir, item_name)
                    if doc and doc["file_hash"] == file_hash:
                        # Already processed and unchanged, no need to yield to analyzer
                        continue

                    item_text_str = str(item_text)
                    chunk[item_name] = {
                        "text": item_text_str
                        if item_text_str.startswith("[STATUS:")
                        else item_name + " " + item_text_str,
                        "hash": file_hash,
                    }
                if chunk:
                    yield chunk
                    chunk = {}
            else:
                from app.core.shared_registry import SharedWorkerPool

                pool = SharedWorkerPool.get_instance(max_workers=max_workers)
                item_to_future = {
                    item: pool.submit(
                        process_item_worker,
                        base_dir,
                        item,
                        progress_callback,
                        db,
                        settings,
                    )
                    for item in batch
                }
                timeout = getattr(settings, "VISUAL_TIMEOUT", None) if settings else None
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

                    doc = db.get_document(base_dir, item_name)
                    if doc and doc["file_hash"] == file_hash:
                        continue

                    item_text_str = str(item_text)
                    chunk[item_name] = {
                        "text": item_text_str
                        if item_text_str.startswith("[STATUS:")
                        else item_name + " " + item_text_str,
                        "hash": file_hash,
                    }

                item_to_future.clear()
                del item_to_future

                if chunk:
                    yield chunk
                    chunk = {}
    finally:
        from app.core.shared_registry import SharedModelRegistry

        registry = SharedModelRegistry.get_instance()
        registry.unload_model("easyocr")
        registry.unload_model("florence-2")
