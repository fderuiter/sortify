"""File movement and organization module.

This module is responsible for safely moving files to new directories.
"""

import logging
import os
import shutil  # noqa: F401

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
    history_manager=None,
    session_id=None,
) -> None:
    """Recursively move files according to the plan."""
    base_dir = os.path.normpath(base_dir)
    if path_map is None:
        path_map = {}

    if not isinstance(plan, dict) or plan.get("__type__") in ("file", "directory"):
        return

    for key, content in plan.items():
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
                        if not os.path.isabs(original_target):
                            final_target = os.path.relpath(new_abs_target, dest_dir)
                        else:
                            final_target = new_abs_target

                        try:
                            _create_junction(final_target, shadow_name)
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

            source_rel_path = os.path.relpath(source_path, base_dir).replace("\\", "/")
            doc = db.get_document(base_dir, source_rel_path)

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
                continue

            if not moved_as_link:
                from app.core.resilient_file_ops import resilient_move

                resilient_move(source_path, dest_path)

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
            rel_dest = os.path.relpath(dest_path, base_dir).replace("\\", "/")
            if db_updates_batch is not None:
                db_updates_batch.append(
                    {
                        "type": "document_path",
                        "args": (base_dir, source_rel_path, rel_dest),
                    }
                )
            else:
                db.update_document_path(base_dir, source_rel_path, rel_dest)
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
                history_manager,
                session_id,
            )


def execute_moves(
    base_dir: str,
    plan: dict,
    db,
    history_manager,
    runtime_settings=None,
    resume: bool = False,
) -> dict:
    """Create directories and safely move files, tracking file-system errors."""
    base_dir = os.path.normpath(base_dir)
    session_id = None
    if not resume:
        # Create a full snapshot of the directory tree and metadata before moving files
        session_id = history_manager.create_snapshot(base_dir)
        logging.info(f"Created snapshot session {session_id} for {base_dir}")
    else:
        logging.info(f"Resuming snapshot session for {base_dir}")
        try:
            sessions = history_manager.get_sessions()
            for s in sessions:
                if s["base_dir"] == base_dir and s["status"] == "active":
                    session_id = s["session_id"]
                    break
        except Exception:
            pass

    # Build path mapping to track where targets move
    moves_list = VerificationEngine.get_moves(base_dir, plan)
    path_map = {}
    for rel_src, src, dst in moves_list:
        path_map[os.path.normcase(os.path.abspath(src))] = os.path.abspath(dst)

    # Execute all moves first
    db_updates_batch = []
    try:
        _execute_moves_recursive(
            base_dir,
            plan,
            db,
            "",
            path_map,
            db_updates_batch,
            runtime_settings=runtime_settings,
            history_manager=history_manager,
            session_id=session_id,
        )

        summary = {"deleted_folders": 0, "protected_folders": 0}
        cleanup_enabled = (
            getattr(runtime_settings, "CLEANUP_EMPTY_FOLDERS", True)
            if runtime_settings
            else True
        )

        # Find the directory nodes in the plan
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

        # Sort by descending depth to delete subdirectories before parents
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
                            from app.core.resilient_file_ops import resilient_remove

                            resilient_remove(src_path)
                            summary["deleted_folders"] += 1
                        elif (
                            src_path
                            and os.path.isdir(src_path)
                            and not os.listdir(src_path)
                        ):
                            from app.core.resilient_file_ops import resilient_remove

                            resilient_remove(src_path)
                            summary["deleted_folders"] += 1
                    except OSError:
                        pass

            # Guarantee complete cleanup of empty directories after all explicit plan folders are processed
            for entry in os.listdir(base_dir):
                entry_path = os.path.join(base_dir, entry)
                if os.path.isdir(entry_path):
                    _remove_empty_dirs(entry_path, protected_paths)
        else:
            for node in dirs_to_process:
                summary["protected_folders"] += 1

        db.execute_batch_updates(db_updates_batch)
        if session_id and history_manager:
            try:
                history_manager.clear_step_ledger(session_id)
            except Exception:
                pass
        return summary

    except Exception as e:
        try:
            db.execute_batch_updates(db_updates_batch)
        except Exception:
            pass

        if session_id and history_manager:
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
