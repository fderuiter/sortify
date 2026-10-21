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
    """Test generating audit report with pass, fail, skipped, and unverified records."""
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
        {
            "source_path": "/tmp/d.txt",
            "destination_path": "/tmp/sorted/d.txt",
            "pre_hash": None,
            "post_hash": "444444444444",
        },
    ]

    report = generate_audit_report("/tmp", records)

    assert report["base_dir"] == os.path.normpath("/tmp")
    assert report["total_files"] == 4
    assert report["verified_pass_count"] == 1
    assert report["mismatch_fail_count"] == 1
    assert report["skipped_count"] == 1
    assert report["unverified_count"] == 1

    recs = report["records"]
    assert recs[0]["status"] == VERIFIED_PASS
    assert recs[0]["status_badge"] == "[PASS]"

    assert recs[1]["status"] == MISMATCH_FAIL
    assert recs[1]["status_badge"] == "[FAIL]"

    assert recs[2]["status"] == SKIPPED
    assert recs[2]["status_badge"] == "[SKIPPED]"

    assert recs[3]["status"] == UNVERIFIED
    assert recs[3]["status_badge"] == "[UNVERIFIED]"


def test_unverified_file_hash_pairs():
    """Test classification of records missing pre-hash, post-hash, or both hashes."""
    records = [
        {
            "source_path": "/tmp/no_pre.txt",
            "destination_path": "/tmp/out/no_pre.txt",
            "pre_hash": "",
            "post_hash": "hash_post_only",
        },
        {
            "source_path": "/tmp/no_post.txt",
            "destination_path": "/tmp/out/no_post.txt",
            "pre_hash": "hash_pre_only",
            "post_hash": None,
        },
        {
            "source_path": "/tmp/no_hash.txt",
            "destination_path": "/tmp/out/no_hash.txt",
            "pre_hash": None,
            "post_hash": None,
        },
    ]

    report = generate_audit_report("/tmp", records)
    assert report["total_files"] == 3
    assert report["unverified_count"] == 3
    assert report["verified_pass_count"] == 0
    assert report["mismatch_fail_count"] == 0

    for rec in report["records"]:
        assert rec["status"] == UNVERIFIED
        assert rec["status_badge"] == "[UNVERIFIED]"


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


def test_export_audit_report_path_traversal_prevention(tmp_path):
    """Test that export attempts escaping the allowed base directory raise AuditExportError."""
    base_dir = tmp_path / "allowed_base"
    base_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()

    records = [
        {
            "source_path": str(base_dir / "file.txt"),
            "destination_path": str(base_dir / "out" / "file.txt"),
            "pre_hash": "123",
            "post_hash": "123",
        }
    ]
    report = generate_audit_report(str(base_dir), records)

    # Relative path traversal escape
    with pytest.raises(AuditExportError) as exc_info:
        export_audit_report_json(report, "../outside_report.json")
    assert "escapes allowed base directory boundary" in str(exc_info.value)

    # Absolute path escape outside base_dir
    outside_file = str(outside_dir / "report.csv")
    with pytest.raises(AuditExportError) as exc_info:
        export_audit_report_csv(report, outside_file)
    assert "escapes allowed base directory boundary" in str(exc_info.value)


def test_export_audit_report_posix_permissions_0o600(tmp_path):
    """Test exported report files have mode 0o600 permissions on POSIX systems."""
    import stat

    base_dir = tmp_path / "secure_base"
    base_dir.mkdir()
    report = generate_audit_report(str(base_dir), [])

    json_path = export_audit_report_json(report, str(base_dir / "report.json"))
    csv_path = export_audit_report_csv(report, str(base_dir / "report.csv"))

    assert os.path.exists(json_path)
    assert os.path.exists(csv_path)

    if os.name != "nt":
        json_mode = stat.S_IMODE(os.stat(json_path).st_mode)
        csv_mode = stat.S_IMODE(os.stat(csv_path).st_mode)
        assert json_mode == 0o600
        assert csv_mode == 0o600


def test_export_audit_report_explicit_base_dir_override(tmp_path):
    """Test that explicit base_dir argument overrides report_data base_dir."""
    base_1 = tmp_path / "base1"
    base_1.mkdir()
    base_2 = tmp_path / "base2"
    base_2.mkdir()

    report = generate_audit_report(str(base_1), [])

    # Exporting to base_2 using explicit base_dir=base_2 should succeed
    out_2 = str(base_2 / "report.json")
    res = export_audit_report(report, out_2, format_type="json", base_dir=str(base_2))
    assert os.path.exists(res)

    # Exporting to base_2 without overriding base_dir should fail because report base_dir is base_1
    with pytest.raises(AuditExportError):
        export_audit_report(report, out_2, format_type="json")
