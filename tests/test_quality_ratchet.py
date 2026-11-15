"""Prove legacy debt cannot grow through file exemptions or baseline updates."""

import json

import pytest

from scripts.quality_ratchet import exception_counts, increases, main, read_baseline


def test_counts_do_not_trade_between_rules_or_files():
    baseline = {"app/old.py": {"S110": 2, "BLE001": 3}}
    assert not increases({"app/old.py": {"S110": 1}}, baseline)
    assert increases({"app/old.py": {"S110": 3}}, baseline)
    assert increases({"app/new.py": {"S110": 1}}, baseline)
    assert increases({"app/old.py": {"SIM105": 1}}, baseline)


@pytest.mark.parametrize(
    "counts",
    [{"../x": {"S110": 1}}, {"app/x.py": {"S110": -1}}, {"app/x.py": {"S110": True}}],
)
def test_invalid_budgets_fail_closed(counts):
    with pytest.raises(ValueError):
        read_baseline(
            json.dumps({"schema": 1, "tool": "exceptions", "counts": counts}),
            "exceptions",
        )


def test_exempt_files_still_reject_growth_and_update_only_shrinks(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    legacy = app / "old.py"
    bad = "try:\n    work()\nexcept Exception:\n    pass\n"
    legacy.write_text(bad)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.ruff.lint.per-file-ignores]\n"app/*" = ["BLE001", "S110", "SIM105"]\n'
    )
    assert exception_counts(tmp_path)["app/old.py"]["S110"] == 1
    args = ["exceptions", "--root", str(tmp_path)]
    assert main([*args, "--initialize"]) == 0
    baseline = tmp_path / "scripts/baselines/exceptions.json"
    initial = baseline.read_text()
    legacy.write_text(bad * 2)
    assert main(args) == 1
    assert main([*args, "--update"]) == 1
    assert baseline.read_text() == initial
    legacy.write_text(bad)
    (app / "new.py").write_text(bad)
    assert main(args) == 1
    (app / "new.py").unlink()
    legacy.write_text("pass\n")
    assert main([*args, "--update"]) == 0
    assert json.loads(baseline.read_text())["counts"] == {}
    assert main([*args, "--initialize"]) == 1


def test_baseline_increases_cannot_be_hidden_by_editing_json(tmp_path, monkeypatch):
    import scripts.quality_ratchet as ratchet

    path = tmp_path / "scripts/baselines/exceptions.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {"schema": 1, "tool": "exceptions", "counts": {"app/a.py": {"S110": 2}}}
        )
    )
    monkeypatch.setattr(ratchet, "exception_counts", lambda root: {})
    monkeypatch.setattr(
        ratchet, "baseline_at_ref", lambda *args: {"app/a.py": {"S110": 1}}
    )
    assert main(["exceptions", "--root", str(tmp_path), "--base-ref", "main"]) == 1
