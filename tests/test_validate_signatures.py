from scripts.validate_signatures import (
    collect_current_definitions,
    extract_cli,
    extract_protocols,
    clean_realpath,
    get_relative_path,
)


def test_extract_protocols_empty_and_valid(tmp_path):
    # Test empty file
    empty_file = tmp_path / "empty.py"
    empty_file.write_text("")
    assert extract_protocols(str(empty_file)) == {}

    # Test file with valid Protocol
    protocol_file = tmp_path / "protocols.py"
    protocol_code = """
from typing import Protocol, List

class MyStrategy(Protocol):
    def run(self, data: List[int], flag: bool = True) -> dict:
        ...
"""
    protocol_file.write_text(protocol_code)
    protocols = extract_protocols(str(protocol_file))

    assert "MyStrategy" in protocols
    my_strategy = protocols["MyStrategy"]
    assert my_strategy["class_name"] == "MyStrategy"
    assert len(my_strategy["methods"]) == 1

    method = my_strategy["methods"][0]
    assert method["name"] == "run"
    assert method["returns"] == "dict"

    # self, data, flag
    params = method["parameters"]
    assert len(params) == 3
    assert params[0]["name"] == "self"
    assert params[1]["name"] == "data"
    assert params[1]["annotation"] == "List[int]"
    assert params[2]["name"] == "flag"
    assert params[2]["annotation"] == "bool"
    assert params[2]["default"] == "True"


def test_extract_cli_valid(tmp_path):
    cli_file = tmp_path / "cli.py"
    cli_code = """
import argparse
parser = argparse.ArgumentParser(description="Test Parser")
parser.add_argument("--verbose", action="store_true", help="Enable verbose logging")
"""
    cli_file.write_text(cli_code)
    cli_calls = extract_cli(str(cli_file))

    assert len(cli_calls) == 1
    call = cli_calls[0]
    assert call["caller"] == "parser"
    assert call["method"] == "add_argument"
    assert call["args"] == ["--verbose"]
    assert call["keywords"] == {
        "action": "store_true",
        "help": "Enable verbose logging",
    }


def test_collect_current_definitions():
    defs = collect_current_definitions()
    assert "protocols" in defs
    assert "cli" in defs
    assert "functions" in defs

    # Check that ClusteringStrategy is extracted
    assert "ClusteringStrategy" in defs["protocols"]
    assert "DocumentExtractor" in defs["protocols"]

    # Check standard public class (non-protocol) is dynamically discovered
    assert "RecursiveKMeansStrategy" in defs["protocols"]

    # Check standalone public function is dynamically discovered
    assert "block_external_network" in defs["functions"]

    # Check CLI keys
    assert "app/main.py" in defs["cli"]
    assert "sandbox_cli.py" in defs["cli"]

    main_cli = defs["cli"]["app/main.py"]
    assert len(main_cli) > 0
    demo_arg = [arg for arg in main_cli if arg["args"] == ["--demo"]]
    assert len(demo_arg) == 1
    assert demo_arg[0]["keywords"]["action"] == "store_true"


def test_clean_realpath_and_get_relative_path(monkeypatch):
    # Test clean_realpath strips Windows long path prefix \\?\
    monkeypatch.setattr("os.path.realpath", lambda path: f"\\\\?\\C:\\foo\\bar")
    assert clean_realpath("some_path") == "C:\\foo\\bar"

    # Test clean_realpath passes through normal path
    monkeypatch.setattr("os.path.realpath", lambda path: "/usr/local/bin")
    assert clean_realpath("some_path") == "/usr/local/bin"

    # Test get_relative_path normalizes drive casing and slashes under mocked Windows env
    def mock_realpath(path):
        if "start" in path:
            return "\\\\?\\D:\\a\\sortify\\sortify"
        return "\\\\?\\d:\\a\\sortify\\sortify\\app\\core\\helper.py"

    monkeypatch.setattr("os.path.realpath", mock_realpath)
    recorded_args = []
    def mock_relpath(path, start):
        recorded_args.append((path, start))
        return "app\\core\\helper.py"
    monkeypatch.setattr("os.path.relpath", mock_relpath)

    rel = get_relative_path("file", "start")
    assert rel == "app/core/helper.py"
    # Verify that get_relative_path successfully normalized both paths to lowercase drive letter 'd:'
    assert recorded_args == [("d:\\a\\sortify\\sortify\\app\\core\\helper.py", "d:\\a\\sortify\\sortify")]

    # Undo monkeypatching to test real filesystem behaviors
    monkeypatch.undo()

    # Since we can't easily mock complex os.path.relpath interactions under different OS environments,
    # let's test with the actual OS path functions by providing real filesystem paths.
    import tempfile
    import os
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a directory and file
        base_dir = os.path.realpath(tmpdir)
        sub_dir = os.path.join(base_dir, "app", "core")
        os.makedirs(sub_dir, exist_ok=True)
        file_path = os.path.join(sub_dir, "helper.py")
        with open(file_path, "w") as f:
            f.write("")

        # Let's verify clean_realpath and get_relative_path behavior on the actual paths
        assert clean_realpath(base_dir) == os.path.realpath(base_dir).replace("\\\\?\\", "")
        rel = get_relative_path(file_path, base_dir)
        assert rel in ("app/core/helper.py", "app\\core\\helper.py") or rel.replace("\\", "/") == "app/core/helper.py"

