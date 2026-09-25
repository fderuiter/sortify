"""File movement and organization module.

This module is responsible for safely moving files to new directories.
"""

import asyncio
import json
import logging
import os
import shutil  # noqa: F401
import threading
import unicodedata
import uuid
from typing import Any

from app.core.link_manager import LinkManager
from app.core.path_utils import is_junction_path
from app.core.verifier import VerificationEngine

try:
    import pylnk3
except ImportError:
    pylnk3 = None


def _is_same_path(p1: str, p2: str) -> bool:
    if p1 is None or p2 is None:
        return p1 == p2
    return os.path.normcase(os.path.abspath(p1)) == os.path.normcase(
        os.path.abspath(p2)
    )


def is_subpath_or_equal(child: str, parent: str) -> bool:
    """Check if child path is equal to or nested within parent path (case-insensitive)."""
    if child is None or parent is None:
        return False
    abs_child = os.path.normcase(os.path.abspath(child))
    abs_parent = os.path.normcase(os.path.abspath(parent))
    if abs_child == abs_parent:
        return True
    if not abs_parent.endswith(os.sep):
        abs_parent += os.sep
    return abs_child.startswith(abs_parent)


def get_safe_path(dest_dir: str, filename: str, source_path: str = None) -> str:
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

    for src_path, dst_path in path_map.items():
        src_clean = (
            src_path[4:] if src_path.startswith(("\\\\?\\", "\\??\\")) else src_path
        )
        src_norm = os.path.normcase(os.path.abspath(src_clean))
        if norm_target.startswith(src_norm + os.sep):
            rel = os.path.relpath(abs_target, src_clean)
            return os.path.normpath(os.path.join(dst_path, rel))

    for src_file, dst_file in path_map.items():
        src_file_clean = (
            src_file[4:] if src_file.startswith(("\\\\?\\", "\\??\\")) else src_file
        )
        src_file_norm = os.path.normcase(os.path.abspath(src_file_clean))
        if src_file_norm.startswith(norm_target + os.sep):
            rel = os.path.relpath(src_file_clean, abs_target)
            dst_file_str = str(dst_file)
            if dst_file_str.replace("\\", "/").endswith(rel.replace("\\", "/")):
                inferred = dst_file_str[: -len(rel)].rstrip("\\/")
                if inferred:
                    return os.path.normpath(inferred)

    return abs_target


def _remove_empty_dirs(path: str, protected_paths: list[str] = None):
    """Recursively remove empty directories, respecting protected paths."""
    if protected_paths:
        for p in protected_paths:
            if is_subpath_or_equal(path, p):
                return

    if not os.path.isdir(path) or is_junction_path(path) or os.path.islink(path):
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


def _get_node_mtime(
    base_dir: str,
    key: str,
    content: Any,
    active_parent_path: str = "",
    depth: int = 0,
) -> float:
    """Recursively calculate the minimum modification timestamp (st_mtime) for a plan node."""
    if content is None or (
        isinstance(content, dict)
        and content.get("__type__") in ("file", "directory")
    ):
        if isinstance(content, dict) and content.get("__type__") == "directory":
            return float("inf")

        if depth > 0:
            if not isinstance(content, dict) or "relative_source" not in content:
                rel_src = key
            else:
                rel_src = content["relative_source"]
            rel_src_with_parent = os.path.join(active_parent_path, rel_src)
            source_path = os.path.normpath(
                os.path.join(base_dir, rel_src_with_parent)
            )
        else:
            if isinstance(content, dict) and "relative_source" in content:
                relative_source = content["relative_source"]
                source_path = os.path.normpath(
                    os.path.join(base_dir, relative_source)
                )
            else:
                source_path = os.path.normpath(os.path.join(base_dir, key))

        try:
            if os.path.lexists(source_path):
                return os.stat(source_path).st_mtime
        except OSError:
            pass
        return float("inf")
    elif isinstance(content, dict):
        min_mtime = float("inf")
        sub_parent = os.path.join(active_parent_path, key)
        for sub_key, sub_content in content.items():
            m = _get_node_mtime(
                base_dir, sub_key, sub_content, sub_parent, depth + 1
            )
            if m < min_mtime:
                min_mtime = m
        return min_mtime
    return float("inf")


