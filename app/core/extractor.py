"""Document extraction and processing module.

This module provides utilities to read text from various file formats.
"""

import concurrent.futures
import csv
import hashlib
import logging
import os
from typing import Callable, Tuple

import pandas as pd
import pypdf
from docx import Document

from app.config import MAX_WORKERS
from app.core.db import db


def get_file_hash(file_path: str) -> str:
    hasher = hashlib.sha256()
    try:
        with open(file_path, 'rb') as f:
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
        if ext == ".txt":
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                text = f.read()
        elif ext == ".docx":
            doc = Document(file_path)
            text = "\n".join([p.text for p in doc.paragraphs])
        elif ext == ".csv":
            with open(file_path, newline="", encoding="utf-8", errors="ignore") as f:
                reader = csv.reader(f)
                text = " ".join([" ".join(row) for row in reader])
        elif ext in [".xlsx", ".xls"]:
            df = pd.read_excel(file_path)
            text = df.to_string()
        elif ext == ".pdf":
            with open(file_path, "rb") as f:
                reader = pypdf.PdfReader(f)
                for page in reader.pages:
                    text += page.extract_text() or ""
    except Exception as e:
        logging.error(
            f"Failed to extract text from {file_path}. Error: {str(e)}", exc_info=True
        )
    return text


def process_item_worker(base_dir: str, item: str, progress_callback: Callable) -> Tuple[str, str, str]:
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


def build_corpus_generator(base_dir: str, items_to_sort: list, progress_callback: Callable, chunk_size: int = 50):
    """Map every item to its text payload asynchronously and yield chunks."""
    chunk = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_item = {
            executor.submit(
                process_item_worker, base_dir, item, progress_callback
            ): item
            for item in items_to_sort
        }
        for future in concurrent.futures.as_completed(future_to_item):
            item_name, item_text, file_hash = future.result()
            
            doc = db.get_document(base_dir, item_name)
            if doc and doc["file_hash"] == file_hash and doc["embedding"] is not None:
                # Already processed and unchanged, no need to yield to analyzer
                continue
                
            chunk[item_name] = {"text": item_name + " " + item_text, "hash": file_hash}
            
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = {}
                
        if chunk:
            yield chunk
