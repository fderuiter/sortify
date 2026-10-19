"""Tests for the shared sort pipeline (issue #708)."""

from pathlib import Path

import pytest

from app.config import AppSettings
from app.core.pipeline import SortPipeline, drop_scope
from app.core.session import AppSession


def _write_corpus(base: Path) -> None:
    (base / "finance_doc.txt").write_text("Finance, investment, and banking report.")
    (base / "tech_doc.txt").write_text(
        "Software engineering, Python, algorithms, and computers."
    )
    (base / "health_doc.txt").write_text(
        "Medical science, clinical trial, doctor, and patient data."
    )


@pytest.fixture
def session(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _write_corpus(src)
    settings = AppSettings()
    settings.AI_CONSENT_GRANTED = False
    s = AppSession(settings, base_dir=str(src))
    try:
        yield s
    finally:
        s.close()


def _planned_sources(plan) -> set[str]:
    """File names anywhere in the plan's folder tree."""
    names: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            for child in node.values():
                walk(child)
        elif isinstance(node, (list, tuple, set)):
            for child in node:
                walk(child)
        elif isinstance(node, str):
            names.add(Path(node).name)

    walk(plan.plan if hasattr(plan, "plan") else plan)
    return names


def test_drop_scope_sorts_a_folder_whole(tmp_path):
    assert drop_scope(str(tmp_path)) == (str(tmp_path), None)


def test_drop_scope_sorts_a_file_inside_its_parent(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert drop_scope(str(f)) == (str(tmp_path), [str(f)])


def test_plan_discovers_every_file_and_moves_nothing(session):
    plan = SortPipeline(session).plan()

    assert {"finance_doc.txt", "tech_doc.txt", "health_doc.txt"} <= _planned_sources(
        plan
    )
    for name in ("finance_doc.txt", "tech_doc.txt", "health_doc.txt"):
        assert (Path(session.base_dir) / name).exists()


def test_plan_limits_to_the_items_given(session):
    plan = SortPipeline(session).plan(["tech_doc.txt"])

    assert "tech_doc.txt" in _planned_sources(plan)
    assert not _planned_sources(plan) & {"finance_doc.txt", "health_doc.txt"}


def test_dry_run_returns_the_plan_and_no_summary(session):
    plan, summary = SortPipeline(session).run(dry_run=True)

    assert plan
    assert summary is None
    assert (Path(session.base_dir) / "finance_doc.txt").exists()


def test_run_moves_the_planned_files(session):
    plan, summary = SortPipeline(session).run()

    assert summary is not None
    remaining = {p.name for p in Path(session.base_dir).iterdir() if p.is_file()}
    assert not remaining & {"finance_doc.txt", "tech_doc.txt", "health_doc.txt"}
