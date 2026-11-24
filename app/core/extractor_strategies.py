"""Defines document extraction strategies for various file types."""

import contextlib
import csv
import email
import email.errors
import email.header
import html
import html.parser
import io
import json
import logging
import os
import re
import zipfile
from datetime import datetime
from typing import Any, Optional, Protocol, cast

import defusedxml.ElementTree as DET
import pypdf

from app.core.exceptions import ArchiveSafetyError
from app.core.progress import emit_progress

_IMAGE_EMBEDDED_DATE_EXTENSIONS = {
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".png",
    ".webp",
}


def _parse_embedded_datetime(raw: Any) -> Optional[datetime]:
    """Parse an embedded EXIF or PDF metadata timestamp into a naive local datetime."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        dt = raw
        if dt.tzinfo is not None:
            with contextlib.suppress(Exception):
                dt = dt.astimezone().replace(tzinfo=None)
            if dt.tzinfo is not None:
                dt = dt.replace(tzinfo=None)
        return dt if 1900 <= dt.year <= 2200 else None

    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="ignore")
    if not isinstance(raw, str):
        return None

    cleaned = raw.strip().strip("\x00").strip()
    if not cleaned:
        return None

    # PDF date format: D:YYYYMMDDHHmmSSOHH'mm'
    pdf_candidate = cleaned[2:].strip() if cleaned.startswith("D:") else cleaned
    pdf_match = re.match(
        r"^(\d{4})(\d{2})?(\d{2})?(?:(\d{2})(\d{2})?(\d{2})?)?(?:[Zz]|[+\-]\d{2}'?\d{2}'?)?$",
        pdf_candidate,
    )
    if pdf_match and (cleaned.startswith("D:") or len(pdf_candidate) >= 8):
        try:
            year = int(pdf_match.group(1))
            month = int(pdf_match.group(2) or 1)
            day = int(pdf_match.group(3) or 1)
            hour = int(pdf_match.group(4) or 0)
            minute = int(pdf_match.group(5) or 0)
            second = int(pdf_match.group(6) or 0)
            if 1900 <= year <= 2200:
                return datetime(year, month, day, hour, minute, second)
        except ValueError:
            pass

    # EXIF and ISO date formats: YYYY:MM:DD HH:MM:SS or YYYY-MM-DD[THH:MM:SS]
    exif_match = re.match(
        r"^(\d{4})[:\-/](\d{1,2})[:\-/](\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?",
        cleaned,
    )
    if exif_match:
        try:
            year = int(exif_match.group(1))
            month = int(exif_match.group(2))
            day = int(exif_match.group(3))
            hour = int(exif_match.group(4) or 0)
            minute = int(exif_match.group(5) or 0)
            second = int(exif_match.group(6) or 0)
            if 1900 <= year <= 2200:
                return datetime(year, month, day, hour, minute, second)
        except ValueError:
            pass

    with contextlib.suppress(ValueError):
        iso_dt = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        return _parse_embedded_datetime(iso_dt)

    return None


def extract_embedded_date(file_path: str | os.PathLike[str]) -> Optional[datetime]:
    """Extract embedded creation/capture timestamp from image EXIF or PDF headers offline.

    Uses strictly bundled Pillow (EXIF tags DateTimeOriginal, DateTimeDigitized, DateTime)
    and pypdf (CreationDate, ModDate) without OCR or network requests.
    """
    try:
        path_str = os.fspath(file_path)
    except TypeError:
        return None

    if not path_str or not os.path.isfile(path_str):
        return None

    ext = os.path.splitext(path_str)[1].lower()
    if ext in _IMAGE_EMBEDDED_DATE_EXTENSIONS:
        try:
            from PIL import Image

            with Image.open(path_str) as img:
                exif = img.getexif() if hasattr(img, "getexif") else None
                exif_ifd: dict[Any, Any] = {}
                if exif:
                    with contextlib.suppress(Exception):
                        if hasattr(exif, "get_ifd"):
                            exif_ifd = dict(exif.get_ifd(0x8769) or {})
                    # Priority: DateTimeOriginal (36867), DateTimeDigitized (36868), DateTime (306)
                    for source_map, tag_id in (
                        (exif_ifd, 36867),
                        (exif, 36867),
                        (exif_ifd, 36868),
                        (exif, 36868),
                        (exif, 306),
                        (exif_ifd, 306),
                    ):
                        raw_val = source_map.get(tag_id) if source_map else None
                        parsed = _parse_embedded_datetime(raw_val)
                        if parsed is not None:
                            return parsed

                info = getattr(img, "info", None)
                if isinstance(info, dict):
                    for key in ("Creation Time", "date:create", "date:modify"):
                        parsed = _parse_embedded_datetime(info.get(key))
                        if parsed is not None:
                            return parsed
        except Exception:
            logging.debug(
                f"Failed to read embedded EXIF date from image {path_str}",
                exc_info=True,
            )
        return None

    if ext == ".pdf":
        try:
            with open(path_str, "rb") as f:
                reader = pypdf.PdfReader(f)
                meta = reader.metadata
                if meta is not None:
                    candidates = [
                        getattr(meta, "creation_date", None),
                        meta.get("/CreationDate") if hasattr(meta, "get") else None,
                        getattr(meta, "creation_date_raw", None),
                        getattr(meta, "modification_date", None),
                        meta.get("/ModDate") if hasattr(meta, "get") else None,
                        getattr(meta, "modification_date_raw", None),
                    ]
                    for raw_val in candidates:
                        parsed = _parse_embedded_datetime(raw_val)
                        if parsed is not None:
                            return parsed
        except Exception:
            logging.debug(
                f"Failed to read embedded PDF date from {path_str}",
                exc_info=True,
            )
        return None

    return None


def get_ocr_reader():
    """Lazily load and return the EasyOCR Reader instance configured for CPU execution."""
    from app.core.shared_registry import SharedModelRegistry

    return SharedModelRegistry.get_instance().get_ocr_reader()


def _emit_progress(
    progress_callback, pct: float = 0.0, stage: str | None = None
) -> None:
    """Safely invoke a progress callback with ratio and optional stage description using central emitter."""
    emit_progress(progress_callback, progress_or_update=pct, stage=stage)


def extract_text_from_image(
    image, settings=None, file_path=None, progress_callback=None
) -> str:
    """Extract character-level text from an image using configured vision engine with dynamic OCR fallback."""
    if settings is None:
        from app.config import AppSettings

        try:
            settings = AppSettings()
        except Exception:
            logging.debug("Could not load AppSettings, using defaults", exc_info=True)

    try:
        from PIL import Image

        if isinstance(image, str):
            file_path = file_path or image
            image = Image.open(image)

        # Check image dimensions / skip threshold / downscaling
        width, height = None, None
        if hasattr(image, "size"):
            try:
                sz = image.size
                if isinstance(sz, tuple) and len(sz) == 2:
                    width, height = sz
            except Exception:
                logging.debug("Could not read image size", exc_info=True)

        if isinstance(width, (int, float)) and isinstance(height, (int, float)):
            max_dimension = (
                getattr(settings, "IMAGE_MAX_DIMENSION", 1000) if settings else 1000
            )

            if max(width, height) > max_dimension:
                ratio = max_dimension / max(width, height)
                new_width = max(min(width, 400), int(width * ratio))
                new_height = max(min(height, 400), int(height * ratio))
                name = file_path if file_path else "In-memory image"
                logging.info(
                    f"Status flag [DOWNSCALED]: Downscaling high-resolution image {name} from {(width, height)} to {(new_width, new_height)} prior to text recognition"
                )
                if (
                    isinstance(getattr(image, "mode", None), str)
                    and image.mode not in ("RGB", "L")
                    and hasattr(image, "convert")
                ):
                    image = image.convert("RGB")
                image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)
        if (
            isinstance(getattr(image, "mode", None), str)
            and image.mode not in ("RGB", "L")
            and hasattr(image, "convert")
        ):
            image = image.convert("RGB")
    except Exception as e:
        logging.error(f"Failed preprocessing image: {e}", exc_info=True)

    vision_engine = (
        getattr(settings, "VISION_ENGINE", "easyocr") if settings else "easyocr"
    )
    if vision_engine == "florence-2":
        try:
            from app.core.shared_registry import SharedModelRegistry

            _emit_progress(
                progress_callback, 0.2, "Initializing Florence-2 VLM model..."
            )
            proc = SharedModelRegistry.get_instance().get_florence_processor()
            if proc is not None:
                _emit_progress(progress_callback, 0.5, "Extracting visual text...")
                res = proc.process_image(image, task_prompt="<OCR>")
                extracted = res.get("sanitized_text", "") or res.get("raw_output", "")
                if extracted and extracted.strip():
                    return extracted.strip()
        except Exception as e:
            logging.warning(
                f"Florence-2 vision engine processing failed for {file_path or 'image'}, falling back to EasyOCR: {e}",
                exc_info=True,
            )

    # Fallback to standard OCR (EasyOCR)
    _emit_progress(progress_callback, 0.3, "Initializing EasyOCR model...")
    reader = get_ocr_reader()
    if reader is None:
        return ""

    try:
        import numpy as np

        img_np = np.array(image)
        _emit_progress(progress_callback, 0.6, "Extracting visual text...")
        results = reader.readtext(img_np)
        extracted_text = " ".join([res[1] for res in results])
        return extracted_text.strip()
    except Exception as e:
        logging.error(f"OCR processing failed: {e}", exc_info=True)
        return ""


class DocumentExtractor(Protocol):
    """Protocol for extracting text from documents."""

    def extract(self, file_path: str) -> str:
        """Extract and return text from the given file."""
        ...


class TxtExtractor:
    """Extractor for plain text files."""

    def extract(self, file_path: str, settings=None) -> str:
        """Extract text from a .txt file."""
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()


def _resolve_text_budgets(settings: Any = None) -> tuple[int, int]:
    """Resolve row and character extraction budgets from Settings."""
    if settings is None:
        from app.config import AppSettings

        try:
            settings = AppSettings()
        except Exception:
            logging.debug("Could not load AppSettings, using defaults", exc_info=True)

    max_rows = int(
        getattr(
            settings,
            "TABULAR_MAX_ROWS",
            getattr(settings, "MAX_SPREADSHEET_ROWS", 10000),
        )
    )
    max_chars = int(
        getattr(
            settings,
            "TABULAR_MAX_CHARACTERS",
            getattr(settings, "MAX_SPREADSHEET_CHARACTERS", 50000),
        )
    )
    return max_rows, max_chars


class _BoundedTextAccumulator:
    """Accumulate extracted text lines up to row and character budgets."""

    def __init__(self, max_rows: int, max_chars: int, separator: str = "\n") -> None:
        """Initialize the bounded accumulator."""
        self.max_rows = max_rows
        self.max_chars = max_chars
        self.separator = separator
        self.lines: list[str] = []
        self.total_chars = 0
        self.row_count = 0
        self.truncated = False
        self.truncation_reason: str | None = None
        self.truncated_bounds: dict[str, Any] | None = None

    def _mark_truncated(self, reason: str) -> None:
        self.truncated = True
        if self.truncation_reason is None:
            self.truncation_reason = reason
            self.truncated_bounds = {
                "max_rows": self.max_rows,
                "max_characters": self.max_chars,
                "max_chars": self.max_chars,
            }

    def add_line(self, line: str) -> bool:
        """Append a line if within row and character budgets."""
        if self.row_count >= self.max_rows:
            self._mark_truncated("MAX_ROWS_EXCEEDED")
            return False

        sep_len = len(self.separator) if self.lines else 0
        added_len = len(line) + sep_len
        if self.total_chars + added_len > self.max_chars:
            remaining = self.max_chars - self.total_chars - sep_len
            if remaining > 0:
                self.lines.append(line[:remaining])
                self.total_chars = self.max_chars
            self._mark_truncated("MAX_CHARACTERS_EXCEEDED")
            return False

        self.lines.append(line)
        self.total_chars += added_len
        self.row_count += 1
        return True

    def apply_to(self, target: Any) -> None:
        """Copy truncation metadata onto an extractor strategy instance."""
        target.truncated = self.truncated
        target.truncation_reason = self.truncation_reason
        target.truncated_bounds = self.truncated_bounds

    def render(self) -> str:
        """Join accumulated lines using the configured separator."""
        return self.separator.join(self.lines)


def _extract_docx_table_lines(table: Any) -> list[str]:
    """Extract non-empty row lines from a Word table including nested tables."""
    row_lines: list[str] = []
    seen_tc_ids: set[int] = set()
    seen_tc_refs: list[Any] = []

    for row in getattr(table, "rows", ()):
        cell_parts: list[str] = []
        for cell in getattr(row, "cells", ()):
            tc = getattr(cell, "_tc", None)
            dedup_obj = tc if tc is not None else cell
            obj_id = id(dedup_obj)
            if obj_id in seen_tc_ids:
                continue
            seen_tc_ids.add(obj_id)
            seen_tc_refs.append(dedup_obj)

            para_texts = [
                p.text.strip()
                for p in getattr(cell, "paragraphs", ())
                if isinstance(getattr(p, "text", None), str) and p.text.strip()
            ]
            nested_tables = list(getattr(cell, "tables", ()))
            if not para_texts and not nested_tables:
                fallback = getattr(cell, "text", None)
                if isinstance(fallback, str) and fallback.strip():
                    para_texts.append(fallback.strip())

            nested_lines: list[str] = []
            for nested in nested_tables:
                nested_lines.extend(_extract_docx_table_lines(nested))

            combined_cell = " ".join([*para_texts, *nested_lines]).strip()
            if combined_cell:
                cell_parts.append(combined_cell)

        if cell_parts:
            row_lines.append(" ".join(cell_parts))

    return row_lines


def _extract_docx_hf_block(hf_container: Any) -> str:
    """Extract text from a Word section header or footer container."""
    parts = [
        p.text.strip()
        for p in getattr(hf_container, "paragraphs", ())
        if isinstance(getattr(p, "text", None), str) and p.text.strip()
    ]
    for tbl in getattr(hf_container, "tables", ()):
        parts.extend(_extract_docx_table_lines(tbl))
    return "\n".join(parts).strip()


def _extract_docx_text(source: Any, max_chars: int | None = None) -> str:
    """Extract headers, body paragraphs, nested tables, and footers from a DOCX source."""
    from docx import Document

    doc = Document(source)

    header_blocks: list[str] = []
    footer_blocks: list[str] = []
    seen_headers: set[str] = set()
    seen_footers: set[str] = set()

    for sec_idx, section in enumerate(getattr(doc, "sections", ())):
        diff_first = (
            getattr(section, "different_first_page_header_footer", False) is True
        )
        header_attrs = (
            ("header", "first_page_header", "even_page_header")
            if diff_first
            else ("header", "even_page_header")
        )
        for attr in header_attrs:
            hf = getattr(section, attr, None)
            if hf is None:
                continue
            if sec_idx > 0 and getattr(hf, "is_linked_to_previous", False) is True:
                continue
            block = _extract_docx_hf_block(hf)
            if block and block not in seen_headers:
                seen_headers.add(block)
                header_blocks.append(block)

        footer_attrs = (
            ("footer", "first_page_footer", "even_page_footer")
            if diff_first
            else ("footer", "even_page_footer")
        )
        for attr in footer_attrs:
            hf = getattr(section, attr, None)
            if hf is None:
                continue
            if sec_idx > 0 and getattr(hf, "is_linked_to_previous", False) is True:
                continue
            block = _extract_docx_hf_block(hf)
            if block and block not in seen_footers:
                seen_footers.add(block)
                footer_blocks.append(block)

    raw_paragraphs = [
        p.text
        for p in getattr(doc, "paragraphs", ())
        if isinstance(getattr(p, "text", None), str)
    ]

    table_lines: list[str] = []
    for tbl in getattr(doc, "tables", ()):
        table_lines.extend(_extract_docx_table_lines(tbl))

    if not header_blocks and not table_lines and not footer_blocks:
        combined = "\n".join(raw_paragraphs)
    else:
        body_paragraphs = [t for t in raw_paragraphs if t.strip()]
        combined = "\n".join(
            [*header_blocks, *body_paragraphs, *table_lines, *footer_blocks]
        )

    if max_chars is not None and len(combined) > max_chars:
        return combined[:max_chars]
    return combined


def _extract_pptx_text(source: Any, max_chars: int | None = None) -> str:
    """Extract text from all slides, grouped shapes, tables, and notes of a PPTX source."""
    from pptx import Presentation  # type: ignore[import-not-found,import-untyped]

    prs = Presentation(source)
    parts: list[str] = []
    curr_len = 0
    visited_ids: set[int] = set()
    visited_refs: list[Any] = []

    def _append_piece(piece: str) -> bool:
        nonlocal curr_len
        cleaned = piece.strip()
        if not cleaned:
            return False
        parts.append(cleaned)
        curr_len += len(cleaned) + 1
        return max_chars is not None and curr_len >= max_chars

    def _walk_shape(shape: Any) -> bool:
        sid = id(shape)
        if sid in visited_ids:
            return False
        visited_ids.add(sid)
        visited_refs.append(shape)

        has_table = getattr(shape, "has_table", False) is True
        if has_table:
            table = getattr(shape, "table", None)
            seen_tc_ids: set[int] = set()
            seen_tc_refs: list[Any] = []
            for row in getattr(table, "rows", ()):
                row_cells: list[str] = []
                for cell in getattr(row, "cells", ()):
                    tc = getattr(cell, "_tc", None)
                    dedup_obj = tc if tc is not None else cell
                    tc_id = id(dedup_obj)
                    if tc_id in seen_tc_ids:
                        continue
                    seen_tc_ids.add(tc_id)
                    seen_tc_refs.append(dedup_obj)
                    cell_txt = getattr(cell, "text", None)
                    if isinstance(cell_txt, str) and cell_txt.strip():
                        row_cells.append(cell_txt.strip())
                if row_cells and _append_piece(" ".join(row_cells)):
                    return True
        else:
            shape_txt = getattr(shape, "text", None)
            if isinstance(shape_txt, str) and shape_txt.strip():
                if _append_piece(shape_txt):
                    return True

        subshapes = getattr(shape, "shapes", None)
        if subshapes is not None:
            with contextlib.suppress(TypeError):
                for child in subshapes:
                    if _walk_shape(child):
                        return True
        return False

    for slide in getattr(prs, "slides", ()):
        limit_reached = False
        for shape in getattr(slide, "shapes", ()):
            if _walk_shape(shape):
                limit_reached = True
                break
        if limit_reached:
            break

        if getattr(slide, "has_notes_slide", False) is True:
            notes_slide = getattr(slide, "notes_slide", None)
            notes_tf = getattr(notes_slide, "notes_text_frame", None)
            notes_txt = getattr(notes_tf, "text", None)
            if isinstance(notes_txt, str) and notes_txt.strip():
                if _append_piece(notes_txt):
                    break

    rendered = "\n".join(parts)
    if max_chars is not None and len(rendered) > max_chars:
        return rendered[:max_chars]
    return rendered


def _extract_delimited_file(
    strategy: Any, file_path: str, delimiter: str, settings: Any = None
) -> str:
    """Stream rows from a delimited text file while enforcing row and character limits."""
    max_rows, max_chars = _resolve_text_budgets(settings)
    acc = _BoundedTextAccumulator(max_rows=max_rows, max_chars=max_chars, separator=" ")

    with open(file_path, newline="", encoding="utf-8", errors="ignore") as f:
        reader = csv.reader(f, delimiter=delimiter)
        for row in reader:
            row_str = " ".join(str(cell) for cell in row if cell is not None)
            if not acc.add_line(row_str):
                break

    acc.apply_to(strategy)
    return acc.render()


class DocxExtractor:
    """Extractor for Microsoft Word documents."""

    def extract(self, file_path: str, settings=None) -> str:
        """Extract paragraphs, nested tables, headers, and footers from a .docx file."""
        return _extract_docx_text(file_path)


class PptxExtractor:
    """Extractor for Microsoft PowerPoint presentations."""

    def extract(self, file_path: str, settings=None) -> str:
        """Extract slide text, grouped shapes, tables, and speaker notes from a .pptx file."""
        return _extract_pptx_text(file_path)


class CsvExtractor:
    """Extractor for comma-separated values files."""

    truncated: bool = False
    truncation_reason: Optional[str] = None
    truncated_bounds: Optional[dict[str, Any]] = None

    def extract(self, file_path: str, settings=None) -> str:
        """Extract text from a .csv file using row-by-row streaming up to row and character limits."""
        return _extract_delimited_file(self, file_path, ",", settings=settings)


class TsvExtractor:
    """Extractor for tab-separated values files."""

    truncated: bool = False
    truncation_reason: Optional[str] = None
    truncated_bounds: Optional[dict[str, Any]] = None

    def extract(self, file_path: str, settings=None) -> str:
        """Extract text from a .tsv file using row-by-row streaming up to row and character limits."""
        return _extract_delimited_file(self, file_path, "\t", settings=settings)


class XlsxExtractor:
    """Extractor for Excel spreadsheets."""

    truncated: bool = False
    truncation_reason: Optional[str] = None
    truncated_bounds: Optional[dict[str, Any]] = None

    def extract(self, file_path: str, settings=None) -> str:
        """Extract text from an Excel file using read-only streaming mode across sheets."""
        self.truncated = False
        self.truncation_reason = None
        self.truncated_bounds = None

        if settings is None:
            from app.config import AppSettings

            try:
                settings = AppSettings()
            except Exception:
                logging.debug(
                    "Could not load AppSettings, using defaults", exc_info=True
                )

        max_sheets = getattr(
            settings,
            "TABULAR_MAX_SHEETS",
            getattr(settings, "MAX_SPREADSHEET_SHEETS", 10),
        )
        max_rows = getattr(
            settings,
            "TABULAR_MAX_ROWS",
            getattr(settings, "MAX_SPREADSHEET_ROWS", 10000),
        )
        max_chars = getattr(
            settings,
            "TABULAR_MAX_CHARACTERS",
            getattr(settings, "MAX_SPREADSHEET_CHARACTERS", 50000),
        )

        extracted_lines = []
        total_chars = 0

        # Primary method: openpyxl in read_only streaming mode
        try:
            import openpyxl

            wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
            try:
                all_sheets = wb.sheetnames
                if len(all_sheets) > max_sheets:
                    self.truncated = True
                    self.truncation_reason = "MAX_SHEETS_EXCEEDED"
                    self.truncated_bounds = {
                        "max_sheets": max_sheets,
                        "max_rows": max_rows,
                        "max_characters": max_chars,
                        "max_chars": max_chars,
                    }
                sheet_names = all_sheets[:max_sheets]
                for sheet_name in sheet_names:
                    if total_chars >= max_chars:
                        self.truncated = True
                        if not self.truncation_reason:
                            self.truncation_reason = "MAX_CHARACTERS_EXCEEDED"
                            self.truncated_bounds = {
                                "max_sheets": max_sheets,
                                "max_rows": max_rows,
                                "max_characters": max_chars,
                                "max_chars": max_chars,
                            }
                        break
                    sheet = wb[sheet_name]
                    row_count = 0
                    for row in sheet.iter_rows(values_only=True):
                        if row_count >= max_rows:
                            self.truncated = True
                            if not self.truncation_reason:
                                self.truncation_reason = "MAX_ROWS_EXCEEDED"
                                self.truncated_bounds = {
                                    "max_sheets": max_sheets,
                                    "max_rows": max_rows,
                                    "max_characters": max_chars,
                                    "max_chars": max_chars,
                                }
                            break
                        row_str = " ".join(
                            str(cell) for cell in row if cell is not None
                        ).strip()

                        added_len = len(row_str) + (1 if extracted_lines else 0)
                        if total_chars + added_len > max_chars:
                            remaining = (
                                max_chars - total_chars - (1 if extracted_lines else 0)
                            )
                            if remaining > 0:
                                extracted_lines.append(row_str[:remaining])
                            total_chars = max_chars
                            self.truncated = True
                            if not self.truncation_reason:
                                self.truncation_reason = "MAX_CHARACTERS_EXCEEDED"
                                self.truncated_bounds = {
                                    "max_sheets": max_sheets,
                                    "max_rows": max_rows,
                                    "max_characters": max_chars,
                                    "max_chars": max_chars,
                                }
                            break

                        extracted_lines.append(row_str)
                        total_chars += added_len
                        row_count += 1
            finally:
                wb.close()

            return "\n".join(extracted_lines) if extracted_lines else ""

        except Exception as e:
            logging.debug(
                f"openpyxl read-only streaming extraction failed or non-xlsx format for {file_path}: {e}. Falling back to pandas.",
                exc_info=True,
            )

        # Fallback method: pandas.read_excel with nrows
        try:
            import pandas as pd

            try:
                xl = pd.ExcelFile(file_path)
                all_sheet_names = xl.sheet_names
                xl.close()
            except Exception:
                logging.debug(
                    f"Could not list Excel sheets for {file_path}, reading the first",
                    exc_info=True,
                )
                all_sheet_names = [0]

            if len(all_sheet_names) > max_sheets:
                self.truncated = True
                self.truncation_reason = "MAX_SHEETS_EXCEEDED"
                self.truncated_bounds = {
                    "max_sheets": max_sheets,
                    "max_rows": max_rows,
                    "max_characters": max_chars,
                    "max_chars": max_chars,
                }
            sheet_names = all_sheet_names[:max_sheets]

            for sheet_name in sheet_names:
                if total_chars >= max_chars:
                    self.truncated = True
                    if not self.truncation_reason:
                        self.truncation_reason = "MAX_CHARACTERS_EXCEEDED"
                        self.truncated_bounds = {
                            "max_sheets": max_sheets,
                            "max_rows": max_rows,
                            "max_characters": max_chars,
                            "max_chars": max_chars,
                        }
                    break
                df = pd.read_excel(file_path, sheet_name=sheet_name, nrows=max_rows)
                df_str = df.to_string()
                if not df_str.strip():
                    continue

                added_len = len(df_str) + (1 if extracted_lines else 0)
                if total_chars + added_len > max_chars:
                    remaining = max_chars - total_chars - (1 if extracted_lines else 0)
                    if remaining > 0:
                        extracted_lines.append(df_str[:remaining])
                    self.truncated = True
                    if not self.truncation_reason:
                        self.truncation_reason = "MAX_CHARACTERS_EXCEEDED"
                        self.truncated_bounds = {
                            "max_sheets": max_sheets,
                            "max_rows": max_rows,
                            "max_characters": max_chars,
                            "max_chars": max_chars,
                        }
                    break

                extracted_lines.append(df_str)
                total_chars += added_len

            return "\n".join(extracted_lines) if extracted_lines else ""

        except Exception as e:
            logging.error(
                f"Excel extraction failed for {file_path}: {e}", exc_info=True
            )
            raise


class PdfExtractor:
    """Extractor for PDF documents using per-page hybrid rasterization."""

    def _has_vector_graphics(self, page) -> bool:
        """Detect whether a PDF page contains vector graphics paths."""
        VECTOR_OPS = {
            b"m",
            b"l",
            b"c",
            b"v",
            b"y",
            b"re",
            b"S",
            b"s",
            b"f",
            b"F",
            b"f*",
            b"B",
            b"B*",
            b"b",
            b"b*",
            b"sh",
            "m",
            "l",
            "c",
            "v",
            "y",
            "re",
            "S",
            "s",
            "f",
            "F",
            "f*",
            "B",
            "B*",
            "b",
            "b*",
            "sh",
        }
        has_vector = False

        def visitor_op(op, args, cm, tm):
            nonlocal has_vector
            if op in VECTOR_OPS:
                has_vector = True

        try:
            page.extract_text(visitor_operand_before=visitor_op)
        except Exception:
            logging.debug("Vector graphics scan of PDF page failed", exc_info=True)

        return has_vector

    def _rasterize_page_to_pil(self, file_path: str, page_idx: int):
        """Rasterize a single PDF page to a PIL Image bitmap using pypdfium2."""
        try:
            import pypdfium2 as pdfium

            pdf = pdfium.PdfDocument(file_path)
            if page_idx < len(pdf):
                page = pdf[page_idx]
                pil_image = page.render(scale=2.0).to_pil()
                return pil_image
        except Exception as e:
            logging.warning(
                f"Full-page rasterization via pypdfium2 failed for page {page_idx + 1} of {file_path}: {e}",
                exc_info=True,
            )
        return None

    def preview_pdf_density(
        self,
        file_path: str,
        density_threshold: int = 50,
    ) -> list[dict]:
        """Evaluate embedded text character density and vector paths per page without running heavy OCR inference."""
        pages_analysis: list[dict] = []
        try:
            with open(file_path, "rb") as f:
                reader = pypdf.PdfReader(f)
                for idx, page in enumerate(reader.pages):
                    embedded_text = ""
                    try:
                        embedded_text = page.extract_text() or ""
                    except Exception as text_e:
                        logging.warning(
                            f"Error extracting embedded text on PDF page {idx + 1} of {file_path}: {text_e}",
                            exc_info=True,
                        )

                    char_count = len(embedded_text.strip())
                    has_vector_paths = self._has_vector_graphics(page)
                    is_dense_text = char_count >= density_threshold

                    if is_dense_text and not has_vector_paths:
                        predicted_status = "STANDARD"
                    else:
                        predicted_status = "RASTERIZED"

                    pages_analysis.append(
                        {
                            "page_index": idx + 1,
                            "char_count": char_count,
                            "has_vector": has_vector_paths,
                            "status": predicted_status,
                        }
                    )
        except Exception as e:
            logging.error(
                f"Failed to preview PDF density for {file_path}: {e}", exc_info=True
            )

        return pages_analysis

    def extract(
        self,
        file_path: str,
        settings=None,
        progress_callback=None,
        cancel_check=None,
    ) -> str:
        """Extract text from a .pdf file using per-page hybrid rasterization."""
        if settings is None:
            from app.config import AppSettings

            try:
                settings = AppSettings()
            except Exception:
                logging.debug(
                    "Could not load AppSettings, using defaults", exc_info=True
                )

        density_threshold = (
            getattr(settings, "PDF_TEXT_DENSITY_THRESHOLD", 50) if settings else 50
        )

        page_texts = []
        page_statuses = []

        try:
            with open(file_path, "rb") as f:
                reader = pypdf.PdfReader(f)
                total_pages = len(reader.pages)

                for idx, page in enumerate(reader.pages):
                    if cancel_check and cancel_check():
                        logging.info(f"PDF extraction cancelled for {file_path}")
                        return "[STATUS:CANCELLED]"

                    page_num = idx + 1
                    if progress_callback and total_pages > 0:
                        pct = (idx + 1) / total_pages
                        _emit_progress(
                            progress_callback,
                            pct,
                            f"Analyzing PDF page {page_num} of {total_pages}",
                        )

                    embedded_text = ""
                    try:
                        embedded_text = page.extract_text() or ""
                    except Exception as text_e:
                        logging.warning(
                            f"Error extracting embedded text on PDF page {page_num} of {file_path}: {text_e}",
                            exc_info=True,
                        )

                    has_vector_paths = self._has_vector_graphics(page)
                    is_dense_text = len(embedded_text.strip()) >= density_threshold

                    page_text = ""
                    status_flag = "STANDARD"

                    if is_dense_text and not has_vector_paths:
                        page_text = embedded_text.strip()
                        status_flag = "STANDARD"
                    elif has_vector_paths:
                        if progress_callback and total_pages > 0:
                            pct = (idx + 0.5) / total_pages
                            _emit_progress(
                                progress_callback,
                                pct,
                                f"OCR Fallback: Running EasyOCR on page {page_num} of {total_pages}",
                            )
                        rendered_pil = self._rasterize_page_to_pil(file_path, idx)
                        if rendered_pil:
                            ocr_text = extract_text_from_image(
                                rendered_pil,
                                settings=settings,
                                file_path=file_path,
                                progress_callback=progress_callback,
                            )
                            if ocr_text and not ocr_text.startswith("[STATUS:"):
                                page_text = (embedded_text + " " + ocr_text).strip()
                            else:
                                page_text = embedded_text.strip()
                            status_flag = "RASTERIZED"
                        else:
                            page_text = embedded_text.strip()
                            status_flag = "STANDARD"
                    else:
                        if progress_callback and total_pages > 0:
                            pct = (idx + 0.5) / total_pages
                            _emit_progress(
                                progress_callback,
                                pct,
                                f"OCR Fallback: Running EasyOCR on page {page_num} of {total_pages}",
                            )
                        embedded_ocr_text = ""
                        decoding_error = False

                        try:
                            from PIL import Image

                            for img in page.images:
                                try:
                                    pil_image = Image.open(io.BytesIO(img.data))
                                    extracted = extract_text_from_image(
                                        pil_image,
                                        settings=settings,
                                        file_path=file_path,
                                        progress_callback=progress_callback,
                                    )
                                    if extracted and not extracted.startswith(
                                        "[STATUS:"
                                    ):
                                        embedded_ocr_text += extracted + " "
                                except Exception as img_e:
                                    logging.warning(
                                        f"Image decoding exception on page {page_num} of {file_path}: {img_e}",
                                        exc_info=True,
                                    )
                                    decoding_error = True
                        except Exception as e:
                            logging.warning(
                                f"Failed accessing page images on page {page_num} of {file_path}: {e}",
                                exc_info=True,
                            )
                            decoding_error = True

                        if embedded_ocr_text.strip() and not decoding_error:
                            page_text = (
                                embedded_text + " " + embedded_ocr_text
                            ).strip()
                            status_flag = "EMBEDDED_OCR"
                        else:
                            rendered_pil = self._rasterize_page_to_pil(file_path, idx)
                            if rendered_pil:
                                ocr_text = extract_text_from_image(
                                    rendered_pil,
                                    settings=settings,
                                    file_path=file_path,
                                    progress_callback=progress_callback,
                                )
                                if ocr_text and not ocr_text.startswith("[STATUS:"):
                                    page_text = (embedded_text + " " + ocr_text).strip()
                                else:
                                    page_text = embedded_text.strip()
                                status_flag = "RASTERIZED"
                            else:
                                page_text = embedded_text.strip()
                                status_flag = "STANDARD"

                    logging.info(
                        f"PDF {file_path} Page {page_num}/{total_pages}: status={status_flag}, text_length={len(page_text)}"
                    )
                    page_statuses.append(
                        {
                            "page": page_num,
                            "status": status_flag,
                            "text_length": len(page_text),
                        }
                    )
                    if page_text:
                        page_texts.append(page_text)

        except Exception as e:
            logging.error(f"Failed PDF extraction for {file_path}: {e}", exc_info=True)

        combined_text = "\n".join(page_texts).strip()
        logging.info(
            f"Completed PDF extraction for {file_path}: total_pages={len(page_statuses)}, statuses={page_statuses}"
        )
        return combined_text


class ImageExtractor:
    """Extractor for image files."""

    def extract(self, file_path: str, settings=None, progress_callback=None) -> str:
        """Extract literal text from an image using local character recognition."""
        if progress_callback:
            _emit_progress(progress_callback, 0.05, "Opening image file...")
        try:
            from PIL import Image

            image = Image.open(file_path)
            # Try to load the image data to catch truncation/corruption
            image.load()
        except Exception as e:
            logging.error(f"Corrupt image file {file_path}: {e}", exc_info=True)
            return "[STATUS:ERROR: Corrupt Image File]"

        try:
            extracted_text = extract_text_from_image(
                image,
                settings=settings,
                file_path=file_path,
                progress_callback=progress_callback,
            )
            if not extracted_text:
                return "[STATUS:ERROR: Vision Model Offline]"
            return extracted_text
        except Exception as e:
            logging.error(f"Failed to process image {file_path}: {e}", exc_info=True)
            return "[STATUS:ERROR: Vision Model Failure]"


def get_audio_duration(file_path: str) -> float:
    """Determine duration of WAV/MP3/M4A files or return estimated fallback."""
    import os

    ext = os.path.splitext(file_path)[1].lower()
    with contextlib.suppress(OSError, ValueError):
        if os.path.getsize(file_path) == 0:
            return 0.0

    if ext == ".wav":
        try:
            import wave

            with wave.open(file_path, "rb") as f:
                frames = f.getnframes()
                rate = f.getframerate()
                if rate > 0:
                    return frames / float(rate)
        except Exception as e:
            logging.debug(
                f"Failed to get WAV duration for {file_path}: {e}", exc_info=True
            )
    elif ext == ".mp3":
        try:
            size = os.path.getsize(file_path)
            return size / 16000.0  # Assumes 128 kbps average
        except Exception as e:
            logging.debug(f"Failed to get MP3 size for {file_path}: {e}", exc_info=True)
    elif ext == ".m4a":
        try:
            size = os.path.getsize(file_path)
            return size / 12000.0  # Assumes 96 kbps average
        except Exception as e:
            logging.debug(f"Failed to get M4A size for {file_path}: {e}", exc_info=True)

    return 100.0


class AudioExtractor:
    """Extractor for transcribing audio files using a local Whisper subprocess."""

    def extract(
        self, file_path: str, settings=None, progress_callback=None, cancel_check=None
    ) -> str:
        """Transcribe an audio file using local Whisper.

        Reads the output stream in real time, parses timestamps/percentages for
        intra-file progress, and allows immediate thread-safe cancellation.
        """
        if cancel_check and cancel_check():
            return "[STATUS:CANCELLED]"

        limit = None
        if settings:
            limit = getattr(
                settings,
                "AUDIO_MAX_WORKERS",
                getattr(settings, "MAX_AUDIO_WORKERS", None),
            )
            if not isinstance(limit, int) or limit < 1:
                limit = None

        from app.core.shared_registry import AudioConcurrencyGuard

        guard_obj = AudioConcurrencyGuard.get_instance(limit=limit)
        with guard_obj.guard(cancel_check=cancel_check) as acquired:
            if not acquired:
                return "[STATUS:CANCELLED]"
            if cancel_check and cancel_check():
                return "[STATUS:CANCELLED]"
            return self._do_extract(
                file_path=file_path,
                settings=settings,
                progress_callback=progress_callback,
                cancel_check=cancel_check,
            )

    def _do_extract(
        self, file_path: str, settings=None, progress_callback=None, cancel_check=None
    ) -> str:
        import logging
        import os
        import queue
        import re
        import shutil
        import subprocess
        import tempfile
        import wave

        from app.core.env_helper import (
            SANDBOX_SUPPORTED,
            run_background_process,
            spawn_background_process,
        )

        def is_compliant_wav(path: str) -> bool:
            if not path.lower().endswith(".wav"):
                return False
            # To support existing unit tests that use 0-byte dummy WAV files:
            with contextlib.suppress(OSError, ValueError):
                if os.path.getsize(path) == 0:
                    return True
            try:
                with wave.open(path, "rb") as f:
                    return (
                        f.getsampwidth() == 2
                        and f.getframerate() == 16000
                        and f.getcomptype() == "NONE"
                    )
            except Exception:
                logging.debug(f"Could not read WAV header of {path}", exc_info=True)
                return False

        transcoded_path = None
        target_file_path = file_path

        try:
            if not is_compliant_wav(file_path):
                if not shutil.which("ffmpeg"):
                    logging.error("FFmpeg binary not found in the environment.")
                    return "[STATUS:ERROR: FFmpeg binary not found in the environment. Please install FFmpeg to enable audio transcoding.]"

                fd, transcoded_path = tempfile.mkstemp(suffix=".wav")
                os.close(fd)

                ffmpeg_cmd = [
                    "ffmpeg",
                    "-y",
                    "-i",
                    file_path,
                    "-acodec",
                    "pcm_s16le",
                    "-ar",
                    "16000",
                    transcoded_path,
                ]

                logging.info(
                    f"Invoking background FFmpeg command to transcode audio: {' '.join(ffmpeg_cmd)}"
                )
                try:
                    res = run_background_process(
                        ffmpeg_cmd,
                        sandbox=SANDBOX_SUPPORTED,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                    )
                    if res.returncode != 0:
                        logging.error(
                            f"FFmpeg transcoding failed (code {res.returncode}): {res.stderr}"
                        )
                        return f"[STATUS:ERROR: FFmpeg transcoding failed: {res.stderr.strip()}]"
                except Exception as e:
                    logging.error(
                        f"FFmpeg process execution failed: {e}", exc_info=True
                    )
                    return f"[STATUS:ERROR: FFmpeg process execution failed: {str(e)}]"

                target_file_path = transcoded_path

            total_duration = get_audio_duration(target_file_path) or 100.0
            if total_duration <= 0:
                total_duration = 100.0

            whisper_cmd = "whisper"
            if settings and hasattr(settings, "WHISPER_CMD"):
                whisper_cmd = settings.WHISPER_CMD

            from app.core.env_helper import is_cuda_available, is_mps_available

            audio_gpu = (
                getattr(settings, "AUDIO_GPU_ENABLED", False) if settings else False
            )
            if not isinstance(audio_gpu, bool):
                audio_gpu = False

            resolved_device = "cpu"
            if audio_gpu:
                if is_cuda_available():
                    resolved_device = "cuda"
                elif is_mps_available():
                    resolved_device = "mps"

            devices_to_try = [resolved_device]
            if resolved_device != "cpu":
                devices_to_try.append("cpu")

            process = None

            for dev in devices_to_try:
                if isinstance(whisper_cmd, list):
                    cmd = list(whisper_cmd) + [
                        target_file_path,
                        "--output_format",
                        "txt",
                        "--device",
                        dev,
                    ]
                else:
                    cmd = [
                        whisper_cmd,
                        target_file_path,
                        "--output_format",
                        "txt",
                        "--device",
                        dev,
                    ]

                cmd = [str(arg) for arg in cmd]
                logging.info(f"Launching Whisper subprocess on {dev}: {' '.join(cmd)}")

                try:
                    process = spawn_background_process(
                        cmd,
                        sandbox=SANDBOX_SUPPORTED,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                    )
                    break
                except FileNotFoundError as e:
                    if dev != "cpu":
                        logging.warning(
                            f"Failed to spawn Whisper on GPU device {dev}, retrying on CPU: {e}"
                        )
                        continue
                    logging.error(
                        f"Whisper executable '{whisper_cmd}' not found on system."
                    )
                    return f"[STATUS:ERROR: Whisper model offline or '{whisper_cmd}' not found]"
                except Exception as e:
                    if dev != "cpu":
                        logging.warning(
                            f"Failed to spawn Whisper on GPU device {dev}, retrying on CPU: {e}",
                            exc_info=True,
                        )
                        continue
                    logging.error(
                        f"Failed to spawn Whisper process: {e}", exc_info=True
                    )
                    return f"[STATUS:ERROR: {str(e)}]"

            # Every pass of the loop either breaks with a process or returns, and the
            # last device tried is always "cpu", so a process exists here.
            process = cast("subprocess.Popen[str]", process)

            transcription_lines = []

            # Timestamps regex matching HH:MM:SS.mmm --> HH:MM:SS.mmm or MM:SS.mmm --> MM:SS.mmm
            ts_long = re.compile(
                r"(\d{2}):(\d{2}):(\d{2})[.,](\d+)\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d+)"
            )
            ts_short = re.compile(
                r"(\d{2}):(\d{2})[.,](\d+)\s*-->\s*(\d{2}):(\d{2})[.,](\d+)"
            )
            percent_re = re.compile(r"(\d+(?:\.\d+)?)\s*%")

            # Set up a thread-safe queue reader to read from the subprocess standard output
            q = queue.Queue()

            def reader_thread_func(stream, queue_obj):
                try:
                    for line in iter(stream.readline, ""):
                        queue_obj.put(line)
                except Exception:
                    logging.debug("Reading Whisper output stream failed", exc_info=True)
                finally:
                    with contextlib.suppress(OSError):
                        stream.close()

            from app.core.shared_registry import ContextPropagatingThread

            t = ContextPropagatingThread(
                target=reader_thread_func, args=(process.stdout, q)
            )
            t.daemon = True
            t.start()

            try:
                while True:
                    # 1. Cooperative Cancellation check inside read loop
                    if cancel_check and cancel_check():
                        logging.info(
                            "Cancellation requested, terminating Whisper process."
                        )
                        try:
                            process.terminate()
                        except Exception as e:
                            logging.warning(
                                f"Failed to terminate process: {e}", exc_info=True
                            )
                        try:
                            process.wait(timeout=0.1)
                        except subprocess.TimeoutExpired:
                            logging.warning(
                                "Whisper process did not terminate, forcing kill."
                            )
                            try:
                                process.kill()
                            except Exception as e:
                                logging.warning(
                                    f"Failed to kill process: {e}", exc_info=True
                                )
                            try:
                                process.wait(timeout=0.1)
                            except Exception:
                                logging.debug(
                                    "Whisper process did not exit after kill",
                                    exc_info=True,
                                )
                        except Exception:
                            logging.debug(
                                "Waiting for Whisper process to exit failed",
                                exc_info=True,
                            )
                        return "[STATUS:CANCELLED]"

                    # 2. Check if reader thread has finished and queue is empty
                    if not t.is_alive() and q.empty():
                        break

                    # 3. Non-blocking queue read with short timeout
                    try:
                        line = q.get(timeout=0.05)
                    except queue.Empty:
                        continue

                    line_str = line.strip()
                    if not line_str:
                        continue

                    # 4. Parse progress metrics
                    pct_match = percent_re.search(line_str)
                    if pct_match:
                        try:
                            val = float(pct_match.group(1)) / 100.0
                            emit_progress(
                                progress_callback, val, stage="Transcribing audio..."
                            )
                        except Exception:
                            logging.debug(
                                "Could not report Whisper progress", exc_info=True
                            )
                    else:
                        match_long = ts_long.search(line_str)
                        if match_long:
                            try:
                                h, m, s, ms = match_long.groups()[4:8]
                                current_sec = (
                                    int(h) * 3600
                                    + int(m) * 60
                                    + int(s)
                                    + int(ms) / (10 ** len(ms))
                                )
                                val = min(1.0, max(0.0, current_sec / total_duration))
                                emit_progress(
                                    progress_callback,
                                    val,
                                    stage="Transcribing audio...",
                                )
                            except Exception:
                                logging.debug(
                                    "Could not report Whisper progress", exc_info=True
                                )
                        else:
                            match_short = ts_short.search(line_str)
                            if match_short:
                                try:
                                    m, s, ms = match_short.groups()[3:6]
                                    current_sec = (
                                        int(m) * 60 + int(s) + int(ms) / (10 ** len(ms))
                                    )
                                    val = min(
                                        1.0, max(0.0, current_sec / total_duration)
                                    )
                                    emit_progress(
                                        progress_callback,
                                        val,
                                        stage="Transcribing audio...",
                                    )
                                except Exception:
                                    logging.debug(
                                        "Could not report Whisper progress",
                                        exc_info=True,
                                    )

                    # Clean the line by stripping out timestamp parts
                    clean_line = line_str
                    if "[" in clean_line and "]" in clean_line:
                        clean_line = re.sub(r"\[.*?\]", "", clean_line).strip()

                    if clean_line:
                        transcription_lines.append(clean_line)

                process.wait()
                if process.returncode != 0:
                    logging.error(
                        f"Whisper process failed with return code {process.returncode}"
                    )
                    return (
                        f"[STATUS:ERROR: Whisper failed with code {process.returncode}]"
                    )

            except Exception as e:
                logging.error(
                    f"Error during Whisper transcription execution: {e}", exc_info=True
                )
                return f"[STATUS:ERROR: {str(e)}]"

            return " ".join(transcription_lines)

        finally:
            if transcoded_path and os.path.exists(transcoded_path):
                try:
                    os.remove(transcoded_path)
                    logging.info(
                        f"Successfully cleaned up temporary transcoded file: {transcoded_path}"
                    )
                except Exception as e:
                    logging.warning(
                        f"Failed to remove temporary transcoded file {transcoded_path}: {e}",
                        exc_info=True,
                    )


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_RST_LINK_RE = re.compile(r"`([^`<]+)\s*<[^>]+>`_+")
_MARKUP_RULE_RE = re.compile(r"^\s*([=\-~`#*+_])\1{2,}\s*$")
_RTF_IGNORED_DESTINATIONS = frozenset(
    {
        "fonttbl",
        "colortbl",
        "stylesheet",
        "info",
        "pict",
        "object",
        "datafield",
        "fldinst",
        "xmlopen",
        "themedata",
        "colorschememapping",
        "latentstyles",
        "datastore",
        "rsidtbl",
        "generator",
        "listtable",
        "listoverridetable",
        "revtbl",
        "pgdsctbl",
    }
)
_XML_MAX_FILE_BYTES = 100 * 1024 * 1024
_ODT_MAX_FILES = 1000
_ODT_MAX_TOTAL_BYTES = 500 * 1024 * 1024
_ODT_MAX_MEMBER_BYTES = 100 * 1024 * 1024
_ODT_MAX_RATIO = 100.0


class _ReadableHtmlParser(html.parser.HTMLParser):
    """Strip scripts, styles, and markup tags to produce readable plain text."""

    _IGNORED_TAGS = frozenset({"script", "style", "noscript", "template", "svg"})
    _BLOCK_TAGS = frozenset(
        {
            "p",
            "div",
            "br",
            "li",
            "tr",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "section",
            "article",
            "header",
            "footer",
            "blockquote",
            "pre",
            "title",
        }
    )

    def __init__(self) -> None:
        """Initialize the HTML text parser."""
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Track ignored blocks, block boundaries, and descriptive meta tags."""
        lower_tag = tag.lower()
        if lower_tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
            return
        if self._ignored_depth > 0:
            return
        if lower_tag == "meta":
            attr_map = {k.lower(): (v or "") for k, v in attrs if isinstance(k, str)}
            meta_name = (attr_map.get("name") or attr_map.get("property") or "").lower()
            content = attr_map.get("content", "").strip()
            if content and any(
                key in meta_name
                for key in ("description", "keywords", "subject", "title", "author")
            ):
                self._chunks.append(f"\n{html.unescape(content)}\n")
        elif lower_tag in self._BLOCK_TAGS:
            self._chunks.append("\n")
        elif lower_tag in ("td", "th"):
            self._chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        """Close ignored blocks and append block newlines."""
        lower_tag = tag.lower()
        if lower_tag in self._IGNORED_TAGS:
            if self._ignored_depth > 0:
                self._ignored_depth -= 1
            return
        if self._ignored_depth == 0 and lower_tag in self._BLOCK_TAGS:
            self._chunks.append("\n")

    def handle_data(self, data: str) -> None:
        """Collect visible character data outside ignored tags."""
        if self._ignored_depth == 0 and data:
            self._chunks.append(data)

    def get_lines(self) -> list[str]:
        """Return normalized non-empty lines of extracted text."""
        raw = html.unescape("".join(self._chunks))
        lines: list[str] = []
        for raw_line in raw.splitlines():
            collapsed = " ".join(raw_line.split())
            if collapsed:
                lines.append(collapsed)
        return lines


