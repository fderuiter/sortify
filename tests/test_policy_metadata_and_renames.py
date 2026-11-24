"""Tests for metadata policy rules (age, dates, EXIF/PDF, size), date/path templates, semantic renames, and #1182 trash/duplicate coexistence."""

from __future__ import annotations

import asyncio
import datetime
import os
import plistlib
import threading
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from PIL import Image
from pydantic import ValidationError
from pypdf import PdfWriter
from textual.widgets import Button, DataTable, Input, Label, Select, Tree

from app.config import AppSettings, Settings
from app.core.daemon import ContinuousWatchdogDaemon
from app.core.domain_contracts import SortingPlanNodeModel
from app.core.extractor_strategies import extract_embedded_date
from app.core.file_renamer import ContextExtractor, FileRenamerEngine
from app.core.metadata import MetadataPass
from app.core.pattern_formatter import PatternTokenFormatter, resolve_disk_file_date
from app.core.pipeline import SortPipeline
from app.core.policy_engine import (
    PolicyEngine,
    PolicyEvaluationResult,
    validate_metadata_expression,
)
from app.core.rule_importers import (
    HazelRuleImporter,
    RuleConflictAnalyzer,
    SortifyRuleImporter,
)
from app.core.rule_templates import export_active_rules
from app.core.session import AppSession
from app.ui.modals.rules import ActiveRuleManagerModal, RuleEditModal
from app.ui.modals.settings import SettingsModal
from app.ui.tui import AutoSorterTUI


def _write_jpeg_with_exif_date(path: Path, exif_dt: str) -> None:
    """Create a valid minimal JPEG with embedded EXIF DateTimeOriginal (tag 36867)."""
    img = Image.new("RGB", (8, 8), color=(32, 64, 128))
    exif = img.getexif()
    exif[36867] = exif_dt
    img.save(path, format="JPEG", exif=exif.tobytes())


def _write_pdf_with_creation_date(path: Path, pdf_dt: str) -> None:
    """Create a valid minimal single-page PDF with /CreationDate metadata."""
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/CreationDate": pdf_dt})
    with open(path, "wb") as f:
        writer.write(f)


def test_config_validates_metadata_policies_and_templates():
    """Verify Settings accepts valid metadata rules, {token} folders, and rename_pattern while rejecting unsafe ones."""
    valid_settings = Settings(
        RENAME_PATTERN="{date}_{category}_{stem}{extension}",
        POLICIES=[
            {
                "type": "age",
                "expression": "> 30d",
                "target_path": "Archive/{year}/{month}",
                "rename_pattern": "{date}_{stem}{extension}",
                "priority": 50,
            },
            {
                "type": "size",
                "expression": ">= 10MB",
                "target_path": "Large/{extension}",
                "priority": 40,
            },
            {
                "type": "modified_date",
                "expression": "< 2024-01-01",
                "target_path": "Historical/{year}",
                "priority": 30,
            },
            {
                "type": "created_date",
                "expression": ">= 2023-06-01",
                "target_path": "Recent/{year}/{month}",
                "priority": 20,
            },
            {
                "type": "exif_date",
                "expression": ">= 2020-01-01",
                "target_path": "Photos/{year}/{month}",
                "priority": 10,
            },
            {
                "type": "age",
                "action": "trash",
                "expression": "> 365d",
                "target_path": "",
                "priority": 5,
            },
        ],
    )
    assert len(valid_settings.POLICIES) == 6
    assert valid_settings.RENAME_PATTERN == "{date}_{category}_{stem}{extension}"

    # Invalid size/age/date expressions rejected
    with pytest.raises(ValidationError, match="Unsupported size unit"):
        Settings(
            POLICIES=[
                {
                    "type": "size",
                    "expression": "> 50parsec",
                    "target_path": "Large",
                    "priority": 10,
                }
            ]
        )

    with pytest.raises(ValidationError, match="duration|age"):
        Settings(
            POLICIES=[
                {
                    "type": "age",
                    "expression": "older than forever",
                    "target_path": "Old",
                    "priority": 10,
                }
            ]
        )

    with pytest.raises(ValidationError, match="date|duration"):
        Settings(
            POLICIES=[
                {
                    "type": "modified_date",
                    "expression": "> not-a-date",
                    "target_path": "Old",
                    "priority": 10,
                }
            ]
        )

    # Path traversal in target_path or rename_pattern rejected
    with pytest.raises(ValidationError, match="traversal"):
        Settings(
            POLICIES=[
                {
                    "type": "age",
                    "expression": "> 7d",
                    "target_path": "../Escape/{year}",
                    "priority": 10,
                }
            ]
        )

    with pytest.raises(ValidationError, match="separators|traversal"):
        Settings(RENAME_PATTERN="../bad_{original}")


def test_embedded_exif_and_pdf_dates_and_corrupt_header_resilience(tmp_path):
    """Verify offline EXIF and PDF CreationDate extraction and graceful fallback on corrupt headers."""
    photo = tmp_path / "vacation.jpg"
    _write_jpeg_with_exif_date(photo, "2022:05:14 10:20:30")
    photo_dt = extract_embedded_date(str(photo))
    assert photo_dt is not None
    assert photo_dt.strftime("%Y-%m-%d") == "2022-05-14"

    pdf_doc = tmp_path / "statement.pdf"
    _write_pdf_with_creation_date(pdf_doc, "D:20201103091500Z")
    pdf_parsed = extract_embedded_date(str(pdf_doc))
    assert pdf_parsed is not None
    assert pdf_parsed.strftime("%Y-%m-%d") == "2020-11-03"

    # Corrupt image and corrupt PDF headers must return None cleanly without raising
    corrupt_jpg = tmp_path / "corrupt.jpg"
    corrupt_jpg.write_bytes(b"\xff\xd8\xff\xe1\x00\x10NOT_VALID_EXIF_DATA")
    assert extract_embedded_date(str(corrupt_jpg)) is None

    corrupt_pdf = tmp_path / "corrupt.pdf"
    corrupt_pdf.write_bytes(b"%PDF-1.4\ngarbage binary header without xref")
    assert extract_embedded_date(str(corrupt_pdf)) is None


