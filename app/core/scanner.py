"""Directory scanning utility and unified ScanFilter."""

import contextlib
import json
import logging
import os
from contextvars import ContextVar
from typing import Any, Optional

from app.core.link_manager import LinkManager
from app.core.path_utils import is_junction_entry

_DEFAULT_QUARANTINE_DIRS = ("_Quarantine_Staging", "_Compliance_Quarantine")
DEFAULT_IGNORED_EXTENSIONS = (".crdownload", ".tmp", ".download")
_ACTIVE_SCAN_FILTER: ContextVar[Optional["ScanFilter"]] = ContextVar(
    "_ACTIVE_SCAN_FILTER", default=None
)


@contextlib.contextmanager
def active_scan_filter(scan_filter: "ScanFilter"):
    """Temporarily bind a ScanFilter for nested get_files_recursively calls."""
    token = _ACTIVE_SCAN_FILTER.set(scan_filter)
    try:
        yield scan_filter
    finally:
        _ACTIVE_SCAN_FILTER.reset(token)


def _normalize_quarantine_dir_prefix(raw_name: str) -> str:
    """Extract top-level directory name for quarantine scan exclusion."""
    cleaned = str(raw_name or "").replace("\\", "/").strip().strip("/")
    if not cleaned:
        return ""
    return cleaned.split("/")[0].strip()


def _get_quarantine_ignore_prefixes(settings: Any = None) -> tuple[str, ...]:
    """Return quarantine directory prefixes that must never be scanned or re-sorted."""
    prefixes: list[str] = list(_DEFAULT_QUARANTINE_DIRS)
    if settings is not None:
        custom_q = _normalize_quarantine_dir_prefix(
            getattr(settings, "QUARANTINE_DIR_NAME", "")
        )
        if custom_q and custom_q not in prefixes:
            prefixes.append(custom_q)
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
    if name.startswith(("_Quarantine_Staging", "_Compliance_Quarantine")):
        return True
    return name in ignore_prefixes


def _norm_boundary_path(path_str: str) -> str:
    """Normalize a path for boundary-aware prefix comparison."""
    expanded = os.path.expanduser(str(path_str))
    return os.path.normcase(os.path.normpath(os.path.abspath(expanded)))


def _is_same_or_under(candidate_abs: str, parent_abs: str) -> bool:
    """Return True when candidate_abs equals parent_abs or resides beneath it."""
    cand = _norm_boundary_path(candidate_abs)
    par = _norm_boundary_path(parent_abs)
    if cand == par:
        return True
    prefix = par if par.endswith(os.sep) else par + os.sep
    if cand.startswith(prefix):
        return True
    try:
        if os.path.exists(candidate_abs) and os.path.exists(parent_abs):
            rc = os.path.normcase(os.path.realpath(candidate_abs))
            rp = os.path.normcase(os.path.realpath(parent_abs))
            if rc == rp:
                return True
            rprefix = rp if rp.endswith(os.sep) else rp + os.sep
            if rc.startswith(rprefix):
                return True
    except OSError:
        pass
    return False


