import json
import os
import sys

import pytest

from scripts import validate_signatures
from scripts.validate_signatures import (
    SNAPSHOT_DIR,
    collect_core_definitions,
    collect_current_definitions,
    collect_modules_and_payloads,
    compute_payload_checksum,
    extract_cli,
    extract_module_signatures,
    extract_protocols,
    verify_snapshot_integrity,
)


def test_extract_module_signatures(tmp_path):
    module_file = tmp_path / "test_module.py"
    module_code = """
import functools
from dataclasses import dataclass

@dataclass
class ServiceWorker:
    name: str

    @staticmethod
    def process_data(value: int = 100) -> str:
        return str(value)

    def _private_method(self):
        pass

def public_top_level_fn(flag: bool = True) -> int:
    return 1 if flag else 0

def _private_top_level_fn():
    pass
"""
    module_file.write_text(module_code)
    sigs = extract_module_signatures(str(module_file))

    assert "classes" in sigs
    assert "functions" in sigs

    # Public class checks
    assert len(sigs["classes"]) == 1
    cls = sigs["classes"][0]
    assert cls["class_name"] == "ServiceWorker"
    assert "@dataclass" in cls["decorators"]

    method_names = [m["name"] for m in cls["methods"]]
    assert "process_data" in method_names
    assert "_private_method" not in method_names

    process_method = [m for m in cls["methods"] if m["name"] == "process_data"][0]
    assert "@staticmethod" in process_method["decorators"]
    assert process_method["parameters"][0]["name"] == "value"
    assert process_method["parameters"][0]["default"] == "100"
    assert process_method["parameters"][0]["annotation"] == "int"

    # Public top level functions
    func_names = [f["name"] for f in sigs["functions"]]
    assert "public_top_level_fn" in func_names
    assert "_private_top_level_fn" not in func_names


def test_dynamic_core_file_discovery(tmp_path):
    core_dir = tmp_path / "core"
    core_dir.mkdir()

    (core_dir / "alpha.py").write_text("def alpha_fn(): pass")
    (core_dir / "beta.py").write_text("def beta_fn(): pass")

    core_defs = collect_core_definitions(str(core_dir))
    keys = list(core_defs.keys())
    assert len(keys) == 2

    # Automatically discovers newly added module without config changes
    (core_dir / "gamma.py").write_text("def gamma_fn(): pass")
    core_defs_updated = collect_core_definitions(str(core_dir))
    assert len(core_defs_updated.keys()) == 3


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
    assert "core" in defs
    assert "cli" in defs

    # Check core module paths
    assert "app/core/analyzer.py" in defs["core"]
    assert "app/core/extractor.py" in defs["core"]

    # Check CLI keys
    assert "app/main.py" in defs["cli"]
    assert "sandbox_cli.py" in defs["cli"]

    main_cli = defs["cli"]["app/main.py"]
    assert len(main_cli) > 0
    demo_arg = [arg for arg in main_cli if arg["args"] == ["--demo"]]
    assert len(demo_arg) == 1
    assert demo_arg[0]["keywords"]["action"] == "store_true"


