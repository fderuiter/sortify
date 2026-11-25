"""Git-scoped secret scanner delegating to `gitleaks` when installed or offline Python rules otherwise."""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent

# Credential file extensions that must never be assigned to Sortify *_key or API key identifiers.
_CREDENTIAL_FILENAME_ASSIGNMENT_RE = re.compile(
    r"""(?i)(?:^|(?<=[^a-zA-Z0-9_]))(?P<ident>node_key|file_key|dest_key|item_key|scoped_key|row_key|api[_-]?key|secret[_-]?key)['"]?\s*[:=]\s*['"](?P<secret>[^'"\r\n]*\.(?:key|pem|env|pfx|p12))['"]"""
)

# Built-in offline Python secret rules aligned with Gitleaks rule IDs.
_OFFLINE_SECRET_RULES: tuple[tuple[str, re.Pattern[str], float], ...] = (
    (
        "stripe-access-token",
        re.compile(
            r"""(?:^|(?<=[^a-zA-Z0-9]))(?P<secret>(?:sk|rk)_(?:live|test)_[0-9a-zA-Z]{10,})(?:$|(?=[^a-zA-Z0-9]))"""
        ),
        0.0,
    ),
    (
        "github-pat",
        re.compile(
            r"""(?:^|(?<=[^a-zA-Z0-9]))(?P<secret>gh[pousr]_[0-9a-zA-Z]{20,}|github_pat_[0-9a-zA-Z_]{22,})(?:$|(?=[^a-zA-Z0-9]))"""
        ),
        0.0,
    ),
    (
        "aws-access-token",
        re.compile(
            r"""(?:^|(?<=[^a-zA-Z0-9]))(?P<secret>(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16})(?:$|(?=[^a-zA-Z0-9]))"""
        ),
        0.0,
    ),
    (
        "slack-bot-token",
        re.compile(
            r"""(?:^|(?<=[^a-zA-Z0-9]))(?P<secret>xox[baprs]-[0-9A-Za-z-]{10,})(?:$|(?=[^a-zA-Z0-9]))"""
        ),
        0.0,
    ),
    (
        "gcp-api-key",
        re.compile(
            r"""(?:^|(?<=[^a-zA-Z0-9]))(?P<secret>AIza[0-9A-Za-z_-]{35})(?:$|(?=[^a-zA-Z0-9]))"""
        ),
        0.0,
    ),
    (
        "mailgun-private-api-token",
        re.compile(
            r"""(?:^|(?<=[^a-zA-Z0-9]))(?P<secret>key-[0-9a-zA-Z]{32})(?:$|(?=[^a-zA-Z0-9]))"""
        ),
        0.0,
    ),
    (
        "private-key",
        re.compile(
            r"""(?P<secret>-----BEGIN\s+(?:[A-Z0-9_-]+\s+)*PRIVATE\s+KEY-----)"""
        ),
        0.0,
    ),
    (
        "jwt",
        re.compile(
            r"""(?:^|(?<=[^a-zA-Z0-9]))(?P<secret>eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{16,})(?:$|(?=[^a-zA-Z0-9]))"""
        ),
        0.0,
    ),
    (
        "generic-api-key",
        re.compile(
            r"""(?i)(?:^|(?<=[^a-zA-Z0-9_]))(?P<key>(?:[a-z0-9]+[_-])*(?:api[_-]?key|secret[_-]?key|access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key|passwd|password|key|secret|token)(?:[_-][a-z0-9]+)*)['"]?\s*[:=]\s*['"](?P<secret>[A-Za-z0-9_./:\\+=-]{10,120})['"]"""
        ),
        3.5,
    ),
)

_DEFAULT_GITLEAKS_GLOBAL_PATHS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|/)\.?gitleaks\.toml$"),
)