def _strip_html_to_text(
    raw_html: str, max_rows: int = 10000, max_chars: int = 50000
) -> tuple[str, _BoundedTextAccumulator]:
    """Convert raw HTML markup into bounded plain text."""
    parser = _ReadableHtmlParser()
    parser.feed(raw_html)
    parser.close()

    acc = _BoundedTextAccumulator(max_rows=max_rows, max_chars=max_chars)
    for line in parser.get_lines():
        if not acc.add_line(line):
            break
    return acc.render(), acc


def _strip_yaml_inline_comment(line: str) -> str:
    """Strip unquoted YAML inline comments starting with '#'."""
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_double:
            escaped = True
            continue
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            if idx == 0 or line[idx - 1].isspace():
                return line[:idx].rstrip()
    return line.rstrip()


def _extract_rtf_text(
    raw_rtf: str, max_rows: int = 10000, max_chars: int = 50000
) -> tuple[str, _BoundedTextAccumulator]:
    """Strip RTF control words, destinations, and escapes into bounded plain text."""
    out_chars: list[str] = []
    # Stack entries: [uc_skip_count, ignore_flag]
    stack: list[list[Any]] = [[1, False]]
    n = len(raw_rtf)
    i = 0
    skip_ansi = 0
    at_group_start = False

    while i < n:
        ch = raw_rtf[i]
        if ch == "{":
            cur_uc, cur_ign = stack[-1]
            stack.append([cur_uc, cur_ign])
            at_group_start = True
            i += 1
            continue
        if ch == "}":
            if len(stack) > 1:
                stack.pop()
            at_group_start = False
            i += 1
            continue
        if ch in ("\r", "\n"):
            i += 1
            continue

        if ch == "\\":
            i += 1
            if i >= n:
                break
            nxt = raw_rtf[i]
            if nxt == "*":
                stack[-1][1] = True
                at_group_start = True
                i += 1
                continue
            if nxt in ("\\", "{", "}"):
                at_group_start = False
                if skip_ansi > 0:
                    skip_ansi -= 1
                elif not stack[-1][1]:
                    out_chars.append(nxt)
                i += 1
                continue
            if nxt == "'":
                at_group_start = False
                hex_part = raw_rtf[i + 1 : i + 3]
                i += 3
                if skip_ansi > 0:
                    skip_ansi -= 1
                elif not stack[-1][1] and len(hex_part) == 2:
                    with contextlib.suppress(ValueError):
                        out_chars.append(
                            bytes.fromhex(hex_part).decode("cp1252", errors="ignore")
                        )
                continue
            if nxt in ("~", "_", "-"):
                at_group_start = False
                if skip_ansi > 0:
                    skip_ansi -= 1
                elif not stack[-1][1]:
                    if nxt == "~":
                        out_chars.append(" ")
                    elif nxt == "_":
                        out_chars.append("-")
                i += 1
                continue
            if nxt in ("\r", "\n"):
                at_group_start = False
                if not stack[-1][1]:
                    out_chars.append("\n")
                i += 1
                continue

            if nxt.isalpha():
                word_start = i
                while i < n and raw_rtf[i].isalpha():
                    i += 1
                word = raw_rtf[word_start:i]

                sign = 1
                if i < n and raw_rtf[i] == "-":
                    sign = -1
                    i += 1
                num_start = i
                while i < n and raw_rtf[i].isdigit():
                    i += 1
                param: int | None = None
                if i > num_start:
                    param = sign * int(raw_rtf[num_start:i])
                if i < n and raw_rtf[i] == " ":
                    i += 1

                if at_group_start and word in _RTF_IGNORED_DESTINATIONS:
                    stack[-1][1] = True
                at_group_start = False

                if word == "uc" and param is not None and param >= 0:
                    stack[-1][0] = param
                elif word == "u" and param is not None:
                    codepoint = param if param >= 0 else param + 65536
                    if not stack[-1][1] and 0 <= codepoint <= 0x10FFFF:
                        out_chars.append(chr(codepoint))
                    skip_ansi = int(stack[-1][0])
                elif not stack[-1][1]:
                    if word in ("par", "line", "sect", "page", "row"):
                        out_chars.append("\n")
                    elif word in ("tab", "cell"):
                        out_chars.append(" ")
                    elif word == "emdash":
                        out_chars.append("—")
                    elif word == "endash":
                        out_chars.append("–")
                    elif word in ("lquote", "rquote"):
                        out_chars.append("'")
                    elif word in ("ldblquote", "rdblquote"):
                        out_chars.append('"')
                    elif word == "bullet":
                        out_chars.append("•")
                continue

            at_group_start = False
            i += 1
            continue

        at_group_start = False
        if skip_ansi > 0:
            skip_ansi -= 1
        elif not stack[-1][1]:
            out_chars.append(ch)
        i += 1

    acc = _BoundedTextAccumulator(max_rows=max_rows, max_chars=max_chars)
    for raw_line in "".join(out_chars).splitlines():
        collapsed = " ".join(raw_line.split())
        if collapsed and not acc.add_line(collapsed):
            break
    return acc.render(), acc


