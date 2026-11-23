"""Automated Dependency CVE Patch Remediation Script for Sortify.

Parses vulnerability reports from `uv audit`, updates lockfiles for fixable package
vulnerabilities via `uv lock --upgrade-package <package>`, creates security branches
and pull requests with appropriate security/dependencies/automerge labels, and notifies
maintainers via issues when unfixable vulnerabilities or CI failures occur.
"""

from __future__ import annotations

import argparse
import ast
import base64
import email
import email.message
import importlib.metadata
import json
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = "remediation_manifest.json"
ALLOWED_STAGED_REPO_PATHS = frozenset(
    {"uv.lock", "pyproject.toml", "offline_bundle/requirements.txt"}
)
SAFE_BRANCH_RE = re.compile(r"^security/patch-[a-z0-9._-]+$")
_STRIPPED_ENV_EXACT = frozenset({"GITHUB_TOKEN", "GH_TOKEN"})
_STRIPPED_ENV_PREFIXES = ("GIT_CONFIG_",)
_PEP503_NORM_RE = re.compile(r"[-_.]+")


def normalize_package_name(name: str) -> str:
    """Normalize a Python distribution name per PEP 503."""
    return _PEP503_NORM_RE.sub("-", name).strip().lower()


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
        r"(https?://)x-access-token:[^@]+@", r"\1x-access-token:[REDACTED]@", text
    )
    text = re.sub(r"(https?://)[^:]+:[^@]+@", r"\1[REDACTED]:[REDACTED]@", text)
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


_KNOWN_IMPORT_NAME_MAP: dict[str, set[str]] = {
    "llama-cpp-python": {"llama_cpp"},
    "pillow": {"PIL"},
    "pydantic-settings": {"pydantic_settings"},
    "python-docx": {"docx"},
    "python-pptx": {"pptx"},
    "pyyaml": {"yaml"},
    "scikit-learn": {"sklearn"},
    "sqlcipher3-wheels": {"sqlcipher3"},
}


def _collect_ast_imports(py_path: Path) -> tuple[set[str], set[str]]:
    """Return (root_imports, full_module_imports) for a Python source file."""
    try:
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
    except Exception:
        return set(), set()

    roots: set[str] = set()
    full_mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
                full_mods.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
            for alias in node.names:
                if alias.name == "*":
                    full_mods.add(node.module)
                else:
                    full_mods.add(f"{node.module}.{alias.name}")
            full_mods.add(node.module)
    return roots, full_mods


def discover_targeted_tests(repo_root: Path, package_name: str) -> list[str]:
    """Identify fast test files affected by package_name via uv.lock reverse deps and AST imports."""
    tests_dir = repo_root / "tests"
    if not tests_dir.is_dir():
        return []

    target_norm = normalize_package_name(package_name)
    closure: set[str] = {target_norm}

    uv_lock_path = repo_root / "uv.lock"
    if uv_lock_path.is_file():
        try:
            lock_data = tomllib.loads(uv_lock_path.read_text(encoding="utf-8"))
            rev_deps: dict[str, set[str]] = {}
            for pkg in lock_data.get("package", []):
                if not isinstance(pkg, dict):
                    continue
                p_name = normalize_package_name(str(pkg.get("name", "")))
                if not p_name or p_name == "smart-autosorter":
                    continue
                for dep in pkg.get("dependencies", []):
                    if isinstance(dep, dict) and dep.get("name"):
                        rev_deps.setdefault(
                            normalize_package_name(str(dep["name"])), set()
                        ).add(p_name)
                opt_map = pkg.get("optional-dependencies")
                if isinstance(opt_map, dict):
                    for extra_deps in opt_map.values():
                        if isinstance(extra_deps, list):
                            for dep in extra_deps:
                                if isinstance(dep, dict) and dep.get("name"):
                                    rev_deps.setdefault(
                                        normalize_package_name(str(dep["name"])), set()
                                    ).add(p_name)
            queue = [target_norm]
            while queue:
                cur = queue.pop()
                for parent in rev_deps.get(cur, ()):
                    if parent not in closure:
                        closure.add(parent)
                        queue.append(parent)
        except Exception as e:
            logger.debug(
                f"Failed to parse uv.lock for reverse dependency discovery: {e}"
            )

    dist_to_imports: dict[str, set[str]] = {
        k: set(v) for k, v in _KNOWN_IMPORT_NAME_MAP.items()
    }
    try:
        for dist in importlib.metadata.distributions():
            d_name = dist.metadata.get("Name")
            if not d_name:
                continue
            norm_d = normalize_package_name(d_name)
            top_txt = dist.read_text("top_level.txt")
            if top_txt:
                mods = {
                    line.strip().split("/")[0]
                    for line in top_txt.splitlines()
                    if line.strip()
                }
                if mods:
                    dist_to_imports.setdefault(norm_d, set()).update(mods)
                    continue
            dist_to_imports.setdefault(norm_d, set()).add(norm_d.replace("-", "_"))
    except Exception:
        pass

    import_roots: set[str] = set()
    for dist_name in closure:
        import_roots.update(
            dist_to_imports.get(dist_name, {dist_name.replace("-", "_")})
        )
        if dist_name.startswith("jaraco-"):
            import_roots.add("jaraco")

    affected_first_party: set[str] = set()
    for folder in ("app", "scripts"):
        base = repo_root / folder
        if not base.is_dir():
            continue
        for py_file in base.rglob("*.py"):
            if py_file.name == "__init__.py":
                continue
            roots, _ = _collect_ast_imports(py_file)
            if roots & import_roots:
                rel = py_file.relative_to(repo_root).with_suffix("")
                affected_first_party.add(".".join(rel.parts))

    matched_tests: list[str] = []
    for test_file in sorted(tests_dir.rglob("test_*.py")):
        roots, full_mods = _collect_ast_imports(test_file)
        if roots & import_roots:
            matched_tests.append(test_file.relative_to(repo_root).as_posix())
            continue
        if affected_first_party and any(
            imp == fp or imp.startswith(fp + ".")
            for imp in full_mods
            for fp in affected_first_party
        ):
            matched_tests.append(test_file.relative_to(repo_root).as_posix())

    return matched_tests


def _extract_failed_junit_nodeids(junit_xml_path: Path, repo_root: Path) -> set[str]:
    """Parse failing/erroring pytest node IDs from a JUnit XML report."""
    if not junit_xml_path.is_file():
        return set()
    try:
        tree = ET.parse(junit_xml_path)
    except Exception:
        return set()

    failed: set[str] = set()
    for testcase in tree.iter("testcase"):
        has_problem = (
            testcase.find("failure") is not None or testcase.find("error") is not None
        )
        if not has_problem:
            continue
        classname = (testcase.attrib.get("classname") or "").strip()
        name = (testcase.attrib.get("name") or "").strip()
        file_attr = (testcase.attrib.get("file") or "").strip()
        if not name:
            continue

        if classname:
            parts = classname.split(".")
            resolved_nodeid: str | None = None
            for split_idx in range(len(parts), 0, -1):
                mod_parts = parts[:split_idx]
                candidate_py = repo_root.joinpath(
                    *mod_parts[:-1], f"{mod_parts[-1]}.py"
                )
                if candidate_py.is_file():
                    rel_py = candidate_py.relative_to(repo_root).as_posix()
                    class_parts = parts[split_idx:]
                    if class_parts:
                        resolved_nodeid = f"{rel_py}::{'::'.join(class_parts)}::{name}"
                    else:
                        resolved_nodeid = f"{rel_py}::{name}"
                    break
            if resolved_nodeid is None:
                if file_attr:
                    resolved_nodeid = f"{file_attr}::{name}"
                else:
                    resolved_nodeid = f"{classname.replace('.', '/')}.py::{name}"
            failed.add(resolved_nodeid)
        elif file_attr:
            failed.add(f"{file_attr}::{name}")
    return failed


def run_import_smoke_check(
    repo_root: Path, package_name: str | None = None
) -> tuple[int, str]:
    """Run a fast Python import smoke check on core application modules and installed package."""
    target_norm = normalize_package_name(package_name) if package_name else ""
    smoke_code = (
        "import importlib, importlib.metadata, re\n"
        "import app, app.config, app.core, app.main\n"
        f"target = {target_norm!r}\n"
        "if target:\n"
        "    for d in importlib.metadata.distributions():\n"
        "        nm = re.sub(r'[-_.]+', '-', (d.metadata.get('Name') or '')).strip().lower()\n"
        "        if nm == target:\n"
        "            top = d.read_text('top_level.txt') or ''\n"
        "            for line in top.splitlines():\n"
        "                mod = line.strip().split('/')[0]\n"
        "                if mod and not mod.startswith('_'):\n"
        "                    importlib.import_module(mod)\n"
        "            break\n"
    )
    cmd = ["uv", "run", "python", "-c", smoke_code]
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
    combined = (res.stdout + "\n" + res.stderr).strip()
    return res.returncode, combined