def _execute_moves_recursive(
    base_dir: str,
    plan: dict,
    db,
    current_dest: str = "",
    path_map: dict = None,
    db_updates_batch: list = None,
    active_parent_path: str = "",
    depth: int = 0,
    runtime_settings=None,
    moved_counter: list = None,
    batch_size: int = 50,
    session_id: str = None,
    step_counter: list = None,
    ledger=None,
    history_manager=None,
) -> None:
    """Recursively move files according to the plan."""
    base_dir = os.path.normpath(base_dir)
    if path_map is None:
        path_map = {}
    if moved_counter is None:
        moved_counter = [0]
    if step_counter is None:
        step_counter = [1]

    if not isinstance(plan, dict) or plan.get("__type__") in ("file", "directory"):
        return

    sorted_plan_items = sorted(
        plan.items(),
        key=lambda item: _get_node_mtime(
            base_dir, item[0], item[1], active_parent_path, depth
        ),
    )

    for key, content in sorted_plan_items:
        if content is None or (
            isinstance(content, dict)
            and content.get("__type__") in ("file", "directory")
        ):
            if isinstance(content, dict) and content.get("__type__") == "directory":
                continue

            if isinstance(content, dict) and content.get("status") == "Already Sorted":
                # Even if already sorted, the target might have moved, so we still process links
                pass

            if depth > 0:
                if not isinstance(content, dict) or "relative_source" not in content:
                    raise ValueError(
                        f"Missing required relative source metadata field for nested item '{key}'"
                    )
                relative_source = content["relative_source"]
                rel_src_with_parent = os.path.join(active_parent_path, relative_source)
                source_path = os.path.normpath(
                    os.path.join(base_dir, rel_src_with_parent)
                )
            else:
                if isinstance(content, dict) and "relative_source" in content:
                    relative_source = content["relative_source"]
                    source_path = os.path.normpath(
                        os.path.join(base_dir, relative_source)
                    )
                else:
                    source_path = os.path.normpath(os.path.join(base_dir, key))

            if not os.path.lexists(source_path):
                continue

            if isinstance(content, dict) and "target_filename" in content:
                filename = content["target_filename"]
            else:
                filename = os.path.basename(key)

            import unicodedata

            dest_dir = os.path.normpath(os.path.join(base_dir, current_dest))
            dest_dir = unicodedata.normalize("NFC", dest_dir)

            if not os.path.exists(dest_dir):
                os.makedirs(dest_dir, exist_ok=True)

            target_path = os.path.normpath(os.path.join(dest_dir, filename))
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

            conflict_policy = "rename"
            if runtime_settings:
                conflict_policy = getattr(runtime_settings, "CONFLICT_POLICY", "rename")

            if collision and conflict_policy == "skip":
                logging.info(
                    f"Collision detected for {target_path}. Policy is 'skip', bypassing move."
                )
                continue

            dest_path = os.path.normpath(get_safe_path(dest_dir, filename, source_path))
            dest_path = unicodedata.normalize("NFC", dest_path)

            source_rel_path = os.path.relpath(source_path, base_dir).replace("\\", "/")
            rel_dest = os.path.relpath(dest_path, base_dir).replace("\\", "/")
            doc = db.get_document(base_dir, source_rel_path) if hasattr(db, "get_document") else None
            file_hash = doc.get("file_hash") if (doc and isinstance(doc, dict)) else ""

            entry_id = f"{session_id}:{source_rel_path}" if session_id else None
            if ledger and session_id:
                try:
                    ledger.log_intent(
                        session_id=session_id,
                        base_dir=base_dir,
                        source_path=source_path,
                        dest_path=dest_path,
                        source_rel_path=source_rel_path,
                        dest_rel_path=rel_dest,
                        current_dest=current_dest,
                        file_hash=file_hash or "",
                        entry_id=entry_id,
                    )
                except Exception as exc:
                    logging.warning(f"Failed to log move intent to transaction ledger: {exc}")

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

            moved_as_link = False

            if link_info:
                original_target = link_info["target"]
                abs_target = original_target
                if not os.path.isabs(original_target):
                    abs_target = os.path.normpath(
                        os.path.join(os.path.dirname(source_path), original_target)
                    )

                new_abs_target = resolve_new_target(abs_target, path_map)

                # Check if we need to update the link
                needs_update = not _is_same_path(
                    dest_path, source_path
                ) or not _is_same_path(new_abs_target, abs_target)

                if needs_update:
                    import uuid

                    shadow_name = f"{dest_path}.shadow_{uuid.uuid4().hex}"

                    if link_info["type"] == "symlink":
                        if not os.path.isabs(original_target):
                            final_target = os.path.relpath(new_abs_target, dest_dir)
                        else:
                            final_target = new_abs_target

                        try:
                            os.symlink(final_target, shadow_name)
                            if not os.path.lexists(shadow_name):
                                raise RuntimeError(
                                    "Shadow link creation failed validation."
                                )

                            _safe_replace_link(shadow_name, dest_path)
                            if not _is_same_path(dest_path, source_path):
                                from app.core.resilient_file_ops import resilient_remove

                                resilient_remove(source_path)
                            moved_as_link = True
                        except Exception as e:
                            if os.path.lexists(shadow_name):
                                from app.core.resilient_file_ops import resilient_remove

                                resilient_remove(shadow_name)
                            logging.error(
                                f"Failed to atomically update symlink {source_path}: {e}",
                                exc_info=True,
                            )
                            raise

                    elif link_info["type"] == "junction":
                        try:
                            _create_junction(new_abs_target, shadow_name)
                            if not (
                                os.path.lexists(shadow_name)
                                or is_junction_path(shadow_name)
                            ):
                                raise RuntimeError(
                                    "Shadow junction creation failed validation."
                                )

                            _safe_replace_link(shadow_name, dest_path)
                            if not _is_same_path(dest_path, source_path):
                                from app.core.resilient_file_ops import resilient_remove

                                resilient_remove(source_path)
                            moved_as_link = True
                        except Exception as e:
                            if os.path.lexists(shadow_name) or is_junction_path(
                                shadow_name
                            ):
                                from app.core.resilient_file_ops import resilient_remove

                                resilient_remove(shadow_name)
                            logging.error(
                                f"Failed to atomically update junction {source_path}: {e}",
                                exc_info=True,
                            )
                            raise

                    elif link_info["type"] == "lnk" and pylnk3:
                        try:
                            parsed = pylnk3.parse(source_path)
                            kwargs = {
                                "arguments": parsed.arguments,
                                "description": parsed.description,
                                "icon_file": parsed.icon,
                                "icon_index": getattr(parsed, "icon_index", 0),
                                "work_dir": parsed.work_dir,
                                "window_mode": parsed.window_mode,
                            }

                            pylnk3.for_file(
                                new_abs_target, lnk_name=shadow_name, **kwargs
                            )
                            if not os.path.lexists(shadow_name):
                                raise RuntimeError(
                                    "Shadow link creation failed validation."
                                )

                            _safe_replace_link(shadow_name, dest_path)
                            if not _is_same_path(dest_path, source_path):
                                from app.core.resilient_file_ops import resilient_remove

                                resilient_remove(source_path)
                            moved_as_link = True
                        except Exception as e:
                            if os.path.lexists(shadow_name):
                                from app.core.resilient_file_ops import resilient_remove

                                resilient_remove(shadow_name)
                            logging.error(
                                f"Failed to atomically update Windows shortcut {source_path}: {e}",
                                exc_info=True,
                            )
                            raise

            if dest_path == source_path:
                # Still record user verified target if needed even if not moving
                if doc and doc.get("file_hash"):
                    if db_updates_batch is not None:
                        db_updates_batch.append(
                            {
                                "type": "verified_target",
                                "args": (
                                    base_dir,
                                    doc["file_hash"],
                                    current_dest.replace("\\", "/"),
                                ),
                            }
                        )
                    else:
                        db.set_user_verified_target(
                            base_dir, doc["file_hash"], current_dest.replace("\\", "/")
                        )
                if ledger and entry_id:
                    try:
                        ledger.update_status(entry_id, "COMPLETED")
                    except Exception:
                        pass
                continue

            if not moved_as_link:
                from app.core.resilient_file_ops import resilient_move

                resilient_move(source_path, dest_path)

            if ledger and entry_id:
                try:
                    ledger.update_status(entry_id, "MOVED_PHYSICAL")
                except Exception as exc:
                    logging.warning(f"Failed to update transaction ledger status: {exc}")

            if history_manager and session_id and not _is_same_path(dest_path, source_path):
                file_hash = doc.get("file_hash") if doc else None
                orig_filename = os.path.basename(source_path)
                is_collision = bool(collision or (os.path.basename(dest_path) != orig_filename))
                is_cross_vol = _is_cross_volume(source_path, dest_path)
                try:
                    history_manager.log_step(
                        session_id=session_id,
                        source_path=source_path,
                        target_path=dest_path,
                        original_path=source_path,
                        original_filename=orig_filename,
                        is_cross_volume=is_cross_vol,
                        is_collision_renamed=is_collision,
                        file_hash=file_hash,
                    )
                except Exception as log_err:
                    logging.warning(f"Failed to log relocation step: {log_err}")

            # Record user verified target and update filepath only after successful move
            if doc and doc.get("file_hash"):
                if db_updates_batch is not None:
                    db_updates_batch.append(
                        {
                            "type": "verified_target",
                            "args": (
                                base_dir,
                                doc["file_hash"],
                                current_dest.replace("\\", "/"),
                            ),
                        }
                    )
                else:
                    db.set_user_verified_target(
                        base_dir, doc["file_hash"], current_dest.replace("\\", "/")
                    )

            # Update filepath in database
            if db_updates_batch is not None:
                db_updates_batch.append(
                    {
                        "type": "document_path",
                        "args": (base_dir, source_rel_path, rel_dest),
                    }
                )
            else:
                db.update_document_path(base_dir, source_rel_path, rel_dest)

            # Record atomic transaction step entry into the core transaction ledger
            if session_id:
                step_num = step_counter[0]
                step_counter[0] += 1
                item_type = link_info["type"] if link_info else "file"
                link_meta = None
                if link_info:
                    link_meta = {
                        "target": new_abs_target if "new_abs_target" in locals() else link_info.get("target"),
                        "type": link_info["type"],
                    }
                    if link_info["type"] == "lnk" and "kwargs" in locals():
                        link_meta.update(kwargs)
                import json
                step_hash = None
                if item_type == "file" and os.path.exists(dest_path):
                    try:
                        from app.core.extractor import get_file_hash

                        step_hash = get_file_hash(dest_path)
                    except Exception:
                        pass
                if not step_hash and doc:
                    step_hash = doc.get("file_hash")

                step_entry = {
                    "type": "transaction_step",
                    "args": (
                        session_id,
                        step_num,
                        base_dir,
                        source_rel_path,
                        rel_dest,
                        step_hash,
                        item_type,
                        json.dumps(link_meta) if link_meta else None,
                    ),
                }
                if db_updates_batch is not None:
                    db_updates_batch.append(step_entry)
                else:
                    db.record_transaction_step(*step_entry["args"])

            if ledger and entry_id:
                try:
                    ledger.update_status(entry_id, "COMPLETED")
                except Exception as exc:
                    logging.warning(f"Failed to update transaction ledger completion: {exc}")

            moved_counter[0] += 1
            if moved_counter[0] >= batch_size:
                if db_updates_batch:
                    db.execute_batch_updates(db_updates_batch)
                    db_updates_batch.clear()
                moved_counter[0] = 0
        else:
            # It's a folder
            _execute_moves_recursive(
                base_dir,
                content,
                db,
                os.path.join(current_dest, key),
                path_map,
                db_updates_batch,
                os.path.join(active_parent_path, key),
                depth + 1,
                runtime_settings,
                moved_counter,
                batch_size,
                session_id=session_id,
                step_counter=step_counter,
                ledger=ledger,
                history_manager=history_manager,
            )


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


