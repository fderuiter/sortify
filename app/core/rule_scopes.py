"""Central folder-scoped rule resolution, profile paths, and scoped settings views."""

from __future__ import annotations

import copy
import ntpath
import os
import re
from pathlib import Path
from typing import Any

from app.core.policy_engine import canonicalize_condition_node

FOLDER_SCOPE_RULE_KEYS: tuple[str, ...] = (
    "KEYWORD_RULES",
    "DISABLED_KEYWORD_RULES",
    "LEARNED_RULES",
    "DISABLED_LEARNED_RULES",
    "EXTENSION_CATEGORIES",
    "POLICIES",
)

_WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def normalize_folder_scope_key(folder_path: str | os.PathLike[str]) -> str:
    """Canonicalize a folder path for lookup and storage in ``FOLDER_RULE_SCOPES``."""
    if not isinstance(folder_path, (str, os.PathLike)):
        raise ValueError("Folder scope key must be a path string.")
    raw = os.fspath(folder_path).strip()
    if not raw:
        raise ValueError("Folder scope key cannot be empty.")
    if "\x00" in raw:
        raise ValueError("Folder scope key cannot contain null bytes.")

    # Preserve foreign Windows drive/UNC paths when loading cross-platform backups on POSIX
    if os.name != "nt" and (
        _WINDOWS_DRIVE_PREFIX_RE.match(raw) or raw.startswith("\\\\")
    ):
        norm_win = ntpath.normpath(raw).lower()
        if len(norm_win) > 3:
            norm_win = norm_win.rstrip("\\/")
        return norm_win

    expanded = os.path.expanduser(raw)
    resolved = os.path.realpath(os.path.abspath(expanded))
    return os.path.normcase(os.path.normpath(resolved))


def resolve_profile_settings_path(
    profile_name: str,
    app_dir: Path | None = None,
) -> Path:
    """Resolve ``~/.autosorter/profiles/<name>.json`` without mutating ``get_app_dir()``."""
    if not isinstance(profile_name, str):
        raise ValueError("Profile name must be a non-empty string.")
    cleaned = profile_name.strip()
    if not cleaned:
        raise ValueError("Profile name cannot be empty.")
    if "\x00" in cleaned:
        raise ValueError("Profile name cannot contain null bytes.")
    if (
        "/" in cleaned
        or "\\" in cleaned
        or ":" in cleaned
        or ".." in cleaned
        or cleaned in (".", "..")
    ):
        raise ValueError(
            f"Invalid profile name '{profile_name}': path separators and traversal segments are forbidden."
        )
    stem = cleaned[:-5].strip() if cleaned.lower().endswith(".json") else cleaned
    if not stem or stem in (".", ".."):
        raise ValueError(f"Invalid profile name '{profile_name}'.")
    if app_dir is None:
        from app.config import _resolve_app_dir

        base_app_dir = _resolve_app_dir()
    else:
        base_app_dir = app_dir
    return base_app_dir / "profiles" / f"{stem}.json"


def resolve_cli_settings_filepath(
    settings_file: str | None = None,
    profile: str | None = None,
    app_dir: Path | None = None,
) -> str | None:
    """Resolve an explicit ``--settings-file`` or ``--profile`` CLI argument to a file path."""
    sf_clean = str(settings_file).strip() if settings_file is not None else ""
    prof_clean = str(profile).strip() if profile is not None else ""

    if sf_clean and prof_clean:
        raise ValueError(
            "Cannot specify both --settings-file and --profile at the same time."
        )
    if sf_clean:
        if "\x00" in sf_clean:
            raise ValueError("Settings file path cannot contain null bytes.")
        resolved_sf = os.path.abspath(os.path.expanduser(sf_clean))
        if os.path.exists(resolved_sf) and os.path.isdir(resolved_sf):
            raise ValueError(
                f"Settings file path '{settings_file}' points to an existing directory."
            )
        return resolved_sf
    if prof_clean:
        return str(resolve_profile_settings_path(prof_clean, app_dir=app_dir))
    return None


def _policy_to_dict(pol: Any) -> dict[str, Any]:
    """Convert a policy dictionary or Pydantic object into a plain mutable dict."""
    if isinstance(pol, dict):
        return copy.deepcopy(pol)
    if hasattr(pol, "model_dump"):
        return dict(pol.model_dump())
    return dict(pol)


def _policy_identity_key(pol: Any) -> tuple[Any, ...]:
    """Build a canonical deduplication/override key for single and compound policies."""
    d = pol if isinstance(pol, dict) else _policy_to_dict(pol)
    p_type = str(d.get("type", "") or "").strip().lower()
    p_act = str(d.get("action", "") or "").strip().lower()
    p_mode = str(d.get("match_mode", "all") or "all").strip().lower()
    p_neg = bool(d.get("negate", False))
    p_regex = bool(d.get("regex", False))
    p_case = bool(d.get("case_sensitive", False))
    raw_conds = d.get("conditions")
    if isinstance(raw_conds, list) and raw_conds:
        cond_sig = tuple(
            sorted(
                canonicalize_condition_node(c, default_case=p_case)
                for c in raw_conds
                if isinstance(c, dict)
            )
        )
        return ("compound", p_type, p_act, p_mode, p_neg, cond_sig)
    raw_expr = str(d.get("expression", "") or "").strip()
    expr_sig = raw_expr if (p_case or p_regex) else raw_expr.lower()
    return ("single", p_type, p_act, p_mode, p_neg, p_regex, p_case, expr_sig)