def run_local_tests(
    repo_root: Path,
    package_name: str | None = None,
    *,
    baseline_uv_lock: str | None = None,
    baseline_pyproject: str | None = None,
) -> tuple[int, str]:
    """Execute fast import smoke checks and targeted pytest verification with baseline comparison."""
    failures_file = repo_root / "pytest_failures.txt"
    had_failures_file = failures_file.exists()
    env = build_sanitized_subprocess_env(
        {"PYTHON_KEYRING_BACKEND": "keyring.backends.fail.Keyring"}
    )

    try:
        if package_name is None:
            cmd = ["uv", "run", "pytest", "-m", "not slow"]
            logger.info(f"Executing local test suite: {' '.join(cmd)}")
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

        if (repo_root / "app").is_dir():
            smoke_rc, smoke_out = run_import_smoke_check(repo_root, package_name)
            if smoke_rc != 0:
                logger.error(
                    f"Import smoke check failed after upgrading {package_name}: {smoke_out}"
                )
                return smoke_rc, f"Import smoke check failed:\n{smoke_out}"

        tests_dir = repo_root / "tests"
        if not tests_dir.is_dir():
            cmd = ["uv", "run", "pytest", "-m", "not slow"]
            res = subprocess.run(
                cmd,
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
            return res.returncode, (res.stdout + "\n" + res.stderr).strip()

        targeted_files = discover_targeted_tests(repo_root, package_name)
        if not targeted_files:
            smoke_test_file = tests_dir / "test_smoke.py"
            if smoke_test_file.is_file():
                targeted_files = ["tests/test_smoke.py"]
            else:
                logger.info(
                    f"Import smoke check passed; no first-party or test modules import {package_name}."
                )
                return (
                    0,
                    f"Import smoke check passed; no targeted test modules depend on {package_name}.",
                )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_dir_path = Path(tmp_dir)
            upgrade_xml = tmp_dir_path / "upgrade_junit.xml"
            cmd = [
                "uv",
                "run",
                "pytest",
                "--no-cov",
                "-m",
                "not slow",
                f"--junitxml={upgrade_xml}",
                *targeted_files,
            ]
            logger.info(
                f"Executing targeted test suite ({len(targeted_files)} file(s)) for {package_name}"
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
            if res.returncode == 0:
                return 0, combined_output

            if baseline_uv_lock is None:
                return res.returncode, combined_output

            post_failures = _extract_failed_junit_nodeids(upgrade_xml, repo_root)
            if not post_failures:
                return res.returncode, combined_output

            uv_lock_file = repo_root / "uv.lock"
            pyproject_file = repo_root / "pyproject.toml"
            upgraded_lock = (
                uv_lock_file.read_text(encoding="utf-8")
                if uv_lock_file.is_file()
                else None
            )
            upgraded_pyproject = (
                pyproject_file.read_text(encoding="utf-8")
                if pyproject_file.is_file()
                else None
            )

            base_xml = tmp_dir_path / "baseline_junit.xml"
            try:
                uv_lock_file.write_text(baseline_uv_lock, encoding="utf-8")
                if baseline_pyproject is not None and pyproject_file.is_file():
                    pyproject_file.write_text(baseline_pyproject, encoding="utf-8")

                base_cmd = [
                    "uv",
                    "run",
                    "pytest",
                    "--no-cov",
                    "-n",
                    "0",
                    f"--junitxml={base_xml}",
                    *sorted(post_failures),
                ]
                logger.info(
                    f"Comparing {len(post_failures)} failing test(s) against baseline lockfile"
                )
                subprocess.run(
                    base_cmd,
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=env,
                )
            finally:
                if upgraded_lock is not None:
                    uv_lock_file.write_text(upgraded_lock, encoding="utf-8")
                if upgraded_pyproject is not None and pyproject_file.is_file():
                    pyproject_file.write_text(upgraded_pyproject, encoding="utf-8")

            baseline_failures = _extract_failed_junit_nodeids(base_xml, repo_root)
            regressions = post_failures - baseline_failures
            if not regressions:
                logger.warning(
                    f"Ignoring {len(post_failures)} pre-existing baseline failure(s) unrelated to "
                    f"{package_name} upgrade: {sorted(post_failures)}"
                )
                return (
                    0,
                    f"Pre-existing baseline test failures ignored: {', '.join(sorted(post_failures))}",
                )
            return res.returncode, combined_output
    except Exception as e:
        logger.error(f"Failed to execute local pytest runner: {e}")
        return 1, str(e)
    finally:
        if not had_failures_file and failures_file.exists():
            try:
                failures_file.unlink()
            except OSError:
                pass


@dataclass
class VulnerabilityDetail:
    vuln_id: str
    summary: str
    fixed_in: str | None = None
    advisory_url: str | None = None
    severity: str = "MEDIUM"
    cvss_score: float | None = None
    aliases: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "vuln_id": self.vuln_id,
            "summary": self.summary,
            "fixed_in": self.fixed_in,
            "advisory_url": self.advisory_url,
            "severity": self.severity,
            "cvss_score": self.cvss_score,
            "aliases": list(self.aliases),
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


def _roundup_cvss_v3(val: float) -> float:
    """FIRST.org CVSS v3.0/v3.1 Roundup function to 1 decimal place."""
    int_input = round(val * 100000)
    if int_input % 10000 == 0:
        return int_input / 100000.0
    return (math.floor(int_input / 10000) + 1) / 10.0


# FIRST.org CVSS v4.0 MacroVector baseline lookup table (EQ1..EQ6)
_CVSS_V4_LOOKUP: dict[str, float] = {
    "000000": 10.0,
    "000001": 9.9,
    "000010": 9.8,
    "000011": 9.5,
    "000020": 9.5,
    "000021": 9.2,
    "000100": 10.0,
    "000101": 9.6,
    "000110": 9.3,
    "000111": 8.7,
    "000120": 9.1,
    "000121": 8.1,
    "000200": 9.3,
    "000201": 9.0,
    "000210": 8.9,
    "000211": 8.0,
    "000220": 8.1,
    "000221": 6.8,
    "001000": 9.8,
    "001001": 9.5,
    "001010": 9.5,
    "001011": 9.2,
    "001020": 9.0,
    "001021": 8.4,
    "001100": 9.3,
    "001101": 9.2,
    "001110": 8.9,
    "001111": 8.1,
    "001120": 8.1,
    "001121": 6.5,
    "001200": 8.8,
    "001201": 8.0,
    "001210": 7.8,
    "001211": 7.0,
    "001220": 6.9,
    "001221": 4.8,
    "002001": 9.2,
    "002011": 8.2,
    "002021": 7.2,
    "002101": 7.9,
    "002111": 6.9,
    "002121": 5.0,
    "002201": 6.9,
    "002211": 5.5,
    "002221": 2.7,
    "010000": 9.9,
    "010001": 9.7,
    "010010": 9.5,
    "010011": 9.2,
    "010020": 9.2,
    "010021": 8.5,
    "010100": 9.5,
    "010101": 9.1,
    "010110": 9.0,
    "010111": 8.3,
    "010120": 8.4,
    "010121": 7.1,
    "010200": 9.2,
    "010201": 8.1,
    "010210": 8.2,
    "010211": 7.1,
    "010220": 7.2,
    "010221": 5.3,
    "011000": 9.5,
    "011001": 9.3,
    "011010": 9.2,
    "011011": 8.5,
    "011020": 8.5,
    "011021": 7.3,
    "011100": 9.2,
    "011101": 8.2,
    "011110": 8.0,
    "011111": 7.2,
    "011120": 7.0,
    "011121": 5.9,
    "011200": 8.4,
    "011201": 7.0,
    "011210": 7.1,
    "011211": 5.2,
    "011220": 5.0,
    "011221": 3.0,
    "012001": 8.6,
    "012011": 7.5,
    "012021": 5.2,
    "012101": 7.1,
    "012111": 5.2,
    "012121": 2.9,
    "012201": 6.3,
    "012211": 2.9,
    "012221": 1.7,
    "100000": 9.8,
    "100001": 9.5,
    "100010": 9.4,
    "100011": 8.7,
    "100020": 9.1,
    "100021": 8.1,
    "100100": 9.4,
    "100101": 8.9,
    "100110": 8.6,
    "100111": 7.4,
    "100120": 7.7,
    "100121": 6.4,
    "100200": 8.7,
    "100201": 7.5,
    "100210": 7.4,
    "100211": 6.3,
    "100220": 6.3,
    "100221": 4.9,
    "101000": 9.4,
    "101001": 8.9,
    "101010": 8.8,
    "101011": 7.7,
    "101020": 7.6,
    "101021": 6.7,
    "101100": 8.6,
    "101101": 7.6,
    "101110": 7.4,
    "101111": 5.8,
    "101120": 5.9,
    "101121": 5.0,
    "101200": 7.2,
    "101201": 5.7,
    "101210": 5.7,
    "101211": 5.2,
    "101220": 5.2,
    "101221": 2.5,
    "102001": 8.3,
    "102011": 7.0,
    "102021": 5.4,
    "102101": 6.5,
    "102111": 5.8,
    "102121": 2.6,
    "102201": 5.3,
    "102211": 2.1,
    "102221": 1.3,
    "110000": 9.5,
    "110001": 9.0,
    "110010": 8.8,
    "110011": 7.6,
    "110020": 7.6,
    "110021": 7.0,
    "110100": 9.0,
    "110101": 7.7,
    "110110": 7.5,
    "110111": 6.2,
    "110120": 6.1,
    "110121": 5.3,
    "110200": 7.7,
    "110201": 6.6,
    "110210": 6.8,
    "110211": 5.9,
    "110220": 5.2,
    "110221": 3.0,
    "111000": 8.9,
    "111001": 7.8,
    "111010": 7.6,
    "111011": 6.7,
    "111020": 6.2,
    "111021": 5.8,
    "111100": 7.4,
    "111101": 5.9,
    "111110": 5.7,
    "111111": 5.7,
    "111120": 4.7,
    "111121": 2.3,
    "111200": 6.1,
    "111201": 5.2,
    "111210": 5.7,
    "111211": 2.9,
    "111220": 2.4,
    "111221": 1.6,
    "112001": 7.1,
    "112011": 5.9,
    "112021": 3.0,
    "112101": 5.8,
    "112111": 2.6,
    "112121": 1.5,
    "112201": 2.3,
    "112211": 1.3,
    "112221": 0.6,
    "200000": 9.3,
    "200001": 8.7,
    "200010": 8.6,
    "200011": 7.2,
    "200020": 7.5,
    "200021": 5.8,
    "200100": 8.6,
    "200101": 7.4,
    "200110": 7.4,
    "200111": 6.1,
    "200120": 5.6,
    "200121": 3.4,
    "200200": 7.0,
    "200201": 5.4,
    "200210": 5.2,
    "200211": 4.0,
    "200220": 4.0,
    "200221": 2.2,
    "201000": 8.5,
    "201001": 7.5,
    "201010": 7.4,
    "201011": 5.5,
    "201020": 6.2,
    "201021": 5.1,
    "201100": 7.2,
    "201101": 5.7,
    "201110": 5.5,
    "201111": 4.1,
    "201120": 4.6,
    "201121": 1.9,
    "201200": 5.3,
    "201201": 3.6,
    "201210": 3.4,
    "201211": 1.9,
    "201220": 1.9,
    "201221": 0.8,
    "202001": 6.4,
    "202011": 5.1,
    "202021": 2.0,
    "202101": 4.7,
    "202111": 2.1,
    "202121": 1.1,
    "202201": 2.4,
    "202211": 0.9,
    "202221": 0.4,
    "210000": 8.8,
    "210001": 7.5,
    "210010": 7.3,
    "210011": 5.3,
    "210020": 6.0,
    "210021": 5.0,
    "210100": 7.3,
    "210101": 5.5,
    "210110": 5.9,
    "210111": 4.0,
    "210120": 4.1,
    "210121": 2.0,
    "210200": 5.4,
    "210201": 4.3,
    "210210": 4.5,
    "210211": 2.2,
    "210220": 2.0,
    "210221": 1.1,
    "211000": 7.5,
    "211001": 5.5,
    "211010": 5.8,
    "211011": 4.5,
    "211020": 4.0,
    "211021": 2.1,
    "211100": 6.1,
    "211101": 5.1,
    "211110": 4.8,
    "211111": 1.8,
    "211120": 2.0,
    "211121": 0.9,
    "211200": 4.6,
    "211201": 1.8,
    "211210": 1.7,
    "211211": 0.7,
    "211220": 0.8,
    "211221": 0.2,
    "212001": 5.3,
    "212011": 2.4,
    "212021": 1.4,
    "212101": 2.4,
    "212111": 1.2,
    "212121": 0.5,
    "212201": 1.0,
    "212211": 0.3,
    "212221": 0.1,
}


def parse_cvss_vector_score(vector: str | None) -> float | None:
    """Compute a numeric CVSS base score from a CVSS v2.0, v3.0, v3.1, or v4.0 vector string."""
    if not vector or not isinstance(vector, str):
        return None
    cleaned = vector.strip().strip("()")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        pass

    parts = [p.strip() for p in cleaned.split("/") if p.strip()]
    if not parts:
        return None

    prefix = parts[0].upper()
    metric_parts = parts[1:] if prefix.startswith("CVSS:") else parts
    metrics: dict[str, str] = {}
    for item in metric_parts:
        if ":" not in item:
            return None
        k, v = item.split(":", 1)
        metrics[k.strip().upper()] = v.strip().upper()

    if prefix in ("CVSS:3.0", "CVSS:3.1"):
        av = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.20}.get(metrics.get("AV", ""))
        ac = {"L": 0.77, "H": 0.44}.get(metrics.get("AC", ""))
        ui = {"N": 0.85, "R": 0.62}.get(metrics.get("UI", ""))
        scope = metrics.get("S", "")
        if scope == "U":
            pr = {"N": 0.85, "L": 0.62, "H": 0.27}.get(metrics.get("PR", ""))
        elif scope == "C":
            pr = {"N": 0.85, "L": 0.68, "H": 0.50}.get(metrics.get("PR", ""))
        else:
            return None
        cia = {"H": 0.56, "L": 0.22, "N": 0.0}
        c = cia.get(metrics.get("C", ""))
        i = cia.get(metrics.get("I", ""))
        a = cia.get(metrics.get("A", ""))
        if None in (av, ac, pr, ui, c, i, a):
            return None
        assert av is not None and ac is not None and pr is not None and ui is not None
        assert c is not None and i is not None and a is not None
        iss = 1.0 - ((1.0 - c) * (1.0 - i) * (1.0 - a))
        if scope == "U":
            impact = 6.42 * iss
        else:
            impact = 7.52 * (iss - 0.029) - 3.25 * ((iss - 0.02) ** 15)
        if impact <= 0:
            return 0.0
        exploitability = 8.22 * av * ac * pr * ui
        if scope == "U":
            return _roundup_cvss_v3(min(impact + exploitability, 10.0))
        return _roundup_cvss_v3(min(1.08 * (impact + exploitability), 10.0))

    if prefix == "CVSS:4.0":
        av_v = metrics.get("AV", "")
        ac_v = metrics.get("AC", "")
        at_v = metrics.get("AT", "")
        pr_v = metrics.get("PR", "")
        ui_v = metrics.get("UI", "")
        vc_v = metrics.get("VC", "")
        vi_v = metrics.get("VI", "")
        va_v = metrics.get("VA", "")
        sc_v = metrics.get("SC", "")
        si_v = metrics.get("SI", "")
        sa_v = metrics.get("SA", "")
        if not all(
            [
                av_v in ("N", "A", "L", "P"),
                ac_v in ("L", "H"),
                at_v in ("N", "P"),
                pr_v in ("N", "L", "H"),
                ui_v in ("N", "P", "A"),
                vc_v in ("H", "L", "N"),
                vi_v in ("H", "L", "N"),
                va_v in ("H", "L", "N"),
                sc_v in ("H", "L", "N"),
                si_v in ("H", "L", "N"),
                sa_v in ("H", "L", "N"),
            ]
        ):
            return None
        if all(x == "N" for x in (vc_v, vi_v, va_v, sc_v, si_v, sa_v)):
            return 0.0
        if av_v == "N" and pr_v == "N" and ui_v == "N":
            eq1 = "0"
        elif (
            (av_v == "N" or pr_v == "N" or ui_v == "N")
            and not (av_v == "N" and pr_v == "N" and ui_v == "N")
            and av_v != "P"
        ):
            eq1 = "1"
        else:
            eq1 = "2"
        eq2 = "0" if (ac_v == "L" and at_v == "N") else "1"
        if vc_v == "H" and vi_v == "H":
            eq3 = "0"
        elif not (vc_v == "H" and vi_v == "H") and (
            vc_v == "H" or vi_v == "H" or va_v == "H"
        ):
            eq3 = "1"
        else:
            eq3 = "2"
        if sc_v == "H" or si_v == "H" or sa_v == "H":
            eq4 = "1"
        else:
            eq4 = "2"
        e_v = metrics.get("E", "A")
        if e_v == "X":
            e_v = "A"
        eq5 = "0" if e_v == "A" else ("1" if e_v == "P" else "2")
        cr_v = metrics.get("CR", "H")
        ir_v = metrics.get("IR", "H")
        ar_v = metrics.get("AR", "H")
        if cr_v == "X":
            cr_v = "H"
        if ir_v == "X":
            ir_v = "H"
        if ar_v == "X":
            ar_v = "H"
        if (
            (cr_v == "H" and vc_v == "H")
            or (ir_v == "H" and vi_v == "H")
            or (ar_v == "H" and va_v == "H")
        ):
            eq6 = "0"
        else:
            eq6 = "1"
        return _CVSS_V4_LOOKUP.get(f"{eq1}{eq2}{eq3}{eq4}{eq5}{eq6}")

    if prefix == "CVSS:2.0" or ("AV" in metrics and "AU" in metrics):
        av2 = {"L": 0.395, "A": 0.646, "N": 1.0}.get(metrics.get("AV", ""))
        ac2 = {"H": 0.35, "M": 0.61, "L": 0.71}.get(metrics.get("AC", ""))
        au2 = {"M": 0.45, "S": 0.56, "N": 0.704}.get(metrics.get("AU", ""))
        cia2 = {"N": 0.0, "P": 0.275, "C": 0.660}
        c2 = cia2.get(metrics.get("C", ""))
        i2 = cia2.get(metrics.get("I", ""))
        a2 = cia2.get(metrics.get("A", ""))
        if None in (av2, ac2, au2, c2, i2, a2):
            return None
        assert av2 is not None and ac2 is not None and au2 is not None
        assert c2 is not None and i2 is not None and a2 is not None
        impact2 = 10.41 * (1.0 - (1.0 - c2) * (1.0 - i2) * (1.0 - a2))
        if impact2 <= 0:
            return 0.0
        exploit2 = 20.0 * av2 * ac2 * au2
        f_imp = 1.176
        return round(((0.6 * impact2) + (0.4 * exploit2) - 1.5) * f_imp, 1)

    return None


_ADVISORY_CACHE: dict[str, tuple[str, float | None]] = {}
_SEVERITY_RANK = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}


