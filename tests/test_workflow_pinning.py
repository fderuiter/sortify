import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = sorted(Path(".github/workflows").glob("*.yml"))
FULL_SHA = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _steps(workflow):
    for job in (workflow.get("jobs") or {}).values():
        yield from job.get("steps") or []


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_third_party_actions_are_pinned_to_commit_shas(path):
    """A tag can be moved to new code; a full commit SHA cannot."""
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    unpinned = [
        step["uses"]
        for step in _steps(workflow)
        if "uses" in step
        and not step["uses"].startswith("./")
        and not FULL_SHA.match(step["uses"])
    ]
    assert not unpinned, (
        f"{path.name} uses actions not pinned to a commit SHA: {unpinned}"
    )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_setup_uv_pins_an_exact_uv_version(path):
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    for step in _steps(workflow):
        if "astral-sh/setup-uv@" in step.get("uses", ""):
            version = (step.get("with") or {}).get("version", "")
            assert re.fullmatch(r"\d+\.\d+\.\d+", version), (
                f"{path.name}: setup-uv must pin an exact uv version, got {version!r}"
            )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_model_weight_caches_restore_only_on_an_exact_key(path):
    """A prefix restore would hand a run weights saved for another manifest."""
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    for step in _steps(workflow):
        if "actions/cache@" not in step.get("uses", ""):
            continue
        cache = step.get("with") or {}
        if str(cache.get("key", "")).startswith("model-weights-"):
            assert "restore-keys" not in cache, (
                f"{path.name}: {step.get('name')!r} restores model weights by prefix"
            )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_declares_a_timeout(path):
    """Without one, a hung job holds a runner for GitHub's six-hour default."""
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    missing = [
        name
        for name, job in (workflow.get("jobs") or {}).items()
        if "uses" not in job and "timeout-minutes" not in job
    ]
    assert not missing, f"{path.name}: jobs without timeout-minutes: {missing}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_checkouts_do_not_persist_credentials(path):
    """A persisted token sits in .git/config for every later step to read.

    Doc Deploy is the one exception: mkdocs gh-deploy pushes with it, so it
    persists the token on main only.
    """
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job_name, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            if "actions/checkout@" not in step.get("uses", ""):
                continue
            persist = (step.get("with") or {}).get("persist-credentials")
            if job_name == "doc-deploy":
                assert persist == "${{ github.ref == 'refs/heads/main' }}"
            else:
                assert persist is False, (
                    f"{path.name}: {job_name} checks out with persisted credentials"
                )


def test_docs_path_filter_includes_application_source():
    """Application source and doc generator changes must trigger documentation workflow."""
    ci_path = next(
        (p for p in WORKFLOWS if p.name == "ci.yml"),
        Path(".github/workflows/ci.yml"),
    )
    workflow = yaml.safe_load(ci_path.read_text(encoding="utf-8"))
    changes_job = workflow.get("jobs", {}).get("changes", {})
    steps = changes_job.get("steps") or []
    filter_step = next(
        (s for s in steps if "dorny/paths-filter@" in s.get("uses", "")), None
    )
    assert filter_step is not None, "paths-filter step not found in changes job"
    raw_filters = (filter_step.get("with") or {}).get("filters", "")
    filters = yaml.safe_load(raw_filters)
    assert "docs" in filters, "docs filter not defined in paths-filter"
    docs_patterns = filters["docs"]
    assert "app/**" in docs_patterns, "app/** must be included in docs path filter"
    assert "scripts/generate_docs.py" in docs_patterns, (
        "scripts/generate_docs.py must be included in docs path filter"
    )
    assert "pyproject.toml" in docs_patterns, (
        "pyproject.toml must be included in docs path filter"
    )