def test_api_signature_snapshot_matches():
    modules = collect_modules_and_payloads()
    assert len(modules) > 0

    assert os.path.exists(SNAPSHOT_DIR)

    for m in modules:
        snap_path = m["snapshot_path"]
        assert os.path.exists(snap_path), (
            f"Missing snapshot for {m['source_rel']} at {snap_path}"
        )

        with open(snap_path, "r", encoding="utf-8") as f:
            snapshot_data = json.load(f)

        is_valid, err_msg, snapshot_payload = verify_snapshot_integrity(
            snapshot_data, snap_path
        )
        assert is_valid, err_msg

        current_json = json.dumps(m["payload"], indent=2, sort_keys=True)
        snapshot_json = json.dumps(snapshot_payload, indent=2, sort_keys=True)

        assert current_json == snapshot_json, (
            f"Signature drift detected for {m['source_rel']} in {snap_path}!"
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
    dict_a = {
        "classes": [
            {
                "class_name": "MyProtocol",
                "methods": [
                    {"name": "run", "async": False, "parameters": [], "returns": "None"}
                ],
            }
        ],
        "functions": [],
    }

    dict_b = {
        "classes": [
            {
                "class_name": "MyProtocol",
                "methods": [
                    {
                        "name": "run",
                        "async": False,
                        "parameters": [
                            {"name": "x", "annotation": "int", "default": None}
                        ],
                        "returns": "None",
                    }
                ],
            }
        ],
        "functions": [],
    }

    dict_c = {
        "classes": [
            {
                "class_name": "MyProtocol",
                "methods": [
                    {"name": "run", "async": True, "parameters": [], "returns": "None"}
                ],
            }
        ],
        "functions": [],
    }

    json_a = json.dumps(dict_a, indent=2, sort_keys=True)
    json_b = json.dumps(dict_b, indent=2, sort_keys=True)
    json_c = json.dumps(dict_c, indent=2, sort_keys=True)

    assert json_a != json_b
    assert json_a != json_c


def test_safe_relpath(monkeypatch):
    from scripts.validate_signatures import safe_relpath

    res = safe_relpath("/app/tests/fake.json", "/app")
    assert res in ("tests/fake.json", "tests\\fake.json")

    def mock_relpath(path, start):
        raise ValueError("path is on mount 'C:', start on mount 'D:'")

    monkeypatch.setattr(os.path, "relpath", mock_relpath)
    assert safe_relpath("/app/tests/fake.json", "/app") == os.path.abspath(
        "/app/tests/fake.json"
    )


def test_validation_runner_detects_mismatch(tmp_path, monkeypatch):
    fake_snap_dir = tmp_path / "api"
    fake_snap_dir.mkdir(parents=True)

    module_snap = fake_snap_dir / "core" / "analyzer_strategies.json"
    module_snap.parent.mkdir(parents=True, exist_ok=True)

    initial_payload = {
        "classes": [
            {
                "class_name": "MyProtocol",
                "decorators": [],
                "methods": [
                    {"name": "run", "async": True, "parameters": [], "returns": "None"}
                ],
            }
        ],
        "functions": [],
    }
    initial_data = {
        "_metadata": {"checksum": compute_payload_checksum(initial_payload)},
        **initial_payload,
    }
    module_snap.write_text(json.dumps(initial_data, indent=2, sort_keys=True))

    changed_payload = {
        "classes": [
            {
                "class_name": "MyProtocol",
                "decorators": [],
                "methods": [
                    {"name": "run", "async": False, "parameters": [], "returns": "None"}
                ],
            }
        ],
        "functions": [],
    }

    changed_defs = {
        "cli": {},
        "core": {"app/core/analyzer_strategies.py": changed_payload},
    }

    monkeypatch.setattr(validate_signatures, "SNAPSHOT_DIR", str(fake_snap_dir))
    monkeypatch.setattr(validate_signatures, "SNAPSHOT_PATH", str(fake_snap_dir))
    monkeypatch.setattr(
        validate_signatures, "collect_current_definitions", lambda: changed_defs
    )
    monkeypatch.setenv("CI", "true")

    exited_code = None

    def mock_exit(code):
        nonlocal exited_code
        exited_code = code
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", mock_exit)
    monkeypatch.setattr(sys, "argv", ["validate_signatures.py"])

    with pytest.raises(SystemExit) as exc_info:
        validate_signatures.main()
    assert exc_info.value.code == 1
    assert exited_code == 1


def test_validation_local_fails_by_default_on_mismatch(tmp_path, monkeypatch):
    fake_snap_dir = tmp_path / "api"
    fake_snap_dir.mkdir(parents=True)

    module_snap = fake_snap_dir / "core" / "analyzer_strategies.json"
    module_snap.parent.mkdir(parents=True, exist_ok=True)

    initial_payload = {
        "classes": [
            {
                "class_name": "MyProtocol",
                "decorators": [],
                "methods": [
                    {"name": "run", "async": True, "parameters": [], "returns": "None"}
                ],
            }
        ],
        "functions": [],
    }
    initial_data = {
        "_metadata": {"checksum": compute_payload_checksum(initial_payload)},
        **initial_payload,
    }
    module_snap.write_text(json.dumps(initial_data, indent=2, sort_keys=True))

    changed_payload = {
        "classes": [
            {
                "class_name": "MyProtocol",
                "decorators": [],
                "methods": [
                    {"name": "run", "async": False, "parameters": [], "returns": "None"}
                ],
            }
        ],
        "functions": [],
    }

    changed_defs = {
        "cli": {},
        "core": {"app/core/analyzer_strategies.py": changed_payload},
    }

    monkeypatch.setattr(validate_signatures, "SNAPSHOT_DIR", str(fake_snap_dir))
    monkeypatch.setattr(validate_signatures, "SNAPSHOT_PATH", str(fake_snap_dir))
    monkeypatch.setattr(
        validate_signatures, "collect_current_definitions", lambda: changed_defs
    )
    monkeypatch.delenv("CI", raising=False)

    exited_code = None

    def mock_exit(code):
        nonlocal exited_code
        exited_code = code
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", mock_exit)
    monkeypatch.setattr(sys, "argv", ["validate_signatures.py"])

    with pytest.raises(SystemExit) as exc_info:
        validate_signatures.main()
    assert exc_info.value.code == 1
    assert exited_code == 1

    # Ensure baseline file was NOT modified
    with open(module_snap, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["classes"][0]["methods"][0]["async"] is True


def test_validation_local_success_on_regenerate(tmp_path, monkeypatch):
    fake_snap_dir = tmp_path / "api"
    fake_snap_dir.mkdir(parents=True)

    module_snap = fake_snap_dir / "core" / "analyzer_strategies.json"
    module_snap.parent.mkdir(parents=True, exist_ok=True)

    initial_payload = {
        "classes": [
            {
                "class_name": "MyProtocol",
                "decorators": [],
                "methods": [
                    {"name": "run", "async": True, "parameters": [], "returns": "None"}
                ],
            }
        ],
        "functions": [],
    }
    initial_data = {
        "_metadata": {"checksum": compute_payload_checksum(initial_payload)},
        **initial_payload,
    }
    module_snap.write_text(json.dumps(initial_data, indent=2, sort_keys=True))

    changed_payload = {
        "classes": [
            {
                "class_name": "MyProtocol",
                "decorators": [],
                "methods": [
                    {"name": "run", "async": False, "parameters": [], "returns": "None"}
                ],
            }
        ],
        "functions": [],
    }

    changed_defs = {
        "cli": {},
        "core": {"app/core/analyzer_strategies.py": changed_payload},
    }

    monkeypatch.setattr(validate_signatures, "SNAPSHOT_DIR", str(fake_snap_dir))
    monkeypatch.setattr(validate_signatures, "SNAPSHOT_PATH", str(fake_snap_dir))
    monkeypatch.setattr(
        validate_signatures, "collect_current_definitions", lambda: changed_defs
    )
    monkeypatch.delenv("CI", raising=False)

    exited_code = None

    def mock_exit(code):
        nonlocal exited_code
        exited_code = code
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", mock_exit)
    monkeypatch.setattr(sys, "argv", ["validate_signatures.py", "--update"])

    with pytest.raises(SystemExit) as exc_info:
        validate_signatures.main()
    assert exc_info.value.code == 0
    assert exited_code == 0

    with open(module_snap, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert data["classes"][0]["methods"][0]["async"] is False
    assert "_metadata" in data
    assert "checksum" in data["_metadata"]
    expected_hash = compute_payload_checksum(changed_payload)
    assert data["_metadata"]["checksum"] == expected_hash


def test_validation_partial_update_preserves_other_snapshots(tmp_path, monkeypatch):
    fake_snap_dir = tmp_path / "api"
    fake_snap_dir.mkdir(parents=True)

    snap1 = fake_snap_dir / "core" / "analyzer_strategies.json"
    snap2 = fake_snap_dir / "core" / "extractor_strategies.json"
    snap1.parent.mkdir(parents=True, exist_ok=True)

    payload1_old = {
        "classes": [{"class_name": "A", "decorators": [], "methods": []}],
        "functions": [],
    }
    payload2_old = {
        "classes": [{"class_name": "B", "decorators": [], "methods": []}],
        "functions": [],
    }

    snap1.write_text(
        json.dumps(
            {
                "_metadata": {"checksum": compute_payload_checksum(payload1_old)},
                **payload1_old,
            },
            indent=2,
        )
    )
    snap2.write_text(
        json.dumps(
            {
                "_metadata": {"checksum": compute_payload_checksum(payload2_old)},
                **payload2_old,
            },
            indent=2,
        )
    )

    payload1_new = {
        "classes": [{"class_name": "A_Modified", "decorators": [], "methods": []}],
        "functions": [],
    }
    payload2_new = {
        "classes": [{"class_name": "B_Modified", "decorators": [], "methods": []}],
        "functions": [],
    }

    changed_defs = {
        "cli": {},
        "core": {
            "app/core/analyzer_strategies.py": payload1_new,
            "app/core/extractor_strategies.py": payload2_new,
        },
    }

    monkeypatch.setattr(validate_signatures, "SNAPSHOT_DIR", str(fake_snap_dir))
    monkeypatch.setattr(validate_signatures, "SNAPSHOT_PATH", str(fake_snap_dir))
    monkeypatch.setattr(
        validate_signatures, "collect_current_definitions", lambda: changed_defs
    )
    monkeypatch.delenv("CI", raising=False)

    exited_code = None

    def mock_exit(code):
        nonlocal exited_code
        exited_code = code
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", mock_exit)
    monkeypatch.setattr(
        sys,
        "argv",
        ["validate_signatures.py", "--update", "app/core/analyzer_strategies.py"],
    )

    with pytest.raises(SystemExit) as exc_info:
        validate_signatures.main()
    assert exc_info.value.code == 0
    assert exited_code == 0

    # snap1 should be updated to payload1_new
    with open(snap1, "r", encoding="utf-8") as f:
        data1 = json.load(f)
    assert data1["classes"][0]["class_name"] == "A_Modified"

    # snap2 should NOT be updated (should still be payload2_old)
    with open(snap2, "r", encoding="utf-8") as f:
        data2 = json.load(f)
    assert data2["classes"][0]["class_name"] == "B"


def test_validation_ci_fails_on_regenerate(tmp_path, monkeypatch):
    fake_snap_dir = tmp_path / "api"
    fake_snap_dir.mkdir(parents=True)

    initial_defs = {"cli": {}, "core": {}}

    monkeypatch.setattr(validate_signatures, "SNAPSHOT_DIR", str(fake_snap_dir))
    monkeypatch.setattr(validate_signatures, "SNAPSHOT_PATH", str(fake_snap_dir))
    monkeypatch.setattr(
        validate_signatures, "collect_current_definitions", lambda: initial_defs
    )
    monkeypatch.setenv("CI", "true")

    exited_code = None

    def mock_exit(code):
        nonlocal exited_code
        exited_code = code
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", mock_exit)
    monkeypatch.setattr(sys, "argv", ["validate_signatures.py", "--update"])

    with pytest.raises(SystemExit) as exc_info:
        validate_signatures.main()
    assert exc_info.value.code == 1
    assert exited_code == 1


def test_validation_fails_on_missing_snapshot(tmp_path, monkeypatch):
    fake_snap_dir = tmp_path / "api"
    fake_snap_dir.mkdir(parents=True)

    current_defs = {
        "cli": {},
        "core": {"app/core/analyzer_strategies.py": {"classes": [], "functions": []}},
    }

    monkeypatch.setattr(validate_signatures, "SNAPSHOT_DIR", str(fake_snap_dir))
    monkeypatch.setattr(validate_signatures, "SNAPSHOT_PATH", str(fake_snap_dir))
    monkeypatch.setattr(
        validate_signatures, "collect_current_definitions", lambda: current_defs
    )
    monkeypatch.delenv("CI", raising=False)

    exited_code = None

    def mock_exit(code):
        nonlocal exited_code
        exited_code = code
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", mock_exit)
    monkeypatch.setattr(sys, "argv", ["validate_signatures.py"])

    with pytest.raises(SystemExit) as exc_info:
        validate_signatures.main()
    assert exc_info.value.code == 1
    assert exited_code == 1


def test_snapshot_integrity_checksum_mismatch(tmp_path, monkeypatch, capsys):
    fake_snap_dir = tmp_path / "api"
    fake_snap_dir.mkdir(parents=True)

    module_snap = fake_snap_dir / "core" / "analyzer_strategies.json"
    module_snap.parent.mkdir(parents=True, exist_ok=True)

    valid_payload = {"classes": [], "functions": []}
    valid_checksum = compute_payload_checksum(valid_payload)

    # Tampered payload in snapshot keeping old checksum
    tampered_data = {
        "_metadata": {
            "checksum": valid_checksum,
        },
        "classes": [{"class_name": "TamperedClass", "decorators": [], "methods": []}],
        "functions": [],
    }
    module_snap.write_text(json.dumps(tampered_data, indent=2, sort_keys=True))

    codebase_payload = {
        "classes": [{"class_name": "NewClass", "decorators": [], "methods": []}],
        "functions": [],
    }

    current_defs = {
        "cli": {},
        "core": {"app/core/analyzer_strategies.py": codebase_payload},
    }

    monkeypatch.setattr(validate_signatures, "SNAPSHOT_DIR", str(fake_snap_dir))
    monkeypatch.setattr(validate_signatures, "SNAPSHOT_PATH", str(fake_snap_dir))
    monkeypatch.setattr(
        validate_signatures, "collect_current_definitions", lambda: current_defs
    )

    exited_code = None

    def mock_exit(code):
        nonlocal exited_code
        exited_code = code
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", mock_exit)
    monkeypatch.setattr(sys, "argv", ["validate_signatures.py"])

    with pytest.raises(SystemExit) as exc_info:
        validate_signatures.main()

    assert exc_info.value.code == 1
    assert exited_code == 1

    captured = capsys.readouterr()
    assert (
        "Error: Snapshot file integrity verification failed! Checksum mismatch"
        in captured.err
    )
    assert "python scripts/validate_signatures.py --regenerate" in captured.err
    assert "Signature diff for 'app/core/analyzer_strategies.py'" in captured.err
    assert "NewClass" in captured.err


def test_snapshot_integrity_missing_metadata(tmp_path, monkeypatch):
    fake_snap_dir = tmp_path / "api"
    fake_snap_dir.mkdir(parents=True)

    module_snap = fake_snap_dir / "core" / "analyzer_strategies.json"
    module_snap.parent.mkdir(parents=True, exist_ok=True)

    data_without_meta = {"classes": [], "functions": []}
    module_snap.write_text(json.dumps(data_without_meta, indent=2, sort_keys=True))

    current_defs = {
        "cli": {},
        "core": {"app/core/analyzer_strategies.py": data_without_meta},
    }

    monkeypatch.setattr(validate_signatures, "SNAPSHOT_DIR", str(fake_snap_dir))
    monkeypatch.setattr(validate_signatures, "SNAPSHOT_PATH", str(fake_snap_dir))
    monkeypatch.setattr(
        validate_signatures, "collect_current_definitions", lambda: current_defs
    )

    exited_code = None

    def mock_exit(code):
        nonlocal exited_code
        exited_code = code
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", mock_exit)
    monkeypatch.setattr(sys, "argv", ["validate_signatures.py"])

    with pytest.raises(SystemExit) as exc_info:
        validate_signatures.main()

    assert exc_info.value.code == 1
    assert exited_code == 1


def test_validation_runner_detects_orphaned_snapshot(tmp_path, monkeypatch):
    fake_snap_dir = tmp_path / "api"
    fake_snap_dir.mkdir(parents=True)

    valid_snap = fake_snap_dir / "core" / "analyzer_strategies.json"
    orphaned_snap = fake_snap_dir / "core" / "deleted_module.json"
    valid_snap.parent.mkdir(parents=True, exist_ok=True)

    payload = {"classes": [], "functions": []}
    data = {"_metadata": {"checksum": compute_payload_checksum(payload)}, **payload}

    valid_snap.write_text(json.dumps(data, indent=2))
    orphaned_snap.write_text(json.dumps(data, indent=2))

    current_defs = {"cli": {}, "core": {"app/core/analyzer_strategies.py": payload}}

    monkeypatch.setattr(validate_signatures, "SNAPSHOT_DIR", str(fake_snap_dir))
    monkeypatch.setattr(validate_signatures, "SNAPSHOT_PATH", str(fake_snap_dir))
    monkeypatch.setattr(
        validate_signatures, "collect_current_definitions", lambda: current_defs
    )
    monkeypatch.delenv("CI", raising=False)

    exited_code = None

    def mock_exit(code):
        nonlocal exited_code
        exited_code = code
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", mock_exit)
    monkeypatch.setattr(sys, "argv", ["validate_signatures.py"])

    # Validation should detect orphan and fail
    with pytest.raises(SystemExit) as exc_info:
        validate_signatures.main()
    assert exc_info.value.code == 1

    # Regenerate should remove orphan
    monkeypatch.setattr(sys, "argv", ["validate_signatures.py", "--update"])
    with pytest.raises(SystemExit) as exc_info:
        validate_signatures.main()
    assert exc_info.value.code == 0
    assert not orphaned_snap.exists()
