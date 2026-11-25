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
    _ADVISORY_CACHE,
    ALLOWED_STAGED_REPO_PATHS,
    MANIFEST_FILENAME,
    GitHubRemediationClient,
    PackageVulnerability,
    VulnerabilityDetail,
    build_failure_issue_payload,
    build_pr_labels_for_severity,
    build_sanitized_subprocess_env,
    create_github_app_jwt,
    cvss_to_severity,
    discover_targeted_tests,
    extract_exemptions_from_security_md,
    fetch_advisory_severity_and_cvss,
    format_security_pr_title,
    get_locked_package_version,
    is_license_compatible,
    main,
    mint_github_app_installation_token,
    normalize_severity,
    parse_audit_output,
    parse_cvss_vector_score,
    publish_staged_remediations,
    remediate_vulnerabilities,
    run_license_audit,
    run_local_tests,
    run_uv_audit,
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
    _ADVISORY_CACHE.clear()
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


def test_parse_cvss_vector_scores_across_versions():
    # CVE-2025-69872 (CVSS 3.1 Critical 9.8)
    assert (
        parse_cvss_vector_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == 9.8
    )
    # PYSEC-2026-3387 (CVSS 3.1 High 7.7 with Changed Scope)
    assert (
        parse_cvss_vector_score("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:N/A:N") == 7.7
    )
    # CVSS 3.0 vector
    assert (
        parse_cvss_vector_score("CVSS:3.0/AV:N/AC:H/PR:N/UI:R/S:U/C:H/I:N/A:N") == 5.3
    )
    # CVSS 4.0 vector
    v4_score = parse_cvss_vector_score(
        "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"
    )
    assert v4_score == 9.3
    # CVSS 2.0 vector
    assert parse_cvss_vector_score("AV:N/AC:L/Au:N/C:P/I:P/A:P") == 7.5
    assert parse_cvss_vector_score("not-a-vector") is None