def _extract_score_from_value(raw_score: Any) -> float | None:
    if isinstance(raw_score, (int, float)):
        return float(raw_score)
    if isinstance(raw_score, str):
        return parse_cvss_vector_score(raw_score)
    return None


def _query_single_advisory(
    vuln_id: str, timeout: float
) -> tuple[str | None, float | None, list[str]]:
    """Query OSV and (for GHSA IDs) GitHub Advisory API for a single vulnerability ID."""
    vid_clean = vuln_id.strip()
    vid_upper = vid_clean.upper()
    discovered_aliases: list[str] = []
    explicit_sev: str | None = None
    scores: list[float] = []

    try:
        url = f"https://api.osv.dev/v1/vulns/{vid_clean}"
        resp = requests.get(url, timeout=min(timeout, 5.0))
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                raw_aliases = data.get("aliases")
                if isinstance(raw_aliases, list):
                    for al in raw_aliases:
                        if isinstance(al, str) and al.strip():
                            discovered_aliases.append(al.strip())

                db_spec = data.get("database_specific", {})
                if isinstance(db_spec, dict):
                    if db_spec.get("severity"):
                        explicit_sev = normalize_severity(str(db_spec["severity"]))
                    if isinstance(db_spec.get("cvss"), dict):
                        cvss_dict = db_spec["cvss"]
                        for key in ("score", "vector_string"):
                            sc = _extract_score_from_value(cvss_dict.get(key))
                            if sc is not None:
                                scores.append(sc)
                                break

                sev_list = data.get("severity")
                if isinstance(sev_list, list):
                    v3_scores: list[float] = []
                    other_scores: list[float] = []
                    for sev_item in sev_list:
                        if not isinstance(sev_item, dict):
                            continue
                        sc = _extract_score_from_value(sev_item.get("score"))
                        if sc is not None:
                            sev_type = str(sev_item.get("type", "")).upper()
                            if "V3" in sev_type or str(
                                sev_item.get("score", "")
                            ).upper().startswith(("CVSS:3.0", "CVSS:3.1")):
                                v3_scores.append(sc)
                            else:
                                other_scores.append(sc)
                    if v3_scores:
                        scores.extend(v3_scores)
                    elif other_scores:
                        scores.extend(other_scores)
    except Exception as e:
        logger.debug(f"OSV lookup failed for {vid_clean}: {e}")

    if vid_upper.startswith("GHSA-") and not scores:
        try:
            gh_token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
            headers = {"Accept": "application/vnd.github.v3+json"}
            if gh_token:
                headers["Authorization"] = f"Bearer {gh_token}"
            url = f"https://api.github.com/advisories/{vid_clean}"
            resp = requests.get(url, headers=headers, timeout=min(timeout, 5.0))
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    cve_id = data.get("cve_id")
                    if isinstance(cve_id, str) and cve_id.strip():
                        discovered_aliases.append(cve_id.strip())
                    identifiers = data.get("identifiers")
                    if isinstance(identifiers, list):
                        for ident in identifiers:
                            if isinstance(ident, dict) and isinstance(
                                ident.get("value"), str
                            ):
                                discovered_aliases.append(ident["value"].strip())

                    gh_sev = data.get("severity")
                    if gh_sev:
                        norm_gh = normalize_severity(str(gh_sev))
                        if explicit_sev is None or _SEVERITY_RANK.get(
                            norm_gh, 0
                        ) > _SEVERITY_RANK.get(explicit_sev, 0):
                            explicit_sev = norm_gh

                    cvss_data = data.get("cvss")
                    if isinstance(cvss_data, dict):
                        for key in ("score", "vector_string"):
                            sc = _extract_score_from_value(cvss_data.get(key))
                            if sc is not None:
                                scores.append(sc)
                                break
                    cvss_sevs = data.get("cvss_severities")
                    if isinstance(cvss_sevs, dict):
                        for sub in ("cvss_v3", "cvss_v4"):
                            sub_obj = cvss_sevs.get(sub)
                            if isinstance(sub_obj, dict):
                                for key in ("score", "vector_string"):
                                    sc = _extract_score_from_value(sub_obj.get(key))
                                    if sc is not None:
                                        scores.append(sc)
                                        break
        except Exception as e:
            logger.debug(f"GitHub Advisory lookup failed for {vid_clean}: {e}")

    best_score = max(scores) if scores else None
    best_sev = explicit_sev
    if best_score is not None:
        score_sev = cvss_to_severity(best_score)
        if best_sev is None or _SEVERITY_RANK.get(score_sev, 0) > _SEVERITY_RANK.get(
            best_sev, 0
        ):
            best_sev = score_sev

    return best_sev, best_score, discovered_aliases


