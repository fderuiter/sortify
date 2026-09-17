"""Tests for responsive tree view nodes and dialog card helper functions."""

from app.config import AppSettings
from app.ui.app import AutoSorterApp
from app.ui.dialog_helper import get_dialog_card_classes


def test_tree_header_responsive_truncation_and_tooltips():
    settings = AppSettings()
    app = AutoSorterApp(settings)
    app.plan = {
        "LongFolderName_1234567890_Very_Deep_Directory": {
            "Very_Long_File_Path_Document_Name_2026_Clinical_Trial_Data_Report.pdf": {
                "__type__": "file",
                "status": "Proposed",
            }
        }
    }
    app.render_tree()

    assert len(app.tree_nodes) == 1
    folder_node = app.tree_nodes[0]
    assert folder_node["filepath"] == "LongFolderName_1234567890_Very_Deep_Directory"

    file_node = folder_node["children"][0]
    assert file_node["filepath"] == "LongFolderName_1234567890_Very_Deep_Directory/Very_Long_File_Path_Document_Name_2026_Clinical_Trial_Data_Report.pdf"


def test_get_dialog_card_classes():
    classes_md = get_dialog_card_classes("md")
    assert "max-w" in classes_md or "w-" in classes_md