_GENERIC_API_KEY_STOPWORDS: tuple[str, ...] = (
    "secret",
    "token",
    "example",
    "sample",
    "password",
    "passwd",
    "placeholder",
    "dummy",
    "fake",
    "changeme",
    "localhost",
    "report",
)

_PKCS12_PATH_RE = re.compile(r"\.(?:p12|pfx)$", re.IGNORECASE)


@dataclass(frozen=True)
class _RuleAllowlist:
    paths: tuple[re.Pattern[str], ...]
    regexes: tuple[re.Pattern[str], ...]
    regex_target: str
    has_commits: bool


@dataclass(frozen=True)
class _GitleaksPolicy:
    global_paths: tuple[re.Pattern[str], ...]
    rule_allowlists: dict[str, tuple[_RuleAllowlist, ...]]


@dataclass(frozen=True)
class SecretFinding:
    """Single redacted secret finding reported by `run_secret_scan`."""

    path: str
    line: int
    rule_id: str
    redacted_snippet: str


def _shannon_entropy(text: str) -> float:
    if not text:
        return 0.0
    length = len(text)
    counts: dict[str, int] = {}
    for ch in text:
        counts[ch] = counts.get(ch, 0) + 1
    entropy = 0.0
    for count in counts.values():
        prob = count / length
        entropy -= prob * math.log2(prob)
    return entropy


def _redact_secret(secret: str) -> str:
    if len(secret) <= 8:
        return "REDACTED"
    return f"{secret[:3]}...REDACTED"


def _load_gitleaks_policy(config_path: Path | None) -> _GitleaksPolicy:
    if config_path is None or not config_path.is_file():
        return _GitleaksPolicy(
            global_paths=_DEFAULT_GITLEAKS_GLOBAL_PATHS,
            rule_allowlists={},
        )

    raw: dict[str, Any] = tomllib.loads(config_path.read_text(encoding="utf-8"))
    extend_cfg = raw.get("extend")
    use_default = not isinstance(extend_cfg, dict) or bool(
        extend_cfg.get("useDefault", False)
    )
    global_paths: list[re.Pattern[str]] = (
        list(_DEFAULT_GITLEAKS_GLOBAL_PATHS) if use_default else []
    )
    global_allow = raw.get("allowlist")
    if isinstance(global_allow, dict):
        for pat in global_allow.get("paths", []):
            if isinstance(pat, str) and pat:
                global_paths.append(re.compile(str(pat)))

    by_rule: dict[str, list[_RuleAllowlist]] = {}
    rules = raw.get("rules", [])
    if isinstance(rules, list):
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            rule_id = rule.get("id")
            if not isinstance(rule_id, str) or not rule_id:
                continue
            allowlists = rule.get("allowlists", [])
            if not isinstance(allowlists, list):
                continue
            parsed_entries: list[_RuleAllowlist] = []
            for entry in allowlists:
                if not isinstance(entry, dict):
                    continue
                paths_compiled: list[re.Pattern[str]] = [
                    re.compile(str(p))
                    for p in entry.get("paths", [])
                    if isinstance(p, str) and p
                ]
                regexes_compiled: list[re.Pattern[str]] = [
                    re.compile(str(r))
                    for r in entry.get("regexes", [])
                    if isinstance(r, str) and r
                ]
                target = str(entry.get("regexTarget", "secret")).strip().lower()
                commits = entry.get("commits")
                has_commits = isinstance(commits, list) and len(commits) > 0
                parsed_entries.append(
                    _RuleAllowlist(
                        paths=tuple(paths_compiled),
                        regexes=tuple(regexes_compiled),
                        regex_target=target or "secret",
                        has_commits=has_commits,
                    )
                )
            if parsed_entries:
                by_rule.setdefault(rule_id, []).extend(parsed_entries)

    return _GitleaksPolicy(
        global_paths=tuple(global_paths),
        rule_allowlists={k: tuple(v) for k, v in by_rule.items()},
    )


