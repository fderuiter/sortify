import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# --- HEADLESS GUI MOCKING ---
from tests.mock_ui import HeadlessTreeview


class DummyWidget:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, name):
        return MagicMock()

    def pack(self, *args, **kwargs):
        pass

    def configure(self, *args, **kwargs):
        pass

    def delete(self, *args, **kwargs):
        pass

    def insert(self, *args, **kwargs):
        pass

class DummyCTk(DummyWidget):
    pass

class DummyVar(DummyWidget):
    def get(self):
        return False

    def set(self, val):
        pass

mock_ctk = MagicMock()
mock_ctk.CTk = DummyCTk
mock_ctk.CTkFrame = DummyWidget
mock_ctk.CTkLabel = DummyWidget
mock_ctk.CTkButton = DummyWidget
mock_ctk.CTkProgressBar = DummyWidget
mock_ctk.CTkSwitch = DummyWidget
mock_ctk.BooleanVar = DummyVar
mock_ctk.CTkScrollableFrame = DummyWidget

mock_tk = MagicMock()
mock_tk.Menu = DummyWidget
mock_tk.Canvas = DummyWidget

mock_ttk = MagicMock()
mock_ttk.Treeview = HeadlessTreeview
mock_ttk.Scrollbar = DummyWidget
mock_tk.ttk = mock_ttk

sys.modules['customtkinter'] = mock_ctk
sys.modules['tkinter'] = mock_tk
sys.modules['tkinter.ttk'] = mock_ttk
sys.modules['tkinter.filedialog'] = MagicMock()
sys.modules['tkinter.messagebox'] = MagicMock()

# Inject dummy settings
from app.config import AppSettings  # noqa: E402

dummy_settings = AppSettings()
sys.modules['app.config'].settings = dummy_settings

from app.ui.app import AutoSorterApp  # noqa: E402

SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "snapshots")

def assert_snapshot(snapshot_name, actual_state):
    os.makedirs(SNAPSHOT_DIR, exist_ok=True)
    snapshot_path = os.path.join(SNAPSHOT_DIR, f"{snapshot_name}.json")
    
    update_snapshots = os.environ.get("UPDATE_SNAPSHOTS") == "1"
    
    if not os.path.exists(snapshot_path) or update_snapshots:
        with open(snapshot_path, "w") as f:
            json.dump(actual_state, f, indent=2)
        if not update_snapshots:
            pytest.fail(f"Snapshot {snapshot_name} generated for the first time. Run again to verify.")
        return
        
    with open(snapshot_path, "r") as f:
        expected_state = json.load(f)
        
    assert actual_state == expected_state, f"Snapshot mismatch for {snapshot_name}"

@pytest.fixture
def headless_app():
    app = AutoSorterApp(dummy_settings)
    app.plan = {}
    app.plan_errors = {}
    return app

def test_empty_plan_rendering(headless_app):
    headless_app.render_tree()
    state = headless_app.tree.dump_state()
    assert_snapshot("empty_plan", state)

def test_clustering_rendering(headless_app):
    headless_app.plan = {
        "Finance Reports": {
            "q1_report.pdf": None,
            "q2_report.pdf": None
        },
        "Images": {
            "vacation.jpg": None
        }
    }
    headless_app.render_tree()
    state = headless_app.tree.dump_state()
    assert_snapshot("clustering_plan", state)

def test_nested_folders_rendering(headless_app):
    headless_app.plan = {
        "Work": {
            "Projects": {
                "Project Alpha": {
                    "spec.docx": None
                }
            }
        }
    }
    headless_app.render_tree()
    state = headless_app.tree.dump_state()
    assert_snapshot("nested_folders_plan", state)

def test_error_states_rendering(headless_app):
    headless_app.plan = {
        "Invoices": {
            "invoice_101.pdf": None,
            "invoice_102.pdf": None
        }
    }
    # Simulate an error on invoice_102.pdf
    headless_app.plan_errors = {
        "invoice_102.pdf": "File locked by another process"
    }
    headless_app.render_tree()
    state = headless_app.tree.dump_state()
    assert_snapshot("error_states_plan", state)

def test_manual_override_visibility(headless_app):
    headless_app.plan = {
        "Documents": {
            "report.txt": None,
            "secret.txt": {
                "__type__": "file",
                "status": "Already Sorted",
                "target_filename": "secret_override.txt"
            }
        }
    }
    headless_app.render_tree()
    state = headless_app.tree.dump_state()
    assert_snapshot("manual_override_plan", state)

