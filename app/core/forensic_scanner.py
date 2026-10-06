"""Forensic scanner for deep drive discovery, archive unpacking, and provenance tracking.

Extracts documents from filesystems, compressed archives (.zip, .tar, .tar.gz),
and email files (.eml, .msg), calculating SHA-256 digests for chain-of-custody tracking.
"""

import email
import hashlib
import logging
import os
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from app.core.exceptions import ArchiveSafetyError
from app.core.extractor import extract_file_text
from app.core.progress import emit_progress
from app.core.resilient_file_ops import _set_posix_mode, resilient_rmtree

logger = logging.getLogger(__name__)


SUPPORTED_DOC_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".doc",
    ".txt",
    ".csv",
    ".xlsx",
    ".xls",
    ".rtf",
    ".eml",
    ".msg",
}

ARCHIVE_EXTENSIONS = {
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".tar.gz",
    ".bz2",
}


@dataclass
class DiscoveredDocument:
    """Represents a document discovered during a forensic drive scan."""

    source_path: str
    relative_path: str
    file_name: str
    file_size_bytes: int
    sha256_hash: str
    archive_origin: Optional[str] = None
    extracted_text: str = ""
    is_duplicate: bool = False
    duplicate_of: Optional[str] = None
    staging_file_path: Optional[str] = None


