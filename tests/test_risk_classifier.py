"""Tests for PR Risk Classifier."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts.classify_pr import (
    PROTECTED_PATHS,
    PRMetadata,
    classify_pr,
    classify_requirements_diff,
    classify_uv_lock_diff,
    classify_version_bump,
    dependency_update_type_from_dependabot_metadata,
    dependency_update_type_from_diffs,
    dependency_update_type_from_title,
    main,
    parse_dependabot_updated_dependencies,
    resolve_dependency_update_type,
)


def test_docs_only_pr_is_eligible():
    meta = PRMetadata(
        pr_number=101,
        title="docs: update installation instructions",
        author="fderuiter",
        author_permission="admin",
        is_fork=False,
        changed_files=["docs/setup.md", "README.md"],
        additions=20,
        deletions=5,
    )
    res = classify_pr(meta)
    assert res.eligible is True
    assert res.risk_level == "low"
    assert "automerge:eligible" in res.labels_to_add
    assert "risk:low" in res.labels_to_add


@pytest.mark.parametrize(
    "path",
    [
        "mkdocs.yml",
        ".pre-commit-config.yaml",
        ".claude/settings.json",
        ".claude/hooks/session-start.sh",
        ".agents/skills/tdd/SKILL.md",
        "AGENTS.md",
        "CLAUDE.md",
        ".gitattributes",
        "offline_bundle/requirements.txt",
    ],
)
def test_newly_protected_config_hook_agent_and_manifest_paths_are_sensitive(path):
    meta = PRMetadata(
        pr_number=1010,
        title=f"chore: update {path}",
        author="fderuiter",
        author_permission="admin",
        labels=["automerge"],
        changed_files=[path],
        additions=4,
        deletions=2,
    )
    res = classify_pr(meta)
    assert res.eligible is False, path
    assert res.risk_level == "sensitive", path
    assert "risk:sensitive" in res.labels_to_add
    assert "automerge:blocked" in res.labels_to_add
    assert any("protected/sensitive" in r for r in res.blocking_reasons)


@pytest.mark.parametrize("permission", ["write", "maintain", "admin"])
def test_small_docs_pr_from_human_maintainer_is_eligible_without_label(permission):
    meta = PRMetadata(
        pr_number=1011,
        title="docs: clarify user guide",
        author="maintainer-user",
        author_permission=permission,
        author_is_bot=False,
        is_fork=False,
        changed_files=["docs/user_guide.md", "README.md"],
        additions=12,
        deletions=3,
    )
    res = classify_pr(meta)
    assert res.eligible is True
    assert res.risk_level == "low"
    assert "automerge:eligible" in res.labels_to_add


@pytest.mark.parametrize(
    ("kwargs", "expected_reason_substr"),
    [
        (
            {
                "author": "google-labs-stitch[bot]",
                "author_permission": "write",
                "author_is_bot": True,
                "changed_files": ["docs/user_guide.md"],
            },
            "automated bots",
        ),
        (
            {
                "author": "stitch",
                "author_permission": "write",
                "changed_files": ["README.md"],
            },
            "automated bots",
        ),
        (
            {
                "author": "external-contributor",
                "author_permission": "write",
                "is_fork": True,
                "changed_files": ["docs/user_guide.md"],
            },
            "external forks",
        ),
        (
            {
                "author": "reader-user",
                "author_permission": "read",
                "is_fork": False,
                "changed_files": ["README.md"],
            },
            "collaborator write access",
        ),
        (
            {
                "author": "unknown-user",
                "changed_files": ["docs/user_guide.md"],
            },
            "collaborator write access",
        ),
        (
            {
                "author": "fderuiter",
                "author_permission": "admin",
                "changed_files": ["docs/user_guide.md"],
                "additions": 400,
                "deletions": 150,
            },
            "PR size exceeds low-risk thresholds",
        ),
        (
            {
                "author": "fderuiter",
                "author_permission": "admin",
                "changed_files": [f"docs/page_{i}.md" for i in range(11)],
                "additions": 20,
                "deletions": 5,
            },
            "PR size exceeds low-risk thresholds",
        ),
    ],
)
def test_unlabelled_docs_pr_blocked_for_bots_forks_non_collaborators_and_oversized_diffs(
    kwargs, expected_reason_substr
):
    meta = PRMetadata(
        pr_number=1012,
        title="docs: documentation update",
        **kwargs,
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert "automerge:blocked" in res.labels_to_add
    assert any(expected_reason_substr in r for r in res.blocking_reasons), (
        res.blocking_reasons
    )


def test_bot_and_fork_docs_pr_with_maintainer_automerge_label_is_eligible():
    for meta in (
        PRMetadata(
            pr_number=1013,
            title="docs: reviewed update",
            author="google-labs-stitch[bot]",
            author_is_bot=True,
            labels=["automerge"],
            changed_files=["docs/user_guide.md"],
            additions=10,
            deletions=2,
        ),
        PRMetadata(
            pr_number=1013,
            title="docs: reviewed update",
            author="external-contributor",
            is_fork=True,
            labels=["automerge"],
            changed_files=["README.md"],
            additions=10,
            deletions=2,
        ),
    ):
        res = classify_pr(meta)
        assert res.eligible is True
        assert res.risk_level == "low"


def test_opt_in_bot_pr_is_eligible():
    meta = PRMetadata(
        pr_number=102,
        title="refactor: improve internal helper",
        author="stitch",
        labels=["automerge"],
        changed_files=["app/core/analyzer.py"],
        additions=15,
        deletions=3,
    )
    res = classify_pr(meta)
    assert res.eligible is True
    assert res.risk_level == "low"
    assert "automerge:eligible" in res.labels_to_add


def test_production_pr_without_automerge_label_is_blocked():
    meta = PRMetadata(
        pr_number=103,
        title="feat: new sorting heuristic",
        author="stitch",
        labels=[],
        changed_files=["app/core/policy_engine.py"],
        additions=30,
        deletions=10,
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert "automerge:blocked" in res.labels_to_add
    assert any("automerge" in r for r in res.blocking_reasons)


def test_draft_pr_is_blocked():
    meta = PRMetadata(
        pr_number=104,
        title="WIP: new feature",
        author="jules",
        is_draft=True,
        labels=["automerge"],
        changed_files=["app/core/resilient_file_ops.py"],
        additions=10,
        deletions=2,
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert any("Draft" in r for r in res.blocking_reasons)


def test_sensitive_path_is_blocked():
    meta = PRMetadata(
        pr_number=105,
        title="fix: crypto initialization",
        author="stitch",
        labels=["automerge"],
        changed_files=["app/core/crypto.py"],
        additions=5,
        deletions=1,
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert res.risk_level == "sensitive"
    assert "risk:sensitive" in res.labels_to_add
    assert any("protected/sensitive" in r for r in res.blocking_reasons)


def test_workflow_change_is_sensitive_and_blocked():
    meta = PRMetadata(
        pr_number=106,
        title="ci: update workflow",
        author="fderuiter",
        labels=["automerge"],
        changed_files=[".github/workflows/ci.yml"],
        additions=12,
        deletions=4,
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert res.risk_level == "sensitive"


def test_snapshot_baseline_change_is_blocked():
    meta = PRMetadata(
        pr_number=107,
        title="test: update snapshots",
        author="stitch",
        labels=["automerge"],
        changed_files=["tests/snapshots/api/core/analyzer_strategies.json"],
        additions=10,
        deletions=10,
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert res.risk_level == "baseline-change"
    assert "risk:baseline-change" in res.labels_to_add


def test_dependabot_minor_is_eligible():
    meta = PRMetadata(
        pr_number=108,
        title="bump ruff from 0.8.0 to 0.8.1",
        author="dependabot[bot]",
        is_dependabot=True,
        labels=["dependencies"],
        changed_files=["pyproject.toml", "uv.lock"],
        additions=10,
        deletions=10,
        dependency_update_type="minor",
    )
    res = classify_pr(meta)
    assert res.eligible is True
    assert res.risk_level == "low"


def test_dependabot_major_is_blocked():
    meta = PRMetadata(
        pr_number=109,
        title="bump pydantic from 2.0 to 3.0",
        author="dependabot[bot]",
        is_dependabot=True,
        labels=["dependencies"],
        changed_files=["pyproject.toml", "uv.lock"],
        additions=15,
        deletions=15,
        dependency_update_type="major",
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert res.risk_level == "high"
    assert any("Major dependency upgrades" in r for r in res.blocking_reasons)


def test_disable_automerge_label_blocked():
    meta = PRMetadata(
        pr_number=111,
        title="docs: update readme",
        author="fderuiter",
        labels=["disable-automerge"],
        changed_files=["README.md"],
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert any("disable-automerge" in r for r in res.blocking_reasons)


def test_unverified_commit_signature_is_blocked():
    meta = PRMetadata(
        pr_number=112,
        title="docs: fix typo",
        author="stitch",
        changed_files=["docs/setup.md"],
        all_commits_verified=False,
        unverified_commit_shas=["sha_unverified_1", "sha_unverified_2"],
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert res.risk_level == "high"
    assert "risk:high" in res.labels_to_add
    assert "automerge:blocked" in res.labels_to_add
    assert any("unverified commit signatures" in r for r in res.blocking_reasons)
    assert "sha_unverified_1" in res.summary_markdown


def test_bot_commit_signature_verification_paths():
    # Genuine signed bot commit
    meta_verified_bot = PRMetadata(
        pr_number=113,
        title="bump dependency",
        author="dependabot[bot]",
        is_dependabot=True,
        labels=["dependencies"],
        changed_files=["pyproject.toml", "uv.lock"],
        all_commits_verified=True,
    )
    res_verified = classify_pr(meta_verified_bot)
    assert res_verified.eligible is True
    assert res_verified.risk_level == "low"

    # Spoofed/unverified bot commit
    meta_unverified_bot = PRMetadata(
        pr_number=114,
        title="bump dependency",
        author="dependabot[bot]",
        is_dependabot=True,
        labels=["dependencies"],
        changed_files=["pyproject.toml", "uv.lock"],
        all_commits_verified=False,
        unverified_commit_shas=["spoofed_bot_sha"],
    )
    res_unverified = classify_pr(meta_unverified_bot)
    assert res_unverified.eligible is False
    assert res_unverified.risk_level == "high"
    assert "automerge:blocked" in res_unverified.labels_to_add
    assert any(
        "unverified commit signatures" in r for r in res_unverified.blocking_reasons
    )


def test_merge_tooling_scripts_are_sensitive():
    for path in (
        "scripts/classify_pr.py",
        "scripts/auto_merge_controller.py",
        "scripts/validate_imports.py",
    ):
        meta = PRMetadata(
            pr_number=115,
            title="chore: tweak",
            author="stitch",
            labels=["automerge"],
            changed_files=[path],
        )
        res = classify_pr(meta)
        assert res.eligible is False, path
        assert res.risk_level == "sensitive"


def test_dependency_manifests_sensitive_unless_dependabot():
    meta = PRMetadata(
        pr_number=116,
        title="chore: add dependency",
        author="stitch",
        labels=["automerge", "dependencies"],
        changed_files=["pyproject.toml", "uv.lock"],
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert res.risk_level == "sensitive"


@pytest.mark.parametrize(
    "path",
    [
        "app/core/resilient_file_ops.py",
        "app/core/ledger.py",
        "app/core/db.py",
        "app/core/db_conn.py",
        "app/core/db_worker.py",
    ],
)
def test_data_safety_modules_are_protected(path):
    meta = PRMetadata(
        pr_number=117,
        title="fix: small change",
        author="stitch",
        labels=["automerge"],
        changed_files=[path],
        additions=2,
        deletions=1,
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert res.risk_level == "sensitive"


def test_dependencies_label_does_not_skip_the_opt_in():
    meta = PRMetadata(
        pr_number=118,
        title="chore: bump helper",
        author="stitch",
        labels=["dependencies"],
        changed_files=["app/core/analyzer.py"],
        additions=2,
        deletions=1,
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert any("'automerge' label" in r for r in res.blocking_reasons)


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Bump requests from 2.31.0 to 2.32.3", "minor"),
        ("build(deps): bump ruff from 0.6.1 to 0.6.9 in /tools", "minor"),
        ("Bump pydantic from 2.9.2 to 3.0.0", "major"),
        ("Bump actions/checkout from v4 to v5", "major"),
        ("Bump textual from 0.79.1 to 0.80.0", "major"),
        ("Bump the pip group across 1 directory with 3 updates", "major"),
        ("Bump numpy", "major"),
    ],
)
def test_dependency_update_type_from_title(title, expected):
    assert dependency_update_type_from_title(title) == expected


def test_protected_paths_are_listed_in_codeowners():
    codeowners = Path(__file__).resolve().parents[1] / ".github" / "CODEOWNERS"
    rule_lines = [
        line.strip()
        for line in codeowners.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    all_patterns = [line.split()[0] for line in rule_lines]
    wildcard_patterns = [p for p in all_patterns if "*" in p]
    assert not wildcard_patterns, (
        f".github/CODEOWNERS must not contain wildcard rules: {wildcard_patterns}"
    )

    owned = {
        line.split()[0]
        for line in rule_lines
        if len(line.split()) >= 2 and "@fderuiter" in line.split()[1:]
    }
    expected_paths = set(PROTECTED_PATHS) | {
        "tests/snapshots/api/",
        "tests/snapshots/tui_svg/",
    }
    missing = [p for p in sorted(expected_paths) if p not in owned]
    assert not missing, f"Add to .github/CODEOWNERS: {missing}"
    unexpected = sorted(owned - expected_paths)
    assert not unexpected, (
        f"Unexpected .github/CODEOWNERS entries outside Tier 3 paths: {unexpected}"
    )


@pytest.mark.parametrize(
    ("title", "risk_label"),
    [
        ("Bump requests from 2.31.0 to 3.0.0", "risk:high"),
        ("Bump requests from 2.31.0 to 2.32.0", "risk:low"),
    ],
)
def test_main_derives_dependabot_update_type_from_title(
    tmp_path, monkeypatch, capsys, title, risk_label
):
    """main() reads the update type off the title, ignoring any value in the JSON."""
    meta_path = tmp_path / "pr_meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "pr_number": 1,
                "title": title,
                "author": "dependabot[bot]",
                "changed_files": ["pyproject.toml", "uv.lock"],
                "additions": 4,
                "deletions": 4,
                "is_dependabot": True,
                "dependency_update_type": "minor",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["classify_pr.py", "--json-input", str(meta_path)])
    main()
    result = json.loads(capsys.readouterr().out)
    assert risk_label in result["labels_to_add"]


def test_automated_security_patch_minor_is_eligible():
    meta = PRMetadata(
        pr_number=201,
        title="[Security] Patch certifi from 2026.7.22 to 2026.10.19",
        author="github-actions[bot]",
        is_security_patch=True,
        labels=["security", "dependencies", "automerge"],
        changed_files=["uv.lock"],
        additions=10,
        deletions=10,
        dependency_update_type="minor",
    )
    res = classify_pr(meta)
    assert res.eligible is True
    assert res.risk_level == "low"
    assert "automerge:eligible" in res.labels_to_add
    assert "risk:low" in res.labels_to_add


def test_automated_security_patch_major_is_blocked():
    meta = PRMetadata(
        pr_number=202,
        title="[Security] Patch requests from 2.8.0 to 3.0.0",
        author="github-actions[bot]",
        is_security_patch=True,
        labels=["security", "dependencies", "automerge"],
        changed_files=["uv.lock"],
        additions=15,
        deletions=15,
        dependency_update_type="major",
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert res.risk_level == "high"
    assert "automerge:blocked" in res.labels_to_add
    assert any("Major dependency upgrades" in r for r in res.blocking_reasons)


def test_automated_security_patch_sensitive_path_is_blocked():
    meta = PRMetadata(
        pr_number=203,
        title="[Security] Patch certifi from 2026.7.22 to 2026.10.19",
        author="github-actions[bot]",
        is_security_patch=True,
        labels=["security", "dependencies", "automerge"],
        changed_files=["uv.lock", "app/core/crypto.py"],
        additions=20,
        deletions=10,
        dependency_update_type="minor",
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert res.risk_level == "sensitive"
    assert "risk:sensitive" in res.labels_to_add
    assert "automerge:blocked" in res.labels_to_add


def test_automated_security_patch_without_automerge_is_blocked_as_sensitive():
    meta = PRMetadata(
        pr_number=204,
        title="[Security] Patch certifi from 2026.7.22 to 2026.10.19",
        author="github-actions[bot]",
        is_security_patch=True,
        labels=["security", "dependencies"],
        changed_files=["uv.lock"],
        additions=10,
        deletions=10,
        dependency_update_type="minor",
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert res.risk_level == "sensitive"
    assert "automerge:blocked" in res.labels_to_add
    assert "risk:sensitive" in res.labels_to_add
    assert any(
        "PR modifies protected/sensitive path(s): uv.lock" in r
        for r in res.blocking_reasons
    )
    assert any(
        "Production code changes require an explicit 'automerge' label" in r
        for r in res.blocking_reasons
    )


def test_agent_pyproject_toml_without_automerge_triggers_sensitive_path():
    meta = PRMetadata(
        pr_number=205,
        title="fix(deps): update dependency in pyproject.toml",
        author="stitch",
        labels=["dependencies"],
        changed_files=["pyproject.toml"],
        additions=2,
        deletions=2,
        dependency_update_type="minor",
    )
    res = classify_pr(meta)
    assert res.eligible is False
    assert res.risk_level == "sensitive"
    assert "automerge:blocked" in res.labels_to_add
    assert any(
        "PR modifies protected/sensitive path(s): pyproject.toml" in r
        for r in res.blocking_reasons
    )


@pytest.mark.parametrize(
    ("old_ver", "new_ver", "expected"),
    [
        ("2.31.0", "2.32.3", "minor"),
        ("2.31.0", "2.31.1", "minor"),
        ("0.6.1", "0.6.9", "minor"),
        ("2.5.1+cpu", "2.5.2+cpu", "minor"),
        ("1.4.0", "1.4.0.post1", "minor"),
        ("2.9.2", "3.0.0", "major"),
        ("0.79.1", "0.80.0", "major"),
        ("2.5.1+cpu", "2.5.2", "major"),
        ("2.32.3", "2.31.0", "major"),
        ("2.31.0", "2.32.0a1", "major"),
        ("1!1.0.0", "2!1.0.1", "major"),
        ("2.31.0", "not-a-version", "major"),
    ],
)
def test_classify_version_bump_cases(old_ver, new_ver, expected):
    assert classify_version_bump(old_ver, new_ver) == expected


def test_classify_uv_lock_diff_minor_grouped_and_multisource():
    base_lock = """\
