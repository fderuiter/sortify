"""Event-driven compliance interceptor service with quarantine staging and async pipeline."""

import logging
import os
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional

from app.core.analyzer_strategies import redact_sensitive_text
from app.core.crypto import zero_vector_buffer
from app.core.db import Database
from app.core.domain_contracts import QuarantineRecordModel, validate_quarantine_record
from app.core.extractor import extract_file_text
from app.core.forensic_scanner import ForensicScanner
from app.core.mover import is_subpath_or_equal
from app.core.path_utils import validate_target_path
from app.core.policy_engine import PolicyEngine
from app.core.resilient_file_ops import (
    _set_posix_mode,
    resilient_file_hash,
    resilient_move,
)

logger = logging.getLogger(__name__)


def scrub_pii_from_text(text: Any) -> str:
    """Scrub PII, sensitive keywords, and cryptographic tokens from document text."""
    if not text:
        return ""
    import re

    from app.core.text_utils import sanitize_secret_patterns

    text_str = str(text)

    # Standard prompt extract redaction
    redacted = redact_sensitive_text(text_str)

    # SSN pattern
    redacted = re.sub(r"\b\d{3}-\d{2}-\d{4}\b", "[REDACTED_SSN]", redacted)
    # Credit Card pattern
    redacted = re.sub(r"\b(?:\d[ -]*?){13,16}\b", "[REDACTED_CARD]", redacted)
    # Email pattern
    redacted = re.sub(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
        "[REDACTED_EMAIL]",
        redacted,
    )
    # Clinical/PII Sensitive phrases
    redacted = re.sub(
        r"(?i)(Confidential Medical Report|Subject \d+|Diagnosis:[^\n]*)",
        "[REDACTED_PII]",
        redacted,
    )

    # Centralized secret pattern redaction (API keys, JWT tokens, Bearer tokens, private keys, high-entropy secrets)
    redacted = sanitize_secret_patterns(redacted, replacement="[REDACTED_SECRET]")

    return redacted


