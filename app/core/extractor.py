"""Document extraction and processing module.

This module provides utilities to read text from various file formats.
"""

import concurrent.futures
import hashlib
import logging
import os
from typing import Callable, Tuple

import pypdf.errors

from app.core.extractor_strategies import registry


def get_file_hash(file_path: str) -> str:
    """Calculate the SHA-256 hash of a file."""
    hasher = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
    except Exception:
        pass
    return hasher.hexdigest()


def extract_file_text(file_path: str) -> str:
    """Extract text content from a given file."""
    ext = os.path.splitext(file_path)[1].lower()
    text = ""
    try:
        extractor = registry.get_extractor(ext)
        if extractor:
            text = extractor.extract(file_path)
            if not text.strip():
                if os.path.getsize(file_path) > 0:
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
    base_dir: str, item: str, progress_callback: Callable, db
) -> Tuple[str, str, str]:
    """Process a single item, checking hash first, and extract its text content."""
    try:
        item_path = os.path.join(base_dir, item)
        if os.path.isfile(item_path):
            file_hash = get_file_hash(item_path)
            doc = db.get_document(base_dir, item)
            if doc and doc["file_hash"] == file_hash and doc["embedding"] is not None:
                # Skip extraction if unchanged
                return item, doc["extracted_text"], file_hash

            text = extract_file_text(item_path)
            return item, text, file_hash
        elif os.path.isdir(item_path):
            return item, item, ""
    except Exception as e:
        logging.error(
            f"General worker failure processing item: {item}. Error: {str(e)}"
        )
    finally:
        progress_callback()

    return item, "", ""


def build_corpus_generator(
    base_dir: str,
    items_to_sort: list,
    progress_callback: Callable,
    max_workers: int,
    db,
    chunk_size: int = 50,
    sequential: bool = False,
    active_model_name: str | None = None,
    active_dimension: int | None = None,
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
    chunk_size : int
        The number of items to yield in each chunk.
    sequential : bool
        If True, items are processed iteratively in exact order to eliminate ingestion noise.

    Yields
    ------
    dict
        A mapping of item names to their text payloads for a chunk of items.
    """
    items_to_sort = sorted(items_to_sort)
    chunk = {}
    if sequential:
        for item in items_to_sort:
            item_name, item_text, file_hash = process_item_worker(base_dir, item, progress_callback, db)

            doc = db.get_document(base_dir, item_name)
            if (
                doc
                and doc["file_hash"] == file_hash
                and doc["embedding"] is not None
                and doc.get("model_name") == active_model_name
                and doc.get("vector_dimension") == active_dimension
            ):
                # Already processed and unchanged, no need to yield to analyzer
                continue

            chunk[item_name] = {"text": item_text if item_text.startswith("[STATUS:") else item_name + " " + item_text, "hash": file_hash}
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = {}
        if chunk:
            yield chunk
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            item_to_future = {
                item: executor.submit(
                    process_item_worker, base_dir, item, progress_callback, db
                )
                for item in items_to_sort
            }
            for item in items_to_sort:
                future = item_to_future[item]
                item_name, item_text, file_hash = future.result()

                doc = db.get_document(base_dir, item_name)
                if (
                    doc
                    and doc["file_hash"] == file_hash
                    and doc["embedding"] is not None
                    and doc.get("model_name") == active_model_name
                    and doc.get("vector_dimension") == active_dimension
                ):
                    continue

                chunk[item_name] = {
                    "text": item_text if item_text.startswith("[STATUS:") else item_name + " " + item_text,
                    "hash": file_hash,
                }
                if len(chunk) >= chunk_size:
                    yield chunk
                    chunk = {}
            if chunk:
                yield chunk