version = 1
requires-python = ">=3.12"

[[package]]
name = "smart-autosorter"
version = "0.1.0"
source = { virtual = "." }

[[package]]
name = "requests"
version = "2.31.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "ruff"
version = "0.8.1"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "torch"
version = "2.5.1"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "torch"
version = "2.5.1+cpu"
source = { registry = "https://download.pytorch.org/whl/cpu" }
"""
    head_lock = """\
version = 1
requires-python = ">=3.12"

[[package]]
name = "smart-autosorter"
version = "0.1.0"
source = { virtual = "." }

[[package]]
name = "requests"
version = "2.32.3"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "ruff"
version = "0.8.4"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "torch"
version = "2.6.0"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "torch"
version = "2.6.0+cpu"
source = { registry = "https://download.pytorch.org/whl/cpu" }
"""
    assert classify_uv_lock_diff(base_lock, head_lock) == "minor"


@pytest.mark.parametrize(
    ("mutated_head", "reason"),
    [
        (
            'version = 1\n[[package]]\nname = "requests"\nversion = "3.0.0"\n',
            "major bump",
        ),
        (
            'version = 1\n[[package]]\nname = "ruff"\nversion = "0.80.0"\n',
            "0.x minor bump",
        ),
        (
            'version = 1\n[[package]]\nname = "requests"\nversion = "2.32.3"\n'
            '[[package]]\nname = "ruff"\nversion = "0.79.1"\n'
            '[[package]]\nname = "evil-pkg"\nversion = "1.0.0"\n',
            "added package",
        ),
        (
            'version = 1\n[[package]]\nname = "requests"\nversion = "2.32.3"\n',
            "removed package",
        ),
        (
            'version = 1\n[[package]]\nname = "requests"\nversion = "2.30.0"\n'
            '[[package]]\nname = "ruff"\nversion = "0.79.1"\n',
            "downgrade",
        ),
        (
            'version = 1\n# tampered comment\n[[package]]\nname = "requests"\nversion = "2.31.0"\n'
            '[[package]]\nname = "ruff"\nversion = "0.79.1"\n',
            "non-version lockfile edit",
        ),
        ("invalid = [toml", "unparseable TOML"),
    ],
)
def test_classify_uv_lock_diff_blocks_breaking_and_tampered_diffs(mutated_head, reason):
    base_lock = (
        "version = 1\n"
        '[[package]]\nname = "requests"\nversion = "2.31.0"\n'
        '[[package]]\nname = "ruff"\nversion = "0.79.1"\n'
    )
    assert classify_uv_lock_diff(base_lock, mutated_head) == "major", reason


def test_classify_requirements_diff_minor_and_breaking():
    base_req = """\
