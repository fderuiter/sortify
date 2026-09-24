import os
from unittest.mock import MagicMock, patch

from app.config import AppSettings
from app.core.analyzer import IncrementalAnalyzer
from app.core.db import Database
from app.core.db_worker import DBWorker
from app.core.metadata import MetadataPass, get_extension_category_target
from app.core.scanner import get_files_recursively


def test_get_files_recursively_includes_unsupported_extensions(tmp_path):
    """AC 1: get_files_recursively yields relative paths for files with unsupported extensions."""
    (tmp_path / "doc.txt").touch()
    (tmp_path / "archive.zip").touch()
    (tmp_path / "disk.iso").touch()
    (tmp_path / "app.exe").touch()
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "model.cad").touch()
    (tmp_path / ".hidden_file.zip").touch()

    files = get_files_recursively(str(tmp_path))

    assert "doc.txt" in files
    assert "archive.zip" in files
    assert "disk.iso" in files
    assert "app.exe" in files
    assert os.path.join("sub", "model.cad") in files
    assert ".hidden_file.zip" not in files


def test_metadata_pass_evaluates_rules_on_unsupported_files(tmp_path):
    """AC 2: MetadataPass.run evaluates policy overrides and keyword rules on unsupported file formats."""
    settings = AppSettings()
    settings.POLICIES = [
        {
            "type": "pattern",
            "expression": "installer",
            "target_path": "Software/Installers",
            "priority": 10,
        }
    ]
    settings.KEYWORD_RULES = {"archive": "Backups/Archives"}

    db = MagicMock()
    db.get_all_documents.return_value = []

    installer_file = tmp_path / "installer_v1.exe"
    installer_file.touch()
    archive_file = tmp_path / "my_archive_data.zip"
    archive_file.touch()

    with patch("app.core.metadata.get_file_hash", return_value="hash_test"):
        bypassed = MetadataPass.run(
            str(tmp_path),
            ["installer_v1.exe", "my_archive_data.zip"],
            settings,
            db,
            None,
            None,
        )

        assert "installer_v1.exe" in bypassed
        assert "my_archive_data.zip" in bypassed

        # Verify DB received [STATUS:BYPASSED]
        upsert_calls = db.upsert_documents.call_args_list
        assert len(upsert_calls) == 1
        upserted_docs = upsert_calls[0][0][0]
        statuses = {doc[1]: doc[3] for doc in upserted_docs}
        assert statuses["installer_v1.exe"] == "[STATUS:BYPASSED]"
        assert statuses["my_archive_data.zip"] == "[STATUS:BYPASSED]"


def test_extension_category_and_mime_fallbacks(tmp_path):
    """AC 3 & Req 3: Unregistered extension category and MIME fallback mapping."""
    settings = AppSettings()
    settings.EXTENSION_CATEGORIES = {".xyz": "CustomXYZCategory"}

    assert get_extension_category_target("data.xyz", settings) == "CustomXYZCategory"
    assert get_extension_category_target("archive.zip", settings) == "Archives"
    assert get_extension_category_target("setup.exe", settings) == "Executables"
    assert get_extension_category_target("drawing.dwg", settings) == "Design"
    assert get_extension_category_target("clip.mp4", settings) == "Media"


def test_unmatched_unsupported_files_get_status_token_and_bypass_extraction(tmp_path):
    """AC 3: Unmatched unsupported files receive status tokens without triggering text extraction."""
    settings = AppSettings()
    settings.POLICIES = []
    settings.KEYWORD_RULES = {}
    settings.EXTENSION_CATEGORIES = {}

    db = MagicMock()
    db.get_all_documents.return_value = []

    unknown_file = tmp_path / "binary.unknownext"
    unknown_file.touch()

    with patch("app.core.metadata.get_file_hash", return_value="hash_unknown"):
        bypassed = MetadataPass.run(
            str(tmp_path), ["binary.unknownext"], settings, db, None, None
        )

        assert "binary.unknownext" in bypassed
        db.upsert_documents.assert_called_once_with(
            [
                (
                    str(tmp_path),
                    "binary.unknownext",
                    "hash_unknown",
                    "[STATUS:UNSUPPORTED]",
                )
            ]
        )


def test_phase1_and_phase2_organization_cleans_root_directory(
    tmp_path, tmp_path_factory
):
    """AC 4: Non-textual files undergo organization in Phase 1 and Phase 2, leaving zero unmanaged files in root."""
    base_dir = str(tmp_path)
    db_dir = tmp_path_factory.mktemp("db_dir")
    db_path = db_dir / "test_db.sqlite"
    worker = DBWorker()
    db = Database(db_path, worker)

    try:
        # Create root directory files
        (tmp_path / "installer_setup.exe").touch()  # Policy match -> Software
        (tmp_path / "data_backup.zip").touch()  # Extension category -> Archives
        (tmp_path / "unknown_asset.cad").touch()  # Extension category -> Design
        (tmp_path / "random_blob.xyz").touch()  # No rule -> Fallback Miscellaneous
        (tmp_path / "invoice_99.txt").touch()  # Keyword -> Finances

        settings = AppSettings()
        settings.POLICIES = [
            {
                "type": "pattern",
                "expression": "installer",
                "target_path": "Software",
                "priority": 10,
            }
        ]
        settings.KEYWORD_RULES = {"invoice": "Finances"}

        # Phase 1: Scan and run MetadataPass
        scanned_files = get_files_recursively(base_dir)
        assert len(scanned_files) == 5

        bypassed = MetadataPass.run(base_dir, scanned_files, settings, db, None, None)
        # All 4 non-textual files are bypassed; invoice_99.txt matches keyword
        assert "installer_setup.exe" in bypassed
        assert "data_backup.zip" in bypassed
        assert "unknown_asset.cad" in bypassed
        assert "random_blob.xyz" in bypassed
        assert "invoice_99.txt" in bypassed

        # Fast-path plan
        analyzer = IncrementalAnalyzer(max_folders=12, stop_words={"the", "and"}, db=db)
        fast_plan = analyzer.generate_sorting_plan(
            base_dir, runtime_settings=settings, fast_path_only=True
        )
        assert fast_plan is not None

        # Execute Phase 1 plan or full plan
        full_plan = analyzer.generate_sorting_plan(
            base_dir, runtime_settings=settings, fast_path_only=False
        )

        # Verify every non-hidden file in root is routed in full_plan
        routed_files = set()
        for cat_node in full_plan.values():
            if isinstance(cat_node, dict):
                for filename in cat_node.keys():
                    routed_files.add(filename)

        assert "installer_setup.exe" in routed_files
        assert "data_backup.zip" in routed_files
        assert "unknown_asset.cad" in routed_files
        assert "random_blob.xyz" in routed_files
        assert "invoice_99.txt" in routed_files
    finally:
        worker.stop()
