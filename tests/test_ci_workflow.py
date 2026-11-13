"""Tests for CI workflow configuration and doc-deploy pipeline."""

from pathlib import Path

import yaml


def _load_ci_workflow():
    workflow_path = Path(".github/workflows/ci.yml")
    assert workflow_path.exists(), f"Workflow file missing: {workflow_path}"
    with open(workflow_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def test_changes_job_docs_path_filter():
    """Verify that the changes job path filter accurately includes all doc-relevant paths

    and excludes unmonitored directories like tests/ and .github/.
    """
    workflow = _load_ci_workflow()
    jobs = workflow.get("jobs", {})
    assert "changes" in jobs, "Missing 'changes' job in ci.yml"

    changes_job = jobs["changes"]
    steps = changes_job.get("steps", [])
    filter_step = next(
        (s for s in steps if "dorny/paths-filter" in s.get("uses", "")), None
    )
    assert filter_step is not None, "Missing dorny/paths-filter step in changes job"

    filters_raw = filter_step.get("with", {}).get("filters", "")
    filters = yaml.safe_load(filters_raw)
    assert "docs" in filters, "Missing 'docs' filter key in changes job"

    doc_paths = filters["docs"]
    required_paths = [
        "app/**",
        "scripts/**",
        "docs/**",
        "mkdocs.yml",
        "pyproject.toml",
    ]
    for req in required_paths:
        assert req in doc_paths, (
            f"Expected '{req}' in doc path filters, found {doc_paths}"
        )

    # Guardrails: verify unmonitored paths are not included
    for excluded in ["tests/**", ".github/**"]:
        assert excluded not in doc_paths, (
            f"Unmonitored path '{excluded}' should not be in doc filter"
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
    assert job.get("if") == "needs.changes.outputs.docs == 'true'"
    assert set(job.get("needs", [])) == {"static-checks", "changes"}

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
