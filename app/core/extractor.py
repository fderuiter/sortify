"""Document extraction and processing module.

This module provides utilities to read text from various file formats.
"""

import concurrent.futures
import csv
import logging
import os
from typing import Callable, Tuple

import pandas as pd
import pypdf
from docx import Document


def extract_file_text(file_path: str) -> str:
    """Extract text content from a given file.

    Parameters
    ----------
    file_path : str
        The path to the file to be extracted.

    Returns
    -------
    str
        The extracted text from the file. Returns an empty string if extraction fails.

    """
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
        # Centralized Error Catching: Logs the file path and specific error stack
        logging.error(
            f"Failed to extract text from {file_path}. Error: {str(e)}", exc_info=True
        )
    return text


def process_item_worker(base_dir: str, item: str, progress_callback: Callable) -> Tuple[str, str]:
    """Process a single item and extract its text content.

    Parameters
    ----------
    base_dir : str
        The base directory containing the item.
    item : str
        The name of the item (file or directory) to process.
    progress_callback : Callable
        A callback function to execute after processing is complete.

    Returns
    -------
    Tuple[str, str]
        A tuple containing the item name and its extracted text.

    """
    try:
        item_path = os.path.join(base_dir, item)
        if os.path.isfile(item_path):
            text = extract_file_text(item_path)
            return item, text
        elif os.path.isdir(item_path):
            return item, item
    except Exception as e:
        logging.error(
            f"General worker failure processing item: {item}. Error: {str(e)}"
        )
    finally:
        # Crucial: Always fire callback so progress tracking doesn't stall out
        progress_callback()

    return item, ""


def build_corpus_generator(base_dir: str, items_to_sort: list, progress_callback: Callable, max_workers: int, chunk_size: int = 50):
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

    Yields
    ------
    dict
        A mapping of item names to their text payloads for a chunk of items.
    """
    chunk = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_item = {
            executor.submit(
                process_item_worker, base_dir, item, progress_callback
            ): item
            for item in items_to_sort
        }
        for future in concurrent.futures.as_completed(future_to_item):
            item_name, item_text = future.result()
            chunk[item_name] = item_name + " " + item_text
            
            if len(chunk) >= chunk_size:
                yield chunk
                chunk = {}
                
        if chunk:
            yield chunk
