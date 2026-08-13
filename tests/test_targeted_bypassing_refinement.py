from app.core.analyzer import IncrementalAnalyzer
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.metadata import MetadataPass


def test_targeted_bypassing_refinement_acceptance_criteria(tmp_path):
    # Setup test DB and components
    db_worker = DBWorker()
    try:
        db_file = tmp_path / "test_targeted_bypassing.db"
        db = Database(db_file, db_worker)

        def sync_db():
            db.worker.execute_write(lambda: None)

        # 1. Create temporary files to scan
        base_dir = tmp_path / "scandir"
        base_dir.mkdir()

        comp_file_name = "compliance_invoice_123.txt"
        comp_file = base_dir / comp_file_name
        comp_file.write_text("Confidential company transaction details.")

        normal_file_name = "regular_document.txt"
        normal_file = base_dir / normal_file_name
        normal_file.write_text("Hello world normal document.")

        # Define initial active policies / rules
        class MockSettings:
            KEYWORD_RULES = {}
            LEARNED_RULES = {}
            POLICIES = [
                {
                    "type": "pattern",
                    "expression": "compliance_invoice",
                    "target_path": "Old Archive",
                    "priority": 10,
                }
            ]
            MAX_DEPTH = 5
            MAX_FEATURES = 3
            PRESERVE_HIERARCHY = False
            CONTEXTUAL_RENAMING = False

        # Execute Pre-Evaluation Metadata Pass
        bypassed = MetadataPass.run(
            str(base_dir),
            [comp_file_name, normal_file_name],
            MockSettings,
            db,
            None,
            None,
        )
        sync_db()

        # Verify Acceptance Criterion 1:
        # "Scanned files that match automated rules bypass text extraction but write a null value to the user-verified database column."
        assert comp_file_name in bypassed
        assert normal_file_name not in bypassed

        # Check what is written in the database
        docs = db.get_all_documents(str(base_dir))

        comp_doc = next(d for d in docs if d[0] == comp_file_name)

        # comp_doc should have extracted_text as '[STATUS:BYPASSED]'
        assert comp_doc[1] == "[STATUS:BYPASSED]"
        # comp_doc user_verified_target_path (4th element d[3]) must be null/None
        assert comp_doc[3] is None or comp_doc[3] == ""

        # Initialize Analyzer
        analyzer = IncrementalAnalyzer(
            max_folders=3, stop_words={"the", "and"}, db=db, model_path=None
        )

        # Run analyzer with old policy
        plan = analyzer.generate_sorting_plan(
            str(base_dir), runtime_settings=MockSettings()
        )

        # Verify file routes to Old Archive
        assert "Old Archive" in plan
        assert comp_file_name in plan["Old Archive"]

        # 2. Verify Acceptance Criterion 2:
        # "Changing a compliance rule dynamically redirects matched files to the updated path during the next scan"
        # Update active policy target path
        class MockSettingsNew:
            KEYWORD_RULES = {}
            LEARNED_RULES = {}
            POLICIES = [
                {
                    "type": "pattern",
                    "expression": "compliance_invoice",
                    "target_path": "New Archive",
                    "priority": 10,
                }
            ]
            MAX_DEPTH = 5
            MAX_FEATURES = 3
            PRESERVE_HIERARCHY = False
            CONTEXTUAL_RENAMING = False

        # Re-run Metadata Pass (next scan)
        bypassed_new = MetadataPass.run(
            str(base_dir),
            [comp_file_name, normal_file_name],
            MockSettingsNew,
            db,
            None,
            None,
        )
        sync_db()
        assert comp_file_name in bypassed_new

        # Re-run analyzer with new settings
        plan_new = analyzer.generate_sorting_plan(
            str(base_dir), runtime_settings=MockSettingsNew()
        )

        # The file must immediately route to "New Archive" without any stale target freezing
        assert "New Archive" in plan_new
        assert comp_file_name in plan_new["New Archive"]
        assert "Old Archive" not in plan_new or comp_file_name not in plan_new.get(
            "Old Archive", {}
        )

        # 3. Verify Acceptance Criterion 3:
        # "Drag-and-drop manual re-routing successfully records the destination folder in the user-verified database column."
        db.set_user_verified_target_path(
            str(base_dir), comp_file_name, "User Dragged Folder"
        )
        sync_db()

        # Re-fetch from database and check user-verified target path
        docs_after_drag = db.get_all_documents(str(base_dir))
        comp_doc_after_drag = next(d for d in docs_after_drag if d[0] == comp_file_name)
        assert comp_doc_after_drag[3] == "User Dragged Folder"

        # Under the PolicyEngine architecture, compliance policies take absolute precedence,
        # overriding manual overrides and routing to the compliance path while raising a conflict.
        plan_after_drag = analyzer.generate_sorting_plan(
            str(base_dir), runtime_settings=MockSettingsNew()
        )
        assert "New Archive" in plan_after_drag
        assert comp_file_name in plan_after_drag["New Archive"]
        assert (
            plan_after_drag["New Archive"][comp_file_name].get("is_conflicted") is True
        )
        assert (
            plan_after_drag["New Archive"][comp_file_name].get("is_corrected") is True
        )
        assert (
            plan_after_drag["New Archive"][comp_file_name].get("original_lock_path")
            == "User Dragged Folder"
        )

        # 4. Verify Acceptance Criterion 4:
        # "Bypassed files are successfully ignored during AI clustering and semantic vector generation."
        # If we inspect generate_sorting_plan logic or db vectors:
        # Non-bypassed documents can get vectors, but '[STATUS:BYPASSED]' starts with '[STATUS:' which is explicitly ignored
        # during semantic embedding retrieval or tfidf eligibility.
        assert db._is_tfidf_eligible(comp_file_name, "[STATUS:BYPASSED]") is False

        # 5. Verify Acceptance Criterion 5:
        # "Scanned files with null user-verified paths and no matching rules route safely to default folders without raising errors."
        class MockSettingsNoRules:
            KEYWORD_RULES = {}
            LEARNED_RULES = {}
            POLICIES = []
            MAX_DEPTH = 5
            MAX_FEATURES = 3
            PRESERVE_HIERARCHY = False
            CONTEXTUAL_RENAMING = False

        # Let's clear the user verified path for compliance file
        db.set_user_verified_target_path(str(base_dir), comp_file_name, None)
        sync_db()

        # Let's run metadata pass and analyzer with no rules
        bypassed_none = MetadataPass.run(
            str(base_dir),
            [comp_file_name, normal_file_name],
            MockSettingsNoRules,
            db,
            None,
            None,
        )
        sync_db()
        plan_no_rules = analyzer.generate_sorting_plan(
            str(base_dir), runtime_settings=MockSettingsNoRules()
        )

        # Since they have null user-verified paths and match no rules, they should end up safely in "Miscellaneous"
        assert "Miscellaneous" in plan_no_rules
        assert comp_file_name in plan_no_rules["Miscellaneous"]

    finally:
        db_worker.stop()