def test_policy_engine_evaluates_age_dates_exif_and_size(tmp_path):
    """Verify PolicyEngine evaluates single-condition age, modified_date, created_date, exif_date, and size rules."""
    now = datetime.datetime(2026, 6, 15, 12, 0, 0)

    old_file = tmp_path / "legacy_notes.txt"
    old_file.write_text("some notes", encoding="utf-8")
    old_ts = datetime.datetime(2025, 1, 10, 8, 0, 0).timestamp()
    os.utime(old_file, (old_ts, old_ts))

    big_file = tmp_path / "recording.bin"
    big_file.write_bytes(b"x" * (25 * 1024))  # 25 KB
    recent_ts = datetime.datetime(2026, 6, 14, 8, 0, 0).timestamp()
    os.utime(big_file, (recent_ts, recent_ts))

    exif_photo = tmp_path / "IMG_0042.jpg"
    _write_jpeg_with_exif_date(exif_photo, "2019:08:23 16:45:00")
    os.utime(exif_photo, (recent_ts, recent_ts))

    pdf_file = tmp_path / "scan_001.pdf"
    _write_pdf_with_creation_date(pdf_file, "D:20210409120000+00'00'")
    os.utime(pdf_file, (recent_ts, recent_ts))

    policies = [
        {
            "type": "exif_date",
            "expression": "< 2022-01-01",
            "target_path": "Media/{year}/{month}",
            "rename_pattern": "{date}_{stem}{extension}",
            "priority": 100,
        },
        {
            "type": "size",
            "expression": ">= 20KB",
            "target_path": "Big/{extension}",
            "priority": 90,
        },
        {
            "type": "age",
            "expression": "> 90d",
            "target_path": "Archive/{year}/{month}",
            "priority": 80,
        },
        {
            "type": "modified_date",
            "expression": ">= 2026-06-01",
            "target_path": "Current/{year}/{month}",
            "priority": 70,
        },
    ]

    res_photo = PolicyEngine.evaluate_policies(
        "IMG_0042.jpg", "", None, policies, abs_file_path=str(exif_photo), now=now
    )
    assert isinstance(res_photo, PolicyEvaluationResult)
    assert res_photo.target_path == "Media/{year}/{month}"
    assert res_photo.resolved_date == "2019-08-23"
    assert res_photo.rename_pattern == "{date}_{stem}{extension}"

    res_pdf = PolicyEngine.evaluate_policies(
        "scan_001.pdf", "", None, policies, abs_file_path=str(pdf_file), now=now
    )
    assert isinstance(res_pdf, PolicyEvaluationResult)
    assert res_pdf.resolved_date == "2021-04-09"

    res_big = PolicyEngine.evaluate_policies(
        "recording.bin", "", None, policies, abs_file_path=str(big_file), now=now
    )
    assert isinstance(res_big, PolicyEvaluationResult)
    assert res_big.target_path == "Big/{extension}"
    assert res_big.resolved_date == "2026-06-14"

    res_old = PolicyEngine.evaluate_policies(
        "legacy_notes.txt", "", None, policies, abs_file_path=str(old_file), now=now
    )
    assert isinstance(res_old, PolicyEvaluationResult)
    assert res_old.target_path == "Archive/{year}/{month}"
    assert res_old.resolved_date == "2025-01-10"


