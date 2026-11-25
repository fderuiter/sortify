"""Audit Reporter for post-sort file verification and export operations."""

import contextlib
import csv
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.exceptions import AuditExportError
from app.core.path_utils import safe_relpath

logger = logging.getLogger(__name__)

VERIFIED_PASS = "VERIFIED_PASS"
MISMATCH_FAIL = "MISMATCH_FAIL"
MOVE_FAIL = "MOVE_FAIL"
SKIPPED = "SKIPPED"
UNVERIFIED = "UNVERIFIED"

STATUS_BADGES = {
    VERIFIED_PASS: "[PASS]",
    MISMATCH_FAIL: "[FAIL]",
    MOVE_FAIL: "[MOVE FAIL]",
    SKIPPED: "[SKIPPED]",
    UNVERIFIED: "[UNVERIFIED]",
}


def generate_audit_report(
    base_dir: str,
    move_records: list[dict[str, Any]],
    session_id: str | None = None,
) -> dict[str, Any]:
    """Generate a structured audit report from a collection of move records.

    Args:
        base_dir: Target base directory for the sorting operation.
        move_records: List of dictionaries containing file move information.
            Expected keys per record:
            - source_path (str)
            - destination_path (str)
            - pre_hash (Optional[str])
            - post_hash (Optional[str])
            - status (Optional[str]) - if omitted, computed from pre/post hashes.
            - error / error_message (Optional[str]) - error description for failed items.

    Returns
    -------
        Dict[str, Any]: Complete audit report containing metrics, detailed records, and failed items.
    """
    base_dir = os.path.normpath(base_dir) if base_dir else ""
    processed_records: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []

    pass_count = 0
    fail_count = 0
    skipped_count = 0
    unverified_count = 0

    for item in move_records:
        src = item.get("source_path") or ""
        dst = item.get("destination_path") or item.get("target_path") or ""
        pre_hash = item.get("pre_hash") or item.get("original_hash")
        post_hash = item.get("post_hash") or item.get("new_hash")
        override_status = item.get("status")
        err_msg = (
            item.get("error")
            or item.get("error_message")
            or item.get("error_description")
            or ""
        )

        if override_status in (
            VERIFIED_PASS,
            MISMATCH_FAIL,
            MOVE_FAIL,
            SKIPPED,
            UNVERIFIED,
        ):
            status = override_status
        elif err_msg or item.get("move_failed"):
            status = MOVE_FAIL
        elif not dst or src == dst or item.get("skipped"):
            status = SKIPPED
        elif pre_hash and post_hash:
            if pre_hash == post_hash:
                status = VERIFIED_PASS
            else:
                status = MISMATCH_FAIL
        else:
            status = UNVERIFIED

        if not err_msg:
            if status == MISMATCH_FAIL:
                err_msg = "Pre-sort and post-sort SHA-256 hash mismatch"
            elif status == MOVE_FAIL:
                err_msg = "File relocation operation failed"

        if status in (MISMATCH_FAIL, MOVE_FAIL):
            fail_count += 1
        elif status == VERIFIED_PASS:
            pass_count += 1
        elif status == SKIPPED:
            skipped_count += 1
        elif status == UNVERIFIED:
            unverified_count += 1

        rel_src = (
            safe_relpath(src, base_dir).replace("\\", "/") if src and base_dir else src
        )
        rel_dst = (
            safe_relpath(dst, base_dir).replace("\\", "/") if dst and base_dir else dst
        )
        filename = os.path.basename(dst or src)

        rec = {
            "filename": filename,
            "relative_source": rel_src,
            "relative_destination": rel_dst,
            "source_path": src,
            "destination_path": dst,
            "pre_hash": pre_hash or "",
            "post_hash": post_hash or "",
            "status": status,
            "status_badge": STATUS_BADGES.get(status, f"[{status}]"),
            "error": err_msg,
            "error_description": err_msg,
        }
        processed_records.append(rec)
        if status in (MISMATCH_FAIL, MOVE_FAIL) or err_msg:
            failed_items.append(rec)

    total_files = len(processed_records)

    report: dict[str, Any] = {
        "base_dir": base_dir,
        "timestamp": datetime.now(UTC).isoformat(),
        "total_files": total_files,
        "verified_pass_count": pass_count,
        "mismatch_fail_count": fail_count,
        "skipped_count": skipped_count,
        "unverified_count": unverified_count,
        "records": processed_records,
        "failed_items": failed_items,
    }
    if session_id is not None:
        report["session_id"] = session_id
    return report