class ScanFilter:
    """Unified path filter enforcing PROTECTED_PATHS, IGNORED_EXTENSIONS, .app bundles, quarantine, hidden files, and scan depth."""

    def __init__(
        self,
        protected_paths: Optional[list[str] | tuple[str, ...]] = None,
        ignored_extensions: Optional[list[str] | tuple[str, ...]] = None,
        quarantine_prefixes: Optional[tuple[str, ...]] = None,
        include_hidden: bool = False,
        max_depth: Optional[int] = None,
        base_dir: Optional[str] = None,
    ) -> None:
        self.base_dir = os.path.abspath(base_dir) if base_dir else None
        self.include_hidden = bool(include_hidden)
        self.max_depth = int(max_depth) if max_depth is not None else None
        self.quarantine_prefixes = (
            tuple(quarantine_prefixes)
            if quarantine_prefixes is not None
            else _get_quarantine_ignore_prefixes()
        )

        raw_exts = (
            DEFAULT_IGNORED_EXTENSIONS
            if ignored_extensions is None
            else ignored_extensions
        )
        norm_exts: list[str] = []
        for ext in raw_exts:
            if not isinstance(ext, str):
                continue
            cleaned = ext.strip().lower()
            if not cleaned:
                continue
            if not cleaned.startswith("."):
                cleaned = f".{cleaned}"
            if cleaned not in norm_exts:
                norm_exts.append(cleaned)
        self.ignored_extensions: tuple[str, ...] = tuple(norm_exts)

        resolved_protected: list[str] = []
        for raw_p in protected_paths or ():
            if not raw_p or not str(raw_p).strip():
                continue
            p_str = os.path.expanduser(str(raw_p).strip())
            if os.path.isabs(p_str):
                candidates = [os.path.abspath(p_str)]
            elif self.base_dir:
                candidates = [
                    os.path.abspath(os.path.join(self.base_dir, p_str)),
                    os.path.abspath(p_str),
                ]
            else:
                candidates = [os.path.abspath(p_str)]
            for cand in candidates:
                if cand not in resolved_protected:
                    resolved_protected.append(cand)
        self.protected_paths: tuple[str, ...] = tuple(resolved_protected)
        self._raw_protected_names: tuple[str, ...] = tuple(
            str(p).strip().strip("/\\")
            for p in (protected_paths or ())
            if p and not os.path.isabs(os.path.expanduser(str(p).strip()))
        )

    @classmethod
    def from_settings(
        cls,
        settings: Any = None,
        base_dir: Optional[str] = None,
        include_hidden: bool = False,
        max_depth: Optional[int] = None,
    ) -> "ScanFilter":
        """Build a ScanFilter from an AppSettings/Settings object or duck-typed mock."""
        prot: list[str] = []
        ignored_exts: Optional[list[str] | tuple[str, ...]] = None
        eff_max_depth = max_depth

        if settings is not None:
            raw_prot = getattr(settings, "PROTECTED_PATHS", None)
            if isinstance(raw_prot, (list, tuple, set)):
                prot = [str(p) for p in raw_prot if p]

            raw_exts = getattr(settings, "IGNORED_EXTENSIONS", None)
            if isinstance(raw_exts, (list, tuple, set)):
                ignored_exts = list(raw_exts)

            if eff_max_depth is None:
                cfg_depth = getattr(settings, "SCAN_MAX_DEPTH", None)
                if isinstance(cfg_depth, int) and not isinstance(cfg_depth, bool):
                    eff_max_depth = cfg_depth

        q_prefixes = _get_quarantine_ignore_prefixes(settings=settings)
        return cls(
            protected_paths=prot,
            ignored_extensions=ignored_exts,
            quarantine_prefixes=q_prefixes,
            include_hidden=include_hidden,
            max_depth=eff_max_depth,
            base_dir=base_dir,
        )

    @staticmethod
    def _all_segments(path_str: str) -> tuple[str, ...]:
        cleaned = str(path_str).replace("\\", "/").strip("/")
        return tuple(seg for seg in cleaned.split("/") if seg and seg != ".")

    def _scoped_relative_parts(
        self, path_str: str, base_dir: Optional[str] = None
    ) -> Optional[tuple[str, ...]]:
        """Return path segments relative to base_dir, or None when path_str is an external absolute path."""
        eff_base = os.path.abspath(base_dir) if base_dir else self.base_dir
        if not os.path.isabs(path_str):
            return self._all_segments(path_str)
        if eff_base and _is_same_or_under(path_str, eff_base):
            try:
                rel = os.path.relpath(os.path.abspath(path_str), eff_base)
            except ValueError:
                return None
            if rel in (".", ""):
                return ()
            return self._all_segments(rel)
        return None

    def is_protected(self, path_str: str, base_dir: Optional[str] = None) -> bool:
        """Return True when path_str is inside or equal to any configured PROTECTED_PATHS."""
        if not path_str:
            return False
        eff_base = os.path.abspath(base_dir) if base_dir else self.base_dir
        abs_cand = (
            os.path.abspath(os.path.expanduser(path_str))
            if os.path.isabs(os.path.expanduser(path_str)) or not eff_base
            else os.path.abspath(os.path.join(eff_base, path_str))
        )
        for prot in self.protected_paths:
            if _is_same_or_under(abs_cand, prot):
                return True
        if eff_base and self._raw_protected_names:
            for rel_name in self._raw_protected_names:
                prot_in_base = os.path.abspath(os.path.join(eff_base, rel_name))
                if _is_same_or_under(abs_cand, prot_in_base):
                    return True
        return False

    def is_app_bundle(self, path_str: str, base_dir: Optional[str] = None) -> bool:
        """Return True when path_str is a .app bundle directory or nested inside one."""
        if not path_str:
            return False
        scoped = self._scoped_relative_parts(path_str, base_dir=base_dir)
        parts = scoped if scoped is not None else self._all_segments(path_str)
        for idx, part in enumerate(parts):
            if part.lower().endswith(".app"):
                # Either an ancestor directory segment (idx < len - 1) or a .app directory/entry itself.
                if idx < len(parts) - 1:
                    return True
                eff_base = os.path.abspath(base_dir) if base_dir else self.base_dir
                abs_p = (
                    os.path.abspath(path_str)
                    if os.path.isabs(path_str) or not eff_base
                    else os.path.abspath(os.path.join(eff_base, path_str))
                )
                if not os.path.exists(abs_p) or os.path.isdir(abs_p):
                    return True
        return False

    def has_ignored_extension(self, path_str: str) -> bool:
        """Return True when path_str ends with one of IGNORED_EXTENSIONS."""
        if not path_str or not self.ignored_extensions:
            return False
        lower_name = os.path.basename(str(path_str).rstrip("/\\")).lower()
        return any(lower_name.endswith(ext) for ext in self.ignored_extensions)

    def is_quarantine(self, path_str: str, base_dir: Optional[str] = None) -> bool:
        """Return True when path_str belongs to a staging or compliance quarantine folder."""
        scoped = self._scoped_relative_parts(path_str, base_dir=base_dir)
        parts = scoped if scoped is not None else self._all_segments(path_str)
        return any(
            _is_quarantine_entry_name(part, self.quarantine_prefixes) for part in parts
        )

    def is_hidden(self, path_str: str, base_dir: Optional[str] = None) -> bool:
        """Return True when hidden files are excluded and any scoped segment starts with '.'."""
        if self.include_hidden:
            return False
        scoped = self._scoped_relative_parts(path_str, base_dir=base_dir)
        if scoped is not None:
            return any(
                part.startswith(".") and part not in (".", "..") for part in scoped
            )
        name = os.path.basename(str(path_str).rstrip("/\\"))
        return name.startswith(".") and name not in (".", "..")

    def exceeds_max_depth(
        self, rel_file_path: str, base_dir: Optional[str] = None
    ) -> bool:
        """Return True when file nesting depth inside base_dir exceeds max_depth (depth 0 = top-level file)."""
        if self.max_depth is None:
            return False
        scoped = self._scoped_relative_parts(rel_file_path, base_dir=base_dir)
        if not scoped:
            return False
        file_depth = max(0, len(scoped) - 1)
        return file_depth > self.max_depth

    def can_descend_dir(
        self,
        dir_abs_path: str,
        dir_rel_path: str,
        current_depth: int,
        base_dir: Optional[str] = None,
    ) -> bool:
        """Return True when scanner may descend into directory dir_rel_path at current_depth."""
        name = os.path.basename(dir_rel_path.rstrip("/\\"))
        if not self.include_hidden and name.startswith("."):
            return False
        if _is_quarantine_entry_name(name, self.quarantine_prefixes):
            return False
        if name.lower().endswith(".app") or self.is_app_bundle(
            dir_abs_path, base_dir=base_dir
        ):
            return False
        if self.is_protected(dir_abs_path, base_dir=base_dir):
            return False
        if self.max_depth is not None and current_depth >= self.max_depth:
            return False
        return True

    def check_path(
        self,
        path_str: str,
        base_dir: Optional[str] = None,
        check_depth: bool = False,
        check_hidden: bool = True,
    ) -> Optional[str]:
        """Return exclusion reason ('protected', 'app_bundle', 'quarantine', 'hidden', 'ignored_extension', 'max_depth') or None."""
        if not path_str:
            return "empty"
        if self.is_protected(path_str, base_dir=base_dir):
            return "protected"
        if self.is_app_bundle(path_str, base_dir=base_dir):
            return "app_bundle"
        if self.is_quarantine(path_str, base_dir=base_dir):
            return "quarantine"
        if check_hidden and self.is_hidden(path_str, base_dir=base_dir):
            return "hidden"
        if self.has_ignored_extension(path_str):
            return "ignored_extension"
        if check_depth and self.exceeds_max_depth(path_str, base_dir=base_dir):
            return "max_depth"
        return None