# Autogenerated by uv
requests==2.31.0 \\
    --hash=sha256:1111
torch==2.5.1 ; sys_platform == 'darwin' \\
    --hash=sha256:2222
torch==2.5.1+cpu ; sys_platform == 'linux' \\
    --hash=sha256:3333
"""
    minor_head = """\
# Autogenerated by uv
requests==2.32.3 \\
    --hash=sha256:4444
torch==2.6.0 ; sys_platform == 'darwin' \\
    --hash=sha256:5555
torch==2.6.0+cpu ; sys_platform == 'linux' \\
    --hash=sha256:6666
"""
    assert classify_requirements_diff(base_req, minor_head) == "minor"

    # Major bump
    assert (
        classify_requirements_diff(
            base_req, minor_head.replace("requests==2.32.3", "requests==3.0.0")
        )
        == "major"
    )
    # Local +cpu tag stripped
    assert (
        classify_requirements_diff(
            base_req, minor_head.replace("torch==2.6.0+cpu", "torch==2.6.0")
        )
        == "major"
    )
    # Added package
    assert (
        classify_requirements_diff(
            base_req, minor_head + "urllib3==2.2.3 --hash=sha256:7777\n"
        )
        == "major"
    )
    # Injected pip option (--extra-index-url)
    assert (
        classify_requirements_diff(
            base_req, "--extra-index-url https://evil.invalid/simple\n" + minor_head
        )
        == "major"
    )
    # Unpinned requirement
    assert (
        classify_requirements_diff(
            base_req, minor_head.replace("requests==2.32.3", "requests>=2.32.3")
        )
        == "major"
    )


def test_dependabot_grouped_and_unpinned_lockfile_pr_is_eligible(
    tmp_path, monkeypatch, capsys
):
    base_lock = (
        "version = 1\n"
        '[[package]]\nname = "requests"\nversion = "2.31.0"\n'
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n'
    )
    head_lock = (
        "version = 1\n"
        '[[package]]\nname = "requests"\nversion = "2.32.3"\n'
        '[[package]]\nname = "certifi"\nversion = "2026.10.19"\n'
    )
    base_reqs = (
        "requests==2.31.0 --hash=sha256:aaa\ncertifi==2026.7.22 --hash=sha256:bbb\n"
    )
    head_reqs = (
        "requests==2.32.3 --hash=sha256:ccc\ncertifi==2026.10.19 --hash=sha256:ddd\n"
    )

    assert (
        dependency_update_type_from_diffs(
            ["pyproject.toml", "uv.lock", "offline_bundle/requirements.txt"],
            base_uv_lock=base_lock,
            head_uv_lock=head_lock,
            base_requirements_txt=base_reqs,
            head_requirements_txt=head_reqs,
        )
        == "minor"
    )

    meta_path = tmp_path / "grouped_pr.json"
    meta_path.write_text(
        json.dumps(
            {
                "pr_number": 310,
                "title": "Bump the pip group across 1 directory with 2 updates",
                "author": "dependabot[bot]",
                "is_dependabot": True,
                "labels": ["dependencies"],
                "changed_files": [
                    "pyproject.toml",
                    "uv.lock",
                    "offline_bundle/requirements.txt",
                ],
                "additions": 25,
                "deletions": 25,
                "base_uv_lock": base_lock,
                "head_uv_lock": head_lock,
                "base_requirements_txt": base_reqs,
                "head_requirements_txt": head_reqs,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["classify_pr.py", "--json-input", str(meta_path)])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["eligible"] is True
    assert result["risk_level"] == "low"
    assert "automerge:eligible" in result["labels_to_add"]


def test_dependabot_grouped_pr_with_breaking_or_missing_manifest_is_blocked():
    base_lock = 'version = 1\n[[package]]\nname = "textual"\nversion = "0.79.1"\n'
    breaking_lock = 'version = 1\n[[package]]\nname = "textual"\nversion = "0.80.0"\n'

    meta_breaking = PRMetadata(
        pr_number=311,
        title="Bump textual",
        author="dependabot[bot]",
        is_dependabot=True,
        labels=["dependencies"],
        changed_files=["pyproject.toml", "uv.lock"],
        base_uv_lock=base_lock,
        head_uv_lock=breaking_lock,
        manifest_diffs_evaluated=True,
    )
    res_breaking = classify_pr(meta_breaking)
    assert res_breaking.eligible is False
    assert res_breaking.risk_level == "high"
    assert "automerge:blocked" in res_breaking.labels_to_add

    # Missing base/head content when manifest_diffs_evaluated=True fails closed as major
    meta_missing = PRMetadata(
        pr_number=312,
        title="Bump requests from 2.31.0 to 2.32.3",
        author="dependabot[bot]",
        is_dependabot=True,
        labels=["dependencies"],
        changed_files=["uv.lock"],
        base_uv_lock=None,
        head_uv_lock=None,
        manifest_diffs_evaluated=True,
    )
    res_missing = classify_pr(meta_missing)
    assert res_missing.eligible is False
    assert res_missing.risk_level == "high"


def test_offline_requirements_and_spoofed_security_patch_blocked_for_non_bots():
    res_reqs = classify_pr(
        PRMetadata(
            pr_number=313,
            title="chore: edit offline requirements",
            author="stitch",
            labels=["automerge", "dependencies"],
            changed_files=["offline_bundle/requirements.txt"],
        )
    )
    assert res_reqs.eligible is False
    assert res_reqs.risk_level == "sensitive"
    assert "automerge:blocked" in res_reqs.labels_to_add

    # Non-bot user cannot claim automated security patch exemption on uv.lock
    res_spoof = classify_pr(
        PRMetadata(
            pr_number=314,
            title="[Security] Patch certifi from 2026.7.22 to 2026.10.19",
            author="contributor",
            is_security_patch=True,
            labels=["security", "dependencies", "automerge"],
            changed_files=["uv.lock"],
            dependency_update_type="minor",
        )
    )
    assert res_spoof.eligible is False
    assert res_spoof.risk_level == "sensitive"
    assert "automerge:blocked" in res_spoof.labels_to_add


def test_classify_pr_workflow_verifies_maintainer_and_fetches_manifests_via_api():
    workflow_path = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "classify-pr.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["classify"]["steps"]

    checkout_step = next(s for s in steps if "actions/checkout@" in s.get("uses", ""))
    assert checkout_step["with"]["ref"] == "refs/heads/main"
    assert checkout_step["with"]["persist-credentials"] is False

    extract_step = next(s for s in steps if s.get("name") == "Extract PR Metadata")
    script = extract_step["with"]["script"]
    assert "github.rest.issues.listEvents" in script
    assert "github.rest.repos.getCollaboratorPermissionLevel" in script
    assert "maintainerPermissions" in script
    assert "isApprovedBot" in script
    assert "authorPermission" in script
    assert "author_permission" in script
    assert "author_is_bot" in script
    assert "is_fork" in script
    assert "github.rest.repos.getContent" in script
    assert "offline_bundle/requirements.txt" in script


def test_offline_requirements_manifest_requires_automerge_opt_in_for_security_patch():
    blocked_meta = PRMetadata(
        pr_number=206,
        title="[Security] Patch certifi from 2026.7.22 to 2026.10.19",
        author="sortify-remediation[bot]",
        is_security_patch=True,
        labels=["security", "dependencies", "severity:high", "priority:urgent"],
        changed_files=["uv.lock", "offline_bundle/requirements.txt"],
        additions=14,
        deletions=14,
        dependency_update_type="minor",
        all_commits_verified=True,
    )
    blocked_res = classify_pr(blocked_meta)
    assert blocked_res.eligible is False
    assert blocked_res.risk_level == "sensitive"
    assert "automerge:blocked" in blocked_res.labels_to_add
    assert any(
        "offline_bundle/requirements.txt" in r for r in blocked_res.blocking_reasons
    )

    eligible_meta = PRMetadata(
        pr_number=207,
        title="[Security] Patch certifi from 2026.7.22 to 2026.10.19",
        author="sortify-remediation[bot]",
        is_security_patch=True,
        labels=["security", "dependencies", "severity:high", "automerge"],
        changed_files=["uv.lock", "offline_bundle/requirements.txt"],
        additions=14,
        deletions=14,
        dependency_update_type="minor",
        all_commits_verified=True,
    )
    eligible_res = classify_pr(eligible_meta)
    assert eligible_res.eligible is True
    assert eligible_res.risk_level == "low"
    assert "automerge:eligible" in eligible_res.labels_to_add


def test_main_cli_fails_closed_when_docs_author_permission_missing(
    tmp_path, monkeypatch, capsys
):
    meta_path = tmp_path / "docs_meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "pr_number": 900,
                "title": "docs: unverified permission",
                "author": "fderuiter",
                "changed_files": ["docs/user_guide.md"],
                "additions": 3,
                "deletions": 1,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["classify_pr.py", "--json-input", str(meta_path)])
    main()
    result = json.loads(capsys.readouterr().out)
    assert result["eligible"] is False
    assert "automerge:blocked" in result["labels_to_add"]


_GROUPED_COMMIT_1109 = """build(deps): bump the python-minor-patch group with 7 updates

