"""Sandbox analysis exercises the production pipeline without moving files."""

import json

import pytest

import sandbox_cli
from app.core.pipeline import SortPipeline
from app.core.session import AppSession


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    folder = tmp_path / "dataset"
    folder.mkdir()
    for name, text in {
        "finance.txt": "invoice banking finance",
        "health.txt": "medical doctor health",
        "tech.txt": "python software computer",
    }.items():
        (folder / name).write_text(text)
    monkeypatch.setattr(sandbox_cli, "SANDBOX_DIR", str(folder))
    monkeypatch.setattr(sandbox_cli, "SANDBOX_DB_PATH", str(tmp_path / "sandbox.db"))
    return folder


@pytest.mark.parametrize("json_output", [False, True])
def test_sandbox_analysis_preserves_bytes_and_output_shape(
    sandbox, capsys, monkeypatch, json_output
):
    before = {p.name: p.read_bytes() for p in sandbox.iterdir()}

    def forbidden(*args, **kwargs):
        pytest.fail("Sandbox analysis must never execute a plan")

    monkeypatch.setattr(SortPipeline, "execute", forbidden)
    sandbox_cli.analyze_all(json_output=json_output)
    output = capsys.readouterr()
    assert isinstance(json.loads(output.out), dict)
    assert ("--- Analysis Sorting Plan ---" in output.err) == (not json_output)
    assert {p.name: p.read_bytes() for p in sandbox.iterdir()} == before


def test_planning_failure_closes_the_real_session(sandbox, monkeypatch):
    closed = []
    original_close = AppSession.close

    def close(session):
        original_close(session)
        closed.append(session)

    def fail(*args, **kwargs):
        raise RuntimeError("extraction failed")

    monkeypatch.setattr(AppSession, "close", close)
    monkeypatch.setattr(SortPipeline, "plan", fail)
    with pytest.raises(RuntimeError, match="extraction failed"):
        sandbox_cli.analyze_all()
    assert len(closed) == 1
    assert not closed[0].db_worker.thread.is_alive()