def discover_existing_folders(
    base_dir: Optional[str],
    dest_dir: Optional[str] = None,
    settings: Any = None,
    include_hidden: bool = False,
) -> list[str]:
    """Discover existing user folders on disk in base_dir and optional dest_dir.

    Skips hidden folders, quarantine folders, .app bundles, symlinks/junctions,
    and PROTECTED_PATHS so only legitimate user category directories are returned.
    """
    discovered_by_key: dict[str, str] = {}
    abs_base_norm = (
        _norm_boundary_path(str(base_dir))
        if base_dir and isinstance(base_dir, (str, os.PathLike))
        else None
    )
    # Order roots so dest_dir casing wins if both base_dir and dest_dir have same folder
    roots: list[str] = []
    for candidate in (base_dir, dest_dir):
        if candidate and isinstance(candidate, (str, os.PathLike)):
            abs_c = os.path.abspath(os.path.expanduser(str(candidate)))
            if os.path.isdir(abs_c) and abs_c not in roots:
                roots.append(abs_c)

    for root in roots:
        filt = ScanFilter.from_settings(
            settings=settings,
            base_dir=root,
            include_hidden=include_hidden,
            max_depth=1,
        )
        try:
            with os.scandir(root) as entries:
                for entry in sorted(entries, key=lambda e: e.name):
                    if not include_hidden and entry.name.startswith("."):
                        continue
                    if is_junction_entry(entry) or entry.is_symlink():
                        continue
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if (
                        abs_base_norm is not None
                        and _norm_boundary_path(entry.path) == abs_base_norm
                    ):
                        continue
                    if _is_quarantine_entry_name(entry.name, filt.quarantine_prefixes):
                        continue
                    if entry.name.lower().endswith(".app"):
                        continue
                    if filt.is_protected(entry.path, base_dir=root):
                        continue
                    norm_key = entry.name.lower()
                    discovered_by_key[norm_key] = entry.name
        except OSError as exc:
            logging.debug("Could not discover folders in %s: %s", root, exc)

    return sorted(discovered_by_key.values(), key=lambda s: (s.lower(), s))


