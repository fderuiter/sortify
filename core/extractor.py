# core/extractor.py
import os
import csv
import concurrent.futures
import pypdf
from docx import Document
import pandas as pd
from config import MAX_WORKERS

def extract_file_text(file_path):
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
    except Exception:
        pass 
    return text

def process_item_worker(base_dir, item):
    item_path = os.path.join(base_dir, item)
    if os.path.isfile(item_path):
        return item, extract_file_text(item_path)
    elif os.path.isdir(item_path):
        return item, item
    return item, ""

def build_corpus(base_dir, items_to_sort):
    corpus = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_item = {
            executor.submit(process_item_worker, base_dir, item): item 
            for item in items_to_sort
        }
        for future in concurrent.futures.as_completed(future_to_item):
            item_name, item_text = future.result()
            # Weight the filename to ensure it's always considered
            corpus[item_name] = item_name + " " + item_text 
    return corpus