def test_metadata_pass_routes_before_text_extraction(tmp_path, monkeypatch):
    """Verify MetadataPass evaluates age/date/exif/size rules before text extraction so matching files skip parsing."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "meta_app_dir"))
    src_dir = tmp_path / "workspace"
    src_dir.mkdir()

    old_doc = src_dir / "old_report.txt"
    old_doc.write_text("Should never need full corpus extraction", encoding="utf-8")
    hist_ts = datetime.datetime(2020, 3, 15, 10, 0, 0).timestamp()
    os.utime(old_doc, (hist_ts, hist_ts))

    large_blob = src_dir / "bundle.dat"
    large_blob.write_bytes(b"Z" * (16 * 1024))

    exif_img = src_dir / "DSC_9911.jpg"
    _write_jpeg_with_exif_date(exif_img, "2021:12:04 09:30:00")

    settings = AppSettings(filepath=str(tmp_path / "settings.json"))
    settings.POLICIES = [
        {
            "type": "exif_date",
            "expression": "< 2023-01-01",
            "target_path": "Photos/{year}/{month}",
            "rename_pattern": "{date}_{stem}{extension}",
            "priority": 30,
        },
        {
            "type": "size",
            "expression": "> 10KB",
            "target_path": "Blobs/{extension}",
            "priority": 20,
        },
        {
            "type": "age",
            "expression": "> 365d",
            "target_path": "ColdStorage/{year}/{month}",
            "priority": 10,
        },
    ]

    session = AppSession(settings, str(src_dir))
    try:
        items = ["old_report.txt", "bundle.dat", "DSC_9911.jpg"]
        matched = MetadataPass.run(str(src_dir), items, settings, session.db)
        assert set(matched) == set(items)

        # Confirm SortPipeline.plan skips build_corpus_generator entirely when MetadataPass matched all files
        pipeline = SortPipeline(session)
        with patch("app.core.extractor.build_corpus_generator") as mock_corpus:
            plan = pipeline.plan()
            mock_corpus.assert_not_called()

        assert "ColdStorage" in plan
        assert "old_report.txt" in plan["ColdStorage"]["2020"]["03"]
        assert "Blobs" in plan
        assert "bundle.dat" in plan["Blobs"]["dat"]
        assert "Photos" in plan
        photo_node = plan["Photos"]["2021"]["12"]["DSC_9911.jpg"]
        assert photo_node["target_filename"] == "2021-12-04_DSC_9911.jpg"
        assert photo_node["file_date"] == "2021-12-04"
        # Validate SortingPlanNodeModel contract preserves file_date
        validated_node = SortingPlanNodeModel.model_validate(photo_node)
        assert validated_node.file_date == "2021-12-04"
    finally:
        session.close()


def test_placeholder_expansion_and_path_sanitization(tmp_path):
    """Verify {date}, {year}, {month}, {day}, {category}, {seq}, {extension}, {stem}, {original} and safety checks."""
    sample_file = tmp_path / "john.doe@example.com report.pdf"
    sample_file.write_text("content", encoding="utf-8")
    ts = datetime.datetime(2023, 7, 4, 15, 0, 0).timestamp()
    os.utime(sample_file, (ts, ts))

    folder = PatternTokenFormatter.format_target_path(
        "Archive/{year}/{month}/{day}/{extension}",
        metadata={
            "category": "Finance",
            "original": sample_file.name,
            "extension": ".pdf",
        },
        seq=3,
        file_path=str(sample_file),
    )
    assert folder == "Archive/2023/07/04/pdf"

    renamed = PatternTokenFormatter.format_pattern(
        "{date}_{category}_{seq} {stem}{extension}",
        metadata={
            "category": "Finance",
            "original": sample_file.name,
            "extension": ".pdf",
        },
        seq=3,
        fallback_original=sample_file.name,
        file_path=str(sample_file),
    )
    # Email PII in stem is redacted by sanitize_name, date comes from disk mtime (2023-07-04), and no double .pdf
    assert renamed.startswith("2023-07-04_Finance_03")
    assert "john.doe@example.com" not in renamed
    assert renamed.endswith("report.pdf")
    assert not renamed.endswith(".pdf.pdf")

    # Fallback to wall-clock today ONLY when disk lookup fails and no date metadata exists
    missing_path = str(tmp_path / "does_not_exist.txt")
    assert resolve_disk_file_date(missing_path) is None
    today_iso = datetime.date.today().isoformat()
    fallback_name = PatternTokenFormatter.format_pattern(
        "{date}_{stem}",
        metadata={"original": "memo.txt", "extension": ".txt"},
        seq=1,
        fallback_original="memo.txt",
        file_path=missing_path,
    )
    assert fallback_name == f"{today_iso}_memo.txt"

    # Traversal / reserved device path templates are rejected by validate_target_path
    with pytest.raises(ValueError, match="traversal"):
        PatternTokenFormatter.format_target_path(
            "../Escape/{year}", metadata={"date": "2023-07-04"}
        )
    with pytest.raises(ValueError, match="reserved"):
        PatternTokenFormatter.format_target_path(
            "CON/{year}", metadata={"date": "2023-07-04"}
        )


def test_end_to_end_cli_pipeline_and_daemon_execute_metadata_policies(
    tmp_path, monkeypatch
):
    """Verify CLI SortPipeline.run and ContinuousWatchdogDaemon move and rename files via metadata policies."""
    app_dir = tmp_path / "app_state"
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(app_dir))

    # 1. CLI SortPipeline.run execution
    cli_dir = tmp_path / "cli_inbox"
    cli_dir.mkdir()
    invoice = cli_dir / "scan09.pdf"
    _write_pdf_with_creation_date(invoice, "D:20220918100000Z")

    settings = AppSettings(filepath=str(tmp_path / "cli_settings.json"))
    settings.POLICIES = [
        {
            "type": "exif_date",
            "expression": "< 2025-01-01",
            "target_path": "Sorted/{year}/{month}",
            "rename_pattern": "{date}_archived_{stem}{extension}",
            "priority": 50,
        }
    ]

    session = AppSession(settings, str(cli_dir))
    try:
        pipeline = SortPipeline(session)
        plan, result = pipeline.run()
        assert result is not None
        assert result.ok
        dest_file = (
            cli_dir / "Sorted" / "2022" / "09" / "2022-09-18_archived_scan09.pdf"
        )
        assert dest_file.is_file()
        assert not invoice.exists()
    finally:
        session.close()

    # 2. ContinuousWatchdogDaemon._run_sorting_sync execution
    daemon_dir = tmp_path / "daemon_inbox"
    daemon_dir.mkdir()
    aged_log = daemon_dir / "server_dump.log"
    aged_log.write_text("log payload", encoding="utf-8")
    ts_2021 = datetime.datetime(2021, 5, 20, 12, 0, 0).timestamp()
    os.utime(aged_log, (ts_2021, ts_2021))

    daemon_settings_path = tmp_path / "daemon_settings.json"
    daemon_settings = AppSettings(filepath=str(daemon_settings_path))
    daemon_settings.POLICIES = [
        {
            "type": "age",
            "expression": "> 60d",
            "target_path": "Logs/{year}/{month}",
            "rename_pattern": "{date}_{stem}{extension}",
            "priority": 40,
        }
    ]
    daemon_settings.flush()

    daemon = ContinuousWatchdogDaemon(daemon_settings, str(daemon_dir))
    daemon._is_running = True
    daemon._run_sorting_sync(threading.Event())

    expected_daemon_dest = (
        daemon_dir / "Logs" / "2021" / "05" / "2021-05-20_server_dump.log"
    )
    assert expected_daemon_dest.is_file()
    assert not aged_log.exists()


def test_tui_f2_single_and_batch_rename_use_historical_disk_timestamp(tmp_path):
    """Verify F2 single and batch renames read historical disk timestamps instead of today's wall-clock date."""

    async def _run() -> None:
        src_dir = tmp_path / "tui_files"
        src_dir.mkdir()
        f1 = src_dir / "IMG_1001.jpg"
        f2 = src_dir / "IMG_1002.jpg"
        f1.write_bytes(b"img1")
        f2.write_bytes(b"img2")

        ts_1 = datetime.datetime(2021, 8, 19, 12, 0, 0).timestamp()
        ts_2 = datetime.datetime(2020, 2, 11, 12, 0, 0).timestamp()
        os.utime(f1, (ts_1, ts_1))
        os.utime(f2, (ts_2, ts_2))

        settings = AppSettings(filepath=str(tmp_path / "tui_settings.json"))
        settings.RENAME_PATTERN = "{date}_{category}_{seq}"

        app = AutoSorterTUI(settings=settings, base_dir=str(src_dir))
        app.plan = {
            "Photos": {
                "IMG_1001.jpg": {
                    "__type__": "file",
                    "relative_source": "IMG_1001.jpg",
                    "status": "pending",
                    "is_checked": False,
                },
                "IMG_1002.jpg": {
                    "__type__": "file",
                    "relative_source": "IMG_1002.jpg",
                    "status": "pending",
                    "is_checked": False,
                },
            }
        }

        async with app.run_test(size=(110, 35)) as pilot:
            await pilot.pause()
            app.rebuild_tree()
            await pilot.pause()

            tree = app.query_one("#plan-tree", Tree)
            photos_node = tree.root.children[0]
            photos_node.expand()
            await pilot.pause()
            file1_node = photos_node.children[0]

            # 1. Single-file F2 rename with {date}_{stem}
            tree.select_node(file1_node)
            await pilot.pause()
            app.action_rename_node()
            await pilot.pause()

            rename_inp = app.screen.query_one("#input-name", Input)
            rename_inp.value = "{date}_{stem}"
            await pilot.pause()

            preview_lbl = app.screen.query_one("#label-preview", Label)
            assert "2021-08-19_IMG_1001.jpg" in str(preview_lbl.render())

            app.screen.query_one("#btn-confirm", Button).press()
            await pilot.pause()
            assert "2021-08-19_IMG_1001.jpg" in app.plan["Photos"]
            assert (
                app.plan["Photos"]["2021-08-19_IMG_1001.jpg"]["target_filename"]
                == "2021-08-19_IMG_1001.jpg"
            )

            # 2. Batch F2 rename across both checked files uses each file's own disk date + RENAME_PATTERN default
            app.plan["Photos"]["2021-08-19_IMG_1001.jpg"]["is_checked"] = True
            app.plan["Photos"]["IMG_1002.jpg"]["is_checked"] = True
            app.rebuild_tree()
            await pilot.pause()
            app.action_rename_node()
            await pilot.pause()

            batch_inp = app.screen.query_one("#input-name", Input)
            assert batch_inp.value == "{date}_{category}_{seq}"
            app.screen.query_one("#btn-confirm", Button).press()
            await pilot.pause()

            assert "2021-08-19_Photos_01.jpg" in app.plan["Photos"]
            assert "2020-02-11_Photos_02.jpg" in app.plan["Photos"]

    asyncio.run(_run())


