"""Unit tests for audit reporter and post-sort verification export functionality."""

import csv
import json
import os

import pytest

from app.core.audit_reporter import (
    MISMATCH_FAIL,
    SKIPPED,
    UNVERIFIED,
    VERIFIED_PASS,
    export_audit_report,
    export_audit_report_csv,
    export_audit_report_json,
    generate_audit_report,
)
from app.core.exceptions import AuditExportError


def test_generate_audit_report_metrics_and_statuses():
    """Test generating audit report with pass, fail, and skipped records."""
    records = [
        {
            "source_path": "/tmp/a.txt",
            "destination_path": "/tmp/sorted/a.txt",
            "pre_hash": "a1b2c3d4e5f6",
            "post_hash": "a1b2c3d4e5f6",
        },
        {
            "source_path": "/tmp/b.txt",
            "destination_path": "/tmp/sorted/b.txt",
            "pre_hash": "111111111111",
            "post_hash": "222222222222",
        },
        {
            "source_path": "/tmp/c.txt",
            "destination_path": "/tmp/c.txt",
            "pre_hash": "333333333333",
            "post_hash": "333333333333",
            "skipped": True,
        },
    ]

    report = generate_audit_report("/tmp", records)

    assert report["base_dir"] == os.path.normpath("/tmp")
    assert report["total_files"] == 3
    assert report["verified_pass_count"] == 1
    assert report["mismatch_fail_count"] == 1
    assert report["skipped_count"] == 1
    assert report["unverified_count"] == 0

    recs = report["records"]
    assert recs[0]["status"] == VERIFIED_PASS
    assert recs[0]["status_badge"] == "[PASS]"

    assert recs[1]["status"] == MISMATCH_FAIL
    assert recs[1]["status_badge"] == "[FAIL]"

    assert recs[2]["status"] == SKIPPED
    assert recs[2]["status_badge"] == "[SKIPPED]"


def test_generate_audit_report_unverified_status():
    """Test generating audit report with missing pre-sort hash yielding UNVERIFIED status."""
    records = [
        {
            "source_path": "/tmp/unverified1.txt",
            "destination_path": "/tmp/sorted/unverified1.txt",
            "pre_hash": "",
            "post_hash": "posthash123456",
        },
        {
            "source_path": "/tmp/unverified2.txt",
            "destination_path": "/tmp/sorted/unverified2.txt",
            "pre_hash": None,
            "post_hash": "posthash789012",
        },
        {
            "source_path": "/tmp/override.txt",
            "destination_path": "/tmp/sorted/override.txt",
            "pre_hash": "hashA",
            "post_hash": "hashA",
            "status": UNVERIFIED,
        },
    ]

    report = generate_audit_report("/tmp", records)

    assert report["total_files"] == 3
    assert report["verified_pass_count"] == 0
    assert report["mismatch_fail_count"] == 0
    assert report["skipped_count"] == 0
    assert report["unverified_count"] == 3

    recs = report["records"]
    assert recs[0]["status"] == UNVERIFIED
    assert recs[0]["status_badge"] == "[UNVERIFIED]"

    assert recs[1]["status"] == UNVERIFIED
    assert recs[1]["status_badge"] == "[UNVERIFIED]"

    assert recs[2]["status"] == UNVERIFIED
    assert recs[2]["status_badge"] == "[UNVERIFIED]"


def test_export_audit_report_json(tmp_path):
    """Test exporting audit report to JSON format."""
    records = [
        {
            "source_path": str(tmp_path / "file1.txt"),
            "destination_path": str(tmp_path / "out" / "file1.txt"),
            "pre_hash": "hash123",
            "post_hash": "hash123",
        }
    ]
    report = generate_audit_report(str(tmp_path), records)
    out_file = str(tmp_path / "audit_report.json")

    res = export_audit_report_json(report, out_file)
    assert os.path.exists(res)

    with open(res, "r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["total_files"] == 1
    assert data["verified_pass_count"] == 1
    assert data["records"][0]["filename"] == "file1.txt"


def test_export_audit_report_csv(tmp_path):
    """Test exporting audit report to CSV format."""
    records = [
        {
            "source_path": str(tmp_path / "file2.txt"),
            "destination_path": str(tmp_path / "out" / "file2.txt"),
            "pre_hash": "hash456",
            "post_hash": "hash456",
        }
    ]
    report = generate_audit_report(str(tmp_path), records)
    out_file = str(tmp_path / "audit_report.csv")

    res = export_audit_report_csv(report, out_file)
    assert os.path.exists(res)

    with open(res, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows = list(reader)

    assert len(rows) == 2
    assert "Filename" in rows[0]
    assert rows[1][0] == "file2.txt"
    assert rows[1][1] == VERIFIED_PASS


def test_export_audit_report_dispatch(tmp_path):
    """Test export_audit_report generic dispatcher for json and csv."""
    records = [
        {
            "source_path": str(tmp_path / "test.txt"),
            "destination_path": str(tmp_path / "out" / "test.txt"),
            "pre_hash": "abc",
            "post_hash": "abc",
        }
    ]
    report = generate_audit_report(str(tmp_path), records)

    json_path = export_audit_report(
        report, str(tmp_path / "r.json"), format_type="json"
    )
    assert json_path.endswith(".json") and os.path.exists(json_path)

    csv_path = export_audit_report(report, str(tmp_path / "r.csv"), format_type="csv")
    assert csv_path.endswith(".csv") and os.path.exists(csv_path)


def test_export_audit_report_permission_error():
    """Test permission error during export raises AuditExportError."""
    report = generate_audit_report("/invalid", [])
    # Invalid directory path on unix / nonexistent root permission
    invalid_path = (
        "/proc/nonexistent_dir/report.json"
        if os.name != "nt"
        else "Z:\\nonexistent_drive\\report.json"
    )

    with pytest.raises(AuditExportError):
        export_audit_report_json(report, invalid_path)