Updates `pypdf` from 7.0.1 to 7.1.0
Updates `pydantic-settings` from 2.16.2 to 2.16.3
Updates `transformers` from 5.22.2 to 5.23.0
Updates `onnxruntime` from 1.31.1 to 1.32.0
Updates `ruff` from 0.17.4 to 0.17.5
Updates `ty` from 0.0.100 to 0.0.103
Updates `tifffile` from 2026.11.8 to 2026.11.12

---
updated-dependencies:
- dependency-name: pypdf
  dependency-version: 7.1.0
  dependency-type: direct:production
  update-type: version-update:semver-minor
  dependency-group: python-minor-patch
- dependency-name: pydantic-settings
  dependency-version: 2.16.3
  dependency-type: direct:production
  update-type: version-update:semver-patch
  dependency-group: python-minor-patch
- dependency-name: transformers
  dependency-version: 5.23.0
  dependency-type: direct:production
  update-type: version-update:semver-minor
  dependency-group: python-minor-patch
- dependency-name: onnxruntime
  dependency-version: 1.32.0
  dependency-type: direct:production
  update-type: version-update:semver-minor
  dependency-group: python-minor-patch
- dependency-name: ruff
  dependency-version: 0.17.5
  dependency-type: direct:development
  update-type: version-update:semver-patch
  dependency-group: python-minor-patch
