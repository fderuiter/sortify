"""Tests for PR Risk Classifier."""

from __future__ import annotations

from scripts.classify_pr import PRMetadata, classify_pr


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
        changed_files=["app/core/resilient_file_ops.py"],
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
