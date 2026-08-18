"""Tests for responsive truncation with interactive hover tooltips across tree view, study cards, policy labels, and modal dialogs."""

from unittest.mock import MagicMock
from nicegui import Client, ui

from app.config import AppSettings
from app.ui.app import AutoSorterApp
from app.ui.cro_forensic_view import CROForensicView
from app.ui.dialog_helper import (
    get_dialog_card_classes,
    STANDARD_DIALOG_CARD_MD,
    STANDARD_DIALOG_CARD_LG,
    STANDARD_DIALOG_CARD_XL,
)
from app.ui.settings import show_settings
from app.core.cro_multi_study_pipeline import MasterPipelineResult, StudyIngestSummary


def test_tree_header_responsive_truncation_and_tooltips():
    """Verify that tree view nodes include truncation, flex-shrink guardrails on action buttons, and tooltip template."""
    settings = AppSettings()
    with Client(None):
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

        # Check tree nodes structure
        assert len(app.tree_nodes) == 1
        folder_node = app.tree_nodes[0]
        assert folder_node["filepath"] == "LongFolderName_1234567890_Very_Deep_Directory"
        assert folder_node["text"] == "LongFolderName_1234567890_Very_Deep_Directory"

        file_node = folder_node["children"][0]
        assert file_node["filepath"] == "LongFolderName_1234567890_Very_Deep_Directory/Very_Long_File_Path_Document_Name_2026_Clinical_Trial_Data_Report.pdf"
        assert file_node["text"] == "Very_Long_File_Path_Document_Name_2026_Clinical_Trial_Data_Report.pdf [Proposed]"

        # Build app UI and inspect tree header slot template
        app.build_ui()
        assert hasattr(app, "tree_view")
        header_slot = app.tree_view.slots.get("default-header")
        assert header_slot is not None

        template = header_slot.template
        # Check truncation and flex-shrink guardrails on tree headers
        assert "truncate" in template
        assert "min-w-0" in template
        assert "shrink-0" in template
        assert "q-tooltip" in template
        assert "prop.node.filepath || prop.node.text" in template


def test_study_card_responsive_header_and_tooltips():
    """Verify that CRO Forensic study cards truncate long study IDs and retain status badges and buttons inside card borders."""
    settings = AppSettings()
    view = CROForensicView(settings)

    long_study_id = "STUDY-2026-CLINICAL-TRIAL-PHASE-3-MULTI-CENTER-ONCOLOGY-PROTOCOL-99281-XYZ-EXTREMELY-LONG-IDENTIFIER"
    mock_study = StudyIngestSummary(
        study_id=long_study_id,
        total_documents=150,
        compliance_score_percent=95.0,
        audit_readiness_status="REGULATORY_READY",
        missing_essential_count=0,
        found_essential_count=10,
        target_directory="/tmp/study",
        audit_report_html_path="/tmp/audit.html",
    )
    mock_result = MasterPipelineResult(
        source_root="/tmp/source",
        target_root="/tmp/target",
        timestamp_utc="2026-08-18T12:00:00Z",
        total_scanned_files=150,
        total_unique_documents=150,
        total_duplicates_detected=0,
        discovered_studies_count=1,
        studies_summary=[mock_study],
    )

    with Client(None):
        view.show_dialog()
        view._render_results(mock_result)
        assert view.results_container is not None


def test_settings_policy_and_rule_truncation_tooltips():
    """Verify that settings view policy labels, keyword rules, and protected paths have tooltips and truncation classes."""
    settings = AppSettings()
    settings.POLICIES = [
        {
            "type": "keyword",
            "expression": "VERY_LONG_POLICY_EXPRESSION_SEARCH_TERM_2026_LABEL",
            "target_path": "Deeply/Nested/Target/Path/For/Policy/Rule/Destination/Folder",
            "priority": 10,
        }
    ]
    settings.KEYWORD_RULES = {
        "EXCESSIVELY_LONG_KEYWORD_STRING_FOR_ROUTING": "Destination/Directory/For/Keyword/Match/Files"
    }
    settings.PROTECTED_PATHS = [
        "/var/app/data/protected/very/long/protected/directory/path/here"
    ]

    with Client(None):
        app = AutoSorterApp(settings)
        show_settings(app, settings)


def test_dialog_container_layout_bounds_and_no_horizontal_scroll():
    """Verify that standard dialog card classes enforce max width/height bounds and disable horizontal scrollbars."""
    md_classes = get_dialog_card_classes("md")
    lg_classes = get_dialog_card_classes("lg")
    xl_classes = get_dialog_card_classes("xl")

    for cls in (md_classes, lg_classes, xl_classes):
        assert "overflow-x-hidden" in cls
        assert "max-h-[90vh]" in cls
        assert "w-full" in cls

    assert "max-w-md" in STANDARD_DIALOG_CARD_MD
    assert "max-w-lg" in STANDARD_DIALOG_CARD_LG
    assert "max-w-4xl" in STANDARD_DIALOG_CARD_XL
