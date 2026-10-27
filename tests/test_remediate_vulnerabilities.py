"""Tests for Automated CVE Patch Remediation Script."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.remediate_vulnerabilities import (
    GitHubRemediationClient,
    extract_exemptions_from_security_md,
    get_locked_package_version,
    parse_audit_output,
    remediate_vulnerabilities,
    run_local_tests,
    sanitize_sensitive_info,
)


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

    (tmp_path / "SECURITY.md").write_text("## Vulnerability Exemptions\n", encoding="utf-8")
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
    mock_gh_client_cls.return_value = mock_client

    (tmp_path / "SECURITY.md").write_text("## Vulnerability Exemptions\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text(
        '[[package]]\nname = "certifi"\nversion = "2026.7.22"\n', encoding="utf-8"
    )

    res = remediate_vulnerabilities(
        repo_root=tmp_path, dry_run=False, token="dummy_token", repo_full="owner/repo"
    )

    assert res is True
    mock_run_tests.assert_called_once_with(tmp_path)
    assert mock_client.create_pull_request.call_count == 1