def _scan_dir_recursive(
    base: str,
    rel_path: str,
    scan_filter: ScanFilter,
    current_depth: int = 0,
) -> list[str]:
    """Walk a directory deterministically while enforcing ScanFilter rules and depth limits."""
    files: list[str] = []
    try:
        scan_target = os.path.join(base, rel_path) if rel_path else base
        if scan_filter.is_protected(
            scan_target, base_dir=base
        ) or scan_filter.is_app_bundle(scan_target, base_dir=base):
            return files

        with os.scandir(scan_target) as entries:
            sorted_entries = sorted(entries, key=lambda e: e.name)
            for entry in sorted_entries:
                if not scan_filter.include_hidden and entry.name.startswith("."):
                    continue
                if _is_quarantine_entry_name(
                    entry.name, scan_filter.quarantine_prefixes
                ):
                    continue
                entry_rel_path = (
                    os.path.join(rel_path, entry.name) if rel_path else entry.name
                )
                entry_abs_path = entry.path

                if scan_filter.is_protected(entry_abs_path, base_dir=base):
                    continue

                # Check for junction, symlink, or .lnk file
                if (
                    is_junction_entry(entry)
                    or entry.is_symlink()
                    or entry.name.lower().endswith(".lnk")
                ):
                    if entry.name.lower().endswith(
                        ".app"
                    ) or scan_filter.has_ignored_extension(entry.name):
                        continue
                    LinkManager.register_link(base, entry_rel_path)
                    files.append(entry_rel_path)
                elif entry.is_dir(follow_symlinks=False):
                    if scan_filter.can_descend_dir(
                        entry_abs_path,
                        entry_rel_path,
                        current_depth=current_depth,
                        base_dir=base,
                    ):
                        files.extend(
                            _scan_dir_recursive(
                                base,
                                entry_rel_path,
                                scan_filter=scan_filter,
                                current_depth=current_depth + 1,
                            )
                        )
                else:
                    if scan_filter.has_ignored_extension(entry.name):
                        continue
                    files.append(entry_rel_path)
    except Exception as e:
        logging.error(
            f"Failed to scan directory {os.path.join(base, rel_path)}: {e}",
            exc_info=True,
        )
    return files


