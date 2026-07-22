"""Defines document extraction strategies for various file types."""

import csv
import io
import logging
from typing import Protocol

import pypdf

_ocr_reader = None
_ocr_reader_loaded = False


def get_ocr_reader():
    """Lazily load and return the EasyOCR Reader instance configured for CPU execution."""
    global _ocr_reader, _ocr_reader_loaded
    if not _ocr_reader_loaded:
        _ocr_reader_loaded = True
        try:
            import torch
            import easyocr

            # CPU optimization
            torch.set_num_threads(2)
            # Create reader on CPU to execute entirely locally
            _ocr_reader = easyocr.Reader(["en"], gpu=False)
        except Exception as e:
            logging.error(f"Failed to load EasyOCR reader: {e}")
            _ocr_reader = None
    return _ocr_reader


def extract_text_from_image(image) -> str:
    """Extract character-level text from an image using the unified EasyOCR engine."""
    reader = get_ocr_reader()
    if reader is None:
        return ""

    try:
        import numpy as np

        img_np = np.array(image)
        results = reader.readtext(img_np)
        extracted_text = " ".join([res[1] for res in results])
        return extracted_text.strip()
    except Exception as e:
        logging.error(f"OCR processing failed: {e}")
        return ""


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
        from docx import Document

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
        import pandas as pd

        dfs = pd.read_excel(file_path, sheet_name=None)
        if isinstance(dfs, dict):
            return "\n".join(df.to_string() for df in dfs.values())
        return dfs.to_string()


class PdfExtractor:
    """Extractor for PDF documents."""

    def extract(self, file_path: str) -> str:
        """Extract text from a .pdf file."""
        text = ""
        try:
            with open(file_path, "rb") as f:
                reader = pypdf.PdfReader(f)
                for page in reader.pages:
                    text += page.extract_text() or ""
        except Exception as e:
            logging.error(f"Failed standard text extraction for {file_path}: {e}")

        if not text.strip():
            # Standard extraction yields no text, attempt visual extraction
            reader = get_ocr_reader()
            if reader is None:
                return text

            visual_text = ""
            try:
                from PIL import Image

                with open(file_path, "rb") as f:
                    pdf_reader = pypdf.PdfReader(f)
                    for page in pdf_reader.pages:
                        for img in page.images:
                            try:
                                pil_image = Image.open(io.BytesIO(img.data))
                                extracted = extract_text_from_image(pil_image)
                                if extracted:
                                    visual_text += extracted + " "
                            except Exception as img_e:
                                logging.error(
                                    f"Failed to process image in PDF {file_path}: {img_e}"
                                )
            except Exception as e:
                logging.error(f"Failed visual extraction for PDF {file_path}: {e}")

            if visual_text.strip():
                return visual_text.strip()

        return text


class ImageExtractor:
    """Extractor for image files."""

    def extract(self, file_path: str) -> str:
        """Extract literal text from an image using local character recognition."""
        try:
            from PIL import Image

            image = Image.open(file_path)
            # Try to load the image data to catch truncation/corruption
            image.load()
        except Exception as e:
            logging.error(f"Corrupt image file {file_path}: {e}")
            return "[STATUS:ERROR: Corrupt Image File]"

        reader = get_ocr_reader()
        if reader is None:
            return "[STATUS:ERROR: Vision Model Offline]"

        try:
            extracted_text = extract_text_from_image(image)
            return extracted_text
        except Exception as e:
            logging.error(f"Failed to process image {file_path}: {e}")
            return "[STATUS:ERROR: Vision Model Failure]"


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

    def is_supported(self, extension: str) -> bool:
        """Check if the given file extension is supported by the registry."""
        return extension.lower() in self._extractors


registry = ExtractorRegistry()
registry.register(".txt", TxtExtractor())
registry.register(".docx", DocxExtractor())
registry.register(".csv", CsvExtractor())
registry.register(".xlsx", XlsxExtractor())
registry.register(".xls", XlsxExtractor())
registry.register(".pdf", PdfExtractor())
registry.register(".png", ImageExtractor())
registry.register(".jpg", ImageExtractor())
registry.register(".jpeg", ImageExtractor())
