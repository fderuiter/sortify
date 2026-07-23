"""Document extraction and processing module.

This module provides utilities to read text from various file formats.
"""

import concurrent.futures
import hashlib
import logging
import os
import struct
from typing import Callable, Tuple

import pypdf.errors

from app.core.extractor_strategies import registry


def get_file_hash(file_path: str) -> str:
    """Calculate the SHA-256 hash of a file.

    For MP3 and M4A files, skips metadata headers and structural atoms
    to isolate the raw audio payload, ensuring stable hashes after tag edits.
    """
    hasher = hashlib.sha256()

    offset = 0
    size_to_hash = -1  # -1 means hash to EOF

    try:
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".mp3":
            with open(file_path, "rb") as f:
                while True:
                    header = f.read(10)
                    if len(header) >= 10 and header[:3] == b"ID3":
                        flags = header[5]
                        size = (
                            (header[6] << 21)
                            | (header[7] << 14)
                            | (header[8] << 7)
                            | header[9]
                        )
                        has_footer = (flags & 0x10) != 0
                        tag_size = 10 + size + (10 if has_footer else 0)
                        offset += tag_size
                        f.seek(tag_size - 10, 1)
                    else:
                        break
        elif ext == ".m4a":
            with open(file_path, "rb") as f:
                while True:
                    header = f.read(8)
                    if len(header) < 8:
                        break
                    box_size, box_type = struct.unpack(">I4s", header)
                    header_size = 8

                    if box_size == 1:
                        box_size = struct.unpack(">Q", f.read(8))[0]
                        header_size = 16
                    elif box_size == 0:
                        # extends to EOF
                        if box_type == b"mdat":
                            offset = f.tell()
                            size_to_hash = -1
                        break

                    if box_type == b"mdat":
                        offset = f.tell()
                        size_to_hash = box_size - header_size
                        break

                    f.seek(box_size - header_size, os.SEEK_CUR)
    except Exception:
        # Fallback to standard whole-file hashing if parsing fails
        offset = 0
        size_to_hash = -1

    try:
        with open(file_path, "rb") as f:
            if offset > 0:
                f.seek(offset)

            bytes_remaining = size_to_hash
            chunk_size = 4096

            while True:
                if bytes_remaining != -1:
                    read_size = min(chunk_size, bytes_remaining)
                    if read_size <= 0:
                        break
                else:
                    read_size = chunk_size

                chunk = f.read(read_size)
                if not chunk:
                    break

                hasher.update(chunk)
                if bytes_remaining != -1:
                    bytes_remaining -= len(chunk)
    except Exception:
        pass

    return hasher.hexdigest()


def extract_file_text(file_path: str, settings=None) -> str:
    """Extract text content from a given file."""
    ext = os.path.splitext(file_path)[1].lower()
    text = ""
    try:
        extractor = registry.get_extractor(ext)
        if extractor:
            text = extractor.extract(file_path, settings=settings)
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
            if (
                doc
                and doc["file_hash"] == file_hash
                and doc.get("extracted_text") != "[STATUS:BYPASSED]"
            ):
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
    if sequential:
        for item in items_to_sort:
            if cancel_check and cancel_check():
                break
            item_name, item_text, file_hash = process_item_worker(
                base_dir, item, progress_callback, db, settings=settings
            )

            doc = db.get_document(base_dir, item_name)
            if (
                doc
                and doc["file_hash"] == file_hash
                and doc.get("extracted_text") != "[STATUS:BYPASSED]"
            ):
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
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            item_to_future = {
                item: executor.submit(
                    process_item_worker, base_dir, item, progress_callback, db, settings
                )
                for item in items_to_sort
            }
            timeout = getattr(settings, "VISUAL_TIMEOUT", None) if settings else None
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
                if (
                    doc
                    and doc["file_hash"] == file_hash
                    and doc.get("extracted_text") != "[STATUS:BYPASSED]"
                ):
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
