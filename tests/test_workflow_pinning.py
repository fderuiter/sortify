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
