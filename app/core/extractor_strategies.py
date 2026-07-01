"""Defines document extraction strategies for various file types."""

import csv
from typing import Protocol

import pandas as pd
import pypdf
from docx import Document


class DocumentExtractor(Protocol):
    """Protocol for extracting text from documents."""
    
    def extract(self, file_path: str) -> str:
        """Extract and return text from the given file."""
        ...

class TxtExtractor:
    """Extractor for plain text files."""
    
    def extract(self, file_path: str) -> str:
        """Extract text from a .txt file."""
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()

class DocxExtractor:
    """Extractor for Microsoft Word documents."""
    
    def extract(self, file_path: str) -> str:
        """Extract text from a .docx file."""
        doc = Document(file_path)
        return "\n".join([p.text for p in doc.paragraphs])

class CsvExtractor:
    """Extractor for comma-separated values files."""
    
    def extract(self, file_path: str) -> str:
        """Extract text from a .csv file."""
        with open(file_path, newline="", encoding="utf-8", errors="ignore") as f:
            reader = csv.reader(f)
            return " ".join([" ".join(row) for row in reader])

class XlsxExtractor:
    """Extractor for Excel spreadsheets."""
    
    def extract(self, file_path: str) -> str:
        """Extract text from an Excel file."""
        df = pd.read_excel(file_path)
        return df.to_string()

class PdfExtractor:
    """Extractor for PDF documents."""
    
    def extract(self, file_path: str) -> str:
        """Extract text from a .pdf file."""
        text = ""
        with open(file_path, "rb") as f:
            reader = pypdf.PdfReader(f)
            for page in reader.pages:
                text += page.extract_text() or ""
        return text

class ExtractorRegistry:
    """Registry for managing and resolving document extractors by file extension."""
    
    def __init__(self):
        """Initialize the extractor registry."""
        self._extractors = {}

    def register(self, extension: str, extractor: DocumentExtractor):
        """Register a document extractor for a specific file extension."""
        self._extractors[extension.lower()] = extractor

    def get_extractor(self, extension: str) -> DocumentExtractor:
        """Retrieve the document extractor for the given file extension."""
        return self._extractors.get(extension.lower())

registry = ExtractorRegistry()
registry.register(".txt", TxtExtractor())
registry.register(".docx", DocxExtractor())
registry.register(".csv", CsvExtractor())
registry.register(".xlsx", XlsxExtractor())
registry.register(".xls", XlsxExtractor())
registry.register(".pdf", PdfExtractor())