def fetch_advisory_severity_and_cvss(
    vuln_id: str,
    timeout: float = 5.0,
    aliases: tuple[str, ...] | list[str] = (),
) -> tuple[str, float | None]:
    """Query remote vulnerability advisory endpoints (OSV / GitHub Advisory API) and aliases for severity and CVSS score.

    Returns (severity_str, cvss_score_float).
    Missing data or network failures default severity to 'MEDIUM' with cvss_score None.
    """
    if not vuln_id or not vuln_id.strip():
        return "MEDIUM", None

    alias_clean = tuple(
        sorted({a.strip().upper() for a in aliases if isinstance(a, str) and a.strip()})
    )
    cache_key = (
        vuln_id.strip().upper()
        if not alias_clean
        else f"{vuln_id.strip().upper()}|{'|'.join(alias_clean)}"
    )
    if cache_key in _ADVISORY_CACHE:
        return _ADVISORY_CACHE[cache_key]

    queue: list[str] = [vuln_id.strip()]
    for al in aliases:
        if isinstance(al, str) and al.strip():
            queue.append(al.strip())

    visited: set[str] = set()
    best_sev: str | None = None
    best_score: float | None = None

    while queue:
        cur_id = queue.pop(0)
        cur_key = cur_id.upper()
        if cur_key in visited:
            continue
        visited.add(cur_key)

        rec_sev, rec_score, rec_aliases = _query_single_advisory(cur_id, timeout)
        if rec_score is not None and (best_score is None or rec_score > best_score):
            best_score = rec_score
        if rec_sev is not None and (
            best_sev is None
            or _SEVERITY_RANK.get(rec_sev, 0) > _SEVERITY_RANK.get(best_sev, 0)
        ):
            best_sev = rec_sev

        for nxt in rec_aliases:
            if nxt.upper() not in visited:
                queue.append(nxt)

    if best_score is not None:
        score_sev = cvss_to_severity(best_score)
        if best_sev is None or _SEVERITY_RANK.get(score_sev, 0) > _SEVERITY_RANK.get(
            best_sev, 0
        ):
            best_sev = score_sev

    final_sev = best_sev if best_sev is not None else "MEDIUM"
    _ADVISORY_CACHE[cache_key] = (final_sev, best_score)
    return final_sev, best_score


