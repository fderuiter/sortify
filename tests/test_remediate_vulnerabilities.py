"""Tests for Automated CVE Patch Remediation Script."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from scripts.auto_merge_controller import (
    AutoMergeAction,
    GitHubClient,
    process_auto_merge_queue,
)
from scripts.classify_pr import (
    PRMetadata,
    classify_pr,
    dependency_update_type_from_title,
)
from scripts.remediate_vulnerabilities import (
    MANIFEST_FILENAME,
    GitHubRemediationClient,
    PackageVulnerability,
    VulnerabilityDetail,
    build_sanitized_subprocess_env,
    create_github_app_jwt,
    cvss_to_severity,
    extract_exemptions_from_security_md,
    fetch_advisory_severity_and_cvss,
    format_security_pr_title,
    get_locked_package_version,
    is_license_compatible,
    main,
    mint_github_app_installation_token,
    normalize_severity,
    parse_audit_output,
    publish_staged_remediations,
    remediate_vulnerabilities,
    run_license_audit,
    run_local_tests,
    sanitize_sensitive_info,
)


def test_cvss_to_severity():
    assert cvss_to_severity(9.8) == "CRITICAL"
    assert cvss_to_severity(9.0) == "CRITICAL"
    assert cvss_to_severity(8.9) == "HIGH"
    assert cvss_to_severity(7.0) == "HIGH"
    assert cvss_to_severity(6.9) == "MEDIUM"
    assert cvss_to_severity(4.0) == "MEDIUM"
    assert cvss_to_severity(3.9) == "LOW"
    assert cvss_to_severity(0.0) == "LOW"
    assert cvss_to_severity(None) == "MEDIUM"


def test_normalize_severity():
    assert normalize_severity("critical") == "CRITICAL"
    assert normalize_severity("HIGH") == "HIGH"
    assert normalize_severity("moderate") == "MEDIUM"
    assert normalize_severity("low") == "LOW"
    assert normalize_severity("unknown") == "MEDIUM"
    assert normalize_severity(None) == "MEDIUM"


@patch("scripts.remediate_vulnerabilities.requests.get")
def test_fetch_advisory_severity_and_cvss_osv_success(mock_get):
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "database_specific": {
            "severity": "CRITICAL",
            "cvss": {"score": 9.8},
        }
    }
    mock_get.return_value = mock_resp

    sev, score = fetch_advisory_severity_and_cvss("GHSA-test-osv-1")
    assert sev == "CRITICAL"
    assert score == 9.8


@patch("scripts.remediate_vulnerabilities.requests.get")
def test_fetch_advisory_severity_and_cvss_fallback_on_error(mock_get):
    mock_get.side_effect = Exception("Connection timeout")

    sev, score = fetch_advisory_severity_and_cvss("GHSA-test-timeout-1")
    assert sev == "MEDIUM"
    assert score is None


def test_package_vulnerability_severity_and_cvss_score():
    v1 = VulnerabilityDetail(
        vuln_id="GHSA-1", summary="Low vuln", severity="LOW", cvss_score=3.1
    )
    v2 = VulnerabilityDetail(
        vuln_id="GHSA-2", summary="Critical vuln", severity="CRITICAL", cvss_score=9.8
    )
    v3 = VulnerabilityDetail(
        vuln_id="GHSA-3", summary="High vuln", severity="HIGH", cvss_score=7.5
    )

    pkg = PackageVulnerability(
        package_name="test-pkg",
        current_version="1.0.0",
        vulnerabilities=[v1, v2, v3],
    )

    assert pkg.severity == "CRITICAL"
    assert pkg.cvss_score == 9.8


@patch("scripts.remediate_vulnerabilities.fetch_advisory_severity_and_cvss")
def test_parse_audit_output_with_severity_and_cvss(mock_fetch):
    mock_fetch.return_value = ("MEDIUM", None)
    sample_output = """
certifi 2026.7.22 has 2 known vulnerabilities:

- GHSA-6q48-6c7w-9gw2: certifi critical flaw
  Severity: Critical (CVSS: 9.8)
  Fixed in: 2026.10.19
  Advisory information: https://example.com/adv1

- GHSA-1234-5678-9000: certifi low flaw
  Severity: Low (CVSS: 2.5)
  Fixed in: 2026.10.19
  Advisory information: https://example.com/adv2
    """
    packages = parse_audit_output(sample_output)
    assert len(packages) == 1
    pkg = packages[0]
    assert len(pkg.vulnerabilities) == 2
    assert pkg.vulnerabilities[0].severity == "CRITICAL"
    assert pkg.vulnerabilities[0].cvss_score == 9.8
    assert pkg.vulnerabilities[1].severity == "LOW"
    assert pkg.vulnerabilities[1].cvss_score == 2.5
    assert pkg.severity == "CRITICAL"


@patch("scripts.remediate_vulnerabilities.run_uv_audit")
@patch("scripts.remediate_vulnerabilities.run_lock_upgrade")
@patch("scripts.remediate_vulnerabilities.get_locked_package_version")
def test_remediate_vulnerabilities_critical_pr_labels(
    mock_get_ver, mock_upgrade, mock_audit, tmp_path: Path
):
    sample_output = """
certifi 2026.7.22 has 1 known vulnerability:

- GHSA-critical-1: Critical certifi vuln
  Severity: Critical (CVSS: 9.8)
  Fixed in: 2026.10.19
    """
    mock_audit.side_effect = [(1, sample_output), (0, "")]
    mock_upgrade.return_value = True
    mock_get_ver.side_effect = ["2026.7.22", "2026.10.19"]

    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n', encoding="utf-8"
    )

    mock_gh_client = MagicMock()
    mock_gh_client.has_open_pr_for_package.return_value = False

    with (
        patch(
            "scripts.remediate_vulnerabilities.GitHubRemediationClient",
            return_value=mock_gh_client,
        ),
        patch("subprocess.run") as mock_sub,
    ):
        mock_sub.return_value = MagicMock(returncode=0, stdout="6 passed", stderr="")
        remediate_vulnerabilities(
            repo_root=tmp_path, token="fake_tok", repo_full="owner/repo"
        )

    mock_gh_client.create_pull_request.assert_called_once()
    kwargs = mock_gh_client.create_pull_request.call_args.kwargs
    assert "severity:critical" in kwargs["labels"]
    assert "priority:urgent" in kwargs["labels"]
    assert "automerge" not in kwargs["labels"]


@patch("scripts.remediate_vulnerabilities.run_uv_audit")
@patch("scripts.remediate_vulnerabilities.run_lock_upgrade")
@patch("scripts.remediate_vulnerabilities.get_locked_package_version")
def test_remediate_vulnerabilities_low_and_medium_pr_omits_automerge_label(
    mock_get_ver, mock_upgrade, mock_audit, tmp_path: Path
):
    sample_output = """
certifi 2026.7.22 has 1 known vulnerability:

- GHSA-low-1: Low certifi vuln
  Severity: Low (CVSS: 3.1)
  Fixed in: 2026.10.19
    """
    mock_audit.side_effect = [(1, sample_output), (0, "")]
    mock_upgrade.return_value = True
    mock_get_ver.side_effect = ["2026.7.22", "2026.10.19"]

    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n', encoding="utf-8"
    )

    mock_gh_client = MagicMock()
    mock_gh_client.has_open_pr_for_package.return_value = False

    with (
        patch(
            "scripts.remediate_vulnerabilities.GitHubRemediationClient",
            return_value=mock_gh_client,
        ),
        patch("subprocess.run") as mock_sub,
    ):
        mock_sub.return_value = MagicMock(returncode=0, stdout="6 passed", stderr="")
        remediate_vulnerabilities(
            repo_root=tmp_path, token="fake_tok", repo_full="owner/repo"
        )

    mock_gh_client.create_pull_request.assert_called_once()
    kwargs = mock_gh_client.create_pull_request.call_args.kwargs
    assert "severity:low" in kwargs["labels"]
    assert "automerge" not in kwargs["labels"]


@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_remediate_vulnerabilities_unfixable_critical_issue_escalation(
    mock_audit, tmp_path: Path
):
    sample_output = """
unfixable-pkg 1.0.0 has 1 known vulnerability:

- GHSA-unfixable-crit: Unfixable critical vulnerability
  Severity: Critical (CVSS: 9.9)
  Fixed in: N/A
    """
    mock_audit.return_value = (1, sample_output)

    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "unfixable-pkg"\nversion = "1.0.0"\n', encoding="utf-8"
    )

    mock_gh_client = MagicMock()

    with patch(
        "scripts.remediate_vulnerabilities.GitHubRemediationClient",
        return_value=mock_gh_client,
    ):
        remediate_vulnerabilities(
            repo_root=tmp_path, token="fake_tok", repo_full="owner/repo"
        )

    mock_gh_client.report_security_issue.assert_called_once()
    kwargs = mock_gh_client.report_security_issue.call_args.kwargs
    assert "severity:critical" in kwargs["labels"]
    assert "priority:urgent" in kwargs["labels"]
    assert "@fderuiter" in kwargs["body"]
    assert "@fderuiter/security-team" not in kwargs["body"]
    assert "@fderuiter/maintainers" not in kwargs["body"]


def test_sanitize_sensitive_info():
    token = "secret_github_token_xyz123"
    raw_text = f"Failed pushing to https://x-access-token:{token}@github.com/owner/repo.git with token {token}"
    sanitized = sanitize_sensitive_info(raw_text, token=token)
    assert token not in sanitized
    assert "[REDACTED]" in sanitized
    assert "https://[REDACTED]:[REDACTED]@github.com/owner/repo.git" in sanitized


@patch("scripts.remediate_vulnerabilities.subprocess.run")
def test_run_local_tests(mock_run, tmp_path: Path):
    mock_run.return_value = MagicMock(returncode=0, stdout="6 passed", stderr="")
    code, output = run_local_tests(tmp_path)
    assert code == 0
    assert "6 passed" in output
    mock_run.assert_called_once()
    assert mock_run.call_args[0][0] == ["uv", "run", "pytest", "-m", "not slow"]


def test_extract_exemptions_from_security_md(tmp_path: Path):
    sec_file = tmp_path / "SECURITY.md"
    sec_file.write_text(
        """
# Security Policy

## Vulnerability Exemptions

| Vulnerability ID | Package | Affected Version | Status / Justification |
| --- | --- | --- | --- |
| `GHSA-w8v5-vhqr-4h9v` | `diskcache` | `5.6.3` | Unsafe pickle deserialization. |
| `CVE-2026-12345` | `foo` | `1.0.0` | Test exemption. |
        """,
        encoding="utf-8",
    )

    exemptions = extract_exemptions_from_security_md(sec_file)
    assert "GHSA-w8v5-vhqr-4h9v" in exemptions
    assert "CVE-2026-12345" in exemptions


def test_parse_audit_output():
    sample_output = """
certifi 2026.7.22 has 2 known vulnerabilities:

- GHSA-6q48-6c7w-9gw2: certifi bundles emSign root CAs with compromised intermediate CA keys

  Fixed in: 2026.10.19

  Advisory information: https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2

- PYSEC-2026-4320: certifi bundles emSign root CAs with compromised intermediate CA keys

  Fixed in: 2026.10.19

  Advisory information: https://github.com/advisories/GHSA-6q48-6c7w-9gw2
    """
    packages = parse_audit_output(sample_output)
    assert len(packages) == 1
    pkg = packages[0]
    assert pkg.package_name == "certifi"
    assert pkg.current_version == "2026.7.22"
    assert len(pkg.vulnerabilities) == 2
    assert pkg.is_fixable is True
    assert pkg.target_fix_version == "2026.10.19"


def test_parse_audit_output_unfixable():
    sample_output = """
vulnerable-lib 1.0.0 has 1 known vulnerability:

- GHSA-xxxx-yyyy-zzzz: Unfixable flaw in vulnerable-lib

  Fixed in: N/A

  Advisory information: https://example.com/advisory
    """
    packages = parse_audit_output(sample_output)
    assert len(packages) == 1
    pkg = packages[0]
    assert pkg.package_name == "vulnerable-lib"
    assert pkg.is_fixable is False


def test_get_locked_package_version(tmp_path: Path):
    lock_file = tmp_path / "uv.lock"
    lock_file.write_text(
        """
[[package]]
name = "certifi"
version = "2026.7.22"

[[package]]
name = "requests"
version = "2.32.3"
        """,
        encoding="utf-8",
    )

    ver = get_locked_package_version(lock_file, "certifi")
    assert ver == "2026.7.22"
    assert get_locked_package_version(lock_file, "nonexistent") is None


def test_github_remediation_client_duplicate_pr_check():
    client = GitHubRemediationClient("fake_token", "owner", "repo")
    client.list_open_prs = MagicMock(
        return_value=[
            {
                "number": 10,
                "title": "[Security] Patch certifi vulnerability to 2026.10.19",
                "head": {"ref": "security/patch-certifi-2026-10-19"},
                "labels": [{"name": "security"}, {"name": "dependencies"}],
            }
        ]
    )

    assert client.has_open_pr_for_package("certifi") is True
    assert client.has_open_pr_for_package("requests") is False


@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_remediate_vulnerabilities_dry_run(mock_audit, tmp_path: Path):
    sample_output = """
certifi 2026.7.22 has 1 known vulnerability:

- GHSA-6q48-6c7w-9gw2: test vuln

  Fixed in: 2026.10.19
    """
    mock_audit.return_value = (1, sample_output)

    # Create dummy SECURITY.md and uv.lock
    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n', encoding="utf-8"
    )

    res = remediate_vulnerabilities(repo_root=tmp_path, dry_run=True)
    assert res is True


@patch("scripts.remediate_vulnerabilities.subprocess.run")
@patch("scripts.remediate_vulnerabilities.run_local_tests")
@patch("scripts.remediate_vulnerabilities.get_locked_package_version")
@patch("scripts.remediate_vulnerabilities.run_lock_upgrade")
@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_remediate_vulnerabilities_test_failure(
    mock_audit,
    mock_lock_upgrade,
    mock_get_version,
    mock_run_tests,
    mock_sub_run,
    tmp_path: Path,
):
    sample_audit = """
certifi 2026.7.22 has 1 known vulnerability:

- GHSA-6q48-6c7w-9gw2: certifi vulnerability

  Fixed in: 2026.10.19
    """
    mock_audit.side_effect = [(1, sample_audit), (0, "Audit passed")]
    mock_lock_upgrade.return_value = True
    mock_get_version.side_effect = ["2026.7.22", "2026.10.19"]

    # Local tests fail
    fake_token = "secret_gh_token_1234"
    test_failure_log = f"FAILED tests/test_core.py - AssertionError: Token={fake_token}"
    mock_run_tests.return_value = (1, test_failure_log)

    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n', encoding="utf-8"
    )

    mock_client = MagicMock()
    mock_client.has_open_pr_for_package.return_value = False

    res = remediate_vulnerabilities(
        repo_root=tmp_path, dry_run=False, token=fake_token, repo_full="owner/repo"
    )

    assert res is False
    mock_run_tests.assert_called_once_with(tmp_path)

    # Verify git checkout uv.lock was called to revert changes
    git_checkout_calls = [
        call
        for call in mock_sub_run.call_args_list
        if call[0][0] == ["git", "checkout", "uv.lock"]
    ]
    assert len(git_checkout_calls) >= 1

    # Verify PR creation was skipped
    assert mock_client.create_pull_request.call_count == 0


@patch("scripts.remediate_vulnerabilities.GitHubRemediationClient")
@patch("scripts.remediate_vulnerabilities.subprocess.run")
@patch("scripts.remediate_vulnerabilities.run_local_tests")
@patch("scripts.remediate_vulnerabilities.get_locked_package_version")
@patch("scripts.remediate_vulnerabilities.run_lock_upgrade")
@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_remediate_vulnerabilities_test_success(
    mock_audit,
    mock_lock_upgrade,
    mock_get_version,
    mock_run_tests,
    mock_sub_run,
    mock_gh_client_cls,
    tmp_path: Path,
):
    sample_audit = """
certifi 2026.7.22 has 1 known vulnerability:

- GHSA-6q48-6c7w-9gw2: certifi vulnerability

  Fixed in: 2026.10.19
    """
    mock_audit.side_effect = [(1, sample_audit), (0, "Audit passed")]
    mock_lock_upgrade.return_value = True
    mock_get_version.side_effect = ["2026.7.22", "2026.10.19"]
    mock_run_tests.return_value = (0, "All 50 tests passed")

    mock_client = MagicMock()
    mock_client.has_open_pr_for_package.return_value = False
    mock_client.create_signed_branch.return_value = "signed_commit_sha_123"
    mock_client.create_pull_request.return_value = {"number": 42}
    mock_gh_client_cls.return_value = mock_client

    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n', encoding="utf-8"
    )

    res = remediate_vulnerabilities(
        repo_root=tmp_path, dry_run=False, token="dummy_token", repo_full="owner/repo"
    )

    assert res is True
    mock_run_tests.assert_called_once_with(tmp_path)
    mock_client.create_signed_branch.assert_called_once()
    branch_kwargs = mock_client.create_signed_branch.call_args.kwargs
    assert branch_kwargs["branch_name"] == "security/patch-certifi-2026-10-19"
    assert branch_kwargs["base_branch"] == "main"
    assert "uv.lock" in branch_kwargs["files"]

    mock_client.create_pull_request.assert_called_once()
    pr_kwargs = mock_client.create_pull_request.call_args.kwargs
    assert pr_kwargs["title"] == "[Security] Patch certifi from 2026.7.22 to 2026.10.19"
    assert pr_kwargs["head_branch"] == "security/patch-certifi-2026-10-19"

    # Unsigned local git commit / git push must never be invoked
    push_or_commit_calls = [
        call
        for call in mock_sub_run.call_args_list
        if len(call[0]) > 0 and call[0][0][:2] in (["git", "push"], ["git", "commit"])
    ]
    assert push_or_commit_calls == []


@patch("scripts.remediate_vulnerabilities.GitHubRemediationClient")
@patch("scripts.remediate_vulnerabilities.subprocess.run")
@patch("scripts.remediate_vulnerabilities.run_local_tests")
@patch("scripts.remediate_vulnerabilities.get_locked_package_version")
@patch("scripts.remediate_vulnerabilities.run_lock_upgrade")
@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_remediate_vulnerabilities_empty_token_omits_git_headers(
    mock_audit,
    mock_lock_upgrade,
    mock_get_version,
    mock_run_tests,
    mock_sub_run,
    mock_gh_client_cls,
    tmp_path: Path,
):
    sample_audit = """
certifi 2026.7.22 has 1 known vulnerability:

- GHSA-6q48-6c7w-9gw2: certifi vulnerability

  Fixed in: 2026.10.19
    """
    mock_audit.side_effect = [(1, sample_audit), (0, "Audit passed")]
    mock_lock_upgrade.return_value = True
    mock_get_version.side_effect = ["2026.7.22", "2026.10.19"]
    mock_run_tests.return_value = (0, "All 50 tests passed")

    mock_client = MagicMock()
    mock_client.has_open_pr_for_package.return_value = False
    mock_gh_client_cls.return_value = mock_client

    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n', encoding="utf-8"
    )

    with patch.dict(
        "os.environ",
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": "stale",
        },
    ):
        res = remediate_vulnerabilities(
            repo_root=tmp_path, dry_run=False, token="   ", repo_full="owner/repo"
        )

    assert res is False
    mock_client.create_pull_request.assert_not_called()
    for call in mock_sub_run.call_args_list:
        env = call.kwargs.get("env") or {}
        assert "GIT_CONFIG_COUNT" not in env
        assert "GIT_CONFIG_KEY_0" not in env
        assert "GIT_CONFIG_VALUE_0" not in env


@patch("scripts.remediate_vulnerabilities.GitHubRemediationClient")
@patch("scripts.remediate_vulnerabilities.subprocess.run")
@patch("scripts.remediate_vulnerabilities.run_local_tests")
@patch("scripts.remediate_vulnerabilities.get_locked_package_version")
@patch("scripts.remediate_vulnerabilities.run_lock_upgrade")
@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_remediate_vulnerabilities_push_failure_called_process_error(
    mock_audit,
    mock_lock_upgrade,
    mock_get_version,
    mock_run_tests,
    mock_sub_run,
    mock_gh_client_cls,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
):
    sample_audit = """
certifi 2026.7.22 has 1 known vulnerability:

- GHSA-6q48-6c7w-9gw2: certifi vulnerability

  Fixed in: 2026.10.19
    """
    mock_audit.side_effect = [(1, sample_audit), (0, "Audit passed")]
    mock_lock_upgrade.return_value = True
    mock_get_version.side_effect = ["2026.7.22", "2026.10.19"]
    mock_run_tests.return_value = (0, "All 50 tests passed")

    mock_client = MagicMock()
    mock_client.has_open_pr_for_package.return_value = False
    mock_client.create_signed_branch.return_value = None
    mock_gh_client_cls.return_value = mock_client

    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n', encoding="utf-8"
    )

    secret_token = "super_secret_pat_9999"
    mock_sub_run.return_value = MagicMock(
        returncode=0,
        stdout=json.dumps(
            [{"Name": "certifi", "Version": "2026.10.19", "License": "MPL-2.0"}]
        ),
        stderr="",
    )

    res = remediate_vulnerabilities(
        repo_root=tmp_path, dry_run=False, token=secret_token, repo_full="owner/repo"
    )

    assert res is False
    assert secret_token not in caplog.text
    mock_client.create_signed_branch.assert_called_once()
    mock_client.create_pull_request.assert_not_called()


# --- Unit Tests for License Verification and Enforcement ---


def test_is_license_compatible_approved():
    approved_licenses = [
        "MIT",
        "MIT License",
        "MIT-0",
        "MIT-CMU",
        "Apache-2.0",
        "Apache Software License",
        "Apache License 2.0",
        "BSD",
        "BSD License",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "0BSD",
        "GPL-3.0",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "GNU General Public License v3 (GPLv3)",
        "AGPL-3.0",
        "LGPL-3.0",
        "LGPL-2.1",
        "GNU Library or Lesser General Public License (LGPL)",
        "Python Software Foundation License",
        "PSF-2.0",
        "MPL-2.0",
        "Mozilla Public License 2.0 (MPL 2.0)",
        "zlib/libpng",
        "ISC",
        "CC0-1.0",
        "Unlicense",
    ]
    for lic in approved_licenses:
        ok, msg = is_license_compatible(lic)
        assert ok is True, f"Expected {lic!r} to be approved, got failure: {msg}"


def test_is_license_compatible_multi_license_expressions():
    # Disjunctive / dual licensing (approved if at least one branch is compatible)
    disjunctive_cases = [
        "Apache-2.0 OR BSD-3-Clause",
        "dual Apache and BSD",
        "Dual-license: Apache-2.0 or BSD-2-Clause",
        "dual Apache and Proprietary",
        "MIT / Apache-2.0",
    ]
    for expr in disjunctive_cases:
        ok, msg = is_license_compatible(expr)
        assert ok is True, (
            f"Expected disjunctive {expr!r} to be approved, got failure: {msg}"
        )

    # Conjunctive licensing (approved if all components are compatible)
    conjunctive_cases = [
        "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
        "DFSG approved; MIT License",
        "BSD-3-Clause, Apache-2.0, dependency licenses",
    ]
    for expr in conjunctive_cases:
        ok, msg = is_license_compatible(expr)
        assert ok is True, (
            f"Expected conjunctive {expr!r} to be approved, got failure: {msg}"
        )

    # Incompatible conjunctive expressions
    incompatible_expressions = [
        "Proprietary AND MIT",
        "dual Proprietary and Commercial",
    ]
    for expr in incompatible_expressions:
        ok, msg = is_license_compatible(expr)
        assert ok is False, (
            f"Expected incompatible {expr!r} to fail, got success: {msg}"
        )


def test_is_license_compatible_prohibited():
    prohibited_licenses = [
        "Proprietary",
        "Commercial",
        "Closed Source",
        "GPL-2.0-only",
        "GPL-2.0",
        "SSPL",
        "BUSL-1.1",
        "All Rights Reserved",
    ]
    for lic in prohibited_licenses:
        ok, msg = is_license_compatible(lic)
        assert ok is False, f"Expected prohibited {lic!r} to fail, got success: {msg}"


def test_is_license_compatible_unknown_and_missing():
    unknown_licenses = [
        "",
        None,
        "   ",
        "UNKNOWN",
        "Unknown",
        "None",
        "UNDEFINED",
        "NOT_FOUND",
        "N/A",
    ]
    for lic in unknown_licenses:
        ok, msg = is_license_compatible(lic)
        assert ok is False, (
            f"Expected unknown/missing {lic!r} to fail, got success: {msg}"
        )


@patch("scripts.remediate_vulnerabilities.subprocess.run")
def test_run_license_audit_approved(mock_sub_run, tmp_path: Path):
    mock_payload = [
        {"Name": "requests", "Version": "2.34.2", "License": "Apache Software License"},
        {"Name": "certifi", "Version": "2026.10.19", "License": "MPL-2.0"},
    ]
    mock_sub_run.return_value = MagicMock(
        returncode=0, stdout=json.dumps(mock_payload), stderr=""
    )

    ok, msg = run_license_audit(tmp_path, package_name="certifi")
    assert ok is True
    assert "compatible" in msg.lower()


@patch("scripts.remediate_vulnerabilities.subprocess.run")
def test_run_license_audit_prohibited(mock_sub_run, tmp_path: Path):
    mock_payload = [
        {"Name": "bad-lib", "Version": "1.0.0", "License": "Proprietary"},
    ]
    mock_sub_run.return_value = MagicMock(
        returncode=0, stdout=json.dumps(mock_payload), stderr=""
    )

    ok, msg = run_license_audit(tmp_path, package_name="bad-lib")
    assert ok is False
    assert "incompatible" in msg.lower() or "proprietary" in msg.lower()


@patch("scripts.remediate_vulnerabilities.subprocess.run")
def test_run_license_audit_unknown(mock_sub_run, tmp_path: Path):
    mock_payload = [
        {"Name": "mystery-lib", "Version": "1.0.0", "License": "UNKNOWN"},
    ]
    mock_sub_run.return_value = MagicMock(
        returncode=0, stdout=json.dumps(mock_payload), stderr=""
    )

    ok, msg = run_license_audit(tmp_path, package_name="mystery-lib")
    assert ok is False
    assert "missing" in msg.lower() or "unknown" in msg.lower()


@patch("scripts.remediate_vulnerabilities.subprocess.run")
def test_run_license_audit_missing_package_metadata(mock_sub_run, tmp_path: Path):
    mock_payload = [
        {"Name": "other-pkg", "Version": "1.0.0", "License": "MIT"},
    ]
    mock_sub_run.return_value = MagicMock(
        returncode=0, stdout=json.dumps(mock_payload), stderr=""
    )

    ok, msg = run_license_audit(tmp_path, package_name="uninstalled-pkg")
    assert ok is False
    assert "missing license metadata" in msg.lower()


@patch("scripts.remediate_vulnerabilities.GitHubRemediationClient")
@patch("scripts.remediate_vulnerabilities.subprocess.run")
@patch("scripts.remediate_vulnerabilities.run_license_audit")
@patch("scripts.remediate_vulnerabilities.run_local_tests")
@patch("scripts.remediate_vulnerabilities.get_locked_package_version")
@patch("scripts.remediate_vulnerabilities.run_lock_upgrade")
@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_remediate_vulnerabilities_incompatible_license_aborts_pr(
    mock_audit,
    mock_lock_upgrade,
    mock_get_version,
    mock_run_tests,
    mock_license_audit,
    mock_sub_run,
    mock_gh_client_cls,
    tmp_path: Path,
):
    sample_audit = """
certifi 2026.7.22 has 1 known vulnerability:

- GHSA-6q48-6c7w-9gw2: certifi vulnerability
  Severity: Critical (CVSS: 9.8)
  Fixed in: 2026.10.19
    """
    mock_audit.side_effect = [(1, sample_audit), (0, "Audit passed")]
    mock_lock_upgrade.return_value = True
    mock_get_version.side_effect = ["2026.7.22", "2026.10.19"]
    mock_run_tests.return_value = (0, "All tests passed")
    # Simulate license audit failure due to incompatible license
    mock_license_audit.return_value = (
        False,
        "Incompatible license 'Proprietary' detected for package 'certifi'",
    )

    mock_client = MagicMock()
    mock_client.has_open_pr_for_package.return_value = False
    mock_gh_client_cls.return_value = mock_client

    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n', encoding="utf-8"
    )

    res = remediate_vulnerabilities(
        repo_root=tmp_path, dry_run=False, token="tok", repo_full="owner/repo"
    )

    # Remediation should report failure
    assert res is False
    # PR should NOT be created
    mock_client.create_pull_request.assert_not_called()
    # Maintainer issue should be reported
    mock_client.report_security_issue.assert_called_once()
    issue_args = mock_client.report_security_issue.call_args.kwargs
    assert "Incompatible license" in issue_args["body"]
    # Lockfile changes should have been reverted
    revert_calls = [
        c
        for c in mock_sub_run.call_args_list
        if len(c[0]) > 0 and c[0][0] == ["git", "checkout", "uv.lock"]
    ]
    assert len(revert_calls) >= 1


@patch("scripts.remediate_vulnerabilities.GitHubRemediationClient")
@patch("scripts.remediate_vulnerabilities.subprocess.run")
@patch("scripts.remediate_vulnerabilities.run_license_audit")
@patch("scripts.remediate_vulnerabilities.run_local_tests")
@patch("scripts.remediate_vulnerabilities.get_locked_package_version")
@patch("scripts.remediate_vulnerabilities.run_lock_upgrade")
@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_remediate_vulnerabilities_compliant_license_generates_pr(
    mock_audit,
    mock_lock_upgrade,
    mock_get_version,
    mock_run_tests,
    mock_license_audit,
    mock_sub_run,
    mock_gh_client_cls,
    tmp_path: Path,
):
    sample_audit = """
certifi 2026.7.22 has 1 known vulnerability:

- GHSA-6q48-6c7w-9gw2: certifi vulnerability
  Severity: Critical (CVSS: 9.8)
  Fixed in: 2026.10.19
    """
    mock_audit.side_effect = [(1, sample_audit), (0, "Audit passed")]
    mock_lock_upgrade.return_value = True
    mock_get_version.side_effect = ["2026.7.22", "2026.10.19"]
    mock_run_tests.return_value = (0, "All tests passed")
    # License audit passes
    mock_license_audit.return_value = (
        True,
        "All package licenses compatible with GPL-3.0.",
    )

    mock_client = MagicMock()
    mock_client.has_open_pr_for_package.return_value = False
    mock_gh_client_cls.return_value = mock_client

    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n', encoding="utf-8"
    )

    res = remediate_vulnerabilities(
        repo_root=tmp_path, dry_run=False, token="tok", repo_full="owner/repo"
    )

    # Remediation should report success
    assert res is True
    # PR should be created
    mock_client.create_pull_request.assert_called_once()


# --- Unit & Integration Tests for Staged Audits, Token Stripping, and GitHub App Publish ---


@patch("scripts.remediate_vulnerabilities.subprocess.run")
def test_pytest_and_pip_licenses_strip_github_tokens_and_git_config(
    mock_sub_run, monkeypatch, tmp_path: Path
):
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret_write_token")
    monkeypatch.setenv("GH_TOKEN", "ghp_secret_personal_token")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "http.extraHeader")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "AUTHORIZATION: basic secret")
    monkeypatch.setenv("SAFE_VAR", "preserved_value")

    sanitized = build_sanitized_subprocess_env()
    assert "GITHUB_TOKEN" not in sanitized
    assert "GH_TOKEN" not in sanitized
    assert "GIT_CONFIG_COUNT" not in sanitized
    assert "GIT_CONFIG_KEY_0" not in sanitized
    assert "GIT_CONFIG_VALUE_0" not in sanitized
    assert sanitized["SAFE_VAR"] == "preserved_value"

    mock_sub_run.return_value = MagicMock(
        returncode=0,
        stdout=json.dumps(
            [{"Name": "certifi", "Version": "2026.10.19", "License": "MPL-2.0"}]
        ),
        stderr="",
    )

    code, _ = run_local_tests(tmp_path)
    assert code == 0
    pytest_env = mock_sub_run.call_args.kwargs["env"]
    assert "GITHUB_TOKEN" not in pytest_env
    assert "GH_TOKEN" not in pytest_env
    assert not any(k.startswith("GIT_CONFIG_") for k in pytest_env)
    assert pytest_env["PYTHON_KEYRING_BACKEND"] == "keyring.backends.fail.Keyring"

    mock_sub_run.reset_mock()
    ok, _ = run_license_audit(tmp_path, package_name="certifi")
    assert ok is True
    licenses_env = mock_sub_run.call_args.kwargs["env"]
    assert "GITHUB_TOKEN" not in licenses_env
    assert "GH_TOKEN" not in licenses_env
    assert not any(k.startswith("GIT_CONFIG_") for k in licenses_env)


@patch("scripts.remediate_vulnerabilities.GitHubRemediationClient")
@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_remediate_vulnerabilities_exits_zero_when_all_already_open(
    mock_audit, mock_gh_client_cls, tmp_path: Path, monkeypatch
):
    sample_audit = """
certifi 2026.7.22 has 1 known vulnerability:

- GHSA-6q48-6c7w-9gw2: certifi vulnerability
  Severity: High (CVSS: 7.5)
  Fixed in: 2026.10.19
    """
    mock_audit.return_value = (1, sample_audit)
    mock_client = MagicMock()
    mock_client.has_open_pr_for_package.return_value = True
    mock_gh_client_cls.return_value = mock_client

    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n', encoding="utf-8"
    )

    monkeypatch.setenv("GITHUB_TOKEN", "read_tok")
    monkeypatch.setenv("GITHUB_REPOSITORY", "fderuiter/sortify")

    # Should not raise SystemExit(1) when all vulnerable packages already have open PRs
    main(["--repo-root", str(tmp_path)])
    mock_client.create_pull_request.assert_not_called()
    mock_client.report_security_issue.assert_not_called()


@patch("scripts.remediate_vulnerabilities.GitHubRemediationClient")
@patch("scripts.remediate_vulnerabilities.subprocess.run")
@patch("scripts.remediate_vulnerabilities.run_license_audit")
@patch("scripts.remediate_vulnerabilities.run_local_tests")
@patch("scripts.remediate_vulnerabilities.run_lock_upgrade")
@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_stage_dir_writes_manifest_and_validated_lockfiles_without_publishing(
    mock_audit,
    mock_lock_upgrade,
    mock_run_tests,
    mock_license_audit,
    mock_sub_run,
    mock_gh_client_cls,
    tmp_path: Path,
):
    sample_audit = """
certifi 2026.7.22 has 1 known vulnerability:

- GHSA-6q48-6c7w-9gw2: certifi vulnerability
  Severity: High (CVSS: 8.1)
  Fixed in: 2026.10.19

urllib3 2.2.0 has 1 known vulnerability:

- GHSA-open-pr-1: urllib3 vulnerability
  Severity: Medium (CVSS: 5.4)
  Fixed in: 2.2.3
    """
    mock_audit.side_effect = [(1, sample_audit), (0, "")]

    uv_lock = tmp_path / "uv.lock"
    original_lock = (
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n\n'
        '[[package]]\nname = "urllib3"\nversion = "2.2.0"\n'
    )
    upgraded_lock = (
        '[[package]]\nname = "certifi"\nversion = "2026.10.19"\n\n'
        '[[package]]\nname = "urllib3"\nversion = "2.2.0"\n'
    )
    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    uv_lock.write_text(original_lock, encoding="utf-8")

    def fake_upgrade(pkg_name: str, root: Path) -> bool:
        assert pkg_name == "certifi"
        (root / "uv.lock").write_text(upgraded_lock, encoding="utf-8")
        return True

    mock_lock_upgrade.side_effect = fake_upgrade
    mock_run_tests.return_value = (0, "1800 passed")
    mock_license_audit.return_value = (True, "All compatible")
    mock_sub_run.return_value = MagicMock(returncode=0)

    mock_client = MagicMock()
    mock_client.has_open_pr_for_package.side_effect = lambda name: name == "urllib3"
    mock_gh_client_cls.return_value = mock_client

    stage_dir = tmp_path / "stage_out"
    ok = remediate_vulnerabilities(
        repo_root=tmp_path,
        token="readonly_tok",
        repo_full="fderuiter/sortify",
        stage_dir=stage_dir,
    )

    assert ok is True
    mock_client.create_signed_branch.assert_not_called()
    mock_client.create_pull_request.assert_not_called()
    mock_client.report_security_issue.assert_not_called()

    # Working tree lockfile was cleanly restored for isolation
    assert uv_lock.read_text(encoding="utf-8") == original_lock

    manifest = json.loads((stage_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert len(manifest["packages"]) == 2
    by_name = {p["package_name"]: p for p in manifest["packages"]}

    assert by_name["certifi"]["status"] == "staged"
    assert by_name["certifi"]["old_version"] == "2026.7.22"
    assert by_name["certifi"]["new_version"] == "2026.10.19"
    assert by_name["certifi"]["severity"] == "HIGH"
    staged_lock_rel = by_name["certifi"]["staged_files"]["uv.lock"]
    assert (stage_dir / staged_lock_rel).read_text(encoding="utf-8") == upgraded_lock

    assert by_name["urllib3"]["status"] == "already_open"
    assert by_name["urllib3"]["severity"] == "MEDIUM"


@patch("scripts.remediate_vulnerabilities.subprocess.run")
@patch("scripts.remediate_vulnerabilities.run_license_audit")
@patch("scripts.remediate_vulnerabilities.run_local_tests")
@patch("scripts.remediate_vulnerabilities.get_locked_package_version")
@patch("scripts.remediate_vulnerabilities.run_lock_upgrade")
@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_stage_dir_records_structured_severity_for_unfixable_test_and_license_failures(
    mock_audit,
    mock_lock_upgrade,
    mock_get_ver,
    mock_run_tests,
    mock_license_audit,
    mock_sub_run,
    tmp_path: Path,
):
    sample_audit = """
unfixable-lib 1.0.0 has 1 known vulnerability:

- GHSA-unfix-1: No upstream patch
  Severity: High (CVSS: 7.8)
  Fixed in: N/A

tornado 6.5.10 has 1 known vulnerability:

- GHSA-torn-1: Tornado cookie parsing
  Severity: Medium (CVSS: 5.3)
  Fixed in: 6.6

badlic-lib 2.0.0 has 1 known vulnerability:

