"""Directory scanning utility."""

import json
import logging
import os

from app.core.link_manager import LinkManager
from app.core.path_utils import is_junction_entry

_DEFAULT_QUARANTINE_DIRS = ("_Quarantine_Staging", "_Compliance_Quarantine")


def _normalize_quarantine_dir_prefix(raw_name: str) -> str:
    """Extract top-level directory name for quarantine scan exclusion."""
    cleaned = str(raw_name or "").replace("\\", "/").strip().strip("/")
    if not cleaned:
        return ""
    return cleaned.split("/")[0].strip()


def _get_quarantine_ignore_prefixes() -> tuple[str, ...]:
    """Return quarantine directory prefixes that must never be scanned or re-sorted."""
    prefixes: list[str] = list(_DEFAULT_QUARANTINE_DIRS)
    try:
        from app.config import AppSettings, get_app_dir

        active_inst = getattr(AppSettings, "_instance", None)
        if active_inst is not None:
            mem_qdir = _normalize_quarantine_dir_prefix(
                getattr(active_inst, "QUARANTINE_DIR_NAME", "")
            )
            if mem_qdir and mem_qdir not in prefixes:
                prefixes.append(mem_qdir)

        settings_path = get_app_dir() / "settings.json"
        if settings_path.exists():
            with open(settings_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                disk_qdir = _normalize_quarantine_dir_prefix(
                    data.get("QUARANTINE_DIR_NAME", "")
                )
                if disk_qdir and disk_qdir not in prefixes:
                    prefixes.append(disk_qdir)
    except (OSError, ValueError, TypeError) as exc:
        logging.debug("Could not read custom quarantine dir from settings: %s", exc)
    return tuple(prefixes)


def _is_quarantine_entry_name(name: str, ignore_prefixes: tuple[str, ...]) -> bool:
    """Return True if directory/entry name belongs to staging or compliance quarantine."""
    if name.startswith("_Quarantine_Staging") or name.startswith(
        "_Compliance_Quarantine"
    ):
        return True
    return name in ignore_prefixes


def _scan_dir_recursive(
    base: str,
    rel_path: str,
    include_hidden: bool,
    ignore_prefixes: tuple[str, ...],
) -> list[str]:
    """Walk a directory recursively while skipping quarantine directories."""
    files: list[str] = []
    try:
        # Sorting the scanned entries ensures deterministic file discovery
        with os.scandir(os.path.join(base, rel_path)) as entries:
            sorted_entries = sorted(entries, key=lambda e: e.name)
            for entry in sorted_entries:
                if not include_hidden and entry.name.startswith("."):
                    continue
                # Quarantine isolation: staged and held compliance quarantine folders must remain excluded from scans
                if _is_quarantine_entry_name(entry.name, ignore_prefixes):
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
                        _scan_dir_recursive(
                            base,
                            entry_rel_path,
                            include_hidden=include_hidden,
                            ignore_prefixes=ignore_prefixes,
                        )
                    )
                else:
                    files.append(entry_rel_path)
    except Exception as e:
        logging.error(
            f"Failed to scan directory {os.path.join(base, rel_path)}: {e}",
            exc_info=True,
        )
    return files


def get_files_recursively(
    base: str, rel_path: str = "", include_hidden: bool = False
) -> list:
    """Recursively list all files in a directory deterministically."""
    if rel_path == "":
        LinkManager.clear()

    ignore_prefixes = _get_quarantine_ignore_prefixes()
    if rel_path:
        first_seg = rel_path.replace("\\", "/").strip("/").split("/")[0]
        if _is_quarantine_entry_name(first_seg, ignore_prefixes):
            return []

    files = _scan_dir_recursive(
        base,
        rel_path,
        include_hidden=include_hidden,
        ignore_prefixes=ignore_prefixes,
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
