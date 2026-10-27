"""Tests for Automated CVE Patch Remediation Script."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.remediate_vulnerabilities import (
    GitHubRemediationClient,
    PackageVulnerability,
    VulnerabilityDetail,
    cvss_to_severity,
    extract_exemptions_from_security_md,
    fetch_advisory_severity_and_cvss,
    get_locked_package_version,
    normalize_severity,
    parse_audit_output,
    remediate_vulnerabilities,
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
    v1 = VulnerabilityDetail(vuln_id="GHSA-1", summary="Low vuln", severity="LOW", cvss_score=3.1)
    v2 = VulnerabilityDetail(vuln_id="GHSA-2", summary="Critical vuln", severity="CRITICAL", cvss_score=9.8)
    v3 = VulnerabilityDetail(vuln_id="GHSA-3", summary="High vuln", severity="HIGH", cvss_score=7.5)

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
def test_remediate_vulnerabilities_critical_pr_labels(mock_get_ver, mock_upgrade, mock_audit, tmp_path: Path):
    sample_output = """
certifi 2026.7.22 has 1 known vulnerability:

- GHSA-critical-1: Critical certifi vuln
  Severity: Critical (CVSS: 9.8)
  Fixed in: 2026.10.19
    """
    mock_audit.side_effect = [(1, sample_output), (0, "")]
    mock_upgrade.return_value = True
    mock_get_ver.side_effect = ["2026.7.22", "2026.10.19"]

    (tmp_path / "SECURITY.md").write_text("## Vulnerability Exemptions\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "certifi"\nversion = "2026.7.22"\n', encoding="utf-8")

    mock_gh_client = MagicMock()
    mock_gh_client.has_open_pr_for_package.return_value = False

    with patch("scripts.remediate_vulnerabilities.GitHubRemediationClient", return_value=mock_gh_client), \
         patch("subprocess.run") as mock_sub:
        mock_sub.return_value = MagicMock(returncode=0)
        remediate_vulnerabilities(repo_root=tmp_path, token="fake_tok", repo_full="owner/repo")

    mock_gh_client.create_pull_request.assert_called_once()
    kwargs = mock_gh_client.create_pull_request.call_args.kwargs
    assert "severity:critical" in kwargs["labels"]
    assert "priority:urgent" in kwargs["labels"]
    assert "automerge" not in kwargs["labels"]


@patch("scripts.remediate_vulnerabilities.run_uv_audit")
def test_remediate_vulnerabilities_unfixable_critical_issue_escalation(mock_audit, tmp_path: Path):
    sample_output = """
unfixable-pkg 1.0.0 has 1 known vulnerability:

- GHSA-unfixable-crit: Unfixable critical vulnerability
  Severity: Critical (CVSS: 9.9)
  Fixed in: N/A
    """
    mock_audit.return_value = (1, sample_output)

    (tmp_path / "SECURITY.md").write_text("## Vulnerability Exemptions\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text('[[package]]\nname = "unfixable-pkg"\nversion = "1.0.0"\n', encoding="utf-8")

    mock_gh_client = MagicMock()

    with patch("scripts.remediate_vulnerabilities.GitHubRemediationClient", return_value=mock_gh_client):
        remediate_vulnerabilities(repo_root=tmp_path, token="fake_tok", repo_full="owner/repo")

    mock_gh_client.report_security_issue.assert_called_once()
    kwargs = mock_gh_client.report_security_issue.call_args.kwargs
    assert "severity:critical" in kwargs["labels"]
    assert "priority:urgent" in kwargs["labels"]
    assert "@fderuiter/security-team" in kwargs["body"]



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