def test_semantic_keyphrase_naming_differs_from_tfidf_and_applies_date_template(
    tmp_path,
):
    """Verify AI_ASSISTED_NAMING + AI_CONSENT_GRANTED + active ONNX embeddings rank phrases semantically."""

    class FakeActiveOnnxEmbeddingManager:
        """Deterministic non-mock embedding stub where 'quarterly tax' aligns with the document vector."""

        is_mock = False
        is_degraded = False

        def is_reconstruction_active(self) -> bool:
            return False

        def generate_embedding(self, text: str) -> list[float]:
            low = (text or "").lower()
            if len(low) > 40:
                # Full document embedding points strongly along [0.99, 0.14, 0.0]
                return [0.99, 0.14, 0.0]
            if "quarterly" in low and "tax" in low:
                # 2-word semantic keyphrase matches document concept closely
                return [0.99, 0.14, 0.0]
            if "assessment" in low:
                return [0.92, 0.39, 0.0]
            if "tax" in low or "quarterly" in low:
                return [0.85, 0.52, 0.0]
            # High-frequency filler word 'item' and other words point orthogonal to document concept
            return [0.05, 0.99, 0.0]

    doc_text = (
        "Invoice Date: 2024-03-28. "
        "item item item item item quarterly tax assessment summary for corporate filing."
    )

    # Pure TF-IDF picks 'item' first because 'item' is repeated 5 times
    tfidf_terms = ContextExtractor.extract_keywords_tfidf(doc_text, max_keywords=3)
    assert tfidf_terms[0] == "item"

    # Active semantic extractor ranks ['quarterly', 'tax', 'assessment'] first by cosine similarity
    emb_mgr = FakeActiveOnnxEmbeddingManager()
    semantic_terms = ContextExtractor.extract_keyphrases_semantic(
        doc_text, emb_mgr, max_keywords=3
    )
    assert semantic_terms is not None
    assert semantic_terms[:2] == ["quarterly", "tax"]
    assert semantic_terms != tfidf_terms

    settings = Settings(
        CONTEXTUAL_RENAMING=True,
        AI_CONSENT_GRANTED=True,
        AI_ASSISTED_NAMING=True,
        RENAME_PATTERN="{date}_{stem}{extension}",
    )
    engine = FileRenamerEngine(runtime_settings=settings, embedding_manager=emb_mgr)
    generated = engine.generate_contextual_name("scan_0001.pdf", doc_text)
    assert generated.startswith("2024-03-28_quarterly_tax")
    assert generated.endswith(".pdf")
    assert isinstance(np.asarray(emb_mgr.generate_embedding(doc_text)), np.ndarray)

    # Without AI consent, engine falls back to TF-IDF while still honoring RENAME_PATTERN
    no_consent_settings = Settings(
        CONTEXTUAL_RENAMING=True,
        AI_CONSENT_GRANTED=False,
        AI_ASSISTED_NAMING=True,
        RENAME_PATTERN="{date}_{stem}{extension}",
    )
    fallback_engine = FileRenamerEngine(
        runtime_settings=no_consent_settings, embedding_manager=emb_mgr
    )
    fallback_name = fallback_engine.generate_contextual_name("scan_0001.pdf", doc_text)
    assert fallback_name.startswith("2024-03-28_item")


