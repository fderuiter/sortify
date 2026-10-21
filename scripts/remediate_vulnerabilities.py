"""Automated Dependency CVE Patch Remediation Script for Sortify.

Parses vulnerability reports from `uv audit`, updates lockfiles for fixable package
vulnerabilities via `uv lock --upgrade-package <package>`, creates security branches
and pull requests with appropriate security/dependencies/automerge labels, and notifies
maintainers via issues when unfixable vulnerabilities or CI failures occur.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)


@dataclass
class VulnerabilityDetail:
    vuln_id: str
    summary: str
    fixed_in: Optional[str] = None
    advisory_url: Optional[str] = None


@dataclass
class PackageVulnerability:
    package_name: str
    current_version: str
    vulnerabilities: List[VulnerabilityDetail] = field(default_factory=list)

    @property
    def target_fix_version(self) -> Optional[str]:
        for v in self.vulnerabilities:
            if (
                v.fixed_in
                and v.fixed_in.strip()
                and v.fixed_in.strip().lower() != "n/a"
            ):
                return v.fixed_in.strip()
        return None

    @property
    def is_fixable(self) -> bool:
        return self.target_fix_version is not None


def extract_exemptions_from_security_md(security_md_path: Path) -> List[str]:
    """Parse SECURITY.md for exempted vulnerability IDs under Vulnerability Exemptions."""
    if not security_md_path.is_file():
        return []

    text = security_md_path.read_text(encoding="utf-8")
    exemptions: List[str] = []

    # Locate Vulnerability Exemptions section
    match = re.search(
        r"##\s+Vulnerability Exemptions\s*\n(.*?)(?=\n##|\Z)", text, re.DOTALL
    )
    if not match:
        return exemptions

    section = match.group(1)
    # Find GHSA, CVE, or PYSEC IDs in table rows
    id_matches = re.findall(
        r"\b(GHSA-[a-zA-Z0-9-]+|CVE-\d+-\d+|PYSEC-\d+-\d+)\b", section
    )
    for vid in id_matches:
        if vid not in exemptions:
            exemptions.append(vid)

    return exemptions


def run_uv_audit(exemptions: List[str], repo_root: Path) -> Tuple[int, str]:
    """Execute uv audit ignoring exempted vulnerability IDs and return (exit_code, output)."""
    cmd = ["uv", "audit", "--color", "never"]
    for ex in exemptions:
        cmd.extend(["--ignore-until-fixed", ex])

    try:
        res = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        combined_output = (res.stdout + "\n" + res.stderr).strip()
        return res.returncode, combined_output
    except Exception as e:
        logger.error(f"Failed to run uv audit: {e}")
        return 1, str(e)


def parse_audit_output(output: str) -> List[PackageVulnerability]:
    """Parse uv audit text output into a list of PackageVulnerability objects.

    Sample uv audit output format:
    certifi 2026.7.22 has 2 known vulnerabilities:

    - GHSA-6q48-6c7w-9gw2: certifi bundles emSign root CAs with compromised intermediate CA keys

      Fixed in: 2026.10.19

      Advisory information: https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2
    """
    packages: List[PackageVulnerability] = []
    lines = output.splitlines()

    current_pkg: Optional[PackageVulnerability] = None
    current_vuln: Optional[VulnerabilityDetail] = None

    pkg_header_re = re.compile(
        r"^\s*([a-zA-Z0-9_\-]+)\s+([0-9a-zA-Z\.\-]+)\s+has\s+\d+\s+known\s+vulnerabilit(?:y|ies):",
        re.IGNORECASE,
    )
    vuln_header_re = re.compile(r"^\s*-\s+([a-zA-Z0-9_\-]+):\s*(.*)$")
    fixed_in_re = re.compile(r"^\s*Fixed in:\s*(.*)$", re.IGNORECASE)
    advisory_re = re.compile(r"^\s*Advisory information:\s*(.*)$", re.IGNORECASE)

    for line in lines:
        pkg_match = pkg_header_re.match(line)
        if pkg_match:
            if current_pkg:
                if current_vuln:
                    current_pkg.vulnerabilities.append(current_vuln)
                    current_vuln = None
                packages.append(current_pkg)
            current_pkg = PackageVulnerability(
                package_name=pkg_match.group(1),
                current_version=pkg_match.group(2),
            )
            continue

        if current_pkg:
            vuln_match = vuln_header_re.match(line)
            if vuln_match:
                if current_vuln:
                    current_pkg.vulnerabilities.append(current_vuln)
                current_vuln = VulnerabilityDetail(
                    vuln_id=vuln_match.group(1),
                    summary=vuln_match.group(2).strip(),
                )
                continue

            if current_vuln:
                fixed_match = fixed_in_re.match(line)
                if fixed_match:
                    current_vuln.fixed_in = fixed_match.group(1).strip()
                    continue

                adv_match = advisory_re.match(line)
                if adv_match:
                    current_vuln.advisory_url = adv_match.group(1).strip()
                    continue

    if current_pkg:
        if current_vuln:
            current_pkg.vulnerabilities.append(current_vuln)
        packages.append(current_pkg)

    return packages


def get_locked_package_version(uv_lock_path: Path, package_name: str) -> Optional[str]:
    """Parse uv.lock to find the locked version of package_name."""
    if not uv_lock_path.is_file():
        return None

    content = uv_lock_path.read_text(encoding="utf-8")
    # Search for [[package]] block with name = "package_name"
    pattern = rf'\[\[package\]\]\s*\nname\s*=\s*"{re.escape(package_name)}"\s*\nversion\s*=\s*"([^"]+)"'
    match = re.search(pattern, content)
    if match:
        return match.group(1)
    return None


def run_lock_upgrade(package_name: str, repo_root: Path) -> bool:
    """Execute `uv lock --upgrade-package <package>`."""
    cmd = ["uv", "lock", "--upgrade-package", package_name]
    logger.info(f"Executing: {' '.join(cmd)}")
    res = subprocess.run(
        cmd, cwd=repo_root, capture_output=True, text=True, check=False
    )
    if res.returncode != 0:
        logger.error(f"Failed to upgrade package {package_name}: {res.stderr}")
        return False
    return True


class GitHubRemediationClient:
    """GitHub API client for checking duplicate PRs, creating PRs, and posting issues."""

    def __init__(self, token: str, owner: str, repo: str):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.base_url = f"https://api.github.com/repos/{owner}/{repo}"
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        }

    def list_open_prs(self) -> List[Dict[str, Any]]:
        url = f"{self.base_url}/pulls?state=open&per_page=100"
        resp = requests.get(url, headers=self.headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def has_open_pr_for_package(self, package_name: str) -> bool:
        """Check if an open PR or security branch already exists for this package."""
        try:
            prs = self.list_open_prs()
            for pr in prs:
                head_ref = pr.get("head", {}).get("ref", "")
                title = pr.get("title", "")
                if f"security/patch-{package_name.lower()}" in head_ref.lower():
                    return True
                if (
                    f"patch {package_name.lower()}" in title.lower()
                    or f"bump {package_name.lower()}" in title.lower()
                ):
                    if "security" in [lbl.get("name") for lbl in pr.get("labels", [])]:
                        return True
            return False
        except Exception as e:
            logger.warning(f"Error checking existing PRs for {package_name}: {e}")
            return False

    def create_pull_request(
        self,
        title: str,
        head_branch: str,
        base_branch: str,
        body: str,
        labels: List[str],
    ) -> Optional[Dict[str, Any]]:
        url = f"{self.base_url}/pulls"
        data = {
            "title": title,
            "head": head_branch,
            "base": base_branch,
            "body": body,
        }
        resp = requests.post(url, headers=self.headers, json=data, timeout=30)
        if resp.status_code not in (200, 201):
            logger.error(f"Failed to create PR: {resp.status_code} {resp.text}")
            return None
        pr_data = resp.json()
        pr_number = pr_data.get("number")

        if pr_number and labels:
            labels_url = f"{self.base_url}/issues/{pr_number}/labels"
            requests.post(
                labels_url, headers=self.headers, json={"labels": labels}, timeout=30
            )

        return pr_data

    def report_security_issue(
        self, title: str, body: str, label_name: str = "security"
    ) -> None:
        """Create or update a maintainer issue when unfixable vulnerabilities occur."""
        try:
            issues_url = (
                f"{self.base_url}/issues?state=open&labels={label_name}&per_page=100"
            )
            resp = requests.get(issues_url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            open_issues = resp.json()

            existing = None
            for issue in open_issues:
                if issue.get(
                    "title"
                ) == title or "Automated Dependency Audit Failure" in issue.get(
                    "title", ""
                ):
                    existing = issue
                    break

            if existing:
                update_url = f"{self.base_url}/issues/{existing['number']}"
                requests.patch(
                    update_url, headers=self.headers, json={"body": body}, timeout=30
                )
                logger.info(f"Updated existing security issue #{existing['number']}")
            else:
                create_url = f"{self.base_url}/issues"
                payload = {"title": title, "body": body, "labels": [label_name]}
                requests.post(
                    create_url, headers=self.headers, json=payload, timeout=30
                )
                logger.info(f"Created new security issue: {title}")
        except Exception as e:
            logger.error(f"Failed to report security issue: {e}")


def remediate_vulnerabilities(
    repo_root: Path,
    dry_run: bool = False,
    token: Optional[str] = None,
    repo_full: Optional[str] = None,
) -> bool:
    """Main remediation handler."""
    security_md = repo_root / "SECURITY.md"
    uv_lock = repo_root / "uv.lock"

    exemptions = extract_exemptions_from_security_md(security_md)
    logger.info(f"Parsed exempted vulnerabilities from SECURITY.md: {exemptions}")

    exit_code, audit_output = run_uv_audit(exemptions, repo_root)
    if exit_code == 0:
        logger.info("No unexempted vulnerabilities found by uv audit.")
        return True

    logger.warning("Unexempted vulnerabilities detected by uv audit. Parsing report...")
    vuln_packages = parse_audit_output(audit_output)

    if not vuln_packages:
        logger.warning(
            "uv audit failed but no packages parsed from output. Raw output:\n"
            + audit_output
        )

    gh_client: Optional[GitHubRemediationClient] = None
    if token and repo_full:
        parts = repo_full.split("/")
        if len(parts) == 2:
            gh_client = GitHubRemediationClient(token, parts[0], parts[1])

    unfixable_reports: List[str] = []
    patched_any = False

    for pkg in vuln_packages:
        package_name = pkg.package_name
        logger.info(
            f"Processing vulnerable package: {package_name} ({pkg.current_version})"
        )

        if not pkg.is_fixable:
            logger.warning(f"No fix version available for package {package_name}.")
            unfixable_reports.append(
                f"- **{package_name}** ({pkg.current_version}): No upstream fix version available."
            )
            continue

        if gh_client and gh_client.has_open_pr_for_package(package_name):
            logger.info(
                f"Open PR already exists for {package_name}. Skipping duplicate PR creation."
            )
            continue

        old_version = (
            get_locked_package_version(uv_lock, package_name) or pkg.current_version
        )

        if dry_run:
            logger.info(
                f"[DRY-RUN] Would upgrade {package_name} from {old_version} to target fix version {pkg.target_fix_version}"
            )
            patched_any = True
            continue

        # Execute uv lock upgrade
        success = run_lock_upgrade(package_name, repo_root)
        if not success:
            unfixable_reports.append(
                f"- **{package_name}** ({old_version}): Upgrade via `uv lock` failed."
            )
            continue

        new_version = get_locked_package_version(uv_lock, package_name)
        if not new_version or new_version == old_version:
            logger.warning(f"`uv lock` did not change version for {package_name}.")
            unfixable_reports.append(
                f"- **{package_name}** ({old_version}): Lockfile remained at version {old_version} after upgrade attempt."
            )
            continue

        logger.info(
            f"Successfully updated {package_name} in uv.lock: {old_version} -> {new_version}"
        )

        # Verify whether audit passes for this package after lock upgrade
        post_exit, post_output = run_uv_audit(exemptions, repo_root)
        post_vulnerabilities = parse_audit_output(post_output) if post_exit != 0 else []
        still_vulnerable = any(
            p.package_name == package_name for p in post_vulnerabilities
        )

        if still_vulnerable:
            logger.warning(
                f"Package {package_name} remains vulnerable after upgrading to {new_version}."
            )
            unfixable_reports.append(
                f"- **{package_name}** ({old_version} -> {new_version}): Vulnerability still present post-upgrade."
            )
            # Revert uv.lock change
            subprocess.run(["git", "checkout", "uv.lock"], cwd=repo_root, check=False)
            continue

        patched_any = True

        # Generate Git branch and PR
        branch_name = (
            f"security/patch-{package_name.lower()}-{new_version.replace('.', '-')}"
        )
        pr_title = f"[Security] Patch {package_name} vulnerability to {new_version}"

        vuln_ids = ", ".join([v.vuln_id for v in pkg.vulnerabilities])
        adv_links = "\n".join(
            [
                f"- [{v.vuln_id}]({v.advisory_url}): {v.summary}"
                for v in pkg.vulnerabilities
                if v.advisory_url
            ]
        )

        pr_body = (
            f"## 🛡️ Automated Security Vulnerability Patch\n\n"
            f"Automated security audit detected fixable vulnerabilities in dependency `{package_name}`.\n\n"
            f"### Upgrade Details\n"
            f"- **Package:** `{package_name}`\n"
            f"- **Previous Version:** `{old_version}`\n"
            f"- **Patched Version:** `{new_version}`\n"
            f"- **Advisories:** {vuln_ids}\n\n"
            f"### Advisory Summaries\n"
            f"{adv_links if adv_links else 'See security advisory database for details.'}\n\n"
            f"---\n"
            f"*Generated by automated CVE patch PR generator workflow.*"
        )

        try:
            # Create local branch and commit
            subprocess.run(
                ["git", "checkout", "-b", branch_name], cwd=repo_root, check=True
            )
            subprocess.run(["git", "add", "uv.lock"], cwd=repo_root, check=True)
            if (repo_root / "pyproject.toml").is_file():
                subprocess.run(
                    ["git", "add", "pyproject.toml"], cwd=repo_root, check=False
                )

            commit_msg = (
                f"fix(security): patch {package_name} vulnerability to {new_version}"
            )
            subprocess.run(
                ["git", "commit", "-m", commit_msg], cwd=repo_root, check=True
            )

            if gh_client:
                # Push branch using token-authenticated remote URL
                push_target = f"https://x-access-token:{token}@github.com/{repo_full}.git"
                subprocess.run(
                    ["git", "push", "-u", push_target, branch_name],
                    cwd=repo_root,
                    check=True,
                )
                # Create PR with labels security, dependencies, automerge
                gh_client.create_pull_request(
                    title=pr_title,
                    head_branch=branch_name,
                    base_branch="main",
                    body=pr_body,
                    labels=["security", "dependencies", "automerge"],
                )
                logger.info(f"Created PR for {package_name} ({branch_name})")

            # Return to main branch
            subprocess.run(["git", "checkout", "main"], cwd=repo_root, check=False)
        except Exception as e:
            logger.error(f"Error creating git branch or PR for {package_name}: {e}")
            subprocess.run(["git", "checkout", "main"], cwd=repo_root, check=False)

    if unfixable_reports and gh_client:
        issue_title = (
            "[Security] Automated Dependency Audit Alert: Unfixable Vulnerabilities"
        )
        issue_body = (
            "## ⚠️ Unfixable Security Vulnerabilities Detected\n\n"
            "The daily automated vulnerability audit identified security vulnerabilities that could not be automatically remediated:\n\n"
            + "\n".join(unfixable_reports)
            + f"\n\n### Raw Audit Report Output\n```text\n{audit_output}\n```\n"
        )
        gh_client.report_security_issue(issue_title, issue_body)

    return patched_any


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    parser = argparse.ArgumentParser(description="Automated CVE Patch Generator")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate audit and patch without git push/PR creation",
    )
    parser.add_argument("--repo-root", default=".", help="Path to repository root")

    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo_full = os.environ.get("GITHUB_REPOSITORY", "fderuiter/sortify")

    remediate_vulnerabilities(
        repo_root=repo_root, dry_run=args.dry_run, token=token, repo_full=repo_full
    )


if __name__ == "__main__":
    main()
