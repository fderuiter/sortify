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