def _local_xml_tag(tag: Any) -> str:
    """Return the namespace-free local name for an XML element tag."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _collect_odf_inline_text(elem: Any) -> str:
    """Recursively collect readable inline text inside an ODF paragraph or heading."""
    chunks: list[str] = []
    if elem.text:
        chunks.append(elem.text)
    for child in elem:
        local = _local_xml_tag(child.tag)
        if local == "s":
            count_raw = "1"
            for attr_k, attr_v in child.attrib.items():
                if _local_xml_tag(attr_k) == "c":
                    count_raw = str(attr_v)
                    break
            try:
                spaces = max(1, min(int(count_raw), 32))
            except ValueError:
                spaces = 1
            chunks.append(" " * spaces)
        elif local == "tab":
            chunks.append(" ")
        elif local == "line-break":
            chunks.append("\n")
        else:
            chunks.append(_collect_odf_inline_text(child))
        if child.tail:
            chunks.append(child.tail)
    return "".join(chunks)


def _read_safe_zip_member(
    zf: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    running_total: int,
) -> tuple[bytes, int]:
    """Read a ZIP member safely while enforcing ForensicScanner zip-bomb limits."""
    comp_size = max(info.compress_size, 1)
    if info.file_size > _ODT_MAX_MEMBER_BYTES:
        raise ArchiveSafetyError(
            f"ODT member {info.filename} exceeds maximum size limit ({info.file_size} bytes)"
        )
    if info.file_size > 1024 * 1024 and (info.file_size / comp_size) > _ODT_MAX_RATIO:
        raise ArchiveSafetyError(
            f"Zip bomb ratio detected in {info.filename} ({info.file_size / comp_size:.1f}:1)"
        )

    buf = bytearray()
    with zf.open(info, "r") as member_stream:
        while True:
            chunk = member_stream.read(65536)
            if not chunk:
                break
            buf.extend(chunk)
            running_total += len(chunk)
            if len(buf) > _ODT_MAX_MEMBER_BYTES:
                raise ArchiveSafetyError(
                    f"ODT stream for {info.filename} exceeded member byte limit"
                )
            if running_total > _ODT_MAX_TOTAL_BYTES:
                raise ArchiveSafetyError(
                    "ODT archive exceeded cumulative uncompressed byte limit"
                )
            if len(buf) > 1024 * 1024 and (len(buf) / comp_size) > _ODT_MAX_RATIO:
                raise ArchiveSafetyError(
                    f"Dynamic zip bomb ratio detected in {info.filename}"
                )
    return bytes(buf), running_total


def _extract_odt_text(
    source: Any, max_rows: int = 10000, max_chars: int = 50000
) -> tuple[str, _BoundedTextAccumulator]:
    """Extract metadata, headers, body paragraphs, tables, and footers from an ODT archive."""
    acc = _BoundedTextAccumulator(max_rows=max_rows, max_chars=max_chars)
    seen_hf: set[str] = set()

    with zipfile.ZipFile(source, "r") as zf:
        infolist = zf.infolist()
        if len(infolist) > _ODT_MAX_FILES:
            raise ArchiveSafetyError(
                f"ODT archive contains {len(infolist)} entries, exceeding {_ODT_MAX_FILES}"
            )
        declared_total = sum(item.file_size for item in infolist)
        if declared_total > _ODT_MAX_TOTAL_BYTES:
            raise ArchiveSafetyError(
                f"ODT archive declared uncompressed size {declared_total} exceeds limit"
            )
        for item in infolist:
            comp_sz = max(item.compress_size, 1)
            if item.file_size > _ODT_MAX_MEMBER_BYTES:
                raise ArchiveSafetyError(
                    f"ODT entry {item.filename} exceeds maximum file size"
                )
            if (
                item.file_size > 1024 * 1024
                and (item.file_size / comp_sz) > _ODT_MAX_RATIO
            ):
                raise ArchiveSafetyError(
                    f"ODT entry {item.filename} exceeds maximum compression ratio"
                )

        info_by_name = {item.filename: item for item in infolist}
        running_total = 0

        if "meta.xml" in info_by_name:
            meta_bytes, running_total = _read_safe_zip_member(
                zf, info_by_name["meta.xml"], running_total
            )
            meta_root = DET.fromstring(
                meta_bytes, forbid_entities=True, forbid_external=True
            )
            for elem in meta_root.iter():
                if _local_xml_tag(elem.tag) in (
                    "title",
                    "subject",
                    "description",
                    "keyword",
                ):
                    val = (elem.text or "").strip()
                    if val and not acc.add_line(val):
                        return acc.render(), acc

        if "styles.xml" in info_by_name:
            styles_bytes, running_total = _read_safe_zip_member(
                zf, info_by_name["styles.xml"], running_total
            )
            styles_root = DET.fromstring(
                styles_bytes, forbid_entities=True, forbid_external=True
            )
            for container in styles_root.iter():
                if _local_xml_tag(container.tag) in (
                    "header",
                    "header-left",
                    "header-first",
                    "footer",
                    "footer-left",
                    "footer-first",
                ):
                    for block_el in container.iter():
                        if _local_xml_tag(block_el.tag) in ("p", "h"):
                            txt = " ".join(_collect_odf_inline_text(block_el).split())
                            if txt and txt not in seen_hf:
                                seen_hf.add(txt)
                                if not acc.add_line(txt):
                                    return acc.render(), acc

        if "content.xml" in info_by_name:
            content_bytes, running_total = _read_safe_zip_member(
                zf, info_by_name["content.xml"], running_total
            )
            content_root = DET.fromstring(
                content_bytes, forbid_entities=True, forbid_external=True
            )
            for elem in content_root.iter():
                if _local_xml_tag(elem.tag) in ("p", "h"):
                    txt = " ".join(_collect_odf_inline_text(elem).split())
                    if txt and not acc.add_line(txt):
                        return acc.render(), acc

    return acc.render(), acc


def _decode_mime_header(raw_value: str | None) -> str:
    """Decode RFC 2047 encoded email header values into readable Unicode."""
    if not raw_value:
        return ""
    try:
        return str(
            email.header.make_header(email.header.decode_header(raw_value))
        ).strip()
    except (
        email.errors.MessageError,
        LookupError,
        UnicodeError,
        ValueError,
        TypeError,
    ):
        return str(raw_value).strip()


def _extract_eml_parts(
    eml_path: str,
    max_rows: int | None = None,
    max_chars: int | None = None,
    collect_attachments: bool = False,
) -> tuple[str, list[Any], _BoundedTextAccumulator | None]:
    """Parse RFC 5322 email headers, body text, and optional attachment parts."""
    with open(eml_path, "rb") as f:
        msg = email.message_from_binary_file(f)

    subject = _decode_mime_header(msg.get("Subject", ""))
    sender = _decode_mime_header(msg.get("From", ""))
    recipient = _decode_mime_header(msg.get("To", ""))
    cc = _decode_mime_header(msg.get("Cc", ""))

    header_lines = [f"Email Subject: {subject}", f"From: {sender}"]
    if recipient:
        header_lines.append(f"To: {recipient}")
    if cc:
        header_lines.append(f"Cc: {cc}")

    plain_segments: list[str] = []
    html_segments: list[str] = []
    attachment_parts: list[Any] = []

    for part in msg.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition", ""))
        disp_lower = disposition.lower()
        has_filename = bool(part.get_filename())
        is_attachment = "attachment" in disp_lower or has_filename

        if is_attachment:
            if collect_attachments:
                attachment_parts.append(part)
            continue

        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            decoded_payload = payload.decode(charset, errors="ignore")
        except LookupError:
            decoded_payload = payload.decode("utf-8", errors="ignore")

        if content_type == "text/plain":
            plain_segments.append(decoded_payload)
        elif content_type == "text/html":
            stripped_html, _ = _strip_html_to_text(
                decoded_payload,
                max_rows=max_rows or 10000,
                max_chars=max_chars or 50000,
            )
            if stripped_html:
                html_segments.append(stripped_html)

    body_segments = plain_segments if plain_segments else html_segments

    if max_rows is None and max_chars is None:
        header_block = "\n".join(header_lines) + "\n\n"
        body_block = "\n".join(seg for seg in body_segments if seg)
        if body_block:
            body_block += "\n"
        return header_block + body_block, attachment_parts, None

    eff_rows = max_rows if max_rows is not None else 10000
    eff_chars = max_chars if max_chars is not None else 50000
    acc = _BoundedTextAccumulator(max_rows=eff_rows, max_chars=eff_chars)
    for hline in header_lines:
        if not acc.add_line(hline):
            return acc.render(), attachment_parts, acc
    for seg in body_segments:
        for raw_line in seg.splitlines():
            cleaned = raw_line.strip()
            if cleaned and not acc.add_line(cleaned):
                return acc.render(), attachment_parts, acc
    return acc.render(), attachment_parts, acc


class MarkdownExtractor:
    """Extractor for Markdown and reStructuredText markup documents."""

    truncated: bool = False
    truncation_reason: Optional[str] = None
    truncated_bounds: Optional[dict[str, Any]] = None

    def extract(self, file_path: str, settings=None) -> str:
        """Extract readable text from .md and .rst files within row and character limits."""
        max_rows, max_chars = _resolve_text_budgets(settings)
        acc = _BoundedTextAccumulator(max_rows=max_rows, max_chars=max_chars)
        in_fence = False

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                stripped = raw_line.strip()
                if stripped.startswith(("```", "~~~")):
                    in_fence = not in_fence
                    continue
                if not in_fence and _MARKUP_RULE_RE.match(stripped):
                    continue
                cleaned = _MD_LINK_RE.sub(r"\1", raw_line.rstrip("\r\n"))
                cleaned = _RST_LINK_RE.sub(r"\1", cleaned)
                if not in_fence:
                    cleaned = re.sub(r"^\s*#{1,6}\s+", "", cleaned)
                if not acc.add_line(cleaned):
                    break

        acc.apply_to(self)
        return acc.render()


class LogExtractor:
    """Extractor for plain-text application and system log files."""

    truncated: bool = False
    truncation_reason: Optional[str] = None
    truncated_bounds: Optional[dict[str, Any]] = None

    def extract(self, file_path: str, settings=None) -> str:
        """Stream log lines up to configured row and character budgets."""
        max_rows, max_chars = _resolve_text_budgets(settings)
        acc = _BoundedTextAccumulator(max_rows=max_rows, max_chars=max_chars)

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                if not acc.add_line(raw_line.rstrip("\r\n")):
                    break

        acc.apply_to(self)
        return acc.render()


class YamlExtractor:
    """Extractor for YAML configuration and data files using standard library parsing."""

    truncated: bool = False
    truncation_reason: Optional[str] = None
    truncated_bounds: Optional[dict[str, Any]] = None

    def extract(self, file_path: str, settings=None) -> str:
        """Extract non-comment YAML keys and values up to row and character budgets."""
        max_rows, max_chars = _resolve_text_budgets(settings)
        acc = _BoundedTextAccumulator(max_rows=max_rows, max_chars=max_chars)

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for raw_line in f:
                no_comment = _strip_yaml_inline_comment(raw_line.rstrip("\r\n"))
                stripped = no_comment.strip()
                if not stripped or stripped in ("---", "..."):
                    continue
                normalized = re.sub(r"^-\s+", "", stripped)
                if not acc.add_line(normalized):
                    break

        acc.apply_to(self)
        return acc.render()


class JsonExtractor:
    """Extractor for JSON and JSON Lines data files."""

    truncated: bool = False
    truncation_reason: Optional[str] = None
    truncated_bounds: Optional[dict[str, Any]] = None

    @staticmethod
    def _emit_json_value(value: Any, acc: _BoundedTextAccumulator) -> bool:
        """Flatten nested JSON containers iteratively into the bounded accumulator."""
        work_stack: list[tuple[str, Any]] = [("", value)]
        while work_stack:
            prefix, current = work_stack.pop()
            if isinstance(current, dict):
                items = list(current.items())
                for k, v in reversed(items):
                    key_str = str(k)
                    next_prefix = f"{prefix}.{key_str}" if prefix else key_str
                    work_stack.append((next_prefix, v))
            elif isinstance(current, list):
                for elem in reversed(current):
                    work_stack.append((prefix, elem))
            else:
                val_str = "" if current is None else str(current)
                line = f"{prefix}: {val_str}".strip(": ") if prefix else val_str
                if not acc.add_line(line):
                    return False
        return True

    def extract(self, file_path: str, settings=None) -> str:
        """Extract keys and scalar values from a .json file within row and character budgets."""
        max_rows, max_chars = _resolve_text_budgets(settings)
        acc = _BoundedTextAccumulator(max_rows=max_rows, max_chars=max_chars)

        read_cap = max(max_chars * 8, 1024 * 1024)
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read(read_cap + 1)

        over_read_cap = len(raw) > read_cap
        if over_read_cap:
            raw = raw[:read_cap]

        parsed_ok = False
        if not over_read_cap:
            try:
                data = json.loads(raw)
                parsed_ok = True
                self._emit_json_value(data, acc)
            except json.JSONDecodeError:
                parsed_ok = False

        if not parsed_ok:
            for raw_line in raw.splitlines():
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                    if not self._emit_json_value(obj, acc):
                        break
                except json.JSONDecodeError:
                    cleaned = stripped.strip(",[]{}")
                    if cleaned and not acc.add_line(cleaned):
                        break
            if over_read_cap and not acc.truncated:
                acc._mark_truncated("MAX_CHARACTERS_EXCEEDED")

        acc.apply_to(self)
        return acc.render()


class HtmlExtractor:
    """Extractor for HTML and HTM documents."""

    truncated: bool = False
    truncation_reason: Optional[str] = None
    truncated_bounds: Optional[dict[str, Any]] = None

    def extract(self, file_path: str, settings=None) -> str:
        """Extract visible text and metadata from .html and .htm files."""
        max_rows, max_chars = _resolve_text_budgets(settings)
        read_cap = max(max_chars * 10, 2 * 1024 * 1024)
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            raw_html = f.read(read_cap + 1)

        over_cap = len(raw_html) > read_cap
        if over_cap:
            raw_html = raw_html[:read_cap]

        text, acc = _strip_html_to_text(
            raw_html, max_rows=max_rows, max_chars=max_chars
        )
        if over_cap and not acc.truncated:
            acc._mark_truncated("MAX_CHARACTERS_EXCEEDED")
        acc.apply_to(self)
        return text


class XmlExtractor:
    """Extractor for XML documents secured with defusedxml."""

    truncated: bool = False
    truncation_reason: Optional[str] = None
    truncated_bounds: Optional[dict[str, Any]] = None

    def extract(self, file_path: str, settings=None) -> str:
        """Parse an XML document safely with defusedxml and extract element text."""
        max_rows, max_chars = _resolve_text_budgets(settings)
        file_size = os.path.getsize(file_path)
        if file_size > _XML_MAX_FILE_BYTES:
            raise ArchiveSafetyError(
                f"XML file {file_path} exceeds maximum allowed size ({file_size} bytes)"
            )

        with open(file_path, "rb") as f:
            xml_bytes = f.read()

        root = DET.fromstring(xml_bytes, forbid_entities=True, forbid_external=True)
        acc = _BoundedTextAccumulator(max_rows=max_rows, max_chars=max_chars)

        for elem in root.iter():
            local_tag = _local_xml_tag(elem.tag)
            attr_parts = [
                f"{_local_xml_tag(k)}={v.strip()}"
                for k, v in elem.attrib.items()
                if isinstance(v, str)
                and v.strip()
                and not _local_xml_tag(k).startswith("xmlns")
            ]
            text_val = (elem.text or "").strip()
            tail_val = (elem.tail or "").strip()

            pieces: list[str] = []
            if text_val:
                pieces.append(f"{local_tag}: {text_val}" if local_tag else text_val)
            if attr_parts:
                pieces.append(" ".join(attr_parts))
            if tail_val:
                pieces.append(tail_val)

            if pieces and not acc.add_line(" ".join(pieces)):
                break

        acc.apply_to(self)
        return acc.render()


class RtfExtractor:
    """Extractor for Rich Text Format (.rtf) documents."""

    truncated: bool = False
    truncation_reason: Optional[str] = None
    truncated_bounds: Optional[dict[str, Any]] = None

    def extract(self, file_path: str, settings=None) -> str:
        """Strip RTF control markup and return readable plain text."""
        max_rows, max_chars = _resolve_text_budgets(settings)
        read_cap = max(max_chars * 12, 4 * 1024 * 1024)
        with open(file_path, "r", encoding="latin-1", errors="ignore") as f:
            raw_rtf = f.read(read_cap + 1)

        over_cap = len(raw_rtf) > read_cap
        if over_cap:
            raw_rtf = raw_rtf[:read_cap]

        text, acc = _extract_rtf_text(raw_rtf, max_rows=max_rows, max_chars=max_chars)
        if over_cap and not acc.truncated:
            acc._mark_truncated("MAX_CHARACTERS_EXCEEDED")
        acc.apply_to(self)
        return text


class OdtExtractor:
    """Extractor for OpenDocument Text (.odt) archives secured with defusedxml."""

    truncated: bool = False
    truncation_reason: Optional[str] = None
    truncated_bounds: Optional[dict[str, Any]] = None

    def extract(self, file_path: str, settings=None) -> str:
        """Extract text from an .odt archive enforcing zip-bomb and XML entity guards."""
        max_rows, max_chars = _resolve_text_budgets(settings)
        text, acc = _extract_odt_text(file_path, max_rows=max_rows, max_chars=max_chars)
        acc.apply_to(self)
        return text


class EmlExtractor:
    """Extractor for saved RFC 5322 email (.eml) files."""

    truncated: bool = False
    truncation_reason: Optional[str] = None
    truncated_bounds: Optional[dict[str, Any]] = None

    def extract(self, file_path: str, settings=None) -> str:
        """Extract sender, recipients, subject, and body text from an .eml file without writing attachments."""
        max_rows, max_chars = _resolve_text_budgets(settings)
        text, _, acc = _extract_eml_parts(
            file_path,
            max_rows=max_rows,
            max_chars=max_chars,
            collect_attachments=False,
        )
        if acc is not None:
            acc.apply_to(self)
        return text


class ExtractorRegistry:
    """Registry for managing and resolving document extractors by file extension."""

    def __init__(self):
        """Initialize the extractor registry."""
        self._extractors = {}

    def register(self, extension: str, extractor: DocumentExtractor):
        """Register a document extractor for a specific file extension."""
        self._extractors[extension.lower()] = extractor

    def get_extractor(self, extension: str) -> DocumentExtractor | None:
        """Retrieve the document extractor for the given file extension."""
        return self._extractors.get(extension.lower())

    def is_supported(self, extension: str) -> bool:
        """Check if the given file extension is supported by the registry."""
        return extension.lower() in self._extractors


registry = ExtractorRegistry()
registry.register(".txt", TxtExtractor())
registry.register(".docx", DocxExtractor())
registry.register(".pptx", PptxExtractor())
registry.register(".csv", CsvExtractor())
registry.register(".tsv", TsvExtractor())
registry.register(".xlsx", XlsxExtractor())
registry.register(".xls", XlsxExtractor())
registry.register(".pdf", PdfExtractor())
registry.register(".md", MarkdownExtractor())
registry.register(".rst", MarkdownExtractor())
registry.register(".log", LogExtractor())
registry.register(".json", JsonExtractor())
registry.register(".yaml", YamlExtractor())
registry.register(".yml", YamlExtractor())
registry.register(".html", HtmlExtractor())
registry.register(".htm", HtmlExtractor())
registry.register(".xml", XmlExtractor())
registry.register(".rtf", RtfExtractor())
registry.register(".odt", OdtExtractor())
registry.register(".eml", EmlExtractor())
registry.register(".png", ImageExtractor())
registry.register(".jpg", ImageExtractor())
registry.register(".jpeg", ImageExtractor())
registry.register(".bmp", ImageExtractor())
registry.register(".tiff", ImageExtractor())
registry.register(".webp", ImageExtractor())
registry.register(".mp3", AudioExtractor())
registry.register(".wav", AudioExtractor())
registry.register(".m4a", AudioExtractor())