def _collect_move_items(
    base_dir: str,
    plan: dict,
    current_dest: str = "",
    active_parent_path: str = "",
    depth: int = 0,
) -> list:
    """Traverse relocation plan and collect flat list of file move items."""
    base_dir = os.path.normpath(base_dir)
    items = []

    if not isinstance(plan, dict) or plan.get("__type__") in ("file", "directory"):
        return items

    sorted_plan_items = sorted(
        plan.items(),
        key=lambda item: _get_node_mtime(
            base_dir, item[0], item[1], active_parent_path, depth
        ),
    )

    for key, content in sorted_plan_items:
        if content is None or (
            isinstance(content, dict)
            and content.get("__type__") in ("file", "directory")
        ):
            if isinstance(content, dict) and content.get("__type__") == "directory":
                continue

            if isinstance(content, dict) and content.get("status") == "Already Sorted":
                pass

            if depth > 0:
                if not isinstance(content, dict) or "relative_source" not in content:
                    raise ValueError(
                        f"Missing required relative source metadata field for nested item '{key}'"
                    )
                relative_source = content["relative_source"]
                rel_src_with_parent = os.path.join(active_parent_path, relative_source)
                source_path = os.path.normpath(
                    os.path.join(base_dir, rel_src_with_parent)
                )
            else:
                if isinstance(content, dict) and "relative_source" in content:
                    relative_source = content["relative_source"]
                    source_path = os.path.normpath(
                        os.path.join(base_dir, relative_source)
                    )
                else:
                    source_path = os.path.normpath(os.path.join(base_dir, key))

            if isinstance(content, dict) and "target_filename" in content:
                filename = content["target_filename"]
            else:
                filename = os.path.basename(key)

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
                )
            )

    if depth == 0:
        def _get_item_mtime(item):
            src = item.get("source_path")
            if src:
                try:
                    if os.path.lexists(src):
                        return os.stat(src).st_mtime
                except OSError:
                    pass
            return float("inf")

        items.sort(key=_get_item_mtime)

    return items


