"""Tests for PR Risk Classifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.classify_pr import (
    PROTECTED_PATHS,
    PRMetadata,
    classify_pr,
    dependency_update_type_from_title,
    main,
)


def test_docs_only_pr_is_eligible():
    meta = PRMetadata(
        pr_number=101,
        title="docs: update installation instructions",
        author="fderuiter",
        changed_files=["docs/setup.md", "README.md"],
        additions=20,
        deletions=5,
    )
    res = classify_pr(meta)
    assert res.eligible is True
    assert res.risk_level == "low"
    assert "automerge:eligible" in res.labels_to_add
    assert "risk:low" in res.labels_to_add


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
    owned = {
        line.split()[0]
        for line in codeowners.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#") and "@fderuiter" in line
    }
    missing = [p for p in PROTECTED_PATHS if p not in owned]
    assert not missing, f"Add to .github/CODEOWNERS: {missing}"


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
