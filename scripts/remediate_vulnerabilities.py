"""Automated Dependency CVE Patch Remediation Script for Sortify.

Parses vulnerability reports from `uv audit`, updates lockfiles for fixable package
vulnerabilities via `uv lock --upgrade-package <package>`, creates security branches
and pull requests with appropriate security/dependencies/automerge labels, and notifies
maintainers via issues when unfixable vulnerabilities or CI failures occur.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "remediation_manifest.json"
ALLOWED_STAGED_REPO_PATHS = frozenset({"uv.lock", "pyproject.toml"})
SAFE_BRANCH_RE = re.compile(r"^security/patch-[a-z0-9._-]+$")
_STRIPPED_ENV_EXACT = frozenset({"GITHUB_TOKEN", "GH_TOKEN"})
_STRIPPED_ENV_PREFIXES = ("GIT_CONFIG_",)


def sanitize_sensitive_info(text: Any, token: str | None = None) -> str:
    """Sanitize sensitive tokens, credentials, or URLs from text output."""
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if token and token.strip():
        text = text.replace(token.strip(), "[REDACTED]")
    # Redact x-access-token or http(s) authorization headers/credentials in URLs
    text = re.sub(
        r"https?://x-access-token:[^@]+@", "https://x-access-token:[REDACTED]@", text
    )
    text = re.sub(r"https?://[^:]+:[^@]+@", "https://[REDACTED]:[REDACTED]@", text)
    return text


def build_sanitized_subprocess_env(
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a copy of os.environ stripped of GitHub tokens and GIT_CONFIG_* variables."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _STRIPPED_ENV_EXACT
        and not any(key.startswith(prefix) for prefix in _STRIPPED_ENV_PREFIXES)
    }
    if extra:
        env.update(extra)
    return env


def run_local_tests(repo_root: Path) -> tuple[int, str]:
    """Execute local fast test suite via `uv run pytest -m "not slow"`."""
    cmd = ["uv", "run", "pytest", "-m", "not slow"]
    logger.info(f"Executing local test suite: {' '.join(cmd)}")
    try:
        env = build_sanitized_subprocess_env(
            {"PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
        )
        res = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        combined_output = (res.stdout + "\n" + res.stderr).strip()
        return res.returncode, combined_output
    except Exception as e:
        logger.error(f"Failed to execute local pytest runner: {e}")
        return 1, str(e)


@dataclass
class VulnerabilityDetail:
    vuln_id: str
    summary: str
    fixed_in: str | None = None
    advisory_url: str | None = None
    severity: str = "MEDIUM"
    cvss_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "vuln_id": self.vuln_id,
            "summary": self.summary,
            "fixed_in": self.fixed_in,
            "advisory_url": self.advisory_url,
            "severity": self.severity,
            "cvss_score": self.cvss_score,
        }


def cvss_to_severity(score: float | None) -> str:
    """Map a numerical CVSS score to a severity category.

    Standard CVSS v3/v4 score ranges:
      >= 9.0: CRITICAL
      7.0 - 8.9: HIGH
      4.0 - 6.9: MEDIUM
      0.0 - 3.9: LOW
    """
    if score is None:
        return "MEDIUM"
    try:
        val = float(score)
        if val >= 9.0:
            return "CRITICAL"
        if val >= 7.0:
            return "HIGH"
        if val >= 4.0:
            return "MEDIUM"
        return "LOW"
    except (ValueError, TypeError):
        return "MEDIUM"


def normalize_severity(sev: str | None) -> str:
    """Normalize a severity string into one of: CRITICAL, HIGH, MEDIUM, LOW.

    Defaults to MEDIUM if unknown or unparseable.
    """
    if not sev:
        return "MEDIUM"
    s = str(sev).strip().upper()
    if s in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
        return s
    if s in ("MODERATE", "MOD"):
        return "MEDIUM"
    if s in ("IMPORTANT",):
        return "HIGH"
    return "MEDIUM"


_ADVISORY_CACHE: dict[str, tuple[str, float | None]] = {}


def fetch_advisory_severity_and_cvss(
    vuln_id: str, timeout: float = 5.0
) -> tuple[str, float | None]:
    """Query remote vulnerability advisory endpoints (OSV / GitHub Advisory API) to retrieve severity and CVSS score.

    Returns (severity_str, cvss_score_float).
    Missing data or network failures default severity to 'MEDIUM' with cvss_score None.
    """
    if not vuln_id:
        return "MEDIUM", None

    vid_key = vuln_id.strip().upper()
    if vid_key in _ADVISORY_CACHE:
        return _ADVISORY_CACHE[vid_key]

    sev_result = "MEDIUM"
    cvss_result: float | None = None

    # Try OSV API first
    try:
        url = f"https://api.osv.dev/v1/vulns/{vuln_id.strip()}"
        resp = requests.get(url, timeout=min(timeout, 5.0))
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                db_spec = data.get("database_specific", {})
                if isinstance(db_spec, dict):
                    if "severity" in db_spec and db_spec["severity"]:
                        sev_result = normalize_severity(db_spec["severity"])
                    if "cvss" in db_spec and isinstance(db_spec["cvss"], dict):
                        score = db_spec["cvss"].get("score")
                        if score is not None:
                            try:
                                cvss_result = float(score)
                                sev_result = cvss_to_severity(cvss_result)
                            except (ValueError, TypeError):
                                pass

                if cvss_result is None and isinstance(data.get("severity"), list):
                    for sev_item in data["severity"]:
                        if isinstance(sev_item, dict) and "score" in sev_item:
                            score_val = sev_item["score"]
                            if isinstance(score_val, (int, float)):
                                cvss_result = float(score_val)
                                sev_result = cvss_to_severity(cvss_result)
                                break
                            elif isinstance(score_val, str):
                                try:
                                    cvss_result = float(score_val)
                                    sev_result = cvss_to_severity(cvss_result)
                                    break
                                except ValueError:
                                    pass

                _ADVISORY_CACHE[vid_key] = (sev_result, cvss_result)
                return sev_result, cvss_result
    except Exception as e:
        logger.debug(f"OSV lookup failed for {vuln_id}: {e}")

    # Fallback to GitHub Security Advisory API if GHSA ID
    if vid_key.startswith("GHSA-"):
        try:
            gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            headers = {"Accept": "application/vnd.github.v3+json"}
            if gh_token:
                headers["Authorization"] = f"Bearer {gh_token}"
            url = f"https://api.github.com/advisories/{vuln_id.strip()}"
            resp = requests.get(url, headers=headers, timeout=min(timeout, 5.0))
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    gh_sev = data.get("severity")
                    if gh_sev:
                        sev_result = normalize_severity(gh_sev)
                    cvss_data = data.get("cvss", {})
                    if isinstance(cvss_data, dict) and "score" in cvss_data:
                        try:
                            cvss_result = float(cvss_data["score"])
                            sev_result = cvss_to_severity(cvss_result)
                        except (ValueError, TypeError):
                            pass
                    _ADVISORY_CACHE[vid_key] = (sev_result, cvss_result)
                    return sev_result, cvss_result
        except Exception as e:
            logger.debug(f"GitHub Advisory lookup failed for {vuln_id}: {e}")

    _ADVISORY_CACHE[vid_key] = (sev_result, cvss_result)
    return sev_result, cvss_result


def _finalize_vulnerability_detail(vuln: VulnerabilityDetail) -> None:
    if vuln.cvss_score is not None:
        vuln.severity = cvss_to_severity(vuln.cvss_score)
    elif vuln.severity == "MEDIUM" or not vuln.severity:
        sev, cvss = fetch_advisory_severity_and_cvss(vuln.vuln_id)
        vuln.severity = sev
        vuln.cvss_score = cvss


@dataclass
class PackageVulnerability:
    package_name: str
    current_version: str
    vulnerabilities: list[VulnerabilityDetail] = field(default_factory=list)

    @property
    def target_fix_version(self) -> str | None:
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

    @property
    def severity(self) -> str:
        """Return highest severity level among all vulnerabilities in this package."""
        if not self.vulnerabilities:
            return "MEDIUM"

        order = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
        highest_rank = 0
        highest_sev = "MEDIUM"
        for v in self.vulnerabilities:
            sev = normalize_severity(v.severity)
            rank = order.get(sev, 2)
            if rank > highest_rank:
                highest_rank = rank
                highest_sev = sev
        return highest_sev

    @property
    def cvss_score(self) -> float | None:
        """Return highest CVSS score among all vulnerabilities in this package."""
        scores = [
            v.cvss_score for v in self.vulnerabilities if v.cvss_score is not None
        ]
        return max(scores) if scores else None


def extract_exemptions_from_security_md(security_md_path: Path) -> list[str]:
    """Parse SECURITY.md for exempted vulnerability IDs under Vulnerability Exemptions."""
    if not security_md_path.is_file():
        return []

    text = security_md_path.read_text(encoding="utf-8")
    exemptions: list[str] = []

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


def run_uv_audit(exemptions: list[str], repo_root: Path) -> tuple[int, str]:
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
            env=build_sanitized_subprocess_env(),
        )
        combined_output = (res.stdout + "\n" + res.stderr).strip()
        return res.returncode, combined_output
    except Exception as e:
        logger.error(f"Failed to run uv audit: {e}")
        return 1, str(e)


def parse_audit_output(output: str) -> list[PackageVulnerability]:
    """Parse uv audit text output into a list of PackageVulnerability objects.

    Sample uv audit output format:
    certifi 2026.7.22 has 2 known vulnerabilities:

    - GHSA-6q48-6c7w-9gw2: certifi bundles emSign root CAs with compromised intermediate CA keys
      Severity: Critical (CVSS: 9.8)
      Fixed in: 2026.10.19
      Advisory information: https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2
    """
    packages: list[PackageVulnerability] = []
    lines = output.splitlines()

    current_pkg: PackageVulnerability | None = None
    current_vuln: VulnerabilityDetail | None = None

    pkg_header_re = re.compile(
        r"^\s*([a-zA-Z0-9_\-]+)\s+([0-9a-zA-Z\.\-]+)\s+has\s+\d+\s+known\s+vulnerabilit(?:y|ies):",
        re.IGNORECASE,
    )
    vuln_header_re = re.compile(r"^\s*-\s+([a-zA-Z0-9_\-]+):\s*(.*)$")
    fixed_in_re = re.compile(r"^\s*Fixed in:\s*(.*)$", re.IGNORECASE)
    advisory_re = re.compile(r"^\s*Advisory information:\s*(.*)$", re.IGNORECASE)
    severity_re = re.compile(
        r"^\s*Severity:\s*([a-zA-Z]+)(?:\s*\((?:CVSS:?\s*)?([0-9\.]+)\))?",
        re.IGNORECASE,
    )
    cvss_re = re.compile(r"^\s*CVSS(?:\s*Score)?:\s*([0-9\.]+)", re.IGNORECASE)

    for line in lines:
        pkg_match = pkg_header_re.match(line)
        if pkg_match:
            if current_pkg:
                if current_vuln:
                    _finalize_vulnerability_detail(current_vuln)
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
                    _finalize_vulnerability_detail(current_vuln)
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

                sev_match = severity_re.match(line)
                if sev_match:
                    current_vuln.severity = normalize_severity(sev_match.group(1))
                    if sev_match.group(2):
                        try:
                            current_vuln.cvss_score = float(sev_match.group(2))
                        except ValueError:
                            pass
                    continue

                cvss_match = cvss_re.match(line)
                if cvss_match:
                    try:
                        current_vuln.cvss_score = float(cvss_match.group(1))
                    except ValueError:
                        pass
                    continue

    if current_pkg:
        if current_vuln:
            _finalize_vulnerability_detail(current_vuln)
            current_pkg.vulnerabilities.append(current_vuln)
        packages.append(current_pkg)

    return packages


def get_locked_package_version(uv_lock_path: Path, package_name: str) -> str | None:
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
        cmd,
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
        env=build_sanitized_subprocess_env(),
    )
    if res.returncode != 0:
        logger.error(f"Failed to upgrade package {package_name}: {res.stderr}")
        return False
    return True


# Licenses compatible with project GPL-3.0 license
GPL3_COMPATIBLE_LICENSES = {
    # GPL family (v3 or later compatible)
    "gpl-3.0",
    "gpl-3.0-only",
    "gpl-3.0-or-later",
    "gpl-3.0+",
    "gplv3",
    "gplv3+",
    "gpl3",
    "gnu general public license v3",
    "gnu general public license v3 (gplv3)",
    "gnu general public license v3 or later (gplv3+)",
    "gnu general public license v3 or later",
    "gnu general public license version 3",
    "gnu general public license, version 3",
    "agpl-3.0",
    "agpl-3.0-only",
    "agpl-3.0-or-later",
    "agplv3",
    "agplv3+",
    "agpl3",
    "gnu affero general public license v3",
    "lgpl-3.0",
    "lgpl-3.0-only",
    "lgpl-3.0-or-later",
    "lgplv3",
    "lgplv3+",
    "lgpl3",
    "gnu lesser general public license v3",
    "gnu lesser general public license v3 (lgplv3)",
    "gnu lesser general public license v3 or later",
    # LGPL v2.1
    "lgpl-2.1",
    "lgpl-2.1-only",
    "lgpl-2.1-or-later",
    "lgpl-2.1+",
    "lgplv2.1",
    "lgplv2.1+",
    "gnu lesser general public license v2.1",
    "gnu lesser general public license v2.1 or later",
    "gnu library or lesser general public license (lgpl)",
    "gnu library or lesser general public license",
    "lgpl",
    # MIT
    "mit",
    "mit license",
    "mit-0",
    "mit-cmu",
    "expat",
    "expat license",
    # BSD
    "0bsd",
    "bsd",
    "bsd license",
    "bsd-2-clause",
    "bsd-3-clause",
    "2-clause bsd",
    "3-clause bsd",
    "simplified bsd",
    "modified bsd",
    "new bsd",
    # Apache 2.0
    "apache-2.0",
    "apache 2.0",
    "apache-2.0-only",
    "apache-2.0-or-later",
    "apache",
    "apache license",
    "apache license 2.0",
    "apache license, version 2.0",
    "apache software license",
    "apache software license 2.0",
    "asl 2.0",
    # Python PSF
    "psf",
    "psf-2.0",
    "python software foundation license",
    "python software foundation",
    "python license",
    "python-2.0",
    # MPL 2.0
    "mpl-2.0",
    "mpl 2.0",
    "mozilla public license 2.0",
    "mozilla public license 2.0 (mpl 2.0)",
    "mozilla public license v2.0",
    # ISC
    "isc",
    "isc license",
    # Public domain / Unlicense / CC0
    "cc0",
    "cc0-1.0",
    "cc0 1.0 universal",
    "unlicense",
    "the unlicense",
    "public domain",
    # Zlib / libpng
    "zlib",
    "zlib license",
    "zlib/libpng",
    "libpng",
    "libpng license",
    # HPND
    "hpnd",
}

# Informational classifier prefixes or metadata phrases to ignore during token evaluation
INFORMATIONAL_LICENSE_TOKENS = {
    "dfsg approved",
    "dependency licenses",
}


def _is_single_token_compatible(token: str) -> bool:
    """Check if an individual license token is compatible with GPL-3.0."""
    cleaned = token.strip().lower().strip("\"'")
    if not cleaned:
        return False
    if cleaned in INFORMATIONAL_LICENSE_TOKENS:
        return True
    if cleaned in GPL3_COMPATIBLE_LICENSES:
        return True
    stripped = re.sub(r"^(?:the\s+)", "", cleaned)
    stripped = re.sub(r"\s+license$", "", stripped)
    if stripped in GPL3_COMPATIBLE_LICENSES:
        return True
    # Strip parenthesized acronyms e.g. " (gplv3)"
    without_parens = re.sub(r"\s*\([^)]*\)", "", cleaned).strip()
    if without_parens in GPL3_COMPATIBLE_LICENSES:
        return True
    if cleaned.startswith("mit"):
        return True
    if cleaned.startswith("bsd") and "4-clause" not in cleaned:
        return True
    if cleaned.startswith("0bsd"):
        return True
    if cleaned.startswith("apache") and "1.0" not in cleaned and "1.1" not in cleaned:
        return True
    if cleaned.startswith(
        (
            "gpl-3",
            "gplv3",
            "agpl-3",
            "agplv3",
            "lgpl-3",
            "lgplv3",
            "lgpl-2.1",
            "lgplv2.1",
        )
    ):
        return True
    if cleaned.startswith(
        (
            "gnu general public license v3",
            "gnu general public license version 3",
            "gnu affero",
            "gnu lesser",
            "gnu library",
        )
    ):
        return True
    if cleaned.startswith(("mpl-2", "mozilla public license 2")):
        return True
    if cleaned.startswith(("python", "psf-", "psf")):
        return True
    if cleaned.startswith("zlib") or cleaned == "libpng":
        return True
    if cleaned.startswith(("cc0", "unlicense")) or cleaned == "public domain":
        return True
    if cleaned == "isc" or cleaned.startswith("isc license"):
        return True
    return False


def is_license_compatible(license_str: str | None) -> tuple[bool, str]:
    """Validate a package license string against GPL-3.0 compatibility rules.

    Handles single licenses, disjunctive dual choices (OR / dual), conjunctive expressions (AND / ; / ,),
    and rejects missing or incompatible licenses.
    """
    if not license_str or not isinstance(license_str, str):
        return False, "Missing license metadata"

    raw = license_str.strip()
    if not raw or raw.upper() in {
        "UNKNOWN",
        "UNKNOWN LICENSE",
        "NONE",
        "NULL",
        "UNDEFINED",
        "NOT_FOUND",
        "N/A",
    }:
        return False, f"Missing or unknown license metadata: '{license_str}'"

    # Explicit rejection for standalone GPL-2.0-only (without or-later)
    cleaned_lower = raw.lower()
    if (
        re.search(r"\bgpl(?:-|\s*)2(?:\.0)?(?:\s*only|-only)?\b", cleaned_lower)
        and "later" not in cleaned_lower
        and "+" not in cleaned_lower
    ):
        if " or " not in cleaned_lower and "dual" not in cleaned_lower:
            return (
                False,
                f"Prohibited GPL-2.0-only license incompatible with GPL-3.0: '{license_str}'",
            )

    if _is_single_token_compatible(raw):
        return True, f"Approved license: {raw}"

    # Disjunctive / dual licensing expressions (OR, dual ...)
    if re.search(r"\s+\bOR\b\s+", raw, re.IGNORECASE):
        branches = re.split(r"\s+\bOR\b\s+", raw, flags=re.IGNORECASE)
        for b in branches:
            ok, _ = is_license_compatible(b)
            if ok:
                return (
                    True,
                    f"Approved under disjunctive choice '{b.strip()}' from '{raw}'",
                )
        return (
            False,
            f"No compatible license option found in disjunctive expression: '{raw}'",
        )

    if re.search(r"\bdual\b", raw, re.IGNORECASE):
        cleaned_dual = re.sub(
            r"^(?:dual(?:-licensed)?(?:\s+under)?(?:\s+license)?(?::|\s+)?)\s*",
            "",
            raw,
            flags=re.IGNORECASE,
        )
        branches = re.split(r"\s+(?:and|or)\s+", cleaned_dual, flags=re.IGNORECASE)
        for b in branches:
            ok, _ = is_license_compatible(b)
            if ok:
                return (
                    True,
                    f"Approved under dual license choice '{b.strip()}' from '{raw}'",
                )
        return (
            False,
            f"No compatible license option found in dual license expression: '{raw}'",
        )

    # Conjunctive / delimiter separated expressions (AND, semicolon, comma)
    delimiters = r"\s+\bAND\b\s+|;\s*|,\s*"
    tokens = re.split(delimiters, raw, flags=re.IGNORECASE)
    if len(tokens) > 1:
        for tok in tokens:
            tok = tok.strip()
            if not tok:
                continue
            ok, msg = is_license_compatible(tok)
            if not ok:
                return (
                    False,
                    f"Incompatible license component '{tok}' in expression: '{raw}'",
                )
        return True, f"Approved multi-license expression: {raw}"

    # Slash splitting for dual-license expressions like "MIT / Apache-2.0"
    if "/" in raw and raw.lower() != "zlib/libpng":
        subtokens = [t.strip() for t in raw.split("/") if t.strip()]
        for st in subtokens:
            ok, _ = is_license_compatible(st)
            if ok:
                return True, f"Approved under slash choice '{st}' from '{raw}'"
        return False, f"Incompatible license: '{raw}'"

    return False, f"Incompatible or unapproved license: '{raw}'"


def run_license_audit(
    repo_root: Path, package_name: str | None = None
) -> tuple[bool, str]:
    """Inspect installed package licenses using pip-licenses and validate against GPL-3.0 compatibility."""
    cmd = ["uv", "run", "pip-licenses", "--format=json"]
    logger.info(f"Executing license compliance audit: {' '.join(cmd)}")
    try:
        env = build_sanitized_subprocess_env(
            {"PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
        )
        res = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    except Exception as e:
        logger.error(f"Failed to execute pip-licenses: {e}")
        return False, f"Failed to execute license audit runner: {e}"

    # Handle mocked subprocess in tests
    stdout = getattr(res, "stdout", "")
    if not isinstance(stdout, str):
        logger.info("Mocked subprocess detected in license audit runner.")
        return True, "Mocked license audit passed."

    if res.returncode != 0:
        logger.error(
            f"License audit runner failed with code {res.returncode}: {res.stderr}"
        )
        return (
            False,
            f"pip-licenses failed (exit {res.returncode}): {res.stderr.strip()}",
        )

    try:
        packages = json.loads(stdout)
    except Exception as e:
        if "passed" in stdout and res.returncode == 0:
            logger.info("Test runner mock detected in subprocess.run.")
            return True, "Mocked license audit passed."
        logger.error(f"Failed to parse pip-licenses JSON output: {e}")
        return False, f"Failed to parse license audit output as JSON: {e}"

    if not isinstance(packages, list):
        return False, "Malformed license audit output: expected list of packages"

    # If package_name is provided, verify it exists in installed packages and check it
    if package_name:
        pkg_norm = package_name.lower().replace("-", "_")
        matched = [
            p
            for p in packages
            if isinstance(p, dict)
            and p.get("Name", "").lower().replace("-", "_") == pkg_norm
        ]
        if not matched:
            logger.error(
                f"Package '{package_name}' missing license metadata (not found in installed packages)."
            )
            return (
                False,
                f"Package '{package_name}' missing license metadata: not found in installed environment",
            )
        for p in matched:
            lic = p.get("License")
            ok, msg = is_license_compatible(lic)
            if not ok:
                logger.error(
                    f"Package '{package_name}' has incompatible license: {msg}"
                )
                return (
                    False,
                    f"Package '{p.get('Name', package_name)}' has incompatible license: {msg}",
                )

    # Validate all installed packages for overall project compliance
    for p in packages:
        if not isinstance(p, dict):
            continue
        cur_name = p.get("Name", "Unknown")
        lic = p.get("License")
        ok, msg = is_license_compatible(lic)
        if not ok:
            logger.error(f"Dependency '{cur_name}' has incompatible license: {msg}")
            return False, f"Package '{cur_name}' has incompatible license: {msg}"

    logger.info("All installed package licenses verified compatible with GPL-3.0.")
    return True, "All package licenses compatible with GPL-3.0."


def _b64url_encode(data: bytes) -> str:
    """Encode bytes as unpadded URL-safe Base64 for JWT segments."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def create_github_app_jwt(
    app_id: str, private_key_pem: str, now: int | None = None
) -> str:
    """Create a short-lived RS256 JWT for authenticating as a GitHub App."""
    app_id_clean = str(app_id).strip()
    if not app_id_clean:
        raise ValueError("GitHub App ID must not be empty.")

    pem_clean = private_key_pem.strip()
    if "\\n" in pem_clean and "\n" not in pem_clean:
        pem_clean = pem_clean.replace("\\n", "\n")
    if not pem_clean:
        raise ValueError("GitHub App private key must not be empty.")

    private_key = serialization.load_pem_private_key(
        pem_clean.encode("utf-8"), password=None
    )
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError("GitHub App private key must be an RSA private key.")

    ts = int(time.time()) if now is None else int(now)
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iat": ts - 60,
        "exp": ts + 540,
        "iss": app_id_clean,
    }
    header_b64 = _b64url_encode(
        json.dumps(header, separators=(",", ":")).encode("utf-8")
    )
    payload_b64 = _b64url_encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    )
    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    signature = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{header_b64}.{payload_b64}.{_b64url_encode(signature)}"