class ScopedSettingsView:
    """Folder-scoped view over an :class:`AppSettings` (or ``Settings``) object.

    Reads and writes ``KEYWORD_RULES``, ``DISABLED_KEYWORD_RULES``,
    ``LEARNED_RULES``, ``DISABLED_LEARNED_RULES``, ``EXTENSION_CATEGORIES``, and
    ``POLICIES`` within ``FOLDER_RULE_SCOPES[scope_key]``, while delegating all
    other settings attributes and persistence methods to ``root_settings``.
    """

    _INTERNAL_ATTRS = frozenset(
        {
            "_root_settings",
            "_folder_path",
            "_scope_key",
            "_fallback_to_global",
        }
    )

    def __init__(
        self,
        root_settings: Any,
        folder_path: str | os.PathLike[str],
        *,
        fallback_to_global: bool = True,
    ) -> None:
        unwrapped = getattr(root_settings, "root_settings", root_settings)
        object.__setattr__(self, "_root_settings", unwrapped)
        raw_path = os.fspath(folder_path).strip()
        object.__setattr__(self, "_folder_path", raw_path)
        object.__setattr__(self, "_scope_key", normalize_folder_scope_key(raw_path))
        object.__setattr__(self, "_fallback_to_global", bool(fallback_to_global))

    @property
    def root_settings(self) -> Any:
        """Return the underlying root ``AppSettings`` instance."""
        return self._root_settings

    @property
    def folder_path(self) -> str:
        """Return the raw folder path string bound to this scoped view."""
        return self._folder_path

    @property
    def scope_key(self) -> str:
        """Return the canonicalized folder key used in ``FOLDER_RULE_SCOPES``."""
        return self._scope_key

    @property
    def fallback_to_global(self) -> bool:
        """Return whether global rule tables are included as baseline defaults."""
        return self._fallback_to_global

    @property
    def filepath(self) -> str | None:
        """Return the underlying configuration file path, if available."""
        return getattr(
            self._root_settings,
            "filepath",
            getattr(self._root_settings, "_filepath", None),
        )

    def for_folder(
        self,
        folder_path: str | os.PathLike[str] | None,
        *,
        fallback_to_global: bool = True,
    ) -> Any:
        """Resolve another folder scope against the underlying root settings."""
        if folder_path is None:
            return self._root_settings
        raw = os.fspath(folder_path).strip()
        if not raw:
            return self._root_settings
        return ScopedSettingsView(
            self._root_settings,
            raw,
            fallback_to_global=fallback_to_global,
        )

    def _get_raw_scope_entry(self) -> dict[str, Any]:
        raw_scopes = getattr(self._root_settings, "FOLDER_RULE_SCOPES", None)
        if not isinstance(raw_scopes, dict):
            return {}
        if self._scope_key in raw_scopes and isinstance(
            raw_scopes[self._scope_key], dict
        ):
            return raw_scopes[self._scope_key]
        for k, v in raw_scopes.items():
            if not isinstance(v, dict):
                continue
            try:
                if normalize_folder_scope_key(str(k)) == self._scope_key:
                    return v
            except ValueError:
                continue
        return {}

    def _read_scoped_rule_table(self, name: str) -> Any:
        scope_entry = self._get_raw_scope_entry()
        if not self._fallback_to_global:
            if name == "POLICIES":
                return [
                    _policy_to_dict(p) for p in (scope_entry.get("POLICIES", []) or [])
                ]
            return copy.deepcopy(dict(scope_entry.get(name, {}) or {}))

        if name in ("KEYWORD_RULES", "DISABLED_KEYWORD_RULES"):
            active_key = "KEYWORD_RULES"
            disabled_key = "DISABLED_KEYWORD_RULES"
            g_act = dict(getattr(self._root_settings, active_key, {}) or {})
            g_dis = dict(getattr(self._root_settings, disabled_key, {}) or {})
            s_act = dict(scope_entry.get(active_key, {}) or {})
            s_dis = dict(scope_entry.get(disabled_key, {}) or {})
            if name == active_key:
                for k in s_dis:
                    g_act.pop(k, None)
                g_act.update(s_act)
                return g_act
            for k in s_act:
                g_dis.pop(k, None)
            g_dis.update(s_dis)
            return g_dis

        if name in ("LEARNED_RULES", "DISABLED_LEARNED_RULES"):
            active_key = "LEARNED_RULES"
            disabled_key = "DISABLED_LEARNED_RULES"
            g_act = dict(getattr(self._root_settings, active_key, {}) or {})
            g_dis = dict(getattr(self._root_settings, disabled_key, {}) or {})
            s_act = dict(scope_entry.get(active_key, {}) or {})
            s_dis = dict(scope_entry.get(disabled_key, {}) or {})
            if name == active_key:
                for k in s_dis:
                    g_act.pop(k, None)
                g_act.update(s_act)
                return g_act
            for k in s_act:
                g_dis.pop(k, None)
            g_dis.update(s_dis)
            return g_dis

        if name == "EXTENSION_CATEGORIES":
            merged_ext = copy.deepcopy(
                dict(getattr(self._root_settings, "EXTENSION_CATEGORIES", {}) or {})
            )
            merged_ext.update(
                copy.deepcopy(dict(scope_entry.get("EXTENSION_CATEGORIES", {}) or {}))
            )
            return merged_ext

        # POLICIES
        g_pols = [
            _policy_to_dict(p)
            for p in (getattr(self._root_settings, "POLICIES", []) or [])
        ]
        s_pols = [_policy_to_dict(p) for p in (scope_entry.get("POLICIES", []) or [])]
        if not s_pols:
            return g_pols
        scoped_identities = {_policy_identity_key(p) for p in s_pols}
        merged_pols = [
            p for p in g_pols if _policy_identity_key(p) not in scoped_identities
        ]
        merged_pols.extend(s_pols)
        return merged_pols

    def _write_scoped_rule_table(self, name: str, value: Any) -> None:
        raw_scopes = getattr(self._root_settings, "FOLDER_RULE_SCOPES", None)
        all_scopes: dict[str, dict[str, Any]] = {}
        if isinstance(raw_scopes, dict):
            for k, v in raw_scopes.items():
                if isinstance(v, dict):
                    try:
                        nk = normalize_folder_scope_key(str(k))
                    except ValueError:
                        nk = str(k)
                    all_scopes[nk] = copy.deepcopy(v)

        curr_scope = dict(all_scopes.get(self._scope_key, {}) or {})

        if name == "POLICIES":
            if not isinstance(value, list):
                raise ValueError("POLICIES must be a list.")
            incoming_pols = [_policy_to_dict(p) for p in value]
            if self._fallback_to_global:
                g_pols = [
                    _policy_to_dict(p)
                    for p in (getattr(self._root_settings, "POLICIES", []) or [])
                ]
                prev_scoped_ids = {
                    _policy_identity_key(p)
                    for p in (curr_scope.get("POLICIES", []) or [])
                }
                g_by_id = {_policy_identity_key(p): p for p in g_pols}
                filtered_pols = [
                    p
                    for p in incoming_pols
                    if _policy_identity_key(p) in prev_scoped_ids
                    or g_by_id.get(_policy_identity_key(p)) != p
                ]
                if filtered_pols:
                    curr_scope["POLICIES"] = filtered_pols
                else:
                    curr_scope.pop("POLICIES", None)
            else:
                if incoming_pols:
                    curr_scope["POLICIES"] = incoming_pols
                else:
                    curr_scope.pop("POLICIES", None)
        else:
            if not isinstance(value, dict):
                raise ValueError(f"{name} must be a dictionary.")
            incoming_map = copy.deepcopy(dict(value))
            if self._fallback_to_global:
                global_map = dict(getattr(self._root_settings, name, {}) or {})
                prev_scoped_map = dict(curr_scope.get(name, {}) or {})
                filtered_map = {
                    k: v
                    for k, v in incoming_map.items()
                    if k in prev_scoped_map or global_map.get(k) != v
                }
                if filtered_map:
                    curr_scope[name] = filtered_map
                else:
                    curr_scope.pop(name, None)
            else:
                if incoming_map:
                    curr_scope[name] = incoming_map
                else:
                    curr_scope.pop(name, None)

        if curr_scope:
            all_scopes[self._scope_key] = curr_scope
        else:
            all_scopes.pop(self._scope_key, None)

        setattr(self._root_settings, "FOLDER_RULE_SCOPES", all_scopes)

    def __getattr__(self, name: str) -> Any:
        """Delegate rule table reads to the folder scope and other attributes to root settings."""
        if name in FOLDER_SCOPE_RULE_KEYS:
            return self._read_scoped_rule_table(name)
        return getattr(self._root_settings, name)

    def __setattr__(self, name: str, value: Any) -> None:
        """Persist rule table assignments into the folder scope and delegate other attributes."""
        if name in self._INTERNAL_ATTRS:
            object.__setattr__(self, name, value)
            return
        if name in FOLDER_SCOPE_RULE_KEYS:
            self._write_scoped_rule_table(name, value)
            return
        setattr(self._root_settings, name, value)


def resolve_scoped_settings(
    settings: Any,
    folder_path: str | os.PathLike[str] | None,
    *,
    fallback_to_global: bool = True,
) -> Any:
    """Resolve a folder-scoped settings view when ``settings`` supports scoping."""
    if settings is None or folder_path is None:
        return settings
    raw = os.fspath(folder_path).strip()
    if not raw:
        return settings
    if callable(getattr(type(settings), "for_folder", None)):
        return settings.for_folder(raw, fallback_to_global=fallback_to_global)
    if isinstance(getattr(settings, "FOLDER_RULE_SCOPES", None), dict):
        return ScopedSettingsView(
            settings,
            raw,
            fallback_to_global=fallback_to_global,
        )
    return settings