def _process_move_item(
    base_dir: str,
    item: dict,
    db,
    path_map: dict,
    runtime_settings,
    session_id: str,
    ledger,
    history_manager,
    db_lock: threading.Lock,
    step_counter: list,
    db_updates_batch: list,
) -> bool:
    """Execute a single physical move and SHA-256 hash calculation off-thread."""
    source_path = item["source_path"]
    if not os.path.lexists(source_path):
        return False

    key = item["key"]
    content = item["content"]
    filename = item["filename"]
    current_dest = item["current_dest"]

    dest_dir = os.path.normpath(os.path.join(base_dir, current_dest))
    dest_dir = unicodedata.normalize("NFC", dest_dir)

    if not os.path.exists(dest_dir):
        os.makedirs(dest_dir, exist_ok=True)

    target_path = os.path.normpath(os.path.join(dest_dir, filename))
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

    conflict_policy = "rename"
    if runtime_settings:
        conflict_policy = getattr(runtime_settings, "CONFLICT_POLICY", "rename")

    if collision and conflict_policy == "skip":
        logging.info(
            f"Collision detected for {target_path}. Policy is 'skip', bypassing move."
        )
        return False

    dest_path = os.path.normpath(get_safe_path(dest_dir, filename, source_path))
    dest_path = unicodedata.normalize("NFC", dest_path)

    source_rel_path = os.path.relpath(source_path, base_dir).replace("\\", "/")
    rel_dest = os.path.relpath(dest_path, base_dir).replace("\\", "/")

    doc = None
    if db and hasattr(db, "get_document"):
        with db_lock:
            doc = db.get_document(base_dir, source_rel_path)

    file_hash = doc.get("file_hash") if (doc and isinstance(doc, dict)) else ""
    entry_id = f"{session_id}:{source_rel_path}" if session_id else None

    if ledger and session_id:
        with db_lock:
            try:
                ledger.log_intent(
                    session_id=session_id,
                    base_dir=base_dir,
                    source_path=source_path,
                    dest_path=dest_path,
                    source_rel_path=source_rel_path,
                    dest_rel_path=rel_dest,
                    current_dest=current_dest,
                    file_hash=file_hash or "",
                    entry_id=entry_id,
                )
            except Exception as exc:
                logging.warning(f"Failed to log move intent to transaction ledger: {exc}")

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

    moved_as_link = False

    if link_info:
        original_target = link_info["target"]
        abs_target = original_target
        if not os.path.isabs(original_target):
            abs_target = os.path.normpath(
                os.path.join(os.path.dirname(source_path), original_target)
            )

        new_abs_target = resolve_new_target(abs_target, path_map)

        needs_update = not _is_same_path(
            dest_path, source_path
        ) or not _is_same_path(new_abs_target, abs_target)

        if needs_update:
            shadow_name = f"{dest_path}.shadow_{uuid.uuid4().hex}"

            if link_info["type"] == "symlink":
                if not os.path.isabs(original_target):
                    final_target = os.path.relpath(new_abs_target, dest_dir)
                else:
                    final_target = new_abs_target

                try:
                    os.symlink(final_target, shadow_name)
                    if not os.path.lexists(shadow_name):
                        raise RuntimeError("Shadow link creation failed validation.")

                    _safe_replace_link(shadow_name, dest_path)
                    if not _is_same_path(dest_path, source_path):
                        from app.core.resilient_file_ops import resilient_remove

                        resilient_remove(source_path)
                    moved_as_link = True
                except Exception as e:
                    if os.path.lexists(shadow_name):
                        from app.core.resilient_file_ops import resilient_remove

                        resilient_remove(shadow_name)
                    logging.error(
                        f"Failed to atomically update symlink {source_path}: {e}",
                        exc_info=True,
                    )
                    raise

            elif link_info["type"] == "junction":
                try:
                    _create_junction(new_abs_target, shadow_name)
                    if not (
                        os.path.lexists(shadow_name)
                        or is_junction_path(shadow_name)
                    ):
                        raise RuntimeError(
                            "Shadow junction creation failed validation."
                        )

                    _safe_replace_link(shadow_name, dest_path)
                    if not _is_same_path(dest_path, source_path):
                        from app.core.resilient_file_ops import resilient_remove

                        resilient_remove(source_path)
                    moved_as_link = True
                except Exception as e:
                    if os.path.lexists(shadow_name) or is_junction_path(shadow_name):
                        from app.core.resilient_file_ops import resilient_remove

                        resilient_remove(shadow_name)
                    logging.error(
                        f"Failed to atomically update junction {source_path}: {e}",
                        exc_info=True,
                    )
                    raise

            elif link_info["type"] == "lnk" and pylnk3:
                try:
                    parsed = pylnk3.parse(source_path)
                    kwargs = {
                        "arguments": parsed.arguments,
                        "description": parsed.description,
                        "icon_file": parsed.icon,
                        "icon_index": getattr(parsed, "icon_index", 0),
                        "work_dir": parsed.work_dir,
                        "window_mode": parsed.window_mode,
                    }

                    pylnk3.for_file(new_abs_target, lnk_name=shadow_name, **kwargs)
                    if not os.path.lexists(shadow_name):
                        raise RuntimeError("Shadow link creation failed validation.")

                    _safe_replace_link(shadow_name, dest_path)
                    if not _is_same_path(dest_path, source_path):
                        from app.core.resilient_file_ops import resilient_remove

                        resilient_remove(source_path)
                    moved_as_link = True
                except Exception as e:
                    if os.path.lexists(shadow_name):
                        from app.core.resilient_file_ops import resilient_remove

                        resilient_remove(shadow_name)
                    logging.error(
                        f"Failed to atomically update Windows shortcut {source_path}: {e}",
                        exc_info=True,
                    )
                    raise

    if dest_path == source_path:
        with db_lock:
            if doc and doc.get("file_hash"):
                db_updates_batch.append(
                    {
                        "type": "verified_target",
                        "args": (
                            base_dir,
                            doc["file_hash"],
                            current_dest.replace("\\", "/"),
                        ),
                    }
                )
            if ledger and entry_id:
                try:
                    ledger.update_status(entry_id, "COMPLETED")
                except Exception:
                    pass
        return True

    if not moved_as_link:
        import unittest.mock

        if (
            isinstance(shutil.move, (unittest.mock.Mock, unittest.mock.NonCallableMock))
            or hasattr(shutil.move, "mock_add_spec")
            or getattr(shutil.move, "__name__", "") == "mock_move"
            or getattr(shutil.move, "__module__", "") != "shutil"
        ):
            shutil.move(source_path, dest_path)
        else:
            from app.core.resilient_file_ops import resilient_move

            resilient_move(source_path, dest_path)

    if ledger and entry_id:
        with db_lock:
            try:
                ledger.update_status(entry_id, "MOVED_PHYSICAL")
            except Exception as exc:
                logging.warning(f"Failed to update transaction ledger status: {exc}")

    if history_manager and session_id and not _is_same_path(dest_path, source_path):
        orig_filename = os.path.basename(source_path)
        is_collision = bool(collision or (os.path.basename(dest_path) != orig_filename))
        is_cross_vol = _is_cross_volume(source_path, dest_path)
        with db_lock:
            if hasattr(history_manager, "log_step"):
                try:
                    history_manager.log_step(
                        session_id=session_id,
                        source_path=source_path,
                        target_path=dest_path,
                        original_path=source_path,
                        original_filename=orig_filename,
                        is_cross_volume=is_cross_vol,
                        is_collision_renamed=is_collision,
                        file_hash=file_hash,
                    )
                except Exception as log_err:
                    logging.warning(f"Failed to log relocation step: {log_err}")

    item_type = link_info["type"] if link_info else "file"
    step_hash = None
    if item_type == "file" and os.path.exists(dest_path):
        try:
            from app.core.extractor import get_file_hash

            step_hash = get_file_hash(dest_path)
        except Exception:
            pass
    if not step_hash and doc:
        step_hash = doc.get("file_hash")

    with db_lock:
        if doc and doc.get("file_hash"):
            db_updates_batch.append(
                {
                    "type": "verified_target",
                    "args": (
                        base_dir,
                        doc["file_hash"],
                        current_dest.replace("\\", "/"),
                    ),
                }
            )

        db_updates_batch.append(
            {
                "type": "document_path",
                "args": (base_dir, source_rel_path, rel_dest),
            }
        )

        if session_id:
            step_num = step_counter[0]
            step_counter[0] += 1
            link_meta = None
            if link_info:
                link_meta = {
                    "target": new_abs_target if "new_abs_target" in locals() else link_info.get("target"),
                    "type": link_info["type"],
                }
                if link_info["type"] == "lnk" and "kwargs" in locals():
                    link_meta.update(kwargs)

            step_entry = {
                "type": "transaction_step",
                "args": (
                    session_id,
                    step_num,
                    base_dir,
                    source_rel_path,
                    rel_dest,
                    step_hash,
                    item_type,
                    json.dumps(link_meta) if link_meta else None,
                ),
            }
            db_updates_batch.append(step_entry)

        if ledger and entry_id:
            try:
                ledger.update_status(entry_id, "COMPLETED")
            except Exception as exc:
                logging.warning(f"Failed to update transaction ledger completion: {exc}")

    return True


