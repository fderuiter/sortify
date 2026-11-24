"""File movement and organization module.

This module is responsible for safely moving files to new directories.
"""

import asyncio
import concurrent.futures
import contextlib
import logging
import os
import shutil  # noqa: F401
import threading
import unicodedata
import uuid
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.core.domain_contracts import _get_val
from app.core.exceptions import SortifyBaseError
from app.core.lifecycle import LifecycleTelemetryMixin
from app.core.link_manager import LinkManager
from app.core.path_utils import is_junction_path, safe_relpath
from app.core.pipeline import PipelineLifecycleState
from app.core.progress import emit_progress
from app.core.verifier import VerificationEngine

try:
    import pylnk3
except ImportError:
    pylnk3 = None


class MoveEngineState(str, Enum):
    """Lifecycle states for AsyncMoveEngine."""

    IDLE = "IDLE"
    PREPARING = "PREPARING"
    MOVING_CHUNK = "MOVING_CHUNK"
    RESOLVING_LINKS = "RESOLVING_LINKS"
    CLEANING_DIRS = "CLEANING_DIRS"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ROLLING_BACK = "ROLLING_BACK"
    ROLLED_BACK = "ROLLED_BACK"


class InvalidMoveStateError(SortifyBaseError, RuntimeError, ValueError):
    """Raised when an invalid MoveEngineState transition is attempted."""


_VALID_MOVE_ENGINE_TRANSITIONS: dict[MoveEngineState, set[MoveEngineState]] = {
    MoveEngineState.IDLE: {
        MoveEngineState.IDLE,
        MoveEngineState.PREPARING,
    },
    MoveEngineState.PREPARING: {
        MoveEngineState.PREPARING,
        MoveEngineState.MOVING_CHUNK,
        MoveEngineState.RESOLVING_LINKS,
        MoveEngineState.CLEANING_DIRS,
        MoveEngineState.COMPLETED,
        MoveEngineState.CANCELLING,
        MoveEngineState.CANCELLED,
        MoveEngineState.FAILED,
    },
    MoveEngineState.MOVING_CHUNK: {
        MoveEngineState.MOVING_CHUNK,
        MoveEngineState.RESOLVING_LINKS,
        MoveEngineState.CLEANING_DIRS,
        MoveEngineState.COMPLETED,
        MoveEngineState.CANCELLING,
        MoveEngineState.CANCELLED,
        MoveEngineState.FAILED,
    },
    MoveEngineState.RESOLVING_LINKS: {
        MoveEngineState.RESOLVING_LINKS,
        MoveEngineState.CLEANING_DIRS,
        MoveEngineState.COMPLETED,
        MoveEngineState.CANCELLING,
        MoveEngineState.CANCELLED,
        MoveEngineState.FAILED,
    },
    MoveEngineState.CLEANING_DIRS: {
        MoveEngineState.CLEANING_DIRS,
        MoveEngineState.COMPLETED,
        MoveEngineState.CANCELLING,
        MoveEngineState.CANCELLED,
        MoveEngineState.FAILED,
    },
    MoveEngineState.CANCELLING: {
        MoveEngineState.CANCELLING,
        MoveEngineState.CANCELLED,
        MoveEngineState.FAILED,
    },
    MoveEngineState.CANCELLED: {
        MoveEngineState.CANCELLED,
        MoveEngineState.IDLE,
        MoveEngineState.PREPARING,
    },
    MoveEngineState.COMPLETED: {
        MoveEngineState.COMPLETED,
        MoveEngineState.IDLE,
        MoveEngineState.PREPARING,
    },
    MoveEngineState.FAILED: {
        MoveEngineState.FAILED,
        MoveEngineState.ROLLING_BACK,
        MoveEngineState.IDLE,
        MoveEngineState.PREPARING,
    },
    MoveEngineState.ROLLING_BACK: {
        MoveEngineState.ROLLING_BACK,
        MoveEngineState.ROLLED_BACK,
        MoveEngineState.FAILED,
    },
    MoveEngineState.ROLLED_BACK: {
        MoveEngineState.ROLLED_BACK,
        MoveEngineState.IDLE,
        MoveEngineState.PREPARING,
    },
}


class MoveOpState(str, Enum):
    """Operation states for individual move records."""

    INTENT = "intent"
    DISPLACEMENT = "displacement"
    DONE = "done"
    VERIFIED = "verified"
    FAILED = "failed"


class DirectoryCleanupStatus(str, Enum):
    """Status values for directory cleanup nodes."""

    TO_BE_DELETED = "To Be Deleted"
    PROTECTED = "Protected"


class MoveVerificationStatus(str, Enum):
    """Verification statuses for moved and retried files."""

    VERIFIED_PASS = "VERIFIED_PASS"
    MISMATCH_FAIL = "MISMATCH_FAIL"
    MOVE_FAIL = "MOVE_FAIL"
    SKIPPED = "SKIPPED"
    UNVERIFIED = "UNVERIFIED"


class MoveResult(dict):
    """Typed mover execution result preserving dictionary backward compatibility."""

    def __init__(
        self,
        session_id: str | None = None,
        deleted_folders: int = 0,
        protected_folders: int = 0,
        cancelled: bool = False,
        audit_report: dict[str, Any] | None = None,
        state: MoveEngineState | None = None,
        **kwargs: Any,
    ):
        data: dict[str, Any] = {
            "session_id": session_id,
            "deleted_folders": deleted_folders,
            "protected_folders": protected_folders,
            "cancelled": cancelled,
            "audit_report": audit_report,
        }
        if state is not None:
            data["state"] = state
        data.update(kwargs)
        super().__init__(data)

    @property
    def session_id(self) -> str | None:
        """Return the unique relocation session identifier."""
        return self.get("session_id")

    @session_id.setter
    def session_id(self, val: str | None) -> None:
        """Set the unique relocation session identifier."""
        self["session_id"] = val

    @property
    def deleted_folders(self) -> int:
        """Return count of deleted empty directories."""
        return self.get("deleted_folders", 0)

    @deleted_folders.setter
    def deleted_folders(self, val: int) -> None:
        """Set count of deleted empty directories."""
        self["deleted_folders"] = val

    @property
    def protected_folders(self) -> int:
        """Return count of protected directories preserved during cleanup."""
        return self.get("protected_folders", 0)

    @protected_folders.setter
    def protected_folders(self, val: int) -> None:
        """Set count of protected directories preserved during cleanup."""
        self["protected_folders"] = val

    @property
    def cancelled(self) -> bool:
        """Return whether relocation execution was cancelled."""
        return bool(self.get("cancelled", False))

    @cancelled.setter
    def cancelled(self, val: bool) -> None:
        """Set cancellation status flag."""
        self["cancelled"] = bool(val)

    @property
    def audit_report(self) -> dict[str, Any] | None:
        """Return structured audit report for file operations."""
        return self.get("audit_report")

    @audit_report.setter
    def audit_report(self, val: dict[str, Any] | None) -> None:
        """Set structured audit report for file operations."""
        self["audit_report"] = val

    @property
    def state(self) -> MoveEngineState | None:
        """Return terminal lifecycle state of the move engine."""
        return self.get("state")

    @state.setter
    def state(self, val: MoveEngineState | None) -> None:
        """Set terminal lifecycle state of the move engine."""
        self["state"] = val

    @property
    def ok(self) -> bool:
        """True when the move execution finished successfully without cancellation or failure."""
        return not self.cancelled and self.get("state") not in (
            MoveEngineState.FAILED,
            MoveEngineState.ROLLING_BACK,
            MoveEngineState.ROLLED_BACK,
        )

    def __getattr__(self, name: str) -> Any:
        """Retrieve attribute from internal dictionary storage."""
        try:
            return self[name]
        except KeyError:
            raise AttributeError(
                f"'MoveResult' object has no attribute {name!r}"
            ) from None

    def __setattr__(self, name: str, value: Any) -> None:
        """Store attribute in internal dictionary storage."""
        self[name] = value

    def __delattr__(self, name: str) -> None:
        """Remove attribute from internal dictionary storage."""
        try:
            del self[name]
        except KeyError:
            raise AttributeError(
                f"'MoveResult' object has no attribute {name!r}"
            ) from None


def _is_same_path(p1: str, p2: str) -> bool:
    if p1 is None or p2 is None:
        return p1 == p2
    return os.path.normcase(os.path.abspath(p1)) == os.path.normcase(
        os.path.abspath(p2)
    )


def _resolve_path(p: Path) -> Path:
    """Resolve symlinks and Windows 8.3 short paths, even if trailing components do not exist.

    Args:
        p: Path object to resolve.

    Returns
    -------
        Resolved Path object with symlinks and 8.3 short names expanded for existing ancestors.
    """
    try:
        p = p.expanduser()
    except Exception:
        pass

    try:
        if p.exists():
            return p.resolve()
    except Exception:
        pass

    parts = []
    curr = p
    while True:
        try:
            if curr.exists():
                break
        except Exception:
            pass
        parent_curr = curr.parent
        if parent_curr == curr:
            break
        parts.append(curr.name)
        curr = parent_curr

    try:
        curr_resolved = curr.resolve()
    except Exception:
        curr_resolved = curr

    for part in reversed(parts):
        curr_resolved = curr_resolved / part

    return curr_resolved


def is_subpath_or_equal(child: str, parent: str) -> bool:
    """Check if child path is equal to or nested within parent path (case-insensitive and cross-platform).

    Args:
        child: Candidate child path to check for subpath containment.
        parent: Boundary parent path to evaluate child against.

    Returns
    -------
        True if child is identical to or located within parent directory boundary, False otherwise.
    """
    if child is None or parent is None:
        return False

    clean_child = str(child).replace("\\", "/")
    clean_parent = str(parent).replace("\\", "/")

    try:
        p_child = _resolve_path(Path(clean_child))
        p_parent = _resolve_path(Path(clean_parent))

        if p_child == p_parent:
            return True
        try:
            if p_child.is_relative_to(p_parent):
                return True
        except (AttributeError, ValueError):
            pass

        s_child = str(p_child).replace("\\", "/").rstrip("/").lower()
        s_parent = str(p_parent).replace("\\", "/").rstrip("/").lower()

        if s_child == s_parent:
            return True
        if s_child.startswith(s_parent + "/"):
            return True
    except Exception:
        pass

    try:
        abs_c = os.path.abspath(clean_child).replace("\\", "/").rstrip("/").lower()
        abs_p = os.path.abspath(clean_parent).replace("\\", "/").rstrip("/").lower()

        if abs_c == abs_p:
            return True
        if abs_c.startswith(abs_p + "/"):
            return True
    except Exception:
        pass

    return False


def get_safe_path(dest_dir: str, filename: str, source_path: str | None = None) -> str:
    """Generate a safe file path to avoid overwriting existing files."""
    base, extension = os.path.splitext(filename)
    counter = 1
    safe_path = os.path.join(dest_dir, filename)
    while os.path.lexists(safe_path):
        if source_path and os.path.lexists(source_path):
            try:
                if os.path.samefile(safe_path, source_path):
                    return safe_path
            except OSError as e:
                if _is_same_path(safe_path, source_path):
                    return safe_path
                logging.error(
                    f"Failed to verify if paths conflict for {safe_path} and {source_path}: {e}",
                    exc_info=True,
                )
        safe_path = os.path.join(dest_dir, f"{base}_{counter}{extension}")
        counter += 1
    return safe_path


def _create_junction(target_path: str, junction_path: str):
    """Create an NTFS directory junction (or fallback symlink on non-Windows/test environments)."""
    abs_target = os.path.abspath(target_path)
    abs_junction = os.path.abspath(junction_path)
    try:
        import _winapi

        if hasattr(_winapi, "CreateJunction"):
            _winapi.CreateJunction(abs_target, abs_junction)
            return
    except (ImportError, AttributeError, OSError):
        pass

    os.symlink(target_path, junction_path, target_is_directory=True)


def _safe_replace_link(shadow_name: str, dest_path: str):
    """Safely replace dest_path with shadow_name, falling back to resilient deletion of dest_path if os.replace fails on Windows directory junctions/links."""
    try:
        os.replace(shadow_name, dest_path)
    except OSError:
        if os.path.lexists(dest_path) or is_junction_path(dest_path):
            from app.core.resilient_file_ops import resilient_remove

            resilient_remove(dest_path)
        os.replace(shadow_name, dest_path)


def resolve_new_target(abs_target: str, path_map: dict) -> str:
    """Resolve the updated target path if the target file or directory moved."""
    if not abs_target or not path_map:
        return abs_target

    if abs_target.startswith(("\\\\?\\", "\\??\\")):
        abs_target = abs_target[4:]

    norm_target = os.path.normcase(os.path.abspath(abs_target))

    if norm_target in path_map:
        return path_map[norm_target]

    target_clean = abs_target.replace("\\", "/").lower()

    for src_path, dst_path in path_map.items():
        src_clean = (
            src_path[4:] if src_path.startswith(("\\\\?\\", "\\??\\")) else src_path
        )
        src_norm = os.path.normcase(os.path.abspath(src_clean))
        if norm_target == src_norm:
            return dst_path
        if src_clean.replace("\\", "/").lower() == target_clean:
            return dst_path
        if norm_target.startswith(src_norm + os.sep) or target_clean.startswith(
            src_clean.replace("\\", "/").lower() + "/"
        ):
            rel = safe_relpath(abs_target, src_clean)
            return os.path.normpath(os.path.join(dst_path, rel))

    for src_file, dst_file in path_map.items():
        src_file_clean = (
            src_file[4:] if src_file.startswith(("\\\\?\\", "\\??\\")) else src_file
        )
        src_file_norm = os.path.normcase(os.path.abspath(src_file_clean))
        if src_file_norm.startswith(norm_target + os.sep) or src_file_clean.replace(
            "\\", "/"
        ).lower().startswith(target_clean + "/"):
            rel = safe_relpath(src_file_clean, abs_target)
            dst_file_str = str(dst_file)
            if (
                dst_file_str.replace("\\", "/")
                .lower()
                .endswith(rel.replace("\\", "/").lower())
            ):
                inferred = dst_file_str[: -len(rel)].rstrip("\\/")
                if inferred:
                    return os.path.normpath(inferred)

    return abs_target


def _update_path_map_entry(
    path_map: dict, source_path: str, dest_path: str, lock=None
) -> None:
    """Immediately update path_map with actual final dest_path, preserving subpath entries."""
    if path_map is None or not source_path or not dest_path:
        return

    def _apply():
        src_clean = (
            source_path[4:]
            if source_path.startswith(("\\\\?\\", "\\??\\"))
            else source_path
        )
        src_key = os.path.normcase(os.path.abspath(src_clean))
        abs_dest = os.path.abspath(dest_path)
        old_dest = path_map.get(src_key)
        path_map[src_key] = abs_dest

        if old_dest and os.path.normcase(os.path.abspath(old_dest)) != os.path.normcase(
            abs_dest
        ):
            for k in list(path_map.keys()):
                if k.startswith(src_key + os.sep):
                    rel = safe_relpath(k, src_key)
                    path_map[k] = os.path.normpath(os.path.join(abs_dest, rel))

    if lock:
        with lock:
            _apply()
    else:
        _apply()