@patch("scripts.remediate_vulnerabilities.requests.get")
def test_fetch_advisory_osv_vectors_and_alias_resolution_for_cve_and_pysec(mock_get):
    """Verify OSV CVSS vector parsing and alias traversal for CVE-2025-69872 and PYSEC-2026-3387."""
    _ADVISORY_CACHE.clear()
    osv_payloads = {
        "https://api.osv.dev/v1/vulns/CVE-2025-69872": {
            "id": "CVE-2025-69872",
            "aliases": ["GHSA-w8v5-vhqr-4h9v"],
            "severity": [
                {
                    "type": "CVSS_V3",
                    "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H",
                }
            ],
        },
        "https://api.osv.dev/v1/vulns/PYSEC-2026-3387": {
            "id": "PYSEC-2026-3387",
            "aliases": ["CVE-2026-49853", "GHSA-3x9g-8vmp-wqvf"],
            "severity": [
                {
                    "type": "CVSS_V3",
                    "score": "CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:N/A:N",
                }
            ],
        },
        "https://api.osv.dev/v1/vulns/GHSA-w8v5-vhqr-4h9v": {
            "id": "GHSA-w8v5-vhqr-4h9v",
            "aliases": ["CVE-2025-69872"],
            "database_specific": {"severity": "MODERATE"},
        },
    }

    def fake_get(url: str, **kwargs):
        if url in osv_payloads:
            return MagicMock(status_code=200, json=lambda u=url: osv_payloads[u])
        return MagicMock(status_code=404, json=dict)

    mock_get.side_effect = fake_get

    sev_cve, score_cve = fetch_advisory_severity_and_cvss("CVE-2025-69872")
    assert sev_cve == "CRITICAL"
    assert score_cve == 9.8
    assert "severity:critical" in build_pr_labels_for_severity(sev_cve)
    assert "priority:urgent" in build_pr_labels_for_severity(sev_cve)

    sev_pysec, score_pysec = fetch_advisory_severity_and_cvss("PYSEC-2026-3387")
    assert sev_pysec == "HIGH"
    assert score_pysec == 7.7
    assert "severity:high" in build_pr_labels_for_severity(sev_pysec)
    assert "priority:urgent" in build_pr_labels_for_severity(sev_pysec)
    _, _, issue_labels = build_failure_issue_payload(
        [{"package_name": "tornado", "severity": sev_pysec}], "audit"
    )
    assert "severity:high" in issue_labels
    assert "priority:urgent" in issue_labels

    # Alias traversal upgrades MODERATE GHSA record to CRITICAL 9.8 from aliased CVE-2025-69872
    _ADVISORY_CACHE.clear()
    sev_alias, score_alias = fetch_advisory_severity_and_cvss("GHSA-w8v5-vhqr-4h9v")
    assert sev_alias == "CRITICAL"
    assert score_alias == 9.8


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
            repo_root=tmp_path,
            token="fake_tok",
            repo_full="owner/repo",
            license_auditor=lambda _root, _pkg: (True, "MPL-2.0"),
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
            repo_root=tmp_path,
            token="fake_tok",
            repo_full="owner/repo",
            license_auditor=lambda _root, _pkg: (True, "MPL-2.0"),
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
    mock_run_tests.assert_called_once_with(
        tmp_path,
        package_name="certifi",
        baseline_uv_lock='[[package]]\nname = "certifi"\nversion = "2026.7.22"\n',
        baseline_pyproject=None,
    )

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
        repo_root=tmp_path,
        dry_run=False,
        token="dummy_token",
        repo_full="owner/repo",
        license_auditor=lambda _root, _pkg: (True, "MPL-2.0"),
    )

    assert res is True
    mock_run_tests.assert_called_once_with(
        tmp_path,
        package_name="certifi",
        baseline_uv_lock='[[package]]\nname = "certifi"\nversion = "2026.7.22"\n',
        baseline_pyproject=None,
    )
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
            repo_root=tmp_path,
            dry_run=False,
            token="   ",
            repo_full="owner/repo",
            license_auditor=lambda _root, _pkg: (True, "MPL-2.0"),
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
    mock_sub_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    res = remediate_vulnerabilities(
        repo_root=tmp_path,
        dry_run=False,
        token=secret_token,
        repo_full="owner/repo",
        license_auditor=lambda _root, _pkg: (True, "MPL-2.0"),
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
        "(MIT OR Apache-2.0) AND BSD-3-Clause",
        "BSL-1.0",
        "Apache-2.0 WITH LLVM-exception",
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
        "Apache-2.0 AND CNRI-Python",
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
        "(MIT OR Apache-2.0) AND Proprietary",
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
        "GPLv2",
        "GNU General Public License v2 (GPLv2)",
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


def test_run_license_audit_approved_with_dotted_names_and_build_exclusion(
    tmp_path: Path,
):
    (tmp_path / "uv.lock").write_text(
        """
[[package]]
name = "smart-autosorter"
version = "0.1.0"
source = { virtual = "." }
dependencies = [
    { name = "certifi" },
    { name = "jaraco-classes" },
]

[package.dev-dependencies]
dev = [{ name = "pytest" }]
notebooks = [{ name = "tornado" }]
build = [{ name = "pyinstaller" }]

[[package]]
name = "certifi"
version = "2026.10.19"

[[package]]
name = "jaraco.classes"
version = "3.4.0"

[[package]]
name = "pytest"
version = "9.0.0"

[[package]]
name = "tornado"
version = "6.5.5"

[[package]]
name = "pyinstaller"
version = "6.19.0"
dependencies = [{ name = "altgraph" }]

[[package]]
name = "altgraph"
version = "0.17.5"
        """,
        encoding="utf-8",
    )
    licenses = {
        ("certifi", "2026.10.19"): "MPL-2.0",
        ("jaraco.classes", "3.4.0"): "MIT",
        ("pytest", "9.0.0"): "MIT",
        ("tornado", "6.5.5"): "Apache-2.0",
        ("pyinstaller", "6.19.0"): "GPLv2-or-later with PyInstaller-exception",
        ("altgraph", "0.17.5"): "GPL-2.0-only",
    }

    ok, msg = run_license_audit(
        tmp_path,
        package_name="jaraco-classes",
        license_resolver=lambda name, ver, _pkg: licenses.get((name, ver)),
    )
    assert ok is True
    assert "compatible" in msg.lower()

    # Uninstalled group dependency (e.g. tornado when running under --group dev) also passes
    ok_t, _ = run_license_audit(
        tmp_path,
        package_name="tornado",
        license_resolver=lambda name, ver, _pkg: licenses.get((name, ver)),
    )
    assert ok_t is True


def test_run_license_audit_prohibited(tmp_path: Path):
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "bad-lib"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    ok, msg = run_license_audit(
        tmp_path,
        package_name="bad-lib",
        license_resolver=lambda _n, _v, _p: "Proprietary",
    )
    assert ok is False
    assert "incompatible" in msg.lower() or "proprietary" in msg.lower()


def test_run_license_audit_unknown(tmp_path: Path):
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "mystery-lib"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    ok, msg = run_license_audit(
        tmp_path,
        package_name="mystery-lib",
        license_resolver=lambda _n, _v, _p: "UNKNOWN",
    )
    assert ok is False
    assert "missing" in msg.lower() or "unknown" in msg.lower()


def test_run_license_audit_missing_package_metadata(tmp_path: Path):
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "other-pkg"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    ok, msg = run_license_audit(
        tmp_path,
        package_name="uninstalled-pkg",
        license_resolver=lambda _n, _v, _p: "MIT",
    )
    assert ok is False
    assert "missing license metadata" in msg.lower()


@patch("scripts.remediate_vulnerabilities._get_metadata_session")
@patch("scripts.remediate_vulnerabilities._build_installed_license_map")
def test_run_license_audit_pep658_wheel_metadata_for_uninstalled_locked_pkg(
    mock_installed, mock_session_fn, tmp_path: Path
):
    """Uninstalled packages in uv.lock resolve license headers via PEP 658 .metadata URLs."""
    from scripts.remediate_vulnerabilities import _LOCKED_LICENSE_CACHE

    _LOCKED_LICENSE_CACHE.clear()
    mock_installed.return_value = {}
    (tmp_path / "uv.lock").write_text(
        """
[[package]]
name = "tornado"
version = "6.5.5"
wheels = [
    { url = "https://files.pythonhosted.org/packages/tornado-6.5.5-cp39-abi3-manylinux1_x86_64.whl" }
]
        """,
        encoding="utf-8",
    )
    fake_session = MagicMock()
    fake_session.get.return_value = MagicMock(
        status_code=200,
        text="Metadata-Version: 2.4\nName: tornado\nVersion: 6.5.5\nLicense-Expression: Apache-2.0\n",
    )
    mock_session_fn.return_value = fake_session

    ok, msg = run_license_audit(tmp_path, package_name="tornado")
    assert ok is True
    assert "compatible" in msg.lower()
    fake_session.get.assert_called_once()
    assert fake_session.get.call_args.args[0].endswith(".whl.metadata")


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
    mock_sub_run.return_value = MagicMock(
        returncode=0,
        stdout='{"vulnerabilities": []}',
        stderr="warning: `uv audit` is experimental and may change without warning",
    )
    audit_rc, audit_out = run_uv_audit(["GHSA-w8v5-vhqr-4h9v"], tmp_path)
    assert audit_rc == 0
    assert audit_out == '{"vulnerabilities": []}'
    audit_env = mock_sub_run.call_args.kwargs["env"]
    assert "GITHUB_TOKEN" not in audit_env
    assert "GH_TOKEN" not in audit_env
    assert not any(k.startswith("GIT_CONFIG_") for k in audit_env)


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


@patch("scripts.remediate_vulnerabilities.fetch_advisory_severity_and_cvss")
@patch("scripts.remediate_vulnerabilities.subprocess.run")
def test_run_uv_audit_json_separates_stderr_warnings_and_parses_structured_output(
    mock_sub_run, mock_fetch, tmp_path: Path
):
    """uv audit --output-format json must ignore experimental stderr warnings when parsing stdout."""
    mock_fetch.return_value = ("HIGH", 7.7)
    structured_json = json.dumps(
        {
            "vulnerabilities": [
                {
                    "dependency": {"name": "tornado", "version": "6.5.4"},
                    "id": "PYSEC-2026-3387",
                    "aliases": ["CVE-2026-49853", "GHSA-3x9g-8vmp-wqvf"],
                    "description": "Tornado HTTP header parsing flaw\nExtra details",
                    "fix_versions": ["6.5.5"],
                    "link": "https://osv.dev/vulnerability/PYSEC-2026-3387",
                }
            ]
        }
    )
    mock_sub_run.return_value = MagicMock(
        returncode=1,
        stdout=structured_json,
        stderr="warning: `uv audit` is experimental and may change without warning.\nwarning: `--ignore-until-fixed` is experimental",
    )

    rc, stdout_payload = run_uv_audit(["GHSA-w8v5-vhqr-4h9v"], tmp_path)
    assert rc == 1
    cmd = mock_sub_run.call_args.args[0]
    assert cmd[:5] == ["uv", "audit", "--output-format", "json", "--color"]
    assert "warning:" not in stdout_payload

    parsed = parse_audit_output(stdout_payload)
    assert len(parsed) == 1
    assert parsed[0].package_name == "tornado"
    assert parsed[0].current_version == "6.5.4"
    assert parsed[0].target_fix_version == "6.5.5"
    assert parsed[0].severity == "HIGH"
    assert parsed[0].cvss_score == 7.7
    mock_fetch.assert_called_once_with(
        "PYSEC-2026-3387", aliases=("CVE-2026-49853", "GHSA-3x9g-8vmp-wqvf")
    )


def test_discover_targeted_tests_maps_reverse_dependencies_and_skips_unrelated_tui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import scripts.remediate_vulnerabilities as rem_mod

    (tmp_path / "app" / "core").mkdir(parents=True)
    (tmp_path / "app" / "ui").mkdir(parents=True)
    (tmp_path / "tests").mkdir(parents=True)

    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "tornado"\nversion = "6.5.5"\n\n'
        '[[package]]\nname = "jaraco-classes"\nversion = "3.4.0"\n\n'
        '[[package]]\nname = "keyring"\nversion = "25.7.0"\n'
        'dependencies = [{ name = "jaraco-classes" }]\n',
        encoding="utf-8",
    )
    (tmp_path / "app" / "core" / "crypto.py").write_text(
        "import keyring\n", encoding="utf-8"
    )
    (tmp_path / "app" / "ui" / "banner.py").write_text(
        "from textual.widgets import Static\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_crypto.py").write_text(
        "from app.core import crypto\n\ndef test_key(): pass\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_critical_enforcement_alert_banner.py").write_text(
        "from app.ui import banner\n\ndef test_banner(): pass\n", encoding="utf-8"
    )

    rem_mod._scan_repo_ast_imports.cache_clear()
    real_collect = rem_mod._collect_ast_imports
    ast_scan_calls = 0

    def counting_collect(py_path: Path) -> tuple[set[str], set[str]]:
        nonlocal ast_scan_calls
        ast_scan_calls += 1
        return real_collect(py_path)

    monkeypatch.setattr(rem_mod, "_collect_ast_imports", counting_collect)

    # Unused package maps to zero tests without pulling in unrelated TUI tests
    tornado_tests = discover_targeted_tests(tmp_path, "tornado")
    assert "tests/test_critical_enforcement_alert_banner.py" not in tornado_tests
    assert tornado_tests == []
    first_scan_calls = ast_scan_calls
    assert first_scan_calls == 4

    # jaraco-classes -> keyring -> app/core/crypto.py -> targeted crypto tests (memoized AST scan)
    jaraco_tests = discover_targeted_tests(tmp_path, "jaraco-classes")
    assert jaraco_tests == ["tests/test_crypto.py"]
    assert "tests/test_critical_enforcement_alert_banner.py" not in jaraco_tests
    assert ast_scan_calls == first_scan_calls
    assert rem_mod._scan_repo_ast_imports.cache_info().hits >= 1

    # Updating uv.lock reverse dependencies reflects immediately while AST cache stays hot
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "tornado"\nversion = "6.5.5"\n\n'
        '[[package]]\nname = "keyring"\nversion = "25.7.0"\n'
        '[package.optional-dependencies]\nextra = [{ name = "tornado" }]\n',
        encoding="utf-8",
    )
    assert discover_targeted_tests(tmp_path, "tornado") == ["tests/test_crypto.py"]
    assert ast_scan_calls == first_scan_calls