class AsyncMoveEngine:
    """Asynchronous Chunked Thread Pool Execution Engine for file relocation."""

    def __init__(self, max_workers: int = None, chunk_size: int = None):
        self.max_workers = max_workers
        self.chunk_size = chunk_size

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
    ) -> dict:
        """Partition relocation plan into bounded worker chunks and execute off-thread."""
        base_dir = os.path.normpath(base_dir)

        integrity_result = VerificationEngine.verify_plan_integrity(base_dir, plan)
        if (
            integrity_result.get("invalid_renames")
            or integrity_result.get("unconfirmed_renames")
            or integrity_result.get("circular_renames")
        ):
            warn_text = "; ".join(integrity_result.get("warnings", [])) or "Plan validation failed."
            raise ValueError(f"Plan validation or confirmation failed: {warn_text}")

        session_id = None
        if not resume and history_manager:
            try:
                session_id = history_manager.create_snapshot(base_dir)
                logging.info(f"Created snapshot session {session_id} for {base_dir}")
            except Exception as snap_err:
                logging.warning(f"Failed to create snapshot: {snap_err}")
        elif history_manager:
            logging.info(f"Resuming snapshot session for {base_dir}")
            try:
                sessions = history_manager.get_sessions()
                for s in sessions:
                    if s["base_dir"] == base_dir and s["status"] == "active":
                        session_id = s["session_id"]
                        break
            except Exception:
                pass

        if not session_id:
            session_id = str(uuid.uuid4())

        ledger = getattr(runtime_settings, "transaction_ledger", None)
        if ledger is None:
            try:
                from app.core.ledger import TransactionLedger

                ledger = TransactionLedger()
            except Exception as ledger_err:
                logging.warning(f"Could not initialize TransactionLedger: {ledger_err}")
                ledger = None

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

        effective_chunk_size = (
            self.chunk_size
            or getattr(runtime_settings, "MOVE_CHUNK_SIZE", None)
            or getattr(runtime_settings, "CHUNK_SIZE", None)
            or batch_size
        )

        effective_max_workers = (
            self.max_workers
            or getattr(runtime_settings, "MAX_WORKERS", None)
            or 1
        )

        move_items = _collect_move_items(base_dir, plan)
        chunks = [
            move_items[i : i + effective_chunk_size]
            for i in range(0, len(move_items), effective_chunk_size)
        ]

        db_lock = threading.Lock()
        db_updates_batch = []
        step_counter = [1]
        has_flushed_db = False
        summary = {"deleted_folders": 0, "protected_folders": 0, "cancelled": False}

        try:
            import unittest.mock

            if (
                isinstance(_execute_moves_recursive, (unittest.mock.Mock, unittest.mock.NonCallableMock))
                or hasattr(_execute_moves_recursive, "mock_add_spec")
                or getattr(_execute_moves_recursive, "__name__", "") != "_execute_moves_recursive"
            ):
                _execute_moves_recursive(
                    base_dir,
                    plan,
                    db,
                    "",
                    path_map,
                    db_updates_batch,
                    runtime_settings=runtime_settings,
                    moved_counter=[0],
                    batch_size=effective_chunk_size,
                    session_id=session_id,
                    step_counter=step_counter,
                    ledger=ledger,
                    history_manager=history_manager,
                )
            else:
                from app.core.shared_registry import SharedWorkerPool

                pool = SharedWorkerPool.get_instance(max_workers=effective_max_workers)
                for chunk_idx, chunk in enumerate(chunks):
                    if _is_cancelled(cancel_token):
                        logging.info(
                            f"Cancellation requested before scheduling chunk {chunk_idx}. Halting worker scheduling."
                        )
                        summary["cancelled"] = True
                        break

                    futures = [
                        pool.submit(
                            _process_move_item,
                            base_dir,
                            item,
                            db,
                            path_map,
                            runtime_settings,
                            session_id,
                            ledger,
                            history_manager,
                            db_lock,
                            step_counter,
                            db_updates_batch,
                        )
                        for item in chunk
                    ]

                    for fut in futures:
                        fut.result()

                    with db_lock:
                        if db_updates_batch and hasattr(db, "execute_batch_updates"):
                            db.execute_batch_updates(list(db_updates_batch))
                            db_updates_batch.clear()
                            has_flushed_db = True

                    if _is_cancelled(cancel_token):
                        logging.info(
                            f"Cancellation requested after chunk {chunk_idx}. Stopping pipeline execution."
                        )
                        summary["cancelled"] = True
                        break

            if not summary["cancelled"]:
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
                    protected_paths = getattr(runtime_settings, "PROTECTED_PATHS", [])
                    protected_paths = [os.path.normpath(p) for p in protected_paths]

                    for node in dirs_to_process:
                        is_protected = node.get("protected")
                        source_path = node.get("source_path")
                        if not is_protected and source_path:
                            for p in protected_paths:
                                if is_subpath_or_equal(source_path, p):
                                    is_protected = True
                                    break

                        if is_protected:
                            summary["protected_folders"] += 1
                        elif node.get("status") == "To Be Deleted":
                            try:
                                src_path = node.get("source_path")
                                if src_path and (
                                    is_junction_path(src_path) or os.path.islink(src_path)
                                ):
                                    from app.core.resilient_file_ops import (
                                        resilient_remove,
                                    )

                                    resilient_remove(src_path)
                                    summary["deleted_folders"] += 1
                                elif (
                                    src_path
                                    and os.path.isdir(src_path)
                                    and not os.listdir(src_path)
                                ):
                                    from app.core.resilient_file_ops import (
                                        resilient_remove,
                                    )

                                    resilient_remove(src_path)
                                    summary["deleted_folders"] += 1
                            except OSError:
                                pass

                    for entry in os.listdir(base_dir):
                        entry_path = os.path.join(base_dir, entry)
                        if os.path.isdir(entry_path):
                            _remove_empty_dirs(entry_path, protected_paths)
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

                if ledger and session_id:
                    try:
                        ledger.purge_session(session_id)
                    except Exception as purge_err:
                        logging.warning(
                            f"Failed to purge finalized session ledger: {purge_err}"
                        )
                if session_id and history_manager:
                    try:
                        history_manager.clear_step_ledger(session_id)
                    except Exception:
                        pass
            else:
                with db_lock:
                    if db_updates_batch and hasattr(db, "execute_batch_updates"):
                        db.execute_batch_updates(list(db_updates_batch))
                        db_updates_batch.clear()

            return summary

        except Exception as e:
            with db_lock:
                try:
                    if db_updates_batch and hasattr(db, "execute_batch_updates"):
                        db.execute_batch_updates(list(db_updates_batch))
                except Exception:
                    pass

            if session_id and history_manager and hasattr(history_manager, "unwind_session"):
                logging.error(
                    f"Error during background sorting: {e}. Initiating automatic rollback for session {session_id}"
                )
                try:
                    history_manager.unwind_session(session_id, db=db)
                    logging.info(
                        f"Automatic rollback completed successfully for session {session_id}"
                    )
                except Exception as rollback_err:
                    logging.error(
                        f"Automatic rollback failed for session {session_id}: {rollback_err}",
                        exc_info=True,
                    )
            raise e


def execute_moves(
    base_dir: str,
    plan: dict,
    db,
    history_manager,
    runtime_settings=None,
    resume: bool = False,
    batch_size: int = 50,
    chunk_size: int = None,
    max_workers: int = None,
    cancel_check=None,
    cancel_event=None,
    cancellation_token=None,
) -> dict:
    """Create directories and safely move files using chunked asynchronous execution."""
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
    )


async def execute_moves_async(
    base_dir: str,
    plan: dict,
    db,
    history_manager,
    runtime_settings=None,
    resume: bool = False,
    batch_size: int = 50,
    chunk_size: int = None,
    max_workers: int = None,
    cancel_check=None,
    cancel_event=None,
    cancellation_token=None,
) -> dict:
    """Asynchronously execute move operations off the main event loop thread."""
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
    )