def test_rule_editor_settings_modal_and_hazel_sortify_roundtrip(tmp_path):
    """Verify RuleEditModal, ActiveRuleManagerModal, SettingsModal, and Sortify/Hazel import-export round-trips."""
    settings = AppSettings(filepath=str(tmp_path / "rules_ui_settings.json"))

    async def _run_ui() -> None:
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            # 1. Save RENAME_PATTERN in SettingsModal
            app.action_open_settings()
            await pilot.pause()
            assert isinstance(app.screen, SettingsModal)
            rename_tmpl_inp = app.screen.query_one("#input-rename-pattern", Input)
            rename_tmpl_inp.value = "{date}_{stem}{extension}"
            await pilot.pause()
            app.screen.query_one("#btn-save", Button).press()
            await pilot.pause()
            assert settings.RENAME_PATTERN == "{date}_{stem}{extension}"

            # 2. Open ActiveRuleManagerModal and add an 'age' policy rule with {year}/{month} and rename_pattern
            app.push_screen(ActiveRuleManagerModal(settings))
            await pilot.pause()

            app.screen.query_one("#btn-add-rule", Button).press()
            await pilot.pause()
            assert isinstance(app.screen, RuleEditModal)

            app.screen.query_one("#select-rule-type", Select).value = "age"
            app.screen.query_one("#input-rule-pattern", Input).value = "bad_expr"
            app.screen.query_one(
                "#input-rule-target", Input
            ).value = "Archive/{year}/{month}"
            app.screen.query_one("#btn-save-rule", Button).press()
            await pilot.pause()
            # Still on RuleEditModal due to validation error
            assert isinstance(app.screen, RuleEditModal)
            err_text = str(app.screen.query_one("#lbl-rule-error", Label).render())
            assert err_text.strip() != ""

            app.screen.query_one("#input-rule-pattern", Input).value = "> 30d"
            app.screen.query_one(
                "#input-rule-rename-pattern", Input
            ).value = "{date}_{stem}{extension}"
            app.screen.query_one("#btn-save-rule", Button).press()
            await pilot.pause()

            assert isinstance(app.screen, ActiveRuleManagerModal)
            table = app.screen.query_one("#rules-table", DataTable)
            assert table.row_count == 1

            app.screen.query_one("#btn-close-rules", Button).press()
            await pilot.pause()

    asyncio.run(_run_ui())

    assert len(settings.POLICIES) == 1
    saved_pol = settings.POLICIES[0]
    assert saved_pol["type"] == "age"
    assert saved_pol["expression"] == "> 30d"
    assert saved_pol["target_path"] == "Archive/{year}/{month}"
    assert saved_pol["rename_pattern"] == "{date}_{stem}{extension}"

    # Add size, modified_date, created_date, and exif_date policies and round-trip via Sortify JSON & Hazel
    settings.POLICIES = [
        saved_pol,
        {
            "type": "size",
            "expression": ">= 50MB",
            "target_path": "Large/{year}",
            "rename_pattern": "{date}_{original}",
            "priority": 20,
        },
        {
            "type": "modified_date",
            "expression": "< 2024-01-01",
            "target_path": "ModifiedArchive/{year}/{month}",
            "priority": 18,
        },
        {
            "type": "created_date",
            "expression": ">= 2025-01-01",
            "target_path": "CreatedRecent/{year}/{month}",
            "priority": 16,
        },
        {
            "type": "exif_date",
            "expression": ">= 2022-06-01",
            "target_path": "Photos/{year}/{month}",
            "rename_pattern": "{date}_{stem}{extension}",
            "priority": 14,
        },
    ]

    # 3. Sortify JSON export -> import -> conflict analyzer -> merge
    json_export_path = tmp_path / "exported_rules.json"
    export_active_rules(settings, "sortify_json", json_export_path)
    sortify_imported = SortifyRuleImporter().parse_file(json_export_path)
    assert len(sortify_imported.policies) == 5
    dst_settings_json = Settings()
    conflicts = RuleConflictAnalyzer.analyze(sortify_imported, dst_settings_json)
    assert conflicts == []
    sortify_imported.merge_into_settings(
        dst_settings_json, default_strategy="overwrite"
    )
    assert len(dst_settings_json.POLICIES) == 5
    exif_merged = next(
        p for p in dst_settings_json.POLICIES if p["type"] == "exif_date"
    )
    assert exif_merged["target_path"] == "Photos/{year}/{month}"
    assert exif_merged["rename_pattern"] == "{date}_{stem}{extension}"

    # 4. Hazel .hazelrules export -> import round-trip
    hazel_export_path = tmp_path / "exported_rules.hazelrules"
    export_active_rules(settings, "hazel", hazel_export_path)
    hazel_imported = HazelRuleImporter().parse_file(hazel_export_path)
    assert len(hazel_imported.policies) == 5
    imported_by_type = {p["type"]: p for p in hazel_imported.policies}
    assert set(imported_by_type.keys()) == {
        "age",
        "size",
        "modified_date",
        "created_date",
        "exif_date",
    }
    assert imported_by_type["age"]["target_path"] == "Archive/{year}/{month}"
    assert imported_by_type["age"]["rename_pattern"] == "{date}_{stem}{extension}"
    assert imported_by_type["exif_date"]["rename_pattern"] == "{date}_{stem}{extension}"

    # 5. Multi-condition and nested boolean Hazel rules emit clear user-facing warnings
    nested_hazel_path = tmp_path / "nested_boolean.hazelrules"
    nested_payload = {
        "Rules": [
            {
                "NSRuleName": "Nested Boolean Rule",
                "NSRuleConditionMode": "all",
                "NSRuleConditions": [
                    {
                        "NSRuleConditionMode": "any",
                        "NSRuleSubconditions": [
                            {"target": "Size", "operator": ">", "value": "10MB"},
                            {"target": "Age", "operator": ">", "value": "30d"},
                        ],
                    }
                ],
                "NSRuleActions": [{"DestinationPath": "Complex"}],
            }
        ]
    }
    with open(nested_hazel_path, "wb") as f:
        plistlib.dump(nested_payload, f)

    nested_res = HazelRuleImporter().parse_file(nested_hazel_path)
    assert nested_res.policies == []
    assert any("nested boolean" in w.lower() for w in nested_res.warnings)

    multi_cond_path = tmp_path / "multi_cond.hazelrules"
    multi_cond_payload = {
        "Rules": [
            {
                "NSRuleName": "Large Old PDFs",
                "NSRuleConditionMode": "all",
                "NSRuleConditions": [
                    {"target": "Size", "operator": ">", "value": "25MB"},
                    {"target": "Age", "operator": ">", "value": "90d"},
                ],
                "NSRuleActions": [{"DestinationPath": "CombinedArchive"}],
            }
        ]
    }
    with open(multi_cond_path, "wb") as f:
        plistlib.dump(multi_cond_payload, f)

    multi_res = HazelRuleImporter().parse_file(multi_cond_path)
    assert multi_res.policies == []
    assert any("multiple conditions" in w.lower() for w in multi_res.warnings)


