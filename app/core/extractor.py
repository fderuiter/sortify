"""Document extraction and processing module.

This module provides utilities to read text from various file formats.
"""

import concurrent.futures
import logging
import os
from typing import Callable, Tuple

import pypdf.errors

from app.core.extractor_strategies import registry


def get_file_hash(file_path: str) -> str:
    """Calculate the SHA-256 hash of a file.

    For MP3 and M4A files, skips metadata headers and structural atoms
    to isolate the raw audio payload, ensuring stable hashes after tag edits.
    """
    from app.core.resilient_file_ops import resilient_file_hash

    return resilient_file_hash(file_path)


def extract_file_text(
    file_path: str, settings=None, progress_callback=None, cancel_check=None
) -> str:
    """Extract text content from a given file."""
    import inspect

    ext = os.path.splitext(file_path)[1].lower()
    text = ""
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

            text = extractor.extract(file_path, **kwargs)
            from app.core.text_utils import sanitize_text

            text = sanitize_text(text)
            if not text.strip():
                text = "[STATUS:EMPTY]"
        else:
            text = "[STATUS:UNSUPPORTED]"
    except pypdf.errors.FileNotDecryptedError:
        text = "[STATUS:ENCRYPTED]"
    except Exception as e:
        logging.error(
            f"Failed to extract text from {file_path}. Error: {str(e)}", exc_info=True
        )
        text = "[STATUS:FAILED]"
    return text


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

            text = extract_file_text(item_path, settings=settings)
            return item, text, file_hash
        elif os.path.isdir(item_path):
            return item, item, ""
    except Exception as e:
        logging.error(
            f"General worker failure processing item: {item}. Error: {str(e)}"
        )
    finally:
        if progress_callback:
            progress_callback()

    return item, "", ""


async def build_corpus_generator_async(
    base_dir: str,
    items_to_sort: list,
    db,
    cancel_check: Callable | None = None,
    settings=None,
    progress_callback: Callable | None = None,
):
    """Asynchronously map every item to its text payload sequentially and yield file-by-file.

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
        for item in items_to_sort:
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
    """Map every item to its text payload asynchronously and yield chunks.

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
        The number of items to yield in each chunk.
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
    chunk = {}
    try:
        if sequential:
            for item in items_to_sort:
                if cancel_check and cancel_check():
                    break
                item_name, item_text, file_hash = process_item_worker(
                    base_dir, item, progress_callback, db, settings=settings
                )

                doc = db.get_document(base_dir, item_name)
                if doc and doc["file_hash"] == file_hash:
                    # Already processed and unchanged, no need to yield to analyzer
                    continue

                chunk[item_name] = {
                    "text": item_text
                    if item_text.startswith("[STATUS:")
                    else item_name + " " + item_text,
                    "hash": file_hash,
                }
                if len(chunk) >= chunk_size:
                    yield chunk
                    chunk = {}
            if chunk:
                yield chunk
        else:
            from app.core.shared_registry import SharedWorkerPool

            pool = SharedWorkerPool.get_instance(max_workers=max_workers)
            item_to_future = {
                item: pool.submit(
                    process_item_worker, base_dir, item, progress_callback, db, settings
                )
                for item in items_to_sort
            }
            timeout = settings.VISUAL_TIMEOUT if settings else None
            for item in items_to_sort:
                if cancel_check and cancel_check():
                    # Attempt to cancel remaining futures
                    for fut in item_to_future.values():
                        fut.cancel()
                    break
                future = item_to_future[item]
                try:
                    item_name, item_text, file_hash = future.result(timeout=timeout)
                except concurrent.futures.TimeoutError:
                    logging.warning(
                        f"Extraction of '{item}' timed out after {timeout} seconds."
                    )
                    item_name = item
                    item_text = "[STATUS:TIMEOUT]"
                    file_hash = ""
                    # Cancel the future if possible
                    future.cancel()

                doc = db.get_document(base_dir, item_name)
                if doc and doc["file_hash"] == file_hash:
                    continue

                chunk[item_name] = {
                    "text": item_text
                    if item_text.startswith("[STATUS:")
                    else item_name + " " + item_text,
                    "hash": file_hash,
                }
                if len(chunk) >= chunk_size:
                    yield chunk
                    chunk = {}
            if chunk:
                yield chunk
    finally:
        from app.core.shared_registry import SharedModelRegistry
        registry = SharedModelRegistry.get_instance()
        registry.unload_model("easyocr")
        registry.unload_model("florence-2")