def _finalize_vulnerability_detail(vuln: VulnerabilityDetail) -> None:
    if vuln.cvss_score is not None:
        vuln.severity = cvss_to_severity(vuln.cvss_score)
    elif vuln.severity == "MEDIUM" or not vuln.severity:
        sev, cvss = fetch_advisory_severity_and_cvss(vuln.vuln_id, aliases=vuln.aliases)
        vuln.severity = sev
        vuln.cvss_score = cvss


def _version_sort_key(ver: str) -> tuple[tuple[int, int | str], ...]:
    tokens = re.findall(r"\d+|[a-zA-Z]+", ver)
    key: list[tuple[int, int | str]] = []
    for tok in tokens:
        if tok.isdigit():
            key.append((1, int(tok)))
        else:
            key.append((0, tok.lower()))
    return tuple(key)


@dataclass
class PackageVulnerability:
    package_name: str
    current_version: str
    vulnerabilities: list[VulnerabilityDetail] = field(default_factory=list)

    @property
    def target_fix_version(self) -> str | None:
        candidates = [
            v.fixed_in.strip()
            for v in self.vulnerabilities
            if v.fixed_in and v.fixed_in.strip() and v.fixed_in.strip().lower() != "n/a"
        ]
        if not candidates:
            return None
        return max(candidates, key=_version_sort_key)

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
    """Execute uv audit in JSON format ignoring exempted vulnerability IDs and return (exit_code, stdout)."""
    cmd = ["uv", "audit", "--output-format", "json", "--color", "never"]
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
        stdout_clean = (res.stdout or "").strip()
        stderr_clean = (res.stderr or "").strip()
        if stderr_clean:
            logger.debug(f"uv audit stderr diagnostics: {stderr_clean}")
        if stdout_clean:
            return res.returncode, stdout_clean
        return res.returncode, stderr_clean
    except Exception as e:
        logger.error(f"Failed to run uv audit: {e}")
        return 1, str(e)


def _parse_json_audit_output(payload: dict[str, Any]) -> list[PackageVulnerability]:
    """Parse structured JSON output from `uv audit --output-format json`."""
    raw_vulns = payload.get("vulnerabilities", [])
    if not isinstance(raw_vulns, list):
        return []

    grouped: dict[tuple[str, str], PackageVulnerability] = {}
    seen_ids_per_pkg: dict[tuple[str, str], set[str]] = {}

    for item in raw_vulns:
        if not isinstance(item, dict):
            continue
        dep = item.get("dependency")
        if not isinstance(dep, dict):
            continue
        pkg_name = str(dep.get("name") or "").strip()
        pkg_ver = str(dep.get("version") or "").strip()
        if not pkg_name or not pkg_ver:
            continue

        vuln_id = str(item.get("id") or item.get("display_id") or "").strip()
        if not vuln_id:
            continue

        key = (normalize_package_name(pkg_name), pkg_ver)
        if key not in grouped:
            grouped[key] = PackageVulnerability(
                package_name=pkg_name,
                current_version=pkg_ver,
            )
            seen_ids_per_pkg[key] = set()

        if vuln_id.upper() in seen_ids_per_pkg[key]:
            continue
        seen_ids_per_pkg[key].add(vuln_id.upper())

        alias_set: list[str] = []
        display_id = item.get("display_id")
        if (
            isinstance(display_id, str)
            and display_id.strip()
            and display_id.strip().upper() != vuln_id.upper()
        ):
            alias_set.append(display_id.strip())
        raw_aliases = item.get("aliases")
        if isinstance(raw_aliases, list):
            for al in raw_aliases:
                if (
                    isinstance(al, str)
                    and al.strip()
                    and al.strip().upper() != vuln_id.upper()
                    and al.strip() not in alias_set
                ):
                    alias_set.append(al.strip())

        fix_versions = item.get("fix_versions")
        fixed_in: str | None = None
        if isinstance(fix_versions, list):
            valid_fixes = [
                str(fv).strip()
                for fv in fix_versions
                if fv is not None
                and str(fv).strip()
                and str(fv).strip().lower() != "n/a"
            ]
            if valid_fixes:
                fixed_in = max(valid_fixes, key=_version_sort_key)
        elif isinstance(item.get("fixed_in"), str):
            fixed_in = item["fixed_in"].strip() or None

        summary = str(item.get("summary") or item.get("description") or "").strip()
        if "\n" in summary:
            summary = summary.splitlines()[0].strip()

        link = item.get("link") or item.get("advisory_url")
        advisory_url = (
            str(link).strip() if isinstance(link, str) and link.strip() else None
        )

        raw_sev = item.get("severity")
        raw_cvss = _extract_score_from_value(item.get("cvss_score") or item.get("cvss"))

        detail = VulnerabilityDetail(
            vuln_id=vuln_id,
            summary=summary,
            fixed_in=fixed_in,
            advisory_url=advisory_url,
            severity=normalize_severity(str(raw_sev)) if raw_sev else "MEDIUM",
            cvss_score=raw_cvss,
            aliases=tuple(alias_set),
        )
        _finalize_vulnerability_detail(detail)
        grouped[key].vulnerabilities.append(detail)

    return list(grouped.values())


def parse_audit_output(output: str) -> list[PackageVulnerability]:
    """Parse uv audit JSON or text output into a list of PackageVulnerability objects.

    Sample uv audit output format:
    certifi 2026.7.22 has 2 known vulnerabilities:

    - GHSA-6q48-6c7w-9gw2: certifi bundles emSign root CAs with compromised intermediate CA keys
      Severity: Critical (CVSS: 9.8)
      Fixed in: 2026.10.19
      Advisory information: https://github.com/certifi/python-certifi/security/advisories/GHSA-6q48-6c7w-9gw2
    """
    stripped = output.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
            if isinstance(payload, dict) and "vulnerabilities" in payload:
                return _parse_json_audit_output(payload)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to decode uv audit JSON output: {e}")
            return []

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
    """Parse uv.lock to find the locked version of package_name (normalizing PEP 503 names)."""
    if not uv_lock_path.is_file():
        return None

    content = uv_lock_path.read_text(encoding="utf-8")
    target_norm = normalize_package_name(package_name)
    try:
        lock_data = tomllib.loads(content)
        for pkg in lock_data.get("package", []):
            if (
                isinstance(pkg, dict)
                and normalize_package_name(str(pkg.get("name", ""))) == target_norm
                and pkg.get("version")
            ):
                return str(pkg["version"])
    except Exception:
        pass

    pattern = rf'\[\[package\]\]\s*\nname\s*=\s*"{re.escape(package_name)}"\s*\nversion\s*=\s*"([^"]+)"'
    match = re.search(pattern, content)
    if match:
        return match.group(1)
    return None


def sync_offline_requirements_manifest(repo_root: Path) -> tuple[bool, str]:
    """Export exact locked runtime + `ml` requirements from uv.lock to offline_bundle/requirements.txt."""
    reqs_file = repo_root / "offline_bundle" / "requirements.txt"
    if not reqs_file.is_file():
        return True, "No tracked offline_bundle/requirements.txt present."

    cmd = [
        "uv",
        "export",
        "--frozen",
        "--format",
        "requirements.txt",
        "--extra",
        "ml",
        "--no-dev",
        "--no-emit-project",
        "-o",
        str(reqs_file),
    ]
    logger.info(
        "Synchronizing offline_bundle/requirements.txt from upgraded uv.lock..."
    )
    try:
        res = subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            env=build_sanitized_subprocess_env(),
        )
    except Exception as e:
        logger.error(f"Failed to export offline_bundle/requirements.txt: {e}")
        return False, str(e)

    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        logger.error(f"uv export for offline_bundle/requirements.txt failed: {err}")
        return False, err
    return True, "Synchronized offline_bundle/requirements.txt with uv.lock."


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
    # Python PSF / CNRI
    "psf",
    "psf-2.0",
    "python software foundation license",
    "python software foundation",
    "python license",
    "python-2.0",
    "cnri-python",
    "cnri-python-gpl-compatible",
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
    # Boost Software License 1.0 (permissive, GPL-3.0 compatible; distinct from BUSL)
    "bsl-1.0",
    "boost software license",
    "boost software license 1.0",
    "boost software license - version 1.0 - august 17th, 2003",
    # Apache 2.0 with LLVM exception
    "apache-2.0 with llvm-exception",
    # HPND
    "hpnd",
}

