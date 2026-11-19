"""Tests for CI workflow configuration, stage gates, and doc-deploy pipeline."""

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


def test_doc_deploy_job_configuration_and_steps():
    """Verify that doc-deploy configures Node 22, Mermaid CLI, doc compilation,

    and deployment only on main.
    """
    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert "doc-deploy" in jobs, "Missing 'doc-deploy' job in ci.yml"

    job = jobs["doc-deploy"]
    assert job.get("timeout-minutes") == 15
    assert job.get("if") == "github.ref == 'refs/heads/main'"
    assert set(job.get("needs", [])) == {"static-checks"}

    steps = job.get("steps", [])
    step_names = [s.get("name", "") for s in steps]

    # Verify Node setup
    node_step = next(
        (s for s in steps if "actions/setup-node" in s.get("uses", "")), None
    )
    assert node_step is not None, "Missing actions/setup-node step in doc-deploy"
    assert node_step.get("with", {}).get("node-version") == "22"

    # Verify Mermaid CLI installation
    mermaid_step = next(
        (s for s in steps if "@mermaid-js/mermaid-cli" in s.get("run", "")), None
    )
    assert mermaid_step is not None, (
        "Missing @mermaid-js/mermaid-cli installation in doc-deploy"
    )

    # Verify dependencies synchronization
    sync_step = next((s for s in steps if "uv sync" in s.get("run", "")), None)
    assert sync_step is not None, "Missing uv sync step in doc-deploy"
    assert "--group docs" in sync_step.get("run", "")

    # Verify doc compilation step
    compile_step = next(
        (s for s in steps if "scripts/generate_docs.py" in s.get("run", "")), None
    )
    assert compile_step is not None, (
        "Missing scripts/generate_docs.py execution in doc-deploy"
    )
    assert "uv run python scripts/generate_docs.py" in compile_step.get("run", "")
    assert "if" not in compile_step, (
        "Documentation compilation should evaluate on PRs as well as main"
    )

    # Verify mkdocs gh-deploy step
    deploy_step = next(
        (s for s in steps if "mkdocs gh-deploy" in s.get("run", "")), None
    )
    assert deploy_step is not None, "Missing mkdocs gh-deploy step in doc-deploy"
    assert deploy_step.get("if") == "github.ref == 'refs/heads/main'"

    # Verify step order: node, mermaid, and python deps must be set up before compilation,
    # and compilation must happen before deploy
    compile_idx = steps.index(compile_step)
    deploy_idx = steps.index(deploy_step)
    sync_idx = steps.index(sync_step)
    node_idx = steps.index(node_step)
    mermaid_idx = steps.index(mermaid_step)

    assert sync_idx < compile_idx, "uv sync must run before doc compilation"
    assert node_idx < compile_idx, "Node setup must run before doc compilation"
    assert mermaid_idx < compile_idx, (
        "Mermaid installation must run before doc compilation"
    )
    assert compile_idx < deploy_idx, (
        "Documentation compilation must run before mkdocs gh-deploy"
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
