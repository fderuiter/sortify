"""Contract tests for Stage 1 catalog auditing, MkDocs validator gating, and TUI snapshot deduplication."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from tests.test_ci_workflow import _find_git_bash, _load_ci_workflow, _run_git


def test_ci_static_checks_catalog_gate_and_deduplicated_snapshots() -> None:
    """Verify static-checks runs full catalog audit once and build-and-test runs TUI snapshots via Fast Smoke Tests."""
    workflow = _load_ci_workflow()
    static_steps = workflow["jobs"]["static-checks"]["steps"]
    catalog_step = next(
        (
            s
            for s in static_steps
            if "app.ui.catalog" in s.get("run", "")
            and "--audit-only" in s.get("run", "")
        ),
        None,
    )
    assert catalog_step is not None, (
        "Missing 'app.ui.catalog --audit-only' step in static-checks"
    )
    assert "if" not in catalog_step
    assert (catalog_step.get("env") or {}).get(
        "PYTHON_KEYRING_BACKEND"
    ) == "keyring.backends.fail.Keyring"

    build_steps = workflow["jobs"]["build-and-test"]["steps"]
    assert not any(
        "test_visual_snapshots.py" in s.get("run", "") for s in build_steps
    ), "build-and-test must not run test_visual_snapshots.py in a separate step"

    fast_step = next(
        (s for s in build_steps if s.get("name") == "Fast Smoke Tests"), None
    )
    assert fast_step is not None
    assert 'pytest -m "not slow and not benchmark"' in fast_step.get("run", "")

    upload_mismatch_step = next(
        (
            s
            for s in build_steps
            if s.get("name") == "Upload Snapshot Mismatch Artifacts"
        ),
        None,
    )
    assert upload_mismatch_step is not None, (
        "Missing 'Upload Snapshot Mismatch Artifacts' step in build-and-test"
    )
    assert upload_mismatch_step.get("if") == "failure()"
    assert build_steps.index(fast_step) < build_steps.index(upload_mismatch_step), (
        "'Upload Snapshot Mismatch Artifacts' must run after 'Fast Smoke Tests'"
    )


def test_static_checks_doc_build_gating_includes_mkdocs_site_validator(
    tmp_path: Path,
) -> None:
    """Verify PRs touching scripts/mkdocs_site_validator.py trigger --build in static-checks."""
    workflow = _load_ci_workflow()
    compile_step = next(
        s
        for s in workflow["jobs"]["static-checks"]["steps"]
        if s.get("name") == "Validate Documentation and Integrity"
    )
    script = compile_step["run"]
    bash_exe = _find_git_bash()

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _run_git(repo_dir, "init", "--quiet", "-b", "main")
    _run_git(repo_dir, "config", "user.name", "Test User")
    _run_git(repo_dir, "config", "user.email", "test@example.com")
    (repo_dir / "scripts").mkdir()
    (repo_dir / "scripts" / "mkdocs_site_validator.py").write_text(
        "x = 1\n", encoding="utf-8"
    )
    _run_git(repo_dir, "add", ".")
    _run_git(repo_dir, "commit", "--quiet", "-m", "initial")
    base_sha = _run_git(repo_dir, "rev-parse", "HEAD")

    _run_git(repo_dir, "checkout", "--quiet", "-b", "feat/validator", base_sha)
    (repo_dir / "scripts" / "mkdocs_site_validator.py").write_text(
        "x = 2\n", encoding="utf-8"
    )
    _run_git(repo_dir, "add", "scripts/mkdocs_site_validator.py")
    _run_git(repo_dir, "commit", "--quiet", "-m", "update validator")
    head_sha = _run_git(repo_dir, "rev-parse", "HEAD")

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    args_log = tmp_path / "uv_args.txt"
    uv_stub = stub_bin / "uv"
    uv_stub.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" > "{args_log.as_posix()}"\n',
        encoding="utf-8",
    )
    uv_stub.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{stub_bin}{os.pathsep}{env.get('PATH', '')}",
            "EVENT_NAME": "pull_request",
            "GITHUB_REF_FULL": "refs/pull/99/merge",
            "BASE_SHA": base_sha,
            "HEAD_SHA": head_sha,
        }
    )
    subprocess.run([bash_exe, "-e", "-c", script], cwd=repo_dir, env=env, check=True)
    assert (
        args_log.read_text(encoding="utf-8")
        .strip()
        .endswith("--check --local-only --build")
    )
