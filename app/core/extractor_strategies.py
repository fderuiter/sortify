import csv
from typing import Protocol

import pandas as pd
import pypdf
from docx import Document


class DocumentExtractor(Protocol):
    def extract(self, file_path: str) -> str:
        ...

class TxtExtractor:
    def extract(self, file_path: str) -> str:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

class DocxExtractor:
    def extract(self, file_path: str) -> str:
        doc = Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs])

class CsvExtractor:
    def extract(self, file_path: str) -> str:
        with open(file_path, newline="", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f)
            return " ".join([" ".join(row) for row in reader])

class XlsxExtractor:
    def extract(self, file_path: str) -> str:
        df = pd.read_excel(file_path)
        return df.to_string()

class PdfExtractor:
    def extract(self, file_path: str) -> str:
        text = ""
        with open(file_path, "rb") as f:
            reader = pypdf.PdfReader(f)
            for page in reader.pages:
                text += page.extract_text() or ""
        return text

class ExtractorRegistry:
    def __init__(self):
        self._extractors = {}

    def register(self, extension: str, extractor: DocumentExtractor):
        self._extractors[extension.lower()] = extractor

    def get_extractor(self, extension: str) -> DocumentExtractor:
        return self._extractors.get(extension.lower())

registry = ExtractorRegistry()
registry.register(".txt", TxtExtractor())
registry.register(".docx", DocxExtractor())
registry.register(".csv", CsvExtractor())
registry.register(".xlsx", XlsxExtractor())
registry.register(".xls", XlsxExtractor())
registry.register(".pdf", PdfExtractor())
