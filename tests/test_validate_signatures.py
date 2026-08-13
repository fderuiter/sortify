from scripts.validate_signatures import (
    collect_current_definitions,
    extract_cli,
    extract_protocols,
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

    # Check that ClusteringStrategy is extracted
    assert "ClusteringStrategy" in defs["protocols"]
    assert "DocumentExtractor" in defs["protocols"]

    # Check CLI keys
    assert "app/main.py" in defs["cli"]
    assert "sandbox_cli.py" in defs["cli"]

    main_cli = defs["cli"]["app/main.py"]
    assert len(main_cli) > 0
    demo_arg = [arg for arg in main_cli if arg["args"] == ["--demo"]]
    assert len(demo_arg) == 1
    assert demo_arg[0]["keywords"]["action"] == "store_true"


def test_api_signature_snapshot_matches():
    import json
    import os

    from scripts.validate_signatures import SNAPSHOT_PATH, collect_current_definitions

    current_definitions = collect_current_definitions()
    is_ci = os.environ.get("CI", "").lower() in ("true", "1")

    if not os.path.exists(SNAPSHOT_PATH):
        if not is_ci:
            os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
            with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
                json.dump(current_definitions, f, indent=2, sort_keys=True)
                f.write("\n")
            print(f"Successfully generated new baseline snapshot at {SNAPSHOT_PATH}")
            return
        else:
            raise AssertionError(
                f"Baseline snapshot file does not exist at {SNAPSHOT_PATH}"
            )

    with open(SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        snapshot_definitions = json.load(f)

    current_json = json.dumps(current_definitions, indent=2, sort_keys=True)
    snapshot_json = json.dumps(snapshot_definitions, indent=2, sort_keys=True)

    if current_json != snapshot_json:
        if not is_ci:
            os.makedirs(os.path.dirname(SNAPSHOT_PATH), exist_ok=True)
            with open(SNAPSHOT_PATH, "w", encoding="utf-8") as f:
                json.dump(current_definitions, f, indent=2, sort_keys=True)
                f.write("\n")
            print(f"Successfully auto-updated baseline snapshot at {SNAPSHOT_PATH}")
            return
        else:
            raise AssertionError(
                f"Public interface or CLI signature drift detected! "
                f"In CI, automated baseline regeneration is disabled. "
                f"Please commit the updated snapshot file '{SNAPSHOT_PATH}'."
            )


def test_extract_async_and_generic_protocols(tmp_path):
    protocol_file = tmp_path / "async_generic_protocols.py"
    protocol_code = """
from typing import Protocol, TypeVar, Generic, List, Optional

T = TypeVar("T")

class AsyncGenericProtocol(Protocol[T]):
    async def process(self, data: T, options: Optional[dict] = None) -> List[T]:
        ...

    def sync_method(self, value: int) -> str:
        ...
"""
    protocol_file.write_text(protocol_code)
    protocols = extract_protocols(str(protocol_file))

    assert "AsyncGenericProtocol" in protocols
    proto_data = protocols["AsyncGenericProtocol"]
    assert proto_data["class_name"] == "AsyncGenericProtocol"
    assert len(proto_data["methods"]) == 2

    # Methods are sorted by name: [process, sync_method]
    methods = proto_data["methods"]
    assert methods[0]["name"] == "process"
    assert methods[0]["async"] is True
    assert methods[0]["returns"] == "List[T]"
    assert len(methods[0]["parameters"]) == 3
    assert methods[0]["parameters"][0]["name"] == "self"
    assert methods[0]["parameters"][1]["name"] == "data"
    assert methods[0]["parameters"][1]["annotation"] == "T"
    assert methods[0]["parameters"][2]["name"] == "options"
    assert methods[0]["parameters"][2]["annotation"] == "Optional[dict]"
    assert methods[0]["parameters"][2]["default"] == "None"

    assert methods[1]["name"] == "sync_method"
    assert methods[1]["async"] is False
    assert methods[1]["returns"] == "str"


def test_extract_typing_variations(tmp_path):
    protocol_file = tmp_path / "typing_variations.py"
    protocol_code = """
import typing

class TypingVariationProtocol(typing.Protocol):
    async def complex_method(
        self,
        *args: str,
        kw_only_val: int = 42,
        **kwargs: typing.Any
    ) -> None:
        ...
"""
    protocol_file.write_text(protocol_code)
    protocols = extract_protocols(str(protocol_file))

    assert "TypingVariationProtocol" in protocols
    proto_data = protocols["TypingVariationProtocol"]
    methods = proto_data["methods"]
    assert len(methods) == 1
    method = methods[0]
    assert method["name"] == "complex_method"
    assert method["async"] is True

    params = method["parameters"]
    # self, *args, kw_only_val, **kwargs
    assert len(params) == 4
    assert params[0]["name"] == "self"
    assert params[1]["name"] == "*args"
    assert params[1]["annotation"] == "str"
    assert params[2]["name"] == "kw_only_val"
    assert params[2]["annotation"] == "int"
    assert params[2]["default"] == "42"
    assert params[3]["name"] == "**kwargs"
    assert params[3]["annotation"] == "typing.Any"


def test_signature_mismatch_detection():
    # Setup two mismatched definition dicts
    dict_a = {
        "protocols": {
            "MyProtocol": {
                "class_name": "MyProtocol",
                "methods": [
                    {
                        "name": "run",
                        "async": False,
                        "parameters": [],
                        "returns": "None"
                    }
                ]
            }
        },
        "cli": {}
    }

    # Dict B has different parameter name (breaking change)
    dict_b = {
        "protocols": {
            "MyProtocol": {
                "class_name": "MyProtocol",
                "methods": [
                    {
                        "name": "run",
                        "async": False,
                        "parameters": [{"name": "x", "annotation": "int", "default": None}],
                        "returns": "None"
                    }
                ]
            }
        },
        "cli": {}
    }

    # Dict C has different async modifier (breaking change)
    dict_c = {
        "protocols": {
            "MyProtocol": {
                "class_name": "MyProtocol",
                "methods": [
                    {
                        "name": "run",
                        "async": True,
                        "parameters": [],
                        "returns": "None"
                    }
                ]
            }
        },
        "cli": {}
    }

    # Test JSON string inequality which triggers mismatch
    import json
    json_a = json.dumps(dict_a, indent=2, sort_keys=True)
    json_b = json.dumps(dict_b, indent=2, sort_keys=True)
    json_c = json.dumps(dict_c, indent=2, sort_keys=True)

    assert json_a != json_b
    assert json_a != json_c


def test_validation_runner_detects_mismatch(tmp_path, monkeypatch):
    import json
    import sys

    from scripts import validate_signatures

    fake_snapshot = tmp_path / "fake_snapshot.json"
    
    # Pre-populate fake snapshot with one definition
    initial_defs = {
        "protocols": {
            "MyProtocol": {
                "class_name": "MyProtocol",
                "methods": [
                    {
                        "name": "run",
                        "async": True,
                        "parameters": [],
                        "returns": "None"
                    }
                ]
            }
        },
        "cli": {}
    }
    
    fake_snapshot.write_text(json.dumps(initial_defs, indent=2, sort_keys=True))
    
    # Now simulate changed codebase definitions (e.g. async changed to false)
    changed_defs = {
        "protocols": {
            "MyProtocol": {
                "class_name": "MyProtocol",
                "methods": [
                    {
                        "name": "run",
                        "async": False,  # mismatch!
                        "parameters": [],
                        "returns": "None"
                    }
                ]
            }
        },
        "cli": {}
    }

    # Mock variables and functions
    monkeypatch.setattr(validate_signatures, "SNAPSHOT_PATH", str(fake_snapshot))
    monkeypatch.setattr(validate_signatures, "collect_current_definitions", lambda: changed_defs)
    monkeypatch.setenv("CI", "true")

    exited_code = None
    def mock_exit(code):
        nonlocal exited_code
        exited_code = code
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", mock_exit)
    monkeypatch.setattr(sys, "argv", ["validate_signatures.py"])

    # Run main, should exit with 1 because of mismatch in CI
    import pytest
    with pytest.raises(SystemExit) as exc_info:
        validate_signatures.main()
    assert exc_info.value.code == 1
    assert exited_code == 1