# Informational classifier prefixes or metadata phrases to ignore during token evaluation
INFORMATIONAL_LICENSE_TOKENS = {
    "dfsg approved",
    "dependency licenses",
}


def _strip_outer_parens(expr: str) -> str:
    """Strip balanced outer parentheses from an SPDX license expression."""
    cur = expr.strip()
    while cur.startswith("(") and cur.endswith(")"):
        depth = 0
        balanced = True
        for idx, ch in enumerate(cur):
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
            if depth == 0 and idx < len(cur) - 1:
                balanced = False
                break
        if balanced and depth == 0:
            cur = cur[1:-1].strip()
        else:
            break
    return cur


def _split_top_level_license(expr: str, pattern: str) -> list[str]:
    """Split a license expression by regex pattern only at parenthesis depth 0."""
    spans: list[tuple[int, int]] = []
    depth = 0
    idx = 0
    while idx < len(expr):
        ch = expr[idx]
        if ch == "(":
            depth += 1
            idx += 1
            continue
        if ch == ")":
            depth = max(0, depth - 1)
            idx += 1
            continue
        if depth == 0:
            m = re.match(pattern, expr[idx:], flags=re.IGNORECASE)
            if m:
                spans.append((idx, idx + m.end()))
                idx += max(1, m.end())
                continue
        idx += 1

    if not spans:
        return [expr]

    parts: list[str] = []
    last = 0
    for start, end in spans:
        parts.append(expr[last:start])
        last = end
    parts.append(expr[last:])
    return parts


def _is_gpl2_only_token(cleaned_lower: str) -> bool:
    """Return True if token denotes standalone GPLv2 / GPL-2.0 without or-later."""
    return (
        bool(re.search(r"\bgpl(?:-|\s*|v)2(?:\.0)?(?:\s*only|-only)?\b", cleaned_lower))
        and "later" not in cleaned_lower
        and "+" not in cleaned_lower
    )


def _is_single_token_compatible(token: str) -> bool:
    """Check if an individual license token is compatible with GPL-3.0."""
    cleaned = token.strip().lower().strip("\"'")
    if not cleaned:
        return False
    if _is_gpl2_only_token(cleaned):
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
    if cleaned.startswith("boost software license"):
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

    raw = _strip_outer_parens(license_str)
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

    cleaned_lower = raw.lower().strip("\"'")

    # 1. Exact known single-license names that contain commas (e.g. "Apache License, Version 2.0")
    if cleaned_lower in GPL3_COMPATIBLE_LICENSES:
        return True, f"Approved license: {raw}"

    # 2. Disjunctive / dual licensing expressions at top-level depth (OR, dual ...)
    or_branches = _split_top_level_license(raw, r"\s+\bOR\b\s+")
    if len(or_branches) > 1:
        for b in or_branches:
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

    # 3. Conjunctive / delimiter separated expressions (AND, semicolon, comma)
    tokens = _split_top_level_license(raw, r"\s+\bAND\b\s+|;\s*|,\s*")
    if len(tokens) > 1:
        for tok in tokens:
            tok = tok.strip()
            if not tok:
                continue
            ok, _ = is_license_compatible(tok)
            if not ok:
                return (
                    False,
                    f"Incompatible license component '{tok}' in expression: '{raw}'",
                )
        return True, f"Approved multi-license expression: {raw}"

    # 4. Slash splitting for dual-license expressions like "MIT / Apache-2.0"
    if "/" in raw and cleaned_lower != "zlib/libpng":
        subtokens = [
            t.strip() for t in _split_top_level_license(raw, r"/") if t.strip()
        ]
        if len(subtokens) > 1:
            for st in subtokens:
                ok, _ = is_license_compatible(st)
                if ok:
                    return True, f"Approved under slash choice '{st}' from '{raw}'"
            return False, f"Incompatible license: '{raw}'"

    # 5. Explicit rejection for standalone GPL-2.0 / GPLv2 (without or-later)
    if _is_gpl2_only_token(cleaned_lower):
        return (
            False,
            f"Prohibited GPL-2.0-only license incompatible with GPL-3.0: '{license_str}'",
        )

    if _is_single_token_compatible(raw):
        return True, f"Approved license: {raw}"

    return False, f"Incompatible or unapproved license: '{raw}'"


_LOCKED_LICENSE_CACHE: dict[tuple[str, str], str] = {}
_HTTP_METADATA_SESSION: requests.Session | None = None


def _get_metadata_session() -> requests.Session:
    global _HTTP_METADATA_SESSION
    if _HTTP_METADATA_SESSION is None:
        _HTTP_METADATA_SESSION = requests.Session()
    return _HTTP_METADATA_SESSION


def _extract_license_from_core_metadata(msg: Any) -> str | None:
    """Extract normalized license string from Python wheel/package Core Metadata headers."""
    lic_expr = msg.get("License-Expression")
    if (
        isinstance(lic_expr, str)
        and lic_expr.strip()
        and lic_expr.strip().upper() not in ("UNKNOWN", "NONE")
    ):
        return lic_expr.strip()

    lic_header = msg.get("License")
    lic_clean = lic_header.strip() if isinstance(lic_header, str) else ""

    raw_classifiers = msg.get_all("Classifier") or []
    license_classifiers: list[str] = []
    for cls in raw_classifiers:
        if not isinstance(cls, str) or not cls.startswith("License ::"):
            continue
        leaf = cls.split("::")[-1].strip()
        if leaf and leaf.lower() not in ("osi approved", "dfsg approved"):
            if leaf not in license_classifiers:
                license_classifiers.append(leaf)

    if license_classifiers:
        is_disjunctive = bool(
            re.search(r"\b(?:dual|or)\b", lic_clean, flags=re.IGNORECASE)
        )
        sep = " OR " if (is_disjunctive and len(license_classifiers) > 1) else "; "
        return sep.join(license_classifiers)

    if lic_clean:
        first_line = lic_clean.splitlines()[0].strip()
        if first_line:
            return first_line
    return None