def _is_globally_ignored(rel_path: str, policy: _GitleaksPolicy) -> bool:
    norm = rel_path.replace("\\", "/").lstrip("./")
    return any(p.search(norm) for p in policy.global_paths)


def _is_rule_allowlisted(
    rule_id: str,
    rel_path: str,
    *,
    secret: str,
    match_text: str,
    line_text: str,
    policy: _GitleaksPolicy,
) -> bool:
    norm = rel_path.replace("\\", "/").lstrip("./")
    for allow in policy.rule_allowlists.get(rule_id, ()):
        if allow.has_commits:
            continue
        if allow.paths and not any(p.search(norm) for p in allow.paths):
            continue
        if allow.regexes:
            if allow.regex_target == "match":
                candidate = match_text
            elif allow.regex_target == "line":
                candidate = line_text
            else:
                candidate = secret
            if not any(r.search(candidate) for r in allow.regexes):
                continue
        if allow.paths or allow.regexes:
            return True
    return False


def _git_null_paths(repo_root: Path, *git_args: str) -> list[str]:
    res = subprocess.run(
        ["git", *git_args],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if res.returncode != 0:
        return []
    raw_items = res.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    return [item for item in raw_items if item]


def enumerate_staged_paths(repo_root: Path) -> list[str]:
    """Return sorted relative paths staged for addition, copy, modification, or rename."""
    paths = _git_null_paths(
        repo_root,
        "diff",
        "--cached",
        "--name-only",
        "--diff-filter=ACMR",
        "-z",
    )
    return sorted(set(paths))


def enumerate_working_tree_paths(repo_root: Path) -> list[str]:
    """Return sorted Git-tracked and non-ignored working-tree paths plus force-staged paths."""
    tracked_and_unignored = _git_null_paths(
        repo_root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
    )
    staged = enumerate_staged_paths(repo_root)
    return sorted(set(tracked_and_unignored) | set(staged))


def _read_staged_blob(repo_root: Path, rel_path: str) -> bytes | None:
    res = subprocess.run(
        ["git", "show", f":{rel_path}"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    return res.stdout


def _has_git_head(repo_root: Path) -> bool:
    res = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=repo_root,
        capture_output=True,
        check=False,
    )
    return res.returncode == 0


def _is_likely_placeholder(secret: str) -> bool:
    lowered = secret.strip().lower()
    if not lowered:
        return True
    if lowered in {
        "none",
        "null",
        "true",
        "false",
        "placeholder",
        "changeme",
        "redacted",
        "[redacted]",
        "[redacted_secret]",
        "your_api_key_here",
    }:
        return True
    if lowered.startswith(("${", "{{", "<", "$")) or "redacted" in lowered:
        return True
    if any(sw in lowered for sw in _GENERIC_API_KEY_STOPWORDS):
        return True
    # Non-digit identifier/path strings match Gitleaks built-in ^[a-zA-Z_.-]+$ allowlist.
    if re.fullmatch(r"[a-zA-Z_./-]+", secret):
        return True
    return False


def _scan_credential_filename_assignments(
    rel_path: str,
    content: bytes,
    policy: _GitleaksPolicy,
) -> list[SecretFinding]:
    if _is_globally_ignored(rel_path, policy) or b"\x00" in content[:8192]:
        return []
    text = content.decode("utf-8", errors="replace")
    findings: list[SecretFinding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for match in _CREDENTIAL_FILENAME_ASSIGNMENT_RE.finditer(line):
            ident = match.group("ident")
            secret = match.group("secret")
            findings.append(
                SecretFinding(
                    path=rel_path,
                    line=line_no,
                    rule_id="credential-file-key-assignment",
                    redacted_snippet=f"{ident}=<REDACTED:{_redact_secret(secret)}>",
                )
            )
    return findings


def _scan_blob_offline(
    rel_path: str,
    content: bytes,
    policy: _GitleaksPolicy,
) -> list[SecretFinding]:
    if _is_globally_ignored(rel_path, policy):
        return []

    findings: list[SecretFinding] = []
    if _PKCS12_PATH_RE.search(rel_path):
        if not _is_rule_allowlisted(
            "pkcs12-file",
            rel_path,
            secret=rel_path,
            match_text=rel_path,
            line_text=rel_path,
            policy=policy,
        ):
            findings.append(
                SecretFinding(
                    path=rel_path,
                    line=1,
                    rule_id="pkcs12-file",
                    redacted_snippet="PKCS#12 certificate archive",
                )
            )

    if b"\x00" in content[:8192]:
        return findings

    findings.extend(_scan_credential_filename_assignments(rel_path, content, policy))
    text = content.decode("utf-8", errors="replace")

    for line_no, line in enumerate(text.splitlines(), start=1):
        if not line or len(line) > 32768:
            continue
        for rule_id, pattern, min_entropy in _OFFLINE_SECRET_RULES:
            for match in pattern.finditer(line):
                secret = match.group("secret")
                match_text = match.group(0)
                if rule_id == "aws-access-token" and secret.endswith("EXAMPLE"):
                    continue
                if min_entropy > 0.0:
                    if _is_likely_placeholder(secret):
                        continue
                    if _shannon_entropy(secret) < min_entropy:
                        continue
                if _is_rule_allowlisted(
                    rule_id,
                    rel_path,
                    secret=secret,
                    match_text=match_text,
                    line_text=line,
                    policy=policy,
                ):
                    continue
                findings.append(
                    SecretFinding(
                        path=rel_path,
                        line=line_no,
                        rule_id=rule_id,
                        redacted_snippet=_redact_secret(secret),
                    )
                )
    return findings


def _materialize_tree(
    staging_root: Path,
    repo_root: Path,
    rel_paths: list[str],
    policy: _GitleaksPolicy,
) -> int:
    count = 0
    for rel_path in rel_paths:
        if _is_globally_ignored(rel_path, policy):
            continue
        src = repo_root / rel_path
        if not src.exists() or src.is_dir() or src.is_symlink():
            continue
        dst = staging_root / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dst)
        except OSError:
            shutil.copy2(src, dst)
        count += 1
    return count


def _materialize_blobs(
    staging_root: Path,
    blobs: dict[str, bytes],
    policy: _GitleaksPolicy,
) -> int:
    count = 0
    for rel_path, payload in blobs.items():
        if _is_globally_ignored(rel_path, policy):
            continue
        dst = staging_root / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(payload)
        count += 1
    return count


def _run_gitleaks_dir(
    gitleaks_bin: str,
    target_dir: Path,
    config_path: Path | None,
) -> int:
    cmd = [gitleaks_bin, "dir", ".", "--redact", "--verbose", "--no-banner"]
    if config_path is not None and config_path.is_file():
        cmd.extend(["--config", str(config_path.resolve())])
    res = subprocess.run(cmd, cwd=target_dir, check=False)
    return res.returncode


def _print_findings(findings: list[SecretFinding]) -> None:
    seen: set[tuple[str, int, str]] = set()
    for item in findings:
        key = (item.path, item.line, item.rule_id)
        if key in seen:
            continue
        seen.add(key)
        print(
            f"{item.path}:{item.line}: [{item.rule_id}] potential secret detected ({item.redacted_snippet})",
            file=sys.stderr,
        )


def run_secret_scan(
    *,
    mode: str = "working-tree",
    repo_root: Path | None = None,
    config_path: Path | None = None,
    filenames: list[str] | None = None,
    force_offline: bool = False,
) -> int:
    """Scan staged changes or non-ignored working-tree files for hardcoded secrets."""
    root = (repo_root or REPO_ROOT).resolve()
    resolved_cfg: Path | None
    if config_path is None:
        default_cfg = root / ".gitleaks.toml"
        resolved_cfg = default_cfg if default_cfg.is_file() else None
    elif config_path.is_absolute():
        resolved_cfg = config_path
    else:
        resolved_cfg = (root / config_path).resolve()

    policy = _load_gitleaks_policy(resolved_cfg)
    gitleaks_bin = None if force_offline else shutil.which("gitleaks")

    if filenames:
        rel_targets: list[str] = []
        for raw in filenames:
            candidate = Path(raw)
            abs_p = (
                candidate.resolve()
                if candidate.is_absolute()
                else (root / candidate).resolve()
            )
            try:
                rel_targets.append(abs_p.relative_to(root).as_posix())
            except ValueError:
                rel_targets.append(raw.replace("\\", "/"))
        rel_targets = sorted(set(rel_targets))

        extra_findings: list[SecretFinding] = []
        for rel in rel_targets:
            disk_file = root / rel
            if disk_file.is_file():
                extra_findings.extend(
                    _scan_credential_filename_assignments(
                        rel, disk_file.read_bytes(), policy
                    )
                )

        if gitleaks_bin:
            with tempfile.TemporaryDirectory(prefix="sortify_secret_scan_") as tmp:
                tmp_dir = Path(tmp)
                staged_count = _materialize_tree(tmp_dir, root, rel_targets, policy)
                rc = (
                    _run_gitleaks_dir(gitleaks_bin, tmp_dir, resolved_cfg)
                    if staged_count > 0
                    else 0
                )
            if extra_findings:
                _print_findings(extra_findings)
                return 1
            return 1 if rc != 0 else 0

        offline_findings: list[SecretFinding] = []
        for rel in rel_targets:
            disk_file = root / rel
            if disk_file.is_file():
                offline_findings.extend(
                    _scan_blob_offline(rel, disk_file.read_bytes(), policy)
                )
        if offline_findings:
            _print_findings(offline_findings)
            return 1
        return 0

    if mode == "staged":
        staged_paths = enumerate_staged_paths(root)
        if not staged_paths:
            return 0

        staged_blobs: dict[str, bytes] = {}
        cred_findings: list[SecretFinding] = []
        for rel in staged_paths:
            blob = _read_staged_blob(root, rel)
            if blob is not None:
                staged_blobs[rel] = blob
                cred_findings.extend(
                    _scan_credential_filename_assignments(rel, blob, policy)
                )

        if gitleaks_bin:
            if _has_git_head(root):
                cmd = [
                    gitleaks_bin,
                    "git",
                    "--staged",
                    "--redact",
                    "--verbose",
                    "--no-banner",
                ]
                if resolved_cfg is not None and resolved_cfg.is_file():
                    cmd.extend(["--config", str(resolved_cfg)])
                rc = subprocess.run(cmd, cwd=root, check=False).returncode
            else:
                with tempfile.TemporaryDirectory(prefix="sortify_staged_scan_") as tmp:
                    tmp_dir = Path(tmp)
                    count = _materialize_blobs(tmp_dir, staged_blobs, policy)
                    rc = (
                        _run_gitleaks_dir(gitleaks_bin, tmp_dir, resolved_cfg)
                        if count > 0
                        else 0
                    )
            if cred_findings:
                _print_findings(cred_findings)
                return 1
            return 1 if rc != 0 else 0

        findings: list[SecretFinding] = []
        for rel, blob in staged_blobs.items():
            findings.extend(_scan_blob_offline(rel, blob, policy))
        if findings:
            _print_findings(findings)
            return 1
        return 0

    # working-tree mode (`gitleaks-dir`)
    wt_paths = enumerate_working_tree_paths(root)
    staged_paths = enumerate_staged_paths(root)
    distinct_staged_blobs: dict[str, bytes] = {}
    cred_wt_findings: list[SecretFinding] = []

    for rel in wt_paths:
        disk_path = root / rel
        if disk_path.is_file() and not disk_path.is_symlink():
            try:
                disk_bytes = disk_path.read_bytes()
            except OSError:
                continue
            cred_wt_findings.extend(
                _scan_credential_filename_assignments(rel, disk_bytes, policy)
            )

    for rel in staged_paths:
        blob = _read_staged_blob(root, rel)
        if blob is None:
            continue
        disk_path = root / rel
        disk_bytes_opt: bytes | None = None
        if disk_path.is_file() and not disk_path.is_symlink():
            try:
                disk_bytes_opt = disk_path.read_bytes()
            except OSError:
                disk_bytes_opt = None
        if disk_bytes_opt != blob:
            distinct_staged_blobs[rel] = blob
            cred_wt_findings.extend(
                _scan_credential_filename_assignments(rel, blob, policy)
            )

    if gitleaks_bin:
        rc_wt = 0
        rc_staged = 0
        with tempfile.TemporaryDirectory(prefix="sortify_wt_scan_") as tmp:
            base_tmp = Path(tmp)
            wt_stage = base_tmp / "worktree"
            wt_stage.mkdir(parents=True, exist_ok=True)
            wt_count = _materialize_tree(wt_stage, root, wt_paths, policy)
            if wt_count > 0:
                rc_wt = _run_gitleaks_dir(gitleaks_bin, wt_stage, resolved_cfg)

            if distinct_staged_blobs:
                idx_stage = base_tmp / "index"
                idx_stage.mkdir(parents=True, exist_ok=True)
                idx_count = _materialize_blobs(idx_stage, distinct_staged_blobs, policy)
                if idx_count > 0:
                    rc_staged = _run_gitleaks_dir(gitleaks_bin, idx_stage, resolved_cfg)

        if cred_wt_findings:
            _print_findings(cred_wt_findings)
            return 1
        return 1 if (rc_wt != 0 or rc_staged != 0) else 0

    offline_wt_findings: list[SecretFinding] = []
    for rel in wt_paths:
        disk_path = root / rel
        if disk_path.is_file() and not disk_path.is_symlink():
            try:
                disk_bytes = disk_path.read_bytes()
            except OSError:
                continue
            offline_wt_findings.extend(_scan_blob_offline(rel, disk_bytes, policy))

    for rel, blob in distinct_staged_blobs.items():
        offline_wt_findings.extend(_scan_blob_offline(rel, blob, policy))

    if offline_wt_findings:
        _print_findings(offline_wt_findings)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for staged and working-tree secret scanning."""
    parser = argparse.ArgumentParser(
        description=(
            "Scan Git-staged diffs or non-ignored working-tree files for secrets, "
            "delegating to gitleaks when available on PATH and running offline "
            "Python credential and entropy rules otherwise."
        )
    )
    parser.add_argument(
        "--mode",
        choices=("staged", "working-tree"),
        default="working-tree",
        help="Scan mode: 'staged' (index changes) or 'working-tree' (non-ignored files + index).",
    )
    parser.add_argument(
        "--staged",
        action="store_const",
        const="staged",
        dest="mode",
        help="Shortcut for --mode staged.",
    )
    parser.add_argument(
        "--working-tree",
        "--dir",
        action="store_const",
        const="working-tree",
        dest="mode",
        help="Shortcut for --mode working-tree.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to .gitleaks.toml configuration (defaults to <repo-root>/.gitleaks.toml).",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Optional Git repository root directory.",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Force offline Python secret scanner even if gitleaks is installed on PATH.",
    )
    parser.add_argument(
        "filenames",
        nargs="*",
        help="Optional explicit files to scan.",
    )
    args = parser.parse_args(argv)
    return run_secret_scan(
        mode=str(args.mode),
        repo_root=args.repo_root,
        config_path=args.config,
        filenames=list(args.filenames),
        force_offline=bool(args.offline),
    )


if __name__ == "__main__":
    sys.exit(main())