class ForensicScanner:
    """Scans storage volumes, extracts archives, and builds cryptographically verified document registries."""

    def __init__(
        self,
        temp_staging_dir: Optional[str] = None,
        max_files: int = 1000,
        max_total_size_bytes: int = 500 * 1024 * 1024,
        max_file_size_bytes: int = 100 * 1024 * 1024,
        max_ratio: float = 100.0,
    ):
        self.staging_dir = temp_staging_dir or tempfile.mkdtemp(
            prefix="sortify_forensic_"
        )
        self.max_files = max_files
        self.max_total_size_bytes = max_total_size_bytes
        self.max_file_size_bytes = max_file_size_bytes
        self.max_ratio = max_ratio
        self.discovered_documents: List[DiscoveredDocument] = []
        self.seen_hashes: Dict[str, str] = {}  # sha256 -> source_path
        self.seen_texts: Dict[str, str] = {}  # sha256 -> extracted text
        self._is_owned_staging_dir = temp_staging_dir is None

    @staticmethod
    def compute_sha256(filepath: str) -> str:
        """Compute SHA-256 cryptographic digest of a file."""
        from app.core.resilient_file_ops import resilient_file_hash

        return resilient_file_hash(filepath)

    def unpack_archive(
        self,
        archive_path: str,
        destination_dir: str,
        max_files: Optional[int] = None,
        max_total_size_bytes: Optional[int] = None,
        max_file_size_bytes: Optional[int] = None,
        max_ratio: Optional[float] = None,
    ) -> List[str]:
        """Safely unpack compressed archives (.zip, .tar, .tgz) into staging destination with streaming limits."""
        eff_max_files = max_files if max_files is not None else self.max_files
        eff_max_total_size = (
            max_total_size_bytes
            if max_total_size_bytes is not None
            else self.max_total_size_bytes
        )
        eff_max_file_size = (
            max_file_size_bytes
            if max_file_size_bytes is not None
            else self.max_file_size_bytes
        )
        eff_max_ratio = max_ratio if max_ratio is not None else self.max_ratio

        extracted_files = []
        created_paths = []
        os.makedirs(destination_dir, exist_ok=True)

        archive_size = 0
        if os.path.exists(archive_path):
            try:
                archive_size = os.path.getsize(archive_path)
            except Exception:
                archive_size = 0

        file_count = 0
        total_uncompressed_bytes = 0
        chunk_size = 64 * 1024  # 64 KB constant chunked buffer

        def check_limits(chunk_len: int, current_file_bytes: int):
            nonlocal total_uncompressed_bytes
            current_file_bytes += chunk_len
            total_uncompressed_bytes += chunk_len

            if current_file_bytes > eff_max_file_size:
                raise ArchiveSafetyError(
                    f"File uncompressed size ({current_file_bytes} bytes) exceeds limit ({eff_max_file_size} bytes)"
                )
            if total_uncompressed_bytes > eff_max_total_size:
                raise ArchiveSafetyError(
                    f"Total uncompressed archive size ({total_uncompressed_bytes} bytes) exceeds limit ({eff_max_total_size} bytes)"
                )
            if archive_size > 0:
                ratio = total_uncompressed_bytes / archive_size
                if ratio > eff_max_ratio:
                    raise ArchiveSafetyError(
                        f"Compression expansion ratio ({ratio:.2f}:1) exceeds limit ({eff_max_ratio:.1f}:1)"
                    )
            elif total_uncompressed_bytes > 0:
                raise ArchiveSafetyError(
                    f"Compression expansion ratio exceeds limit ({eff_max_ratio:.1f}:1) for 0-byte archive"
                )

            return current_file_bytes

        try:
            if zipfile.is_zipfile(archive_path):
                with zipfile.ZipFile(archive_path, "r") as zf:
                    for member in zf.namelist():
                        # Sanitize paths against directory traversal (Zip Slip)
                        if member.startswith("/") or ".." in member:
                            continue
                        target = os.path.abspath(os.path.join(destination_dir, member))
                        dest_abs = os.path.abspath(destination_dir)
                        if not target.startswith(dest_abs):
                            continue

                        if member.endswith("/"):
                            os.makedirs(target, exist_ok=True)
                            created_paths.append(target)
                        else:
                            file_count += 1
                            if file_count > eff_max_files:
                                raise ArchiveSafetyError(
                                    f"Archive file count ({file_count}) exceeds limit ({eff_max_files})"
                                )
                            os.makedirs(os.path.dirname(target), exist_ok=True)
                            created_paths.append(target)

                            curr_file_size = 0
                            with zf.open(member) as src, open(target, "wb") as dst:
                                while True:
                                    chunk = src.read(chunk_size)
                                    if not chunk:
                                        break
                                    curr_file_size = check_limits(
                                        len(chunk), curr_file_size
                                    )
                                    dst.write(chunk)

                            _set_posix_mode(target, 0o600)
                            extracted_files.append(target)

            elif tarfile.is_tarfile(archive_path):
                with tarfile.open(archive_path, "r:*") as tf:
                    for member in tf.getmembers():
                        if member.name.startswith("/") or ".." in member.name:
                            continue
                        target = os.path.abspath(
                            os.path.join(destination_dir, member.name)
                        )
                        dest_abs = os.path.abspath(destination_dir)
                        if not target.startswith(dest_abs):
                            continue

                        if member.isdir():
                            os.makedirs(target, exist_ok=True)
                            created_paths.append(target)
                        elif member.isfile() or member.isreg():
                            file_count += 1
                            if file_count > eff_max_files:
                                raise ArchiveSafetyError(
                                    f"Archive file count ({file_count}) exceeds limit ({eff_max_files})"
                                )
                            os.makedirs(os.path.dirname(target), exist_ok=True)
                            created_paths.append(target)

                            curr_file_size = 0
                            src = tf.extractfile(member)
                            if src is not None:
                                with src, open(target, "wb") as dst:
                                    while True:
                                        chunk = src.read(chunk_size)
                                        if not chunk:
                                            break
                                        curr_file_size = check_limits(
                                            len(chunk), curr_file_size
                                        )
                                        dst.write(chunk)
                                _set_posix_mode(target, 0o600)
                                extracted_files.append(target)

        except ArchiveSafetyError as ase:
            logger.warning(
                f"Archive safety limit exceeded during extraction of {archive_path}: {ase}"
            )
            for p in reversed(created_paths):
                try:
                    if os.path.isfile(p) or os.path.islink(p):
                        os.remove(p)
                    elif os.path.isdir(p):
                        os.rmdir(p)
                except Exception as cleanup_err:
                    logger.debug(f"Cleanup error for {p}: {cleanup_err}")
            raise ase

        except Exception as e:
            logger.warning(f"Error unpacking archive {archive_path}: {e}")

        return extracted_files

    def parse_eml_file(
        self, eml_path: str, destination_dir: str
    ) -> tuple[str, List[str]]:
        """Extract body text and attachments from an .eml email file."""
        extracted_attachments = []
        body_text = ""

        try:
            with open(eml_path, "rb") as f:
                msg = email.message_from_binary_file(f)

            subject = msg.get("Subject", "")
            sender = msg.get("From", "")
            body_text = f"Email Subject: {subject}\nFrom: {sender}\n\n"

            for part in msg.walk():
                content_type = part.get_content_type()
                disposition = str(part.get("Content-Disposition", ""))

                if content_type == "text/plain" and "attachment" not in disposition:
                    charset = part.get_content_charset() or "utf-8"
                    payload = part.get_payload(decode=True)
                    if payload:
                        body_text += payload.decode(charset, errors="ignore") + "\n"

                elif "attachment" in disposition or part.get_filename():
                    filename = part.get_filename()
                    if filename:
                        os.makedirs(destination_dir, exist_ok=True)
                        att_path = os.path.join(destination_dir, filename)
                        payload = part.get_payload(decode=True)
                        if payload:
                            with open(att_path, "wb") as af:
                                af.write(payload)
                            extracted_attachments.append(att_path)
        except Exception as e:
            logger.warning(f"Failed to parse email {eml_path}: {e}")

        return body_text, extracted_attachments

    def scan_drive(
        self,
        source_root: str,
        progress_callback: Optional[Callable[..., None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> List[DiscoveredDocument]:
        """Perform comprehensive forensic scan of a source drive or directory."""
        self.discovered_documents.clear()
        self.seen_hashes.clear()
        self.seen_texts.clear()

        source_root = os.path.abspath(source_root)
        count = 0

        for root, _, files in os.walk(source_root):
            for file in files:
                if cancel_check and cancel_check():
                    logger.info("Forensic scan cancelled by user.")
                    return self.discovered_documents

                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, source_root)
                _, ext = os.path.splitext(file)
                ext_lower = ext.lower()

                # 1. Handle Compressed Archives
                if ext_lower in ARCHIVE_EXTENSIONS or file.endswith(".tar.gz"):
                    archive_staging = os.path.join(
                        self.staging_dir,
                        "archives",
                        hashlib.md5(full_path.encode()).hexdigest(),
                    )
                    try:
                        extracted = self.unpack_archive(full_path, archive_staging)
                        for ext_file in extracted:
                            if os.path.isfile(ext_file):
                                _, e_ext = os.path.splitext(ext_file)
                                if e_ext.lower() in SUPPORTED_DOC_EXTENSIONS:
                                    self._ingest_file(
                                        source_path=full_path,
                                        actual_file_path=ext_file,
                                        rel_path=os.path.relpath(ext_file, archive_staging),
                                        archive_origin=rel_path,
                                    )
                                    count += 1
                                    emit_progress(
                                        progress_callback,
                                        stage=f"Unpacked: {os.path.basename(ext_file)}",
                                        unit_count=count,
                                        unit_type="items",
                                    )
                    except ArchiveSafetyError as ase:
                        logger.warning(
                            f"Forensic scan skipped unsafe archive '{full_path}': {ase}"
                        )

                # 2. Handle EML Emails
                elif ext_lower == ".eml":
                    email_staging = os.path.join(
                        self.staging_dir,
                        "emails",
                        hashlib.md5(full_path.encode()).hexdigest(),
                    )
                    email_body, attachments = self.parse_eml_file(
                        full_path, email_staging
                    )

                    # Ingest email body as document
                    self._ingest_file(
                        source_path=full_path,
                        actual_file_path=full_path,
                        rel_path=rel_path,
                        pre_extracted_text=email_body,
                    )
                    count += 1

                    # Ingest attachments
                    for att in attachments:
                        self._ingest_file(
                            source_path=full_path,
                            actual_file_path=att,
                            rel_path=f"{rel_path} -> {os.path.basename(att)}",
                            archive_origin=rel_path,
                        )
                        count += 1
                    emit_progress(
                        progress_callback,
                        stage=f"Email parsed: {file}",
                        unit_count=count,
                        unit_type="items",
                    )

                # 3. Handle Standard Supported Documents
                elif ext_lower in SUPPORTED_DOC_EXTENSIONS:
                    self._ingest_file(
                        source_path=full_path,
                        actual_file_path=full_path,
                        rel_path=rel_path,
                    )
                    count += 1
                    if count % 5 == 0:
                        emit_progress(
                            progress_callback,
                            stage=f"Discovered: {file}",
                            unit_count=count,
                            unit_type="items",
                        )

        return self.discovered_documents

    def _ingest_file(
        self,
        source_path: str,
        actual_file_path: str,
        rel_path: str,
        archive_origin: Optional[str] = None,
        pre_extracted_text: Optional[str] = None,
    ) -> DiscoveredDocument:
        """Process a single file, calculate hash, extract text, and register document."""
        try:
            file_size = os.path.getsize(actual_file_path)
            sha256 = self.compute_sha256(actual_file_path)
        except Exception as e:
            logger.warning(f"Failed to read file metadata for {actual_file_path}: {e}")
            file_size = 0
            sha256 = "ERROR"

        is_dup = False
        dup_of = None
        if sha256 in self.seen_hashes:
            is_dup = True
            dup_of = self.seen_hashes[sha256]
        else:
            self.seen_hashes[sha256] = source_path

        # Text extraction
        if is_dup and sha256 != "ERROR" and pre_extracted_text is None:
            text = self.seen_texts.get(sha256, "")
        elif pre_extracted_text is not None:
            text = str(pre_extracted_text)
            if sha256 != "ERROR":
                self.seen_texts[sha256] = text
        else:
            try:
                text = str(extract_file_text(actual_file_path) or "")
            except Exception as e:
                logger.warning(f"Extraction error for {actual_file_path}: {e}")
                text = ""
            if sha256 != "ERROR":
                self.seen_texts[sha256] = text

        doc = DiscoveredDocument(
            source_path=source_path,
            relative_path=rel_path,
            file_name=os.path.basename(actual_file_path),
            file_size_bytes=file_size,
            sha256_hash=sha256,
            archive_origin=archive_origin,
            extracted_text=text,
            is_duplicate=is_dup,
            duplicate_of=dup_of,
            staging_file_path=actual_file_path,
        )
        self.discovered_documents.append(doc)
        return doc

    def cleanup(self):
        """Clean up temporary staging directories."""
        if self._is_owned_staging_dir and os.path.lexists(self.staging_dir):
            try:
                resilient_rmtree(self.staging_dir)
            except Exception as e:
                logger.warning(f"Error cleaning staging dir {self.staging_dir}: {e}")
