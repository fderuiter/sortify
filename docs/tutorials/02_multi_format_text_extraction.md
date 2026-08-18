<!-- This document is automatically generated from notebooks/02_multi_format_text_extraction.ipynb. Do not edit manually. -->

# Multi-Format Text Extraction & Local Session Management

## 1. Context & Overview
This notebook demonstrates the document ingestion layer of **Smart AutoSorter AI Pro**. 
We will explore:
1. **Local Session Management**: Initializing and managing the lifespan of an application run via `AppSession`.
2. **Multi-Format Text Extraction**: Writing programmatically supported formats (`.txt`, `.csv`, `.docx`, `.xlsx`) to disk and extracting their text content safely.
3. **Database Inspection**: Inspecting the internal SQLite databases to verify that file text, hashes, and schemas are correctly registered in the system's storage layer.

```python
import csv
import os
import tempfile

# Install helpers/libraries for office formats
import docx
import openpyxl

# Core Smart AutoSorter imports
from app.config import Settings
from app.core.db_conn import clear_connection_cache, get_db_connection
from app.core.extractor import build_corpus_generator
from app.core.session import AppSession
```

## 2. Sandbox Setup & Generating Sample Documents
To guarantee isolated execution, we create a temporary directory and generate files across various supported formats.

```python
# Set up a secure, isolated temporary workspace
sandbox_dir = tempfile.TemporaryDirectory()
base_dir = sandbox_dir.name
print(f"[*] Safe base workspace directory: {base_dir}")

# 1. Create a plain text (.txt) file
txt_path = os.path.join(base_dir, "sample_notes.txt")
with open(txt_path, "w", encoding="utf-8") as f:
    f.write("Software development notes. Python programming, unit tests, and continuous integration pipelines.")

# 2. Create a CSV (.csv) spreadsheet file
csv_path = os.path.join(base_dir, "sample_spreadsheet.csv")
with open(csv_path, "w", encoding="utf-8", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["Item", "Amount", "Department"])
    writer.writerow(["Servers", "25000", "Infrastructure"])
    writer.writerow(["Monitors", "12000", "Hardware"])

# 3. Create a Microsoft Word (.docx) file
docx_path = os.path.join(base_dir, "sample_doc.docx")
doc = docx.Document()
doc.add_heading("Project Specification", level=1)
doc.add_paragraph("This document details clinical trial results, healthcare diagnostics, and medical treatments.")
doc.save(docx_path)

# 4. Create an Excel (.xlsx) workbook file
xlsx_path = os.path.join(base_dir, "sample_ledger.xlsx")
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Financial Overview"
ws["A1"] = "Quarterly Budget Report"
ws["A2"] = "Total Investment Assets and Balance Sheets"
ws["B2"] = 750000
wb.save(xlsx_path)

files_created = [os.path.basename(p) for p in [txt_path, csv_path, docx_path, xlsx_path]]
print(f"[+] Generated {len(files_created)} sample multi-format files: {files_created}")
```

## 3. Session Initialization & Core Ingestion Pipeline
We now instantiate our custom `AppSession` utilizing explicit `Settings`. We then trigger the synchronous `build_corpus_generator` to run text extraction across all the generated formats.

```python
# Initialize settings with AI consent disabled for deterministic, lightweight extraction
settings = Settings(AI_CONSENT_GRANTED=False, MAX_FOLDERS=5)

# Initialize AppSession
session = AppSession(settings=settings, base_dir=base_dir)
print(f"[+] AppSession initialized! ID: {session.session_id}")
print(f"[*] Session directory containing logs & databases: {session.session_dir}")

def progress_callback():
    """Progress callback function for text extraction."""
    print("    -> Extracting file...")

# Execute synchronous extraction of text payload and hashing from sample documents
generator = build_corpus_generator(
    base_dir=base_dir,
    items_to_sort=files_created,
    progress_callback=progress_callback,
    max_workers=2,
    db=session.db,
    chunk_size=10,
    sequential=True,
    settings=settings
)

extracted_chunks = list(generator)
print(f"\n[+] Ingestion complete! Processed {len(extracted_chunks)} chunks.")
```

## 4. Database Inspection via SQL
We connect directly to the session's SQLite database file (`autosorter.db`) to inspect the tables. This allows developers to audit the exact extracted text strings and calculated unique file hashes stored persistently.

```python
db_file = session.session_dir / "autosorter.db"
print(f"[*] Connecting to session database: {db_file}")

conn = get_db_connection(str(db_file))
cursor = conn.cursor()

# 1. Fetch tables schema list
cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
tables = cursor.fetchall()
print(f"[+] Database Tables present: {[t[0] for t in tables]}")

# 2. Query documents table
cursor.execute("SELECT filepath, file_hash, extracted_text FROM documents;")
records = cursor.fetchall()

print("\n--- Persisted Documents & Extracted Content ---")
for filepath, f_hash, text in records:
    truncated_text = text[:100] + "..." if len(text) > 100 else text
    print(f"File: {filepath}")
    print(f"  Hash: {f_hash}")
    print(f"  Text preview: {truncated_text}")
    print("-" * 45)

conn.close()
```

## 5. Clean Resource Teardown
Finally, close the session to release file locks on databases, and safely delete the temporary sandbox.

```python
print("[*] Closing session and cleaning up directories...")
session.close()
clear_connection_cache(only_current_and_inactive=False)
sandbox_dir.cleanup()
print("[+] Environment cleaned up. Extraction workflow complete!")
```

