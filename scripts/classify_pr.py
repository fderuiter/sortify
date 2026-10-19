"""PR Risk Classification Engine for Sortify Auto-Merge System.

Evaluates pull request metadata (file paths, diff size, author, draft state, labels)
and determines risk tier and auto-merge eligibility without executing untrusted PR code.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List

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


def dependency_update_type_from_title(title: str) -> str:
    """Classify a Dependabot title ("Bump x from 1.2.3 to 2.0.0") as major or minor.

    A change in the first version component is major, and so is a change in the
    second while the first is 0, as semver treats 0.x minors as breaking. Titles
    without a single "from A to B" (grouped updates) count as major, so a person
    reviews them.
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


@dataclass
class PRMetadata:
    """Metadata describing a Pull Request for risk evaluation."""

    pr_number: int = 0
    title: str = ""
    author: str = ""
    is_draft: bool = False
    labels: List[str] = field(default_factory=list)
    changed_files: List[str] = field(default_factory=list)
    additions: int = 0
    deletions: int = 0
    is_dependabot: bool = False
    dependency_update_type: str = "minor"  # 'major', 'minor', 'patch'
    all_commits_verified: bool = True
    unverified_commit_shas: List[str] = field(default_factory=list)


@dataclass
class ClassificationResult:
    """Result of PR risk classification."""

    eligible: bool
    risk_level: str  # 'low', 'medium', 'high', 'sensitive', 'baseline-change'
    labels_to_add: List[str]
    blocking_reasons: List[str]
    summary_markdown: str

    def to_dict(self) -> Dict[str, Any]:
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
    blocking_reasons: List[str] = []
    risk_level = "low"
    labels_to_add: List[str] = []

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
            not meta.is_dependabot
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
    if meta.is_dependabot:
        if meta.dependency_update_type == "major":
            if risk_level not in ("sensitive", "high"):
                risk_level = "high"
            blocking_reasons.append("Major dependency upgrades require human review.")

    # 6. Diff size check
    total_diff = meta.additions + meta.deletions
    if len(meta.changed_files) > 10 or total_diff > 500:
        if risk_level not in ("sensitive", "high", "baseline-change"):
            risk_level = "medium"
        if non_docs_files and "automerge" not in meta.labels and not meta.is_dependabot:
            blocking_reasons.append(
                f"PR size exceeds low-risk thresholds ({len(meta.changed_files)} files, {total_diff} lines) without 'automerge' label."
            )

    # 7. Production code opt-in requirement
    is_docs_only = len(meta.changed_files) > 0 and len(non_docs_files) == 0
    # Only the exact Dependabot login counts: anyone can add a "dependencies" label.
    is_safe_dependabot = meta.is_dependabot and meta.dependency_update_type != "major"

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
    is_dependabot = data.get("is_dependabot", False)
    # Derived from the title, as auto_merge_controller does, so the labels
    # this posts agree with what the controller will merge.
    meta = PRMetadata(
        pr_number=data.get("pr_number", 0),
        title=title,
        author=data.get("author", ""),
        is_draft=data.get("is_draft", False),
        labels=data.get("labels", []),
        changed_files=data.get("changed_files", []),
        additions=data.get("additions", 0),
        deletions=data.get("deletions", 0),
        is_dependabot=is_dependabot,
        dependency_update_type=(
            dependency_update_type_from_title(title) if is_dependabot else "minor"
        ),
        all_commits_verified=data.get("all_commits_verified", True),
        unverified_commit_shas=data.get("unverified_commit_shas", []),
    )

    result = classify_pr(meta)
    output_json = json.dumps(result.to_dict(), indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(output_json)

    print(output_json)


if __name__ == "__main__":
    main()
