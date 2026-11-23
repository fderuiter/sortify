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
    # Modules that can lose or corrupt a user's files (ADR 0005, ADR 0006)
    "app/core/mover.py",
    "app/core/resilient_file_ops.py",
    "app/core/ledger.py",
    "app/core/history.py",
    "app/core/session.py",
    "app/core/db.py",
    "app/core/db_conn.py",
    "app/core/db_worker.py",
    "app/core/quarantine_interceptor.py",
    "scripts/",
    "smart-autosorter.spec",
    "network_rules.json",
    ".gitleaks.toml",
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
]

DOCS_PATHS = [
    r"^docs/.*",
    r"^README\.md$",
    r"^mkdocs\.yml$",
]

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
    reviews them when lockfile diffs are unavailable.
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
    base_uv_lock: str | None = None
    head_uv_lock: str | None = None
    base_requirements_txt: str | None = None
    head_requirements_txt: str | None = None
    manifest_diffs_evaluated: bool = False


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
    is_automated_security_patch = (
        is_bot_author
        and (
            meta.is_security_patch
            or ("security" in meta.labels and "dependencies" in meta.labels)
        )
        and "automerge" in meta.labels
    )

    has_manifest_diff_inputs = meta.manifest_diffs_evaluated or any(
        content is not None
        for content in (
            meta.base_uv_lock,
            meta.head_uv_lock,
            meta.base_requirements_txt,
            meta.head_requirements_txt,
        )
    )
    effective_dependency_update_type = (
        dependency_update_type_from_diffs(
            meta.changed_files,
            base_uv_lock=meta.base_uv_lock,
            head_uv_lock=meta.head_uv_lock,
            base_requirements_txt=meta.base_requirements_txt,
            head_requirements_txt=meta.head_requirements_txt,
        )
        if has_manifest_diff_inputs
        and (
            is_dependabot
            or (is_bot_author and meta.is_security_patch)
            or is_automated_security_patch
        )
        else meta.dependency_update_type
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
        is_sens = any(re.match(pat, path) for pat in SENSITIVE_PATHS) or (
            not is_dependabot
            and not is_automated_security_patch
            and any(re.match(pat, path) for pat in DEPENDENCY_MANIFEST_PATHS)
        )
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
            f"PR modifies snapshot or API contract baseline file(s): {', '.join(baseline_file_matches[:3])}"
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
            non_docs_files
            and "automerge" not in meta.labels
            and not is_dependabot
            and not is_automated_security_patch
        ):
            blocking_reasons.append(
                f"PR size exceeds low-risk thresholds ({len(meta.changed_files)} files, {total_diff} lines) without 'automerge' label."
            )

    # 7. Production code opt-in requirement
    is_docs_only = len(meta.changed_files) > 0 and len(non_docs_files) == 0
    is_safe_dependabot = is_dependabot and effective_dependency_update_type != "major"
    if not is_docs_only and not is_safe_dependabot:
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
        with open(args.json_input, "r", encoding="utf-8") as f:
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

    if is_dependabot or is_security_patch:
        if has_manifest_keys:
            dep_update_type = dependency_update_type_from_diffs(
                changed_files,
                base_uv_lock=base_uv_lock,
                head_uv_lock=head_uv_lock,
                base_requirements_txt=base_requirements_txt,
                head_requirements_txt=head_requirements_txt,
            )
        else:
            dep_update_type = dependency_update_type_from_title(title)
    else:
        dep_update_type = "minor"

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
        all_commits_verified=data.get("all_commits_verified", True),
        unverified_commit_shas=data.get("unverified_commit_shas", []),
        base_uv_lock=base_uv_lock,
        head_uv_lock=head_uv_lock,
        base_requirements_txt=base_requirements_txt,
        head_requirements_txt=head_requirements_txt,
        manifest_diffs_evaluated=has_manifest_keys,
    )

    result = classify_pr(meta)
    output_json = json.dumps(result.to_dict(), indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)

    print(output_json)


if __name__ == "__main__":
    main()