def test_validate_metadata_expression_helper():
    """Verify validate_metadata_expression accepts valid expressions and rejects malformed ones."""
    assert validate_metadata_expression("size", "> 10mb") is None
    assert validate_metadata_expression("age", "older than 14 days") is None
    assert validate_metadata_expression("modified_date", "before 2024-05-01") is None

    with pytest.raises(ValueError, match="Unsupported metadata policy rule type"):
        validate_metadata_expression("unknown_type", "> 10")


def test_metadata_policies_coexist_with_trash_action_and_byte_duplicates(
    tmp_path, monkeypatch
):
    """Verify metadata rules support action='trash', round-trip via Hazel Trash, and coexist with byte-duplicate staging."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "coexist_app_state"))
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    # 1. Expired scratch file (> 365d) targeted by age + action="trash" with empty target_path
    expired_tmp = inbox / "stale_cache.txt"
    expired_tmp.write_text("stale cache content", encoding="utf-8")
    ts_2018 = datetime.datetime(2018, 4, 10, 9, 0, 0).timestamp()
    os.utime(expired_tmp, (ts_2018, ts_2018))

    # 2. Byte-identical duplicate pair (2023 mtime) targeted by modified_date + rename_pattern + DUPLICATE_ACTION="stage"
    dup_payload = b"Identical contract payload for byte-verified duplicate staging"
    primary_doc = inbox / "contract_a.txt"
    dup_copy = inbox / "contract_b.txt"
    primary_doc.write_bytes(dup_payload)
    dup_copy.write_bytes(dup_payload)
    ts_2023 = datetime.datetime(2023, 11, 5, 14, 30, 0).timestamp()
    os.utime(primary_doc, (ts_2023, ts_2023))
    os.utime(dup_copy, (ts_2023, ts_2023))

    settings = AppSettings(filepath=str(tmp_path / "coexist_settings.json"))
    settings.DUPLICATE_ACTION = "stage"
    settings.DETECT_ORIGINAL_BY = "name"
    settings.CONTEXTUAL_RENAMING = True
    settings.POLICIES = [
        {
            "type": "age",
            "action": "trash",
            "expression": "> 1800d",
            "target_path": "",
            "priority": 95,
        },
        {
            "type": "modified_date",
            "expression": "< 2025-01-01",
            "target_path": "Contracts/{year}/{month}",
            "rename_pattern": "{date}_{stem}{extension}",
            "priority": 80,
        },
    ]

    # Hazel export -> import round-trip preserves Age + NSRuleActionTrash with empty target_path
    hazel_trash_file = tmp_path / "trash_age.hazelrules"
    export_active_rules(settings, "hazel", hazel_trash_file)
    imported_hazel = HazelRuleImporter().parse_file(hazel_trash_file)
    age_trash_pol = next(p for p in imported_hazel.policies if p["type"] == "age")
    assert age_trash_pol["action"] == "trash"
    assert age_trash_pol["target_path"] == ""

    session = AppSession(settings, str(inbox))
    try:
        # MetadataPass bypasses stale_cache.txt via static age+trash match
        bypassed = MetadataPass.run(
            str(inbox),
            ["stale_cache.txt", "contract_a.txt", "contract_b.txt"],
            settings,
            session.db,
        )
        assert "stale_cache.txt" in bypassed

        pipeline = SortPipeline(session)
        plan, run_res = pipeline.run()
        assert run_res is not None
        assert run_res.ok

        # Primary copy (contract_a.txt by 'name') moved into Contracts/2023/11 and renamed with historical date
        expected_primary = (
            inbox / "Contracts" / "2023" / "11" / "2023-11-05_contract_a.txt"
        )
        assert expected_primary.is_file()
        assert expected_primary.read_bytes() == dup_payload
        assert not primary_doc.exists()
        assert not dup_copy.exists()
        assert not expired_tmp.exists()

        # Byte duplicate staged into .sortify_duplicates/<sid>/ and trashed file staged into _trash/ (neither renamed by FileRenamerEngine)
        staging_root = inbox / ".sortify_duplicates"
        assert staging_root.is_dir()
        staged_files = [p.name for p in staging_root.rglob("*.txt")]
        assert "contract_b.txt" in staged_files
        assert "stale_cache.txt" in staged_files

        # Full session undo restores all three original files cleanly
        session_id = str(run_res.summary["session_id"])
        undo_res = pipeline.undo(session_id)
        assert undo_res.ok
        assert primary_doc.is_file()
        assert dup_copy.is_file()
        assert expired_tmp.is_file()
    finally:
        session.close()


def test_rule_edit_and_settings_modals_validate_all_policy_types_and_rename_templates(
    tmp_path,
):
    """Verify RuleEditModal exposes only valid PolicyRule types, rejects illegal rename chars inline, and SettingsModal toggles AI_ASSISTED_NAMING cleanly."""
    from scripts.validate_config import EXPECTED_POLICY_TYPES

    option_values = {val for _, val in RuleEditModal.RULE_TYPE_OPTIONS}
    assert option_values == EXPECTED_POLICY_TYPES

    async def _exercise_modals() -> None:
        settings = AppSettings(filepath=str(tmp_path / "modal_edge_settings.json"))
        settings._settings_model.AI_CONSENT_GRANTED = True
        settings.SORTING_STRATEGY = "generative"
        settings.AI_ASSISTED_NAMING = True
        settings.KEYWORD_RULES = {"legacy_kw": "Legacy"}

        tui = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        async with tui.run_test(size=(100, 38)) as pilot:
            # 1. SettingsModal rejects illegal characters / path separators in RENAME_PATTERN
            # and clears AI_ASSISTED_NAMING when switching SORTING_STRATEGY away from generative
            tui.action_open_settings()
            await pilot.pause()
            s_modal = tui.screen
            assert isinstance(s_modal, SettingsModal)

            ren_input = s_modal.query_one("#input-rename-pattern", Input)
            ren_input.value = "sub/bad:name"
            s_modal.action_save()
            await pilot.pause()
            assert tui.screen is s_modal

            ren_input.value = "{date}_{stem}{extension}"
            s_modal.query_one("#select-strategy", Select).value = "default"
            s_modal.action_save()
            await pilot.pause()
            assert settings.RENAME_PATTERN == "{date}_{stem}{extension}"
            assert settings.SORTING_STRATEGY == "default"
            assert settings.AI_ASSISTED_NAMING is False

            # 2. RuleEditModal rejects illegal rename_pattern chars inline and supports
            # promoting a keyword rule to an empty-target duplicate policy
            mgr = ActiveRuleManagerModal(settings)
            tui.push_screen(mgr)
            await pilot.pause()

            mgr.action_edit_rule()
            await pilot.pause()
            edit_modal = tui.screen
            assert isinstance(edit_modal, RuleEditModal)

            edit_modal.query_one(
                "#input-rule-rename-pattern", Input
            ).value = "bad:template"
            edit_modal.action_save()
            await pilot.pause()
            assert tui.screen is edit_modal
            assert (
                "illegal"
                in str(edit_modal.query_one("#lbl-rule-error", Label).render()).lower()
            )

            edit_modal.query_one("#select-rule-type", Select).value = "duplicate"
            edit_modal.query_one("#input-rule-pattern", Input).value = ""
            edit_modal.query_one("#input-rule-target", Input).value = ""
            edit_modal.query_one("#input-rule-rename-pattern", Input).value = ""
            edit_modal.action_save()
            await pilot.pause()

            assert "legacy_kw" not in settings.KEYWORD_RULES
            dup_pol = next(p for p in settings.POLICIES if p.get("type") == "duplicate")
            assert dup_pol["expression"] == "*"
            assert dup_pol["target_path"] == ""
            assert dup_pol["action"] == "trash"

    asyncio.run(_exercise_modals())


def test_unified_policy_actions_and_manual_rescan_coexistence(tmp_path, monkeypatch):
    """Verify metadata rules, {token} folders, and rename_pattern coexist with #1187 unified actions and #1189 TUI rescan persistence."""
    monkeypatch.setenv("AUTOSORTER_APP_DIR", str(tmp_path / "app_store"))
    ws = tmp_path / "workspace"
    ws.mkdir()

    # 1. Old ledger file (> 60d) routed by age + action='archive' + {year}/{month} + rename_pattern
    old_ledger = ws / "ledger_raw.txt"
    old_ledger.write_text("Quarterly ledger statement without sensitive PII.")
    ts_2022 = datetime.datetime(2022, 5, 14, 12, 0, 0).timestamp()
    os.utime(old_ledger, (ts_2022, ts_2022))

    # 2. Large dump (> 1KB) held by size + action='quarantine' (must NOT be renamed by global RENAME_PATTERN)
    large_hold = ws / "dump_payload.txt"
    large_hold.write_text("payload " * 300)
    os.utime(large_hold, (ts_2022, ts_2022))

    settings = AppSettings(filepath=str(tmp_path / "coexist_settings.json"))
    settings._settings_model.AI_CONSENT_GRANTED = False
    settings.CONTEXTUAL_RENAMING = True
    settings.RENAME_PATTERN = "{date}_GLOBAL_{stem}{extension}"
    settings.POLICIES = [
        {
            "type": "size",
            "expression": "> 1KB",
            "target_path": "",
            "action": "quarantine",
            "priority": 95,
            "enabled": True,
        },
        {
            "type": "age",
            "expression": "> 60d",
            "target_path": "Archive/{year}/{month}",
            "rename_pattern": "{date}_ARCHIVED_{stem}{extension}",
            "action": "archive",
            "priority": 90,
            "enabled": True,
        },
    ]

    session = AppSession(settings, base_dir=str(ws), source_dirs=[str(ws)])
    try:
        pipeline = SortPipeline(session, base_dir=str(ws), settings=settings)
        plan = pipeline.plan()
        exec_res = pipeline.execute(plan)
        assert exec_res.ok

        archived_file = (
            ws / "Archive" / "2022" / "05" / "2022-05-14_ARCHIVED_ledger_raw.txt"
        )
        quarantined_file = ws / "_Compliance_Quarantine" / "dump_payload.txt"
        assert archived_file.is_file()
        assert quarantined_file.is_file()
        assert not old_ledger.exists()
        assert not large_hold.exists()

        # Verify #1187 unified audit trail recorded archive and quarantine actions
        q_records = session.db.get_quarantine_records_by_base_dir(str(ws))
        actions_recorded = {rec["policy_action"] for rec in q_records}
        assert {"archive", "quarantine"}.issubset(actions_recorded)

        # Full undo restores both files cleanly
        assert exec_res.session_id is not None
        undo_res = pipeline.undo(exec_res.session_id)
        assert undo_res.ok
        assert old_ledger.is_file()
        assert large_hold.is_file()

        # 3. Verify #1189 manual TUI F2 rename with historical {date} persists across rescan
        settings.POLICIES = []
        settings.CONTEXTUAL_RENAMING = False
        settings.RENAME_PATTERN = ""

        async def _verify_manual_rescan() -> None:
            from textual.widgets import Tree

            app = AutoSorterTUI(settings=settings, base_dir=str(ws), skip_wizard=True)
            app.app_session = session
            app.pipeline = pipeline
            async with app.run_test(size=(100, 34)) as pilot:
                app.plan = session.generate_sorting_plan()
                app.rebuild_tree()
                await pilot.pause(0.05)

                tree = app.query_one("#plan-tree", Tree)
                stack = [tree.root]
                ledger_node = None
                while stack:
                    curr = stack.pop()
                    if curr.data and curr.data.get("is_file"):
                        if "ledger_raw.txt" in str(curr.data.get("key", "")):
                            ledger_node = curr
                            break
                    stack.extend(reversed(curr.children))
                assert ledger_node is not None
                for fi in app._collect_file_nodes_from_dict(app.plan):
                    fi["is_checked"] = False
                app.active_tree_node = ledger_node
                tree.select_node(ledger_node)
                await pilot.pause(0.02)

                app.action_rename_node()
                await pilot.pause(0.05)
                ren_inp = app.screen.query_one("#input-name", Input)
                ren_inp.value = "{date}_{stem}"
                await pilot.pause(0.02)
                app.screen.query_one("#btn-confirm", Button).press()
                await pilot.pause(0.05)

                # Rescan and reapply manual edits (#1189)
                app.plan = session.generate_sorting_plan()
                app._reapply_manual_plan_edits()
                plan_dict = app.plan.plan if hasattr(app.plan, "plan") else app.plan
                all_nodes = app._collect_file_nodes_from_dict(plan_dict)
                renamed_matches = [
                    n
                    for n in all_nodes
                    if n.get("target_filename") == "2022-05-14_ledger_raw.txt"
                ]
                assert len(renamed_matches) == 1
                assert renamed_matches[0]["is_locked"] is True

        asyncio.run(_verify_manual_rescan())
    finally:
        session.close()


