"""Event-driven compliance interceptor service with quarantine staging and async pipeline."""

import contextlib
import logging
import os
import re
import shutil
import time
import unicodedata
import uuid
from typing import Any, Optional

from app.config import resolve_effective_auto_quarantine_ratings
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
    file_hash: Optional[str] = None,
    file_path: Optional[str] = None,
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

    if hasattr(db, "is_quarantine_hash_released"):
        try:
            return bool(db.is_quarantine_hash_released(target_hash, base_dir=base_dir))
        except (OSError, ValueError, RuntimeError, AttributeError) as exc:
            logger.debug("Could not query quarantine_release_exemptions: %s", exc)

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

        if (
            rec.get("file_hash") == target_hash
            or rec.get("released_file_hash") == target_hash
        ):
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
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
CLINICAL_PII_PATTERN = re.compile(
    r"(?i)(Confidential Medical Report|Subject \d+|Diagnosis:[^\n]*)"
)


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
    # Email pattern
    redacted = EMAIL_PATTERN.sub("[REDACTED_EMAIL]", redacted)
    # Clinical/PII Sensitive phrases
    redacted = CLINICAL_PII_PATTERN.sub("[REDACTED_PII]", redacted)

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
        policies: Optional[list[dict[str, Any]]] = None,
        worker_timeout: float = 300.0,
        jev_classifier: Optional[Any] = None,
        auto_quarantine_ratings: Optional[list[str]] = None,
        runtime_settings: Optional[Any] = None,
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

        self.runtime_settings = runtime_settings
        self._explicit_auto_quarantine_ratings = (
            [str(r).upper() for r in auto_quarantine_ratings if r]
            if auto_quarantine_ratings is not None
            else None
        )
        self.auto_quarantine_ratings = resolve_effective_auto_quarantine_ratings(
            runtime_settings=runtime_settings,
            explicit_ratings=self._explicit_auto_quarantine_ratings,
        )

    def _get_effective_auto_quarantine_ratings(self) -> list[str]:
        """Return active sensitivity ratings accounting for live runtime_settings changes."""
        if self.runtime_settings is not None:
            return resolve_effective_auto_quarantine_ratings(
                runtime_settings=self.runtime_settings,
                explicit_ratings=self._explicit_auto_quarantine_ratings,
            )
        return list(self.auto_quarantine_ratings)

    def _get_active_policies(self) -> list[dict[str, Any]]:
        """Return sorted policies from explicit initialization or live runtime_settings."""
        if self.policies:
            return self.policies
        if self.runtime_settings is not None:
            raw_policies = getattr(self.runtime_settings, "POLICIES", None) or []
            if raw_policies:
                return PolicyEngine.sort_policies(raw_policies)
        return []

    def _match_user_routing_rules(
        self, file_path: str, extracted_text: str
    ) -> Optional[str]:
        """Evaluate KEYWORD_RULES and LEARNED_RULES from runtime_settings before JEV holds."""
        if self.runtime_settings is None:
            return None
        filename_only = os.path.basename(file_path or "").lower()
        doc_lower = (extracted_text or "").lower()
        haystack = f"{filename_only} {doc_lower}".strip()

        keyword_rules = getattr(self.runtime_settings, "KEYWORD_RULES", None) or {}
        if isinstance(keyword_rules, dict):
            for keyword, target_folder in keyword_rules.items():
                if not isinstance(keyword, str) or not keyword.strip():
                    continue
                if keyword.strip().lower() in haystack and target_folder:
                    return str(target_folder)

        learned_rules = getattr(self.runtime_settings, "LEARNED_RULES", None) or {}
        if isinstance(learned_rules, dict):
            for keyword, target_folder in learned_rules.items():
                if not isinstance(keyword, str) or not keyword.strip():
                    continue
                if keyword.strip().lower() in haystack and target_folder:
                    return str(target_folder)

        return None

    @staticmethod
    def _resolve_staged_filepath(
        staged_filepath: Optional[str], base_dir: Optional[str]
    ) -> str:
        """Resolve a relative or absolute staged_filepath against base_dir."""
        if not staged_filepath:
            return ""
        if os.path.isabs(staged_filepath):
            return os.path.abspath(staged_filepath)
        effective_base = os.path.abspath(base_dir) if base_dir else os.getcwd()
        candidate = os.path.normpath(os.path.join(effective_base, staged_filepath))
        if os.path.exists(candidate) or not os.path.exists(staged_filepath):
            return candidate
        return os.path.abspath(staged_filepath)

    def _resolve_original_filepath(
        self, base_dir: str, original_filepath: Optional[str]
    ) -> str:
        """Safely resolve original_filepath within base_dir, preserving valid relative subfolders."""
        effective_base = os.path.abspath(base_dir) if base_dir else os.getcwd()
        raw_orig = (original_filepath or "restored_file").replace("\\", "/").strip("/")
        if not raw_orig:
            raw_orig = "restored_file"
        orig_dir = os.path.dirname(raw_orig)
        orig_name = os.path.basename(raw_orig) or "restored_file"
        safe_parent = self.resolve_safe_target_dir(
            effective_base, orig_dir if orig_dir else None, default_subfolder=""
        )
        return os.path.normpath(os.path.join(safe_parent, orig_name))

    def list_quarantine_records(
        self,
        base_dir: Optional[str] = None,
        status: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List quarantine records via the service layer."""
        if base_dir:
            return self.db.get_quarantine_records_by_base_dir(base_dir, status=status)
        records = self.db.get_all_quarantine_records()
        if status:
            status_upper = status.upper()
            records = [
                r for r in records if (r.get("status") or "").upper() == status_upper
            ]
        return records

    def get_quarantine_record_details(self, job_id: str) -> Optional[dict[str, Any]]:
        """Retrieve a single quarantine record via the service layer."""
        return self.db.get_quarantine_record(job_id)

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
        original_relative_path: Optional[str] = None,
        policy_action: Optional[str] = None,
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
        if isinstance(self.db, Database) and os.path.exists(self.db.db_path):
            from app.core.db_conn import get_db_connection

            with contextlib.suppress(Exception):
                get_db_connection(self.db.db_path)

        return {
            "job_id": job_id,
            "base_dir": abs_base_dir,
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

        raw_staged_path = record["staged_filepath"]
        base_dir = record["base_dir"]
        orig_rel_path = record["original_filepath"]

        if base_dir:
            base_dir = os.path.abspath(base_dir)
        staged_path = self._resolve_staged_filepath(raw_staged_path, base_dir)

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

            # 4. Extended Policy Engine & User Routing Rules Evaluation
            active_policies = self._get_active_policies()
            matched_rule = PolicyEngine.evaluate_policies(
                file_path=orig_rel_path,
                doc_text=extracted_text,
                status_match="",
                policies=active_policies,
                is_sorted=True,
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

            matched_user_rule_target: Optional[str] = None
            if matched_rule is None:
                matched_user_rule_target = self._match_user_routing_rules(
                    orig_rel_path, extracted_text
                )
                if matched_user_rule_target and not target_subfolder:
                    target_subfolder = matched_user_rule_target

            has_non_quarantine_rule = bool(
                (matched_rule is not None and action != "quarantine")
                or matched_user_rule_target is not None
            )

            # Re-check timeout guardrail before action execution
            if (
                effective_timeout <= 0
                or (time.perf_counter() - start_time) >= effective_timeout
            ):
                raise TimeoutError(
                    f"Forensic scanning job exceeded timeout of {effective_timeout}s"
                )

            auto_ratings = self._get_effective_auto_quarantine_ratings()
            current_file_hash = record.get("file_hash")
            is_approved_release = False
            if (auto_ratings and not has_non_quarantine_rule) or action in (
                "quarantine",
                "sensitivity_hold",
            ):
                if (
                    not current_file_hash
                    and staged_path
                    and os.path.exists(staged_path)
                ):
                    with contextlib.suppress(Exception):
                        current_file_hash = resilient_file_hash(staged_path)
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
                auto_ratings
                and not is_approved_release
                and not has_non_quarantine_rule
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
            if action == "redact":
                # Execute PII Scrubbing & Release Pipeline
                scrubbed_text = scrub_pii_from_text(extracted_text)

                # Overwrite staged file with sanitized content. A failure must
                # stop the job here: releasing the file now would publish the
                # unredacted original as "sanitized". It propagates to the
                # handler below, which routes the job to manual review.
                if os.path.exists(staged_path):
                    with open(staged_path, "w", encoding="utf-8") as f:
                        f.write(scrubbed_text)
                    _set_posix_mode(staged_path, 0o600)

                # If vector exists in DB, scrub vector
                if record.get("file_hash"):
                    try:
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
                dest_file_path = os.path.join(dest_dir, os.path.basename(orig_rel_path))

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
                archive_file_path = os.path.join(
                    archive_dir, os.path.basename(orig_rel_path)
                )

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
                dest_file_path = os.path.join(dest_dir, os.path.basename(orig_rel_path))

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
                    if os.path.exists(staged_path):
                        resilient_remove(staged_path)
                    if os.path.exists(orig_full_path):
                        resilient_remove(orig_full_path)

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
                # Default release to target destination or original relative location.
                # When the daemon staged a copy while leaving the original source file
                # untouched in base_dir for SortPipeline, verify and clean up staging
                # in place instead of creating a duplicate `<name>_1` file (#907).
                orig_full_path = self._resolve_original_filepath(
                    base_dir, orig_rel_path
                )
                if os.path.isfile(orig_full_path) and self._is_same_content(
                    staged_path, orig_full_path
                ):
                    dest_file_path = orig_full_path
                    os.makedirs(os.path.dirname(dest_file_path), exist_ok=True)
                    release_source = None
                elif target_subfolder:
                    dest_dir = self.resolve_safe_target_dir(
                        base_dir, target_subfolder, default_subfolder=""
                    )
                    os.makedirs(dest_dir, exist_ok=True)
                    dest_file_path = os.path.join(
                        dest_dir, os.path.basename(orig_rel_path)
                    )
                    release_source = (
                        None
                        if self._is_same_content(staged_path, dest_file_path)
                        else staged_path
                    )
                else:
                    dest_file_path = orig_full_path
                    os.makedirs(os.path.dirname(dest_file_path), exist_ok=True)
                    release_source = (
                        None
                        if self._is_same_content(staged_path, dest_file_path)
                        else staged_path
                    )

                was_held_reinspection = (record.get("status") or "").upper() in (
                    "QUARANTINED",
                    "MANUAL_REVIEW_REQUIRED",
                    "DEAD_LETTER_QUEUE",
                )

                with atomic_quarantine_relocation(
                    release_source, dest_file_path
                ) as actual_dest:
                    final_hash = (
                        resilient_file_hash(actual_dest)
                        if os.path.exists(actual_dest)
                        else record["file_hash"]
                    )
                    rel_actual = safe_relpath(actual_dest, base_dir).replace("\\", "/")
                    update_kwargs: dict[str, Any] = {}
                    if isinstance(self.db, Database):
                        update_kwargs["verified_target_path"] = rel_actual
                        update_kwargs["released_file_hash"] = final_hash
                    self.db.update_quarantine_status(
                        job_id=job_id,
                        status="RELEASED",
                        policy_action="release",
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "RELEASED",
                            "details": f"Document released from quarantine to {actual_dest}",
                            "approved_file_hash": final_hash
                            if was_held_reinspection
                            else None,
                            "record_release_exemption": was_held_reinspection,
                        },
                        **update_kwargs,
                    )
                    self.db.upsert_document(
                        base_dir,
                        rel_actual,
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
        self, job_ids: list[str], timeout_override: Optional[float] = None
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
        dest_dir: Optional[str] = None,
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
                staged_path = self._resolve_staged_filepath(
                    raw_staged_path, effective_base
                )

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
                    candidate_dest = os.path.join(
                        target_parent, os.path.basename(orig_rel)
                    )
                else:
                    candidate_dest = self._resolve_original_filepath(
                        effective_base, orig_rel
                    )
                    target_parent = os.path.dirname(candidate_dest) or effective_base

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
                    release_source = (
                        None
                        if self._is_same_content(staged_path, dest_file_path)
                        else staged_path
                    )
                    with atomic_quarantine_relocation(
                        release_source, dest_file_path
                    ) as actual_dest:
                        verified_target = actual_dest
                        try:
                            if is_subpath_or_equal(actual_dest, effective_base):
                                verified_target = safe_relpath(
                                    actual_dest, effective_base
                                ).replace("\\", "/")
                        except (ValueError, OSError):
                            verified_target = actual_dest

                        audit_entry: dict[str, Any] = {
                            "timestamp": time.time(),
                            "status": "RELEASED",
                            "details": f"Released from quarantine modal review to {actual_dest}",
                            "user_approved_release": True,
                            "record_release_exemption": True,
                            "verified_target_path": verified_target,
                        }
                        if approved_hash:
                            audit_entry["approved_file_hash"] = approved_hash
                        update_kwargs = {}
                        if isinstance(self.db, Database):
                            update_kwargs["verified_target_path"] = verified_target
                            update_kwargs["released_file_hash"] = approved_hash
                        self.db.update_quarantine_status(
                            job_id=job_id,
                            status="RELEASED",
                            policy_action="release",
                            audit_entry=audit_entry,
                            **update_kwargs,
                        )
                        if approved_hash and hasattr(
                            self.db, "record_quarantine_release_exemption"
                        ):
                            self.db.record_quarantine_release_exemption(
                                file_hash=approved_hash,
                                base_dir=effective_base,
                                verified_target_path=verified_target,
                                job_id=job_id,
                            )
                        dest_file_path = actual_dest
                    if release_source is None and os.path.exists(staged_path):
                        resilient_remove(staged_path)
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

    def dismiss_quarantine_batch(
        self,
        job_ids: list[str],
        *,
        purge: bool = False,
        confirm_purge: bool = False,
        conflict_policy: str = "rename",
    ) -> list[dict[str, Any]]:
        """Restore staged files to original_filepath or purge after confirmation, marking records DISMISSED."""
        if purge and not confirm_purge:
            raise ValueError(
                "Purging quarantined files requires explicit confirmation (confirm_purge=True)."
            )

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

                base_dir = record.get("base_dir") or ""
                effective_base = os.path.abspath(base_dir) if base_dir else os.getcwd()
                orig_rel = record.get("original_filepath") or "restored_file"
                raw_staged_path = record.get("staged_filepath")
                staged_path = self._resolve_staged_filepath(
                    raw_staged_path, effective_base
                )

                file_hash = None
                if staged_path and os.path.exists(staged_path):
                    with contextlib.suppress(Exception):
                        file_hash = resilient_file_hash(staged_path)
                if not file_hash:
                    file_hash = record.get("file_hash")

                if purge:
                    self.db.update_quarantine_status(
                        job_id=job_id,
                        status="DISMISSED",
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "DISMISSED",
                            "details": (
                                f"Dismissed manually via quarantine vault modal review; "
                                f"purged staged file '{staged_path or raw_staged_path}' after confirmation"
                            ),
                            "purged": True,
                        },
                    )
                    if staged_path and os.path.exists(staged_path):
                        resilient_remove(staged_path)
                    results.append(
                        {
                            "job_id": job_id,
                            "status": "DISMISSED",
                            "action": "purged",
                            "purged_path": staged_path,
                        }
                    )
                    continue

                if staged_path and os.path.exists(staged_path):
                    candidate_dest = self._resolve_original_filepath(
                        effective_base, orig_rel
                    )
                    os.makedirs(os.path.dirname(candidate_dest), exist_ok=True)

                    if os.path.lexists(candidate_dest):
                        if self._is_same_content(staged_path, candidate_dest):
                            dest_file_path = candidate_dest
                            relocate_source = None
                        elif normalized_conflict_policy == "skip":
                            raise FileExistsError(
                                f"Original destination already exists: '{candidate_dest}' (conflict policy: 'skip')"
                            )
                        elif normalized_conflict_policy == "rename":
                            safe_dest = get_safe_path(
                                os.path.dirname(candidate_dest),
                                os.path.basename(candidate_dest),
                                staged_path,
                            )
                            dest_file_path = unicodedata.normalize("NFC", safe_dest)
                            relocate_source = staged_path
                        else:
                            backup_dir = overwrite_backup_dir(effective_base, job_id)
                            rel_p = os.path.basename(candidate_dest)
                            with contextlib.suppress(ValueError, OSError):
                                cand_rel = safe_relpath(candidate_dest, effective_base)
                                if not cand_rel.startswith("..") and not os.path.isabs(
                                    cand_rel
                                ):
                                    rel_p = cand_rel
                            backup_file_path = os.path.join(backup_dir, rel_p)
                            os.makedirs(
                                os.path.dirname(backup_file_path), exist_ok=True
                            )
                            if os.path.lexists(backup_file_path):
                                backup_file_path = get_safe_path(
                                    os.path.dirname(backup_file_path),
                                    os.path.basename(backup_file_path),
                                )
                            displaced_backup = resilient_move(
                                candidate_dest, backup_file_path
                            )
                            dest_file_path = candidate_dest
                            relocate_source = staged_path
                    else:
                        dest_file_path = candidate_dest
                        relocate_source = staged_path

                    try:
                        with atomic_quarantine_relocation(
                            relocate_source, dest_file_path
                        ) as actual_dest:
                            verified_target = actual_dest
                            with contextlib.suppress(Exception):
                                if is_subpath_or_equal(actual_dest, effective_base):
                                    verified_target = safe_relpath(
                                        actual_dest, effective_base
                                    ).replace("\\", "/")
                            update_kwargs: dict[str, Any] = {}
                            if isinstance(self.db, Database):
                                update_kwargs["verified_target_path"] = verified_target
                                update_kwargs["released_file_hash"] = file_hash
                            self.db.update_quarantine_status(
                                job_id=job_id,
                                status="DISMISSED",
                                audit_entry={
                                    "timestamp": time.time(),
                                    "status": "DISMISSED",
                                    "details": (
                                        f"Dismissed manually via quarantine vault modal review; "
                                        f"restored staged file to {actual_dest}"
                                    ),
                                    "restored_path": actual_dest,
                                    "approved_file_hash": file_hash,
                                    "record_release_exemption": True,
                                },
                                **update_kwargs,
                            )
                            if file_hash and hasattr(
                                self.db, "record_quarantine_release_exemption"
                            ):
                                self.db.record_quarantine_release_exemption(
                                    file_hash=file_hash,
                                    base_dir=effective_base,
                                    verified_target_path=verified_target,
                                    job_id=job_id,
                                )
                            dest_file_path = actual_dest
                        if relocate_source is None and os.path.exists(staged_path):
                            resilient_remove(staged_path)
                    except Exception:
                        if (
                            displaced_backup
                            and dest_file_path
                            and os.path.lexists(displaced_backup)
                            and not os.path.lexists(dest_file_path)
                        ):
                            with contextlib.suppress(Exception):
                                resilient_move(displaced_backup, dest_file_path)
                        raise

                    results.append(
                        {
                            "job_id": job_id,
                            "status": "DISMISSED",
                            "action": "restored",
                            "restored_path": dest_file_path,
                        }
                    )
                else:
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
                            "action": "dismissed",
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

    def recover_orphaned_jobs(
        self, timeout_seconds: Optional[float] = None
    ) -> list[str]:
        """Detect and reset orphaned inspection jobs exceeding configured timeouts back to STAGED."""
        effective_timeout = (
            timeout_seconds if timeout_seconds is not None else self.worker_timeout
        )
        return self.db.recover_orphaned_quarantine_jobs(
            timeout_seconds=effective_timeout
        )