@patch("scripts.remediate_vulnerabilities.subprocess.run")
def test_run_local_tests_ignores_preexisting_baseline_failure_and_catches_new_regression(
    mock_sub_run, tmp_path: Path
):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "mod.py").write_text("import certifi\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_mod.py").write_text(
        "from app import mod\ndef test_preexisting(): pass\ndef test_new_reg(): pass\n",
        encoding="utf-8",
    )
    baseline_lock = '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n'
    upgraded_lock = '[[package]]\nname = "certifi"\nversion = "2026.10.19"\n'
    (tmp_path / "uv.lock").write_text(upgraded_lock, encoding="utf-8")

    def write_junit(cmd: list[str], failing_names: list[str]) -> None:
        for arg in cmd:
            if arg.startswith("--junitxml="):
                xml_path = Path(arg.split("=", 1)[1])
                cases = "".join(
                    f'<testcase classname="tests.test_mod" name="{n}"><failure message="boom"/></testcase>'
                    for n in failing_names
                )
                xml_path.write_text(
                    f'<testsuite tests="{len(failing_names)}">{cases}</testsuite>',
                    encoding="utf-8",
                )

    # Case 1: Same failure exists in both upgrade and baseline -> returns 0 (ignored) and cleans pytest_failures.txt
    def fake_run_preexisting(cmd, **kwargs):
        if cmd[:3] == ["uv", "run", "python"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        (tmp_path / "pytest_failures.txt").write_text(
            "stale failure\n", encoding="utf-8"
        )
        write_junit(cmd, ["test_preexisting"])
        return MagicMock(returncode=1, stdout="1 failed", stderr="")

    mock_sub_run.side_effect = fake_run_preexisting
    rc_ok, msg_ok = run_local_tests(
        tmp_path, package_name="certifi", baseline_uv_lock=baseline_lock
    )
    assert rc_ok == 0
    assert "Pre-existing baseline test failures ignored" in msg_ok
    assert (tmp_path / "uv.lock").read_text(encoding="utf-8") == upgraded_lock
    assert not (tmp_path / "pytest_failures.txt").exists()

    # Case 2: Upgrade introduces a new failure that passes on baseline -> returns non-zero
    call_idx = {"pytest": 0}

    def fake_run_regression(cmd, **kwargs):
        if cmd[:3] == ["uv", "run", "python"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        call_idx["pytest"] += 1
        if call_idx["pytest"] == 1:
            write_junit(cmd, ["test_new_reg"])
            return MagicMock(returncode=1, stdout="FAILED test_new_reg", stderr="")
        write_junit(cmd, [])
        return MagicMock(returncode=0, stdout="1 passed", stderr="")

    mock_sub_run.side_effect = fake_run_regression
    rc_fail, msg_fail = run_local_tests(
        tmp_path, package_name="certifi", baseline_uv_lock=baseline_lock
    )
    assert rc_fail == 1
    assert "FAILED test_new_reg" in msg_fail


@patch("scripts.remediate_vulnerabilities.subprocess.run")
@patch("scripts.remediate_vulnerabilities.sync_offline_requirements_manifest")
@patch("scripts.remediate_vulnerabilities.run_local_tests")
@patch("scripts.remediate_vulnerabilities.run_lock_upgrade")
@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_stage_and_publish_commits_uv_lock_and_offline_requirements_atomically(
    mock_audit,
    mock_upgrade,
    mock_run_tests,
    mock_sync_offline,
    mock_sub_run,
    tmp_path: Path,
):
    assert "offline_bundle/requirements.txt" in ALLOWED_STAGED_REPO_PATHS
    audit_json = json.dumps(
        {
            "vulnerabilities": [
                {
                    "dependency": {"name": "certifi", "version": "2026.7.22"},
                    "id": "GHSA-6q48-6c7w-9gw2",
                    "summary": "CA issue",
                    "fix_versions": ["2026.10.19"],
                    "severity": "HIGH",
                    "cvss_score": 7.5,
                }
            ]
        }
    )
    mock_audit.side_effect = [(1, audit_json), (0, '{"vulnerabilities": []}')]

    orig_lock = '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n'
    new_lock = '[[package]]\nname = "certifi"\nversion = "2026.10.19"\n'
    orig_offline = "certifi==2026.7.22 --hash=sha256:" + ("1" * 64) + "\n"
    new_offline = "certifi==2026.10.19 --hash=sha256:" + ("2" * 64) + "\n"

    (tmp_path / "SECURITY.md").write_text(
        "## Vulnerability Exemptions\n", encoding="utf-8"
    )
    (tmp_path / "uv.lock").write_text(orig_lock, encoding="utf-8")
    (tmp_path / "offline_bundle").mkdir()
    (tmp_path / "offline_bundle" / "requirements.txt").write_text(
        orig_offline, encoding="utf-8"
    )

    def do_upgrade(_pkg: str, root: Path) -> bool:
        (root / "uv.lock").write_text(new_lock, encoding="utf-8")
        return True

    def do_sync(root: Path) -> tuple[bool, str]:
        (root / "offline_bundle" / "requirements.txt").write_text(
            new_offline, encoding="utf-8"
        )
        return True, "synced"

    mock_upgrade.side_effect = do_upgrade
    mock_sync_offline.side_effect = do_sync
    mock_run_tests.return_value = (0, "ok")
    mock_sub_run.return_value = MagicMock(returncode=0)

    stage_dir = tmp_path / "stage_atomic"
    ok = remediate_vulnerabilities(
        repo_root=tmp_path,
        stage_dir=stage_dir,
        license_auditor=lambda _r, _p: (True, "MPL-2.0"),
    )
    assert ok is True

    # Working tree files must be restored cleanly without drift
    assert (tmp_path / "uv.lock").read_text(encoding="utf-8") == orig_lock
    assert (tmp_path / "offline_bundle" / "requirements.txt").read_text(
        encoding="utf-8"
    ) == orig_offline

    manifest = json.loads((stage_dir / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    staged_files = manifest["packages"][0]["staged_files"]
    assert set(staged_files.keys()) == {"uv.lock", "offline_bundle/requirements.txt"}
    assert (stage_dir / staged_files["uv.lock"]).read_text(encoding="utf-8") == new_lock
    assert (stage_dir / staged_files["offline_bundle/requirements.txt"]).read_text(
        encoding="utf-8"
    ) == new_offline


@patch("scripts.remediate_vulnerabilities.requests.patch")
@patch("scripts.remediate_vulnerabilities.requests.post")
@patch("scripts.remediate_vulnerabilities.requests.get")
def test_clean_publish_auto_closes_resolved_issue_944_preserving_triage_labels(
    mock_get, mock_post, mock_patch, tmp_path: Path
):
    stage_dir = tmp_path / "clean_stage"
    stage_dir.mkdir()
    clean_manifest = {
        "schema_version": 1,
        "base_branch": "main",
        "audit_clean": True,
        "audit_output": '{"vulnerabilities": []}',
        "packages": [],
    }
    (stage_dir / MANIFEST_FILENAME).write_text(
        json.dumps(clean_manifest), encoding="utf-8"
    )

    existing_issue_944 = {
        "number": 944,
        "title": "[Security] Automated Dependency Audit Failure",
        "labels": [
            {"name": "bug"},
            {"name": "security"},
            {"name": "ready-for-human"},
            {"name": "severity:high"},
            {"name": "priority:urgent"},
        ],
    }
    unrelated_security_issue = {
        "number": 950,
        "title": "Harden SQLCipher key rotation UX",
        "labels": [{"name": "security"}, {"name": "enhancement"}],
    }
    mock_get.return_value = MagicMock(
        status_code=200,
        json=lambda: [existing_issue_944, unrelated_security_issue],
        raise_for_status=lambda: None,
    )
    mock_post.return_value = MagicMock(status_code=201, json=lambda: {"id": 1})
    mock_patch.return_value = MagicMock(status_code=200, json=lambda: {"number": 944})

    ok = publish_staged_remediations(
        stage_dir=stage_dir,
        repo_full="fderuiter/sortify",
        token="app_tok",
        audit_job_status="success",
    )
    assert ok is True

    # Resolution comment posted only on #944
    mock_post.assert_called_once()
    assert mock_post.call_args.args[0].endswith("/issues/944/comments")

    # Issue #944 closed with preserved human triage labels and stripped stale severity/urgent labels
    mock_patch.assert_called_once()
    assert mock_patch.call_args.args[0].endswith("/issues/944")
    patch_payload = mock_patch.call_args.kwargs["json"]
    assert patch_payload["state"] == "closed"
    assert patch_payload["labels"] == ["bug", "security", "ready-for-human"]