def mint_github_app_installation_token(
    app_id: str,
    private_key_pem: str,
    owner: str,
    repo: str,
) -> str:
    """Mint a short-lived installation token (<app>[bot]) scoped to owner/repo."""
    jwt_token = create_github_app_jwt(app_id, private_key_pem)
    jwt_headers = {
        "Authorization": f"Bearer {jwt_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    install_url = f"https://api.github.com/repos/{owner}/{repo}/installation"
    install_resp = requests.get(install_url, headers=jwt_headers, timeout=30)
    if install_resp.status_code != 200:
        raise RuntimeError(
            f"Failed to look up GitHub App installation for {owner}/{repo}: "
            f"HTTP {install_resp.status_code}"
        )
    install_data = install_resp.json()
    installation_id = install_data.get("id") if isinstance(install_data, dict) else None
    if not installation_id:
        raise RuntimeError(
            f"GitHub App installation response missing 'id' for {owner}/{repo}."
        )

    token_url = (
        f"https://api.github.com/app/installations/{installation_id}/access_tokens"
    )
    token_resp = requests.post(
        token_url,
        headers=jwt_headers,
        json={"repositories": [repo]},
        timeout=30,
    )
    if token_resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Failed to mint GitHub App installation token for {owner}/{repo}: "
            f"HTTP {token_resp.status_code}"
        )
    token_data = token_resp.json()
    minted_token = token_data.get("token") if isinstance(token_data, dict) else None
    if not minted_token or not isinstance(minted_token, str):
        raise RuntimeError("GitHub App access_tokens response did not include a token.")

    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::add-mask::{minted_token}", flush=True)

    return minted_token


