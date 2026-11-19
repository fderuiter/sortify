"""Tests for CI workflow configuration, stage gates, and doc-deploy pipeline."""

import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml


def _load_ci_workflow():
    workflow_path = Path(".github/workflows/ci.yml")
    assert workflow_path.exists(), f"Workflow file missing: {workflow_path}"
    with open(workflow_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_no_changes_path_filter_job():
    """Verify that the redundant changes path filter job is removed from ci.yml."""
    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert "changes" not in jobs, (
        "Redundant 'changes' path filter job should be removed from ci.yml"
    )


def test_static_checks_builds_and_uploads_docs_site_artifact_on_main():
    """Verify static-checks builds docs under read-only permissions and uploads docs-site on main."""
    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert "static-checks" in jobs, "Missing 'static-checks' job in ci.yml"

    job = jobs["static-checks"]
    assert "permissions" not in job or job.get("permissions") == {"contents": "read"}

    steps = job.get("steps", [])
    checkout_step = next(
        (s for s in steps if "actions/checkout@" in s.get("uses", "")), None
    )
    assert checkout_step is not None, "Missing actions/checkout step in static-checks"
    assert (checkout_step.get("with") or {}).get("persist-credentials") is False

    sync_step = next((s for s in steps if "uv sync" in s.get("run", "")), None)
    assert sync_step is not None, "Missing uv sync step in static-checks"
    assert "--all-groups" in sync_step.get("run", "")

    node_step = next(
        (s for s in steps if "actions/setup-node@" in s.get("uses", "")), None
    )
    assert node_step is not None, "Missing actions/setup-node step in static-checks"
    assert (node_step.get("with") or {}).get("node-version") == "22"

    mermaid_step = next(
        (s for s in steps if "@mermaid-js/mermaid-cli" in s.get("run", "")), None
    )
    assert mermaid_step is not None, (
        "Missing @mermaid-js/mermaid-cli installation in static-checks"
    )

    compile_step = next(
        (s for s in steps if "scripts/generate_docs.py --check" in s.get("run", "")),
        None,
    )
    assert compile_step is not None, (
        "Missing 'scripts/generate_docs.py --check' step in static-checks"
    )
    assert "if" not in compile_step, (
        "Documentation compilation and validation must run on PRs as well as main"
    )

    upload_step = next(
        (
            s
            for s in steps
            if "actions/upload-artifact@" in s.get("uses", "")
            and (s.get("with") or {}).get("name") == "docs-site"
        ),
        None,
    )
    assert upload_step is not None, (
        "Missing actions/upload-artifact step for docs-site in static-checks"
    )
    assert upload_step.get("if") == "github.ref == 'refs/heads/main'"
    upload_with = upload_step.get("with") or {}
    assert upload_with.get("path") == "site/"
    assert upload_with.get("if-no-files-found") == "error"

    sync_idx = steps.index(sync_step)
    node_idx = steps.index(node_step)
    mermaid_idx = steps.index(mermaid_step)
    compile_idx = steps.index(compile_step)
    upload_idx = steps.index(upload_step)

    assert sync_idx < compile_idx, "uv sync must run before doc compilation"
    assert node_idx < compile_idx, "Node setup must run before doc compilation"
    assert mermaid_idx < compile_idx, (
        "Mermaid installation must run before doc compilation"
    )
    assert compile_idx < upload_idx, (
        "Documentation compilation must run before uploading docs-site"
    )


def test_doc_deploy_job_gated_behind_required_ci_and_isolated():
    """Verify doc-deploy waits for required-ci, locks concurrency, and deploys without checkout or build tools."""
    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert "doc-deploy" in jobs, "Missing 'doc-deploy' job in ci.yml"

    job = jobs["doc-deploy"]
    assert job.get("timeout-minutes") == 15
    assert job.get("if") == "github.ref == 'refs/heads/main'"
    assert _needs_set(job) == {"required-ci"}
    assert job.get("concurrency") == {
        "group": "doc-deploy",
        "cancel-in-progress": False,
    }
    assert job.get("permissions") == {"contents": "write"}

    steps = job.get("steps", [])
    forbidden_uses = (
        "actions/checkout",
        "astral-sh/setup-uv",
        "actions/setup-python",
        "actions/setup-node",
    )
    for step in steps:
        uses = step.get("uses", "")
        run_cmd = step.get("run", "")
        assert not any(action in uses for action in forbidden_uses), (
            f"doc-deploy must not check out code or install build toolchains, found {uses!r}"
        )
        assert "npm " not in run_cmd and "uv " not in run_cmd, (
            "doc-deploy must not execute npm or uv under contents: write"
        )
        assert "mkdocs" not in run_cmd, (
            "doc-deploy must publish the prebuilt artifact rather than running mkdocs"
        )

    download_step = next(
        (s for s in steps if "actions/download-artifact@" in s.get("uses", "")), None
    )
    assert download_step is not None, (
        "Missing actions/download-artifact step in doc-deploy"
    )
    assert download_step.get("with") == {"name": "docs-site", "path": "site"}

    deploy_step = next(
        (s for s in steps if s.get("name") == "Deploy Documentation"), None
    )
    assert deploy_step is not None, "Missing 'Deploy Documentation' step in doc-deploy"
    assert deploy_step.get("env") == {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}

    deploy_script = deploy_step.get("run", "")
    assert "${{ secrets.GITHUB_TOKEN }}" not in deploy_script, (
        "GITHUB_TOKEN must be passed through env rather than interpolated into run"
    )
    assert ".nojekyll" in deploy_script
    assert "GITHUB_SHA" in deploy_script
    assert "refs/heads/gh-pages" in deploy_script
    assert steps.index(download_step) < steps.index(deploy_step)


def _find_git_bash() -> str:
    if sys.platform == "win32":
        git_exe = shutil.which("git")
        if git_exe:
            git_root = Path(git_exe).resolve().parent.parent
            for candidate in (
                git_root / "bin" / "bash.exe",
                git_root / "usr" / "bin" / "bash.exe",
            ):
                if candidate.is_file():
                    return str(candidate)
    bash_exe = shutil.which("bash")
    assert bash_exe is not None, "bash executable is required to test doc-deploy script"
    return bash_exe


def _run_git(cwd: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


def test_doc_deploy_script_publishes_site_and_skips_stale_reruns(tmp_path: Path):
    """Execute the ci.yml doc-deploy shell script against a bare git remote across edge cases."""
    workflow = _load_ci_workflow()
    deploy_step = next(
        s
        for s in workflow["jobs"]["doc-deploy"]["steps"]
        if s.get("name") == "Deploy Documentation"
    )
    deploy_script = deploy_step["run"]
    bash_exe = _find_git_bash()

    server_root = tmp_path / "server"
    bare_repo = server_root / "fderuiter" / "sortify.git"
    bare_repo.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", "--quiet", str(bare_repo)], check=True)

    # Create three linear commits on main: sha1 -> sha2 -> sha3
    upstream_work = tmp_path / "upstream"
    upstream_work.mkdir()
    _run_git(upstream_work, "init", "--quiet")
    _run_git(upstream_work, "config", "user.name", "Test User")
    _run_git(upstream_work, "config", "user.email", "test@example.com")

    shas: list[str] = []
    for idx in (1, 2, 3):
        (upstream_work / "README.md").write_text(f"commit {idx}\n", encoding="utf-8")
        _run_git(upstream_work, "add", "README.md")
        _run_git(upstream_work, "commit", "--quiet", "-m", f"main commit {idx}")
        shas.append(_run_git(upstream_work, "rev-parse", "HEAD"))
    _run_git(upstream_work, "push", "--quiet", str(bare_repo), "HEAD:refs/heads/main")
    sha1, sha2, sha3 = shas

    def run_deploy(sha: str, files: dict[str, str]) -> subprocess.CompletedProcess[str]:
        runner_dir = tmp_path / f"run-{sha[:7]}-{len(list(tmp_path.iterdir()))}"
        site_dir = runner_dir / "site"
        site_dir.mkdir(parents=True)
        for rel_path, content in files.items():
            target = site_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "GITHUB_TOKEN": "ghs_ephemeral_test_token_value",
                "GITHUB_SERVER_URL": server_root.resolve().as_uri(),
                "GITHUB_REPOSITORY": "fderuiter/sortify",
                "GITHUB_SHA": sha,
            }
        )
        return subprocess.run(
            [bash_exe, "-c", deploy_script],
            cwd=runner_dir,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    # 1. Initial publish when gh-pages does not exist yet
    run_deploy(sha1, {"index.html": "<h1>v1</h1>", "stale/page.html": "old"})
    gh_files = set(
        _run_git(
            bare_repo, "ls-tree", "-r", "--name-only", "refs/heads/gh-pages"
        ).splitlines()
    )
    assert gh_files == {".nojekyll", "index.html", "stale/page.html"}
    assert f"Deployed {sha1}" in _run_git(
        bare_repo, "log", "-1", "--format=%B", "refs/heads/gh-pages"
    )

    # 2. Publish sha2: removes stale files, restores .nojekyll, fast-forwards gh-pages
    run_deploy(sha2, {"index.html": "<h1>v2</h1>"})
    gh_files_v2 = set(
        _run_git(
            bare_repo, "ls-tree", "-r", "--name-only", "refs/heads/gh-pages"
        ).splitlines()
    )
    assert gh_files_v2 == {".nojekyll", "index.html"}
    assert (
        _run_git(bare_repo, "show", "refs/heads/gh-pages:index.html") == "<h1>v2</h1>"
    )
    head_after_v2 = _run_git(bare_repo, "rev-parse", "refs/heads/gh-pages")

    # 3. Out-of-order rerun of older sha1 must skip pushing and preserve sha2 on gh-pages
    stale_res = run_deploy(sha1, {"index.html": "<h1>v1-rerun</h1>"})
    assert "Skipping deployment" in stale_res.stdout
    assert _run_git(bare_repo, "rev-parse", "refs/heads/gh-pages") == head_after_v2
    assert (
        _run_git(bare_repo, "show", "refs/heads/gh-pages:index.html") == "<h1>v2</h1>"
    )

    # 4. Legacy MkDocs short-SHA message ("Deployed <short> with MkDocs version: 1.6.1") also blocks older reruns
    _run_git(upstream_work, "fetch", "--quiet", str(bare_repo), "refs/heads/gh-pages")
    _run_git(upstream_work, "checkout", "--quiet", "-B", "gh-pages", "FETCH_HEAD")
    _run_git(
        upstream_work,
        "commit",
        "--quiet",
        "--amend",
        "-m",
        f"Deployed {sha2[:7]} with MkDocs version: 1.6.1",
    )
    _run_git(
        upstream_work,
        "push",
        "--quiet",
        "--force",
        str(bare_repo),
        "HEAD:refs/heads/gh-pages",
    )
    legacy_head = _run_git(bare_repo, "rev-parse", "refs/heads/gh-pages")
    legacy_stale_res = run_deploy(sha1, {"index.html": "<h1>v1-against-legacy</h1>"})
    assert "Skipping deployment" in legacy_stale_res.stdout
    assert _run_git(bare_repo, "rev-parse", "refs/heads/gh-pages") == legacy_head

    # 5. Revert commit sha3 (identical tree to v2) still records sha3 so a later sha1/sha2 rerun cannot clobber
    run_deploy(sha3, {"index.html": "<h1>v2</h1>"})
    assert f"Deployed {sha3}" in _run_git(
        bare_repo, "log", "-1", "--format=%B", "refs/heads/gh-pages"
    )
    head_after_v3 = _run_git(bare_repo, "rev-parse", "refs/heads/gh-pages")

    # 6. Same-SHA rerun with identical site skips empty push cleanly
    same_res = run_deploy(sha3, {"index.html": "<h1>v2</h1>"})
    assert "already up to date" in same_res.stdout
    assert _run_git(bare_repo, "rev-parse", "refs/heads/gh-pages") == head_after_v3
    assert "ghs_ephemeral_test_token_value" not in (bare_repo / "config").read_text(
        encoding="utf-8"
    )


FAST_STAGE_JOBS = {"security-and-audit", "type-check", "static-checks"}
GATED_TEST_JOBS = ("build-and-test", "heavy-tests", "verify-offline-bundle")


def _needs_set(job: dict) -> set[str]:
    needs = job.get("needs", [])
    if isinstance(needs, str):
        return {needs}
    return set(needs)


def test_fast_stage_jobs_run_in_parallel_with_least_privilege():
    """Verify first-stage fast checks have no upstream needs and keep least privilege."""
    workflow = _load_ci_workflow()
    assert workflow.get("permissions") == {"contents": "read"}

    jobs = workflow.get("jobs", {})
    for job_name in FAST_STAGE_JOBS:
        assert job_name in jobs, f"Missing fast-check job {job_name!r} in ci.yml"
        assert not _needs_set(jobs[job_name]), (
            f"First-stage job {job_name!r} must run immediately without needs"
        )

    pr_read_jobs = [
        name
        for name, job in jobs.items()
        if (job.get("permissions") or {}).get("pull-requests") == "read"
    ]
    assert pr_read_jobs == ["security-and-audit"], (
        f"Only 'security-and-audit' should request pull-requests: read, got {pr_read_jobs}"
    )


@pytest.mark.parametrize("job_name", GATED_TEST_JOBS)
def test_expensive_test_jobs_gated_behind_fast_checks(job_name: str):
    """Verify expensive test and bundle jobs wait for all three fast-check jobs."""
    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert job_name in jobs, f"Missing {job_name!r} job in ci.yml"

    job = jobs[job_name]
    assert _needs_set(job) == FAST_STAGE_JOBS, (
        f"{job_name!r} must declare needs on {sorted(FAST_STAGE_JOBS)}"
    )
    assert "if" not in job, (
        f"{job_name!r} must not override default failure/skip gating with an 'if' condition"
    )


def test_required_ci_gate_depends_on_all_upstream_jobs_and_fails_closed():
    """Verify required-ci aggregates all six upstream jobs and fails closed on failure or skip."""
    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert "required-ci" in jobs, "Missing 'required-ci' job in ci.yml"

    job = jobs["required-ci"]
    assert job.get("name") == "Required CI"
    assert job.get("if") == "${{ always() }}"

    expected_upstream = FAST_STAGE_JOBS | set(GATED_TEST_JOBS)
    assert _needs_set(job) == expected_upstream

    steps = job.get("steps", [])
    gate_step = next(
        (s for s in steps if "actions/github-script@" in s.get("uses", "")), None
    )
    assert gate_step is not None, (
        "Missing actions/github-script gate step in required-ci"
    )

    script = (gate_step.get("with") or {}).get("script", "")
    assert "${{ toJson(needs) }}" in script
    assert "jobData.result !== 'success'" in script
    assert "core.setFailed(" in script


def test_heavy_tests_runs_single_uv_sync_with_ml_extra():
    """Verify heavy-tests runs uv sync once with --extra ml during dependency installation."""
    pyproject = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    optional_deps = pyproject.get("project", {}).get("optional-dependencies", {})
    assert "ml" in optional_deps and optional_deps["ml"], (
        "pyproject.toml must define non-empty [project.optional-dependencies].ml"
    )

    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert "heavy-tests" in jobs, "Missing 'heavy-tests' job in ci.yml"

    steps = jobs["heavy-tests"].get("steps", [])
    uv_sync_steps = [s for s in steps if "uv sync" in s.get("run", "")]
    assert len(uv_sync_steps) == 1, (
        f"heavy-tests must execute 'uv sync' in exactly one step, found {len(uv_sync_steps)}"
    )

    install_step = uv_sync_steps[0]
    assert install_step.get("name") == "Install dependencies"
    install_run = install_step.get("run", "")
    assert install_run.count("uv sync") == 1
    assert install_run.strip() == "uv sync --frozen --all-groups --extra ml"

    test_step = next(
        (s for s in steps if s.get("name") == "Heavy ML and Integration Tests"), None
    )
    assert test_step is not None, "Missing 'Heavy ML and Integration Tests' step"
    assert "uv sync" not in test_step.get("run", "")
    assert 'pytest -m "slow"' in test_step.get("run", "")
    assert steps.index(install_step) < steps.index(test_step)


def test_no_ci_job_executes_duplicate_uv_sync():
    """Verify no job in ci.yml executes uv sync more than once."""
    workflow = _load_ci_workflow()
    for job_name, job in workflow.get("jobs", {}).items():
        sync_count = sum(
            step.get("run", "").count("uv sync") for step in job.get("steps", [])
        )
        assert sync_count <= 1, (
            f"Job {job_name!r} runs 'uv sync' {sync_count} times; expected at most once"
        )