- dependency-name: ty
  dependency-version: 0.0.103
  dependency-type: direct:development
  update-type: version-update:semver-patch
  dependency-group: python-minor-patch
- dependency-name: tifffile
  dependency-version: 2026.11.12
  dependency-type: indirect
  update-type: version-update:semver-patch
  dependency-group: python-minor-patch
...

Signed-off-by: dependabot[bot] <support@github.com>
"""

_GROUPED_BODY_1109 = """Bumps the python-minor-patch group with 7 updates:

| Package | From | To |
| --- | --- | --- |
| [pypdf](repo) | `7.0.1` | `7.1.0` |
| [pydantic-settings](repo) | `2.16.2` | `2.16.3` |
| [transformers](repo) | `5.22.2` | `5.23.0` |
| [onnxruntime](repo) | `1.31.1` | `1.32.0` |
| [ruff](repo) | `0.17.4` | `0.17.5` |
| [ty](repo) | `0.0.100` | `0.0.103` |
| [tifffile](repo) | `2026.11.8` | `2026.11.12` |

Updates `pypdf` from 7.0.1 to 7.1.0
<details>
<summary>Release notes</summary>
Updates `pypdf` from 1.0.0 to 7.1.0
</details>
"""

_GROUPED_COMMIT_1134 = """build(deps): bump the python-minor-patch group across 1 directory with 2 updates

