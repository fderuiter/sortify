"""Forensic scanner for deep drive discovery, archive unpacking, and provenance tracking.

Extracts documents from filesystems, compressed archives (.zip, .tar, .tar.gz),
and email files (.eml, .msg), calculating SHA-256 digests for chain-of-custody tracking.
"""

import email
import hashlib
import logging
import os
import shutil
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from app.core.extractor import extract_file_text
from app.core.resilient_file_ops import resilient_rmtree

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

    def __init__(self, temp_staging_dir: Optional[str] = None):
        self.staging_dir = temp_staging_dir or tempfile.mkdtemp(
            prefix="sortify_forensic_"
        )
        self.discovered_documents: List[DiscoveredDocument] = []
        self.seen_hashes: Dict[str, str] = {}  # sha256 -> source_path
        self._is_owned_staging_dir = temp_staging_dir is None

    @staticmethod
    def compute_sha256(filepath: str) -> str:
        """Compute SHA-256 cryptographic digest of a file."""
        hasher = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(65536):
                hasher.update(chunk)
        return hasher.hexdigest()

    def unpack_archive(self, archive_path: str, destination_dir: str) -> List[str]:
        """Safely unpack compressed archives (.zip, .tar, .tgz) into staging destination."""
        extracted_files = []
        os.makedirs(destination_dir, exist_ok=True)

        try:
            if zipfile.is_zipfile(archive_path):
                with zipfile.ZipFile(archive_path, "r") as zf:
                    # Sanitize paths against directory traversal (Zip Slip)
                    for member in zf.namelist():
                        if member.startswith("/") or ".." in member:
                            continue
                        target = os.path.join(destination_dir, member)
                        if member.endswith("/"):
                            os.makedirs(target, exist_ok=True)
                        else:
                            os.makedirs(os.path.dirname(target), exist_ok=True)
                            with zf.open(member) as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            extracted_files.append(target)

            elif tarfile.is_tarfile(archive_path):
                with tarfile.open(archive_path, "r:*") as tf:
                    for member in tf.getmembers():
                        if member.name.startswith("/") or ".." in member.name:
                            continue
                        tf.extract(member, path=destination_dir)
                        extracted_files.append(
                            os.path.join(destination_dir, member.name)
                        )
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
        progress_callback: Optional[Callable[[int, str], None]] = None,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> List[DiscoveredDocument]:
        """Perform comprehensive forensic scan of a source drive or directory."""
        self.discovered_documents.clear()
        self.seen_hashes.clear()

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
                                if progress_callback:
                                    progress_callback(
                                        count, f"Unpacked: {os.path.basename(ext_file)}"
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
                    if progress_callback:
                        progress_callback(count, f"Email parsed: {file}")

                # 3. Handle Standard Supported Documents
                elif ext_lower in SUPPORTED_DOC_EXTENSIONS:
                    self._ingest_file(
                        source_path=full_path,
                        actual_file_path=full_path,
                        rel_path=rel_path,
                    )
                    count += 1
                    if progress_callback and count % 5 == 0:
                        progress_callback(count, f"Discovered: {file}")

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
        if pre_extracted_text is not None:
            text = pre_extracted_text
        else:
            try:
                text = extract_file_text(actual_file_path) or ""
            except Exception as e:
                logger.warning(f"Extraction error for {actual_file_path}: {e}")
                text = ""

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
