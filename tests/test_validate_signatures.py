import ast
import json
import os
import pytest
from scripts.validate_signatures import extract_protocols, extract_cli, collect_current_definitions


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
        "help": "Enable verbose logging"
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