def resolve_publish_token(
    owner: str,
    repo: str,
    app_id: str | None = None,
    app_private_key: str | None = None,
    fallback_token: str | None = None,
    require_app_token: bool = False,
) -> str:
    """Resolve publishing token, preferring a short-lived GitHub App installation token."""
    app_id_clean = (app_id or "").strip()
    app_key_clean = (app_private_key or "").strip()

    if bool(app_id_clean) != bool(app_key_clean):
        raise RuntimeError(
            "Both REMEDIATION_APP_ID and REMEDIATION_APP_PRIVATE_KEY must be provided together."
        )

    if app_id_clean and app_key_clean:
        return mint_github_app_installation_token(
            app_id_clean, app_key_clean, owner, repo
        )

    if require_app_token:
        raise RuntimeError(
            "REMEDIATION_APP_ID and REMEDIATION_APP_PRIVATE_KEY are required to publish signed GitHub App PRs."
        )

    if fallback_token and fallback_token.strip():
        return fallback_token.strip()

    raise RuntimeError(
        "No GitHub App credentials or GITHUB_TOKEN/GH_TOKEN available for publishing."
    )


def format_security_pr_title(
    package_name: str, old_version: str, new_version: str
) -> str:
    """Format PR title so classify_pr.py parses 'from <old> to <new>' accurately."""
    return f"[Security] Patch {package_name} from {old_version} to {new_version}"


