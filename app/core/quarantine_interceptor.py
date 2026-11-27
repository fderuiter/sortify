"""Event-driven compliance interceptor service with quarantine staging and async pipeline."""

import contextlib
import logging
import os
import re
import shutil
import time
import unicodedata
import uuid
from typing import Any

from app.core.analyzer_strategies import redact_sensitive_text
from app.core.crypto import zero_vector_buffer
from app.core.db import Database
from app.core.domain_contracts import QuarantineRecordModel, validate_quarantine_record
from app.core.exceptions import ArchiveSafetyError
from app.core.extractor import extract_file_text
from app.core.forensic_scanner import ForensicScanner
from app.core.mover import get_safe_path, is_subpath_or_equal, overwrite_backup_dir
from app.core.path_utils import safe_relpath, validate_target_path
from app.core.policy_engine import PolicyEngine
from app.core.resilient_file_ops import (
    _set_posix_mode,
    atomic_quarantine_relocation,
    resilient_file_hash,
    resilient_move,
    resilient_remove,
)
from app.core.text_utils import sanitize_secret_patterns

logger = logging.getLogger(__name__)


def _is_content_approved_for_release(
    db: Any,
    base_dir: str = "",
    file_hash: str | None = None,
    file_path: str | None = None,
) -> bool:
    """Return True if an unchanged file's SHA-256 hash has an approved user release exemption."""
    if db is None:
        return False

    target_hash = file_hash
    if not target_hash and file_path and os.path.exists(file_path):
        try:
            target_hash = resilient_file_hash(file_path)
        except (OSError, ValueError) as exc:
            logger.debug(
                "Could not compute release-approval hash for '%s': %s", file_path, exc
            )
            return False

    if not target_hash:
        return False

    records: list[dict[str, Any]] = []
    try:
        if hasattr(db, "get_all_quarantine_records"):
            records = db.get_all_quarantine_records() or []
        elif hasattr(db, "get_quarantine_records_by_base_dir"):
            records = (
                db.get_quarantine_records_by_base_dir(base_dir, status="RELEASED") or []
            )
    except (OSError, ValueError, RuntimeError, AttributeError) as exc:
        logger.debug("Could not query release-approval records: %s", exc)
        return False

    for rec in records:
        if not isinstance(rec, dict):
            continue
        if (rec.get("status") or "").upper() != "RELEASED":
            continue
        if (rec.get("policy_action") or "").lower() != "release":
            continue

        if rec.get("file_hash") == target_hash:
            return True

        audit_log = rec.get("audit_log") or []
        if isinstance(audit_log, list):
            for entry in audit_log:
                if (
                    isinstance(entry, dict)
                    and entry.get("approved_file_hash") == target_hash
                ):
                    return True

    return False