def _resolve_pending_links(
    base_dir: str,
    pending_links: list,
    path_map: dict,
    db=None,
    session_id: str | None = None,
) -> None:
    """Secondary pass: resolve and update shortcut/link targets after all physical moves in batch complete."""
    if not pending_links:
        return

    for item in pending_links:
        dest_path = item["dest_path"]
        source_path = item["source_path"]
        link_info = item["link_info"]

        if not link_info:
            continue

        original_target = link_info["target"]
        abs_target = original_target
        if not os.path.isabs(original_target):
            abs_target = os.path.normpath(
                os.path.join(os.path.dirname(source_path), original_target)
            )

        new_abs_target = resolve_new_target(abs_target, path_map)

        needs_update = not _is_same_path(dest_path, source_path) or not _is_same_path(
            new_abs_target, abs_target
        )

        if needs_update and (
            os.path.lexists(dest_path) or os.path.lexists(source_path)
        ):
            shadow_name = f"{dest_path}.shadow_{uuid.uuid4().hex}"
            dest_dir = os.path.dirname(dest_path)
            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir, exist_ok=True)

            if link_info["type"] == "symlink":
                if not os.path.isabs(original_target):
                    final_target = safe_relpath(new_abs_target, dest_dir)
                else:
                    final_target = new_abs_target

                try:
                    os.symlink(final_target, shadow_name)
                    if not os.path.lexists(shadow_name):
                        raise RuntimeError("Shadow link creation failed validation.")
                    _safe_replace_link(shadow_name, dest_path)
                    if not _is_same_path(dest_path, source_path) and os.path.lexists(
                        source_path
                    ):
                        from app.core.resilient_file_ops import resilient_remove

                        resilient_remove(source_path)
                except Exception as e:
                    if os.path.lexists(shadow_name):
                        from app.core.resilient_file_ops import resilient_remove

                        resilient_remove(shadow_name)
                    if os.path.lexists(dest_path) and not _is_same_path(
                        dest_path, source_path
                    ):
                        from app.core.resilient_file_ops import resilient_remove

                        try:
                            resilient_remove(dest_path)
                        except OSError:
                            pass
                    logging.error(
                        f"Failed to atomically update symlink {dest_path}: {e}",
                        exc_info=True,
                    )
                    raise

            elif link_info["type"] == "junction":
                try:
                    _create_junction(new_abs_target, shadow_name)
                    if not (
                        os.path.lexists(shadow_name) or is_junction_path(shadow_name)
                    ):
                        raise RuntimeError(
                            "Shadow junction creation failed validation."
                        )
                    _safe_replace_link(shadow_name, dest_path)
                    if not _is_same_path(dest_path, source_path) and os.path.lexists(
                        source_path
                    ):
                        from app.core.resilient_file_ops import resilient_remove

                        resilient_remove(source_path)
                except Exception as e:
                    if os.path.lexists(shadow_name) or is_junction_path(shadow_name):
                        from app.core.resilient_file_ops import resilient_remove

                        resilient_remove(shadow_name)
                    if os.path.lexists(dest_path) and not _is_same_path(
                        dest_path, source_path
                    ):
                        from app.core.resilient_file_ops import resilient_remove

                        try:
                            resilient_remove(dest_path)
                        except OSError:
                            pass
                    logging.error(
                        f"Failed to atomically update junction {dest_path}: {e}",
                        exc_info=True,
                    )
                    raise

            elif link_info["type"] == "lnk" and pylnk3:
                try:
                    parsed = None
                    lnk_candidate = (
                        dest_path
                        if (dest_path and os.path.exists(dest_path))
                        else source_path
                    )
                    if lnk_candidate and os.path.exists(lnk_candidate):
                        parsed = pylnk3.parse(lnk_candidate)
                    kwargs = {}
                    if parsed:
                        kwargs = {
                            "arguments": getattr(parsed, "arguments", None),
                            "description": getattr(parsed, "description", None),
                            "icon_file": getattr(parsed, "icon", None),
                            "icon_index": getattr(parsed, "icon_index", 0),
                            "work_dir": getattr(parsed, "work_dir", None),
                            "window_mode": getattr(parsed, "window_mode", None),
                        }

                    pylnk3.for_file(new_abs_target, lnk_name=shadow_name, **kwargs)
                    if not os.path.lexists(shadow_name):
                        raise RuntimeError("Shadow link creation failed validation.")
                    _safe_replace_link(shadow_name, dest_path)
                    if not _is_same_path(dest_path, source_path) and os.path.lexists(
                        source_path
                    ):
                        from app.core.resilient_file_ops import resilient_remove

                        resilient_remove(source_path)
                except Exception as e:
                    if os.path.lexists(shadow_name):
                        from app.core.resilient_file_ops import resilient_remove

                        resilient_remove(shadow_name)
                    if os.path.lexists(dest_path) and not _is_same_path(
                        dest_path, source_path
                    ):
                        from app.core.resilient_file_ops import resilient_remove

                        try:
                            resilient_remove(dest_path)
                        except OSError:
                            pass
                    logging.error(
                        f"Failed to atomically update Windows shortcut {dest_path}: {e}",
                        exc_info=True,
                    )
                    raise


OVERWRITE_BACKUP_DIR = ".sortify_overwritten"
DUPLICATE_STAGING_DIR = ".sortify_duplicates"
_OWNED_HIDDEN_DIRS = {".branches", OVERWRITE_BACKUP_DIR, DUPLICATE_STAGING_DIR}


def overwrite_backup_dir(base_dir: str, session_id: str) -> str:
    """Return the folder where a session parks the files its overwrites displaced."""
    return os.path.join(base_dir, OVERWRITE_BACKUP_DIR, session_id)


def duplicate_staging_dir(base_dir: str, session_id: str) -> str:
    """Return the folder where a session parks verified duplicate or trashed copies for undo."""
    return os.path.join(base_dir, DUPLICATE_STAGING_DIR, session_id)


def _displace_for_overwrite(
    dest_path: str, base_dir: str, session_id, journal=None
) -> str:
    """Move the file at dest_path into the session's backup folder instead of deleting it.

    ``journal(path)`` is called with the backup path before the file moves, and
    again if the move had to pick another name, so undo can always find it.
    """
    from app.core.resilient_file_ops import resilient_move

    planned = os.path.join(
        overwrite_backup_dir(base_dir, session_id or f"nosession_{uuid.uuid4().hex}"),
        safe_relpath(dest_path, base_dir),
    )
    if journal:
        journal(planned)
    backup_path = resilient_move(dest_path, planned)
    if journal and not _is_same_path(backup_path, planned):
        journal(backup_path)
    logging.info(f"Overwrite: moved existing {dest_path} aside to {backup_path}")
    return backup_path


def _restore_displaced(backup_path: str, dest_path: str) -> None:
    """Put a displaced file back after the move that would have replaced it failed."""
    from app.core.resilient_file_ops import resilient_move

    try:
        restored = resilient_move(backup_path, dest_path)
    except Exception as err:
        logging.error(
            f"Overwrite failed and {dest_path} could not be restored; it is kept at {backup_path}: {err}"
        )
        return
    if not _is_same_path(restored, dest_path):
        logging.warning(
            f"Overwrite failed and {dest_path} is now occupied; restored the original to {restored}."
        )


def _record_move_intent(moves, session_id, source_path, dest_path, src_sha256):
    """Commit the move's intent row (ADR 0006) and return its seq.

    A failure raises, so the move never happens without its row.
    """
    if not (moves and session_id):
        return None
    size = None
    try:
        if os.path.isfile(source_path) and not os.path.islink(source_path):
            size = os.lstat(source_path).st_size
    except OSError:
        pass
    return moves.record_intent(
        session_id, source_path, dest_path, src_sha256=src_sha256, size=size
    )


def _record_move_intents_batch(
    moves: Any,
    session_id: str | None,
    intents: list[tuple[str, str, str | None, int | None]],
) -> list[int | None]:
    """Commit a chunk's pre-move ``intent`` rows in one batch before any file moves."""
    if not intents:
        return []
    if not (moves and session_id):
        return [None] * len(intents)
    if hasattr(moves, "record_intents"):
        seqs = moves.record_intents(session_id, intents)
        return [int(s) for s in seqs]
    return [
        moves.record_intent(session_id, src, dst, src_sha256=sha or None, size=sz)
        for src, dst, sha, sz in intents
    ]


def _flush_chunk_completions_and_db(
    db: Any,
    moves: Any,
    session_id: str | None,
    completions: list[dict[str, Any]],
    db_updates: list[dict[str, Any]],
) -> bool:
    """Commit chunk move state completions and document updates, coalescing into one transaction when shared."""
    has_moves = bool(moves and session_id and completions)
    has_db_updates = bool(db_updates and db and hasattr(db, "execute_batch_updates"))
    if not has_moves and not has_db_updates:
        return False

    can_coalesce = (
        has_moves
        and db is not None
        and hasattr(db, "execute_batch_updates")
        and "execute_batch_updates" not in getattr(db, "__dict__", {})
        and hasattr(db, "db_path")
        and hasattr(moves, "db_path")
        and hasattr(moves, "_apply_completions_on_conn")
        and os.path.abspath(str(db.db_path)) == os.path.abspath(str(moves.db_path))
        and "mark_done" not in getattr(moves, "__dict__", {})
        and "mark_verified" not in getattr(moves, "__dict__", {})
        and "mark_failed" not in getattr(moves, "__dict__", {})
    )
    if can_coalesce:
        db.execute_batch_updates(
            db_updates,
            extra_conn_ops=lambda conn: moves._apply_completions_on_conn(
                conn, session_id, completions
            ),
        )
        return True

    if has_moves:
        if hasattr(moves, "complete_moves"):
            moves.complete_moves(session_id, completions)
        else:
            for comp in completions:
                seq = comp["seq"]
                st = comp["state"]
                req = bool(comp.get("required", True))
                if st == MoveOpState.VERIFIED.value:
                    _record_move_state(
                        moves,
                        session_id,
                        seq,
                        MoveOpState.DONE,
                        required=req,
                        dst=comp.get("dst"),
                        displaced=comp.get("displaced"),
                    )
                    _record_move_state(
                        moves,
                        session_id,
                        seq,
                        MoveOpState.VERIFIED,
                        required=False,
                    )
                elif st == MoveOpState.DONE.value:
                    _record_move_state(
                        moves,
                        session_id,
                        seq,
                        MoveOpState.DONE,
                        required=req,
                        dst=comp.get("dst"),
                        displaced=comp.get("displaced"),
                    )
                elif st == MoveOpState.FAILED.value:
                    _record_move_state(
                        moves,
                        session_id,
                        seq,
                        MoveOpState.FAILED,
                        required=False,
                        error=comp.get("error"),
                    )

    if has_db_updates:
        db.execute_batch_updates(db_updates)
        return True

    return has_moves


def _get_safe_path_with_reservations(
    dest_dir: str,
    filename: str,
    source_path: str | None,
    reserved_dests: set[str],
) -> str:
    """Resolve a collision-free destination path accounting for both disk and in-chunk reservations."""
    candidate = get_safe_path(dest_dir, filename, source_path)
    norm_cand = os.path.normcase(os.path.abspath(candidate))
    if norm_cand not in reserved_dests:
        return candidate
    if source_path and _is_same_path(candidate, source_path):
        return candidate
    base, extension = os.path.splitext(filename)
    counter = 1
    while True:
        cand = os.path.join(dest_dir, f"{base}_{counter}{extension}")
        norm_c = os.path.normcase(os.path.abspath(cand))
        if norm_c not in reserved_dests and not os.path.lexists(cand):
            return cand
        counter += 1


def _record_move_state(
    moves, session_id, seq, state: MoveOpState | str, required: bool = True, **fields
) -> None:
    """Move the intent row ``seq`` to ``state``; a failure raises unless not ``required``."""
    if not (moves and session_id) or seq is None:
        return
    st_val = state.value if isinstance(state, MoveOpState) else str(state)
    method = "record_displacement" if st_val == "displacement" else f"mark_{st_val}"
    try:
        getattr(moves, method)(session_id, seq, **fields)
    except Exception as err:
        if required:
            raise
        logging.warning(f"Failed to mark move {session_id}#{seq} {st_val}: {err}")


def _execute_ledger_move(
    moves: Any,
    session_id: str,
    base_dir: str,
    src_path: str,
    dst_path: str,
    src_hash: str,
    *,
    allow_overwrite: bool = False,
) -> tuple[str, str | None]:
    """Record a MoveLedger intent, move src_path to dst_path, verify SHA-256, and mark VERIFIED."""
    from app.core.extractor import get_file_hash
    from app.core.resilient_file_ops import resilient_move

    seq = _record_move_intent(moves, session_id, src_path, dst_path, src_hash)
    displaced_path = None
    overwritten_path = None
    try:
        if (
            allow_overwrite
            and os.path.lexists(dst_path)
            and not _is_same_path(src_path, dst_path)
        ):
            overwritten_path = dst_path
            displaced_path = _displace_for_overwrite(
                dst_path,
                base_dir,
                session_id,
                journal=lambda path: _record_move_state(
                    moves,
                    session_id,
                    seq,
                    MoveOpState.DISPLACEMENT,
                    displaced=path,
                ),
            )
        try:
            dst_parent = os.path.dirname(os.path.abspath(dst_path))
            if dst_parent:
                os.makedirs(dst_parent, exist_ok=True)
            actual_dst = resilient_move(src_path, dst_path)
        except Exception:
            if displaced_path and overwritten_path:
                _restore_displaced(displaced_path, overwritten_path)
            raise
    except Exception as err:
        _record_move_state(
            moves,
            session_id,
            seq,
            MoveOpState.FAILED,
            required=False,
            error=str(err),
        )
        raise

    _record_move_state(
        moves,
        session_id,
        seq,
        MoveOpState.DONE,
        dst=actual_dst,
        displaced=displaced_path,
    )
    post_hash = None
    if os.path.isfile(actual_dst) and not os.path.islink(actual_dst):
        try:
            post_hash = get_file_hash(actual_dst)
        except OSError:
            post_hash = None
    if post_hash and (not src_hash or post_hash == src_hash):
        _record_move_state(
            moves,
            session_id,
            seq,
            MoveOpState.VERIFIED,
            required=False,
        )
    return actual_dst, post_hash


def _record_policy_quarantine_audit(
    db: Any,
    db_lock: threading.Lock,
    *,
    base_dir: str,
    original_filepath: str,
    staged_filepath: str,
    file_hash: str | None,
    policy_action: str,
    transitions: list[tuple[str, dict[str, Any]]],
    error_message: str | None = None,
) -> None:
    """Persist a quarantine_records audit lifecycle trail for unified policy execution."""
    if not db or not hasattr(db, "stage_quarantine_record"):
        return
    import time

    with db_lock:
        try:
            job_id = f"qjob_mover_{uuid.uuid4().hex[:12]}"
            db.stage_quarantine_record(
                job_id=job_id,
                base_dir=os.path.abspath(base_dir) if base_dir else base_dir,
                original_filepath=original_filepath,
                staged_filepath=staged_filepath,
                file_hash=file_hash,
                policy_action=policy_action,
            )
            if hasattr(db, "update_quarantine_status"):
                for idx, (status_val, extra_audit) in enumerate(transitions):
                    entry = {
                        "timestamp": time.time(),
                        "status": status_val,
                        **extra_audit,
                    }
                    err_kw = error_message if idx == len(transitions) - 1 else None
                    db.update_quarantine_status(
                        job_id=job_id,
                        status=status_val,
                        policy_action=policy_action,
                        error_message=err_kw,
                        audit_entry=entry,
                    )
        except Exception as audit_err:
            logging.warning(
                f"Failed to record policy quarantine audit entry for {original_filepath}: {audit_err}"
            )


def _is_foreign_hidden_dir(path: str) -> bool:
    name = os.path.basename(os.path.normpath(path))
    return name.startswith(".") and name not in _OWNED_HIDDEN_DIRS


def _remove_empty_dirs(path: str, protected_paths: list[str] | None = None):
    """Recursively remove empty directories, respecting protected paths."""
    if protected_paths:
        for p in protected_paths:
            if is_subpath_or_equal(path, p):
                return

    if not os.path.isdir(path) or is_junction_path(path) or os.path.islink(path):
        return

    # Hidden directories (.git, .venv, ...) are not ours, and empty ones can be
    # meaningful (e.g. .git/refs/tags), so leave them alone. ``.branches`` holds
    # the sorter's own rollback safety copies and may be pruned.
    if _is_foreign_hidden_dir(path):
        return

    for entry in os.listdir(path):
        entry_path = os.path.join(path, entry)
        if is_junction_path(entry_path) or os.path.islink(entry_path):
            continue
        if os.path.isdir(entry_path):
            _remove_empty_dirs(entry_path, protected_paths)

    if not os.listdir(path):
        from app.core.resilient_file_ops import resilient_remove

        try:
            resilient_remove(path)
        except OSError:
            pass