- GHSA-lic-1: License switch
  Severity: Critical (CVSS: 9.4)
  Fixed in: 2.1.0
    """
    mock_audit.side_effect = [(1, sample_audit), (0, ""), (0, "")]
    mock_lock_upgrade.return_value = True
    # Calls: unfixable-lib old_version, tornado old/new, badlic-lib old/new
    mock_get_ver.side_effect = ["1.0.0", "6.5.10", "6.6", "2.0.0", "2.1.0"]
    mock_run_tests.side_effect = [
        (1, "FAILED tests/test_critical_enforcement_alert_banner.py"),
        (0, "passed"),
    ]
    mock_license_audit.return_value = (False, "Proprietary license")
    mock_sub_run.return_value = MagicMock(returncode=0)

    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")

    stage_dir = tmp_path / "stage_failures"
    ok = remediate_vulnerabilities(repo_root=tmp_path, stage_dir=stage_dir)
    assert ok is False

    manifest = json.loads((stage_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    by_name = {p["package_name"]: p for p in manifest["packages"]}

    assert by_name["unfixable-lib"]["status"] == "failed"
    assert by_name["unfixable-lib"]["failure_stage"] == "unfixable"
    assert by_name["unfixable-lib"]["severity"] == "HIGH"
    assert by_name["unfixable-lib"]["cvss_score"] == 7.8

    assert by_name["tornado"]["status"] == "failed"
    assert by_name["tornado"]["failure_stage"] == "tests"
    assert by_name["tornado"]["severity"] == "MEDIUM"
    assert by_name["tornado"]["cvss_score"] == 5.3

    assert by_name["badlic-lib"]["status"] == "failed"
    assert by_name["badlic-lib"]["failure_stage"] == "license"
    assert by_name["badlic-lib"]["severity"] == "CRITICAL"
    assert by_name["badlic-lib"]["cvss_score"] == 9.4


def _generate_test_rsa_pem() -> tuple[rsa.RSAPrivateKey, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    return key, pem


@patch("scripts.remediate_vulnerabilities.requests.post")
@patch("scripts.remediate_vulnerabilities.requests.get")
def test_create_github_app_jwt_and_mint_installation_token(mock_get, mock_post):
    key, pem = _generate_test_rsa_pem()
    jwt_token = create_github_app_jwt("123456", pem, now=1_700_000_000)
    parts = jwt_token.split(".")
    assert len(parts) == 3

    def _b64_decode(seg: str) -> bytes:
        pad = "=" * (-len(seg) % 4)
        return base64.urlsafe_b64decode(seg + pad)

    header = json.loads(_b64_decode(parts[0]))
    payload = json.loads(_b64_decode(parts[1]))
    signature = _b64_decode(parts[2])

    assert header == {"alg": "RS256", "typ": "JWT"}
    assert payload == {"iat": 1_699_999_940, "exp": 1_700_000_540, "iss": "123456"}

    # Verify cryptographic RS256 signature with the public key
    key.public_key().verify(
        signature,
        f"{parts[0]}.{parts[1]}".encode("ascii"),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )

    mock_get.return_value = MagicMock(status_code=200, json=lambda: {"id": 987654})
    mock_post.return_value = MagicMock(
        status_code=201, json=lambda: {"token": "ghs_app_bot_token_abc"}
    )

    escaped_pem = pem.replace("\n", "\\n")
    token = mint_github_app_installation_token(
        "123456", escaped_pem, "fderuiter", "sortify"
    )
    assert token == "ghs_app_bot_token_abc"
    assert mock_post.call_args.kwargs["json"] == {"repositories": ["sortify"]}


@patch("scripts.remediate_vulnerabilities.requests.post")
@patch("scripts.remediate_vulnerabilities.requests.get")
def test_git_data_api_signed_branch_and_publish_verifies_http_responses(
    mock_get, mock_post, tmp_path: Path
):
    _, pem = _generate_test_rsa_pem()

    stage_dir = tmp_path / "stage"
    pkg_dir = stage_dir / "packages" / "certifi"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "uv.lock").write_text(
        'name = "certifi"\nversion = "2026.10.19"\n', encoding="utf-8"
    )

    manifest = {
        "schema_version": 1,
        "base_branch": "main",
        "audit_clean": False,
        "audit_output": "certifi vuln",
        "packages": [
            {
                "package_name": "certifi",
                "old_version": "2026.7.22",
                "new_version": "2026.10.19",
                "severity": "HIGH",
                "cvss_score": 7.5,
                "status": "staged",
                "pr_labels": [
                    "security",
                    "dependencies",
                    "severity:high",
                    "priority:urgent",
                ],
                "branch_name": "security/patch-certifi-2026-10-19",
                "staged_files": {"uv.lock": "packages/certifi/uv.lock"},
                "vulnerabilities": [
                    {
                        "vuln_id": "GHSA-6q48-6c7w-9gw2",
                        "summary": "CA compromise",
                        "fixed_in": "2026.10.19",
                        "advisory_url": "https://github.com/advisories/GHSA-6q48-6c7w-9gw2",
                        "severity": "HIGH",
                        "cvss_score": 7.5,
                    }
                ],
            }
        ],
    }
    (stage_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")

    def fake_get(url: str, **kwargs):
        if url.endswith("/installation"):
            return MagicMock(status_code=200, json=lambda: {"id": 111})
        if "/pulls?" in url:
            return MagicMock(
                status_code=200, json=lambda: [], raise_for_status=lambda: None
            )
        if url.endswith("/git/ref/heads/main"):
            return MagicMock(
                status_code=200, json=lambda: {"object": {"sha": "base_sha_1"}}
            )
        if url.endswith("/git/commits/base_sha_1"):
            return MagicMock(
                status_code=200, json=lambda: {"tree": {"sha": "tree_sha_0"}}
            )
        raise AssertionError(f"Unexpected GET {url}")

    post_payloads: list[tuple[str, dict]] = []

    def fake_post_success(url: str, **kwargs):
        payload = kwargs.get("json") or {}
        post_payloads.append((url, payload))
        if url.endswith("/access_tokens"):
            return MagicMock(status_code=201, json=lambda: {"token": "ghs_minted_bot"})
        if url.endswith("/git/blobs"):
            return MagicMock(status_code=201, json=lambda: {"sha": "blob_sha_1"})
        if url.endswith("/git/trees"):
            return MagicMock(status_code=201, json=lambda: {"sha": "tree_sha_1"})
        if url.endswith("/git/commits"):
            return MagicMock(
                status_code=201,
                json=lambda: {
                    "sha": "signed_sha_1",
                    "verification": {"verified": True},
                },
            )
        if url.endswith("/git/refs"):
            return MagicMock(
                status_code=201,
                json=lambda: {"ref": "refs/heads/security/patch-certifi-2026-10-19"},
            )
        if url.endswith("/pulls"):
            return MagicMock(status_code=201, json=lambda: {"number": 77})
        if url.endswith("/issues/77/labels"):
            return MagicMock(status_code=200, json=lambda: [])
        raise AssertionError(f"Unexpected POST {url}")

    mock_get.side_effect = fake_get
    mock_post.side_effect = fake_post_success

    ok = publish_staged_remediations(
        stage_dir=stage_dir,
        repo_full="fderuiter/sortify",
        audit_job_status="success",
        app_id="12345",
        app_private_key=pem,
        require_app_token=True,
    )
    assert ok is True

    commit_calls = [p for url, p in post_payloads if url.endswith("/git/commits")]
    assert len(commit_calls) == 1
    assert "author" not in commit_calls[0]
    assert "committer" not in commit_calls[0]
    assert commit_calls[0]["parents"] == ["base_sha_1"]

    pr_calls = [p for url, p in post_payloads if url.endswith("/pulls")]
    assert len(pr_calls) == 1
    assert (
        pr_calls[0]["title"] == "[Security] Patch certifi from 2026.7.22 to 2026.10.19"
    )

    saved_manifest = json.loads(
        (stage_dir / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert saved_manifest["packages"][0]["status"] == "patched"


@pytest.mark.parametrize("failing_endpoint", ["/git/refs", "/pulls"])
@patch("scripts.remediate_vulnerabilities.requests.post")
@patch("scripts.remediate_vulnerabilities.requests.get")
def test_publish_exits_one_when_ref_creation_or_create_pull_request_fails(
    mock_get, mock_post, failing_endpoint: str, tmp_path: Path, monkeypatch
):
    stage_dir = tmp_path / "stage_fail"
    pkg_dir = stage_dir / "packages" / "certifi"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "uv.lock").write_text(
        'name = "certifi"\nversion = "2026.10.19"\n', encoding="utf-8"
    )

    manifest = {
        "schema_version": 1,
        "base_branch": "main",
        "audit_clean": False,
        "audit_output": "",
        "packages": [
            {
                "package_name": "certifi",
                "old_version": "2026.7.22",
                "new_version": "2026.10.19",
                "severity": "MEDIUM",
                "cvss_score": 5.0,
                "status": "staged",
                "pr_labels": ["security", "dependencies", "severity:medium"],
                "branch_name": "security/patch-certifi-2026-10-19",
                "staged_files": {"uv.lock": "packages/certifi/uv.lock"},
                "vulnerabilities": [],
            }
        ],
    }
    (stage_dir / MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")

    def fake_get(url: str, **kwargs):
        if "/pulls?" in url:
            return MagicMock(
                status_code=200, json=lambda: [], raise_for_status=lambda: None
            )
        if url.endswith("/git/ref/heads/main"):
            return MagicMock(
                status_code=200, json=lambda: {"object": {"sha": "base_sha_1"}}
            )
        if url.endswith("/git/commits/base_sha_1"):
            return MagicMock(
                status_code=200, json=lambda: {"tree": {"sha": "tree_sha_0"}}
            )
        raise AssertionError(f"Unexpected GET {url}")

    def fake_post(url: str, **kwargs):
        if url.endswith(failing_endpoint):
            return MagicMock(status_code=422, text="Validation Failed")
        if url.endswith("/git/blobs"):
            return MagicMock(status_code=201, json=lambda: {"sha": "blob_sha_1"})
        if url.endswith("/git/trees"):
            return MagicMock(status_code=201, json=lambda: {"sha": "tree_sha_1"})
        if url.endswith("/git/commits"):
            return MagicMock(status_code=201, json=lambda: {"sha": "signed_sha_1"})
        if url.endswith("/git/refs"):
            return MagicMock(
                status_code=201,
                json=lambda: {"ref": "refs/heads/security/patch-certifi-2026-10-19"},
            )
        raise AssertionError(f"Unexpected POST {url}")

    mock_get.side_effect = fake_get
    mock_post.side_effect = fake_post

    monkeypatch.setenv("GITHUB_TOKEN", "tok_123")
    monkeypatch.setenv("GITHUB_REPOSITORY", "fderuiter/sortify")
    monkeypatch.delenv("REMEDIATION_APP_ID", raising=False)
    monkeypatch.delenv("REMEDIATION_APP_PRIVATE_KEY", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        main(["--publish-from", str(stage_dir)])
    assert exc_info.value.code == 1

    saved_manifest = json.loads(
        (stage_dir / MANIFEST_FILENAME).read_text(encoding="utf-8")
    )
    assert saved_manifest["packages"][0]["status"] == "failed"


def test_generated_pr_title_satisfies_classify_pr_and_auto_merge_controller():
    minor_title = format_security_pr_title("certifi", "2026.7.22", "2026.10.19")
    major_title = format_security_pr_title("cryptography", "49.0.1", "50.0.0")

    assert minor_title == "[Security] Patch certifi from 2026.7.22 to 2026.10.19"
    assert dependency_update_type_from_title(minor_title) == "minor"
    assert dependency_update_type_from_title(major_title) == "major"

    # Without human automerge opt-in, security PR stays blocked for human review
    blocked_res = classify_pr(
        PRMetadata(
            pr_number=600,
            title=minor_title,
            author="sortify-remediation[bot]",
            labels=["security", "dependencies", "severity:high", "priority:urgent"],
            changed_files=["uv.lock"],
            additions=12,
            deletions=12,
            is_security_patch=True,
            dependency_update_type=dependency_update_type_from_title(minor_title),
            all_commits_verified=True,
        )
    )
    assert blocked_res.eligible is False

    # With human automerge opt-in and signed <app>[bot] commit, both classify_pr and auto_merge_controller pass
    client = MagicMock(spec=GitHubClient)
    client.list_open_pull_requests.return_value = [
        {
            "number": 601,
            "labels": [
                {"name": "automerge:eligible"},
                {"name": "security"},
                {"name": "dependencies"},
            ],
            "draft": False,
        }
    ]
    client.get_pull_request.return_value = {
        "number": 601,
        "title": minor_title,
        "draft": False,
        "state": "open",
        "head": {"sha": "signed_sha_601", "ref": "security/patch-certifi-2026-10-19"},
        "mergeable": True,
        "mergeable_state": "clean",
        "user": {"login": "sortify-remediation[bot]"},
        "labels": [
            {"name": "security"},
            {"name": "dependencies"},
            {"name": "severity:high"},
            {"name": "automerge"},
            {"name": "automerge:eligible"},
        ],
    }
    client.get_commit_verification.return_value = {"verified": True, "reason": "valid"}
    client.list_pull_request_files.return_value = [
        {"filename": "uv.lock", "additions": 10, "deletions": 10}
    ]
    client.list_pull_request_commits.return_value = [
        {"sha": "signed_sha_601", "commit": {"verification": {"verified": True}}}
    ]
    client.list_issue_events.return_value = [
        {
            "event": "labeled",
            "label": {"name": "automerge"},
            "actor": {"login": "fderuiter", "type": "User"},
        }
    ]
    client.get_collaborator_permission.return_value = "admin"
    client.get_required_ci_conclusion.return_value = "success"
    client.merge_pull_request.return_value = True

    merge_res = process_auto_merge_queue(client)
    assert merge_res.action == AutoMergeAction.MERGED
    assert merge_res.pr_id == 601


@patch("scripts.remediate_vulnerabilities.requests.patch")
@patch("scripts.remediate_vulnerabilities.requests.get")
@patch("scripts.remediate_vulnerabilities.subprocess.run")
@patch("scripts.remediate_vulnerabilities.run_local_tests")
@patch("scripts.remediate_vulnerabilities.get_locked_package_version")
@patch("scripts.remediate_vulnerabilities.run_lock_upgrade")
@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_issue_944_medium_failure_in_critical_banner_test_preserves_triage_labels(
    mock_audit,
    mock_lock_upgrade,
    mock_get_ver,
    mock_run_tests,
    mock_sub_run,
    mock_get,
    mock_patch,
    tmp_path: Path,
):
    """Regression test for Issue #944: 'critical' in pytest output must not override MEDIUM CVSS severity or strip triage labels."""
    sample_audit = """
tornado 6.5.10 has 1 known vulnerability:

- GHSA-tornado-med: Tornado cookie parsing issue
  Severity: Medium (CVSS: 5.3)
  Fixed in: 6.6
    """
    mock_audit.side_effect = [(1, sample_audit), (0, "")]
    mock_lock_upgrade.return_value = True
    mock_get_ver.side_effect = ["6.5.10", "6.6"]
    mock_run_tests.return_value = (
        1,
        "FAILED tests/test_critical_enforcement_alert_banner.py::"
        "test_verifier_differentiates_critical_collisions_and_warnings - "
        "AssertionError: CRITICAL FILE COLLISION has_critical_errors",
    )
    mock_sub_run.return_value = MagicMock(returncode=0)

    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "tornado"\nversion = "6.5.10"\n', encoding="utf-8"
    )

    stage_dir = tmp_path / "stage_944"
    stage_ok = remediate_vulnerabilities(repo_root=tmp_path, stage_dir=stage_dir)
    assert stage_ok is False

    # Simulate existing Issue #944 with human triage labels AND stale severity:critical
    existing_issue_944 = {
        "number": 944,
        "title": "[Security] Automated Dependency Audit Failure",
        "labels": [
            {"name": "bug"},
            {"name": "security"},
            {"name": "ready-for-human"},
            {"name": "needs-triage"},
            {"name": "severity:critical"},
            {"name": "priority:urgent"},
        ],
    }
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: [existing_issue_944],
        raise_for_status=lambda: None,
    )
    mock_patch.return_value = MagicMock(status_code=200, json=lambda: {"number": 944})

    pub_ok = publish_staged_remediations(
        stage_dir=stage_dir,
        repo_full="fderuiter/sortify",
        token="pub_tok",
        audit_job_status="failure",
    )
    assert pub_ok is False

    mock_patch.assert_called_once()
    patch_url = mock_patch.call_args.args[0]
    patch_json = mock_patch.call_args.kwargs["json"]
    assert patch_url.endswith("/issues/944")

    updated_labels = patch_json["labels"]
    assert "severity:medium" in updated_labels
    assert "severity:critical" not in updated_labels
    assert "priority:urgent" not in updated_labels
    assert "bug" in updated_labels
    assert "ready-for-human" in updated_labels
    assert "needs-triage" in updated_labels
    assert "security" in updated_labels

    # MEDIUM severity must NOT mention @fderuiter or invalid team handles
    assert "@fderuiter" not in patch_json["body"]


@patch("scripts.remediate_vulnerabilities.requests.patch")
@patch("scripts.remediate_vulnerabilities.requests.get")
def test_publish_cancelled_audit_job_updates_issue_944_preserving_labels(
    mock_get, mock_patch, tmp_path: Path
):
    existing_issue_944 = {
        "number": 944,
        "title": "[Security] Automated Dependency Audit Failure",
        "labels": [
            {"name": "bug"},
            {"name": "security"},
            {"name": "ready-for-human"},
        ],
    }
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: [existing_issue_944],
        raise_for_status=lambda: None,
    )
    mock_patch.return_value = MagicMock(status_code=200, json=lambda: {"number": 944})

    missing_stage_dir = tmp_path / "no_artifact_from_timeout"
    ok = publish_staged_remediations(
        stage_dir=missing_stage_dir,
        repo_full="fderuiter/sortify",
        token="pub_tok",
        audit_job_status="cancelled",
    )
    assert ok is False
    mock_patch.assert_called_once()
    payload = mock_patch.call_args.kwargs["json"]
    assert "bug" in payload["labels"]
    assert "ready-for-human" in payload["labels"]
    assert "severity:medium" in payload["labels"]
    assert "cancelled" in payload["body"]