SSN_PATTERN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
CREDIT_CARD_PATTERN = re.compile(r"\b(?:\d[ -]*?){13,16}\b")
PHONE_PATTERN = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}(?!\d)"
)
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
CLINICAL_PII_PATTERN = re.compile(
    r"(?i)(Confidential Medical Report|Subject \d+|Diagnosis:[^\n]*)"
)
INLINE_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b(?:api[_-]?key|secret(?:[_-]?key|[_-]?token)?|access[_-]?token|auth[_-]?token|bearer[_-]?token|password|passwd|token)\s*[:=]\s*(?:['\"][^'\"]+['\"]|[^\s,;'\"]+)"
)

_REDACTABLE_TEXT_EXTENSIONS = frozenset(
    {
        ".txt",
        ".md",
        ".csv",
        ".tsv",
        ".json",
        ".log",
        ".xml",
        ".yaml",
        ".yml",
        ".ini",
        ".cfg",
        ".conf",
        ".html",
        ".htm",
        ".rst",
    }
)
_REDACTABLE_OFFICE_EXTENSIONS = frozenset({".docx", ".xlsx"})


def _is_redactable_extension(file_path: str) -> bool:
    """Return True when file_path has a plain-text or supported OOXML extension."""
    ext = os.path.splitext(str(file_path or ""))[1].lower()
    return ext in _REDACTABLE_TEXT_EXTENSIONS or ext in _REDACTABLE_OFFICE_EXTENSIONS


def scrub_pii_from_text(text: Any) -> str:
    """Scrub PII, sensitive keywords, and cryptographic tokens from document text."""
    if not text:
        return ""

    text_str = str(text)

    # Standard prompt extract redaction
    redacted = redact_sensitive_text(text_str)

    # SSN pattern
    redacted = SSN_PATTERN.sub("[REDACTED_SSN]", redacted)
    # Credit Card pattern
    redacted = CREDIT_CARD_PATTERN.sub("[REDACTED_CARD]", redacted)
    # Phone number pattern
    redacted = PHONE_PATTERN.sub("[REDACTED_PHONE]", redacted)
    # Email pattern
    redacted = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", redacted)
    # Clinical/PII Sensitive phrases
    redacted = CLINICAL_PII_PATTERN.sub("[REDACTED_PII]", redacted)
    # Inline key/value secret assignments (supports both ':' and '=')
    redacted = INLINE_SECRET_ASSIGNMENT_PATTERN.sub("[REDACTED_SECRET]", redacted)

    # Centralized secret pattern redaction (API keys, JWT tokens, Bearer tokens, private keys, high-entropy secrets)
    redacted = sanitize_secret_patterns(redacted, replacement="[REDACTED_SECRET]")

    return redacted


def _redact_document_to_path(
    src_path: str,
    out_path: str,
    extra_regex: str | None = None,
) -> tuple[bool, str]:
    """Scrub sensitive content from supported text, .docx, and .xlsx files into out_path.

    Returns ``(True, scrubbed_text)`` when the container format is supported and
    valid, or ``(False, "")`` when ``src_path`` is a ``.pdf`` or other binary
    format that must be held intact in quarantine rather than corrupted.
    Filesystem ``OSError`` failures during reading or writing propagate to the caller.
    """
    ext = os.path.splitext(str(src_path or ""))[1].lower()
    compiled_extra: re.Pattern[str] | None = None
    if extra_regex:
        with contextlib.suppress(re.error):
            compiled_extra = re.compile(extra_regex, re.IGNORECASE)

    def _scrub_val(raw: str) -> str:
        res = scrub_pii_from_text(raw)
        if compiled_extra is not None:
            res = compiled_extra.sub("[REDACTED_POLICY]", res)
        return res

    if ext in _REDACTABLE_TEXT_EXTENSIONS:
        try:
            with open(src_path, encoding="utf-8") as fh:
                raw_text = fh.read()
        except UnicodeDecodeError:
            return False, ""
        scrubbed_text = _scrub_val(raw_text)
        out_dir = os.path.dirname(os.path.abspath(out_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(scrubbed_text)
        _set_posix_mode(out_path, 0o600)
        return True, scrubbed_text

    if ext == ".docx":
        import zipfile

        import docx
        from docx.opc.exceptions import PackageNotFoundError

        try:
            doc = docx.Document(src_path)
        except (PackageNotFoundError, zipfile.BadZipFile, KeyError, ValueError):
            return False, ""

        collected_chunks: list[str] = []

        def _scrub_paragraph(para: Any) -> None:
            runs = list(getattr(para, "runs", []) or [])
            for run in runs:
                if run.text:
                    run.text = _scrub_val(run.text)
            full_after = para.text or ""
            scrubbed_full = _scrub_val(full_after)
            if scrubbed_full != full_after:
                if runs:
                    runs[0].text = scrubbed_full
                    for extra_run in runs[1:]:
                        extra_run.text = ""
                else:
                    para.text = scrubbed_full
            if para.text:
                collected_chunks.append(para.text)

        def _scrub_table(table: Any) -> None:
            for row in table.rows:
                for cell in row.cells:
                    for para in cell.paragraphs:
                        _scrub_paragraph(para)
                    for nested in cell.tables:
                        _scrub_table(nested)

        for para in doc.paragraphs:
            _scrub_paragraph(para)
        for tbl in doc.tables:
            _scrub_table(tbl)
        for section in doc.sections:
            for container in (
                section.header,
                section.footer,
                section.first_page_header,
                section.first_page_footer,
                section.even_page_header,
                section.even_page_footer,
            ):
                if container is not None:
                    for para in container.paragraphs:
                        _scrub_paragraph(para)
                    for tbl in container.tables:
                        _scrub_table(tbl)

        core_props = getattr(doc, "core_properties", None)
        if core_props is not None:
            for prop_name in ("author", "comments", "subject", "title", "keywords"):
                val = getattr(core_props, prop_name, None)
                if isinstance(val, str) and val:
                    setattr(core_props, prop_name, _scrub_val(val))

        out_dir = os.path.dirname(os.path.abspath(out_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        doc.save(out_path)
        _set_posix_mode(out_path, 0o600)
        return True, "\n".join(collected_chunks)

    if ext == ".xlsx":
        import zipfile

        import openpyxl

        try:
            wb = openpyxl.load_workbook(src_path)
        except (zipfile.BadZipFile, KeyError, ValueError):
            return False, ""

        xlsx_chunks: list[str] = []
        for sheet in wb.worksheets:
            for row in sheet.iter_rows():
                for cell in row:
                    if isinstance(cell.value, str) and cell.value:
                        scrubbed_cell = _scrub_val(cell.value)
                        if scrubbed_cell != cell.value:
                            cell.value = scrubbed_cell
                        xlsx_chunks.append(str(cell.value))
                    if (
                        cell.comment
                        and isinstance(cell.comment.text, str)
                        and cell.comment.text
                    ):
                        cell.comment.text = _scrub_val(cell.comment.text)
                        xlsx_chunks.append(cell.comment.text)

        props = getattr(wb, "properties", None)
        if props is not None:
            for prop_name in ("creator", "description", "subject", "title", "keywords"):
                val = getattr(props, prop_name, None)
                if isinstance(val, str) and val:
                    setattr(props, prop_name, _scrub_val(val))

        out_dir = os.path.dirname(os.path.abspath(out_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        wb.save(out_path)
        wb.close()
        _set_posix_mode(out_path, 0o600)
        return True, "\n".join(xlsx_chunks)

    return False, ""


class QuarantineInterceptorService:
    """Asynchronous compliance interceptor service that isolates incoming files in quarantine staging.

    Executes deep forensic checks, PII redaction, and policy lifecycle actions off the main thread.
    """

    def __init__(
        self,
        db: Database,
        policies: list[dict[str, Any]] | None = None,
        worker_timeout: float = 300.0,
        jev_classifier: Any | None = None,
        auto_quarantine_ratings: list[str] | None = None,
        runtime_settings: Any | None = None,
    ):
        self.db = db
        self.policies = PolicyEngine.sort_policies(policies)
        self.worker_timeout = worker_timeout
        self.forensic_scanner = ForensicScanner()
        self.dlq_records: list[dict[str, Any]] = []
        if jev_classifier is not None:
            self.jev_classifier = jev_classifier
        else:
            try:
                from app.core.jev_classifier import JevClassifierEngine

                self.jev_classifier = JevClassifierEngine()
            except Exception:
                logger.warning(
                    "Jev classifier unavailable; sensitivity pre-classification disabled",
                    exc_info=True,
                )
                self.jev_classifier = None

        if auto_quarantine_ratings is not None:
            self.auto_quarantine_ratings = [r.upper() for r in auto_quarantine_ratings]
        elif runtime_settings and hasattr(runtime_settings, "AUTO_QUARANTINE_RATINGS"):
            self.auto_quarantine_ratings = [
                r.upper()
                for r in getattr(runtime_settings, "AUTO_QUARANTINE_RATINGS", [])
            ]
        else:
            self.auto_quarantine_ratings = []

    def resolve_safe_target_dir(
        self,
        base_dir: str,
        target_subfolder: str | None,
        default_subfolder: str = "",
    ) -> str:
        """Validate target subfolder and ensure containment within base_dir boundaries.

        Args:
            base_dir: The base parent directory path acting as the boundary.
            target_subfolder: Optional relative subfolder path requested for output.
            default_subfolder: Default relative subfolder path to fall back on if target_subfolder
                is invalid or escapes the base_dir boundary.

        Returns
        -------
            Normalized absolute or relative safe destination directory path strictly contained
            within base_dir.

        Notes
        -----
            If target_subfolder fails validation (e.g. contains illegal OS characters, absolute path
            roots, or directory traversal segments like '..') or escapes base_dir, this function logs
            a security warning and safely returns the fallback directory.
        """
        fallback_dir = os.path.normpath(
            os.path.join(base_dir, default_subfolder) if default_subfolder else base_dir
        )

        if not target_subfolder or not target_subfolder.strip():
            return fallback_dir

        try:
            validate_target_path(target_subfolder)
        except Exception as e:
            logger.warning(
                f"Security warning: Target subfolder '{target_subfolder}' failed validation ({e}). "
                f"Falling back to default directory '{fallback_dir}'.",
                exc_info=True,
            )
            return fallback_dir

        candidate_dir = os.path.normpath(os.path.join(base_dir, target_subfolder))

        try:
            if not is_subpath_or_equal(candidate_dir, base_dir):
                logger.warning(
                    f"Security warning: Target directory '{candidate_dir}' escapes base directory '{base_dir}'. "
                    f"Falling back to default directory '{fallback_dir}'."
                )
                return fallback_dir
        except Exception as e:
            logger.warning(
                f"Security warning: Error checking boundary containment for '{candidate_dir}' ({e}). "
                f"Falling back to default directory '{fallback_dir}'.",
                exc_info=True,
            )
            return fallback_dir

        return candidate_dir

    @staticmethod
    def _is_same_content(staged_path: str, dest_path: str) -> bool:
        """Whether `dest_path` already holds exactly the bytes staged at `staged_path`."""
        if not (os.path.isfile(staged_path) and os.path.isfile(dest_path)):
            return False
        if os.path.samefile(staged_path, dest_path):
            return False
        try:
            return resilient_file_hash(staged_path) == resilient_file_hash(dest_path)
        except Exception:
            logger.debug("Could not compare staged and release files", exc_info=True)
            return False

    def stage_incoming_file(
        self,
        source_path: str,
        base_dir: str,
        original_relative_path: str | None = None,
        policy_action: str | None = None,
    ) -> dict[str, Any]:
        """Immediately place incoming unverified file into temporary _Quarantine_Staging prior to compliance evaluation."""
        abs_source = os.path.abspath(source_path)
        if not os.path.exists(abs_source):
            raise FileNotFoundError(f"Source file not found for staging: {source_path}")

        abs_base_dir = (
            os.path.abspath(base_dir)
            if base_dir
            else os.path.abspath(os.path.dirname(abs_source))
        )

        job_id = f"qjob_{uuid.uuid4().hex[:12]}"
        file_name = os.path.basename(abs_source)
        rel_path = original_relative_path or file_name

        quarantine_dir = os.path.join(abs_base_dir, "_Quarantine_Staging")
        os.makedirs(quarantine_dir, exist_ok=True)
        _set_posix_mode(quarantine_dir, 0o700)

        staged_file_name = f"{job_id}_{file_name}"
        staged_path = os.path.abspath(os.path.join(quarantine_dir, staged_file_name))

        # Copy file to quarantine staging area
        shutil.copy2(abs_source, staged_path)
        _set_posix_mode(staged_path, 0o600)

        file_hash = None
        try:
            file_hash = resilient_file_hash(staged_path)
        except Exception:
            logger.warning(
                f"Could not hash staged quarantine file '{staged_path}'", exc_info=True
            )

        # Record atomic STAGED state transition in database
        self.db.stage_quarantine_record(
            job_id=job_id,
            base_dir=abs_base_dir,
            original_filepath=rel_path,
            staged_filepath=staged_path,
            file_hash=file_hash,
            policy_action=policy_action,
        )

        return {
            "job_id": job_id,
            "base_dir": abs_base_dir,
            "original_filepath": rel_path,
            "staged_filepath": staged_path,
            "file_hash": file_hash,
            "status": "STAGED",
        }

    def process_quarantine_job(
        self, job_id: str, timeout_override: float | None = None
    ) -> QuarantineRecordModel:
        """Execute deep forensic scanning, PII redaction, policy evaluation, and archival lifecycle actions."""
        return self._run_quarantine_job(
            job_id=job_id,
            timeout_override=timeout_override,
            phase0_only=False,
        )

    def _process_phase0_job(
        self, job_id: str, timeout_override: float | None = None
    ) -> QuarantineRecordModel:
        """Execute daemon Phase 0 safety checks and quarantine holds without routing non-quarantine policies."""
        return self._run_quarantine_job(
            job_id=job_id,
            timeout_override=timeout_override,
            phase0_only=True,
        )

    def _delete_phase0_record(self, job_id: str) -> None:
        """Remove a transient Phase 0 staging record when SortPipeline will execute downstream routing."""
        try:
            from app.core.db import get_db_connection

            db_path = getattr(self.db, "db_path", None)
            worker = getattr(self.db, "worker", None)
            if db_path and worker and hasattr(worker, "execute_write"):

                def _delete_row() -> None:
                    conn = get_db_connection(db_path)
                    with conn:
                        conn.execute(
                            "DELETE FROM quarantine_records WHERE job_id = ?",
                            (job_id,),
                        )

                worker.execute_write(_delete_row)
        except (OSError, ValueError, RuntimeError, AttributeError):
            logger.debug("Could not prune transient Phase 0 record %s", job_id)

    def _run_quarantine_job(
        self,
        job_id: str,
        timeout_override: float | None = None,
        phase0_only: bool = False,
    ) -> QuarantineRecordModel:
        """Run quarantine job with optional Phase 0 safety-and-hold restriction."""
        record = self.db.get_quarantine_record(job_id)
        if not isinstance(record, dict):
            record = None
        if not record:
            raise ValueError(f"Quarantine record not found for job_id: {job_id}")

        effective_timeout = (
            timeout_override if timeout_override is not None else self.worker_timeout
        )
        start_time = time.perf_counter()

        # 1. State transition: STAGED -> IN_INSPECTION
        self.db.update_quarantine_status(
            job_id=job_id,
            status="IN_INSPECTION",
            audit_entry={
                "timestamp": time.time(),
                "status": "IN_INSPECTION",
                "details": "Background worker started forensic inspection and policy evaluation",
            },
        )

        staged_path = record["staged_filepath"]
        base_dir = record["base_dir"]
        orig_rel_path = record["original_filepath"]
        forensic_extracted_files: list[str] = []

        if base_dir:
            base_dir = os.path.abspath(base_dir)
        if staged_path and not os.path.isabs(staged_path) and base_dir:
            candidate_staged = os.path.normpath(os.path.join(base_dir, staged_path))
            if os.path.exists(candidate_staged) or not os.path.exists(staged_path):
                staged_path = candidate_staged

        try:
            # Check timeout guardrail
            if (
                effective_timeout <= 0
                or (time.perf_counter() - start_time) >= effective_timeout
            ):
                raise TimeoutError(
                    f"Forensic scanning job exceeded timeout of {effective_timeout}s"
                )

            # 2. Deep Forensic Scanning & Extraction
            extracted_text = ""
            if os.path.exists(staged_path):
                ext = os.path.splitext(staged_path)[1].lower()
                if ext in (".zip", ".tar", ".gz", ".tgz", ".tar.gz"):
                    try:
                        extracted_files = self.forensic_scanner.unpack_archive(
                            staged_path, os.path.dirname(staged_path)
                        )
                        forensic_extracted_files = list(extracted_files)
                        extracted_texts = []
                        for ef in extracted_files:
                            if os.path.isfile(ef):
                                _set_posix_mode(ef, 0o600)
                                t = str(extract_file_text(ef) or "")
                                if t:
                                    extracted_texts.append(t)
                        extracted_text = "\n".join(extracted_texts)
                    except ArchiveSafetyError as ase:
                        logger.warning(
                            f"Archive safety limit error in job {job_id}: {ase}"
                        )
                        err_msg = f"Archive safety violation: {ase}"
                        self.db.update_quarantine_status(
                            job_id=job_id,
                            status="MANUAL_REVIEW_REQUIRED",
                            error_message=err_msg,
                            audit_entry={
                                "timestamp": time.time(),
                                "status": "MANUAL_REVIEW_REQUIRED",
                                "details": f"Archive safety limit exceeded: {ase}",
                            },
                        )
                        dlq_item = self.db.get_quarantine_record(job_id)
                        if dlq_item:
                            self.dlq_records.append(dlq_item)
                            return validate_quarantine_record(dlq_item)
                        fallback = {
                            "job_id": job_id,
                            "status": "MANUAL_REVIEW_REQUIRED",
                            "error_message": err_msg,
                            "error": err_msg,
                        }
                        return validate_quarantine_record(fallback)
                else:
                    extracted_text = str(extract_file_text(staged_path) or "")

            # Re-check timeout guardrail
            if (
                effective_timeout <= 0
                or (time.perf_counter() - start_time) >= effective_timeout
            ):
                raise TimeoutError(
                    f"Forensic scanning job exceeded timeout of {effective_timeout}s"
                )

            # 3. Dynamic Plugin Lifecycle Scan Hooks
            try:
                from app.core.plugin_registry import PluginRegistry

                PluginRegistry.get_instance().trigger_quarantine_scan(
                    orig_rel_path=orig_rel_path,
                    extracted_text=extracted_text,
                    base_dir=base_dir,
                )
            except Exception as e:
                logger.warning(
                    f"Plugin quarantine scan execution warning: {e}", exc_info=True
                )

            # 4. Extended Policy Engine Evaluation
            orig_full_for_eval = (
                os.path.join(base_dir, orig_rel_path)
                if base_dir and orig_rel_path
                else ""
            )
            eval_abs_path = (
                orig_full_for_eval
                if (orig_full_for_eval and os.path.isfile(orig_full_for_eval))
                else staged_path
            )
            matched_rule = PolicyEngine.evaluate_policies(
                file_path=orig_rel_path,
                doc_text=extracted_text,
                status_match="",
                policies=self.policies,
                is_sorted=True,
                abs_file_path=eval_abs_path,
            )

            action = (
                (
                    getattr(matched_rule, "action", "")
                    or (
                        matched_rule.get("action", "")
                        if isinstance(matched_rule, dict)
                        else ""
                    )
                ).lower()
                if matched_rule
                else (record.get("policy_action") or "").lower()
            )

            raw_target_subfolder = (
                getattr(matched_rule, "target_path", None)
                or (
                    matched_rule.get("target_path")
                    if isinstance(matched_rule, dict)
                    else None
                )
                if matched_rule
                else None
            )
            pol_rename_pattern = (
                getattr(matched_rule, "rename_pattern", "")
                or (
                    matched_rule.get("rename_pattern", "")
                    if isinstance(matched_rule, dict)
                    else ""
                )
                if matched_rule
                else ""
            )
            pol_resolved_date = (
                getattr(matched_rule, "resolved_date", None)
                or (
                    matched_rule.get("resolved_date")
                    if isinstance(matched_rule, dict)
                    else None
                )
                if matched_rule
                else None
            )

            target_subfolder = raw_target_subfolder
            if target_subfolder and "{" in str(target_subfolder):
                from app.core.pattern_formatter import PatternTokenFormatter

                with contextlib.suppress(ValueError):
                    target_subfolder = PatternTokenFormatter.format_target_path(
                        str(target_subfolder),
                        metadata={
                            "date": pol_resolved_date,
                            "category": str(target_subfolder).split("/")[0]
                            or "uncategorized",
                        },
                        seq=1,
                        fallback_original=os.path.basename(orig_rel_path),
                        file_path=eval_abs_path,
                    )

            target_basename = os.path.basename(orig_rel_path)
            if (
                pol_rename_pattern
                and str(pol_rename_pattern).strip()
                and action in ("redact", "archive", "retain", "allow", "release")
            ):
                from app.core.pattern_formatter import PatternTokenFormatter

                leaf_cat = (
                    str(target_subfolder or action or "uncategorized")
                    .replace("\\", "/")
                    .strip("/")
                    .split("/")[-1]
                    or "uncategorized"
                )
                target_basename = PatternTokenFormatter.format_pattern(
                    str(pol_rename_pattern),
                    metadata={
                        "date": pol_resolved_date,
                        "category": leaf_cat,
                    },
                    seq=1,
                    fallback_original=os.path.basename(orig_rel_path),
                    file_path=eval_abs_path,
                )

            # Re-check timeout guardrail before action execution
            if (
                effective_timeout <= 0
                or (time.perf_counter() - start_time) >= effective_timeout
            ):
                raise TimeoutError(
                    f"Forensic scanning job exceeded timeout of {effective_timeout}s"
                )

            current_file_hash = None
            if staged_path and os.path.exists(staged_path):
                with contextlib.suppress(Exception):
                    current_file_hash = resilient_file_hash(staged_path)
            if not current_file_hash:
                current_file_hash = record.get("file_hash")

            is_approved_release = _is_content_approved_for_release(
                self.db,
                base_dir=base_dir or "",
                file_hash=current_file_hash,
            )
            if is_approved_release and action in ("quarantine", "sensitivity_hold"):
                logger.info(
                    f"Skipping repeat quarantine hold for approved unchanged content '{orig_rel_path}'."
                )
                action = "allow"

            # 4b. Upstream Barrier: Jev Pre-Classification & Sensitivity Hold for unguided / default documents
            if (
                not is_approved_release
                and action
                not in ("redact", "archive", "quarantine", "retain", "unarchive")
                and self.jev_classifier is not None
            ):
                try:
                    jev_res = self.jev_classifier.classify(
                        file_path=staged_path,
                        text_content=extracted_text,
                    )
                    sens_rating = (
                        getattr(jev_res, "sensitivity_rating", "LOW")
                        if not isinstance(jev_res, dict)
                        else jev_res.get("sensitivity_rating", "LOW")
                    ) or "LOW"
                    sens_rating = str(sens_rating).upper()

                    sens_score = (
                        getattr(jev_res, "sensitivity_score", 0.0)
                        if not isinstance(jev_res, dict)
                        else jev_res.get("sensitivity_score", 0.0)
                    )
                    category_val = (
                        getattr(jev_res, "category", "Uncategorized")
                        if not isinstance(jev_res, dict)
                        else jev_res.get("category", "Uncategorized")
                    )

                    auto_ratings = [
                        r.upper() for r in getattr(self, "auto_quarantine_ratings", [])
                    ]
                    if sens_rating in auto_ratings:
                        logger.info(
                            f"Jev pre-classification assigned sensitivity rating '{sens_rating}' to {orig_rel_path}. "
                            f"Holding file in quarantine staging on compliance hold."
                        )
                        self.db.update_quarantine_status(
                            job_id=job_id,
                            status="QUARANTINED",
                            policy_action="sensitivity_hold",
                            audit_entry={
                                "timestamp": time.time(),
                                "status": "QUARANTINED",
                                "policy_action": "sensitivity_hold",
                                "details": (
                                    f"Jev sensitivity pre-classification assigned {sens_rating} rating "
                                    f"(score: {sens_score:.2f}); retained in quarantine staging on compliance hold"
                                ),
                                "sensitivity_rating": sens_rating,
                                "sensitivity_score": sens_score,
                                "category": category_val,
                            },
                        )
                        rec = self.db.get_quarantine_record(job_id)
                        if rec:
                            return validate_quarantine_record(rec)
                        record["status"] = "QUARANTINED"
                        record["policy_action"] = "sensitivity_hold"
                        return validate_quarantine_record(record)
                except Exception as jev_err:
                    logger.warning(
                        f"Jev pre-classification warning for {orig_rel_path}: {jev_err}",
                        exc_info=True,
                    )

            # 5. Policy Lifecycle Action Execution
            if phase0_only:
                if action == "quarantine":
                    self.db.update_quarantine_status(
                        job_id=job_id,
                        status="QUARANTINED",
                        policy_action="quarantine",
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "QUARANTINED",
                            "details": "Document retained in quarantine staging for compliance review",
                        },
                    )
                    res_q = self.db.get_quarantine_record(job_id)
                    if isinstance(res_q, dict):
                        return validate_quarantine_record(res_q)
                    record["status"] = "QUARANTINED"
                    record["policy_action"] = "quarantine"
                    return validate_quarantine_record(record)

                for ef in forensic_extracted_files:
                    if os.path.exists(ef):
                        with contextlib.suppress(OSError):
                            resilient_remove(ef)
                if staged_path and os.path.exists(staged_path):
                    with contextlib.suppress(OSError):
                        resilient_remove(staged_path)
                self._delete_phase0_record(job_id)
                return validate_quarantine_record(
                    {
                        "job_id": job_id,
                        "base_dir": base_dir or "",
                        "original_filepath": orig_rel_path,
                        "staged_filepath": staged_path,
                        "file_hash": current_file_hash or "",
                        "status": "RELEASED",
                        "policy_action": "release",
                        "audit_log": [],
                    }
                )

            if action == "redact":
                rule_type = (
                    getattr(matched_rule, "type", "")
                    or (
                        matched_rule.get("type", "")
                        if isinstance(matched_rule, dict)
                        else ""
                    )
                    if matched_rule
                    else ""
                )
                rule_expr = (
                    getattr(matched_rule, "expression", "")
                    or (
                        matched_rule.get("expression", "")
                        if isinstance(matched_rule, dict)
                        else ""
                    )
                    if matched_rule
                    else ""
                )
                extra_regex = (
                    str(rule_expr)
                    if str(rule_type).lower() == "regex" and rule_expr
                    else None
                )

                # Overwrite staged file with format-aware sanitized content.
                # Filesystem OSError propagates to the handler below -> MANUAL_REVIEW_REQUIRED.
                # Binary/PDF files return was_redacted=False and are held intact in quarantine.
                was_redacted = False
                scrubbed_text = ""
                if os.path.exists(staged_path):
                    was_redacted, scrubbed_text = _redact_document_to_path(
                        staged_path,
                        staged_path,
                        extra_regex=extra_regex,
                    )

                if not was_redacted:
                    self.db.update_quarantine_status(
                        job_id=job_id,
                        status="QUARANTINED",
                        policy_action="redact",
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "QUARANTINED",
                            "policy_action": "redact",
                            "details": (
                                "Binary or PDF container cannot be safely text-redacted in place; "
                                "held intact in quarantine for manual review"
                            ),
                        },
                    )
                    res_hold = self.db.get_quarantine_record(job_id)
                    if isinstance(res_hold, dict):
                        return validate_quarantine_record(res_hold)
                    record["status"] = "QUARANTINED"
                    record["policy_action"] = "redact"
                    return validate_quarantine_record(record)

                # If vector exists in DB, scrub and purge vector
                if record.get("file_hash"):
                    try:
                        if hasattr(self.db, "_purge_document_vector"):
                            self.db._purge_document_vector(base_dir, orig_rel_path)
                        else:
                            vec = self.db.get_document_vector(base_dir, orig_rel_path)
                            if vec:
                                zero_vector_buffer(vec)
                    except Exception:
                        logger.warning(
                            f"Failed to scrub document vector for '{orig_rel_path}'",
                            exc_info=True,
                        )

                # Log REDACTED transition
                self.db.update_quarantine_status(
                    job_id=job_id,
                    status="REDACTED",
                    policy_action="redact",
                    audit_entry={
                        "timestamp": time.time(),
                        "status": "REDACTED",
                        "details": "PII scrubbing completed; sensitive tokens sanitized",
                    },
                )

                # Release document to target folder
                dest_dir = self.resolve_safe_target_dir(
                    base_dir, target_subfolder, default_subfolder="Redacted_Documents"
                )
                dest_subfolder = safe_relpath(dest_dir, base_dir).replace("\\", "/")
                if dest_subfolder == ".":
                    dest_subfolder = ""
                os.makedirs(dest_dir, exist_ok=True)
                dest_file_path = os.path.join(dest_dir, target_basename)

                with atomic_quarantine_relocation(
                    staged_path, dest_file_path
                ) as actual_dest:
                    # Transition to RELEASED
                    self.db.update_quarantine_status(
                        job_id=job_id,
                        status="RELEASED",
                        policy_action="redact",
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "RELEASED",
                            "details": f"Sanitized document released to {dest_subfolder}",
                        },
                    )

                    # Upsert sanitized document record in DB
                    final_hash = (
                        resilient_file_hash(actual_dest)
                        if os.path.exists(actual_dest)
                        else record["file_hash"]
                    )
                    self.db.upsert_document(
                        base_dir,
                        safe_relpath(actual_dest, base_dir).replace("\\", "/"),
                        final_hash,
                        scrubbed_text,
                    )

            elif action == "archive":
                archive_dir = self.resolve_safe_target_dir(
                    base_dir, target_subfolder, default_subfolder="Archive"
                )
                archive_subfolder = safe_relpath(archive_dir, base_dir).replace(
                    "\\", "/"
                )
                if archive_subfolder == ".":
                    archive_subfolder = ""
                os.makedirs(archive_dir, exist_ok=True)
                archive_file_path = os.path.join(archive_dir, target_basename)

                with atomic_quarantine_relocation(staged_path, archive_file_path):
                    self.db.update_quarantine_status(
                        job_id=job_id,
                        status="ARCHIVED",
                        policy_action="archive",
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "ARCHIVED",
                            "details": f"Document archived to {archive_subfolder}",
                        },
                    )

            elif action == "quarantine":
                self.db.update_quarantine_status(
                    job_id=job_id,
                    status="QUARANTINED",
                    policy_action="quarantine",
                    audit_entry={
                        "timestamp": time.time(),
                        "status": "QUARANTINED",
                        "details": "Document retained in quarantine staging for compliance review",
                    },
                )

            elif action == "retain":
                dest_dir = self.resolve_safe_target_dir(
                    base_dir, target_subfolder, default_subfolder="Retained_Documents"
                )
                dest_subfolder = safe_relpath(dest_dir, base_dir).replace("\\", "/")
                if dest_subfolder == ".":
                    dest_subfolder = ""
                os.makedirs(dest_dir, exist_ok=True)
                dest_file_path = os.path.join(dest_dir, target_basename)

                with atomic_quarantine_relocation(staged_path, dest_file_path):
                    self.db.update_quarantine_status(
                        job_id=job_id,
                        status="RELEASED",
                        policy_action="retain",
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "RELEASED",
                            "details": f"Document retained and released to {dest_subfolder}",
                        },
                    )

            elif action == "unarchive":
                dest_dir = self.resolve_safe_target_dir(
                    base_dir, target_subfolder, default_subfolder=""
                )
                os.makedirs(dest_dir, exist_ok=True)

                unpacked_child_files = []
                try:
                    unpacked_child_files = self.forensic_scanner.unpack_archive(
                        staged_path, dest_dir
                    )
                except ArchiveSafetyError as ase:
                    logger.warning(
                        f"Archive safety limit error during unarchive action in job {job_id}: {ase}"
                    )
                    err_msg = f"Archive safety violation during unarchive: {ase}"
                    self.db.update_quarantine_status(
                        job_id=job_id,
                        status="MANUAL_REVIEW_REQUIRED",
                        error_message=err_msg,
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "MANUAL_REVIEW_REQUIRED",
                            "details": f"Unarchive policy action aborted: {ase}",
                        },
                    )
                    dlq_item = self.db.get_quarantine_record(job_id)
                    if dlq_item:
                        self.dlq_records.append(dlq_item)
                        return validate_quarantine_record(dlq_item)
                    fallback = {
                        "job_id": job_id,
                        "status": "MANUAL_REVIEW_REQUIRED",
                        "error_message": err_msg,
                        "error": err_msg,
                    }
                    return validate_quarantine_record(fallback)

                post_action = (
                    (
                        getattr(matched_rule, "post_unarchive_action", None)
                        or (
                            matched_rule.get("post_unarchive_action")
                            if isinstance(matched_rule, dict)
                            else None
                        )
                        or getattr(matched_rule, "post_action", None)
                        or (
                            matched_rule.get("post_action")
                            if isinstance(matched_rule, dict)
                            else None
                        )
                        or getattr(matched_rule, "archive_action", None)
                        or (
                            matched_rule.get("archive_action")
                            if isinstance(matched_rule, dict)
                            else None
                        )
                    )
                    or "delete"
                ).lower()

                orig_full_path = os.path.join(base_dir, orig_rel_path)

                if post_action in ("move", "archive", "relocate"):
                    archive_target = (
                        getattr(matched_rule, "post_unarchive_target", None)
                        or (
                            matched_rule.get("post_unarchive_target")
                            if isinstance(matched_rule, dict)
                            else None
                        )
                        or getattr(matched_rule, "archive_target", None)
                        or (
                            matched_rule.get("archive_target")
                            if isinstance(matched_rule, dict)
                            else None
                        )
                    )
                    archive_dir = self.resolve_safe_target_dir(
                        base_dir, archive_target, default_subfolder="Archive"
                    )
                    os.makedirs(archive_dir, exist_ok=True)
                    archive_file_path = os.path.join(
                        archive_dir, os.path.basename(orig_rel_path)
                    )
                    if os.path.exists(staged_path):
                        with atomic_quarantine_relocation(
                            staged_path, archive_file_path
                        ):
                            pass
                    if os.path.exists(orig_full_path) and os.path.abspath(
                        orig_full_path
                    ) != os.path.abspath(archive_file_path):
                        resilient_remove(orig_full_path)
                elif post_action in ("retain", "keep"):
                    if os.path.exists(staged_path):
                        if not os.path.exists(orig_full_path):
                            shutil.copy2(staged_path, orig_full_path)
                        resilient_remove(staged_path)
                else:
                    backup_archive_path = os.path.join(
                        overwrite_backup_dir(base_dir, job_id),
                        "unarchive_src",
                        orig_rel_path,
                    )
                    if os.path.exists(orig_full_path):
                        resilient_move(orig_full_path, backup_archive_path)
                        if os.path.exists(staged_path):
                            resilient_remove(staged_path)
                    elif os.path.exists(staged_path):
                        resilient_move(staged_path, backup_archive_path)

                dest_subfolder = safe_relpath(dest_dir, base_dir).replace("\\", "/")
                if dest_subfolder == ".":
                    dest_subfolder = ""

                self.db.update_quarantine_status(
                    job_id=job_id,
                    status="UNARCHIVED",
                    policy_action="unarchive",
                    audit_entry={
                        "timestamp": time.time(),
                        "status": "UNARCHIVED",
                        "policy_action": "unarchive",
                        "details": f"Archive extracted ({len(unpacked_child_files)} files) to '{dest_subfolder or '.'}'; source archive action: {post_action}",
                        "unpacked_files": unpacked_child_files,
                        "post_action": post_action,
                    },
                )

            else:
                # Default release to original relative location when no explicit
                # lifecycle action matched so actionless routing policies never
                # create duplicate copies before SortPipeline runs.
                effective_subfolder = target_subfolder if action else None
                dest_dir = self.resolve_safe_target_dir(
                    base_dir, effective_subfolder, default_subfolder=""
                )
                dest_subfolder = safe_relpath(dest_dir, base_dir).replace("\\", "/")
                if dest_subfolder == ".":
                    dest_subfolder = ""
                os.makedirs(dest_dir, exist_ok=True)
                dest_file_path = (
                    os.path.join(base_dir, orig_rel_path)
                    if not action
                    else os.path.join(dest_dir, target_basename)
                )

                # Releasing back over the unchanged original (the daemon stages a
                # copy and leaves the original in place): keep the original rather
                # than relocating the staged copy to a free `<name>_1` (#907).
                release_source = staged_path
                if self._is_same_content(staged_path, dest_file_path):
                    release_source = None

                with atomic_quarantine_relocation(
                    release_source, dest_file_path
                ) as actual_dest:
                    self.db.update_quarantine_status(
                        job_id=job_id,
                        status="RELEASED",
                        policy_action="release",
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "RELEASED",
                            "details": f"Document released from quarantine to {dest_file_path}",
                        },
                    )
                    final_hash = (
                        resilient_file_hash(actual_dest)
                        if os.path.exists(actual_dest)
                        else record["file_hash"]
                    )
                    self.db.upsert_document(
                        base_dir,
                        safe_relpath(actual_dest, base_dir).replace("\\", "/"),
                        final_hash,
                        str(extracted_text),
                    )
                if release_source is None and os.path.exists(staged_path):
                    resilient_remove(staged_path)

            res = self.db.get_quarantine_record(job_id)
            if isinstance(res, dict):
                if action == "unarchive" and "unpacked_child_files" in locals():
                    res["unpacked_files"] = unpacked_child_files
                return validate_quarantine_record(res)
            record["status"] = "UNARCHIVED" if action == "unarchive" else "RELEASED"
            record["policy_action"] = action or "release"
            if action == "unarchive" and "unpacked_child_files" in locals():
                record["unpacked_files"] = unpacked_child_files
            return validate_quarantine_record(record)

        except TimeoutError as te:
            logger.error(f"Quarantine worker timeout for job {job_id}: {te}")
            err_msg = str(te)
            self.db.update_quarantine_status(
                job_id=job_id,
                status="MANUAL_REVIEW_REQUIRED",
                error_message=err_msg,
                audit_entry={
                    "timestamp": time.time(),
                    "status": "MANUAL_REVIEW_REQUIRED",
                    "details": f"Worker timeout exceeded: {err_msg}. Routed to Dead-Letter Queue.",
                },
            )
            # Record failed job in dead-letter tracking
            dlq_item = self.db.get_quarantine_record(job_id)
            if dlq_item:
                self.dlq_records.append(dlq_item)
                return validate_quarantine_record(dlq_item)
            fallback = {
                "job_id": job_id,
                "status": "MANUAL_REVIEW_REQUIRED",
                "error_message": err_msg,
                "error": err_msg,
            }
            return validate_quarantine_record(fallback)

        except Exception as e:
            logger.error(
                f"Error processing quarantine job {job_id}: {e}", exc_info=True
            )
            err_msg = str(e)
            self.db.update_quarantine_status(
                job_id=job_id,
                status="MANUAL_REVIEW_REQUIRED",
                error_message=err_msg,
                audit_entry={
                    "timestamp": time.time(),
                    "status": "MANUAL_REVIEW_REQUIRED",
                    "details": f"Inspection failed with error: {err_msg}",
                },
            )
            dlq_item = self.db.get_quarantine_record(job_id)
            if dlq_item:
                self.dlq_records.append(dlq_item)
                return validate_quarantine_record(dlq_item)
            fallback = {
                "job_id": job_id,
                "status": "MANUAL_REVIEW_REQUIRED",
                "error_message": err_msg,
                "error": err_msg,
            }
            return validate_quarantine_record(fallback)

    def process_quarantine_batch(
        self, job_ids: list[str], timeout_override: float | None = None
    ) -> list[Any]:
        """Execute forensic scanning and policy evaluation for a batch of quarantine job IDs.

        Guarantees isolated record execution so individual job failures do not halt
        remaining batch operations.
        """
        results = []
        unique_ids = list(dict.fromkeys(job_ids))
        for job_id in unique_ids:
            try:
                res = self.process_quarantine_job(
                    job_id, timeout_override=timeout_override
                )
                results.append(res)
            except Exception as e:
                logger.error(
                    f"Batch process failure for job ID '{job_id}': {e}", exc_info=True
                )
                results.append(
                    {
                        "job_id": job_id,
                        "status": "FAILED",
                        "error": str(e),
                    }
                )
        return results

    def release_quarantine_batch(
        self,
        job_ids: list[str],
        dest_dir: str | None = None,
        conflict_policy: str = "rename",
        allow_external: bool = False,
        **kwargs: Any,
    ) -> list[dict[str, Any]]:
        """Release a batch of quarantined items to target destination directories.

        Guarantees isolated atomic file relocations and individual audit logging.
        """
        if isinstance(conflict_policy, bool):
            allow_external = conflict_policy
            conflict_policy = "rename"
        allow_external = allow_external or kwargs.get("allow_external_dest", False)

        normalized_conflict_policy = (conflict_policy or "rename").lower()
        if normalized_conflict_policy not in ("rename", "skip", "overwrite"):
            raise ValueError(
                f"Invalid conflict policy: '{conflict_policy}'. Expected 'rename', 'skip', or 'overwrite'."
            )

        results = []
        unique_ids = list(dict.fromkeys(job_ids))
        for job_id in unique_ids:
            displaced_backup = None
            dest_file_path = None
            try:
                record = self.db.get_quarantine_record(job_id)
                if not record:
                    results.append(
                        {
                            "job_id": job_id,
                            "status": "FAILED",
                            "error": f"Quarantine record not found for job ID '{job_id}'",
                        }
                    )
                    continue

                if record.get("status") == "RELEASED":
                    results.append(
                        {
                            "job_id": job_id,
                            "status": "RELEASED",
                            "details": f"Quarantine job '{job_id}' is already released",
                        }
                    )
                    continue

                if record.get("status") == "DISMISSED":
                    results.append(
                        {
                            "job_id": job_id,
                            "status": "FAILED",
                            "error": f"Cannot release dismissed quarantine job '{job_id}'",
                        }
                    )
                    continue

                base_dir = record.get("base_dir") or ""
                orig_rel = record.get("original_filepath") or "released_file"
                effective_base = os.path.abspath(base_dir) if base_dir else os.getcwd()

                raw_staged_path = record.get("staged_filepath")
                staged_path = raw_staged_path
                if staged_path:
                    if not os.path.isabs(staged_path):
                        candidate_staged = os.path.normpath(
                            os.path.join(effective_base, staged_path)
                        )
                        if os.path.exists(candidate_staged) or not os.path.exists(
                            staged_path
                        ):
                            staged_path = candidate_staged
                    else:
                        staged_path = os.path.abspath(staged_path)

                if not staged_path or not os.path.exists(staged_path):
                    err_msg = f"Staged file missing at '{raw_staged_path}'"
                    self.db.update_quarantine_status(
                        job_id=job_id,
                        status="MANUAL_REVIEW_REQUIRED",
                        error_message=err_msg,
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "MANUAL_REVIEW_REQUIRED",
                            "details": f"Batch release failed: {err_msg}",
                        },
                    )
                    results.append(
                        {
                            "job_id": job_id,
                            "status": "FAILED",
                            "error": err_msg,
                        }
                    )
                    continue

                if dest_dir:
                    target_parent = os.path.abspath(dest_dir)
                else:
                    target_parent = effective_base

                if not allow_external and not is_subpath_or_equal(
                    target_parent, effective_base
                ):
                    err_msg = (
                        f"Release destination directory '{target_parent}' escapes "
                        f"base directory boundary '{effective_base}'"
                    )
                    self.db.update_quarantine_status(
                        job_id=job_id,
                        status="MANUAL_REVIEW_REQUIRED",
                        error_message=err_msg,
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "MANUAL_REVIEW_REQUIRED",
                            "details": f"Batch release failed: {err_msg}",
                        },
                    )
                    results.append(
                        {
                            "job_id": job_id,
                            "status": "FAILED",
                            "error": err_msg,
                        }
                    )
                    continue

                os.makedirs(target_parent, exist_ok=True)
                candidate_dest = os.path.join(target_parent, os.path.basename(orig_rel))

                if os.path.lexists(candidate_dest):
                    if normalized_conflict_policy == "skip":
                        err_msg = (
                            f"Destination file already exists: '{candidate_dest}' "
                            f"(conflict policy: 'skip')"
                        )
                        raise FileExistsError(err_msg)
                    elif normalized_conflict_policy == "rename":
                        safe_dest = get_safe_path(
                            target_parent, os.path.basename(candidate_dest), staged_path
                        )
                        dest_file_path = unicodedata.normalize("NFC", safe_dest)
                    elif normalized_conflict_policy == "overwrite":
                        backup_dir = overwrite_backup_dir(effective_base, job_id)
                        try:
                            rel_p = safe_relpath(candidate_dest, effective_base)
                            if rel_p.startswith("..") or os.path.isabs(rel_p):
                                rel_p = os.path.basename(candidate_dest)
                        except (ValueError, OSError):
                            rel_p = os.path.basename(candidate_dest)
                        backup_file_path = os.path.join(backup_dir, rel_p)
                        os.makedirs(os.path.dirname(backup_file_path), exist_ok=True)
                        if os.path.lexists(backup_file_path):
                            backup_file_path = get_safe_path(
                                os.path.dirname(backup_file_path),
                                os.path.basename(backup_file_path),
                            )
                        displaced_backup = resilient_move(
                            candidate_dest, backup_file_path
                        )
                        dest_file_path = candidate_dest
                else:
                    dest_file_path = candidate_dest

                approved_hash = None
                with contextlib.suppress(Exception):
                    approved_hash = resilient_file_hash(staged_path)
                if not approved_hash:
                    approved_hash = record.get("file_hash")

                try:
                    with atomic_quarantine_relocation(staged_path, dest_file_path):
                        audit_entry: dict[str, Any] = {
                            "timestamp": time.time(),
                            "status": "RELEASED",
                            "details": f"Released from quarantine modal review to {dest_file_path}",
                            "user_approved_release": True,
                        }
                        if approved_hash:
                            audit_entry["approved_file_hash"] = approved_hash
                        self.db.update_quarantine_status(
                            job_id=job_id,
                            status="RELEASED",
                            policy_action="release",
                            audit_entry=audit_entry,
                        )
                except Exception:
                    if (
                        displaced_backup
                        and os.path.lexists(displaced_backup)
                        and not os.path.lexists(dest_file_path)
                    ):
                        with contextlib.suppress(Exception):
                            resilient_move(displaced_backup, dest_file_path)
                    raise

                results.append(
                    {
                        "job_id": job_id,
                        "status": "RELEASED",
                        "destination": dest_file_path,
                    }
                )
            except Exception as e:
                logger.error(
                    f"Batch release failure for job ID '{job_id}': {e}", exc_info=True
                )
                with contextlib.suppress(Exception):
                    self.db.update_quarantine_status(
                        job_id=job_id,
                        status="MANUAL_REVIEW_REQUIRED",
                        error_message=str(e),
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "MANUAL_REVIEW_REQUIRED",
                            "details": f"Batch release failed: {e}",
                        },
                    )
                results.append(
                    {
                        "job_id": job_id,
                        "status": "FAILED",
                        "error": str(e),
                    }
                )
        return results

    def dismiss_quarantine_batch(self, job_ids: list[str]) -> list[dict[str, Any]]:
        """Mark a batch of quarantine records as DISMISSED in database with audit logs."""
        results = []
        unique_ids = list(dict.fromkeys(job_ids))
        for job_id in unique_ids:
            try:
                record = self.db.get_quarantine_record(job_id)
                if not record:
                    results.append(
                        {
                            "job_id": job_id,
                            "status": "FAILED",
                            "error": f"Quarantine record not found for job ID '{job_id}'",
                        }
                    )
                    continue

                if record.get("status") == "DISMISSED":
                    results.append(
                        {
                            "job_id": job_id,
                            "status": "DISMISSED",
                            "details": f"Quarantine job '{job_id}' is already dismissed",
                        }
                    )
                    continue

                if record.get("status") == "RELEASED":
                    results.append(
                        {
                            "job_id": job_id,
                            "status": "FAILED",
                            "error": f"Cannot dismiss already released quarantine job '{job_id}'",
                        }
                    )
                    continue

                self.db.update_quarantine_status(
                    job_id=job_id,
                    status="DISMISSED",
                    audit_entry={
                        "timestamp": time.time(),
                        "status": "DISMISSED",
                        "details": "Dismissed manually via quarantine vault modal review",
                    },
                )
                results.append(
                    {
                        "job_id": job_id,
                        "status": "DISMISSED",
                    }
                )
            except Exception as e:
                logger.error(
                    f"Batch dismiss failure for job ID '{job_id}': {e}", exc_info=True
                )
                results.append(
                    {
                        "job_id": job_id,
                        "status": "FAILED",
                        "error": str(e),
                    }
                )
        return results
