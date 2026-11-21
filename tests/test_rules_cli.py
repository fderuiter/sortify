"""Tests for headless `smart-autosorter rules import` and `rules export` CLI subcommands."""

import io
import json
import os
import plistlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings


def run_cli(args: list[str], env: dict[str, str] | None = None) -> tuple[int, str, str]:
    """Run app/main.py in-process and return (returncode, stdout, stderr)."""
    old_env = os.environ.copy()
    repo_root = str(Path(__file__).parent.parent.resolve())
    os.environ["PYTHONPATH"] = repo_root + os.pathsep + os.environ.get("PYTHONPATH", "")
    if env:
        os.environ.update(env)

    stdout_cap = io.StringIO()
    stderr_cap = io.StringIO()
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = False
    mock_stdin.encoding = "utf-8"

    code = 0
    with (
        patch("sys.argv", ["smart-autosorter"] + args),
        patch("sys.stdout", stdout_cap),
        patch("sys.stderr", stderr_cap),
        patch("sys.stdin", mock_stdin),
    ):
        try:
            from app.main import main

            main()
        except SystemExit as exc:
            code = (
                exc.code
                if isinstance(exc.code, int)
                else (0 if exc.code is None else 1)
            )
        finally:
            os.environ.clear()
            os.environ.update(old_env)

    return code, stdout_cap.getvalue(), stderr_cap.getvalue()


@pytest.mark.xdist_group(name="rules_cli")
def test_rules_import_dry_run_default_disabled_and_enable_flag(tmp_path):
    """Verify `rules import` previews without saving on --dry-run, stages disabled by default, and activates with --enable."""
    settings_path = tmp_path / "settings.json"
    seed = AppSettings(filepath=str(settings_path))
    seed.KEYWORD_RULES = {"invoice": "Existing/Invoices"}
    seed._save()
    seed.flush()

    hazel_file = tmp_path / "imported.hazelrules"
    hazel_payload = {
        "Rules": [
            {
                "NSRuleName": "Invoices Absolute",
                "NSRuleConditions": [
                    {"target": "Name", "operator": "contains", "value": "invoice"},
                    {"target": "Size", "operator": "greater than", "value": "5MB"},
                ],
                "NSRuleActions": [{"DestinationPath": "/Users/alice/Finance/Invoices"}],
            },
            {
                "NSRuleName": "Receipts New",
                "NSRuleConditions": [
                    {"target": "Name", "operator": "contains", "value": "receipt"},
                ],
                "NSRuleActions": [{"DestinationPath": "Finance/Receipts"}],
            },
            {
                "NSRuleName": "Archive Extractor",
                "NSRuleConditions": [
                    {"target": "Extension", "operator": "is", "value": "zip"},
                ],
                "NSRuleActions": [
                    {
                        "NSRuleActionType": "NSRuleActionUnarchive",
                        "DestinationPath": "/Users/alice/Archives/Unpacked",
                    }
                ],
            },
        ]
    }
    with open(hazel_file, "wb") as f:
        plistlib.dump(hazel_payload, f)

    # 1. Dry-run JSON preview does NOT modify settings on disk
    code, stdout, stderr = run_cli(
        [
            "rules",
            "import",
            str(hazel_file),
            "--settings-file",
            str(settings_path),
            "--dry-run",
            "--json",
        ]
    )
    assert code == 0, stderr
    preview = json.loads(stdout)
    assert preview["status"] == "preview"
    assert preview["dry_run"] is True
    assert preview["import_disabled"] is True
    assert preview["keyword_rules"]["invoice"] == "Finance/Invoices"
    assert preview["keyword_rules"]["receipt"] == "Finance/Receipts"
    assert len(preview["conflicts"]) == 1
    assert preview["conflicts"][0]["identifier"] == "invoice"
    assert preview["conflicts"][0]["resolution"] == "keep_existing"
    assert any("converted destination path" in w.lower() for w in preview["warnings"])
    assert any("size" in w.lower() for w in preview["warnings"])

    reloaded_after_dry = AppSettings(filepath=str(settings_path))
    assert reloaded_after_dry.KEYWORD_RULES == {"invoice": "Existing/Invoices"}
    assert reloaded_after_dry.DISABLED_KEYWORD_RULES == {}
    assert reloaded_after_dry.POLICIES == []

    # 2. Live merge with default disabled staging + per-rule --resolve override
    code_merge, stdout_merge, stderr_merge = run_cli(
        [
            "rules",
            "import",
            str(hazel_file),
            "--settings-file",
            str(settings_path),
            "--resolve",
            "invoice=overwrite",
            "--json",
        ]
    )
    assert code_merge == 0, stderr_merge
    merged = json.loads(stdout_merge)
    assert merged["status"] == "merged"
    assert merged["import_disabled"] is True

    reloaded_disabled = AppSettings(filepath=str(settings_path))
    # Overwritten existing active keyword stays in KEYWORD_RULES; brand-new 'receipt' staged in DISABLED_KEYWORD_RULES
    assert reloaded_disabled.KEYWORD_RULES == {"invoice": "Finance/Invoices"}
    assert reloaded_disabled.DISABLED_KEYWORD_RULES == {"receipt": "Finance/Receipts"}
    assert len(reloaded_disabled.POLICIES) == 1
    assert reloaded_disabled.POLICIES[0]["action"] == "unarchive"
    assert reloaded_disabled.POLICIES[0]["enabled"] is False

    # 3. Live merge with --enable into a clean settings file activates rules immediately
    active_settings_path = tmp_path / "active_settings.json"
    code_en, stdout_en, stderr_en = run_cli(
        [
            "rules",
            "import",
            str(hazel_file),
            "--settings-file",
            str(active_settings_path),
            "--enable",
        ]
    )
    assert code_en == 0, stderr_en
    assert "Rule Import Summary" in stdout_en
    reloaded_active = AppSettings(filepath=str(active_settings_path))
    assert reloaded_active.KEYWORD_RULES["receipt"] == "Finance/Receipts"
    assert reloaded_active.DISABLED_KEYWORD_RULES == {}
    assert reloaded_active.POLICIES[0].get("enabled", True) is True