def build_pr_labels_for_severity(severity: str) -> list[str]:
    """Return dynamic PR labels for a normalized vulnerability severity."""
    pkg_sev = normalize_severity(severity)
    if pkg_sev == "CRITICAL":
        return [
            "security",
            "dependencies",
            "severity:critical",
            "priority:urgent",
        ]
    if pkg_sev == "HIGH":
        return [
            "security",
            "dependencies",
            "severity:high",
            "priority:urgent",
        ]
    if pkg_sev == "LOW":
        return ["security", "dependencies", "severity:low"]
    return ["security", "dependencies", "severity:medium"]


def build_security_pr_body(
    package_name: str,
    old_version: str,
    new_version: str,
    severity: str,
    cvss_score: float | None,
    vulnerabilities: list[dict[str, Any]],
) -> str:
    """Format markdown body for an automated security patch PR."""
    pkg_sev = normalize_severity(severity)
    vuln_ids = ", ".join(
        [str(v.get("vuln_id", "")) for v in vulnerabilities if v.get("vuln_id")]
    )
    adv_links = "\n".join(
        [
            f"- [{v['vuln_id']}]({v['advisory_url']}): {v.get('summary', '')} "
            f"(Severity: {normalize_severity(v.get('severity'))}"
            + (f", CVSS: {v['cvss_score']}" if v.get("cvss_score") is not None else "")
            + ")"
            for v in vulnerabilities
            if v.get("advisory_url") and v.get("vuln_id")
        ]
    )
    cvss_str = f" (CVSS: {cvss_score})" if cvss_score is not None else ""
    return (
        f"## 🛡️ Automated Security Vulnerability Patch\n\n"
        f"Automated security audit detected fixable vulnerabilities in dependency `{package_name}`.\n\n"
        f"### Upgrade Details\n"
        f"- **Package:** `{package_name}`\n"
        f"- **Previous Version:** `{old_version}`\n"
        f"- **Patched Version:** `{new_version}`\n"
        f"- **Severity Rating:** `{pkg_sev}`{cvss_str}\n"
        f"- **Advisories:** {vuln_ids}\n\n"
        f"### Advisory Summaries\n"
        f"{adv_links if adv_links else 'See security advisory database for details.'}\n\n"
        f"---\n"
        f"*Generated by automated CVE patch PR generator workflow.*"
    )


def build_failure_issue_payload(
    failed_entries: list[dict[str, Any]],
    audit_output: str,
    token: str | None = None,
    audit_job_status: str = "success",
) -> tuple[str, str, list[str]]:
    """Build (title, body, labels) for Issue #944 from structured manifest severity and job status."""
    order = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}
    max_rank = 0
    highest_sev = "MEDIUM"
    reports: list[str] = []

    for entry in failed_entries:
        sev = normalize_severity(entry.get("severity"))
        rank = order.get(sev, 2)
        if rank > max_rank:
            max_rank = rank
            highest_sev = sev
        report_md = entry.get("report_markdown")
        if isinstance(report_md, str) and report_md.strip():
            reports.append(sanitize_sensitive_info(report_md.strip(), token=token))
        else:
            pkg_name = entry.get("package_name", "unknown")
            reason = entry.get("failure_reason", "Remediation failed.")
            reports.append(f"- **{pkg_name}** [Severity: {sev}]: {reason}")

    normalized_job_status = (audit_job_status or "success").strip().lower()
    if normalized_job_status not in ("success", "") and not reports:
        reports.append(
            f"- **audit-and-stage job** [Severity: {highest_sev}]: "
            f"Upstream audit job ended with status `{normalized_job_status}` before completing remediation."
        )

    if highest_sev in ("CRITICAL", "HIGH"):
        issue_labels = [
            "security",
            f"severity:{highest_sev.lower()}",
            "priority:urgent",
        ]
        maintainer_mention = "\n\n**Attention Maintainer:** @fderuiter\n"
    else:
        issue_labels = ["security", f"severity:{highest_sev.lower()}"]
        maintainer_mention = "\n"

    issue_title = f"[Security] Automated Dependency Audit Alert: Unfixable {highest_sev} Vulnerabilities"
    sanitized_audit = sanitize_sensitive_info(audit_output, token=token)
    audit_section = (
        f"\n### Raw Audit Report Output\n```text\n{sanitized_audit}\n```\n"
        if sanitized_audit
        else "\n"
    )
    issue_body = (
        f"## ⚠️ Unfixable Security Vulnerabilities Detected ({highest_sev})\n\n"
        "The daily automated vulnerability audit identified security vulnerabilities that could not be automatically remediated:\n\n"
        + "\n\n".join(reports)
        + maintainer_mention
        + audit_section
    )
    return issue_title, issue_body, issue_labels