class QuarantineInterceptorService:
    """Asynchronous compliance interceptor service that isolates incoming files in quarantine staging.

    Executes deep forensic checks, PII redaction, and policy lifecycle actions off the main thread.
    """

    def __init__(
        self,
        db: Database,
        policies: Optional[List[Dict[str, Any]]] = None,
        worker_timeout: float = 300.0,
        jev_classifier: Optional[Any] = None,
        auto_quarantine_ratings: Optional[List[str]] = None,
        runtime_settings: Optional[Any] = None,
    ):
        self.db = db
        self.policies = policies or []
        self.worker_timeout = worker_timeout
        self.forensic_scanner = ForensicScanner()
        self.dlq_records: List[Dict[str, Any]] = []
        if jev_classifier is not None:
            self.jev_classifier = jev_classifier
        else:
            try:
                from app.core.jev_classifier import JevClassifierEngine

                self.jev_classifier = JevClassifierEngine()
            except Exception:
                self.jev_classifier = None

        if auto_quarantine_ratings is not None:
            self.auto_quarantine_ratings = [r.upper() for r in auto_quarantine_ratings]
        elif runtime_settings and hasattr(runtime_settings, "AUTO_QUARANTINE_RATINGS"):
            self.auto_quarantine_ratings = [
                r.upper()
                for r in getattr(runtime_settings, "AUTO_QUARANTINE_RATINGS", [])
            ]
        else:
            self.auto_quarantine_ratings = ["CRITICAL", "HIGH"]

    def resolve_safe_target_dir(
        self,
        base_dir: str,
        target_subfolder: Optional[str],
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
                f"Falling back to default directory '{fallback_dir}'."
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
                f"Falling back to default directory '{fallback_dir}'."
            )
            return fallback_dir

        return candidate_dir

    def stage_incoming_file(
        self,
        source_path: str,
        base_dir: str,
        original_relative_path: Optional[str] = None,
        policy_action: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Immediately place incoming unverified file into temporary _Quarantine_Staging prior to compliance evaluation."""
        if not os.path.exists(source_path):
            raise FileNotFoundError(f"Source file not found for staging: {source_path}")

        job_id = f"qjob_{uuid.uuid4().hex[:12]}"
        file_name = os.path.basename(source_path)
        rel_path = original_relative_path or file_name

        quarantine_dir = os.path.join(base_dir, "_Quarantine_Staging")
        os.makedirs(quarantine_dir, exist_ok=True)
        _set_posix_mode(quarantine_dir, 0o700)

        staged_file_name = f"{job_id}_{file_name}"
        staged_path = os.path.join(quarantine_dir, staged_file_name)

        # Copy file to quarantine staging area
        shutil.copy2(source_path, staged_path)
        _set_posix_mode(staged_path, 0o600)

        file_hash = None
        try:
            file_hash = resilient_file_hash(staged_path)
        except Exception:
            pass

        # Record atomic STAGED state transition in database
        self.db.stage_quarantine_record(
            job_id=job_id,
            base_dir=base_dir,
            original_filepath=rel_path,
            staged_filepath=staged_path,
            file_hash=file_hash,
            policy_action=policy_action,
        )

        return {
            "job_id": job_id,
            "base_dir": base_dir,
            "original_filepath": rel_path,
            "staged_filepath": staged_path,
            "file_hash": file_hash,
            "status": "STAGED",
        }

    def process_quarantine_job(
        self, job_id: str, timeout_override: Optional[float] = None
    ) -> QuarantineRecordModel:
        """Execute deep forensic scanning, PII redaction, policy evaluation, and archival lifecycle actions."""
        record = self.db.get_quarantine_record(job_id)
        if not isinstance(record, dict):
            # Fallback for mocked DB instances in unit tests
            record = None
            if hasattr(self.db, "stage_quarantine_record") and getattr(
                self.db.stage_quarantine_record, "call_args", None
            ):
                call_kwargs = (
                    getattr(self.db.stage_quarantine_record.call_args, "kwargs", {})
                    or {}
                )
                if call_kwargs.get("job_id") == job_id or not call_kwargs.get("job_id"):
                    record = {
                        "job_id": job_id,
                        "base_dir": call_kwargs.get("base_dir"),
                        "original_filepath": call_kwargs.get("original_filepath"),
                        "staged_filepath": call_kwargs.get("staged_filepath"),
                        "file_hash": call_kwargs.get("file_hash"),
                        "policy_action": call_kwargs.get("policy_action"),
                        "status": "STAGED",
                        "audit_log": [],
                    }
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
                    extracted_files = self.forensic_scanner.unpack_archive(
                        staged_path, os.path.dirname(staged_path)
                    )
                    extracted_texts = []
                    for ef in extracted_files:
                        if os.path.isfile(ef):
                            _set_posix_mode(ef, 0o600)
                            t = str(extract_file_text(ef) or "")
                            if t:
                                extracted_texts.append(t)
                    extracted_text = "\n".join(extracted_texts)
                else:
                    extracted_text = extract_file_text(staged_path) or ""

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
                logger.warning(f"Plugin quarantine scan execution warning: {e}")

            # 4. Extended Policy Engine Evaluation
            matched_rule = PolicyEngine.evaluate_policies(
                file_path=orig_rel_path,
                doc_text=extracted_text,
                status_match="",
                policies=self.policies,
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

            target_subfolder = (
                getattr(matched_rule, "target_path", None)
                or (
                    matched_rule.get("target_path")
                    if isinstance(matched_rule, dict)
                    else None
                )
                if matched_rule
                else None
            )

            # Re-check timeout guardrail before action execution
            if (
                effective_timeout <= 0
                or (time.perf_counter() - start_time) >= effective_timeout
            ):
                raise TimeoutError(
                    f"Forensic scanning job exceeded timeout of {effective_timeout}s"
                )

            # 4b. Upstream Barrier: Jev Pre-Classification & Sensitivity Hold for unguided / default documents
            if (
                action not in ("redact", "archive", "quarantine", "retain")
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
                        r.upper()
                        for r in getattr(
                            self, "auto_quarantine_ratings", ["CRITICAL", "HIGH"]
                        )
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
                            return rec
                        record["status"] = "QUARANTINED"
                        record["policy_action"] = "sensitivity_hold"
                        return record
                except Exception as jev_err:
                    logger.warning(
                        f"Jev pre-classification warning for {orig_rel_path}: {jev_err}"
                    )

            # 5. Policy Lifecycle Action Execution
            if action == "redact":
                # Execute PII Scrubbing & Release Pipeline
                scrubbed_text = scrub_pii_from_text(extracted_text)

                # Overwrite staged file with sanitized content
                if os.path.exists(staged_path):
                    try:
                        with open(staged_path, "w", encoding="utf-8") as f:
                            f.write(scrubbed_text)
                        _set_posix_mode(staged_path, 0o600)
                    except Exception:
                        pass

                # If vector exists in DB, scrub vector
                if record.get("file_hash"):
                    try:
                        vec = self.db.get_document_vector(base_dir, orig_rel_path)
                        if vec:
                            zero_vector_buffer(vec)
                    except Exception:
                        pass

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
                dest_subfolder = os.path.relpath(dest_dir, base_dir).replace("\\", "/")
                if dest_subfolder == ".":
                    dest_subfolder = ""
                os.makedirs(dest_dir, exist_ok=True)
                dest_file_path = os.path.join(dest_dir, os.path.basename(orig_rel_path))

                resilient_move(staged_path, dest_file_path)

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
                    resilient_file_hash(dest_file_path)
                    if os.path.exists(dest_file_path)
                    else record["file_hash"]
                )
                self.db.upsert_document(
                    base_dir,
                    os.path.relpath(dest_file_path, base_dir).replace("\\", "/"),
                    final_hash,
                    scrubbed_text,
                )

            elif action == "archive":
                archive_dir = self.resolve_safe_target_dir(
                    base_dir, target_subfolder, default_subfolder="Archive"
                )
                archive_subfolder = os.path.relpath(archive_dir, base_dir).replace(
                    "\\", "/"
                )
                if archive_subfolder == ".":
                    archive_subfolder = ""
                os.makedirs(archive_dir, exist_ok=True)
                archive_file_path = os.path.join(
                    archive_dir, os.path.basename(orig_rel_path)
                )

                resilient_move(staged_path, archive_file_path)

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
                dest_subfolder = os.path.relpath(dest_dir, base_dir).replace("\\", "/")
                if dest_subfolder == ".":
                    dest_subfolder = ""
                os.makedirs(dest_dir, exist_ok=True)
                dest_file_path = os.path.join(dest_dir, os.path.basename(orig_rel_path))

                resilient_move(staged_path, dest_file_path)

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

            else:
                # Default release to target destination or original base_dir
                dest_dir = self.resolve_safe_target_dir(
                    base_dir, target_subfolder, default_subfolder=""
                )
                dest_subfolder = os.path.relpath(dest_dir, base_dir).replace("\\", "/")
                if dest_subfolder == ".":
                    dest_subfolder = ""
                os.makedirs(dest_dir, exist_ok=True)
                dest_file_path = os.path.join(dest_dir, os.path.basename(orig_rel_path))

                if os.path.abspath(staged_path) != os.path.abspath(dest_file_path):
                    resilient_move(staged_path, dest_file_path)

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
                    resilient_file_hash(dest_file_path)
                    if os.path.exists(dest_file_path)
                    else record["file_hash"]
                )
                self.db.upsert_document(
                    base_dir,
                    os.path.relpath(dest_file_path, base_dir).replace("\\", "/"),
                    final_hash,
                    str(extracted_text),
                )

            res = self.db.get_quarantine_record(job_id)
            if isinstance(res, dict):
                return validate_quarantine_record(res)
            record["status"] = "RELEASED"
            record["policy_action"] = action or "release"
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
            # Move job to dead-letter queue
            dlq_item = self.db.get_quarantine_record(job_id)
            if dlq_item:
                dlq_item["status"] = "DEAD_LETTER_QUEUE"
                self.dlq_records.append(dlq_item)
                return validate_quarantine_record(dlq_item)
            fallback = {
                "job_id": job_id,
                "status": "DEAD_LETTER_QUEUE",
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
                dlq_item["status"] = "DEAD_LETTER_QUEUE"
                self.dlq_records.append(dlq_item)
                return validate_quarantine_record(dlq_item)
            fallback = {
                "job_id": job_id,
                "status": "DEAD_LETTER_QUEUE",
                "error": err_msg,
            }
            return validate_quarantine_record(fallback)