@pytest.mark.xdist_group(name="rules_cli")
def test_rules_export_formats_dry_run_and_errors(tmp_path):
    """Verify `rules export` supports Sortify JSON, Hazel, and DropIt INI plus --dry-run and error handling."""
    settings_path = tmp_path / "export_settings.json"
    seed = AppSettings(filepath=str(settings_path))
    seed.KEYWORD_RULES = {"contract": "Legal/Contracts"}
    seed.POLICIES = [
        {
            "type": "unarchive",
            "expression": "*.zip",
            "target_path": "Archives/Unpacked",
            "action": "unarchive",
            "priority": 85,
        }
    ]
    seed._save()
    seed.flush()

    # 1. Dry-run export does not create output file
    dry_out = tmp_path / "dry_export.ini"
    code_dry, stdout_dry, stderr_dry = run_cli(
        [
            "rules",
            "export",
            str(dry_out),
            "--settings-file",
            str(settings_path),
            "--dry-run",
            "--json",
        ]
    )
    assert code_dry == 0, stderr_dry
    dry_data = json.loads(stdout_dry)
    assert dry_data["status"] == "preview"
    assert dry_data["format"] == "dropit_ini"
    assert not dry_out.exists()

    # 2. Export to DropIt INI, Hazel, and Sortify JSON
    for filename, expected_fmt in [
        ("exported.ini", "dropit_ini"),
        ("exported.hazelrules", "hazel"),
        ("exported.json", "sortify_json"),
    ]:
        out_path = tmp_path / filename
        code_exp, stdout_exp, stderr_exp = run_cli(
            [
                "rules",
                "export",
                str(out_path),
                "--settings-file",
                str(settings_path),
                "--json",
            ]
        )
        assert code_exp == 0, stderr_exp
        exp_data = json.loads(stdout_exp)
        assert exp_data["status"] == "exported"
        assert exp_data["format"] == expected_fmt
        assert out_path.exists()

    # 3. Error handling: missing subcommand, missing file, invalid --resolve, corrupt file
    code_missing_sub, _, _ = run_cli(["rules"])
    assert code_missing_sub == 2

    code_missing_file, stdout_nf, _ = run_cli(
        ["rules", "import", str(tmp_path / "missing.ini"), "--json"]
    )
    assert code_missing_file == 1
    assert json.loads(stdout_nf)["status"] == "error"

    bad_file = tmp_path / "corrupt.xml"
    bad_file.write_text("<broken_xml", encoding="utf-8")
    code_corrupt, stdout_corrupt, _ = run_cli(
        ["rules", "import", str(bad_file), "--json"]
    )
    assert code_corrupt == 1
    assert json.loads(stdout_corrupt)["status"] == "error"

    code_bad_res, _, _ = run_cli(
        ["rules", "import", str(tmp_path / "exported.ini"), "--resolve", "bad_syntax"]
    )
    assert code_bad_res == 2