def resolve_audit_report(
    audit_report: dict[str, Any] | None,
    base_dir: str | None,
    plan: Any,
    session_id: str | None = None,
) -> dict[str, Any] | None:
    """Return ``audit_report`` or synthesize an audit report from ``plan`` moves.

    Parameters
    ----------
    audit_report : dict[str, Any] | None
        Existing post-sort audit report dictionary, if already generated.
    base_dir : str | None
        Target base directory for resolving planned move paths.
    plan : Any
        Sorting plan dictionary or contract model used when ``audit_report`` is empty.
    session_id : str | None, optional
        Sort session identifier to preserve on the resolved report.

    Returns
    -------
    dict[str, Any] | None
        Existing or synthesized audit report dictionary, or ``None`` when unavailable.
    """
    if audit_report:
        if session_id and not audit_report.get("session_id"):
            audit_report = dict(audit_report)
            audit_report["session_id"] = session_id
        return audit_report
    if not plan:
        return None
    from app.core.verifier import VerificationEngine

    effective_base = base_dir or "."
    moves = VerificationEngine.get_moves(effective_base, plan)
    records = [{"source_path": src, "destination_path": dst} for _, src, dst in moves]
    return generate_audit_report(effective_base, records, session_id=session_id)


def _validate_and_resolve_export_path(
    output_path: str,
    base_dir: str | None = None,
    report_data: dict[str, Any] | None = None,
) -> str:
    """Validate export output path, blocking traversal sequences and writes inside the sorted folder.

    Args:
        output_path: Proposed export file path (relative or absolute).
        base_dir: Active sorted folder boundary (exports inside this folder are blocked).
        report_data: Audit report dictionary containing fallback 'base_dir'.

    Returns
    -------
        str: Absolute resolved target file path.

    Raises
    ------
        AuditExportError: If target path is empty, contains directory traversal, or writes inside the sorted folder.
    """
    if not output_path or not str(output_path).strip():
        raise AuditExportError("Export output path cannot be empty.")

    raw_path_str = str(output_path).strip()
    normalized_sep = raw_path_str.replace("\\", "/")
    if ".." in Path(raw_path_str).parts or ".." in Path(normalized_sep).parts:
        msg = (
            f"Export target path '{output_path}' contains invalid directory traversal "
            "sequences"
        )
        logger.error(msg)
        raise AuditExportError(msg)

    expanded = os.path.expanduser(raw_path_str)
    if os.path.isabs(expanded):
        resolved_target = Path(expanded).resolve()
    else:
        resolved_cwd = Path.cwd().resolve()
        resolved_target = (resolved_cwd / expanded).resolve()

    target_base: str | None = None
    if base_dir and str(base_dir).strip():
        target_base = str(base_dir).strip()
    elif report_data and isinstance(report_data, dict):
        rep_base = report_data.get("base_dir")
        if rep_base and str(rep_base).strip():
            target_base = str(rep_base).strip()

    if target_base:
        resolved_base = Path(os.path.expanduser(target_base)).resolve()
        if resolved_target == resolved_base or resolved_target.is_relative_to(
            resolved_base
        ):
            msg = (
                f"Export target path '{output_path}' resolves inside sorted folder "
                f"'{resolved_base}'. Choose an external path outside the sorted folder."
            )
            logger.error(msg)
            raise AuditExportError(msg)

    return str(resolved_target)


