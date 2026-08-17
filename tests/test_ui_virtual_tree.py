import sys
import pytest
from unittest.mock import MagicMock
from nicegui import Client

from app.config import AppSettings
from app.ui.app import AutoSorterApp

@pytest.mark.anyio
async def test_dynamic_virtual_tree_expansion():
    """Verify dynamic virtual tree expansion behavior."""
    # Use nicegui Client block for proper initialization environment
    with Client(None):
        settings = AppSettings()
        app = AutoSorterApp(settings)
        app.disable_auto_expand_in_test = True  # Enable lazy evaluation in test
        
        # Define a standard plan hierarchy
        app.plan = {
            "Documents": {
                "Invoices": {
                    "invoice_1.pdf": {"__type__": "file", "status": "Proposed"},
                },
                "report.txt": {"__type__": "file", "status": "Proposed"},
            },
            "Images": {
                "photo.png": {"__type__": "file", "status": "Proposed"},
            },
        }

        # 1. Initial Render:
        # Since expanded_nodes is empty, only top level directories ("Documents", "Images") should be populated,
        # and their children should be empty/contain only a dummy node.
        app.render_tree()
        
        assert len(app.tree_nodes) == 2
        
        # Verify "Documents" node
        docs_node = next(n for n in app.tree_nodes if n["text"] == "Documents")
        assert docs_node["id"] == "Documents"
        assert docs_node["is_file"] is False
        # It should have exactly one dummy child because it's not expanded
        assert len(docs_node["children"]) == 1
        dummy_child = docs_node["children"][0]
        assert dummy_child["id"] == "Documents__dummy"
        assert dummy_child["is_file"] is False
        assert dummy_child["disabled"] is True

        # Verify "Images" node
        images_node = next(n for n in app.tree_nodes if n["text"] == "Images")
        assert images_node["id"] == "Images"
        assert images_node["is_file"] is False
        assert len(images_node["children"]) == 1
        assert images_node["children"][0]["id"] == "Images__dummy"

        # 2. Expanding "Documents":
        # Simulate user expanding "Documents" folder
        class MockEvent:
            def __init__(self, value):
                self.value = value

        # Expansion event receives currently expanded keys/IDs
        await app.handle_tree_expand(MockEvent(["Documents"]))

        assert app.expanded_nodes == {"Documents"}

        # Render tree should now expand "Documents" but keep "Images" collapsed,
        # and also keep nested "Documents/Invoices" collapsed.
        app.render_tree()

        docs_node = next(n for n in app.tree_nodes if n["text"] == "Documents")
        # Direct children of "Documents" are "Invoices" and "report.txt"
        assert len(docs_node["children"]) == 2
        
        # Verify "report.txt" child
        file_node = next(n for n in docs_node["children"] if n["is_file"])
        assert file_node["id"] == "Documents/report.txt"
        assert "report.txt" in file_node["text"]
        assert file_node["filepath"] == "Documents/report.txt"

        # Verify nested "Invoices" child is present but collapsed (with dummy)
        invoices_node = next(n for n in docs_node["children"] if not n["is_file"])
        assert invoices_node["id"] == "Documents/Invoices"
        assert len(invoices_node["children"]) == 1
        assert invoices_node["children"][0]["id"] == "Documents/Invoices__dummy"

        # Verify "Images" is still collapsed
        images_node = next(n for n in app.tree_nodes if n["text"] == "Images")
        assert len(images_node["children"]) == 1
        assert images_node["children"][0]["id"] == "Images__dummy"

        # 3. Expanding "Documents/Invoices":
        await app.handle_tree_expand(MockEvent(["Documents", "Documents/Invoices"]))
        assert app.expanded_nodes == {"Documents", "Documents/Invoices"}

        app.render_tree()

        docs_node = next(n for n in app.tree_nodes if n["text"] == "Documents")
        invoices_node = next(n for n in docs_node["children"] if not n["is_file"])
        
        # Verify invoice_1.pdf is now fully loaded
        assert len(invoices_node["children"]) == 1
        invoice_file_node = invoices_node["children"][0]
        assert invoice_file_node["id"] == "Documents/Invoices/invoice_1.pdf"
        assert invoice_file_node["is_file"] is True
        assert invoice_file_node["filepath"] == "Documents/Invoices/invoice_1.pdf"

        # 4. Collapsing "Documents":
        await app.handle_tree_expand(MockEvent([]))
        assert app.expanded_nodes == set()

        app.render_tree()
        docs_node = next(n for n in app.tree_nodes if n["text"] == "Documents")
        # Should be back to 1 dummy child
        assert len(docs_node["children"]) == 1
        assert docs_node["children"][0]["id"] == "Documents__dummy"
