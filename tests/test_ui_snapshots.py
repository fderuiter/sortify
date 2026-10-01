import json
import os

import pytest

import app.config as app_config
from app.config import AppSettings
from app.ui.app import AutoSorterApp

pytestmark = pytest.mark.xdist_group(name="tui")

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "snapshots")


def assert_snapshot(snapshot_name, actual_state):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    snapshot_path = os.path.join(SNAPSHOT_DIR, f"{snapshot_name}.json")

    update_snapshots = os.environ.get("UPDATE_SNAPSHOTS") == "1"

    if not os.path.exists(snapshot_path) or update_snapshots:
        with open(snapshot_path, "w") as f:
            json.dump(actual_state, f, indent=2)
        if not update_snapshots:
            pytest.fail(
                f"Snapshot {snapshot_name} generated for the first time. Run again to verify."
            )
        return

    with open(snapshot_path, "r") as f:
        expected_state = json.load(f)

    assert actual_state == expected_state, f"Snapshot mismatch for {snapshot_name}"


@pytest.fixture
def headless_app(tmp_path, monkeypatch):
    monkeypatch.setattr(AppSettings, "_trigger_save", lambda self: self._save())
    dummy_settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    dummy_settings.AI_CONSENT_GRANTED = False
    monkeypatch.setattr(app_config, "settings", dummy_settings, raising=False)

    app = AutoSorterApp(dummy_settings)
    app.plan = {}
    app.plan_errors = {}
    yield app

    try:
        from app.core.shared_registry import SharedModelRegistry

        reg = getattr(SharedModelRegistry, "_instance", None)
        if reg is not None:
            reg._cached_settings = None
    except Exception:
        pass


def test_empty_plan_rendering(headless_app):
    headless_app.render_tree()
    state = headless_app.get_tree_state()
    assert_snapshot("empty_plan", state)


def test_clustering_rendering(headless_app):
    headless_app.plan = {
        "Finance Reports": {"q1_report.pdf": None, "q2_report.pdf": None},
        "Images": {"vacation.jpg": None},
    }
    headless_app.render_tree()
    state = headless_app.get_tree_state()
    assert_snapshot("clustering_plan", state)


def test_nested_folders_rendering(headless_app):
    headless_app.plan = {"Work": {"Projects": {"Project Alpha": {"spec.docx": None}}}}
    headless_app.render_tree()
    state = headless_app.get_tree_state()
    assert_snapshot("nested_folders_plan", state)


def test_error_states_rendering(headless_app):
    headless_app.plan = {"Invoices": {"invoice_101.pdf": None, "invoice_102.pdf": None}}
    # Simulate an error on invoice_102.pdf
    headless_app.plan_errors = {"invoice_102.pdf": "File locked by another process"}
    headless_app.render_tree()
    state = headless_app.get_tree_state()
    assert_snapshot("error_states_plan", state)


def test_manual_override_visibility(headless_app):
    headless_app.plan = {
        "Documents": {
            "report.txt": None,
            "secret.txt": {
                "__type__": "file",
                "status": "Already Sorted",
                "target_filename": "secret_override.txt",
            },
        }
    }
    headless_app.render_tree()
    state = headless_app.get_tree_state()
    assert_snapshot("manual_override_plan", state)
