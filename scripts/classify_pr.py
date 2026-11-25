"""PR Risk Classification Engine for Sortify Auto-Merge System.

Evaluates pull request metadata (file paths, diff size, author, draft state, labels)
and determines risk tier and auto-merge eligibility without executing untrusted PR code.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass, field
from typing import Any

# Paths that are never auto-merged and need the owner's approval (ADR 0009).
# Written in CODEOWNERS syntax: each entry must also appear in
# .github/CODEOWNERS, which tests/test_risk_classifier.py checks.
PROTECTED_PATHS = [
    ".github/",
    "SECURITY.md",
    "PRIVACY.md",
    "app/config.py",
    "app/core/crypto.py",
    # Modules that can lose or corrupt a user's files (ADR 0005, ADR 0006, ADR 0008)
    "app/core/mover.py",
    "app/core/moves.py",
    "app/core/resilient_file_ops.py",
    "app/core/ledger.py",
    "app/core/history.py",
    "app/core/session.py",
    "app/core/db.py",
    "app/core/db_conn.py",
    "app/core/db_worker.py",
    "app/core/store.py",
    "app/core/quarantine_interceptor.py",
    "scripts/",
    "smart-autosorter.spec",
    "network_rules.json",
    ".gitleaks.toml",
    "mkdocs.yml",
    ".pre-commit-config.yaml",
    ".claude/",
    ".agents/",
    "AGENTS.md",
    "CLAUDE.md",
    ".gitattributes",
    "offline_bundle/requirements.txt",
]


def _protected_path_regex(pattern: str) -> str:
    """Translate a PROTECTED_PATHS entry into an anchored regex for a repo path."""
    if pattern.endswith("/"):
        return f"^{re.escape(pattern)}.*"
    return f"^{re.escape(pattern)}$"


SENSITIVE_PATHS = [_protected_path_regex(p) for p in PROTECTED_PATHS]

# Dependency manifests are sensitive unless the PR comes from Dependabot itself
# (callers must set is_dependabot from the exact "dependabot[bot]" login, not
# from a label), so routine Dependabot bumps can still be auto-merged.
DEPENDENCY_MANIFEST_PATHS = [
    r"^pyproject\.toml$",
    r"^uv\.lock$",
    r"^offline_bundle/requirements\.txt$",
]

BASELINE_PATHS = [
    r"^tests/snapshots/api/.*",
    r"^tests/snapshots/tui_svg/.*",
    r"^tests/ty_baseline\.json$",
]

DOCS_PATHS = [
    r"^docs/.*",
    r"^README\.md$",
]

MAINTAINER_PERMISSIONS = frozenset({"admin", "maintain", "write"})

_DEPENDABOT_VERSION_RE = re.compile(
    r"\bfrom\s+v?(?P<old>\d+(?:\.\d+)*)\S*\s+to\s+v?(?P<new>\d+(?:\.\d+)*)",
    re.IGNORECASE,
)

_PEP440_VERSION_RE = re.compile(
    r"^v?(?:(?P<epoch>\d+)!)?(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-._]?(?P<pre_l>a|alpha|b|beta|c|rc|pre|preview)[-._]?(?P<pre_n>\d*))?"
    r"(?:(?:-(?P<post_n1>\d+))|(?:[-._]?(?P<post_l>post|rev|r)[-._]?(?P<post_n2>\d*)))?"
    r"(?:[-._]?dev[-._]?(?P<dev_n>\d*))?"
    r"(?:\+(?P<local>[a-zA-Z0-9]+(?:[-._][a-zA-Z0-9]+)*))?$",
    re.IGNORECASE,
)

_REQ_PIN_RE = re.compile(
    r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?:\[[^\]]*\])?==([^\s;]+)$"
)

_PRE_RELEASE_RANK = {
    "a": 0,
    "alpha": 0,
    "b": 1,
    "beta": 1,
    "c": 2,
    "rc": 2,
    "pre": 2,
    "preview": 2,
}


def _canonical_pkg_name(name: str) -> str:
    """Normalize a Python distribution name per PEP 503."""
    return re.sub(r"[-_.]+", "-", name.strip().lower()).strip("-")


def _parse_pep440_version(
    version_str: str,
) -> tuple[int, tuple[int, ...], tuple[Any, ...], bool, str] | None:
    """Parse a PEP 440 version string into components and a comparison key."""
    cleaned = version_str.strip()
    if not cleaned:
        return None
    match = _PEP440_VERSION_RE.match(cleaned)
    if not match:
        return None

    epoch = int(match.group("epoch") or 0)
    release = tuple(int(part) for part in match.group("release").split("."))
    trimmed = list(release)
    while len(trimmed) > 1 and trimmed[-1] == 0:
        trimmed.pop()
    rel_key = tuple(trimmed)

    pre_l = match.group("pre_l")
    pre_n = match.group("pre_n")
    post_n1 = match.group("post_n1")
    post_l = match.group("post_l")
    post_n2 = match.group("post_n2")
    dev_m = match.group("dev_n")
    has_dev = dev_m is not None
    has_post = post_n1 is not None or post_l is not None

    if pre_l is None and not has_post and has_dev:
        pre_key: tuple[int, ...] = (-1,)
    elif pre_l is not None:
        pre_key = (0, _PRE_RELEASE_RANK[pre_l.lower()], int(pre_n or 0))
    else:
        pre_key = (1,)

    if has_post:
        post_num = int(post_n1) if post_n1 is not None else int(post_n2 or 0)
        post_key: tuple[int, ...] = (1, post_num)
    else:
        post_key = (-1,)

    dev_key: tuple[int, ...] = (0, int(dev_m or 0)) if has_dev else (1,)
    local_tag = (match.group("local") or "").lower()
    has_pre_or_dev = pre_l is not None or has_dev
    cmp_key = (epoch, rel_key, pre_key, post_key, dev_key, local_tag)
    return epoch, release, cmp_key, has_pre_or_dev, local_tag


def classify_version_bump(old_version: str, new_version: str) -> str:
    """Classify a single package version transition as 'minor' or 'major'.

    Returns 'major' for unparseable versions, epoch changes, major upgrades,
    initial zero-major (0.x) minor upgrades, version downgrades, transitions
    from stable to pre/dev releases, or local build tag changes.
    """
    old_parsed = _parse_pep440_version(old_version)
    new_parsed = _parse_pep440_version(new_version)
    if old_parsed is None or new_parsed is None:
        return "major"

    old_epoch, old_rel, old_cmp, old_pre_dev, old_local = old_parsed
    new_epoch, new_rel, new_cmp, new_pre_dev, new_local = new_parsed

    if old_epoch != new_epoch:
        return "major"
    if old_local != new_local:
        return "major"
    if new_pre_dev and not old_pre_dev:
        return "major"
    if new_cmp < old_cmp:
        return "major"

    old_major = old_rel[0]
    new_major = new_rel[0]
    if old_major != new_major:
        return "major"

    old_minor = old_rel[1] if len(old_rel) > 1 else 0
    new_minor = new_rel[1] if len(new_rel) > 1 else 0
    if old_major == 0 and old_minor != new_minor:
        return "major"

    return "minor"


def _parse_uv_lock_packages(
    content: str,
) -> dict[tuple[str, tuple[tuple[str, str], ...]], list[str]] | None:
    """Parse package entries from uv.lock keyed by (canonical_name, source_items)."""
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError:
        return None

    packages = data.get("package")
    if not isinstance(packages, list) or not packages:
        return None

    grouped: dict[tuple[str, tuple[tuple[str, str], ...]], list[str]] = {}
    for pkg in packages:
        if not isinstance(pkg, dict):
            return None
        raw_name = pkg.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return None
        raw_source = pkg.get("source")
        source_key: tuple[tuple[str, str], ...] = ()
        if isinstance(raw_source, dict):
            source_key = tuple(sorted((str(k), str(v)) for k, v in raw_source.items()))
            if raw_source.get("virtual") == "." and "version" not in pkg:
                continue
        elif raw_source is not None:
            return None

        raw_version = pkg.get("version")
        if not isinstance(raw_version, str) or not raw_version.strip():
            return None
        if _parse_pep440_version(raw_version) is None:
            return None

        key = (_canonical_pkg_name(raw_name), source_key)
        grouped.setdefault(key, []).append(raw_version.strip())

    if not grouped:
        return None

    for key, versions in grouped.items():
        versions.sort(
            key=lambda v: (_parse_pep440_version(v) or (0, (), (), False, ""))[2]
        )
        grouped[key] = versions

    return grouped


def _diff_uv_lock(base_content: str, head_content: str) -> tuple[str, int]:
    """Compare base and head uv.lock contents and return (update_type, changed_count)."""
    base_pkgs = _parse_uv_lock_packages(base_content)
    head_pkgs = _parse_uv_lock_packages(head_content)
    if base_pkgs is None or head_pkgs is None:
        return "major", 0

    if set(base_pkgs.keys()) != set(head_pkgs.keys()):
        return "major", 0

    changed_count = 0
    for key, base_versions in base_pkgs.items():
        head_versions = head_pkgs[key]
        if len(base_versions) != len(head_versions):
            return "major", 0
        for old_v, new_v in zip(base_versions, head_versions):
            if old_v == new_v:
                continue
            changed_count += 1
            if classify_version_bump(old_v, new_v) == "major":
                return "major", changed_count

    if changed_count == 0 and base_content != head_content:
        return "major", 0
    return "minor", changed_count


def classify_uv_lock_diff(base_content: str, head_content: str) -> str:
    """Classify package version diffs between base and head uv.lock as 'minor' or 'major'."""
    return _diff_uv_lock(base_content, head_content)[0]


def _parse_requirements_manifest(content: str) -> dict[tuple[str, str], str] | None:
    """Parse pinned entries from offline_bundle/requirements.txt keyed by (name, marker)."""
    logical_lines: list[str] = []
    current_parts: list[str] = []

    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        has_continuation = raw_line.rstrip().endswith("\\")
        part = stripped[:-1].strip() if stripped.endswith("\\") else stripped
        if part:
            current_parts.append(part)
        if not has_continuation and current_parts:
            logical_lines.append(" ".join(current_parts))
            current_parts = []

    if current_parts:
        logical_lines.append(" ".join(current_parts))

    if not logical_lines:
        return None

    entries: dict[tuple[str, str], str] = {}
    for entry in logical_lines:
        if entry.startswith("-"):
            return None
        parts = re.split(r"\s+(?=--)", entry)
        req_and_marker = parts[0].strip()
        for opt in parts[1:]:
            if not opt.strip().startswith("--hash="):
                return None

        if ";" in req_and_marker:
            spec_raw, marker_raw = req_and_marker.split(";", maxsplit=1)
            spec_str = spec_raw.strip()
            marker_str = " ".join(marker_raw.split())
        else:
            spec_str = req_and_marker
            marker_str = ""

        pin_match = _REQ_PIN_RE.match(spec_str)
        if not pin_match:
            return None

        pkg_name = _canonical_pkg_name(pin_match.group(1))
        version_str = pin_match.group(2).strip()
        if _parse_pep440_version(version_str) is None:
            return None

        key = (pkg_name, marker_str)
        if key in entries:
            return None
        entries[key] = version_str

    return entries


def _diff_requirements_txt(base_content: str, head_content: str) -> tuple[str, int]:
    """Compare base and head requirements.txt contents and return (update_type, changed_count)."""
    base_reqs = _parse_requirements_manifest(base_content)
    head_reqs = _parse_requirements_manifest(head_content)
    if base_reqs is None or head_reqs is None:
        return "major", 0

    if set(base_reqs.keys()) != set(head_reqs.keys()):
        return "major", 0

    changed_count = 0
    for key, old_v in base_reqs.items():
        new_v = head_reqs[key]
        if old_v == new_v:
            continue
        changed_count += 1
        if classify_version_bump(old_v, new_v) == "major":
            return "major", changed_count

    if changed_count == 0 and base_content != head_content:
        return "major", 0
    return "minor", changed_count


def classify_requirements_diff(base_content: str, head_content: str) -> str:
    """Classify package version diffs in offline_bundle/requirements.txt as 'minor' or 'major'."""
    return _diff_requirements_txt(base_content, head_content)[0]


def dependency_update_type_from_diffs(
    changed_files: list[str] | tuple[str, ...] = (),
    *,
    base_uv_lock: str | None = None,
    head_uv_lock: str | None = None,
    base_requirements_txt: str | None = None,
    head_requirements_txt: str | None = None,
) -> str:
    """Evaluate dependency update risk across uv.lock and offline_bundle/requirements.txt.

    Compares base and head package versions for every changed lockfile/manifest,
    blocking major upgrades, initial 0.x minor upgrades, added/removed packages,
    missing base/head contents, and unparseable version changes as 'major'.
    """
    normalized_files = {
        path.strip().replace("\\", "/") for path in changed_files if path.strip()
    }
    check_uv = "uv.lock" in normalized_files or (
        not normalized_files and (base_uv_lock is not None or head_uv_lock is not None)
    )
    check_req = "offline_bundle/requirements.txt" in normalized_files or (
        not normalized_files
        and (base_requirements_txt is not None or head_requirements_txt is not None)
    )

    if not check_uv and not check_req:
        return "major"

    total_changed = 0
    if check_uv:
        if base_uv_lock is None or head_uv_lock is None:
            return "major"
        uv_type, uv_changed = _diff_uv_lock(base_uv_lock, head_uv_lock)
        if uv_type == "major":
            return "major"
        total_changed += uv_changed

    if check_req:
        if base_requirements_txt is None or head_requirements_txt is None:
            return "major"
        req_type, req_changed = _diff_requirements_txt(
            base_requirements_txt, head_requirements_txt
        )
        if req_type == "major":
            return "major"
        total_changed += req_changed

    if total_changed == 0:
        return "major"
    return "minor"


def dependency_update_type_from_title(title: str) -> str:
    """Classify a Dependabot title ("Bump x from 1.2.3 to 2.0.0") as major or minor.

    A change in the first version component is major, and so is a change in the
    second while the first is 0, as semver treats 0.x minors as breaking. Titles
    without a single "from A to B" (grouped updates) count as major, so a person
    reviews them when lockfile diffs and Dependabot metadata are unavailable.
    """
    matches = list(_DEPENDABOT_VERSION_RE.finditer(title))
    if len(matches) != 1:
        return "major"
    old = [int(part) for part in matches[0].group("old").split(".")]
    new = [int(part) for part in matches[0].group("new").split(".")]
    if old[0] != new[0]:
        return "major"
    if old[0] == 0 and (old[1:2] or [0]) != (new[1:2] or [0]):
        return "major"
    return "minor"


_HTML_DETAILS_OR_QUOTE_RE = re.compile(
    r"<(?P<tag>details|blockquote)\b[^>]*>[\s\S]*?</(?P=tag)>",
    re.IGNORECASE,
)

_DEPENDABOT_YAML_HEADER_RE = re.compile(
    r"(?:^|\r?\n)---[ \t]*\r?\nupdated-dependencies:[ \t]*\r?\n",
    re.IGNORECASE,
)

_DEPENDABOT_UPDATES_LINE_RE = re.compile(
    r"^[ \t]*Updates\s+`(?P<name>[^`\r\n]+)`\s+from\s+v?(?P<old>[^\s`]+)\s+to\s+v?(?P<new>[^\s`]+?)[ \t]*\.?[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)

_DEPENDABOT_BUMPS_LINE_RE = re.compile(
    r"^[ \t]*Bumps\s+(?:\[(?P<link_name>[^\]\r\n]+)\]\([^)\r\n]*\)|`(?P<code_name>[^`\r\n]+)`)\s+"
    r"from\s+v?(?P<old>[^\s`]+)\s+to\s+v?(?P<new>[^\s`]+?)[ \t]*\.?[ \t]*$",
    re.MULTILINE | re.IGNORECASE,
)

_DEPENDABOT_TABLE_ROW_RE = re.compile(
    r"^[ \t]*\|\s*(?:\[(?P<link_name>[^\]|\r\n]+)\]\([^)\r\n]*\)|`(?P<code_name>[^`\r\n]+)`)\s*\|\s*"
    r"`?v?(?P<old>\d[^\s|`]*?)`?\s*\|\s*`?v?(?P<new>\d[^\s|`]*?)`?\s*\|[ \t]*$",
    re.MULTILINE,
)

_DEPENDABOT_GROUP_COUNT_RE = re.compile(
    r"\b(?:bump|bumps)\s+the\s+\S+\s+group\b[^\r\n]*?\bwith\s+(?P<count>\d+)\s+updates?\b",
    re.IGNORECASE,
)

_MERGE_COMMIT_SUBJECT_RE = re.compile(
    r"^Merge\s+(?:branch|remote-tracking\s+branch|commit)\b",
    re.IGNORECASE,
)


def _normalize_dep_identifier(name: str) -> str:
    """Normalize a dependency name (preserving ecosystem slashes when present)."""
    base = name.split("[", 1)[0].strip().lower()
    if "/" in base:
        return "/".join(_canonical_pkg_name(part) for part in base.split("/"))
    return _canonical_pkg_name(base)


def _unquote_yaml_scalar(raw_value: str) -> str | None:
    """Strip optional matching single or double quotes from a simple YAML scalar."""
    val = raw_value.strip()
    if not val:
        return ""
    if val[0] in ("'", '"'):
        if len(val) < 2 or val[-1] != val[0]:
            return None
        return val[1:-1].strip()
    if any(q in val for q in ("'", '"')):
        return None
    return val


def parse_dependabot_updated_dependencies(
    text: str,
) -> list[dict[str, str]] | None:
    """Parse Dependabot's ``updated-dependencies:`` YAML commit trailer using stdlib.

    Returns an empty list when no ``updated-dependencies`` trailer is present, a
    list of parsed dependency dicts when valid, or ``None`` if a trailer header
    appears but is malformed or incomplete.
    """
    if "updated-dependencies" not in text.lower():
        return []

    header_match = _DEPENDABOT_YAML_HEADER_RE.search(text)
    if not header_match:
        return None

    tail_lines = text[header_match.end() :].splitlines()
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    closed = False

    for raw_line in tail_lines:
        stripped = raw_line.strip()
        if stripped in ("...", "---"):
            closed = True
            break
        if not stripped:
            break
        if raw_line.startswith("- "):
            if current is not None:
                entries.append(current)
            current = {}
            kv_text = raw_line[2:].strip()
        elif raw_line.startswith("  ") and current is not None:
            kv_text = stripped
        else:
            return None

        if ":" not in kv_text:
            return None
        key_raw, val_raw = kv_text.split(":", maxsplit=1)
        key = key_raw.strip().lower()
        val = _unquote_yaml_scalar(val_raw)
        if not key or val is None:
            return None
        current[key] = val

    if current is not None:
        entries.append(current)

    if not entries or not closed:
        return None

    for item in entries:
        if not item.get("dependency-name") or not item.get("dependency-version"):
            return None

    return entries


def _classify_dependabot_yaml_entry(
    entry: dict[str, str],
    transitions_by_pkg: dict[str, tuple[str, str]],
) -> str:
    """Classify one ``updated-dependencies`` item as 'minor' or 'major'."""
    dep_name = _normalize_dep_identifier(entry.get("dependency-name", ""))
    dep_version = entry.get("dependency-version", "").strip().lstrip("vV")
    update_type = entry.get("update-type", "").strip().lower()
    if not dep_name or not dep_version:
        return "major"

    parsed = _parse_pep440_version(dep_version)
    if parsed is None:
        return "major"
    _epoch, release, new_cmp, has_pre_or_dev, local_tag = parsed
    if has_pre_or_dev or local_tag:
        return "major"

    if dep_name in transitions_by_pkg:
        old_v, transition_new_v = transitions_by_pkg[dep_name]
        transition_parsed = _parse_pep440_version(transition_new_v)
        if transition_parsed is None or transition_parsed[2] != new_cmp:
            return "major"
        if classify_version_bump(old_v, transition_new_v) == "major":
            return "major"

    if update_type == "version-update:semver-patch":
        return "minor"
    if update_type == "version-update:semver-minor":
        if release[0] == 0:
            return "major"
        return "minor"
    if not update_type and dep_name in transitions_by_pkg:
        old_v, transition_new_v = transitions_by_pkg[dep_name]
        return classify_version_bump(old_v, transition_new_v)
    return "major"


def _extract_dependabot_body_transitions(
    text: str,
) -> dict[str, tuple[str, str]] | None:
    """Extract verified ``(old_version, new_version)`` pairs keyed by normalized package name.

    Strips ``<details>`` and ``<blockquote>`` sections so upstream release notes
    cannot spoof version transitions, and returns ``None`` if duplicate entries
    for the same package disagree on versions.
    """
    header_match = _DEPENDABOT_YAML_HEADER_RE.search(text)
    prose = text[: header_match.start()] if header_match else text
    sanitized = _HTML_DETAILS_OR_QUOTE_RE.sub("", prose)

    transitions: dict[str, tuple[str, str]] = {}

    def _record(raw_name: str, raw_old: str, raw_new: str) -> bool:
        norm_name = _normalize_dep_identifier(raw_name)
        old_clean = raw_old.strip().rstrip(".")
        new_clean = raw_new.strip().rstrip(".")
        if not norm_name or not old_clean or not new_clean:
            return False
        if norm_name in transitions and transitions[norm_name] != (
            old_clean,
            new_clean,
        ):
            return False
        transitions[norm_name] = (old_clean, new_clean)
        return True

    for match in _DEPENDABOT_TABLE_ROW_RE.finditer(sanitized):
        pkg = match.group("link_name") or match.group("code_name") or ""
        if not _record(pkg, match.group("old"), match.group("new")):
            return None

    for match in _DEPENDABOT_UPDATES_LINE_RE.finditer(sanitized):
        if not _record(match.group("name"), match.group("old"), match.group("new")):
            return None

    for match in _DEPENDABOT_BUMPS_LINE_RE.finditer(sanitized):
        pkg = match.group("link_name") or match.group("code_name") or ""
        if not _record(pkg, match.group("old"), match.group("new")):
            return None

    return transitions


def _expected_group_update_count(*texts: str) -> int | None:
    """Extract the expected package update count from grouped Dependabot headers/titles."""
    counts: set[int] = set()
    for text in texts:
        if not text:
            continue
        sanitized = _HTML_DETAILS_OR_QUOTE_RE.sub("", text)
        for match in _DEPENDABOT_GROUP_COUNT_RE.finditer(sanitized):
            count = int(match.group("count"))
            if count <= 0:
                return -1
            counts.add(count)
    if not counts:
        return None
    if len(counts) > 1:
        return -1
    return next(iter(counts))


def _contains_dependabot_metadata_markers(
    commit_messages: list[str] | tuple[str, ...],
    pr_body: str,
) -> bool:
    """Return True when commit messages or PR body include Dependabot metadata markers."""
    for text in (*commit_messages, pr_body):
        if not text or not text.strip():
            continue
        if "updated-dependencies" in text.lower():
            return True
        sanitized = _HTML_DETAILS_OR_QUOTE_RE.sub("", text)
        if (
            _DEPENDABOT_UPDATES_LINE_RE.search(sanitized)
            or _DEPENDABOT_BUMPS_LINE_RE.search(sanitized)
            or _DEPENDABOT_TABLE_ROW_RE.search(sanitized)
            or _DEPENDABOT_GROUP_COUNT_RE.search(sanitized)
        ):
            return True
    return False


def dependency_update_type_from_dependabot_metadata(
    commit_messages: list[str] | tuple[str, ...] = (),
    *,
    pr_body: str = "",
    title: str = "",
) -> str:
    """Classify single or grouped Dependabot updates from commit trailers and PR metadata.

    Returns ``'minor'`` only when verified metadata confirms every updated package
    is a non-``0.x`` minor bump or a patch bump. Missing, partial, inconsistent,
    ``0.x`` minor, or major version updates fail closed to ``'major'``.
    """
    expected_count = _expected_group_update_count(title, pr_body, *commit_messages)
    if expected_count is not None and expected_count <= 0:
        return "major"

    non_merge_commits = [
        msg
        for msg in commit_messages
        if isinstance(msg, str)
        and msg.strip()
        and not _MERGE_COMMIT_SUBJECT_RE.match(msg.strip())
    ]

    verified_packages: dict[str, str] = {}

    for msg in non_merge_commits:
        yaml_entries = parse_dependabot_updated_dependencies(msg)
        if yaml_entries is None:
            return "major"
        msg_transitions = _extract_dependabot_body_transitions(msg)
        if msg_transitions is None:
            return "major"

        if not yaml_entries and not msg_transitions:
            subject = msg.strip().splitlines()[0]
            if (
                expected_count is None
                and len(list(_DEPENDABOT_VERSION_RE.finditer(subject))) == 1
            ):
                if dependency_update_type_from_title(subject) == "major":
                    return "major"
                verified_packages[f"__title__:{subject}"] = "minor"
                continue
            return "major"

        for pkg_name, (old_v, new_v) in msg_transitions.items():
            if classify_version_bump(old_v, new_v) == "major":
                return "major"
            verified_packages[pkg_name] = new_v

        if yaml_entries:
            yaml_names = {
                _normalize_dep_identifier(item.get("dependency-name", ""))
                for item in yaml_entries
            }
            if msg_transitions and set(msg_transitions.keys()) != yaml_names:
                return "major"
            for item in yaml_entries:
                if _classify_dependabot_yaml_entry(item, msg_transitions) == "major":
                    return "major"
                dep_name = _normalize_dep_identifier(item["dependency-name"])
                verified_packages[dep_name] = (
                    item["dependency-version"].strip().lstrip("vV")
                )

    if pr_body and pr_body.strip():
        body_yaml = parse_dependabot_updated_dependencies(pr_body)
        if body_yaml is None:
            return "major"
        body_transitions = _extract_dependabot_body_transitions(pr_body)
        if body_transitions is None:
            return "major"

        if not non_merge_commits and not body_yaml and not body_transitions:
            return "major"

        for pkg_name, (old_v, new_v) in body_transitions.items():
            if classify_version_bump(old_v, new_v) == "major":
                return "major"
            if pkg_name in verified_packages:
                existing_parsed = _parse_pep440_version(verified_packages[pkg_name])
                body_parsed = _parse_pep440_version(new_v)
                if (
                    existing_parsed is None
                    or body_parsed is None
                    or existing_parsed[2] != body_parsed[2]
                ):
                    return "major"
            else:
                verified_packages[pkg_name] = new_v

        if body_yaml:
            for item in body_yaml:
                if _classify_dependabot_yaml_entry(item, body_transitions) == "major":
                    return "major"
                dep_name = _normalize_dep_identifier(item["dependency-name"])
                verified_packages.setdefault(
                    dep_name, item["dependency-version"].strip().lstrip("vV")
                )

    if not verified_packages:
        return "major"

    if expected_count is not None and len(verified_packages) < expected_count:
        return "major"

    return "minor"


def resolve_dependency_update_type(
    *,
    title: str,
    changed_files: list[str] | tuple[str, ...] = (),
    is_dependabot: bool = False,
    is_security_patch: bool = False,
    all_commits_verified: bool = True,
    commit_messages: list[str] | tuple[str, ...] = (),
    pr_body: str = "",
    base_uv_lock: str | None = None,
    head_uv_lock: str | None = None,
    base_requirements_txt: str | None = None,
    head_requirements_txt: str | None = None,
    manifest_diffs_evaluated: bool = False,
    fallback_update_type: str | None = None,
) -> str:
    """Resolve dependency update risk from lockfile diffs, Dependabot metadata, or title."""
    if not (is_dependabot or is_security_patch):
        return fallback_update_type if fallback_update_type is not None else "minor"

    has_manifest_diff_inputs = manifest_diffs_evaluated or any(
        content is not None
        for content in (
            base_uv_lock,
            head_uv_lock,
            base_requirements_txt,
            head_requirements_txt,
        )
    )
    has_dependabot_meta_inputs = is_dependabot and (
        bool(commit_messages) or bool(pr_body and pr_body.strip())
    )

    if has_manifest_diff_inputs:
        diff_type = dependency_update_type_from_diffs(
            changed_files,
            base_uv_lock=base_uv_lock,
            head_uv_lock=head_uv_lock,
            base_requirements_txt=base_requirements_txt,
            head_requirements_txt=head_requirements_txt,
        )
        if diff_type == "major":
            return "major"
        if has_dependabot_meta_inputs and _contains_dependabot_metadata_markers(
            commit_messages, pr_body
        ):
            if not all_commits_verified:
                return "major"
            if (
                dependency_update_type_from_dependabot_metadata(
                    commit_messages,
                    pr_body=pr_body,
                    title=title,
                )
                == "major"
            ):
                return "major"
        return "minor"

    if has_dependabot_meta_inputs:
        if not all_commits_verified:
            return "major"
        return dependency_update_type_from_dependabot_metadata(
            commit_messages,
            pr_body=pr_body,
            title=title,
        )

    if fallback_update_type is not None:
        return fallback_update_type
    return dependency_update_type_from_title(title)


BOT_AUTHORS = {
    "dependabot[bot]",
    "dependabot",
    "stitch",
    "jules",
    "github-actions[bot]",
}


def is_approved_automation_bot(author: str) -> bool:
    """Return True if author is an approved automation bot account."""
    return bool(author) and (author in BOT_AUTHORS or author.endswith("[bot]"))


@dataclass
class PRMetadata:
    """Metadata describing a Pull Request for risk evaluation."""

    pr_number: int = 0
    title: str = ""
    author: str = ""
    is_draft: bool = False
    labels: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    is_dependabot: bool = False
    is_security_patch: bool = False
    dependency_update_type: str = "minor"  # 'major', 'minor', 'patch'
    all_commits_verified: bool = True
    unverified_commit_shas: list[str] = field(default_factory=list)
    commit_messages: list[str] = field(default_factory=list)
    pr_body: str = ""
    base_uv_lock: str | None = None
    head_uv_lock: str | None = None
    base_requirements_txt: str | None = None
    head_requirements_txt: str | None = None
    manifest_diffs_evaluated: bool = False
    author_permission: str = "none"
    author_is_bot: bool = False
    is_fork: bool = False


@dataclass
class ClassificationResult:
    """Result of PR risk classification."""

    eligible: bool
    risk_level: str  # 'low', 'medium', 'high', 'sensitive', 'baseline-change'
    labels_to_add: list[str]
    blocking_reasons: list[str]
    summary_markdown: str

    def to_dict(self) -> dict[str, Any]:
        """Convert classification result to a dictionary."""
        return {
            "eligible": self.eligible,
            "risk_level": self.risk_level,
            "labels_to_add": self.labels_to_add,
            "blocking_reasons": self.blocking_reasons,
            "summary_markdown": self.summary_markdown,
        }


def classify_pr(meta: PRMetadata) -> ClassificationResult:
    """Classify a PR based on metadata and determine auto-merge eligibility."""
    blocking_reasons: list[str] = []
    risk_level = "low"
    labels_to_add: list[str] = []

    is_dependabot = bool(meta.is_dependabot and meta.author == "dependabot[bot]")
    is_bot_author = is_approved_automation_bot(meta.author)
    is_any_bot = bool(is_bot_author or meta.author_is_bot)
    is_automated_security_patch = (
        is_bot_author
        and (
            meta.is_security_patch
            or ("security" in meta.labels and "dependencies" in meta.labels)
        )
        and "automerge" in meta.labels
    )

    effective_dependency_update_type = resolve_dependency_update_type(
        title=meta.title,
        changed_files=meta.changed_files,
        is_dependabot=is_dependabot,
        is_security_patch=(is_bot_author and meta.is_security_patch)
        or is_automated_security_patch,
        all_commits_verified=meta.all_commits_verified,
        commit_messages=meta.commit_messages,
        pr_body=meta.pr_body,
        base_uv_lock=meta.base_uv_lock,
        head_uv_lock=meta.head_uv_lock,
        base_requirements_txt=meta.base_requirements_txt,
        head_requirements_txt=meta.head_requirements_txt,
        manifest_diffs_evaluated=meta.manifest_diffs_evaluated,
        fallback_update_type=meta.dependency_update_type,
    )

    # 0. Commit signature check
    if not meta.all_commits_verified:
        if risk_level not in ("sensitive", "high"):
            risk_level = "high"
        shas_str = (
            f": {', '.join(meta.unverified_commit_shas[:3])}"
            if meta.unverified_commit_shas
            else ""
        )
        if len(meta.unverified_commit_shas) > 3:
            shas_str += f" (+{len(meta.unverified_commit_shas) - 3} more)"
        blocking_reasons.append(f"PR contains unverified commit signatures{shas_str}.")

    # 1. Disable override check
    if "disable-automerge" in meta.labels:
        blocking_reasons.append(
            "Auto-merge is explicitly disabled via 'disable-automerge' label."
        )

    # 2. Draft check
    if meta.is_draft:
        blocking_reasons.append("Draft PRs cannot be auto-merged.")

    # 3. Path analysis
    sensitive_file_matches = []
    baseline_file_matches = []
    non_docs_files = []

    for path in meta.changed_files:
        is_dep_manifest = any(re.match(pat, path) for pat in DEPENDENCY_MANIFEST_PATHS)
        is_protected = any(re.match(pat, path) for pat in SENSITIVE_PATHS)
        is_sens = (
            is_protected
            and not (is_dep_manifest and (is_dependabot or is_automated_security_patch))
        ) or (is_dep_manifest and not is_dependabot and not is_automated_security_patch)
        if is_sens:
            sensitive_file_matches.append(path)

        is_base = any(re.match(pat, path) for pat in BASELINE_PATHS)
        if is_base:
            baseline_file_matches.append(path)

        is_doc = any(re.match(pat, path) for pat in DOCS_PATHS)
        if not is_doc:
            non_docs_files.append(path)

    if sensitive_file_matches:
        risk_level = "sensitive"
        blocking_reasons.append(
            f"PR modifies protected/sensitive path(s): {', '.join(sensitive_file_matches[:3])}"
            + (
                f" (+{len(sensitive_file_matches) - 3} more)"
                if len(sensitive_file_matches) > 3
                else ""
            )
        )

    if baseline_file_matches:
        if risk_level not in ("sensitive", "high"):
            risk_level = "baseline-change"
        blocking_reasons.append(
            f"PR modifies snapshot, type, or API contract baseline file(s): {', '.join(baseline_file_matches[:3])}"
        )

    # 5. Dependency check
    if is_dependabot or is_automated_security_patch:
        if effective_dependency_update_type == "major":
            if risk_level not in ("sensitive", "high"):
                risk_level = "high"
            blocking_reasons.append("Major dependency upgrades require human review.")

    # 6. Diff size check
    total_diff = meta.additions + meta.deletions
    if len(meta.changed_files) > 10 or total_diff > 500:
        if risk_level not in ("sensitive", "high", "baseline-change"):
            risk_level = "medium"
        if (
            "automerge" not in meta.labels
            and not is_dependabot
            and not is_automated_security_patch
        ):
            blocking_reasons.append(
                f"PR size exceeds low-risk thresholds ({len(meta.changed_files)} files, {total_diff} lines) without 'automerge' label."
            )

    # 7. Opt-in requirement for production code and untrusted/bot/fork docs changes
    is_docs_only = len(meta.changed_files) > 0 and len(non_docs_files) == 0
    is_trusted_human_docs_author = (
        bool(meta.author)
        and not is_any_bot
        and not meta.is_fork
        and meta.author_permission in MAINTAINER_PERMISSIONS
    )
    is_safe_dependabot = is_dependabot and effective_dependency_update_type != "major"
    if is_docs_only:
        if not is_trusted_human_docs_author and "automerge" not in meta.labels:
            if is_any_bot:
                blocking_reasons.append(
                    "Documentation changes by automated bots require a maintainer-applied 'automerge' label."
                )
            elif meta.is_fork:
                blocking_reasons.append(
                    "Documentation changes from external forks require a maintainer-applied 'automerge' label."
                )
            else:
                blocking_reasons.append(
                    "Documentation changes by authors without collaborator write access require a maintainer-applied 'automerge' label."
                )
    elif not is_safe_dependabot:
        if "automerge" not in meta.labels:
            blocking_reasons.append(
                "Production code changes require an explicit 'automerge' label to opt into automated evaluation."
            )

    eligible = len(blocking_reasons) == 0

    labels_to_add.append(f"risk:{risk_level}")
    if eligible:
        labels_to_add.append("automerge:eligible")
    else:
        labels_to_add.append("automerge:blocked")

    md_lines = [
        "## Auto-Merge Risk Classification",
        "",
        f"- **Eligibility Status:** {'✅ **Eligible**' if eligible else '❌ **Blocked / Human Review Required**'}",
        f"- **Risk Level:** `{risk_level}`",
        f"- **Changed Files:** {len(meta.changed_files)}",
        f"- **Total Diff:** +{meta.additions} / -{meta.deletions} ({total_diff} lines)",
        f"- **Opt-in Label Present (`automerge`):** {'Yes' if 'automerge' in meta.labels else 'No'}",
        "",
    ]

    if blocking_reasons:
        md_lines.append("### Blocking Reasons")
        for reason in blocking_reasons:
            md_lines.append(f"- {reason}")
    else:
        md_lines.append(
            "This PR meets all low-risk criteria and will be processed by the auto-merge controller once required CI passes."
        )

    summary_md = "\n".join(md_lines)

    return ClassificationResult(
        eligible=eligible,
        risk_level=risk_level,
        labels_to_add=labels_to_add,
        blocking_reasons=blocking_reasons,
        summary_markdown=summary_md,
    )


def main() -> None:
    """CLI entry point for PR risk classification."""
    parser = argparse.ArgumentParser(
        description="Classify PR risk and determine auto-merge eligibility."
    )
    parser.add_argument(
        "--json-input", help="JSON string or path to JSON file containing PR metadata."
    )
    parser.add_argument(
        "--output", help="Optional path to save classification JSON output."
    )

    args = parser.parse_args()

    if not args.json_input:
        raw_data = sys.stdin.read()
    elif args.json_input.startswith("{"):
        raw_data = args.json_input
    else:
        with open(args.json_input, encoding="utf-8") as f:
            raw_data = f.read()

    data = json.loads(raw_data)
    title = data.get("title", "")
    labels = data.get("labels", [])
    author = data.get("author", "")
    changed_files = data.get("changed_files", [])
    is_dependabot = bool(
        data.get("is_dependabot", False) and author == "dependabot[bot]"
    )
    is_security_patch = is_approved_automation_bot(author) and (
        bool(data.get("is_security_patch", False))
        or ("security" in labels and "dependencies" in labels)
    )
    manifest_keys = (
        "base_uv_lock",
        "head_uv_lock",
        "base_requirements_txt",
        "head_requirements_txt",
    )
    has_manifest_keys = bool(data.get("manifest_diffs_evaluated", False)) or any(
        key in data for key in manifest_keys
    )
    base_uv_lock = data.get("base_uv_lock")
    head_uv_lock = data.get("head_uv_lock")
    base_requirements_txt = data.get("base_requirements_txt")
    head_requirements_txt = data.get("head_requirements_txt")
    all_commits_verified = bool(data.get("all_commits_verified", True))
    raw_commit_messages = data.get("commit_messages", [])
    commit_messages = (
        [m for m in raw_commit_messages if isinstance(m, str) and m.strip()]
        if isinstance(raw_commit_messages, list)
        else []
    )
    raw_pr_body = data.get("pr_body", data.get("body", ""))
    pr_body = raw_pr_body if isinstance(raw_pr_body, str) else ""

    dep_update_type = resolve_dependency_update_type(
        title=title,
        changed_files=changed_files,
        is_dependabot=is_dependabot,
        is_security_patch=is_security_patch,
        all_commits_verified=all_commits_verified,
        commit_messages=commit_messages,
        pr_body=pr_body,
        base_uv_lock=base_uv_lock,
        head_uv_lock=head_uv_lock,
        base_requirements_txt=base_requirements_txt,
        head_requirements_txt=head_requirements_txt,
        manifest_diffs_evaluated=has_manifest_keys,
        fallback_update_type=None,
    )

    raw_permission = data.get("author_permission")
    author_permission = (
        raw_permission.strip().lower()
        if isinstance(raw_permission, str) and raw_permission.strip()
        else "none"
    )
    author_is_bot = bool(
        data.get("author_is_bot", False) or is_approved_automation_bot(author)
    )
    is_fork = bool(data.get("is_fork", False))

    meta = PRMetadata(
        pr_number=data.get("pr_number", 0),
        title=title,
        author=author,
        is_draft=data.get("is_draft", False),
        labels=labels,
        changed_files=changed_files,
        additions=data.get("additions", 0),
        deletions=data.get("deletions", 0),
        is_dependabot=is_dependabot,
        is_security_patch=is_security_patch,
        dependency_update_type=dep_update_type,
        all_commits_verified=all_commits_verified,
        unverified_commit_shas=data.get("unverified_commit_shas", []),
        commit_messages=commit_messages,
        pr_body=pr_body,
        base_uv_lock=base_uv_lock,
        head_uv_lock=head_uv_lock,
        base_requirements_txt=base_requirements_txt,
        head_requirements_txt=head_requirements_txt,
        manifest_diffs_evaluated=has_manifest_keys,
        author_permission=author_permission,
        author_is_bot=author_is_bot,
        is_fork=is_fork,
    )

    result = classify_pr(meta)
    output_json = json.dumps(result.to_dict(), indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)

    print(output_json)


if __name__ == "__main__":
    main()