def _prune_emptied_dirs(
    dirs: set, base_dir: str, protected_paths: list[str] | None = None
) -> None:
    """Remove the given directories, and then their parents, while they are empty.

    Stops at ``base_dir``, at hidden directories and at protected paths.
    """
    from app.core.resilient_file_ops import resilient_remove

    base_norm = os.path.normcase(os.path.abspath(base_dir))
    for start in sorted(dirs, key=len, reverse=True):
        current = os.path.abspath(start)
        while True:
            cur_norm = os.path.normcase(current)
            if cur_norm == base_norm or not is_subpath_or_equal(current, base_dir):
                break
            if _is_foreign_hidden_dir(current) or current.lower().endswith(".app"):
                break
            if protected_paths and any(
                is_subpath_or_equal(current, p) for p in protected_paths
            ):
                break
            if (
                not os.path.isdir(current)
                or is_junction_path(current)
                or os.path.islink(current)
                or os.listdir(current)
            ):
                break
            try:
                resilient_remove(current)
            except OSError:
                break
            current = os.path.dirname(current)


def _is_cross_volume(src: str, dst: str) -> bool:
    """Check if moving src to dst crosses file system device or drive boundaries."""
    try:
        src_dev = os.stat(src).st_dev if os.path.lexists(src) else None
        dst_dir = os.path.dirname(dst)
        if not os.path.exists(dst_dir):
            os.makedirs(dst_dir, exist_ok=True)
        dst_dev = os.stat(dst_dir).st_dev
        if src_dev is not None and src_dev != dst_dev:
            return True
    except OSError:
        pass
    try:
        src_drive = os.path.splitdrive(os.path.abspath(src))[0].upper()
        dst_drive = os.path.splitdrive(os.path.abspath(dst))[0].upper()
        if src_drive and dst_drive and src_drive != dst_drive:
            return True
    except Exception:
        pass
    return False


def _resolve_source_path(
    base_dir: str,
    key: str,
    content: Any,
    active_parent_path: str = "",
    depth: int = 0,
) -> str:
    """Resolve normalized absolute source path for a plan node."""
    src_p = _get_val(content, "source_path") or _get_val(content, "filepath")
    src_root = _get_val(content, "source_root")
    rel_src = _get_val(content, "relative_source") or key
    raw_key = key

    if isinstance(src_p, str) and "::" in src_p:
        parsed_root, parsed_sp = src_p.split("::", 1)
        src_root = src_root or parsed_root
        src_p = parsed_sp

    if isinstance(raw_key, str) and "::" in raw_key:
        parsed_root, raw_key = raw_key.split("::", 1)
        src_root = src_root or parsed_root

    if "::" in rel_src:
        parsed_root, parsed_rel = rel_src.split("::", 1)
        src_root = src_root or parsed_root
        rel_src = parsed_rel

    effective_base = src_root if src_root else base_dir
    abs_sp = None

    if src_p:
        abs_sp = os.path.normpath(
            src_p if os.path.isabs(src_p) else os.path.join(effective_base, src_p)
        )
        if os.path.lexists(abs_sp):
            return abs_sp

    if depth > 0 and active_parent_path:
        alt = os.path.normpath(os.path.join(effective_base, rel_src))
        primary = os.path.normpath(
            os.path.join(effective_base, active_parent_path, rel_src)
        )
        key_cand = os.path.normpath(os.path.join(effective_base, raw_key))
        if os.path.lexists(alt) and not _is_same_path(alt, primary):
            return alt
        if os.path.lexists(primary):
            return primary
        if os.path.lexists(alt):
            return alt
        if os.path.lexists(key_cand):
            return key_cand
        if abs_sp is not None:
            return abs_sp
        return primary

    cand = os.path.normpath(os.path.join(effective_base, rel_src))
    if os.path.lexists(cand):
        return cand
    if abs_sp is not None:
        return abs_sp
    return cand


def _get_node_mtime(
    base_dir: str,
    key: str,
    content: Any,
    active_parent_path: str = "",
    depth: int = 0,
) -> float:
    """Recursively calculate the minimum modification timestamp (st_mtime) for a plan node."""
    node_type = _get_val(content, "node_type") or _get_val(content, "__type__")
    rel_src = _get_val(content, "relative_source")
    target_fn = _get_val(content, "target_filename")

    if (
        content is None
        or node_type in ("file", "directory")
        or rel_src is not None
        or target_fn is not None
    ):
        if node_type == "directory":
            return float("inf")

        source_path = _resolve_source_path(
            base_dir, key, content, active_parent_path, depth
        )

        try:
            if os.path.lexists(source_path):
                return os.stat(source_path).st_mtime
        except OSError:
            pass
        return float("inf")
    elif isinstance(content, dict) or hasattr(content, "items"):
        items = (
            content.items()
            if hasattr(content, "items")
            else getattr(content, "plan", {}).items()
        )
        min_mtime = float("inf")
        sub_parent = os.path.join(active_parent_path, key)
        for sub_key, sub_content in items:
            m = _get_node_mtime(base_dir, sub_key, sub_content, sub_parent, depth + 1)
            if m < min_mtime:
                min_mtime = m
        return min_mtime
    return float("inf")


def _get_node_priority_key(
    base_dir: str,
    key: str,
    content: Any,
    active_parent_path: str = "",
    depth: int = 0,
) -> tuple[int, float, float]:
    """Recursively calculate priority key tuple (archival_priority, -archival_priority_score, st_mtime) for a plan node."""
    node_type = _get_val(content, "node_type") or _get_val(content, "__type__")
    rel_src = _get_val(content, "relative_source")
    target_fn = _get_val(content, "target_filename")

    if (
        content is None
        or node_type in ("file", "directory")
        or rel_src is not None
        or target_fn is not None
    ):
        if node_type == "directory":
            return (999, 0.0, float("inf"))

        source_path = _resolve_source_path(
            base_dir, key, content, active_parent_path, depth
        )

        mtime = float("inf")
        try:
            if os.path.lexists(source_path):
                mtime = os.stat(source_path).st_mtime
        except OSError:
            pass

        arch_prio = 5
        arch_score = 0.0
        prio = _get_val(content, "archival_priority")
        if prio is not None:
            try:
                arch_prio = int(prio)
            except (ValueError, TypeError):
                arch_prio = 5
        score = _get_val(content, "archival_priority_score")
        if score is not None:
            try:
                arch_score = float(score)
            except (ValueError, TypeError):
                arch_score = 0.0

        return (arch_prio, -arch_score, mtime)

    elif isinstance(content, dict) or hasattr(content, "items"):
        items = (
            content.items()
            if hasattr(content, "items")
            else getattr(content, "plan", {}).items()
        )
        child_keys = []
        sub_parent = os.path.join(active_parent_path, key)
        for sub_key, sub_content in items:
            k = _get_node_priority_key(
                base_dir, sub_key, sub_content, sub_parent, depth + 1
            )
            child_keys.append(k)
        if child_keys:
            return min(child_keys)
        return (999, 0.0, float("inf"))

    return (5, 0.0, float("inf"))


def _is_cancelled(token) -> bool:
    """Evaluate if a cancellation token, flag, event or callable requests cancellation."""
    if token is None:
        return False
    if callable(token):
        try:
            return bool(token())
        except Exception:
            return False
    if hasattr(token, "is_set") and callable(token.is_set):
        return bool(token.is_set())
    if hasattr(token, "is_cancelled"):
        val = token.is_cancelled
        return bool(val() if callable(val) else val)
    if hasattr(token, "cancelled") and callable(token.cancelled):
        return bool(token.cancelled())
    return False


def _get_item_mtime(item):
    src = item.get("source_path")
    if src:
        try:
            if os.path.lexists(src):
                return os.stat(src).st_mtime
        except OSError:
            pass
    return float("inf")


def _get_item_age_tier(item):
    mtime = _get_item_mtime(item)
    if mtime == float("inf"):
        return (999, float("inf"))
    import time

    now = time.time()
    age_days = (now - mtime) / 86400.0
    if age_days >= 365:
        tier = 0  # > 365 days (Archival)
    elif age_days >= 90:
        tier = 1  # 90-365 days (Stale)
    elif age_days >= 30:
        tier = 2  # 30-90 days (Recent)
    else:
        tier = 3  # < 30 days (New)
    return (tier, mtime)


def _get_item_priority_key(item: dict) -> tuple[int, float, float]:
    content = item.get("content")
    if isinstance(content, dict):
        prio = content.get("archival_priority")
        try:
            arch_prio = int(prio) if prio is not None else 5
        except (ValueError, TypeError):
            arch_prio = 5
        score = content.get("archival_priority_score")
        try:
            arch_score = float(score) if score is not None else 0.0
        except (ValueError, TypeError):
            arch_score = 0.0
    else:
        arch_prio = 5
        arch_score = 0.0
    mtime = _get_item_mtime(item)
    return (arch_prio, -arch_score, mtime)


def _collect_move_items(
    base_dir: str,
    plan: dict,
    current_dest: str = "",
    active_parent_path: str = "",
    depth: int = 0,
    priority: str | None = None,
) -> list:
    """Traverse relocation plan and collect flat list of file move items."""
    base_dir = os.path.normpath(base_dir)
    items = []

    curr_plan = (
        plan.plan if hasattr(plan, "plan") and isinstance(plan.plan, dict) else plan
    )
    if not isinstance(curr_plan, dict):
        return items

    sorted_plan_items = sorted(
        curr_plan.items(),
        key=lambda item: _get_node_priority_key(
            base_dir, item[0], item[1], active_parent_path, depth
        ),
    )

    for key, content in sorted_plan_items:
        node_type = _get_val(content, "node_type") or _get_val(content, "__type__")
        is_leaf = (
            content is None
            or node_type in ("file", "directory")
            or isinstance(content, BaseModel)
        )
        if is_leaf:
            if node_type == "directory":
                continue
            if (
                _get_val(content, "is_excluded")
                or _get_val(content, "excluded")
                or _get_val(content, "is_checked") is False
            ):
                continue

            if _get_val(content, "status") == "Already Sorted":
                pass

            if depth > 0 and _get_val(content, "relative_source") is None:
                if content is not None and not (isinstance(content, (dict, BaseModel))):
                    raise ValueError(
                        f"Missing required relative source metadata field for nested item '{key}'"
                    )

            source_path = _resolve_source_path(
                base_dir, key, content, active_parent_path, depth
            )

            from app.core.scanner import parse_item_identifier

            tgt_fn = _get_val(content, "target_filename")
            if tgt_fn is not None:
                _, clean_tf = parse_item_identifier(str(tgt_fn), base_dir)
                filename = os.path.basename(clean_tf)
            else:
                _, rel_key = parse_item_identifier(str(key), base_dir)
                filename = os.path.basename(rel_key)

            items.append(
                {
                    "key": key,
                    "content": content,
                    "source_path": source_path,
                    "filename": filename,
                    "current_dest": current_dest,
                    "active_parent_path": active_parent_path,
                    "depth": depth,
                }
            )
        else:
            items.extend(
                _collect_move_items(
                    base_dir,
                    content,
                    os.path.join(current_dest, key),
                    os.path.join(active_parent_path, key),
                    depth + 1,
                    priority=priority,
                )
            )

    if depth == 0:
        p_mode = (
            priority.strip().lower() if isinstance(priority, str) else None
        ) or "priority"
        if p_mode in ("age_tier", "tier", "age_tier_desc"):
            items.sort(key=_get_item_age_tier)
        elif p_mode in ("mtime", "age", "oldest_first"):
            items.sort(key=_get_item_mtime)
        elif p_mode in ("newest_first", "recent"):
            items.sort(key=lambda x: -_get_item_mtime(x))
        elif p_mode in ("standard", "traversal", "none"):
            pass
        else:
            items.sort(key=_get_item_priority_key)

    return items


def _get_target_lock(
    target_locks: dict[str, threading.Lock] | None,
    lock_guard: threading.Lock,
    target_path: str,
) -> contextlib.AbstractContextManager[Any]:
    """Return a per-destination lock so concurrent workers never race on the same target path."""
    if target_locks is None:
        return contextlib.nullcontext()
    norm_target = os.path.normcase(os.path.abspath(target_path))
    with lock_guard:
        t_lock = target_locks.get(norm_target)
        if t_lock is None:
            t_lock = threading.Lock()
            target_locks[norm_target] = t_lock
    return t_lock


def _process_move_group(
    base_dir: str,
    group_items: list[dict],
    db,
    path_map: dict,
    runtime_settings,
    session_id: str,
    history_manager,
    db_lock: threading.Lock,
    db_updates_batch: list,
    audit_records: list | None = None,
    pending_links: list | None = None,
    chunk_hash_map: dict | None = None,
    target_locks: dict[str, threading.Lock] | None = None,
    unarchived_children_out: list[str] | None = None,
) -> list[bool]:
    """Execute a bucket of move items targeting the same destination sequentially in plan order."""
    results: list[bool] = []
    for item in group_items:
        if _process_move_item is _DEFAULT_PROCESS_MOVE_ITEM:
            results.append(
                _process_move_item(
                    base_dir,
                    item,
                    db,
                    path_map,
                    runtime_settings,
                    session_id,
                    history_manager,
                    db_lock,
                    db_updates_batch,
                    audit_records,
                    pending_links,
                    chunk_hash_map,
                    target_locks=target_locks,
                    unarchived_children_out=unarchived_children_out,
                )
            )
        else:
            results.append(
                _process_move_item(
                    base_dir,
                    item,
                    db,
                    path_map,
                    runtime_settings,
                    session_id,
                    history_manager,
                    db_lock,
                    db_updates_batch,
                    audit_records,
                    pending_links,
                    chunk_hash_map,
                )
            )
    return results