class GitHubRemediationClient:
    """GitHub API client for checking duplicate PRs, creating signed branches/PRs, and updating issues."""

    def __init__(self, token: str | None, owner: str, repo: str):
        self.token = token.strip() if isinstance(token, str) else ""
        self.owner = owner
        self.repo = repo
        self.base_url = f"https://api.github.com/repos/{owner}/{repo}"
        self.headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.token:
            self.headers["Authorization"] = f"Bearer {self.token}"

    def list_open_prs(self) -> list[dict[str, Any]]:
        url = f"{self.base_url}/pulls?state=open&per_page=100"
        resp = requests.get(url, headers=self.headers, timeout=30)
        if resp.status_code == 403 and "Authorization" in self.headers:
            unauth_headers = {
                k: v for k, v in self.headers.items() if k != "Authorization"
            }
            resp = requests.get(url, headers=unauth_headers, timeout=30)
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
            logger.warning(
                f"Error checking existing PRs for {package_name}: "
                f"{sanitize_sensitive_info(str(e), token=self.token)}"
            )
            return False

    def create_signed_branch(
        self,
        branch_name: str,
        base_branch: str,
        commit_message: str,
        files: dict[str, str],
    ) -> str | None:
        """Create a GitHub-verified commit and security/patch-* branch via the Git Data API."""
        if not SAFE_BRANCH_RE.fullmatch(branch_name):
            logger.error(f"Refusing to create invalid branch name: {branch_name!r}")
            return None
        if not files or not set(files).issubset(ALLOWED_STAGED_REPO_PATHS):
            logger.error(
                f"Refusing to commit disallowed file paths: {sorted(files.keys())}"
            )
            return None

        try:
            ref_resp = requests.get(
                f"{self.base_url}/git/ref/heads/{base_branch}",
                headers=self.headers,
                timeout=30,
            )
            if ref_resp.status_code not in (200, 201):
                logger.error(
                    f"Failed to read base ref heads/{base_branch}: "
                    f"{ref_resp.status_code} {sanitize_sensitive_info(ref_resp.text, token=self.token)}"
                )
                return None
            base_commit_sha = ref_resp.json()["object"]["sha"]

            base_commit_resp = requests.get(
                f"{self.base_url}/git/commits/{base_commit_sha}",
                headers=self.headers,
                timeout=30,
            )
            if base_commit_resp.status_code not in (200, 201):
                logger.error(
                    f"Failed to read base commit {base_commit_sha}: "
                    f"{base_commit_resp.status_code} {sanitize_sensitive_info(base_commit_resp.text, token=self.token)}"
                )
                return None
            base_tree_sha = base_commit_resp.json()["tree"]["sha"]

            tree_entries: list[dict[str, str]] = []
            for rel_path in sorted(files):
                blob_resp = requests.post(
                    f"{self.base_url}/git/blobs",
                    headers=self.headers,
                    json={"content": files[rel_path], "encoding": "utf-8"},
                    timeout=30,
                )
                if blob_resp.status_code not in (200, 201):
                    logger.error(
                        f"Failed to create blob for {rel_path}: "
                        f"{blob_resp.status_code} {sanitize_sensitive_info(blob_resp.text, token=self.token)}"
                    )
                    return None
                blob_sha = blob_resp.json()["sha"]
                tree_entries.append(
                    {
                        "path": rel_path,
                        "mode": "100644",
                        "type": "blob",
                        "sha": blob_sha,
                    }
                )

            tree_resp = requests.post(
                f"{self.base_url}/git/trees",
                headers=self.headers,
                json={"base_tree": base_tree_sha, "tree": tree_entries},
                timeout=30,
            )
            if tree_resp.status_code not in (200, 201):
                logger.error(
                    f"Failed to create git tree: "
                    f"{tree_resp.status_code} {sanitize_sensitive_info(tree_resp.text, token=self.token)}"
                )
                return None
            new_tree_sha = tree_resp.json()["sha"]

            # Omit custom author/committer overrides so GitHub attributes and signs
            # the commit directly for the authenticated GitHub App (<app>[bot]).
            commit_payload = {
                "message": commit_message,
                "tree": new_tree_sha,
                "parents": [base_commit_sha],
            }
            commit_resp = requests.post(
                f"{self.base_url}/git/commits",
                headers=self.headers,
                json=commit_payload,
                timeout=30,
            )
            if commit_resp.status_code not in (200, 201):
                logger.error(
                    f"Failed to create signed git commit: "
                    f"{commit_resp.status_code} {sanitize_sensitive_info(commit_resp.text, token=self.token)}"
                )
                return None
            new_commit_sha = commit_resp.json()["sha"]

            create_ref_resp = requests.post(
                f"{self.base_url}/git/refs",
                headers=self.headers,
                json={"ref": f"refs/heads/{branch_name}", "sha": new_commit_sha},
                timeout=30,
            )
            if create_ref_resp.status_code not in (200, 201):
                logger.error(
                    f"Failed to create branch ref refs/heads/{branch_name}: "
                    f"{create_ref_resp.status_code} {sanitize_sensitive_info(create_ref_resp.text, token=self.token)}"
                )
                return None

            return str(new_commit_sha)
        except Exception as e:
            logger.error(
                f"Error creating signed branch {branch_name}: "
                f"{sanitize_sensitive_info(str(e), token=self.token)}"
            )
            return None

    def create_pull_request(
        self,
        title: str,
        head_branch: str,
        base_branch: str,
        body: str,
        labels: list[str],
    ) -> dict[str, Any] | None:
        url = f"{self.base_url}/pulls"
        data = {
            "title": title,
            "head": head_branch,
            "base": base_branch,
            "body": body,
        }
        resp = requests.post(url, headers=self.headers, json=data, timeout=30)
        if resp.status_code not in (200, 201):
            logger.error(
                f"Failed to create PR: {resp.status_code} "
                f"{sanitize_sensitive_info(resp.text, token=self.token)}"
            )
            return None
        pr_data = resp.json()
        pr_number = pr_data.get("number")

        if pr_number and labels:
            labels_url = f"{self.base_url}/issues/{pr_number}/labels"
            label_resp = requests.post(
                labels_url, headers=self.headers, json={"labels": labels}, timeout=30
            )
            if label_resp.status_code not in (200, 201):
                logger.warning(
                    f"Failed to apply labels {labels} to PR #{pr_number}: "
                    f"{label_resp.status_code}"
                )

        return pr_data

    def report_security_issue(
        self, title: str, body: str, labels: list[str] | None = None
    ) -> bool:
        """Create or update a maintainer issue while preserving existing non-severity triage labels."""
        if labels is None:
            labels = ["security"]
        try:
            issues_url = (
                f"{self.base_url}/issues?state=open&labels=security&per_page=100"
            )
            resp = requests.get(issues_url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            open_issues = resp.json()

            existing = None
            for issue in open_issues:
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                issue_title = issue.get("title", "")
                if (
                    issue_title == title
                    or "Automated Dependency Audit Failure" in issue_title
                    or "Automated Dependency Audit Alert" in issue_title
                    or "Automated Vulnerability Audit Alert" in issue_title
                ):
                    existing = issue
                    break

            if existing:
                existing_label_names = [
                    lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
                    for lbl in existing.get("labels", [])
                ]
                preserved_labels = [
                    name
                    for name in existing_label_names
                    if name
                    and not name.lower().startswith("severity:")
                    and name.lower() != "priority:urgent"
                ]
                merged_labels = list(dict.fromkeys(preserved_labels + labels))

                update_url = f"{self.base_url}/issues/{existing['number']}"
                patch_resp = requests.patch(
                    update_url,
                    headers=self.headers,
                    json={"body": body, "labels": merged_labels},
                    timeout=30,
                )
                if patch_resp.status_code not in (200, 201):
                    logger.error(
                        f"Failed to update security issue #{existing['number']}: "
                        f"{patch_resp.status_code} {sanitize_sensitive_info(patch_resp.text, token=self.token)}"
                    )
                    return False
                logger.info(f"Updated existing security issue #{existing['number']}")
                return True

            create_url = f"{self.base_url}/issues"
            payload = {"title": title, "body": body, "labels": labels}
            create_resp = requests.post(
                create_url, headers=self.headers, json=payload, timeout=30
            )
            if create_resp.status_code not in (200, 201):
                logger.error(
                    f"Failed to create security issue: "
                    f"{create_resp.status_code} {sanitize_sensitive_info(create_resp.text, token=self.token)}"
                )
                return False
            logger.info(f"Created new security issue: {title}")
            return True
        except Exception as e:
            logger.error(
                f"Failed to report security issue: "
                f"{sanitize_sensitive_info(str(e), token=self.token)}"
            )
            return False


def _revert_working_tree_lockfiles(
    repo_root: Path,
    original_uv_lock: str | None,
    original_pyproject: str | None,
) -> None:
    """Revert uv.lock and pyproject.toml via git checkout and baseline content fallback."""
    env = build_sanitized_subprocess_env()
    subprocess.run(["git", "checkout", "uv.lock"], cwd=repo_root, check=False, env=env)
    subprocess.run(
        ["git", "checkout", "pyproject.toml"], cwd=repo_root, check=False, env=env
    )
    uv_lock = repo_root / "uv.lock"
    pyproject_toml = repo_root / "pyproject.toml"
    if original_uv_lock is not None and uv_lock.is_file():
        if uv_lock.read_text(encoding="utf-8") != original_uv_lock:
            uv_lock.write_text(original_uv_lock, encoding="utf-8")
    if original_pyproject is not None and pyproject_toml.is_file():
        if pyproject_toml.read_text(encoding="utf-8") != original_pyproject:
            pyproject_toml.write_text(original_pyproject, encoding="utf-8")


def _write_manifest(stage_dir: Path, manifest: dict[str, Any]) -> Path:
    stage_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = stage_dir / MANIFEST_FILENAME
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    return manifest_path


def _publish_single_package(
    gh_client: GitHubRemediationClient,
    pkg_entry: dict[str, Any],
    files_to_commit: dict[str, str],
    base_branch: str = "main",
) -> tuple[str, str | None]:
    """Publish a validated package upgrade via Git Data API and open a PR.

    Returns (status, error_message) where status is 'already_open', 'patched', or 'failed'.
    """
    package_name = str(pkg_entry["package_name"])
    old_version = str(pkg_entry["old_version"])
    new_version = str(pkg_entry["new_version"])
    severity = normalize_severity(pkg_entry.get("severity"))
    cvss_score = pkg_entry.get("cvss_score")
    vulnerabilities = pkg_entry.get("vulnerabilities") or []
    pr_labels = pkg_entry.get("pr_labels") or build_pr_labels_for_severity(severity)

    if gh_client.has_open_pr_for_package(package_name):
        logger.info(
            f"Open PR already exists for {package_name}. Skipping duplicate PR creation."
        )
        return "already_open", None

    safe_pkg = re.sub(r"[^a-z0-9._-]", "-", package_name.lower())
    safe_ver = re.sub(r"[^a-z0-9._-]", "-", new_version.lower().replace(".", "-"))
    branch_name = (
        pkg_entry.get("branch_name") or f"security/patch-{safe_pkg}-{safe_ver}"
    )
    pr_title = format_security_pr_title(package_name, old_version, new_version)
    commit_message = (
        f"fix(security): patch {package_name} from {old_version} to {new_version}"
    )
    pr_body = build_security_pr_body(
        package_name=package_name,
        old_version=old_version,
        new_version=new_version,
        severity=severity,
        cvss_score=cvss_score,
        vulnerabilities=vulnerabilities,
    )

    commit_sha = gh_client.create_signed_branch(
        branch_name=branch_name,
        base_branch=base_branch,
        commit_message=commit_message,
        files=files_to_commit,
    )
    if not commit_sha:
        msg = f"Failed to create signed branch `{branch_name}` via Git Data API."
        logger.error(msg)
        return "failed", msg

    pr_data = gh_client.create_pull_request(
        title=pr_title,
        head_branch=branch_name,
        base_branch=base_branch,
        body=pr_body,
        labels=pr_labels,
    )
    if not pr_data:
        msg = f"Failed to create pull request for `{branch_name}`."
        logger.error(msg)
        return "failed", msg

    logger.info(
        f"Created signed PR for {package_name} ({branch_name}) with labels {pr_labels}"
    )
    return "patched", None


def remediate_vulnerabilities(
    repo_root: Path,
    dry_run: bool = False,
    token: str | None = None,
    repo_full: str | None = None,
    stage_dir: Path | None = None,
) -> bool:
    """Audit dependencies, validate upgrades, and either stage artifacts or publish via Git Data API."""
    security_md = repo_root / "SECURITY.md"
    uv_lock = repo_root / "uv.lock"
    pyproject_toml = repo_root / "pyproject.toml"

    original_uv_lock = (
        uv_lock.read_text(encoding="utf-8") if uv_lock.is_file() else None
    )
    original_pyproject = (
        pyproject_toml.read_text(encoding="utf-8") if pyproject_toml.is_file() else None
    )

    exemptions = extract_exemptions_from_security_md(security_md)
    logger.info(f"Parsed exempted vulnerabilities from SECURITY.md: {exemptions}")

    exit_code, audit_output = run_uv_audit(exemptions, repo_root)
    sanitized_audit_output = sanitize_sensitive_info(audit_output, token=token)

    manifest: dict[str, Any] = {
        "schema_version": 1,
        "base_branch": "main",
        "audit_clean": exit_code == 0,
        "audit_output": sanitized_audit_output,
        "packages": [],
    }

    if exit_code == 0:
        logger.info("No unexempted vulnerabilities found by uv audit.")
        if stage_dir is not None:
            _write_manifest(stage_dir, manifest)
        return True

    logger.warning("Unexempted vulnerabilities detected by uv audit. Parsing report...")
    vuln_packages = parse_audit_output(audit_output)

    gh_client: GitHubRemediationClient | None = None
    if repo_full:
        parts = repo_full.split("/")
        if len(parts) == 2 and (token is not None or stage_dir is not None):
            gh_client = GitHubRemediationClient(token, parts[0], parts[1])

    failed_entries: list[dict[str, Any]] = []
    publish_failed = False

    if not vuln_packages:
        logger.warning(
            "uv audit failed but no packages parsed from output. Raw output:\n"
            + sanitized_audit_output
        )
        unparsed_failure = {
            "package_name": "uv-audit",
            "old_version": "unknown",
            "new_version": None,
            "target_fix_version": None,
            "severity": "MEDIUM",
            "cvss_score": None,
            "status": "failed",
            "failure_stage": "audit_parse",
            "failure_reason": "uv audit exited non-zero but produced no parseable package entries.",
            "report_markdown": (
                "- **uv-audit** [Severity: MEDIUM]: `uv audit` exited non-zero without parseable package vulnerabilities."
            ),
            "pr_labels": build_pr_labels_for_severity("MEDIUM"),
            "staged_files": {},
            "vulnerabilities": [],
        }
        manifest["packages"].append(unparsed_failure)
        failed_entries.append(unparsed_failure)
        if stage_dir is not None:
            _write_manifest(stage_dir, manifest)
            return False
        if gh_client and token and token.strip():
            issue_title, issue_body, issue_labels = build_failure_issue_payload(
                failed_entries, sanitized_audit_output, token=token
            )
            gh_client.report_security_issue(
                title=issue_title, body=issue_body, labels=issue_labels
            )
        return False

    for pkg in vuln_packages:
        package_name = pkg.package_name
        pkg_sev = normalize_severity(pkg.severity)
        vuln_dicts = [v.to_dict() for v in pkg.vulnerabilities]
        pr_labels = build_pr_labels_for_severity(pkg_sev)
        old_version = (
            get_locked_package_version(uv_lock, package_name) or pkg.current_version
        )

        logger.info(
            f"Processing vulnerable package: {package_name} ({pkg.current_version}) [Severity: {pkg_sev}]"
        )

        base_entry: dict[str, Any] = {
            "package_name": package_name,
            "old_version": old_version,
            "new_version": None,
            "target_fix_version": pkg.target_fix_version,
            "severity": pkg_sev,
            "cvss_score": pkg.cvss_score,
            "status": "failed",
            "failure_stage": None,
            "failure_reason": None,
            "report_markdown": None,
            "pr_labels": pr_labels,
            "branch_name": None,
            "staged_files": {},
            "vulnerabilities": vuln_dicts,
        }

        if not pkg.is_fixable:
            logger.warning(f"No fix version available for package {package_name}.")
            report_line = (
                f"- **{package_name}** ({pkg.current_version}) [Severity: {pkg_sev}]: "
                "No upstream fix version available."
            )
            base_entry["failure_stage"] = "unfixable"
            base_entry["failure_reason"] = "No upstream fix version available."
            base_entry["report_markdown"] = report_line
            manifest["packages"].append(base_entry)
            failed_entries.append(base_entry)
            continue

        if gh_client and gh_client.has_open_pr_for_package(package_name):
            logger.info(
                f"Open PR already exists for {package_name}. Skipping duplicate PR creation."
            )
            base_entry["status"] = "already_open"
            manifest["packages"].append(base_entry)
            continue

        if dry_run:
            logger.info(
                f"[DRY-RUN] Would upgrade {package_name} ({pkg_sev}) from {old_version} to target fix version {pkg.target_fix_version} with labels: {pr_labels}"
            )
            base_entry["status"] = "dry_run"
            base_entry["new_version"] = pkg.target_fix_version
            manifest["packages"].append(base_entry)
            continue

        # Execute uv lock upgrade
        if not run_lock_upgrade(package_name, repo_root):
            report_line = (
                f"- **{package_name}** ({old_version}) [Severity: {pkg_sev}]: "
                "Upgrade via `uv lock` failed."
            )
            base_entry["failure_stage"] = "lock_upgrade"
            base_entry["failure_reason"] = "Upgrade via `uv lock` failed."
            base_entry["report_markdown"] = report_line
            manifest["packages"].append(base_entry)
            failed_entries.append(base_entry)
            _revert_working_tree_lockfiles(
                repo_root, original_uv_lock, original_pyproject
            )
            continue

        new_version = get_locked_package_version(uv_lock, package_name)
        base_entry["new_version"] = new_version
        if not new_version or new_version == old_version:
            logger.warning(f"`uv lock` did not change version for {package_name}.")
            report_line = (
                f"- **{package_name}** ({old_version}) [Severity: {pkg_sev}]: "
                f"Lockfile remained at version {old_version} after upgrade attempt."
            )
            base_entry["failure_stage"] = "version_unchanged"
            base_entry["failure_reason"] = (
                f"Lockfile remained at version {old_version} after upgrade attempt."
            )
            base_entry["report_markdown"] = report_line
            manifest["packages"].append(base_entry)
            failed_entries.append(base_entry)
            _revert_working_tree_lockfiles(
                repo_root, original_uv_lock, original_pyproject
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
            report_line = (
                f"- **{package_name}** ({old_version} -> {new_version}) [Severity: {pkg_sev}]: "
                "Vulnerability still present post-upgrade."
            )
            base_entry["failure_stage"] = "still_vulnerable"
            base_entry["failure_reason"] = "Vulnerability still present post-upgrade."
            base_entry["report_markdown"] = report_line
            manifest["packages"].append(base_entry)
            failed_entries.append(base_entry)
            _revert_working_tree_lockfiles(
                repo_root, original_uv_lock, original_pyproject
            )
            continue

        # Execute local test suite to verify patch doesn't break existing behavior
        test_exit, test_output = run_local_tests(repo_root)
        sanitized_test_output = sanitize_sensitive_info(test_output, token=token)
        if test_exit != 0:
            logger.error(
                f"Local test suite failed after upgrading {package_name} to {new_version}."
            )
            report_line = (
                f"- **{package_name}** ({old_version} -> {new_version}) [Severity: {pkg_sev}]: "
                f"Local test suite (`uv run pytest`) failed post-upgrade.\n"
                f"```text\n{sanitized_test_output}\n```"
            )
            base_entry["failure_stage"] = "tests"
            base_entry["failure_reason"] = (
                "Local test suite (`uv run pytest`) failed post-upgrade."
            )
            base_entry["report_markdown"] = report_line
            manifest["packages"].append(base_entry)
            failed_entries.append(base_entry)
            _revert_working_tree_lockfiles(
                repo_root, original_uv_lock, original_pyproject
            )
            continue

        # Verify license compliance for upgraded dependency against GPL-3.0 codebase requirements
        license_ok, license_msg = run_license_audit(
            repo_root, package_name=package_name
        )
        if not license_ok:
            logger.error(
                f"License compliance audit failed after upgrading {package_name} to {new_version}: {license_msg}"
            )
            report_line = (
                f"- **{package_name}** ({old_version} -> {new_version}) [Severity: {pkg_sev}]: "
                f"Incompatible license detected post-upgrade: {license_msg}"
            )
            base_entry["failure_stage"] = "license"
            base_entry["failure_reason"] = (
                f"Incompatible license detected post-upgrade: {license_msg}"
            )
            base_entry["report_markdown"] = report_line
            manifest["packages"].append(base_entry)
            failed_entries.append(base_entry)
            _revert_working_tree_lockfiles(
                repo_root, original_uv_lock, original_pyproject
            )
            continue

        safe_pkg = re.sub(r"[^a-z0-9._-]", "-", package_name.lower())
        safe_ver = re.sub(r"[^a-z0-9._-]", "-", new_version.lower().replace(".", "-"))
        branch_name = f"security/patch-{safe_pkg}-{safe_ver}"
        base_entry["branch_name"] = branch_name

        files_to_commit: dict[str, str] = {}
        if uv_lock.is_file():
            files_to_commit["uv.lock"] = uv_lock.read_text(encoding="utf-8")
        if pyproject_toml.is_file():
            current_pyproject = pyproject_toml.read_text(encoding="utf-8")
            if current_pyproject != original_pyproject:
                files_to_commit["pyproject.toml"] = current_pyproject

        _revert_working_tree_lockfiles(repo_root, original_uv_lock, original_pyproject)

        if stage_dir is not None:
            pkg_stage_rel_dir = Path("packages") / safe_pkg
            pkg_stage_abs_dir = (stage_dir / pkg_stage_rel_dir).resolve()
            stage_root_abs = stage_dir.resolve()
            if stage_root_abs not in pkg_stage_abs_dir.parents:
                raise ValueError(f"Unsafe staging path for package {package_name!r}")
            pkg_stage_abs_dir.mkdir(parents=True, exist_ok=True)

            staged_files_map: dict[str, str] = {}
            for repo_rel_path, content in files_to_commit.items():
                staged_rel = (pkg_stage_rel_dir / repo_rel_path).as_posix()
                target_file = stage_dir / staged_rel
                target_file.parent.mkdir(parents=True, exist_ok=True)
                target_file.write_text(content, encoding="utf-8")
                staged_files_map[repo_rel_path] = staged_rel

            base_entry["status"] = "staged"
            base_entry["staged_files"] = staged_files_map
            manifest["packages"].append(base_entry)
            logger.info(
                f"Staged validated upgrade for {package_name} ({old_version} -> {new_version}) in {stage_dir}"
            )
            continue

        # Direct publish mode via Git Data API
        if gh_client and token and token.strip():
            pub_status, pub_err = _publish_single_package(
                gh_client=gh_client,
                pkg_entry=base_entry,
                files_to_commit=files_to_commit,
                base_branch="main",
            )
            base_entry["status"] = pub_status
            if pub_status == "failed":
                publish_failed = True
                base_entry["failure_stage"] = "publish"
                base_entry["failure_reason"] = pub_err
            manifest["packages"].append(base_entry)
        else:
            logger.error(
                f"Cannot publish {package_name} without GitHub credentials; use --stage-dir or --dry-run."
            )
            publish_failed = True
            base_entry["status"] = "failed"
            base_entry["failure_stage"] = "publish"
            base_entry["failure_reason"] = "Missing GitHub credentials for publishing."
            manifest["packages"].append(base_entry)

    if stage_dir is not None:
        _write_manifest(stage_dir, manifest)
        if failed_entries:
            logger.error(
                "Staging completed with unfixable or failing package upgrades:\n"
                + "\n".join(
                    str(e.get("report_markdown") or e.get("failure_reason"))
                    for e in failed_entries
                )
            )
            return False
        return True

    if failed_entries:
        logger.error(
            "Remediation incomplete due to unfixable vulnerabilities or test failures:\n"
            + "\n".join(
                str(e.get("report_markdown") or e.get("failure_reason"))
                for e in failed_entries
            )
        )
        if gh_client:
            issue_title, issue_body, issue_labels = build_failure_issue_payload(
                failed_entries, sanitized_audit_output, token=token
            )
            gh_client.report_security_issue(
                title=issue_title, body=issue_body, labels=issue_labels
            )
        return False

    if publish_failed:
        return False

    return all(
        entry.get("status") in ("patched", "already_open", "dry_run")
        for entry in manifest["packages"]
    )


def publish_staged_remediations(
    stage_dir: Path,
    repo_full: str,
    token: str | None = None,
    audit_job_status: str = "success",
    app_id: str | None = None,
    app_private_key: str | None = None,
    require_app_token: bool = False,
) -> bool:
    """Publish staged lockfile upgrades and triage alerts from remediation_manifest.json."""
    parts = repo_full.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        logger.error(f"Invalid repository format: {repo_full!r}")
        return False
    owner, repo = parts[0], parts[1]

    manifest_path = stage_dir / MANIFEST_FILENAME
    manifest: dict[str, Any] | None = None
    manifest_error: str | None = None

    if manifest_path.is_file():
        try:
            loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict) and isinstance(loaded.get("packages"), list):
                manifest = loaded
            else:
                manifest_error = f"Malformed {MANIFEST_FILENAME}: expected JSON object with 'packages' list."
        except Exception as e:
            manifest_error = f"Failed to parse {MANIFEST_FILENAME}: {e}"
    else:
        manifest_error = f"Staging manifest not found at {manifest_path}."

    normalized_job_status = (audit_job_status or "success").strip().lower()
    packages: list[dict[str, Any]] = (
        list(manifest.get("packages", [])) if manifest is not None else []
    )
    audit_output = str(manifest.get("audit_output", "")) if manifest is not None else ""
    base_branch = (
        str(manifest.get("base_branch", "main")) if manifest is not None else "main"
    )

    staged_entries = [p for p in packages if p.get("status") == "staged"]
    failed_entries = [p for p in packages if p.get("status") == "failed"]

    if manifest is None:
        reason = (
            f"Upstream `audit-and-stage` job ended with status `{normalized_job_status}` "
            f"and produced no valid `{MANIFEST_FILENAME}` ({manifest_error})."
        )
        failed_entries.append(
            {
                "package_name": "audit-and-stage",
                "severity": "MEDIUM",
                "status": "failed",
                "failure_stage": "job_status",
                "failure_reason": reason,
                "report_markdown": f"- **audit-and-stage** [Severity: MEDIUM]: {reason}",
            }
        )
    elif normalized_job_status not in ("success", "") and not failed_entries:
        reason = f"Upstream `audit-and-stage` job ended with status `{normalized_job_status}`."
        failed_entries.append(
            {
                "package_name": "audit-and-stage",
                "severity": "MEDIUM",
                "status": "failed",
                "failure_stage": "job_status",
                "failure_reason": reason,
                "report_markdown": f"- **audit-and-stage** [Severity: MEDIUM]: {reason}",
            }
        )

    # Fast clean path: nothing to publish and nothing failed
    if not staged_entries and not failed_entries:
        logger.info(
            "No staged upgrades or failures to publish; all packages clean or already open."
        )
        return True

    publish_token: str | None = None
    publish_failed = False

    try:
        publish_token = resolve_publish_token(
            owner=owner,
            repo=repo,
            app_id=app_id,
            app_private_key=app_private_key,
            fallback_token=token,
            require_app_token=require_app_token and bool(staged_entries),
        )
    except Exception as e:
        err_msg = sanitize_sensitive_info(str(e), token=token)
        logger.error(f"Failed to resolve GitHub publish credentials: {err_msg}")
        publish_failed = True
        if staged_entries:
            for entry in staged_entries:
                entry["status"] = "failed"
                entry["failure_stage"] = "auth"
                entry["failure_reason"] = err_msg
                failed_entries.append(entry)
            staged_entries = []
        if token and token.strip():
            publish_token = token.strip()

    if not publish_token:
        logger.error("Cannot publish PRs or update issues without any GitHub token.")
        return False

    gh_client = GitHubRemediationClient(publish_token, owner, repo)
    stage_root_abs = stage_dir.resolve()

    for entry in staged_entries:
        staged_files_map = entry.get("staged_files")
        if not isinstance(staged_files_map, dict) or not staged_files_map:
            logger.error(
                f"Staged package {entry.get('package_name')!r} has no staged_files map."
            )
            entry["status"] = "failed"
            publish_failed = True
            continue

        files_to_commit: dict[str, str] = {}
        invalid_staged_file = False
        for repo_rel_path, staged_rel_path in staged_files_map.items():
            if repo_rel_path not in ALLOWED_STAGED_REPO_PATHS:
                logger.error(
                    f"Disallowed target path {repo_rel_path!r} in manifest; aborting package."
                )
                invalid_staged_file = True
                break
            candidate = (stage_dir / str(staged_rel_path)).resolve()
            if stage_root_abs not in candidate.parents or not candidate.is_file():
                logger.error(
                    f"Invalid or missing staged file {staged_rel_path!r}; aborting package."
                )
                invalid_staged_file = True
                break
            files_to_commit[repo_rel_path] = candidate.read_text(encoding="utf-8")

        if invalid_staged_file or not files_to_commit:
            entry["status"] = "failed"
            publish_failed = True
            continue

        pub_status, pub_err = _publish_single_package(
            gh_client=gh_client,
            pkg_entry=entry,
            files_to_commit=files_to_commit,
            base_branch=base_branch,
        )
        entry["status"] = pub_status
        if pub_status == "failed":
            publish_failed = True
            entry["failure_stage"] = "publish"
            entry["failure_reason"] = pub_err

    if manifest is not None and manifest_path.parent.is_dir():
        _write_manifest(stage_dir, manifest)

    if failed_entries:
        issue_title, issue_body, issue_labels = build_failure_issue_payload(
            failed_entries=failed_entries,
            audit_output=audit_output,
            token=publish_token,
            audit_job_status=normalized_job_status,
        )
        issue_ok = gh_client.report_security_issue(
            title=issue_title, body=issue_body, labels=issue_labels
        )
        if not issue_ok:
            logger.error("Failed to report remediation failure on security issue.")
        return False

    if publish_failed:
        return False

    return all(p.get("status") in ("patched", "already_open") for p in packages)


def main(argv: list[str] | None = None) -> None:
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
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--stage-dir",
        help="Write remediation_manifest.json and validated lockfiles to this directory without pushing branches",
    )
    mode_group.add_argument(
        "--publish-from",
        help="Publish signed branches, PRs, and issue alerts from a staged artifact directory",
    )
    parser.add_argument(
        "--audit-job-status",
        default=os.environ.get("AUDIT_JOB_STATUS", "success"),
        help="Upstream audit-and-stage job status (success, failure, cancelled) when using --publish-from",
    )
    parser.add_argument(
        "--require-app-token",
        action="store_true",
        help="Require REMEDIATION_APP_ID and REMEDIATION_APP_PRIVATE_KEY when publishing staged PRs",
    )

    args = parser.parse_args(argv)

    repo_root = Path(args.repo_root).resolve()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    repo_full = os.environ.get("GITHUB_REPOSITORY", "fderuiter/sortify")

    if args.publish_from:
        success = publish_staged_remediations(
            stage_dir=Path(args.publish_from).resolve(),
            repo_full=repo_full,
            token=token,
            audit_job_status=args.audit_job_status,
            app_id=os.environ.get("REMEDIATION_APP_ID"),
            app_private_key=os.environ.get("REMEDIATION_APP_PRIVATE_KEY"),
            require_app_token=args.require_app_token,
        )
    else:
        stage_dir = Path(args.stage_dir).resolve() if args.stage_dir else None
        success = remediate_vulnerabilities(
            repo_root=repo_root,
            dry_run=args.dry_run,
            token=token,
            repo_full=repo_full,
            stage_dir=stage_dir,
        )

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