Updates `llama-cpp-python` from 0.3.36 to 0.3.37
Updates `ty` from 0.0.103 to 0.0.104

---
updated-dependencies:
- dependency-name: llama-cpp-python
  dependency-version: 0.3.37
  dependency-type: direct:production
  update-type: version-update:semver-patch
  dependency-group: python-minor-patch
- dependency-name: ty
  dependency-version: 0.0.104
  dependency-type: direct:development
  update-type: version-update:semver-patch
  dependency-group: python-minor-patch
...
"""

_GROUPED_COMMIT_1152 = """build(deps): bump the python-minor-patch group with 3 updates

Updates `ty` from 0.0.104 to 0.0.105
Updates `mpmath` from 1.3.0 to 1.4.1
Updates `narwhals` from 2.28.0 to 2.29.0

---
updated-dependencies:
- dependency-name: ty
  dependency-version: 0.0.105
  dependency-type: direct:development
  update-type: version-update:semver-patch
  dependency-group: python-minor-patch
- dependency-name: mpmath
  dependency-version: 1.4.1
  dependency-type: indirect
  update-type: version-update:semver-minor
  dependency-group: python-minor-patch
- dependency-name: narwhals
  dependency-version: '2.29.0'
  dependency-type: indirect
  update-type: version-update:semver-minor
  dependency-group: python-minor-patch
...
"""


def test_parse_dependabot_updated_dependencies_handles_grouped_and_malformed_blocks():
    parsed_1109 = parse_dependabot_updated_dependencies(_GROUPED_COMMIT_1109)
    assert parsed_1109 is not None
    assert len(parsed_1109) == 7
    assert parsed_1109[0]["dependency-name"] == "pypdf"
    assert parsed_1109[4]["dependency-name"] == "ruff"
    assert parsed_1109[4]["update-type"] == "version-update:semver-patch"

    parsed_1152 = parse_dependabot_updated_dependencies(_GROUPED_COMMIT_1152)
    assert parsed_1152 is not None
    assert len(parsed_1152) == 3
    assert parsed_1152[2]["dependency-version"] == "2.29.0"

    # Unclosed YAML trailer fails closed to None
    unclosed = (
        "Bump group\n\n---\nupdated-dependencies:\n"
        "- dependency-name: ruff\n  dependency-version: 0.17.5\n"
    )
    assert parse_dependabot_updated_dependencies(unclosed) is None

    # Missing dependency-version fails closed to None
    missing_ver = (
        "Bump group\n\n---\nupdated-dependencies:\n- dependency-name: ruff\n...\n"
    )
    assert parse_dependabot_updated_dependencies(missing_ver) is None


@pytest.mark.parametrize(
    ("title", "commit_msg", "pr_body"),
    [
        (
            "build(deps): bump the python-minor-patch group with 7 updates",
            _GROUPED_COMMIT_1109,
            _GROUPED_BODY_1109,
        ),
        (
            "build(deps): bump the python-minor-patch group across 1 directory with 2 updates",
            _GROUPED_COMMIT_1134,
            "Bumps the python-minor-patch group across 1 directory with 2 updates: [llama-cpp-python](repo) and [ty](repo).\n\nUpdates `llama-cpp-python` from 0.3.36 to 0.3.37\nUpdates `ty` from 0.0.103 to 0.0.104\n",
        ),
        (
            "build(deps): bump the python-minor-patch group with 3 updates",
            _GROUPED_COMMIT_1152,
            "",
        ),
    ],
)
def test_grouped_dependabot_minor_patch_metadata_classifies_eligible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    title: str,
    commit_msg: str,
    pr_body: str,
):
    assert (
        dependency_update_type_from_dependabot_metadata(
            [commit_msg], pr_body=pr_body, title=title
        )
        == "minor"
    )

    meta = PRMetadata(
        pr_number=1109,
        title=title,
        author="dependabot[bot]",
        is_dependabot=True,
        labels=["dependencies"],
        changed_files=["pyproject.toml"],
        additions=12,
        deletions=12,
        all_commits_verified=True,
        commit_messages=[commit_msg],
        pr_body=pr_body,
    )
    res = classify_pr(meta)
    assert res.eligible is True
    assert res.risk_level == "low"
    assert res.labels_to_add == ["risk:low", "automerge:eligible"]

    # Also verify CLI JSON input path
    payload_path = tmp_path / "grouped_meta.json"
    payload_path.write_text(
        json.dumps(
            {
                "pr_number": 1109,
                "title": title,
                "pr_body": pr_body,
                "commit_messages": [commit_msg],
                "author": "dependabot[bot]",
                "is_dependabot": True,
                "labels": ["dependencies"],
                "changed_files": ["pyproject.toml"],
                "additions": 12,
                "deletions": 12,
                "all_commits_verified": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "sys.argv", ["classify_pr.py", "--json-input", str(payload_path)]
    )
    main()
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["eligible"] is True
    assert cli_out["risk_level"] == "low"
    assert cli_out["labels_to_add"] == ["risk:low", "automerge:eligible"]


def test_grouped_dependabot_blocks_zero_x_minor_major_and_malformed_metadata():
    # 0.x minor bump in grouped update fails closed to major / blocked
    zero_x_minor_commit = """build(deps): bump the python-minor-patch group with 2 updates

Updates `ruff` from 0.17.5 to 0.18.0
Updates `ty` from 0.0.104 to 0.0.105

---
updated-dependencies:
- dependency-name: ruff
  dependency-version: 0.18.0
  dependency-type: direct:development
  update-type: version-update:semver-minor
  dependency-group: python-minor-patch
- dependency-name: ty
  dependency-version: 0.0.105
  dependency-type: direct:development
  update-type: version-update:semver-patch
  dependency-group: python-minor-patch
