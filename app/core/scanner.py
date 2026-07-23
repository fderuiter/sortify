"""Directory scanning utility."""

import logging
import os

from app.core.extractor_strategies import registry
from app.core.link_manager import LinkManager


def get_files_recursively(
    base: str, rel_path: str = "", include_hidden: bool = False, depth_limit: int = None
) -> list:
    """Recursively list all files in a directory deterministically using an iterative stack-based approach."""
    if rel_path == "":
        LinkManager.clear()

    if depth_limit is None:
        try:
            from app.config import AppSettings
            settings = AppSettings()
            depth_limit = settings.FOLDER_DEPTH_LIMIT
        except Exception:
            depth_limit = 100

    if not isinstance(depth_limit, int) or depth_limit <= 0:
        depth_limit = 100

    files = []

    init_depth = 0
    if rel_path:
        init_depth = len(os.path.normpath(rel_path).split(os.sep))

    stack = [("dir", os.path.basename(rel_path) or "", False, rel_path, init_depth)]

    while stack:
        type_, name, is_symlink, curr_rel, depth = stack.pop()

        if type_ == "file":
            if is_symlink:
                LinkManager.register_link(base, curr_rel)
                files.append(curr_rel)
            else:
                _, ext = os.path.splitext(name)
                if registry.is_supported(ext):
                    files.append(curr_rel)
                elif include_hidden:
                    files.append(curr_rel)
        elif type_ == "dir":
            if depth >= depth_limit:
                continue

            try:
                full_path = os.path.join(base, curr_rel)
                with os.scandir(full_path) as entries:
                    sorted_entries = sorted(entries, key=lambda e: e.name)
                    for entry in reversed(sorted_entries):
                        if not include_hidden and entry.name.startswith("."):
                            continue
                        entry_rel_path = (
                            os.path.join(curr_rel, entry.name) if curr_rel else entry.name
                        )

                        is_lnk_or_symlink = entry.is_symlink() or entry.name.lower().endswith(".lnk")
                        if is_lnk_or_symlink:
                            stack.append(("file", entry.name, True, entry_rel_path, depth + 1))
                        elif entry.is_dir(follow_symlinks=False):
                            stack.append(("dir", entry.name, False, entry_rel_path, depth + 1))
                        else:
                            stack.append(("file", entry.name, False, entry_rel_path, depth + 1))
            except Exception as e:
                logging.error(
                    f"Failed to scan directory {os.path.join(base, curr_rel)}: {e}",
                    exc_info=True,
                )

    if rel_path == "":
        return sorted(files)
    return files