def test_metadata_policy_rename_collision_and_post_unarchive_template(
    tmp_path, monkeypatch
):
    """Verify colliding policy rename_pattern disambiguates cleanly (#1189) and post_unarchive_target expands {year}/{month} (#1187)."""
    import zipfile

    monkeypatch.setenv(
        "AUTOSORTER_APP_DIR", str(tmp_path / "collision_unarchive_state")
    )
    ws = tmp_path / "workspace"
    ws.mkdir()

    # Two distinct files sharing the exact same historical date targeted by a colliding rename_pattern
    r1 = ws / "scan_01.txt"
    r2 = ws / "scan_02.txt"
    r1.write_text("First receipt payload", encoding="utf-8")
    r2.write_text("Second receipt payload", encoding="utf-8")
    ts_2023 = datetime.datetime(2023, 5, 1, 10, 0, 0).timestamp()
    os.utime(r1, (ts_2023, ts_2023))
    os.utime(r2, (ts_2023, ts_2023))

    # Historical .zip archive routed by unarchive + post_unarchive_action='move' + post_unarchive_target='RawArchives/{year}/{month}'
    bundle_zip = ws / "monthly_bundle.zip"
    with zipfile.ZipFile(bundle_zip, "w") as zf:
        zf.writestr("inner_note.txt", "Extracted archive child content")
    ts_2022 = datetime.datetime(2022, 8, 19, 15, 0, 0).timestamp()
    os.utime(bundle_zip, (ts_2022, ts_2022))

    settings = AppSettings(filepath=str(tmp_path / "collision_unarchive_settings.json"))
    settings._settings_model.AI_CONSENT_GRANTED = False
    settings.CONFLICT_POLICY = "rename"
    settings.POLICIES = [
        {
            "type": "unarchive",
            "expression": "monthly_bundle",
            "target_path": "Unpacked/{year}",
            "action": "unarchive",
            "post_unarchive_action": "move",
            "post_unarchive_target": "RawArchives/{year}/{month}",
            "priority": 95,
            "enabled": True,
        },
        {
            "type": "modified_date",
            "expression": "< 2024-01-01",
            "target_path": "Receipts/{year}/{month}",
            "rename_pattern": "{date}_receipt{extension}",
            "priority": 80,
            "enabled": True,
        },
    ]

    session = AppSession(settings, base_dir=str(ws), source_dirs=[str(ws)])
    try:
        pipeline = SortPipeline(session, base_dir=str(ws), settings=settings)
        plan = pipeline.plan()
        res = pipeline.execute(plan)
        assert res.ok

        receipt_dir = ws / "Receipts" / "2023" / "05"
        assert receipt_dir.is_dir()
        receipt_files = sorted(p.name for p in receipt_dir.glob("*.txt"))
        assert len(receipt_files) == 2
        assert "2023-05-01_receipt.txt" in receipt_files
        assert {p.read_text(encoding="utf-8") for p in receipt_dir.glob("*.txt")} == {
            "First receipt payload",
            "Second receipt payload",
        }

        relocated_zip = ws / "RawArchives" / "2022" / "08" / "monthly_bundle.zip"
        assert relocated_zip.is_file()
        assert not bundle_zip.exists()

        assert res.session_id is not None
        undo_res = pipeline.undo(res.session_id)
        assert undo_res.ok
        assert r1.is_file()
        assert r2.is_file()
        assert bundle_zip.is_file()
    finally:
        session.close()