def _resolve_prepared_destination(
    *,
    base_dir: str,
    prepared: dict[str, Any],
    db: Any,
    path_map: dict,
    db_lock: threading.Lock,
    db_updates_batch: list,
    pending_links: list | None,
    reserved_dests: set[str] | None = None,
) -> str:
    """Resolve collision policy, boundary safety, quarantine staging, and link tracking for a prepared item.

    Returns ``"skipped"`` when the source vanished or ``conflict_policy == "skip"``
    bypassed a collision, ``"noop"`` when source and destination resolve to the
    same path without multi-step policy transforms, ``"special_policy"`` when
    ``unarchive`` or ``redact`` handling should run, or ``"ready"`` when standard
    physical relocation should proceed.
    """
    source_path = prepared["source_path"]
    if not os.path.lexists(source_path):
        return "skipped"

    dest_dir = prepared["dest_dir"]
    filename = prepared["filename"]
    conflict_policy = prepared["conflict_policy"]

    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)

    target_path = os.path.normpath(os.path.join(dest_dir, filename))
    norm_target = os.path.normcase(os.path.abspath(target_path))
    collision = False
    if os.path.lexists(target_path):
        is_same = False
        if os.path.lexists(source_path):
            try:
                is_same = os.path.samefile(target_path, source_path)
            except OSError:
                is_same = _is_same_path(target_path, source_path)
        if not is_same:
            collision = True
    elif (
        reserved_dests is not None
        and norm_target in reserved_dests
        and not _is_same_path(target_path, source_path)
    ):
        collision = True

    if collision and conflict_policy == "skip":
        logging.info(
            f"Collision detected for {target_path}. Policy is 'skip', bypassing move."
        )
        return "skipped"

    if collision and conflict_policy == "overwrite":
        dest_path = target_path
    elif reserved_dests is not None:
        dest_path = os.path.normpath(
            _get_safe_path_with_reservations(
                dest_dir, filename, source_path, reserved_dests
            )
        )
    else:
        dest_path = os.path.normpath(get_safe_path(dest_dir, filename, source_path))
    dest_path = unicodedata.normalize("NFC", dest_path)

    if not is_subpath_or_equal(dest_path, base_dir):
        msg = f"Runtime boundary violation: Destination path '{dest_path}' attempts to escape target base directory '{base_dir}'."
        logging.error(msg)
        raise ValueError(msg)

    if reserved_dests is not None:
        reserved_dests.add(os.path.normcase(os.path.abspath(dest_path)))
    _update_path_map_entry(path_map, source_path, dest_path, lock=db_lock)

    rel_dest = safe_relpath(dest_path, base_dir).replace("\\", "/")
    abs_dest_path = os.path.abspath(dest_path)
    prepared["dest_path"] = dest_path
    prepared["collision"] = collision

    if prepared["is_sensitivity_hold"] and db:
        with db_lock:
            try:
                import time

                job_id = f"qjob_mover_{uuid.uuid4().hex[:12]}"
                if hasattr(db, "stage_quarantine_record"):
                    db.stage_quarantine_record(
                        job_id=job_id,
                        base_dir=prepared["abs_base_dir"] or base_dir,
                        original_filepath=prepared["source_rel_path"],
                        staged_filepath=abs_dest_path,
                        file_hash=prepared["candidate_sha256"],
                        policy_action="sensitivity_hold",
                    )
                if hasattr(db, "update_quarantine_status"):
                    db.update_quarantine_status(
                        job_id=job_id,
                        status="IN_INSPECTION",
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "IN_INSPECTION",
                            "details": "Downstream AsyncMoveEngine compliance inspection",
                        },
                    )
                    db.update_quarantine_status(
                        job_id=job_id,
                        status="QUARANTINED",
                        policy_action="sensitivity_hold",
                        audit_entry={
                            "timestamp": time.time(),
                            "status": "QUARANTINED",
                            "policy_action": "sensitivity_hold",
                            "details": (
                                f"Downstream AsyncMoveEngine barrier intercepted {prepared['sens_rating_str']} sensitivity file; "
                                f"redirected to {prepared['quarantine_dir_name']}"
                            ),
                            "sensitivity_rating": prepared["sens_rating_str"],
                            "sensitivity_score": prepared["sens_score"],
                            "source_path": prepared["source_rel_path"],
                            "destination_path": rel_dest,
                        },
                    )
            except Exception as audit_err:
                logging.warning(
                    f"Failed to record mover compliance audit entry: {audit_err}"
                )

    if prepared["link_info"] and pending_links is not None:
        link_entry = {
            "source_path": source_path,
            "dest_path": dest_path,
            "link_info": prepared["link_info"],
        }
        prepared["link_entry"] = link_entry
        with db_lock:
            pending_links.append(link_entry)

    policy_action = prepared.get("policy_action") or ""
    if policy_action in ("unarchive", "redact") and not prepared["is_sensitivity_hold"]:
        return "special_policy"

    if dest_path == source_path:
        if (
            policy_action in ("quarantine", "archive", "retain")
            and not prepared["is_sensitivity_hold"]
        ):
            status_for_action = (
                "QUARANTINED"
                if policy_action == "quarantine"
                else ("ARCHIVED" if policy_action == "archive" else "RELEASED")
            )
            _record_policy_quarantine_audit(
                db,
                db_lock,
                base_dir=base_dir,
                original_filepath=prepared["source_rel_path"],
                staged_filepath=abs_dest_path,
                file_hash=prepared["pre_hash"] or prepared["file_hash"],
                policy_action=policy_action,
                transitions=[
                    (
                        "IN_INSPECTION",
                        {"details": f"SortPipeline {policy_action} policy inspection"},
                    ),
                    (
                        status_for_action,
                        {
                            "policy_action": policy_action,
                            "details": f"Policy action {policy_action} applied in place at {rel_dest}",
                        },
                    ),
                ],
            )
        if prepared["file_hash"] and not prepared["is_quarantine_dest"]:
            with db_lock:
                db_updates_batch.append(
                    {
                        "type": "verified_target",
                        "args": (
                            base_dir,
                            prepared["file_hash"],
                            prepared["current_dest"].replace("\\", "/"),
                        ),
                    }
                )
        return "noop"

    return "ready"


def _process_move_item(
    base_dir: str,
    item: dict,
    db,
    path_map: dict,
    runtime_settings,
    session_id: str,
    history_manager,
    db_lock: threading.Lock,
    db_updates_batch: list,
    audit_records: list | None = None,
    pending_links: list | None = None,
    chunk_hash_map: dict | None = None,
    target_locks: dict[str, threading.Lock] | None = None,
    unarchived_children_out: list[str] | None = None,
) -> bool:
    """Execute a single physical move and SHA-256 hash calculation off-thread."""
    prepared = _prepare_chunk_move_item(
        base_dir, item, db, runtime_settings, db_lock, chunk_hash_map
    )
    if prepared is None:
        return False

    target_path = os.path.normpath(
        os.path.join(prepared["dest_dir"], prepared["filename"])
    )
    with _get_target_lock(target_locks, db_lock, target_path):
        resolution = _resolve_prepared_destination(
            base_dir=base_dir,
            prepared=prepared,
            db=db,
            path_map=path_map,
            db_lock=db_lock,
            db_updates_batch=db_updates_batch,
            pending_links=pending_links,
            reserved_dests=None,
        )
        if resolution == "skipped":
            return False
        if resolution == "noop":
            return True
        if resolution == "special_policy":
            return _execute_special_policy_move(
                base_dir=base_dir,
                prepared=prepared,
                target_path=target_path,
                db=db,
                path_map=path_map,
                session_id=session_id,
                history_manager=history_manager,
                db_lock=db_lock,
                db_updates_batch=db_updates_batch,
                audit_records=audit_records,
                unarchived_children_out=unarchived_children_out,
            )

        moves = getattr(history_manager, "moves", None) if history_manager else None
        prepared["move_seq"] = _record_move_intent(
            moves,
            session_id,
            prepared["source_path"],
            prepared["dest_path"],
            prepared["pre_hash"],
        )
        try:
            _execute_prepared_move_group(
                base_dir,
                [prepared],
                path_map,
                session_id,
                moves,
                db_lock,
                db=db,
            )
        except Exception as err:
            _record_move_state(
                moves,
                session_id,
                prepared["move_seq"],
                MoveOpState.FAILED,
                required=False,
                error=str(err),
            )
            raise

        outcome = prepared["outcome"]
        _record_move_state(
            moves,
            session_id,
            prepared["move_seq"],
            MoveOpState.DONE,
            dst=outcome["dst"],
            displaced=outcome["displaced"],
        )
        if outcome["state"] == MoveOpState.VERIFIED.value:
            _record_move_state(
                moves,
                session_id,
                prepared["move_seq"],
                MoveOpState.VERIFIED,
                required=False,
            )
        with db_lock:
            db_updates_batch.extend(outcome["db_updates"])
            if audit_records is not None:
                audit_records.append(outcome["audit_record"])
        return True


_DEFAULT_PROCESS_MOVE_ITEM = _process_move_item