def get_files_recursively(
    base: str,
    rel_path: str = "",
    include_hidden: bool = False,
    max_depth: Optional[int] = None,
    settings: Any = None,
    scan_filter: Optional[ScanFilter] = None,
) -> list:
    """List files in a directory deterministically while honoring ScanFilter and max_depth."""
    if rel_path == "":
        LinkManager.clear()

    ctx_filter = (
        _ACTIVE_SCAN_FILTER.get()
        if scan_filter is None
        and settings is None
        and max_depth is None
        and not include_hidden
        else None
    )
    active_filter = (
        scan_filter
        or ctx_filter
        or ScanFilter.from_settings(
            settings=settings,
            base_dir=base,
            include_hidden=include_hidden,
            max_depth=max_depth,
        )
    )
    if rel_path:
        first_seg = rel_path.replace("\\", "/").strip("/").split("/")[0]
        if _is_quarantine_entry_name(first_seg, active_filter.quarantine_prefixes):
            return []
        if active_filter.is_protected(
            os.path.join(base, rel_path), base_dir=base
        ) or active_filter.is_app_bundle(os.path.join(base, rel_path), base_dir=base):
            return []

    start_depth = (
        len([s for s in rel_path.replace("\\", "/").strip("/").split("/") if s])
        if rel_path
        else 0
    )
    files = _scan_dir_recursive(
        base,
        rel_path,
        scan_filter=active_filter,
        current_depth=start_depth,
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
    source_dirs: list[str],
    include_hidden: bool = False,
    max_depth: Optional[int] = None,
    settings: Any = None,
    scan_filter: Optional[ScanFilter] = None,
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
        return get_files_recursively(
            norm_sources[0],
            include_hidden=include_hidden,
            max_depth=max_depth,
            settings=settings,
            scan_filter=scan_filter,
        )

    all_files = []
    for src_root in norm_sources:
        scanned = get_files_recursively(
            src_root,
            include_hidden=include_hidden,
            max_depth=max_depth,
            settings=settings,
            scan_filter=scan_filter,
        )
        for rel_path in scanned:
            all_files.append(f"{src_root}::{rel_path}")

    return sorted(all_files)
