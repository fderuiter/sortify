# core/extractor.py
"""
Extractor Module

This module handles the extraction of text from various file formats
including PDF, DOCX, CSV, TXT, and Excel files.
"""

import os
import csv
import logging
import concurrent.futures
import pypdf
from docx import Document
import pandas as pd
from config import MAX_WORKERS, LOG_FILE

# Configure Centralized Logger
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - [%(filename)s:%(lineno)d] - %(message)s'
)

def extract_file_text(file_path):
    """
    Extracts text content from a given file based on its extension.
    
    Supported extensions include .txt, .docx, .csv, .xlsx, .xls, and .pdf.

    :param file_path: The absolute or relative path to the file.
    :type file_path: str
    :return: The extracted text as a single string. Returns empty string on failure.
    :rtype: str
    :raises Exception: Catches and logs any extraction errors without halting execution.
    """
    ext = os.path.splitext(file_path)[1].lower()
    text = ""
    try:
        if ext == '.txt':
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                text = f.read()
        elif ext == '.docx':
            doc = Document(file_path)
            text = "\n".join([p.text for p in doc.paragraphs])
        elif ext == '.csv':
            with open(file_path, newline='', encoding='utf-8', errors='ignore') as f:
                reader = csv.reader(f)
                text = " ".join([" ".join(row) for row in reader])
        elif ext in ['.xlsx', '.xls']:
            df = pd.read_excel(file_path)
            text = df.to_string()
        elif ext == '.pdf':
            with open(file_path, 'rb') as f:
                reader = pypdf.PdfReader(f)
                for page in reader.pages:
                    text += page.extract_text() or ""
    except Exception as e:
        # Centralized Error Catching: Logs the file path and specific error stack
        logging.error(f"Failed to extract text from {file_path}. Error: {str(e)}", exc_info=True)
    return text

def process_item_worker(base_dir, item, progress_callback):
    """
    Worker function to process a single item (file or directory) for text extraction.

    :param base_dir: The base directory where the item is located.
    :type base_dir: str
    :param item: The name of the file or directory to process.
    :type item: str
    :param progress_callback: A function to call upon completion to update UI progress.
    :type progress_callback: callable
    :return: A tuple containing the item name and its extracted text.
    :rtype: tuple(str, str)
    :raises Exception: Catches and logs any unexpected worker failures.
    """
    try:
        item_path = os.path.join(base_dir, item)
        if os.path.isfile(item_path):
            text = extract_file_text(item_path)
            return item, text
        elif os.path.isdir(item_path):
            return item, item
    except Exception as e:
        logging.error(f"General worker failure processing item: {item}. Error: {str(e)}")
    finally:
        # Crucial: Always fire callback so progress tracking doesn't stall out
        progress_callback()
        
    return item, ""

def build_corpus(base_dir, items_to_sort, progress_callback):
    """
    Maps every item to its text payload asynchronously while updating UI progress.

    :param base_dir: The base directory containing items to process.
    :type base_dir: str
    :param items_to_sort: A list of filenames or directories to include in the corpus.
    :type items_to_sort: list[str]
    :param progress_callback: A callback function to update progress in the UI.
    :type progress_callback: callable
    :return: A dictionary mapping filenames to their extracted text.
    :rtype: dict
    """
    corpus = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_item = {
            executor.submit(process_item_worker, base_dir, item, progress_callback): item 
            for item in items_to_sort
        }
        for future in concurrent.futures.as_completed(future_to_item):
            item_name, item_text = future.result()
            corpus[item_name] = item_name + " " + item_text 
    return corpus