def _collect_lockfile_packages_for_license_audit(
    repo_root: Path,
) -> tuple[list[dict[str, Any]], set[str]] | None:
    """Return (non_build_locked_packages, build_only_package_names) from pyproject.toml and uv.lock."""
    uv_lock_path = repo_root / "uv.lock"
    if not uv_lock_path.is_file():
        return None
    try:
        lock_data = tomllib.loads(uv_lock_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Failed to parse uv.lock for license audit: {e}")
        return None

    packages = lock_data.get("package", [])
    if not isinstance(packages, list) or not packages:
        return None

    by_name: dict[str, list[dict[str, Any]]] = {}
    root_entry: dict[str, Any] | None = None
    root_name: str | None = None

    for pkg in packages:
        if not isinstance(pkg, dict) or not pkg.get("name"):
            continue
        norm_name = normalize_package_name(str(pkg["name"]))
        by_name.setdefault(norm_name, []).append(pkg)
        source = pkg.get("source")
        if isinstance(source, dict) and (
            source.get("virtual") == "." or source.get("editable") == "."
        ):
            root_entry = pkg
            root_name = norm_name

    if root_entry is not None and root_name is not None:
        roots: list[tuple[str, str | None]] = [(root_name, None)]
        opt_deps = root_entry.get("optional-dependencies")
        if isinstance(opt_deps, dict):
            for extra_name in opt_deps:
                roots.append((root_name, str(extra_name)))
        dev_groups = root_entry.get("dev-dependencies")
        if isinstance(dev_groups, dict):
            for grp_name, grp_deps in dev_groups.items():
                if grp_name == "build" or not isinstance(grp_deps, list):
                    continue
                for dep in grp_deps:
                    if isinstance(dep, dict) and dep.get("name"):
                        dn = normalize_package_name(str(dep["name"]))
                        roots.append((dn, None))
                        for ex in dep.get("extra") or []:
                            roots.append((dn, str(ex)))

        seen: set[tuple[str, str | None]] = set()
        non_build_names: set[str] = set()
        queue = list(roots)
        while queue:
            cur_name, cur_extra = queue.pop()
            if (cur_name, cur_extra) in seen:
                continue
            seen.add((cur_name, cur_extra))
            if cur_name != root_name:
                non_build_names.add(cur_name)
            for entry in by_name.get(cur_name, []):
                if cur_extra is None:
                    deps_list = entry.get("dependencies", [])
                else:
                    entry_opts = entry.get("optional-dependencies", {})
                    deps_list = (
                        entry_opts.get(cur_extra, [])
                        if isinstance(entry_opts, dict)
                        else []
                    )
                if not isinstance(deps_list, list):
                    continue
                for dep in deps_list:
                    if isinstance(dep, dict) and dep.get("name"):
                        dn = normalize_package_name(str(dep["name"]))
                        queue.append((dn, None))
                        for ex in dep.get("extra") or []:
                            queue.append((dn, str(ex)))

        all_third_party = {n for n in by_name if n != root_name}
        build_only = all_third_party - non_build_names
        audited = [
            pkg
            for pkg in packages
            if isinstance(pkg, dict)
            and normalize_package_name(str(pkg.get("name", ""))) in non_build_names
        ]
        return audited, build_only

    # Lockfiles without a virtual root entry: exclude packages declared solely in [dependency-groups].build
    build_only_from_pyproject: set[str] = set()
    pyproject_path = repo_root / "pyproject.toml"
    if pyproject_path.is_file():
        try:
            pdata = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
            non_build_declared: set[str] = set()
            proj = pdata.get("project", {})
            for req in proj.get("dependencies", []):
                m = re.match(r"^\s*([A-Za-z0-9._-]+)", str(req))
                if m:
                    non_build_declared.add(normalize_package_name(m.group(1)))
            for extra_reqs in (proj.get("optional-dependencies") or {}).values():
                for req in extra_reqs:
                    m = re.match(r"^\s*([A-Za-z0-9._-]+)", str(req))
                    if m:
                        non_build_declared.add(normalize_package_name(m.group(1)))
            dep_groups = pdata.get("dependency-groups") or {}
            build_declared: set[str] = set()
            for grp, reqs in dep_groups.items():
                if not isinstance(reqs, list):
                    continue
                for req in reqs:
                    if not isinstance(req, str):
                        continue
                    m = re.match(r"^\s*([A-Za-z0-9._-]+)", req)
                    if not m:
                        continue
                    nm = normalize_package_name(m.group(1))
                    if grp == "build":
                        build_declared.add(nm)
                    else:
                        non_build_declared.add(nm)
            direct_build_only = build_declared - non_build_declared
            if direct_build_only:
                queue_b = list(direct_build_only)
                while queue_b:
                    cur_b = queue_b.pop()
                    if (
                        cur_b in build_only_from_pyproject
                        or cur_b in non_build_declared
                    ):
                        continue
                    build_only_from_pyproject.add(cur_b)
                    for entry in by_name.get(cur_b, []):
                        for dep in entry.get("dependencies", []):
                            if isinstance(dep, dict) and dep.get("name"):
                                queue_b.append(normalize_package_name(str(dep["name"])))
        except Exception:
            pass

    audited_fallback = [
        pkg
        for pkg in packages
        if isinstance(pkg, dict)
        and pkg.get("name")
        and normalize_package_name(str(pkg["name"])) not in build_only_from_pyproject
    ]
    return audited_fallback, build_only_from_pyproject


def _build_installed_license_map() -> dict[tuple[str, str], str]:
    installed_map: dict[tuple[str, str], str] = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata.get("Name")
        ver = dist.metadata.get("Version")
        if not name or not ver:
            continue
        lic = _extract_license_from_core_metadata(dist.metadata)
        if lic:
            installed_map[(normalize_package_name(name), ver.strip())] = lic
    return installed_map


def _default_locked_license_resolver(
    installed_map: dict[tuple[str, str], str],
) -> Callable[[str, str, dict[str, Any]], str | None]:
    """Build the production license resolver backed by installed metadata, PEP 658 wheel metadata, and registry JSON."""

    def _resolve(name: str, version: str, pkg_entry: dict[str, Any]) -> str | None:
        key = (normalize_package_name(name), version.strip())
        if key in installed_map:
            return installed_map[key]
        if key in _LOCKED_LICENSE_CACHE:
            return _LOCKED_LICENSE_CACHE[key]

        session = _get_metadata_session()
        wheels = pkg_entry.get("wheels")
        if isinstance(wheels, list):
            for whl in wheels:
                if not isinstance(whl, dict):
                    continue
                whl_url = whl.get("url")
                if not isinstance(whl_url, str) or not whl_url.strip():
                    continue
                try:
                    resp = session.get(f"{whl_url.strip()}.metadata", timeout=5.0)
                    if resp.status_code == 200 and resp.text:
                        msg = email.message_from_string(resp.text)
                        lic = _extract_license_from_core_metadata(msg)
                        if lic:
                            _LOCKED_LICENSE_CACHE[key] = lic
                            return lic
                except Exception as e:
                    logger.debug(
                        f"PEP 658 metadata fetch failed for {name}=={version}: {e}"
                    )

        source = pkg_entry.get("source")
        registry = source.get("registry") if isinstance(source, dict) else None
        if isinstance(registry, str) and registry.rstrip("/").endswith("/simple"):
            origin = registry.rstrip("/")[: -len("/simple")]
            try:
                resp = session.get(f"{origin}/pypi/{name}/{version}/json", timeout=5.0)
                if resp.status_code == 200:
                    info = (resp.json() or {}).get("info") or {}
                    if isinstance(info, dict):
                        msg = email.message.Message()
                        if info.get("license_expression"):
                            msg["License-Expression"] = str(info["license_expression"])
                        if info.get("license"):
                            msg["License"] = str(info["license"])
                        for cls in info.get("classifiers") or []:
                            if isinstance(cls, str):
                                msg["Classifier"] = cls
                        lic = _extract_license_from_core_metadata(msg)
                        if lic:
                            _LOCKED_LICENSE_CACHE[key] = lic
                            return lic
            except Exception as e:
                logger.debug(
                    f"Registry JSON metadata fetch failed for {name}=={version}: {e}"
                )

        return None

    return _resolve


def run_license_audit(
    repo_root: Path,
    package_name: str | None = None,
    *,
    license_resolver: Callable[[str, str, dict[str, Any]], str | None] | None = None,
) -> tuple[bool, str]:
    """Validate licenses for all locked non-build dependencies in uv.lock against GPL-3.0 compatibility."""
    collected = _collect_lockfile_packages_for_license_audit(repo_root)
    if collected is None:
        msg = f"Missing or unreadable uv.lock in {repo_root}"
        logger.error(msg)
        return False, msg

    audited_packages, build_only_names = collected
    resolver = (
        license_resolver
        if license_resolver is not None
        else _default_locked_license_resolver(_build_installed_license_map())
    )

    if package_name:
        pkg_norm = normalize_package_name(package_name)
        if pkg_norm in build_only_names:
            logger.info(
                f"Ignoring standalone build tool '{package_name}' during runtime/ML license validation."
            )
        else:
            matched = [
                pkg
                for pkg in audited_packages
                if normalize_package_name(str(pkg.get("name", ""))) == pkg_norm
            ]
            if not matched:
                logger.error(
                    f"Package '{package_name}' missing license metadata (not found in uv.lock)."
                )
                return (
                    False,
                    f"Package '{package_name}' missing license metadata: not found in uv.lock",
                )
            for pkg in matched:
                raw_name = str(pkg.get("name", package_name))
                ver = str(pkg.get("version", ""))
                lic = resolver(raw_name, ver, pkg)
                ok, reason = is_license_compatible(lic)
                if not ok:
                    logger.error(
                        f"Package '{package_name}' ({ver}) has incompatible license: {reason}"
                    )
                    return (
                        False,
                        f"Package '{raw_name}' has incompatible license: {reason}",
                    )

    for pkg in audited_packages:
        raw_name = str(pkg.get("name", "Unknown"))
        ver = str(pkg.get("version", ""))
        lic = resolver(raw_name, ver, pkg)
        ok, reason = is_license_compatible(lic)
        if not ok:
            logger.error(
                f"Locked dependency '{raw_name}' ({ver}) has incompatible license: {reason}"
            )
            return False, f"Package '{raw_name}' has incompatible license: {reason}"

    logger.info(
        f"All {len(audited_packages)} locked non-build package entries verified compatible with GPL-3.0."
    )
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

    def close_resolved_security_issues(self, comment_body: str | None = None) -> bool:
        """Comment on and close open automated security audit alert issues while preserving triage labels."""
        resolution_comment = comment_body or (
            "✅ **Automated Dependency Audit Clean:** All unexempted dependency vulnerabilities "
            "have been resolved and verified against `uv.lock`. Closing this automated security alert."
        )
        try:
            issues_url = (
                f"{self.base_url}/issues?state=open&labels=security&per_page=100"
            )
            resp = requests.get(issues_url, headers=self.headers, timeout=30)
            resp.raise_for_status()
            open_issues = resp.json()
            if not isinstance(open_issues, list):
                return True

            all_ok = True
            for issue in open_issues:
                if not isinstance(issue, dict) or "pull_request" in issue:
                    continue
                issue_title = str(issue.get("title", ""))
                if not (
                    "Automated Dependency Audit Failure" in issue_title
                    or "Automated Dependency Audit Alert" in issue_title
                    or "Automated Vulnerability Audit Alert" in issue_title
                ):
                    continue

                issue_number = issue.get("number")
                if not issue_number:
                    continue

                existing_label_names = [
                    lbl.get("name", "") if isinstance(lbl, dict) else str(lbl)
                    for lbl in issue.get("labels", [])
                ]
                preserved_labels = [
                    name
                    for name in existing_label_names
                    if name
                    and not name.lower().startswith("severity:")
                    and name.lower() != "priority:urgent"
                ]
                if not any(name.lower() == "security" for name in preserved_labels):
                    preserved_labels.append("security")

                comment_url = f"{self.base_url}/issues/{issue_number}/comments"
                comment_resp = requests.post(
                    comment_url,
                    headers=self.headers,
                    json={"body": resolution_comment},
                    timeout=30,
                )
                if comment_resp.status_code not in (200, 201):
                    logger.warning(
                        f"Failed to post resolution comment on security issue #{issue_number}: "
                        f"{comment_resp.status_code}"
                    )
                    all_ok = False
                    continue

                close_url = f"{self.base_url}/issues/{issue_number}"
                patch_resp = requests.patch(
                    close_url,
                    headers=self.headers,
                    json={"state": "closed", "labels": preserved_labels},
                    timeout=30,
                )
                if patch_resp.status_code not in (200, 201):
                    logger.error(
                        f"Failed to close resolved security issue #{issue_number}: "
                        f"{patch_resp.status_code} {sanitize_sensitive_info(patch_resp.text, token=self.token)}"
                    )
                    all_ok = False
                else:
                    logger.info(f"Closed resolved security alert issue #{issue_number}")
            return all_ok
        except Exception as e:
            logger.error(
                f"Failed to close resolved security issues: "
                f"{sanitize_sensitive_info(str(e), token=self.token)}"
            )
            return False


def _revert_working_tree_lockfiles(
    repo_root: Path,
    original_uv_lock: str | None,
    original_pyproject: str | None,
    original_offline_reqs: str | None = None,
) -> None:
    """Revert uv.lock, pyproject.toml, and offline_bundle/requirements.txt cleanly."""
    env = build_sanitized_subprocess_env()
    subprocess.run(["git", "checkout", "uv.lock"], cwd=repo_root, check=False, env=env)
    subprocess.run(
        ["git", "checkout", "pyproject.toml"], cwd=repo_root, check=False, env=env
    )
    offline_reqs = repo_root / "offline_bundle" / "requirements.txt"
    if original_offline_reqs is not None:
        subprocess.run(
            ["git", "checkout", "offline_bundle/requirements.txt"],
            cwd=repo_root,
            check=False,
            env=env,
        )
    uv_lock = repo_root / "uv.lock"
    pyproject_toml = repo_root / "pyproject.toml"
    if original_uv_lock is not None and uv_lock.is_file():
        if uv_lock.read_text(encoding="utf-8") != original_uv_lock:
            uv_lock.write_text(original_uv_lock, encoding="utf-8")
    if original_pyproject is not None and pyproject_toml.is_file():
        if pyproject_toml.read_text(encoding="utf-8") != original_pyproject:
            pyproject_toml.write_text(original_pyproject, encoding="utf-8")
    if original_offline_reqs is not None and offline_reqs.is_file():
        if offline_reqs.read_text(encoding="utf-8") != original_offline_reqs:
            offline_reqs.write_text(original_offline_reqs, encoding="utf-8")


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
    *,
    license_auditor: Callable[[Path, str | None], tuple[bool, str]] | None = None,
) -> bool:
    """Audit dependencies, validate upgrades, and either stage artifacts or publish via Git Data API."""
    security_md = repo_root / "SECURITY.md"
    uv_lock = repo_root / "uv.lock"
    pyproject_toml = repo_root / "pyproject.toml"
    offline_reqs = repo_root / "offline_bundle" / "requirements.txt"

    original_uv_lock = (
        uv_lock.read_text(encoding="utf-8") if uv_lock.is_file() else None
    )
    original_pyproject = (
        pyproject_toml.read_text(encoding="utf-8") if pyproject_toml.is_file() else None
    )
    original_offline_reqs = (
        offline_reqs.read_text(encoding="utf-8") if offline_reqs.is_file() else None
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

    gh_client: GitHubRemediationClient | None = None
    if repo_full:
        parts = repo_full.split("/")
        if len(parts) == 2 and (token is not None or stage_dir is not None):
            gh_client = GitHubRemediationClient(token, parts[0], parts[1])

    if exit_code == 0:
        logger.info("No unexempted vulnerabilities found by uv audit.")
        if stage_dir is not None:
            _write_manifest(stage_dir, manifest)
            return True
        if not dry_run and gh_client is not None and token and token.strip():
            gh_client.close_resolved_security_issues()
        return True

    logger.warning("Unexempted vulnerabilities detected by uv audit. Parsing report...")
    vuln_packages = parse_audit_output(audit_output)

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
                "- **uv-audit** [Severity: MEDIUM]: `uv audit` exited non-zero without parseable package entries."
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
        pkg_norm = normalize_package_name(package_name)
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
                repo_root, original_uv_lock, original_pyproject, original_offline_reqs
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
                repo_root, original_uv_lock, original_pyproject, original_offline_reqs
            )
            continue

        logger.info(
            f"Successfully updated {package_name} in uv.lock: {old_version} -> {new_version}"
        )

        # Verify whether audit passes for this package after lock upgrade
        post_exit, post_output = run_uv_audit(exemptions, repo_root)
        post_vulnerabilities = parse_audit_output(post_output) if post_exit != 0 else []
        still_vulnerable = any(
            normalize_package_name(p.package_name) == pkg_norm
            for p in post_vulnerabilities
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
                repo_root, original_uv_lock, original_pyproject, original_offline_reqs
            )
            continue

        # Export offline_bundle/requirements.txt alongside uv.lock when present
        if offline_reqs.is_file():
            sync_ok, sync_msg = sync_offline_requirements_manifest(repo_root)
            if not sync_ok:
                sanitized_sync_msg = sanitize_sensitive_info(sync_msg, token=token)
                report_line = (
                    f"- **{package_name}** ({old_version} -> {new_version}) [Severity: {pkg_sev}]: "
                    f"Offline manifest synchronization failed: {sanitized_sync_msg}"
                )
                base_entry["failure_stage"] = "offline_manifest"
                base_entry["failure_reason"] = (
                    f"Offline manifest synchronization failed: {sanitized_sync_msg}"
                )
                base_entry["report_markdown"] = report_line
                manifest["packages"].append(base_entry)
                failed_entries.append(base_entry)
                _revert_working_tree_lockfiles(
                    repo_root,
                    original_uv_lock,
                    original_pyproject,
                    original_offline_reqs,
                )
                continue

        # Execute smoke checks + targeted test suite with baseline comparison
        test_exit, test_output = run_local_tests(
            repo_root,
            package_name=package_name,
            baseline_uv_lock=original_uv_lock,
            baseline_pyproject=original_pyproject,
        )
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
                repo_root, original_uv_lock, original_pyproject, original_offline_reqs
            )
            continue

        # Verify license compliance for upgraded dependency and locked graph against GPL-3.0
        if license_auditor is not None:
            license_ok, license_msg = license_auditor(repo_root, package_name)
        else:
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
                repo_root, original_uv_lock, original_pyproject, original_offline_reqs
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
        if offline_reqs.is_file():
            files_to_commit["offline_bundle/requirements.txt"] = offline_reqs.read_text(
                encoding="utf-8"
            )

        _revert_working_tree_lockfiles(
            repo_root, original_uv_lock, original_pyproject, original_offline_reqs
        )

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
        if (
            manifest is not None
            and bool(manifest.get("audit_clean"))
            and normalized_job_status in ("success", "")
        ):
            clean_token: str | None = None
            try:
                clean_token = resolve_publish_token(
                    owner=owner,
                    repo=repo,
                    app_id=app_id,
                    app_private_key=app_private_key,
                    fallback_token=token,
                    require_app_token=False,
                )
            except Exception as e:
                logger.debug(
                    f"Skipping stale alert auto-close without publish credentials: "
                    f"{sanitize_sensitive_info(str(e), token=token)}"
                )
            if clean_token:
                clean_client = GitHubRemediationClient(clean_token, owner, repo)
                if not clean_client.close_resolved_security_issues():
                    logger.error(
                        "Failed to close resolved security alert issues after clean audit."
                    )
                    return False
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