...
"""
    assert (
        dependency_update_type_from_dependabot_metadata(
            [zero_x_minor_commit],
            title="build(deps): bump the python-minor-patch group with 2 updates",
        )
        == "major"
    )

    # Major bump in grouped update fails closed to major / blocked
    major_commit = """build(deps): bump the python-minor-patch group with 2 updates

Updates `pypdf` from 7.1.0 to 8.0.0
Updates `ty` from 0.0.104 to 0.0.105

---
updated-dependencies:
- dependency-name: pypdf
  dependency-version: 8.0.0
  dependency-type: direct:production
  update-type: version-update:semver-major
  dependency-group: python-minor-patch
- dependency-name: ty
  dependency-version: 0.0.105
  dependency-type: direct:development
  update-type: version-update:semver-patch
  dependency-group: python-minor-patch
...
"""
    res_major = classify_pr(
        PRMetadata(
            pr_number=1201,
            title="build(deps): bump the python-minor-patch group with 2 updates",
            author="dependabot[bot]",
            is_dependabot=True,
            labels=["dependencies"],
            changed_files=["pyproject.toml"],
            commit_messages=[major_commit],
            all_commits_verified=True,
        )
    )
    assert res_major.eligible is False
    assert res_major.risk_level == "high"
    assert "automerge:blocked" in res_major.labels_to_add

    # Title says 3 updates, YAML only lists 2 -> count mismatch fails closed
    assert (
        dependency_update_type_from_dependabot_metadata(
            [_GROUPED_COMMIT_1134],
            title="build(deps): bump the python-minor-patch group with 3 updates",
        )
        == "major"
    )

    # Missing metadata entirely on grouped PR fails closed
    assert (
        resolve_dependency_update_type(
            title="build(deps): bump the python-minor-patch group with 2 updates",
            changed_files=["pyproject.toml"],
            is_dependabot=True,
            all_commits_verified=True,
            commit_messages=["chore: update deps without metadata"],
            pr_body="No structured metadata here.",
        )
        == "major"
    )

    # Unverified commit signature fails closed even if YAML looks benign
    assert (
        resolve_dependency_update_type(
            title="build(deps): bump the python-minor-patch group with 2 updates",
            changed_files=["pyproject.toml"],
            is_dependabot=True,
            all_commits_verified=False,
            commit_messages=[_GROUPED_COMMIT_1134],
        )
        == "major"
    )


def _load_classify_workflow_script(step_name: str) -> str:
    workflow_path = (
        Path(__file__).resolve().parents[1]
        / ".github"
        / "workflows"
        / "classify-pr.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["classify"]["steps"]
    step = next(s for s in steps if s.get("name") == step_name)
    return str(step["with"]["script"])


def _run_github_script_in_node(
    tmp_path: Path,
    script_body: str,
    harness_state: dict[str, Any],
) -> subprocess.CompletedProcess[str]:
    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.skip("node binary not available in test environment")

    state_file = tmp_path / "state.json"
    out_file = tmp_path / "out.json"
    runner_file = tmp_path / "runner.cjs"
    state_file.write_text(json.dumps(harness_state), encoding="utf-8")

    runner_source = (
        """
const fs = require('fs');
const state = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
const outPath = process.argv[3];
const calls = {
  removedLabels: [],
  addedLabels: [],
  statuses: [],
  summaryMarkdown: '',
};

function maybeThrow(endpoint) {
  const errSpec = (state.errors || {})[endpoint];
  if (!errSpec) return;
  if (errSpec.onlyLabel && errSpec.onlyLabel !== arguments[1]) return;
  const err = new Error(errSpec.message || endpoint + ' failed');
  err.status = errSpec.status;
  throw err;
}

const github = {
  paginate: async (fn, params) => {
    const res = await fn(params);
    return res.data;
  },
  rest: {
    pulls: {
      listFiles: async () => {
        maybeThrow('listFiles');
        return { data: state.files || [] };
      },
      listCommits: async () => {
        maybeThrow('listCommits');
        return { data: state.commits || [] };
      },
    },
    issues: {
      listLabelsOnIssue: async () => {
        maybeThrow('listLabelsOnIssue');
        return { data: (state.labels || []).map(name => ({ name })) };
      },
      listEvents: async () => {
        maybeThrow('listEvents');
        return { data: state.events || [] };
      },
      removeLabel: async ({ name }) => {
        maybeThrow('removeLabel', name);
        calls.removedLabels.push(name);
        return { data: {} };
      },
      addLabels: async ({ labels }) => {
        maybeThrow('addLabels');
        calls.addedLabels.push(...labels);
        return { data: {} };
      },
    },
    repos: {
      getCollaboratorPermissionLevel: async ({ username }) => {
        maybeThrow('getCollaboratorPermissionLevel');
        const perms = state.permissions || {};
        return { data: { permission: perms[username] || 'none' } };
      },
      getContent: async ({ path, ref }) => {
        maybeThrow('getContent');
        const key = `${ref}:${path}`;
        const files = state.rawFiles || {};
        if (!(key in files)) {
          const err = new Error('Not Found');
          err.status = 404;
          throw err;
        }
        return { data: files[key] };
      },
      createCommitStatus: async (payload) => {
        maybeThrow('createCommitStatus');
        calls.statuses.push(payload);
        return { data: {} };
      },
    },
  },
};

const context = {
  repo: { owner: 'fderuiter', repo: 'sortify' },
  payload: { pull_request: state.pr || {} },
};

const core = {
  summary: {
    addRaw: (md) => {
      calls.summaryMarkdown += md;
      return core.summary;
    },
    write: async () => {},
  },
};

