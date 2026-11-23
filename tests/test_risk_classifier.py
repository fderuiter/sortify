"""Tests for PR Risk Classifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts.classify_pr import (
    PROTECTED_PATHS,
    PRMetadata,
    classify_pr,
    classify_requirements_diff,
    classify_uv_lock_diff,
    classify_version_bump,
    dependency_update_type_from_diffs,
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
    assert checkout_step["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    assert checkout_step["with"]["persist-credentials"] is False

    extract_step = next(s for s in steps if s.get("name") == "Extract PR Metadata")
    script = extract_step["with"]["script"]
    assert "github.rest.issues.listEvents" in script
    assert "github.rest.repos.getCollaboratorPermissionLevel" in script
    assert "maintainerPermissions" in script
    assert "isApprovedBot" in script
    assert "github.rest.repos.getContent" in script
    assert "offline_bundle/requirements.txt" in script