def _prepare_chunk_move_item(
    base_dir: str,
    item: dict,
    db: Any,
    runtime_settings: Any,
    db_lock: threading.Lock,
    chunk_hash_map: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Pre-compute source metadata, sensitivity routing, and pre-move SHA-256 off-thread without DB writes."""
    source_path = item["source_path"]
    if not os.path.lexists(source_path):
        return None

    if runtime_settings is not None:
        from app.core.scanner import ScanFilter

        item_filter = ScanFilter.from_settings(
            settings=runtime_settings, base_dir=base_dir, max_depth=None
        )
        if (
            item_filter.is_protected(source_path)
            or item_filter.is_app_bundle(source_path)
            or item_filter.has_ignored_extension(source_path)
        ):
            return None

    content = item["content"]
    filename = item["filename"]
    current_dest = item["current_dest"]

    raw_policy_action = _get_val(content, "policy_action")
    policy_action = str(raw_policy_action).strip().lower() if raw_policy_action else ""
    post_unarchive_action = (
        str(_get_val(content, "post_unarchive_action") or "delete").strip().lower()
    )
    post_unarchive_target = _get_val(content, "post_unarchive_target")

    # Downstream Barrier: Evaluate Sensitivity Metadata Before Physical Relocation
    sens_rating = _get_val(content, "sensitivity_rating")
    sens_score = _get_val(content, "sensitivity_score") or 0.0

    sens_rating_str = str(sens_rating or "LOW").upper()
    auto_ratings = [
        r.upper()
        for r in (getattr(runtime_settings, "AUTO_QUARANTINE_RATINGS", []) or [])
        if r
    ]
    quarantine_dir_name = (
        getattr(runtime_settings, "QUARANTINE_DIR_NAME", "_Compliance_Quarantine")
        or "_Compliance_Quarantine"
    )

    abs_base_dir = os.path.abspath(base_dir) if base_dir else ""
    candidate_sha256: str | None = None
    is_approved_release = False
    if sens_rating_str in auto_ratings or policy_action == "quarantine":
        from app.core.quarantine_interceptor import _is_content_approved_for_release
        from app.core.resilient_file_ops import resilient_file_hash

        if os.path.exists(source_path):
            try:
                candidate_sha256 = resilient_file_hash(source_path)
            except Exception:
                candidate_sha256 = None
        is_approved_release = _is_content_approved_for_release(
            db,
            base_dir=abs_base_dir,
            file_hash=candidate_sha256,
            file_path=source_path,
        )

    if is_approved_release and policy_action == "quarantine":
        policy_action = ""

    is_sensitivity_hold = sens_rating_str in auto_ratings and not is_approved_release
    active_dest = current_dest
    if policy_action == "quarantine" and not active_dest:
        active_dest = quarantine_dir_name
    elif policy_action == "archive" and not active_dest:
        active_dest = "Archive"
    elif policy_action == "retain" and not active_dest:
        active_dest = "Retained_Documents"
    elif policy_action == "redact" and not active_dest:
        active_dest = "Redacted_Documents"

    if is_sensitivity_hold:
        if active_dest != quarantine_dir_name:
            logging.warning(
                f"Downstream Barrier Interception: File {source_path} has sensitivity rating {sens_rating_str}. "
                f"Blocking move to '{current_dest}' and redirecting to '{quarantine_dir_name}'."
            )
        active_dest = quarantine_dir_name

    dest_dir = os.path.normpath(os.path.join(base_dir, active_dest))
    dest_dir = unicodedata.normalize("NFC", dest_dir)

    node_overwrite = bool(
        _get_val(content, "overwrite") or _get_val(content, "allow_overwrite")
    )
    node_policy_raw = _get_val(content, "conflict_policy")
    if node_overwrite:
        conflict_policy = "overwrite"
    elif isinstance(node_policy_raw, str) and node_policy_raw.lower() in (
        "rename",
        "skip",
        "overwrite",
    ):
        conflict_policy = node_policy_raw.lower()
    elif runtime_settings:
        conflict_policy = getattr(runtime_settings, "CONFLICT_POLICY", "rename")
    else:
        conflict_policy = "rename"

    source_rel_path = safe_relpath(source_path, base_dir).replace("\\", "/")

    if chunk_hash_map is not None:
        file_hash = chunk_hash_map.get(source_rel_path) or ""
    else:
        doc = None
        if db and hasattr(db, "get_document"):
            with db_lock:
                doc = db.get_document(base_dir, source_rel_path)
        file_hash = doc.get("file_hash") if (doc and isinstance(doc, dict)) else ""

    pre_hash = ""
    src_dev = None
    src_ino = None
    src_size = None
    intent_size = None
    if os.path.exists(source_path):
        try:
            st = os.stat(source_path)
            src_dev = st.st_dev
            src_ino = st.st_ino
            src_size = st.st_size
        except Exception:
            pass
        try:
            if os.path.isfile(source_path) and not os.path.islink(source_path):
                intent_size = os.lstat(source_path).st_size
        except OSError:
            pass
        try:
            from app.core.extractor import get_file_hash

            pre_hash = get_file_hash(source_path)
        except Exception:
            pre_hash = ""

    link_info = LinkManager.get_link_info(source_path)
    if not link_info:
        if is_junction_path(source_path):
            try:
                target = os.readlink(source_path)
                if target.startswith(("\\\\?\\", "\\??\\")):
                    target = target[4:]
                link_info = {"type": "junction", "target": target}
            except OSError:
                pass
        elif os.path.islink(source_path):
            try:
                target = os.readlink(source_path)
                if target.startswith(("\\\\?\\", "\\??\\")):
                    target = target[4:]
                link_info = {"type": "symlink", "target": target}
            except OSError:
                pass

    qdir_top = quarantine_dir_name.replace("\\", "/").strip("/").split("/")[0]
    active_dest_top = active_dest.replace("\\", "/").strip("/").split("/")[0]
    is_quarantine_dest = active_dest_top in (
        "_Compliance_Quarantine",
        "_Quarantine_Staging",
        qdir_top,
    )

    return {
        "source_path": source_path,
        "content": content,
        "filename": filename,
        "current_dest": current_dest,
        "active_dest": active_dest,
        "dest_dir": dest_dir,
        "conflict_policy": conflict_policy,
        "source_rel_path": source_rel_path,
        "abs_base_dir": abs_base_dir,
        "is_sensitivity_hold": is_sensitivity_hold,
        "sens_rating_str": sens_rating_str,
        "sens_score": sens_score,
        "quarantine_dir_name": quarantine_dir_name,
        "candidate_sha256": candidate_sha256,
        "file_hash": file_hash,
        "pre_hash": pre_hash,
        "src_dev": src_dev,
        "src_ino": src_ino,
        "src_size": src_size,
        "intent_size": intent_size,
        "link_info": link_info,
        "is_quarantine_dest": is_quarantine_dest,
        "policy_action": policy_action,
        "post_unarchive_action": post_unarchive_action,
        "post_unarchive_target": post_unarchive_target,
        "dest_path": None,
        "collision": False,
        "move_seq": None,
        "link_entry": None,
        "outcome": None,
    }


def _execute_special_policy_move(
    *,
    base_dir: str,
    prepared: dict[str, Any],
    target_path: str,
    db: Any,
    path_map: dict,
    session_id: str,
    history_manager: Any,
    db_lock: threading.Lock,
    db_updates_batch: list,
    audit_records: list | None,
    unarchived_children_out: list[str] | None,
) -> bool:
    """Execute multi-step MoveLedger policy transforms (``unarchive`` or ``redact``)."""
    source_path = prepared["source_path"]
    if not os.path.lexists(source_path):
        return False

    content = prepared.get("content")
    filename = prepared["filename"]
    dest_dir = prepared["dest_dir"]
    conflict_policy = prepared["conflict_policy"]
    collision = bool(prepared.get("collision", False))
    source_rel_path = prepared["source_rel_path"]
    quarantine_dir_name = prepared["quarantine_dir_name"]
    file_hash = prepared["file_hash"]
    pre_hash = prepared["pre_hash"]
    policy_action = prepared.get("policy_action") or ""
    post_unarchive_action = prepared.get("post_unarchive_action") or "delete"
    post_unarchive_target = prepared.get("post_unarchive_target")
    moves = getattr(history_manager, "moves", None) if history_manager else None

    if policy_action == "unarchive":
        import tarfile
        import zipfile

        from app.core.exceptions import ArchiveSafetyError
        from app.core.forensic_scanner import ForensicScanner
        from app.core.resilient_file_ops import resilient_rmtree

        extract_stage_dir = os.path.join(
            overwrite_backup_dir(base_dir, session_id),
            "unarchive_children",
            uuid.uuid4().hex[:12],
        )
        os.makedirs(extract_stage_dir, exist_ok=True)
        try:
            staged_children = ForensicScanner().unpack_archive(
                source_path, extract_stage_dir
            )
        except (ArchiveSafetyError, zipfile.BadZipFile, tarfile.TarError) as ase:
            resilient_rmtree(extract_stage_dir)
            q_dir = os.path.normpath(os.path.join(base_dir, quarantine_dir_name))
            os.makedirs(q_dir, exist_ok=True)
            q_dst = get_safe_path(q_dir, os.path.basename(source_path), source_path)
            actual_q_dst = q_dst
            post_q_hash = pre_hash
            if not _is_same_path(source_path, q_dst):
                actual_q_dst, post_q_hash = _execute_ledger_move(
                    moves, session_id, base_dir, source_path, q_dst, pre_hash
                )
            _update_path_map_entry(path_map, source_path, actual_q_dst, lock=db_lock)
            err_msg = f"Archive safety violation during unarchive: {ase}"
            _record_policy_quarantine_audit(
                db,
                db_lock,
                base_dir=base_dir,
                original_filepath=source_rel_path,
                staged_filepath=os.path.abspath(actual_q_dst),
                file_hash=pre_hash or file_hash,
                policy_action="unarchive",
                transitions=[
                    (
                        "IN_INSPECTION",
                        {"details": "SortPipeline archive safety inspection"},
                    ),
                    (
                        "MANUAL_REVIEW_REQUIRED",
                        {"details": f"Unarchive policy action aborted: {ase}"},
                    ),
                ],
                error_message=err_msg,
            )
            if audit_records is not None:
                with db_lock:
                    audit_records.append(
                        {
                            "source_path": source_path,
                            "destination_path": actual_q_dst,
                            "pre_hash": pre_hash,
                            "post_hash": post_q_hash,
                        }
                    )
            return True

        # 1. Handle original archive source before placing children so undo replays
        # children first and restores the archive source last.
        archive_final_path = source_path
        if post_unarchive_action in ("move", "archive", "relocate"):
            raw_arch_sub = str(post_unarchive_target or "Archive").strip() or "Archive"
            arch_dir = os.path.normpath(os.path.join(base_dir, raw_arch_sub))
            if not is_subpath_or_equal(arch_dir, base_dir):
                arch_dir = os.path.normpath(os.path.join(base_dir, "Archive"))
            os.makedirs(arch_dir, exist_ok=True)
            arch_dst = get_safe_path(
                arch_dir, os.path.basename(source_path), source_path
            )
            if not _is_same_path(source_path, arch_dst):
                archive_final_path, _ = _execute_ledger_move(
                    moves, session_id, base_dir, source_path, arch_dst, pre_hash
                )
                rel_arch_dst = safe_relpath(archive_final_path, base_dir).replace(
                    "\\", "/"
                )
                with db_lock:
                    db_updates_batch.append(
                        {
                            "type": "document_path",
                            "args": (base_dir, source_rel_path, rel_arch_dst),
                        }
                    )
            _update_path_map_entry(
                path_map, source_path, archive_final_path, lock=db_lock
            )
        elif post_unarchive_action in ("retain", "keep"):
            archive_final_path = source_path
        else:
            backup_zip_dst = os.path.join(
                overwrite_backup_dir(base_dir, session_id),
                "unarchive_src",
                source_rel_path,
            )
            archive_final_path, _ = _execute_ledger_move(
                moves, session_id, base_dir, source_path, backup_zip_dst, pre_hash
            )
            _update_path_map_entry(
                path_map, source_path, archive_final_path, lock=db_lock
            )
            if db and hasattr(db, "worker"):
                try:
                    from app.core.db import get_db_connection

                    db_path = getattr(db, "db_path", None)
                    if db_path:

                        def _delete_archive_doc() -> None:
                            conn = get_db_connection(db_path)
                            with conn:
                                conn.execute(
                                    "DELETE FROM documents WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                                    (base_dir, source_rel_path, source_rel_path),
                                )

                        db.worker.execute_write(_delete_archive_doc)
                except Exception:
                    pass

        # 2. Move extracted child files from session staging to target directory via MoveLedger
        from app.core.extractor import get_file_hash

        placed_children: list[str] = []
        for staged_child in staged_children:
            if not os.path.isfile(staged_child):
                continue
            rel_in_zip = safe_relpath(staged_child, extract_stage_dir)
            child_sub_dir = os.path.dirname(rel_in_zip)
            child_dest_dir = (
                os.path.normpath(os.path.join(dest_dir, child_sub_dir))
                if child_sub_dir
                else dest_dir
            )
            if not is_subpath_or_equal(child_dest_dir, base_dir):
                continue
            os.makedirs(child_dest_dir, exist_ok=True)
            child_dst_candidate = get_safe_path(
                child_dest_dir, os.path.basename(rel_in_zip)
            )
            child_hash = ""
            try:
                child_hash = get_file_hash(staged_child)
            except OSError:
                child_hash = ""
            actual_child_dst, _ = _execute_ledger_move(
                moves,
                session_id,
                base_dir,
                staged_child,
                child_dst_candidate,
                child_hash,
            )
            placed_children.append(actual_child_dst)

        if unarchived_children_out is not None and placed_children:
            with db_lock:
                unarchived_children_out.extend(placed_children)

        dest_sub_display = safe_relpath(dest_dir, base_dir).replace("\\", "/")
        if dest_sub_display == ".":
            dest_sub_display = ""
        _record_policy_quarantine_audit(
            db,
            db_lock,
            base_dir=base_dir,
            original_filepath=source_rel_path,
            staged_filepath=os.path.abspath(archive_final_path),
            file_hash=pre_hash or file_hash,
            policy_action="unarchive",
            transitions=[
                (
                    "IN_INSPECTION",
                    {"details": "SortPipeline archive inspection completed"},
                ),
                (
                    "UNARCHIVED",
                    {
                        "policy_action": "unarchive",
                        "details": (
                            f"Archive extracted ({len(placed_children)} files) to "
                            f"'{dest_sub_display or '.'}'; source archive action: {post_unarchive_action}"
                        ),
                        "unpacked_files": placed_children,
                        "post_action": post_unarchive_action,
                    },
                ),
            ],
        )
        if audit_records is not None:
            with db_lock:
                audit_records.append(
                    {
                        "source_path": source_path,
                        "destination_path": archive_final_path,
                        "pre_hash": pre_hash,
                        "post_hash": pre_hash,
                    }
                )
        return True

    if policy_action == "redact":
        from app.core.extractor import get_file_hash
        from app.core.quarantine_interceptor import _redact_document_to_path
        from app.core.resilient_file_ops import resilient_remove

        routed_by_val = str(_get_val(content, "routed_by") or "").lower()
        match_expr = _get_val(content, "match") or _get_val(content, "keyword")
        extra_regex = (
            str(match_expr) if routed_by_val == "regex" and match_expr else None
        )

        staged_redacted_path = os.path.join(
            overwrite_backup_dir(base_dir, session_id),
            "redact_out",
            f"{uuid.uuid4().hex[:8]}_{filename}",
        )
        was_redacted, scrubbed_text = _redact_document_to_path(
            source_path,
            staged_redacted_path,
            extra_regex=extra_regex,
        )
        if not was_redacted:
            if os.path.exists(staged_redacted_path):
                with contextlib.suppress(OSError):
                    resilient_remove(staged_redacted_path)
            q_dir = os.path.normpath(os.path.join(base_dir, quarantine_dir_name))
            os.makedirs(q_dir, exist_ok=True)
            q_dst = get_safe_path(q_dir, filename, source_path)
            actual_q_dst = q_dst
            post_q_hash = pre_hash
            if not _is_same_path(source_path, q_dst):
                actual_q_dst, post_q_hash = _execute_ledger_move(
                    moves, session_id, base_dir, source_path, q_dst, pre_hash
                )
            _update_path_map_entry(path_map, source_path, actual_q_dst, lock=db_lock)
            rel_q_dst = safe_relpath(actual_q_dst, base_dir).replace("\\", "/")
            with db_lock:
                db_updates_batch.append(
                    {
                        "type": "document_path",
                        "args": (base_dir, source_rel_path, rel_q_dst),
                    }
                )
            _record_policy_quarantine_audit(
                db,
                db_lock,
                base_dir=base_dir,
                original_filepath=source_rel_path,
                staged_filepath=os.path.abspath(actual_q_dst),
                file_hash=pre_hash or file_hash,
                policy_action="redact",
                transitions=[
                    (
                        "IN_INSPECTION",
                        {"details": "SortPipeline redaction format inspection"},
                    ),
                    (
                        "QUARANTINED",
                        {
                            "policy_action": "redact",
                            "details": (
                                "Binary or PDF container cannot be safely text-redacted in place; "
                                "held intact in quarantine for manual review"
                            ),
                        },
                    ),
                ],
            )
            if audit_records is not None:
                with db_lock:
                    audit_records.append(
                        {
                            "source_path": source_path,
                            "destination_path": actual_q_dst,
                            "pre_hash": pre_hash,
                            "post_hash": post_q_hash,
                        }
                    )
            return True

        redacted_hash = ""
        try:
            redacted_hash = get_file_hash(staged_redacted_path)
        except OSError:
            redacted_hash = ""

        # Move 1: displace unredacted original into session backup so undo restores original bytes
        backup_orig_dst = os.path.join(
            overwrite_backup_dir(base_dir, session_id),
            "redact_orig",
            source_rel_path,
        )
        _execute_ledger_move(
            moves, session_id, base_dir, source_path, backup_orig_dst, pre_hash
        )

        # Move 2: place scrubbed output into target destination
        allow_ow = bool(collision and conflict_policy == "overwrite")
        redacted_dest_candidate = (
            target_path
            if allow_ow
            else os.path.normpath(get_safe_path(dest_dir, filename))
        )
        redacted_dest_candidate = unicodedata.normalize("NFC", redacted_dest_candidate)
        actual_redacted_dst, post_redacted_hash = _execute_ledger_move(
            moves,
            session_id,
            base_dir,
            staged_redacted_path,
            redacted_dest_candidate,
            redacted_hash,
            allow_overwrite=allow_ow,
        )
        _update_path_map_entry(path_map, source_path, actual_redacted_dst, lock=db_lock)
        rel_redacted_dst = safe_relpath(actual_redacted_dst, base_dir).replace(
            "\\", "/"
        )

        if db:
            with db_lock:
                try:
                    if hasattr(db, "_purge_document_vector"):
                        db._purge_document_vector(base_dir, source_rel_path)
                        if rel_redacted_dst != source_rel_path:
                            db._purge_document_vector(base_dir, rel_redacted_dst)
                    if (
                        rel_redacted_dst != source_rel_path
                        and hasattr(db, "worker")
                        and getattr(db, "db_path", None)
                    ):
                        from app.core.db import get_db_connection

                        db_p = db.db_path

                        def _delete_orig_doc() -> None:
                            conn = get_db_connection(db_p)
                            with conn:
                                conn.execute(
                                    "DELETE FROM documents WHERE base_dir = ? AND (filepath = ? OR REPLACE(filepath, '\\', '/') = ?)",
                                    (base_dir, source_rel_path, source_rel_path),
                                )

                        db.worker.execute_write(_delete_orig_doc)
                    if hasattr(db, "upsert_document"):
                        db.upsert_document(
                            base_dir,
                            rel_redacted_dst,
                            post_redacted_hash or redacted_hash,
                            scrubbed_text,
                        )
                except Exception as vec_err:
                    logging.warning(
                        f"Failed to update scrubbed document/vector state for {rel_redacted_dst}: {vec_err}"
                    )

        _record_policy_quarantine_audit(
            db,
            db_lock,
            base_dir=base_dir,
            original_filepath=source_rel_path,
            staged_filepath=os.path.abspath(actual_redacted_dst),
            file_hash=post_redacted_hash or redacted_hash,
            policy_action="redact",
            transitions=[
                (
                    "IN_INSPECTION",
                    {"details": "SortPipeline redaction inspection started"},
                ),
                (
                    "REDACTED",
                    {
                        "policy_action": "redact",
                        "details": "PII and secret scrubbing completed",
                    },
                ),
                (
                    "RELEASED",
                    {
                        "policy_action": "redact",
                        "details": f"Sanitized document released to {rel_redacted_dst}",
                    },
                ),
            ],
        )
        if audit_records is not None:
            with db_lock:
                audit_records.append(
                    {
                        "source_path": source_path,
                        "destination_path": actual_redacted_dst,
                        "pre_hash": redacted_hash,
                        "post_hash": post_redacted_hash or redacted_hash,
                    }
                )
        return True

    return False


def _execute_prepared_move_group(
    base_dir: str,
    group: list[dict[str, Any]],
    path_map: dict,
    session_id: str,
    moves_ledger: Any,
    db_lock: threading.Lock,
    db: Any = None,
) -> None:
    """Execute physical file moves and post-move SHA-256 verification off-thread.

    Commits ``record_displacement`` synchronously before moving bytes when an
    overwrite displaces an existing target file; buffers ``done``/``verified``/
    ``failed`` outcomes and DB updates for the chunk completion commit.
    """
    from app.core.resilient_file_ops import resilient_move

    for p in group:
        source_path = p["source_path"]
        dest_path = p["dest_path"]
        move_seq = p["move_seq"]
        conflict_policy = p["conflict_policy"]
        pre_hash = p["pre_hash"]
        file_hash = p["file_hash"]
        link_info = p["link_info"]

        displaced_path = None
        overwritten_path = None
        try:
            if (
                conflict_policy == "overwrite"
                and os.path.lexists(dest_path)
                and not _is_same_path(source_path, dest_path)
            ):
                overwritten_path = dest_path
                displaced_path = _displace_for_overwrite(
                    dest_path,
                    base_dir,
                    session_id,
                    journal=lambda path, seq=move_seq: _record_move_state(
                        moves_ledger,
                        session_id,
                        seq,
                        MoveOpState.DISPLACEMENT,
                        displaced=path,
                    ),
                )

            try:
                actual_dest = resilient_move(source_path, dest_path)
                if actual_dest != dest_path:
                    dest_path = actual_dest
                    _update_path_map_entry(
                        path_map, source_path, dest_path, lock=db_lock
                    )
                    if p.get("link_entry") is not None:
                        with db_lock:
                            p["link_entry"]["dest_path"] = dest_path
                rel_dest = safe_relpath(dest_path, base_dir).replace("\\", "/")
            except Exception as err:
                logging.error(
                    f"Failed to move file {source_path} to {dest_path}: {err}"
                )
                if displaced_path and overwritten_path:
                    _restore_displaced(displaced_path, overwritten_path)
                raise
        except Exception as err:
            p["outcome"] = {
                "state": MoveOpState.FAILED.value,
                "error": str(err),
                "required": False,
            }
            raise

        item_type = link_info["type"] if link_info else "file"
        step_hash = None
        if item_type == "file" and os.path.exists(dest_path):
            try:
                from app.core.extractor import get_file_hash

                dest_stat = None
                try:
                    dest_stat = os.stat(dest_path)
                except Exception:
                    pass

                if (
                    dest_stat
                    and p["src_ino"]
                    and dest_stat.st_ino
                    and p["src_dev"] is not None
                    and dest_stat.st_dev == p["src_dev"]
                    and dest_stat.st_ino == p["src_ino"]
                    and p["src_size"] is not None
                    and dest_stat.st_size == p["src_size"]
                    and pre_hash
                ):
                    step_hash = pre_hash
                else:
                    step_hash = get_file_hash(dest_path)
            except Exception:
                pass

        if step_hash and step_hash == pre_hash:
            final_state = MoveOpState.VERIFIED.value
        else:
            final_state = MoveOpState.DONE.value
            if move_seq is not None and pre_hash:
                logging.warning(
                    f"Moved {source_path} to {dest_path}, but its hash no longer matches the source."
                )

        post_hash = step_hash
        if post_hash is None and os.path.exists(dest_path):
            try:
                from app.core.extractor import get_file_hash

                post_hash = get_file_hash(dest_path)
            except Exception:
                post_hash = None

        policy_action = p.get("policy_action") or ""
        if (
            policy_action in ("quarantine", "archive", "retain")
            and not p["is_sensitivity_hold"]
        ):
            status_for_action = (
                "QUARANTINED"
                if policy_action == "quarantine"
                else ("ARCHIVED" if policy_action == "archive" else "RELEASED")
            )
            _record_policy_quarantine_audit(
                db,
                db_lock,
                base_dir=base_dir,
                original_filepath=p["source_rel_path"],
                staged_filepath=os.path.abspath(dest_path),
                file_hash=post_hash or pre_hash or file_hash,
                policy_action=policy_action,
                transitions=[
                    (
                        "IN_INSPECTION",
                        {"details": f"SortPipeline {policy_action} policy inspection"},
                    ),
                    (
                        status_for_action,
                        {
                            "policy_action": policy_action,
                            "details": f"Policy action {policy_action} relocated document to {rel_dest}",
                        },
                    ),
                ],
            )

        item_db_updates: list[dict[str, Any]] = []
        if file_hash and not p["is_quarantine_dest"]:
            item_db_updates.append(
                {
                    "type": "verified_target",
                    "args": (
                        base_dir,
                        file_hash,
                        p["current_dest"].replace("\\", "/"),
                    ),
                }
            )
        item_db_updates.append(
            {
                "type": "document_path",
                "args": (base_dir, p["source_rel_path"], rel_dest),
            }
        )

        p["outcome"] = {
            "state": final_state,
            "dst": dest_path,
            "displaced": displaced_path,
            "required": True,
            "db_updates": item_db_updates,
            "audit_record": {
                "source_path": source_path,
                "destination_path": dest_path,
                "pre_hash": pre_hash,
                "post_hash": post_hash,
            },
        }


def _prepare_disposal_item(
    base_dir: str,
    item: dict,
    path_map: dict,
    session_id: str,
    moves: Any,
    *,
    moves_by_src: dict[str, list[Any]] | None = None,
    reserved_dests: set[str] | None = None,
) -> dict[str, Any] | None:
    """Validate ADR 0005 primary survival and prepare staging target/hash off-lock."""
    from app.core.extractor import get_file_hash
    from app.core.moves import VERIFIED
    from app.core.resilient_file_ops import (
        duplicate_staging_relpath,
        same_file_content,
    )
    from app.core.scanner import parse_item_identifier

    source_path = item["source_path"]
    if not os.path.lexists(source_path):
        return None

    content = item.get("content")
    dup_action = _get_val(content, "duplicate_action")
    pol_action = _get_val(content, "policy_action")
    is_dup = bool(_get_val(content, "is_duplicate"))
    dup_of = _get_val(content, "duplicate_of")

    # ADR 0005: Never displace or trash a duplicate until the surviving primary is verified
    if is_dup or dup_of:
        if not dup_of:
            logging.warning(
                "ADR 0005 guard: refusing to stage duplicate %s without surviving primary reference.",
                source_path,
            )
            return None

        dup_of_str = str(dup_of)
        if os.path.isabs(dup_of_str) and "::" not in dup_of_str:
            primary_orig_abs = os.path.normpath(dup_of_str)
        else:
            p_root, p_rel = parse_item_identifier(dup_of_str, base_dir)
            primary_orig_abs = os.path.normpath(
                os.path.abspath(os.path.join(p_root, p_rel))
            )
        norm_primary_orig = os.path.normcase(os.path.abspath(primary_orig_abs))

        verified_primary_path: str | None = None
        primary_moved_in_ledger = False
        if moves is not None and session_id:
            if moves_by_src is not None:
                matching_rows = moves_by_src.get(norm_primary_orig, [])
            else:
                session_moves = moves.list_moves(session_id)
                matching_rows = [
                    m
                    for m in session_moves
                    if os.path.normcase(os.path.abspath(m.src)) == norm_primary_orig
                ]
            if matching_rows:
                primary_moved_in_ledger = True
                latest = matching_rows[-1]
                if latest.state == VERIFIED and os.path.isfile(latest.dst):
                    verified_primary_path = latest.dst

        if primary_moved_in_ledger:
            if not verified_primary_path:
                logging.warning(
                    "ADR 0005 guard: refusing to stage duplicate %s because surviving primary %s is not marked verified in MoveLedger.",
                    source_path,
                    primary_orig_abs,
                )
                return None
        else:
            cand_dst = path_map.get(norm_primary_orig) or primary_orig_abs
            if moves is not None and not _is_same_path(cand_dst, primary_orig_abs):
                if not os.path.isfile(cand_dst):
                    logging.warning(
                        "ADR 0005 guard: refusing to stage duplicate %s because primary destination %s is missing.",
                        source_path,
                        cand_dst,
                    )
                    return None
                verified_primary_path = cand_dst
            elif os.path.isfile(cand_dst):
                verified_primary_path = cand_dst

        if not verified_primary_path or not os.path.isfile(verified_primary_path):
            logging.warning(
                "ADR 0005 guard: refusing to stage duplicate %s because primary file could not be located on disk.",
                source_path,
            )
            return None

        try:
            if os.path.samefile(verified_primary_path, source_path):
                return None
        except OSError:
            if _is_same_path(verified_primary_path, source_path):
                return None

        if not same_file_content(verified_primary_path, source_path):
            logging.warning(
                "ADR 0005 guard: refusing to stage %s because its bytes do not match primary %s.",
                source_path,
                verified_primary_path,
            )
            return None

    src_root = _get_val(content, "source_root") or base_dir
    stage_rel = duplicate_staging_relpath(source_path, base_dir, source_root=src_root)
    eff_session = session_id or f"nosession_{uuid.uuid4().hex}"
    is_trash_target = dup_action == "trash" or pol_action == "trash"
    stage_base = duplicate_staging_dir(base_dir, eff_session)
    if is_trash_target:
        stage_base = os.path.join(stage_base, "_trash")

    planned_stage_target = os.path.normpath(os.path.join(stage_base, stage_rel))
    stage_dir = os.path.dirname(planned_stage_target)
    os.makedirs(stage_dir, exist_ok=True)
    if reserved_dests is not None:
        dest_path = os.path.normpath(
            _get_safe_path_with_reservations(
                stage_dir,
                os.path.basename(planned_stage_target),
                source_path,
                reserved_dests,
            )
        )
        reserved_dests.add(os.path.normcase(os.path.abspath(dest_path)))
    else:
        dest_path = os.path.normpath(
            get_safe_path(
                stage_dir, os.path.basename(planned_stage_target), source_path
            )
        )

    pre_hash = ""
    intent_size = None
    try:
        if os.path.isfile(source_path) and not os.path.islink(source_path):
            intent_size = os.lstat(source_path).st_size
    except OSError:
        pass
    try:
        pre_hash = get_file_hash(source_path)
    except (OSError, ValueError, TypeError):
        pre_hash = ""

    return {
        "source_path": source_path,
        "dest_path": dest_path,
        "pre_hash": pre_hash,
        "intent_size": intent_size,
        "is_trash_target": is_trash_target,
        "source_rel_path": safe_relpath(source_path, base_dir).replace("\\", "/"),
    }


def _stage_disposal_chunk(
    base_dir: str,
    items: list[dict],
    db: Any,
    path_map: dict,
    session_id: str,
    history_manager: Any,
    db_lock: threading.Lock,
    db_updates_batch: list,
    audit_records: list | None = None,
    *,
    moves_by_src: dict[str, list[Any]] | None = None,
    flush_db: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    """Stage a chunk of duplicate or policy-trashed items with batched MoveLedger commits."""
    from app.core.extractor import get_file_hash
    from app.core.moves import STATUS_ABORTED, format_move_failure
    from app.core.resilient_file_ops import resilient_move

    moves_ledger = getattr(history_manager, "moves", None) if history_manager else None
    reserved_stage_dests: set[str] = set()
    prepared_disposals: list[dict[str, Any]] = []
    for disp_item in items:
        prep_d = _prepare_disposal_item(
            base_dir=base_dir,
            item=disp_item,
            path_map=path_map,
            session_id=session_id,
            moves=moves_ledger,
            moves_by_src=moves_by_src,
            reserved_dests=reserved_stage_dests,
        )
        if prep_d is not None:
            prepared_disposals.append(prep_d)
    if not prepared_disposals:
        return [], False

    disp_intents = [
        (
            pd["source_path"],
            pd["dest_path"],
            pd["pre_hash"],
            pd["intent_size"],
        )
        for pd in prepared_disposals
    ]
    disp_seqs = _record_move_intents_batch(moves_ledger, session_id, disp_intents)
    disp_completions: list[dict[str, Any]] = []
    disp_db_updates: list[dict[str, Any]] = []
    staged_entries: list[dict[str, Any]] = []
    disp_exc: BaseException | None = None

    for idx_d, (pd, d_seq) in enumerate(zip(prepared_disposals, disp_seqs)):
        try:
            final_dest = resilient_move(pd["source_path"], pd["dest_path"])
        except Exception as err:
            if d_seq is not None:
                disp_completions.append(
                    {
                        "seq": d_seq,
                        "state": MoveOpState.FAILED.value,
                        "error": str(err),
                        "required": False,
                    }
                )
            for rem_seq in disp_seqs[idx_d + 1 :]:
                if rem_seq is not None:
                    disp_completions.append(
                        {
                            "seq": rem_seq,
                            "state": MoveOpState.FAILED.value,
                            "error": format_move_failure(STATUS_ABORTED),
                            "required": False,
                        }
                    )
            disp_exc = err
            break

        post_hash = get_file_hash(final_dest) if os.path.isfile(final_dest) else ""
        d_state = (
            MoveOpState.VERIFIED.value
            if (pd["pre_hash"] and post_hash == pd["pre_hash"])
            else MoveOpState.DONE.value
        )
        if d_seq is not None:
            disp_completions.append(
                {
                    "seq": d_seq,
                    "state": d_state,
                    "dst": final_dest,
                    "displaced": None,
                    "required": True,
                }
            )
        _update_path_map_entry(path_map, pd["source_path"], final_dest, lock=db_lock)
        rel_dest = safe_relpath(final_dest, base_dir).replace("\\", "/")
        disp_db_updates.append(
            {
                "type": "document_path",
                "args": (base_dir, pd["source_rel_path"], rel_dest),
            }
        )
        if audit_records is not None:
            with db_lock:
                audit_records.append(
                    {
                        "source_path": pd["source_path"],
                        "destination_path": final_dest,
                        "pre_hash": pd["pre_hash"],
                        "post_hash": post_hash or pd["pre_hash"],
                    }
                )
        staged_entries.append(
            {
                "source_path": pd["source_path"],
                "staged_path": final_dest,
                "action": "trash" if pd["is_trash_target"] else "stage",
                "sha256": pd["pre_hash"],
            }
        )

    flushed = False
    if flush_db:
        with db_lock:
            if db_updates_batch:
                disp_db_updates = list(db_updates_batch) + disp_db_updates
                db_updates_batch.clear()
        flushed = _flush_chunk_completions_and_db(
            db,
            moves_ledger,
            session_id,
            disp_completions,
            disp_db_updates,
        )
    else:
        _flush_chunk_completions_and_db(
            None, moves_ledger, session_id, disp_completions, []
        )
        if disp_db_updates:
            with db_lock:
                db_updates_batch.extend(disp_db_updates)

    if disp_exc is not None:
        raise disp_exc
    return staged_entries, flushed


def _stage_disposal_item(
    base_dir: str,
    item: dict,
    db,
    path_map: dict,
    session_id: str,
    history_manager,
    db_lock: threading.Lock,
    db_updates_batch: list,
    audit_records: list | None = None,
) -> dict[str, Any] | None:
    """Stage a redundant duplicate or policy-trashed file into ``.sortify_duplicates/<session_id>/``.

    Enforces ADR 0005 (surviving primary must be ``verified`` in ``MoveLedger`` or
    verified byte-identical on disk if stationary) and ADR 0006 (every staging
    relocation is recorded in ``MoveLedger`` so ``SortPipeline.undo`` replays it).
    """
    entries, _ = _stage_disposal_chunk(
        base_dir=base_dir,
        items=[item],
        db=db,
        path_map=path_map,
        session_id=session_id,
        history_manager=history_manager,
        db_lock=db_lock,
        db_updates_batch=db_updates_batch,
        audit_records=audit_records,
        flush_db=False,
    )
    return entries[0] if entries else None


def _submit_bounded_to_pool(
    pool: Any,
    max_concurrency: int,
    fn: Any,
    task_args: list[tuple[Any, ...]],
) -> tuple[list[Any], BaseException | None]:
    """Submit tasks to ``SharedWorkerPool`` bounded by ``max_concurrency`` and stop on first error."""
    limit = max(1, int(max_concurrency))
    if limit == 1:
        results: list[Any] = []
        for args in task_args:
            fut = pool.submit(fn, *args)
            exc = fut.exception()
            if exc is not None:
                return results, exc
            results.append(fut.result())
        return results, None

    it = iter(enumerate(task_args))
    in_flight: dict[concurrent.futures.Future[Any], int] = {}
    ordered_results: dict[int, Any] = {}
    first_exc: BaseException | None = None

    for idx, args in it:
        in_flight[pool.submit(fn, *args)] = idx
        if len(in_flight) >= limit:
            break

    while in_flight:
        done, _ = concurrent.futures.wait(
            in_flight.keys(),
            return_when=concurrent.futures.FIRST_COMPLETED,
        )
        for fut in done:
            idx = in_flight.pop(fut)
            if not fut.cancelled():
                exc = fut.exception()
                if exc is not None and first_exc is None:
                    first_exc = exc
                elif exc is None:
                    ordered_results[idx] = fut.result()
        if first_exc is not None:
            for pending_fut in list(in_flight.keys()):
                pending_fut.cancel()
            if in_flight:
                concurrent.futures.wait(in_flight.keys())
            in_flight.clear()
            break
        while len(in_flight) < limit:
            try:
                next_idx, next_args = next(it)
            except StopIteration:
                break
            in_flight[pool.submit(fn, *next_args)] = next_idx

    return [ordered_results[i] for i in sorted(ordered_results)], first_exc


class AsyncMoveEngine(LifecycleTelemetryMixin):
    """Asynchronous Chunked Thread Pool Execution Engine for file relocation."""

    def __init__(self, max_workers: int | None = None, chunk_size: int | None = None):
        self.max_workers = max_workers
        self.chunk_size = chunk_size
        self._state_lock = threading.RLock()
        self._state = MoveEngineState.IDLE

    @property
    def state(self) -> MoveEngineState:
        """Return the current lifecycle state of the move engine."""
        with self._state_lock:
            return self._state

    def transition_to(
        self, target: MoveEngineState | str, context: dict | None = None
    ) -> MoveEngineState:
        """Safely transition engine state protected by re-entrant thread lock."""
        with self._state_lock:
            try:
                target_state = (
                    target
                    if isinstance(target, MoveEngineState)
                    else MoveEngineState(target)
                )
            except ValueError:
                raise InvalidMoveStateError(
                    f"Unknown or invalid target state: {target!r}"
                ) from None

            valid_targets = _VALID_MOVE_ENGINE_TRANSITIONS.get(self._state, set())
            if target_state not in valid_targets:
                raise InvalidMoveStateError(
                    f"Illegal move engine transition from {self._state.value} to {target_state.value}"
                )
            old_state = self._state
            self._state = target_state

        self._notify_lifecycle_listeners(
            old_state,
            target_state,
            context=context,
            event_type="MOVE_ENGINE_STATE_CHANGED",
        )
        return self._state

    def reset(self) -> MoveEngineState:
        """Reset the engine back to IDLE state."""
        with self._state_lock:
            return self.transition_to(MoveEngineState.IDLE)

    def execute(
        self,
        base_dir: str,
        plan: dict,
        db,
        history_manager,
        runtime_settings=None,
        resume: bool = False,
        batch_size: int = 50,
        cancel_check=None,
        cancel_event=None,
        cancellation_token=None,
        priority: str | None = None,
        progress_callback=None,
        pipeline: Any = None,
        **kwargs: Any,
    ) -> MoveResult:
        """Partition relocation plan into bounded worker chunks and execute file moves off-thread.

        Args:
            base_dir: Root directory path where file movements occur.
            plan: Mapping of category names to target file move specifications.
            db: Database connection instance for tracking transaction records.
            history_manager: History manager instance for session snapshots/rollbacks.
            runtime_settings: Optional runtime configuration settings object.
            resume: Whether this execution is resuming a previously incomplete session.
            batch_size: Number of database entries per batch transaction write.
            cancel_check: Optional callable that returns True if cancellation was requested.
            cancel_event: Optional threading or asyncio event signaling cancellation.
            cancellation_token: Optional cancellation token object for cooperative cancellation.
            priority: Execution priority tier string.
            progress_callback: Optional callback receiving progress ratio updates during file movement.
            pipeline: Optional pipeline instance for lifecycle state coordination.
            **kwargs: Additional keyword arguments for future extensibility.

        Returns
        -------
            MoveResult: Execution results containing overall session status and processed item counts.

        Raises
        ------
            ValueError: If base_dir or plan verification fails integrity checks.
            RuntimeError: If a history manager is given but the undo snapshot cannot be created.
        """
        base_dir = os.path.normpath(base_dir)
        self.transition_to(MoveEngineState.PREPARING)

        session_id = kwargs.get("parent_session_id")
        is_child_session = bool(session_id)
        db_lock = threading.Lock()
        db_updates_batch: list[Any] = []
        audit_records: list[dict] = []
        pending_links: list = []
        unarchived_children_out: list[str] = []
        has_flushed_db = False
        summary: MoveResult | None = None

        try:
            effective_progress_cb = progress_callback or getattr(
                runtime_settings, "progress_callback", None
            )

            active_conflict_policy = str(
                getattr(runtime_settings, "CONFLICT_POLICY", "rename") or "rename"
            ).lower()
            integrity_result = VerificationEngine.verify_plan_integrity(
                base_dir, plan, conflict_policy=active_conflict_policy
            )
            if (
                integrity_result.get("invalid_renames")
                or integrity_result.get("unconfirmed_renames")
                or integrity_result.get("circular_renames")
                or integrity_result.get("escaping_paths")
            ):
                warn_text = (
                    "; ".join(integrity_result.get("warnings", []))
                    or "Plan validation failed."
                )
                raise ValueError(f"Plan validation or confirmation failed: {warn_text}")

            # No transaction spans the sort. Documents and move records share one
            # store (ADR 0008), so a sort-long write lock would stall every move
            # committed from a worker thread. Each chunk's updates commit on their own,
            # and unwind_session replays moves backwards if the sort fails.
            moves_list = VerificationEngine.get_moves(base_dir, plan)
            path_map = {
                os.path.normcase(os.path.abspath(src)): os.path.abspath(dst)
                for rel_src, src, dst in moves_list
            }

            cancel_token = (
                cancel_check
                or cancel_event
                or cancellation_token
                or getattr(runtime_settings, "cancel_check", None)
                or getattr(runtime_settings, "cancel_event", None)
            )

            effective_chunk_size = self.chunk_size or batch_size

            effective_max_workers = (
                self.max_workers or getattr(runtime_settings, "MAX_WORKERS", None) or 1
            )

            effective_priority = priority or getattr(runtime_settings, "priority", None)

            from app.core.scanner import ScanFilter

            scan_filter = ScanFilter.from_settings(
                settings=runtime_settings, base_dir=base_dir, max_depth=None
            )
            raw_move_items = _collect_move_items(
                base_dir, plan, priority=effective_priority
            )
            all_filtered_items = [
                item
                for item in raw_move_items
                if not (
                    scan_filter.is_protected(item["source_path"])
                    or scan_filter.is_app_bundle(item["source_path"])
                    or scan_filter.has_ignored_extension(item["source_path"])
                )
            ]
            move_items = []
            disposal_items = []
            for item in all_filtered_items:
                c_node = item.get("content")
                d_act = _get_val(c_node, "duplicate_action")
                p_act = _get_val(c_node, "policy_action")
                if d_act == "skip":
                    continue
                if d_act in ("stage", "trash") or p_act == "trash":
                    disposal_items.append(item)
                else:
                    move_items.append(item)

            auto_ratings = {
                str(r).upper()
                for r in (
                    getattr(runtime_settings, "AUTO_QUARANTINE_RATINGS", []) or []
                )
                if r
            }
            quarantine_dir_name = (
                getattr(
                    runtime_settings,
                    "QUARANTINE_DIR_NAME",
                    "_Compliance_Quarantine",
                )
                or "_Compliance_Quarantine"
            )

            def _item_may_move(item: dict) -> bool:
                content = item.get("content")
                if _get_val(content, "policy_action"):
                    return True
                src_abs = os.path.normcase(os.path.abspath(item["source_path"]))
                dst_abs = os.path.normcase(
                    os.path.abspath(
                        os.path.join(base_dir, item["current_dest"], item["filename"])
                    )
                )
                if src_abs != dst_abs:
                    return True
                if auto_ratings and isinstance(content, dict):
                    sens = str(content.get("sensitivity_rating") or "LOW").upper()
                    if (
                        sens in auto_ratings
                        and item["current_dest"] != quarantine_dir_name
                    ):
                        return True
                return False

            has_candidate_moves = any(
                _item_may_move(item) for item in move_items
            ) or bool(disposal_items)

            registered_session = False
            if (
                not is_child_session
                and not resume
                and history_manager
                and has_candidate_moves
            ):
                # Without a registered session nothing could undo or roll back the
                # moves, so refuse to start rather than move files untracked.
                try:
                    session_id = history_manager.start_session(base_dir)
                except Exception as sess_err:
                    raise RuntimeError(
                        f"Refusing to move files: could not register sort session for {base_dir}: {sess_err}"
                    ) from sess_err
                if not session_id:
                    raise RuntimeError(
                        f"Refusing to move files: could not register sort session for {base_dir}."
                    )
                registered_session = True
                logging.info(f"Started sort session {session_id} for {base_dir}")
            elif not is_child_session and resume and history_manager:
                logging.info(f"Resuming sort session for {base_dir}")
                try:
                    sessions = history_manager.get_sessions()
                    for s in sessions:
                        if (
                            _is_same_path(s["base_dir"], base_dir)
                            and s["status"] == "active"
                        ):
                            session_id = s["session_id"]
                            registered_session = True
                            break
                except Exception:
                    pass
                if (
                    not session_id
                    and has_candidate_moves
                    and hasattr(history_manager, "start_session")
                ):
                    try:
                        session_id = history_manager.start_session(base_dir)
                        if session_id:
                            registered_session = True
                    except Exception:
                        pass

            if not session_id and (resume or has_candidate_moves):
                session_id = str(uuid.uuid4())
            total_items = len(move_items) + len(disposal_items)
            chunks = [
                move_items[i : i + effective_chunk_size]
                for i in range(0, len(move_items), effective_chunk_size)
            ]
            total_chunks = len(chunks)

            if total_chunks == 0 and not disposal_items:
                emit_progress(
                    effective_progress_cb,
                    1.0,
                    stage="Relocation complete",
                    unit_count=0,
                    unit_type="files",
                )

            self.emit_telemetry(
                "move_start",
                {
                    "session_id": session_id,
                    "total_chunks": total_chunks,
                    "total_items": total_items,
                },
            )

            summary = MoveResult(
                session_id=session_id,
                deleted_folders=0,
                protected_folders=0,
                cancelled=False,
                state=self.state,
                lifecycle_state=PipelineLifecycleState.COMPLETED,
                rollback_completed=None,
            )

            from app.core.shared_registry import SharedWorkerPool

            pool = SharedWorkerPool.get_instance(max_workers=effective_max_workers)
            target_locks: dict[str, threading.Lock] = {}
            for chunk_idx, chunk in enumerate(chunks):
                if _is_cancelled(cancel_token):
                    logging.info(
                        f"Cancellation requested before scheduling chunk {chunk_idx}. Halting worker scheduling."
                    )
                    self.transition_to(MoveEngineState.CANCELLING)
                    self.emit_telemetry(
                        "move_cancelled",
                        {
                            "session_id": session_id,
                            "chunk_index": chunk_idx,
                        },
                    )
                    summary.cancelled = True
                    summary["lifecycle_state"] = PipelineLifecycleState.CANCELLED
                    if pipeline and hasattr(pipeline, "transition_to"):
                        try:
                            if pipeline.state != PipelineLifecycleState.CANCELLED:
                                pipeline.transition_to(PipelineLifecycleState.CANCELLED)
                        except Exception:
                            pass
                    break

                self.transition_to(
                    MoveEngineState.MOVING_CHUNK,
                    context={"chunk_index": chunk_idx, "total_chunks": total_chunks},
                )
                self.emit_telemetry(
                    "move_progress",
                    {
                        "session_id": session_id,
                        "chunk_index": chunk_idx,
                        "total_chunks": total_chunks,
                        "chunk_size": len(chunk),
                    },
                )
                summary.state = self.state

                chunk_rel_paths = [
                    safe_relpath(item["source_path"], base_dir).replace("\\", "/")
                    for item in chunk
                    if "source_path" in item
                ]
                chunk_hash_map = None
                if db and hasattr(db, "get_document_hashes"):
                    try:
                        chunk_hash_map = db.get_document_hashes(
                            base_dir, chunk_rel_paths
                        )
                    except Exception as prefetch_err:
                        logging.warning(
                            f"Failed to batch prefetch document hashes: {prefetch_err}"
                        )
                        chunk_hash_map = None
                elif db and hasattr(db, "get_document"):
                    try:
                        chunk_hash_map = {
                            p: (db.get_document(base_dir, p) or {}).get("file_hash", "")
                            for p in chunk_rel_paths
                        }
                    except Exception:
                        chunk_hash_map = None
                elif db is None:
                    chunk_hash_map = dict.fromkeys(chunk_rel_paths, "")

                if _process_move_item is not _DEFAULT_PROCESS_MOVE_ITEM:
                    target_buckets: dict[str, list[dict]] = {}
                    for item in chunk:
                        cand_target = os.path.normcase(
                            os.path.abspath(
                                os.path.join(
                                    base_dir,
                                    item.get("current_dest", ""),
                                    item.get("filename", ""),
                                )
                            )
                        )
                        target_buckets.setdefault(cand_target, []).append(item)

                    _, group_exc = _submit_bounded_to_pool(
                        pool,
                        effective_max_workers,
                        _process_move_group,
                        [
                            (
                                base_dir,
                                bucket_items,
                                db,
                                path_map,
                                runtime_settings,
                                session_id,
                                history_manager,
                                db_lock,
                                db_updates_batch,
                                audit_records,
                                pending_links,
                                chunk_hash_map,
                                target_locks,
                                unarchived_children_out,
                            )
                            for bucket_items in target_buckets.values()
                        ],
                    )
                    if group_exc is not None:
                        raise group_exc
                    with db_lock:
                        if len(db_updates_batch) >= batch_size and hasattr(
                            db, "execute_batch_updates"
                        ):
                            db.execute_batch_updates(list(db_updates_batch))
                            db_updates_batch.clear()
                            has_flushed_db = True
                else:
                    prep_results, prep_exc = _submit_bounded_to_pool(
                        pool,
                        effective_max_workers,
                        _prepare_chunk_move_item,
                        [
                            (
                                base_dir,
                                item,
                                db,
                                runtime_settings,
                                db_lock,
                                chunk_hash_map,
                            )
                            for item in chunk
                        ],
                    )
                    if prep_exc is not None:
                        raise prep_exc
                    prepared_items = [
                        prep_res for prep_res in prep_results if prep_res is not None
                    ]

                    reserved_dests: set[str] = set()
                    active_prepared: list[dict[str, Any]] = []
                    special_policy_prepared: list[dict[str, Any]] = []
                    for p in prepared_items:
                        resolution = _resolve_prepared_destination(
                            base_dir=base_dir,
                            prepared=p,
                            db=db,
                            path_map=path_map,
                            db_lock=db_lock,
                            db_updates_batch=db_updates_batch,
                            pending_links=pending_links,
                            reserved_dests=reserved_dests,
                        )
                        if resolution == "ready":
                            active_prepared.append(p)
                        elif resolution == "special_policy":
                            special_policy_prepared.append(p)

                    for sp in special_policy_prepared:
                        sp_target_path = os.path.normpath(
                            os.path.join(sp["dest_dir"], sp["filename"])
                        )
                        with _get_target_lock(target_locks, db_lock, sp_target_path):
                            _execute_special_policy_move(
                                base_dir=base_dir,
                                prepared=sp,
                                target_path=sp_target_path,
                                db=db,
                                path_map=path_map,
                                session_id=str(session_id or ""),
                                history_manager=history_manager,
                                db_lock=db_lock,
                                db_updates_batch=db_updates_batch,
                                audit_records=audit_records,
                                unarchived_children_out=unarchived_children_out,
                            )

                    moves_ledger = (
                        getattr(history_manager, "moves", None)
                        if history_manager
                        else None
                    )
                    if active_prepared:
                        # Commit all pre-move intents in one batch before moving any bytes (ADR 0006).
                        intents_payload = [
                            (
                                p["source_path"],
                                p["dest_path"],
                                p["pre_hash"],
                                p["intent_size"],
                            )
                            for p in active_prepared
                        ]
                        assigned_seqs = _record_move_intents_batch(
                            moves_ledger, session_id, intents_payload
                        )
                        for p, seq in zip(active_prepared, assigned_seqs):
                            p["move_seq"] = seq

                        # Group items sharing a destination path (e.g. multiple overwrites) so
                        # distinct destinations move concurrently while identical targets serialize.
                        dest_groups: dict[str, list[dict[str, Any]]] = {}
                        for p in active_prepared:
                            norm_dst = os.path.normcase(os.path.abspath(p["dest_path"]))
                            dest_groups.setdefault(norm_dst, []).append(p)

                        # Never leave moves in flight: on the first failure, cancel
                        # what hasn't started and wait for the rest before any rollback.
                        _, first_exc = _submit_bounded_to_pool(
                            pool,
                            effective_max_workers,
                            _execute_prepared_move_group,
                            [
                                (
                                    base_dir,
                                    group,
                                    path_map,
                                    session_id,
                                    moves_ledger,
                                    db_lock,
                                    db,
                                )
                                for group in dest_groups.values()
                            ],
                        )

                        from app.core.moves import STATUS_ABORTED, format_move_failure

                        chunk_completions: list[dict[str, Any]] = []
                        for p in active_prepared:
                            outcome: dict[str, Any] = p.get("outcome") or {
                                "state": MoveOpState.FAILED.value,
                                "error": format_move_failure(STATUS_ABORTED),
                                "required": False,
                            }
                            if p["move_seq"] is not None:
                                chunk_completions.append(
                                    {
                                        "seq": p["move_seq"],
                                        "state": outcome["state"],
                                        "dst": outcome.get("dst"),
                                        "displaced": outcome.get("displaced"),
                                        "error": outcome.get("error"),
                                        "required": outcome.get("required", True),
                                    }
                                )
                            outcome_updates = outcome.get("db_updates")
                            if isinstance(outcome_updates, list) and outcome_updates:
                                with db_lock:
                                    db_updates_batch.extend(outcome_updates)
                            outcome_audit = outcome.get("audit_record")
                            if (
                                isinstance(outcome_audit, dict)
                                and audit_records is not None
                            ):
                                with db_lock:
                                    audit_records.append(outcome_audit)

                        with db_lock:
                            updates_to_flush = list(db_updates_batch)
                            db_updates_batch.clear()

                        try:
                            if _flush_chunk_completions_and_db(
                                db,
                                moves_ledger,
                                session_id,
                                chunk_completions,
                                updates_to_flush,
                            ):
                                has_flushed_db = True
                        except Exception:
                            if first_exc is None:
                                raise
                            logging.warning(
                                "Failed to flush post-error chunk completions before rollback",
                                exc_info=True,
                            )

                        if first_exc is not None:
                            raise first_exc
                    else:
                        with db_lock:
                            if db_updates_batch and hasattr(
                                db, "execute_batch_updates"
                            ):
                                db.execute_batch_updates(list(db_updates_batch))
                                db_updates_batch.clear()
                                has_flushed_db = True

                processed_count = min(
                    (chunk_idx + 1) * effective_chunk_size, total_items
                )
                progress_ratio = (
                    (chunk_idx + 1) / total_chunks if total_chunks > 0 else 1.0
                )
                emit_progress(
                    effective_progress_cb,
                    progress_ratio,
                    stage=f"Moved {processed_count} of {total_items} files",
                    unit_count=processed_count,
                    unit_type="files",
                )

                if _is_cancelled(cancel_token):
                    logging.info(
                        f"Cancellation requested after chunk {chunk_idx}. Stopping pipeline execution."
                    )
                    self.transition_to(MoveEngineState.CANCELLING)
                    summary.cancelled = True
                    break

            if summary.cancelled or self.state == MoveEngineState.CANCELLING:
                with db_lock:
                    if db_updates_batch and hasattr(db, "execute_batch_updates"):
                        db.execute_batch_updates(list(db_updates_batch))
                        db_updates_batch.clear()
                self.transition_to(MoveEngineState.CANCELLED)
                summary.state = self.state
            else:
                staged_duplicates: list[dict[str, Any]] = []
                if (
                    disposal_items
                    and isinstance(session_id, str)
                    and not _is_cancelled(cancel_token)
                ):
                    moves_ledger = (
                        getattr(history_manager, "moves", None)
                        if history_manager
                        else None
                    )
                    moves_by_src: dict[str, list[Any]] | None = None
                    if (
                        moves_ledger is not None
                        and session_id
                        and hasattr(moves_ledger, "list_moves")
                    ):
                        moves_by_src = {}
                        for m in moves_ledger.list_moves(session_id):
                            norm_m_src = os.path.normcase(os.path.abspath(m.src))
                            moves_by_src.setdefault(norm_m_src, []).append(m)

                    disposal_chunks = [
                        disposal_items[i : i + effective_chunk_size]
                        for i in range(0, len(disposal_items), effective_chunk_size)
                    ]
                    for disp_chunk in disposal_chunks:
                        if _is_cancelled(cancel_token):
                            summary.cancelled = True
                            break
                        chunk_staged, chunk_flushed = _stage_disposal_chunk(
                            base_dir=base_dir,
                            items=disp_chunk,
                            db=db,
                            path_map=path_map,
                            session_id=session_id,
                            history_manager=history_manager,
                            db_lock=db_lock,
                            db_updates_batch=db_updates_batch,
                            audit_records=audit_records,
                            moves_by_src=moves_by_src,
                            flush_db=True,
                        )
                        staged_duplicates.extend(chunk_staged)
                        if chunk_flushed:
                            has_flushed_db = True

                    if staged_duplicates:
                        emit_progress(
                            effective_progress_cb,
                            1.0,
                            stage=f"Staged {len(staged_duplicates)} verified duplicate(s)",
                            unit_count=total_items,
                            unit_type="files",
                        )
                summary["staged_duplicates"] = staged_duplicates

                if pending_links:
                    self.transition_to(MoveEngineState.RESOLVING_LINKS)
                    summary.state = self.state
                    _resolve_pending_links(
                        base_dir=base_dir,
                        pending_links=pending_links,
                        path_map=path_map,
                        db=db,
                        session_id=session_id,
                    )
                cleanup_enabled = (
                    getattr(runtime_settings, "CLEANUP_EMPTY_FOLDERS", True)
                    if runtime_settings
                    else True
                )

                dirs_to_process = []

                def _find_dir_nodes(node):
                    if not isinstance(node, dict) or node.get("__type__") in (
                        "file",
                        "directory",
                    ):
                        return
                    for k, v in node.items():
                        if isinstance(v, dict) and v.get("__type__") == "directory":
                            dirs_to_process.append(v)
                        elif isinstance(v, dict) and v.get("__type__") != "file":
                            _find_dir_nodes(v)

                _find_dir_nodes(plan)
                dirs_to_process.sort(
                    key=lambda x: len(x["source_path"].replace("\\", "/").split("/")),
                    reverse=True,
                )

                if cleanup_enabled:
                    self.transition_to(MoveEngineState.CLEANING_DIRS)
                    summary.state = self.state
                    protected_paths = list(scan_filter.protected_paths)

                    for node in dirs_to_process:
                        is_protected = node.get("protected")
                        source_path = node.get("source_path")
                        if not is_protected and source_path:
                            if scan_filter.is_protected(
                                source_path
                            ) or scan_filter.is_app_bundle(source_path):
                                is_protected = True

                        if is_protected:
                            summary["protected_folders"] += 1
                        elif node.get("status") in (
                            DirectoryCleanupStatus.TO_BE_DELETED,
                            "To Be Deleted",
                        ):
                            try:
                                src_path = node.get("source_path")
                                if (
                                    src_path
                                    and (
                                        is_junction_path(src_path)
                                        or os.path.islink(src_path)
                                    )
                                    or (
                                        src_path
                                        and os.path.isdir(src_path)
                                        and not os.listdir(src_path)
                                    )
                                ):
                                    from app.core.resilient_file_ops import (
                                        resilient_remove,
                                    )

                                    resilient_remove(src_path)
                                    summary["deleted_folders"] += 1
                            except OSError:
                                pass

                    # Only prune folders this session emptied, never pre-existing
                    # empty or hidden ones elsewhere in the tree.
                    with db_lock:
                        moved_sources = [
                            src for src in path_map if not os.path.lexists(src)
                        ]
                    _prune_emptied_dirs(
                        {os.path.dirname(src) for src in moved_sources},
                        base_dir,
                        protected_paths,
                    )
                else:
                    for node in dirs_to_process:
                        summary["protected_folders"] += 1

                with db_lock:
                    if (db_updates_batch or not has_flushed_db) and hasattr(
                        db, "execute_batch_updates"
                    ):
                        db.execute_batch_updates(list(db_updates_batch))
                        db_updates_batch.clear()
                        has_flushed_db = True

                real_move_count = len(audit_records)
                if session_id and history_manager:
                    ledger = getattr(history_manager, "moves", None)
                    if ledger is not None and hasattr(ledger, "list_moves"):
                        try:
                            from app.core.moves import DONE, VERIFIED

                            recorded_moves = ledger.list_moves(session_id)
                            real_move_count = sum(
                                1
                                for m in recorded_moves
                                if getattr(m, "state", None) in (DONE, VERIFIED)
                            )
                        except Exception:
                            real_move_count = len(audit_records)

                if (
                    not is_child_session
                    and session_id
                    and history_manager
                    and real_move_count > 0
                    and hasattr(history_manager, "complete_session")
                ):
                    try:
                        history_manager.complete_session(session_id)
                    except Exception as complete_err:
                        logging.warning(
                            f"Failed to mark session {session_id} completed: {complete_err}"
                        )
                elif (
                    not is_child_session
                    and session_id
                    and registered_session
                    and history_manager
                    and real_move_count == 0
                ):
                    try:
                        if hasattr(history_manager, "_transition_session_status"):
                            hm_db = getattr(history_manager, "db", None)
                            hm_worker = getattr(hm_db, "worker", None)
                            if hm_worker is not None and hasattr(
                                hm_worker, "execute_write"
                            ):
                                hm_worker.execute_write(
                                    lambda: history_manager._transition_session_status(
                                        session_id, "abandoned"
                                    )
                                )
                            else:
                                history_manager._transition_session_status(
                                    session_id, "abandoned"
                                )
                    except Exception as abandon_err:
                        logging.debug(
                            f"Could not mark empty session {session_id} abandoned: {abandon_err}"
                        )
                    if not resume:
                        session_id = None
                        summary["session_id"] = None

                self.transition_to(MoveEngineState.COMPLETED)
                summary.state = self.state

            from app.core.audit_reporter import generate_audit_report

            if summary.get("cancelled"):
                summary["lifecycle_state"] = PipelineLifecycleState.CANCELLED
            summary["audit_report"] = generate_audit_report(base_dir, audit_records)
            summary["unarchived_children"] = list(unarchived_children_out)
            self.emit_telemetry(
                "move_finish",
                {
                    "session_id": session_id,
                    "total_items": total_items,
                    "cancelled": bool(summary.get("cancelled")),
                },
            )
            return summary

        except Exception as e:
            self.emit_telemetry(
                "move_failed",
                {
                    "session_id": session_id,
                    "error": str(e),
                },
            )
            with db_lock:
                if db_updates_batch and hasattr(db, "execute_batch_updates"):
                    try:
                        db.execute_batch_updates(list(db_updates_batch))
                        db_updates_batch.clear()
                    except Exception:
                        pass

            self.transition_to(MoveEngineState.FAILED)
            rollback_succeeded = False
            if (
                session_id
                and history_manager
                and hasattr(history_manager, "unwind_session")
            ):
                self.transition_to(MoveEngineState.ROLLING_BACK)
                logging.error(
                    f"Error during background sorting: {e}. Initiating automatic rollback for session {session_id}"
                )
                if pipeline and hasattr(pipeline, "transition_to"):
                    try:
                        if pipeline.state != PipelineLifecycleState.ROLLING_BACK:
                            pipeline.transition_to(PipelineLifecycleState.ROLLING_BACK)
                    except Exception:
                        pass
                try:
                    history_manager.unwind_session(session_id, db=db)
                    self.transition_to(MoveEngineState.ROLLED_BACK)
                    rollback_succeeded = True
                    logging.info(
                        f"Automatic rollback completed successfully for session {session_id}"
                    )
                except Exception as rollback_err:
                    logging.error(
                        f"Automatic rollback failed for session {session_id}: {rollback_err}",
                        exc_info=True,
                    )
            if summary is not None:
                summary["lifecycle_state"] = PipelineLifecycleState.FAILED
                summary["rollback_completed"] = rollback_succeeded
            if pipeline and hasattr(pipeline, "transition_to"):
                try:
                    setattr(pipeline, "_rollback_completed", rollback_succeeded)
                    if pipeline.state != PipelineLifecycleState.FAILED:
                        pipeline.transition_to(PipelineLifecycleState.FAILED)
                except Exception:
                    pass
            raise e

    def retry_failed_moves(
        self,
        base_dir: str,
        failed_items: list[dict[str, Any]],
        db=None,
        history_manager=None,
        runtime_settings=None,
        progress_callback=None,
        **kwargs,
    ) -> dict[str, Any]:
        """Re-attempt file move operations for selected or all failed items in mover engine."""
        return retry_failed_moves(
            base_dir,
            failed_items,
            db=db,
            history_manager=history_manager,
            runtime_settings=runtime_settings,
            progress_callback=progress_callback,
            **kwargs,
        )


def execute_moves(
    base_dir: str,
    plan: dict,
    db,
    history_manager,
    runtime_settings=None,
    resume: bool = False,
    batch_size: int = 50,
    chunk_size: int | None = None,
    max_workers: int | None = None,
    cancel_check=None,
    cancel_event=None,
    cancellation_token=None,
    priority: str | None = None,
    progress_callback=None,
    source_dirs: list[str] | None = None,
    pipeline: Any = None,
    **kwargs: Any,
) -> MoveResult:
    """Create directories and safely move files using chunked asynchronous execution.

    Args:
        base_dir: Target base directory path.
        plan: Move operations plan dictionary.
        db: Database connection instance.
        history_manager: History tracking manager.
        runtime_settings: Optional application runtime configuration.
        resume: Flag indicating whether to resume an existing session.
        batch_size: Database transaction batch size.
        chunk_size: Number of files per worker execution chunk.
        max_workers: Maximum concurrency level for worker tasks.
        cancel_check: Optional function returning True if cancellation requested.
        cancel_event: Optional thread event indicating cancellation.
        cancellation_token: Optional cancellation token object.
        priority: Priority level identifier.
        progress_callback: Optional progress emission callback function.
        source_dirs: Optional list of individual source directories being sorted.
        pipeline: Optional pipeline instance for lifecycle state coordination.
        **kwargs: Additional keyword arguments for future extensibility.

    Returns
    -------
        MoveResult: Execution summary and status details.
    """
    engine = AsyncMoveEngine(max_workers=max_workers, chunk_size=chunk_size)
    return engine.execute(
        base_dir=base_dir,
        plan=plan,
        db=db,
        history_manager=history_manager,
        runtime_settings=runtime_settings,
        resume=resume,
        batch_size=batch_size,
        cancel_check=cancel_check,
        cancel_event=cancel_event,
        cancellation_token=cancellation_token,
        priority=priority,
        progress_callback=progress_callback,
        pipeline=pipeline,
        **kwargs,
    )


async def execute_moves_async(
    base_dir: str,
    plan: dict,
    db,
    history_manager,
    runtime_settings=None,
    resume: bool = False,
    batch_size: int = 50,
    chunk_size: int | None = None,
    max_workers: int | None = None,
    cancel_check=None,
    cancel_event=None,
    cancellation_token=None,
    priority: str | None = None,
    progress_callback=None,
) -> dict:
    """Asynchronously execute move operations off the main event loop thread.

    Args:
        base_dir: Target base directory path.
        plan: Move operations plan dictionary.
        db: Database connection instance.
        history_manager: History tracking manager.
        runtime_settings: Optional application runtime configuration.
        resume: Flag indicating whether to resume an existing session.
        batch_size: Database transaction batch size.
        chunk_size: Number of files per worker execution chunk.
        max_workers: Maximum concurrency level for worker tasks.
        cancel_check: Optional function returning True if cancellation requested.
        cancel_event: Optional thread event indicating cancellation.
        cancellation_token: Optional cancellation token object.
        priority: Priority level identifier.
        progress_callback: Optional progress emission callback function.

    Returns
    -------
        dict: Execution summary and status details.
    """
    return await asyncio.to_thread(
        execute_moves,
        base_dir,
        plan,
        db,
        history_manager,
        runtime_settings=runtime_settings,
        resume=resume,
        batch_size=batch_size,
        chunk_size=chunk_size,
        max_workers=max_workers,
        cancel_check=cancel_check,
        cancel_event=cancel_event,
        cancellation_token=cancellation_token,
        priority=priority,
        progress_callback=progress_callback,
    )


def retry_failed_moves(
    base_dir: str,
    failed_items: list[dict[str, Any]],
    db=None,
    history_manager=None,
    runtime_settings=None,
    progress_callback=None,
    **kwargs,
) -> dict[str, Any]:
    """Re-attempt file move operations for selected or all failed items in mover engine.

    Args:
        base_dir: Target base directory path.
        failed_items: List of failed item dictionaries (audit records or queue items).
        db: Optional database connection instance.
        history_manager: Optional history manager instance.
        runtime_settings: Optional application runtime settings.
        progress_callback: Optional progress emission callback.

    Returns
    -------
        dict: Updated execution summary containing regenerated audit report.
    """
    from app.core.audit_reporter import generate_audit_report

    base_dir = os.path.normpath(base_dir) if base_dir else ""
    retried_records: list[dict[str, Any]] = []
    total = len(failed_items)

    for idx, item in enumerate(failed_items):
        src = item.get("source_path") or item.get("relative_source") or ""
        dst = item.get("destination_path") or item.get("relative_destination") or ""

        if src and not os.path.isabs(src) and base_dir:
            src = os.path.normpath(os.path.join(base_dir, src))
        if dst and not os.path.isabs(dst) and base_dir:
            dst = os.path.normpath(os.path.join(base_dir, dst))

        pre_hash = item.get("pre_hash") or ""
        post_hash = None
        move_error = None

        if src and os.path.exists(src):
            try:
                dst_dir = os.path.dirname(dst)
                if dst_dir:
                    os.makedirs(dst_dir, exist_ok=True)

                from app.core.resilient_file_ops import resilient_move

                dst = resilient_move(src, dst)
            except Exception as err:
                move_error = str(err)
                logging.error(f"Retry move failed for {src} -> {dst}: {err}")

        # Post-move verification
        if not move_error and os.path.exists(dst):
            try:
                from app.core.extractor import get_file_hash

                post_hash = get_file_hash(dst)
            except Exception:
                post_hash = None

            if not pre_hash:
                pre_hash = post_hash

            status = (
                MoveVerificationStatus.VERIFIED_PASS
                if (pre_hash == post_hash)
                else MoveVerificationStatus.MISMATCH_FAIL
            )
            if status == MoveVerificationStatus.MISMATCH_FAIL:
                move_error = "Pre-sort and post-sort SHA-256 hash mismatch"
        else:
            status = MoveVerificationStatus.MOVE_FAIL
            if not move_error:
                move_error = "Source file does not exist or move failed"

        rec = {
            "source_path": src,
            "destination_path": dst,
            "pre_hash": pre_hash,
            "post_hash": post_hash,
            "status": status.value,
            "error": (
                move_error if status != MoveVerificationStatus.VERIFIED_PASS else None
            ),
        }
        retried_records.append(rec)

        if progress_callback:
            try:
                progress_callback((idx + 1) / total if total > 0 else 1.0)
            except Exception:
                pass

    report = generate_audit_report(base_dir, retried_records)
    return {
        "base_dir": base_dir,
        "retried_count": total,
        "audit_report": report,
    }


FileMover = AsyncMoveEngine