(async () => {
"""
        + script_body
        + """
  fs.writeFileSync(outPath, JSON.stringify(calls));
})().catch((err) => {
  console.error(err && err.stack ? err.stack : String(err));
  process.exit(1);
});
"""
    )
    runner_file.write_text(runner_source, encoding="utf-8")
    return subprocess.run(
        [node_bin, str(runner_file), str(state_file), str(out_file)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )


def test_classify_workflow_extract_script_verifies_maintainer_on_all_prs_and_fails_on_api_errors(
    tmp_path: Path,
):
    extract_script = _load_classify_workflow_script("Extract PR Metadata")
    base_pr: dict[str, Any] = {
        "number": 1155,
        "title": "build(deps): bump group",
        "body": "Updates `ty` from 0.0.104 to 0.0.105",
        "draft": False,
        "user": {"login": "dependabot[bot]", "type": "Bot"},
        "base": {
            "sha": "base_sha_1",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
        "head": {
            "sha": "head_sha_1",
            "ref": "dependabot/pip/group",
            "repo": {"id": 1, "full_name": "fderuiter/sortify"},
        },
    }
    commits: list[dict[str, Any]] = [
        {
            "sha": "c1",
            "parents": [{"sha": "p0"}],
            "commit": {
                "message": _GROUPED_COMMIT_1152,
                "verification": {"verified": True},
            },
        },
        {
            "sha": "c2_merge",
            "parents": [{"sha": "c1"}, {"sha": "main_tip"}],
            "commit": {
                "message": "Merge branch 'main' into dependabot/pip/group",
                "verification": {"verified": True},
            },
        },
    ]

    # 1. Bot-applied 'automerge' (even by 'stitch' User account or on dependabot[bot] PR) is stripped
    for actor in (
        {"login": "dependabot[bot]", "type": "Bot"},
        {"login": "stitch", "type": "User"},
        {"login": "jules", "type": "User"},
        {"login": "reader", "type": "User"},
    ):
        proc = _run_github_script_in_node(
            tmp_path,
            extract_script,
            {
                "pr": base_pr,
                "files": [
                    {"filename": "pyproject.toml", "additions": 2, "deletions": 2}
                ],
                "commits": commits,
                "labels": ["dependencies", "automerge"],
                "events": [
                    {
                        "event": "labeled",
                        "label": {"name": "automerge"},
                        "actor": actor,
                    }
                ],
                "permissions": {"stitch": "write", "jules": "admin", "reader": "read"},
            },
        )
        assert proc.returncode == 0, proc.stderr
        extracted = json.loads((tmp_path / "pr_meta.json").read_text(encoding="utf-8"))
        assert "automerge" not in extracted["labels"]
        assert extracted["commit_messages"] == [_GROUPED_COMMIT_1152]
        assert extracted["pr_body"] == "Updates `ty` from 0.0.104 to 0.0.105"

    # 2. Human maintainer ('write', 'maintain', 'admin') 'automerge' is preserved
    proc_maint = _run_github_script_in_node(
        tmp_path,
        extract_script,
        {
            "pr": base_pr,
            "files": [{"filename": "uv.lock", "additions": 4, "deletions": 4}],
            "commits": commits,
            "labels": ["dependencies", "automerge"],
            "events": [
                {
                    "event": "labeled",
                    "label": {"name": "automerge"},
                    "actor": {"login": "fderuiter", "type": "User"},
                }
            ],
            "permissions": {"fderuiter": "maintain"},
            "rawFiles": {},  # 404 on getContent is tolerated and recorded as null
        },
    )
    assert proc_maint.returncode == 0, proc_maint.stderr
    extracted_maint = json.loads(
        (tmp_path / "pr_meta.json").read_text(encoding="utf-8")
    )
    assert "automerge" in extracted_maint["labels"]
    assert extracted_maint["base_uv_lock"] is None
    assert extracted_maint["head_uv_lock"] is None

    # 3. Non-404 REST error on listLabelsOnIssue or getContent fails the step
    for endpoint in ("listLabelsOnIssue", "getContent"):
        proc_err = _run_github_script_in_node(
            tmp_path,
            extract_script,
            {
                "pr": base_pr,
                "files": [{"filename": "uv.lock", "additions": 1, "deletions": 1}],
                "commits": commits,
                "labels": ["dependencies"],
                "errors": {endpoint: {"status": 500, "message": "Internal Error"}},
            },
        )
        assert proc_err.returncode != 0


def test_classify_workflow_sync_script_posts_completed_status_and_propagates_non_404_errors(
    tmp_path: Path,
):
    sync_script = _load_classify_workflow_script("Sync PR Labels & Post Status Check")
    pr_payload: dict[str, Any] = {
        "number": 1156,
        "head": {"sha": "head_sha_sync"},
    }
    res_file = tmp_path / "classification_result.json"

    # 1. Blocked PR posts state='success' (never 'pending') with 'Human review required (<risk>)'
    #    and ignores 404 when removing a missing label
    res_file.write_text(
        json.dumps(
            {
                "eligible": False,
                "risk_level": "high",
                "labels_to_add": ["risk:high", "automerge:blocked"],
                "blocking_reasons": ["Major dependency upgrades require human review."],
                "summary_markdown": "## Summary\n",
            }
        ),
        encoding="utf-8",
    )
    proc_blocked = _run_github_script_in_node(
        tmp_path,
        sync_script,
        {
            "pr": pr_payload,
            "labels": ["automerge:eligible", "risk:low", "dependencies"],
            "errors": {
                "removeLabel": {
                    "status": 404,
                    "onlyLabel": "risk:low",
                    "message": "Label does not exist",
                }
            },
        },
    )
    assert proc_blocked.returncode == 0, proc_blocked.stderr
    calls_blocked = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert calls_blocked["removedLabels"] == ["automerge:eligible"]
    assert calls_blocked["addedLabels"] == ["risk:high", "automerge:blocked"]
    assert len(calls_blocked["statuses"]) == 1
    assert calls_blocked["statuses"][0] == {
        "owner": "fderuiter",
        "repo": "sortify",
        "sha": "head_sha_sync",
        "state": "success",
        "context": "automerge/classification",
        "description": "Human review required (high)",
    }

    # 2. Eligible PR posts state='success' with 'Eligible for auto-merge'
    res_file.write_text(
        json.dumps(
            {
                "eligible": True,
                "risk_level": "low",
                "labels_to_add": ["risk:low", "automerge:eligible"],
                "blocking_reasons": [],
                "summary_markdown": "## Summary\n",
            }
        ),
        encoding="utf-8",
    )
    proc_eligible = _run_github_script_in_node(
        tmp_path,
        sync_script,
        {
            "pr": pr_payload,
            "labels": ["automerge:blocked", "risk:high"],
        },
    )
    assert proc_eligible.returncode == 0, proc_eligible.stderr
    calls_eligible = json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert calls_eligible["statuses"][0]["state"] == "success"
    assert calls_eligible["statuses"][0]["description"] == "Eligible for auto-merge"

    # 3. Non-404 REST errors on listLabelsOnIssue, removeLabel, addLabels, or createCommitStatus fail the run
    for endpoint in (
        "listLabelsOnIssue",
        "removeLabel",
        "addLabels",
        "createCommitStatus",
    ):
        proc_fail = _run_github_script_in_node(
            tmp_path,
            sync_script,
            {
                "pr": pr_payload,
                "labels": ["automerge:blocked"],
                "errors": {endpoint: {"status": 500, "message": "API failure"}},
            },
        )
        assert proc_fail.returncode != 0