def _open_secure_export_file(
    abs_path: str, mode: str = "w", newline: str | None = None
):
    """Open an export report file with mode 0o600 permissions.

    Args:
        abs_path: Resolved absolute file path.
        mode: Text open mode ("w").
        newline: Line ending configuration.

    Returns
    -------
        TextIO stream handle.

    Raises
    ------
        AuditExportError: If directory creation or file opening fails.
    """
    out_dir = os.path.dirname(abs_path)

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY

    try:
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        fd = os.open(abs_path, flags, 0o600)
        if os.name != "nt":
            with contextlib.suppress(OSError):
                os.chmod(fd, 0o600)
        return open(
            fd,
            mode=mode,
            encoding="utf-8",
            newline=newline,
            closefd=True,
        )
    except (OSError, PermissionError) as err:
        msg = f"Failed to create secure export file at {abs_path}: {err}"
        logger.error(msg)
        raise AuditExportError(msg) from err


def export_audit_report_json(
    report_data: dict[str, Any],
    output_path: str,
    base_dir: str | None = None,
) -> str:
    """Export audit report to a structured JSON file on disk.

    Args:
        report_data: Audit report dictionary generated by generate_audit_report.
        output_path: Target output file path.
        base_dir: Optional base directory boundary for containment validation.

    Returns
    -------
        str: Absolute path of created JSON file.

    Raises
    ------
        AuditExportError: If file writing or permission checks fail.
    """
    abs_path = _validate_and_resolve_export_path(
        output_path, base_dir=base_dir, report_data=report_data
    )
    try:
        with _open_secure_export_file(abs_path, "w") as f:
            json.dump(report_data, f, indent=2, ensure_ascii=False)
        logger.info(f"Audit report successfully exported to JSON: {abs_path}")
        return abs_path
    except AuditExportError:
        raise
    except (OSError, PermissionError) as err:
        msg = f"Failed to export audit report to JSON at {abs_path}: {err}"
        logger.error(msg)
        raise AuditExportError(msg) from err


def export_audit_report_csv(
    report_data: dict[str, Any],
    output_path: str,
    base_dir: str | None = None,
) -> str:
    """Export audit report to a structured CSV file on disk.

    Args:
        report_data: Audit report dictionary generated by generate_audit_report.
        output_path: Target output file path.
        base_dir: Optional base directory boundary for containment validation.

    Returns
    -------
        str: Absolute path of created CSV file.

    Raises
    ------
        AuditExportError: If file writing or permission checks fail.
    """
    abs_path = _validate_and_resolve_export_path(
        output_path, base_dir=base_dir, report_data=report_data
    )
    try:
        records = report_data.get("records", [])
        fieldnames = [
            "Filename",
            "Status",
            "Status Badge",
            "Pre-Sort SHA-256",
            "Post-Sort SHA-256",
            "Relative Source",
            "Relative Destination",
            "Source Path",
            "Destination Path",
        ]
        with _open_secure_export_file(abs_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(fieldnames)
            for rec in records:
                writer.writerow(
                    [
                        rec.get("filename", ""),
                        rec.get("status", ""),
                        rec.get("status_badge", ""),
                        rec.get("pre_hash", ""),
                        rec.get("post_hash", ""),
                        rec.get("relative_source", ""),
                        rec.get("relative_destination", ""),
                        rec.get("source_path", ""),
                        rec.get("destination_path", ""),
                    ]
                )
        logger.info(f"Audit report successfully exported to CSV: {abs_path}")
        return abs_path
    except AuditExportError:
        raise
    except (OSError, PermissionError) as err:
        msg = f"Failed to export audit report to CSV at {abs_path}: {err}"
        logger.error(msg)
        raise AuditExportError(msg) from err


def export_audit_report(
    report_data: dict[str, Any],
    output_path: str,
    format_type: str = "json",
    base_dir: str | None = None,
) -> str:
    """Export audit report to disk in specified format ('json' or 'csv').

    Args:
        report_data: Audit report dictionary.
        output_path: Destination file path.
        format_type: Format identifier ('json' or 'csv').
        base_dir: Optional base directory boundary for containment validation.

    Returns
    -------
        str: Absolute path of exported report file.
    """
    fmt = format_type.strip().lower()
    if fmt == "csv":
        return export_audit_report_csv(report_data, output_path, base_dir=base_dir)
    return export_audit_report_json(report_data, output_path, base_dir=base_dir)
