"""Unit test suite for SimulationExporter engine."""

import json
import os
import time
from pathlib import Path

import pytest

from app.core.simulation_exporter import SimulationExporter


def _is_ci_or_parallel() -> bool:
    return (
        "PYTEST_XDIST_WORKER" in os.environ
        or "CI" in os.environ
        or os.environ.get("GITHUB_ACTIONS") == "true"
    )


@pytest.fixture
def sample_plan_with_data(tmp_path):
    f1 = tmp_path / "financial_doc.pdf"
    f1.write_text("Financial audit document content.")
    f2 = tmp_path / "secret_key.txt"
    f2.write_text("Secret API key sk_live_12345678901234567890123456.")

    plan = {
        "Finance": {
            "financial_doc.pdf": {
                "__type__": "file",
                "filepath": str(f1),
                "relative_source": "financial_doc.pdf",
                "target_filename": "financial_doc.pdf",
                "sensitivity_rating": "HIGH",
                "sensitivity_score": 0.85,
                "confirmed": True,
            },
            "secret_key.txt": {
                "__type__": "file",
                "filepath": str(f2),
                "relative_source": "secret_key.txt",
                "target_filename": "secret_key.txt",
                "sensitivity_rating": "CRITICAL",
                "sensitivity_score": 0.95,
                "confirmed": False,
            },
        }
    }
    return plan, str(tmp_path)


def test_export_json_structure(sample_plan_with_data, tmp_path):
    plan, base_dir = sample_plan_with_data
    out_json = tmp_path / "report.json"

    exporter = SimulationExporter(plan, base_dir=base_dir)
    json_str = exporter.export_json(out_json)

    assert out_json.exists()
    parsed = json.loads(json_str)

    assert parsed["title"] == "Sortify Dry-Run Simulation Report"
    assert "generated_at" in parsed
    assert "summary" in parsed
    assert parsed["summary"]["total_files"] == 2
    assert "move_mappings" in parsed
    assert len(parsed["move_mappings"]) == 2
    assert "plan_hierarchy" in parsed


def test_export_html_structure(sample_plan_with_data, tmp_path):
    plan, base_dir = sample_plan_with_data
    out_html = tmp_path / "report.html"

    exporter = SimulationExporter(plan, base_dir=base_dir)
    html_str = exporter.export_html(out_html)

    assert out_html.exists()
    assert "<!DOCTYPE html>" in html_str
    assert "Sortify Dry-Run Simulation Report" in html_str
    assert "metrics-grid" in html_str
    assert "mappingsTable" in html_str
    assert "searchInput" in html_str
    assert "filterSelect" in html_str
    # Zero external CDN resources guardrail
    assert "http://" not in html_str
    assert "https://" not in html_str


def test_scrubbing_user_home_and_credentials(tmp_path):
    home_dir = str(Path.home())
    home_file = os.path.join(home_dir, "sensitive_folder", "sk_live_999999999999999999999999.txt")

    plan = {
        "Docs": {
            "sk_live_999999999999999999999999.txt": {
                "__type__": "file",
                "filepath": home_file,
                "relative_source": "sk_live_999999999999999999999999.txt",
                "target_filename": "sk_live_999999999999999999999999.txt",
                "sensitivity_rating": "HIGH",
            }
        }
    }

    exporter = SimulationExporter(plan, base_dir=home_dir)
    json_str = exporter.export_json()
    html_str = exporter.export_html()

    # User home path should be scrubbed
    assert home_dir not in json_str
    assert home_dir not in html_str
    # Sensitive credential sk_live_ should be scrubbed
    assert "sk_live_999999999999999999999999" not in json_str
    assert "sk_live_999999999999999999999999" not in html_str


def test_read_only_guardrail(sample_plan_with_data, tmp_path):
    plan, base_dir = sample_plan_with_data

    # Count files in base_dir before export
    initial_files = set(os.listdir(base_dir))

    exporter = SimulationExporter(plan, base_dir=base_dir)
    exporter.export_json()
    exporter.export_html()

    # Verify target directory remains untouched
    final_files = set(os.listdir(base_dir))
    assert initial_files == final_files


def test_export_performance_large_plan(tmp_path):
    large_plan = {}
    folder = large_plan.setdefault("Archive", {})
    for i in range(10000):
        folder[f"file_{i}.txt"] = {
            "__type__": "file",
            "relative_source": f"file_{i}.txt",
            "target_filename": f"file_{i}.txt",
            "sensitivity_rating": "LOW",
        }

    exporter = SimulationExporter(large_plan, base_dir=str(tmp_path))

    start_time = time.perf_counter()
    json_content = exporter.export_json()
    html_content = exporter.export_html()
    duration = time.perf_counter() - start_time

    assert len(json_content) > 0
    assert len(html_content) > 0
    sla_threshold = 12.0 if _is_ci_or_parallel() else 2.0
    assert duration < sla_threshold


def test_cli_sort_export_report_dry_run(tmp_path, capsys):
    doc = tmp_path / "test.txt"
    doc.write_text("Sample file content for sorting.")

    out_html = tmp_path / "cli_report.html"

    from app.config import AppSettings
    from app.main import build_parser, handle_sort_command

    parser = build_parser()
    args = parser.parse_args([
        "sort",
        str(tmp_path),
        "--dry-run",
        "--export-report",
        str(out_html),
    ])

    settings = AppSettings()
    with pytest.raises(SystemExit) as exc_info:
        handle_sort_command(args, settings)

    assert exc_info.value.code == 0
    assert out_html.exists()
    assert "<!DOCTYPE html>" in out_html.read_text(encoding="utf-8")
    assert doc.exists()  # Dry run preserved original physical file


def test_cli_scan_export_report_json(tmp_path, capsys):
    doc = tmp_path / "doc.pdf"
    doc.write_text("PDF content sample.")

    out_json = tmp_path / "scan_report.json"

    from app.config import AppSettings
    from app.main import build_parser, handle_scan_command

    parser = build_parser()
    args = parser.parse_args([
        "scan",
        str(tmp_path),
        "--export-report",
        str(out_json),
        "--report-format",
        "json",
    ])

    settings = AppSettings()
    with pytest.raises(SystemExit) as exc_info:
        handle_scan_command(args, settings)

    assert exc_info.value.code == 0
    assert out_json.exists()
    parsed = json.loads(out_json.read_text(encoding="utf-8"))
    assert parsed["summary"]["total_files"] >= 1


def test_tui_export_report_modal(tmp_path):
    import asyncio

    from app.config import AppSettings
    from app.ui.tui import AutoSorterTUI

    async def _test():
        settings = AppSettings()
        settings._settings_model.AI_CONSENT_GRANTED = True
        app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
        app.plan = {
            "Docs": {
                "test.txt": {
                    "__type__": "file",
                    "relative_source": "test.txt",
                    "target_filename": "test.txt",
                    "sensitivity_rating": "LOW",
                }
            }
        }

        async with app.run_test() as pilot:
            # Trigger export report modal via action
            app.action_export_simulation_report()
            await pilot.pause()

            # Submit export in modal
            await pilot.press("enter")
            await pilot.pause()

            default_out = tmp_path / "simulation_report.html"
            assert default_out.exists()
            assert "<!DOCTYPE html>" in default_out.read_text(encoding="utf-8")

    asyncio.run(_test())

