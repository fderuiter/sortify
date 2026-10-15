"""Directory scanning utility."""

import logging
import os

from app.core.link_manager import LinkManager
from app.core.path_utils import is_junction_entry


def get_files_recursively(
    base: str, rel_path: str = "", include_hidden: bool = False
) -> list:
    """Recursively list all files in a directory deterministically."""
    files = []
    if rel_path == "":
        LinkManager.clear()

    try:
        # Sorting the scanned entries ensures deterministic file discovery
        with os.scandir(os.path.join(base, rel_path)) as entries:
            sorted_entries = sorted(entries, key=lambda e: e.name)
            for entry in sorted_entries:
                if not include_hidden and entry.name.startswith("."):
                    continue
                # Quarantine isolation: unreleased files in _Quarantine_Staging must remain inaccessible to standard scans
                if entry.name == "_Quarantine_Staging" or entry.name.startswith(
                    "_Quarantine_Staging"
                ):
                    continue
                entry_rel_path = (
                    os.path.join(rel_path, entry.name) if rel_path else entry.name
                )

                # Check for junction, symlink, or .lnk file
                if (
                    is_junction_entry(entry)
                    or entry.is_symlink()
                    or entry.name.lower().endswith(".lnk")
                ):
                    LinkManager.register_link(base, entry_rel_path)
                    files.append(entry_rel_path)
                elif entry.is_dir(follow_symlinks=False):
                    files.extend(
                        get_files_recursively(
                            base, entry_rel_path, include_hidden=include_hidden
                        )
                    )
                else:
                    files.append(entry_rel_path)
    except Exception as e:
        logging.error(
            f"Failed to scan directory {os.path.join(base, rel_path)}: {e}",
            exc_info=True,
        )

    if rel_path == "":
        return sorted(files)
    return files


def parse_item_identifier(item: str, default_base_dir: str = "") -> tuple[str, str]:
    """Parse an item identifier into (source_root, relative_path)."""
    if "::" in item:
        src_root, rel_path = item.split("::", 1)
        return os.path.normpath(src_root), rel_path
    base = os.path.normpath(default_base_dir) if default_base_dir else ""
    return base, item


def get_files_from_sources(
    source_dirs: list[str], include_hidden: bool = False
) -> list[str]:
    """Scan all configured source roots and produce namespaced relative path identifiers."""
    norm_sources = []
    for d in source_dirs:
        if d:
            abs_d = os.path.normpath(os.path.abspath(os.path.expanduser(d)))
            if abs_d not in norm_sources and os.path.isdir(abs_d):
                norm_sources.append(abs_d)

    if not norm_sources:
        return []

    if len(norm_sources) == 1:
        return get_files_recursively(norm_sources[0], include_hidden=include_hidden)

    all_files = []
    for src_root in norm_sources:
        scanned = get_files_recursively(src_root, include_hidden=include_hidden)
        for rel_path in scanned:
            all_files.append(f"{src_root}::{rel_path}")

    return sorted(all_files)
