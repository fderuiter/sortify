import json
import os
import subprocess
import sys

import pytest

from scripts.snapshot_merge_driver import (
    compute_payload_checksum,
    extract_metadata_and_payload,
    run_json_merge_driver,
    run_svg_merge_driver,
)


@pytest.fixture
def base_dir():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_gitattributes_registration(base_dir):
    """Verify .gitattributes defines custom merge drivers for API snapshots and TUI SVGs."""
    gitattributes_path = os.path.join(base_dir, ".gitattributes")
    assert os.path.exists(gitattributes_path)
    with open(gitattributes_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "tests/snapshots/api_snapshot.json merge=snapshot-json" in content
    assert "merge=snapshot-json" in content
    assert "tests/snapshots/tui_svg/*.svg merge=snapshot-svg" in content


def test_checksum_computation():
    """Verify payload SHA-256 checksum calculation is canonical and deterministic."""
    payload = {
        "classes": [{"class_name": "TestClass", "decorators": [], "methods": []}],
        "functions": [],
    }
    checksum1 = compute_payload_checksum(payload)
    checksum2 = compute_payload_checksum(payload)
    assert checksum1 == checksum2
    assert len(checksum1) == 64  # SHA-256 hex digest length


def test_concurrent_json_additions_and_checksum_update(tmp_path):
    """Verify concurrent non-conflicting additions in JSON snapshots merge automatically with updated checksum."""
    o_data = {
        "_metadata": {"checksum": "old"},
        "classes": [
            {
                "class_name": "BaseClass",
                "decorators": [],
                "methods": [
                    {
                        "async": False,
                        "decorators": [],
                        "name": "__init__",
                        "parameters": [{"annotation": None, "default": None, "name": "self"}],
                        "returns": "None",
                    }
                ],
            }
        ],
        "functions": [
            {
                "async": False,
                "decorators": [],
                "name": "base_func",
                "parameters": [],
                "returns": None,
            }
        ],
    }

    # Branch A adds method to BaseClass and new class AClass
    a_data = json.loads(json.dumps(o_data))
    a_data["classes"][0]["methods"].append(
        {
            "async": False,
            "decorators": [],
            "name": "method_a",
            "parameters": [{"annotation": None, "default": None, "name": "self"}],
            "returns": "str",
        }
    )
    a_data["classes"].append(
        {
            "class_name": "AClass",
            "decorators": [],
            "methods": [],
        }
    )

    # Branch B adds function BFunc
    b_data = json.loads(json.dumps(o_data))
    b_data["functions"].append(
        {
            "async": False,
            "decorators": [],
            "name": "b_func",
            "parameters": [],
            "returns": "int",
        }
    )

    f_o = tmp_path / "O.json"
    f_a = tmp_path / "A.json"
    f_b = tmp_path / "B.json"

    f_o.write_text(json.dumps(o_data, indent=2))
    f_a.write_text(json.dumps(a_data, indent=2))
    f_b.write_text(json.dumps(b_data, indent=2))

    exit_code = run_json_merge_driver(str(f_o), str(f_a), str(f_b), "test_api.json")
    assert exit_code == 0

    merged = json.loads(f_a.read_text())
    meta, payload = extract_metadata_and_payload(merged)

    # Check payload contents
    class_names = [c["class_name"] for c in payload["classes"]]
    assert "BaseClass" in class_names
    assert "AClass" in class_names

    base_class = next(c for c in payload["classes"] if c["class_name"] == "BaseClass")
    method_names = [m["name"] for m in base_class["methods"]]
    assert "__init__" in method_names
    assert "method_a" in method_names

    func_names = [f["name"] for f in payload["functions"]]
    assert "base_func" in func_names
    assert "b_func" in func_names

    # Check checksum updated correctly
    computed_cs = compute_payload_checksum(payload)
    assert meta["checksum"] == computed_cs


def test_contract_breaking_change_detection(tmp_path):
    """Verify that dropping a public signature or required parameter triggers contract breaking errors and non-zero exit."""
    o_data = {
        "_metadata": {"checksum": "123"},
        "classes": [
            {
                "class_name": "CoreClass",
                "decorators": [],
                "methods": [
                    {
                        "async": False,
                        "decorators": [],
                        "name": "essential_method",
                        "parameters": [{"annotation": None, "default": None, "name": "self"}],
                        "returns": None,
                    }
                ],
            }
        ],
        "functions": [
            {
                "async": False,
                "decorators": [],
                "name": "essential_function",
                "parameters": [{"annotation": "int", "default": None, "name": "req_param"}],
                "returns": None,
            }
        ],
    }

    # Branch A drops essential_method from CoreClass
    a_data = json.loads(json.dumps(o_data))
    a_data["classes"][0]["methods"] = []

    # Branch B is unchanged
    b_data = json.loads(json.dumps(o_data))

    f_o = tmp_path / "O.json"
    f_a = tmp_path / "A.json"
    f_b = tmp_path / "B.json"

    f_o.write_text(json.dumps(o_data, indent=2))
    f_a.write_text(json.dumps(a_data, indent=2))
    f_b.write_text(json.dumps(b_data, indent=2))

    exit_code = run_json_merge_driver(str(f_o), str(f_a), str(f_b), "test_api.json")
    assert exit_code != 0


def test_json_collision_conflict_marker_fallback(tmp_path):
    """Verify that unresolvable AST/JSON collisions trigger conflict markers and exit code 1."""
    o_data = {
        "functions": [
            {
                "async": False,
                "decorators": [],
                "name": "conflicting_function",
                "parameters": [],
                "returns": "str",
            }
        ]
    }

    a_data = json.loads(json.dumps(o_data))
    a_data["functions"][0]["returns"] = "int"

    b_data = json.loads(json.dumps(o_data))
    b_data["functions"][0]["returns"] = "bool"

    f_o = tmp_path / "O.json"
    f_a = tmp_path / "A.json"
    f_b = tmp_path / "B.json"

    f_o.write_text(json.dumps(o_data, indent=2))
    f_a.write_text(json.dumps(a_data, indent=2))
    f_b.write_text(json.dumps(b_data, indent=2))

    exit_code = run_json_merge_driver(str(f_o), str(f_a), str(f_b), "test_api.json")
    assert exit_code == 1

    content = f_a.read_text()
    assert "<<<<<<< OURS" in content
    assert "=======" in content
    assert ">>>>>>> THEIRS" in content


def test_svg_3way_merge_and_conflict_fallback(tmp_path):
    """Verify 3-way SVG merging of additions and fallback to conflict markers on collision."""
    svg_o = '<svg xmlns="http://www.w3.org/2000/svg"><g id="main"></g></svg>'
    svg_a = '<svg xmlns="http://www.w3.org/2000/svg"><g id="main"></g><rect id="r1"/></svg>'
    svg_b = '<svg xmlns="http://www.w3.org/2000/svg"><g id="main"></g><circle id="c1"/></svg>'

    f_o = tmp_path / "O.svg"
    f_a = tmp_path / "A.svg"
    f_b = tmp_path / "B.svg"

    f_o.write_text(svg_o)
    f_a.write_text(svg_a)
    f_b.write_text(svg_b)

    exit_code = run_svg_merge_driver(str(f_o), str(f_a), str(f_b), "tui.svg")
    assert exit_code == 0

    merged_svg = f_a.read_text()
    assert '<rect id="r1"' in merged_svg or 'id="r1"' in merged_svg
    assert '<circle id="c1"' in merged_svg or 'id="c1"' in merged_svg

    # Test SVG conflict fallback on invalid/incompatible SVG
    f_a.write_text("invalid svgours <<<<")
    f_b.write_text("invalid svgtheirs >>>>")
    exit_code = run_svg_merge_driver(str(f_o), str(f_a), str(f_b), "tui.svg")
    assert exit_code == 1
    content = f_a.read_text()
    assert "<<<<<<< OURS" in content


def test_cli_setup_command(base_dir):
    """Verify python scripts/snapshot_merge_driver.py --setup registers git config settings."""
    script_path = os.path.join(base_dir, "scripts", "snapshot_merge_driver.py")
    res = subprocess.run([sys.executable, script_path, "--setup"], capture_output=True, text=True, cwd=base_dir)
    assert res.returncode == 0
    assert "Successfully configured snapshot merge drivers" in res.stdout

    check_json = subprocess.run(["git", "config", "--get", "merge.snapshot-json.driver"], capture_output=True, text=True, cwd=base_dir)
    assert check_json.returncode == 0
    assert "snapshot_merge_driver.py" in check_json.stdout

    check_svg = subprocess.run(["git", "config", "--get", "merge.snapshot-svg.driver"], capture_output=True, text=True, cwd=base_dir)
    assert check_svg.returncode == 0
    assert "snapshot_merge_driver.py" in check_svg.stdout


def test_cli_resolver_command(base_dir):
    """Verify python scripts/snapshot_merge_driver.py --cli runs inspection mode cleanly."""
    script_path = os.path.join(base_dir, "scripts", "snapshot_merge_driver.py")
    res = subprocess.run([sys.executable, script_path, "--cli"], capture_output=True, text=True, cwd=base_dir)
    assert res.returncode == 0
    assert "Interactive Snapshot Conflict & Diff Resolver" in res.stdout
