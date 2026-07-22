"""Defines document extraction strategies for various file types."""

import csv
import io
import logging
from typing import Protocol

import pypdf

_vision_model = None
_vision_model_loaded = False


def get_vision_model():
    """Lazily load and return the vision-language model pipeline."""
    global _vision_model, _vision_model_loaded
    if not _vision_model_loaded:
        _vision_model_loaded = True
        try:
            import torch
            from transformers import pipeline

            # Keep the vision-language model CPU-optimized
            torch.set_num_threads(2)
            _vision_model = pipeline(
                "image-to-text",
                model="Salesforce/blip-image-captioning-base",
                device=-1,  # Force CPU
            )
        except Exception as e:
            logging.error(f"Failed to load vision model: {e}")
            _vision_model = None
    return _vision_model


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
            model = get_vision_model()
            if model is None:
                return text

            visual_text = ""
            try:
                from PIL import Image

                with open(file_path, "rb") as f:
                    reader = pypdf.PdfReader(f)
                    for page in reader.pages:
                        for img in page.images:
                            try:
                                pil_image = Image.open(io.BytesIO(img.data))
                                result = model(pil_image)
                                if (
                                    result
                                    and isinstance(result, list)
                                    and "generated_text" in result[0]
                                ):
                                    visual_text += result[0]["generated_text"] + " "
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
        """Extract descriptive text from an image using a vision-language model."""
        try:
            from PIL import Image

            image = Image.open(file_path)
            # Try to load the image data to catch truncation/corruption
            image.load()
        except Exception as e:
            logging.error(f"Corrupt image file {file_path}: {e}")
            return "[STATUS:ERROR: Corrupt Image File]"

        model = get_vision_model()
        if model is None:
            return "[STATUS:ERROR: Vision Model Offline]"

        try:
            result = model(image)
            if result and isinstance(result, list) and "generated_text" in result[0]:
                return result[0]["generated_text"]
            return ""
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
