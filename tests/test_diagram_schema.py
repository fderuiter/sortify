"""Unit tests for lazy diagram schema registry and module-level getattr resolution."""

import concurrent.futures
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

import scripts.diagram_schema as ds
from scripts.diagram_schema import (
    CONSTANT_TO_KEY_MAP,
    BaseDiagramSpec,
    ComponentDiagramSpec,
    DiagramNode,
    LazyDiagramSpecsDict,
    SequenceDiagramSpec,
    StateDiagramSpec,
    get_diagram_spec,
)


def _is_ci_or_parallel() -> bool:
    return (
        "PYTEST_XDIST_WORKER" in os.environ
        or "CI" in os.environ
        or os.environ.get("GITHUB_ACTIONS") == "true"
    )


def test_diagram_schema_import_performance():
    """Verify cold scripts.diagram_schema import in a fresh subprocess creates zero eager BaseDiagramSpec models."""
    repo_root = str(Path(__file__).resolve().parent.parent)
    probe = (
        "import json, sys, time\n"
        f"sys.path.insert(0, {repo_root!r})\n"
        "from pydantic import BaseModel, Field, field_validator\n"
        "t0 = time.perf_counter()\n"
        "import scripts.diagram_schema as ds\n"
        "import_time_ms = (time.perf_counter() - t0) * 1000\n"
        "eager_specs = [\n"
        "    k for k, v in vars(ds).items() if isinstance(v, ds.BaseDiagramSpec)\n"
        "]\n"
        "module_dir = dir(ds)\n"
        "raw_dir = ds.__dir__()\n"
        "print(json.dumps({\n"
        "    'import_time_ms': import_time_ms,\n"
        "    'eager_specs': eager_specs,\n"
        "    'instantiated_count': len(ds._INSTANTIATED_SPECS),\n"
        "    'dir_len': len(module_dir),\n"
        "    'dir_unique_len': len(set(module_dir)),\n"
        "    'raw_dir_len': len(raw_dir),\n"
        "    'raw_dir_unique_len': len(set(raw_dir)),\n"
        "}))\n"
    )
    clean_env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("COV_CORE_", "COVERAGE_"))
    }
    proc = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        check=True,
        env=clean_env,
    )
    payload = json.loads(proc.stdout.strip())

    assert payload["eager_specs"] == []
    assert payload["instantiated_count"] == 0
    assert payload["dir_len"] == payload["dir_unique_len"]
    assert payload["raw_dir_len"] == payload["raw_dir_unique_len"]

    sla_threshold = 250.0 if _is_ci_or_parallel() else 100.0
    assert payload["import_time_ms"] < sla_threshold, (
        f"Cold import time {payload['import_time_ms']:.2f}ms exceeded {sla_threshold}ms SLA threshold"
    )

    assert not any(isinstance(v, BaseDiagramSpec) for v in vars(ds).values())
    assert CONSTANT_TO_KEY_MAP.keys().isdisjoint(vars(ds).keys())


def test_lazy_instantiation_on_access():
    """Verify SYSTEM_DIAGRAM_SPECS lazily instantiates models on demand and memoizes result."""
    ds.reset_diagram_specs_cache()
    assert len(ds._INSTANTIATED_SPECS) == 0

    # Access a spec via dictionary indexing
    spec1 = ds.SYSTEM_DIAGRAM_SPECS["core_architecture"]
    assert isinstance(spec1, ComponentDiagramSpec)
    assert spec1.id == "core_architecture"
    assert len(ds._INSTANTIATED_SPECS) == 1

    # Access the same spec again; must return identical memoized instance
    spec2 = ds.SYSTEM_DIAGRAM_SPECS["core_architecture"]
    assert spec1 is spec2
    assert len(ds._INSTANTIATED_SPECS) == 1


def test_module_getattr_spec_resolution():
    """Verify module-level __getattr__ intercepts constant accesses, populates _INSTANTIATED_SPECS, and deduplicates __dir__."""
    assert not any(isinstance(v, BaseDiagramSpec) for v in vars(ds).values())
    assert len(dir(ds)) == len(set(dir(ds)))
    assert len(ds.__dir__()) == len(set(ds.__dir__()))
    assert "SYSTEM_DIAGRAM_SPECS" in dir(ds)
    assert set(CONSTANT_TO_KEY_MAP).issubset(dir(ds))

    ds.reset_diagram_specs_cache()
    assert len(ds._INSTANTIATED_SPECS) == 0

    # Access via getattr / module import and confirm _INSTANTIATED_SPECS hydration
    arch_spec = getattr(ds, "CORE_ARCHITECTURE_SPEC")
    assert isinstance(arch_spec, ComponentDiagramSpec)
    assert arch_spec.id == "core_architecture"
    assert set(ds._INSTANTIATED_SPECS) == {"core_architecture"}

    cro_spec = getattr(ds, "CRO_MULTI_STUDY_PIPELINE_SPEC")
    assert isinstance(cro_spec, ComponentDiagramSpec)
    assert cro_spec.id == "cro_multi_study_pipeline"
    assert "cro_multi_study_pipeline" in ds._INSTANTIATED_SPECS

    seq_spec = getattr(ds, "ARCHITECTURE_ASYNC_PROCESSING_SPEC")
    assert isinstance(seq_spec, SequenceDiagramSpec)
    assert seq_spec.id == "architecture_async_processing"
    assert "architecture_async_processing" in ds._INSTANTIATED_SPECS

    state_spec = getattr(ds, "ARCHITECTURE_WATCHDOG_STATE_SPEC")
    assert isinstance(state_spec, StateDiagramSpec)
    assert state_spec.id == "architecture_watchdog_state"
    assert "architecture_watchdog_state" in ds._INSTANTIATED_SPECS

    move_spec = getattr(ds, "ARCHITECTURE_MOVE_ENGINE_STATE_SPEC")
    assert isinstance(move_spec, StateDiagramSpec)
    assert move_spec.id == "architecture_move_engine_state"
    assert getattr(ds, "MOVE_ENGINE_STATE_SPEC") is move_spec

    pipe_spec = getattr(ds, "ARCHITECTURE_PIPELINE_STATE_SPEC")
    assert isinstance(pipe_spec, StateDiagramSpec)
    assert pipe_spec.id == "architecture_pipeline_state"
    assert getattr(ds, "PIPELINE_LIFECYCLE_STATE_SPEC") is pipe_spec

    sess_spec = getattr(ds, "ARCHITECTURE_SESSION_STATE_SPEC")
    assert isinstance(sess_spec, StateDiagramSpec)
    assert sess_spec.id == "architecture_session_state"
    assert getattr(ds, "SESSION_STATE_MACHINE_SPEC") is sess_spec

    dl_spec = getattr(ds, "ARCHITECTURE_DOWNLOAD_STATE_SPEC")
    assert isinstance(dl_spec, StateDiagramSpec)
    assert dl_spec.id == "architecture_download_state"
    assert getattr(ds, "DOWNLOAD_STATE_SPEC") is dl_spec

    # Verify all 52 aliases in CONSTANT_TO_KEY_MAP hydrate _INSTANTIATED_SPECS and match get_diagram_spec
    ds.reset_diagram_specs_cache()
    assert len(ds._INSTANTIATED_SPECS) == 0
    for alias, spec_key in CONSTANT_TO_KEY_MAP.items():
        assert alias not in vars(ds)
        resolved = getattr(ds, alias)
        assert spec_key in ds._INSTANTIATED_SPECS
        assert resolved is ds._INSTANTIATED_SPECS[spec_key]
        assert resolved is get_diagram_spec(spec_key)
    assert len(ds._INSTANTIATED_SPECS) == len(ds._SPEC_FACTORIES) == 48

    # Verify accessing non-existent attribute raises AttributeError
    with pytest.raises(
        AttributeError, match="has no attribute 'NON_EXISTENT_CONSTANT'"
    ):
        _ = getattr(ds, "NON_EXISTENT_CONSTANT")


def test_lazy_dict_interface():
    """Verify LazyDiagramSpecsDict provides complete dictionary interface compliance."""
    specs_dict = ds.SYSTEM_DIAGRAM_SPECS
    assert isinstance(specs_dict, LazyDiagramSpecsDict)
    assert isinstance(specs_dict, dict)

    # Length & Containment
    assert len(specs_dict) == 48
    assert "core_architecture" in specs_dict
    assert "architecture_move_engine_state" in specs_dict
    assert "architecture_pipeline_state" in specs_dict
    assert "architecture_session_state" in specs_dict
    assert "architecture_download_state" in specs_dict
    assert "non_existent_key" not in specs_dict

    # .get() behavior
    default_marker = object()
    assert specs_dict.get("core_architecture") is not None
    assert specs_dict.get("non_existent_key", default_marker) is default_marker

    # Keys, Values, Items
    all_keys = list(specs_dict.keys())
    assert "core_architecture" in all_keys
    assert len(all_keys) == 48

    all_items = list(specs_dict.items())
    assert len(all_items) == 48
    assert any(
        k == "core_architecture" and v.id == "core_architecture" for k, v in all_items
    )

    all_values = list(specs_dict.values())
    assert len(all_values) == 48

    # .copy()
    dict_copy = specs_dict.copy()
    assert isinstance(dict_copy, dict)
    assert len(dict_copy) == 48
    assert dict_copy["core_architecture"].id == "core_architecture"

    # Repr string
    repr_str = repr(specs_dict)
    assert "LazyDiagramSpecsDict" in repr_str


def test_thread_safe_memoization():
    """Verify thread-safe memoization cache under concurrent multi-threaded access."""
    ds.reset_diagram_specs_cache()

    keys = list(ds._SPEC_FACTORIES.keys())

    def worker_access(key: str):
        # Access both via dict and via get_diagram_spec
        s1 = ds.SYSTEM_DIAGRAM_SPECS[key]
        s2 = get_diagram_spec(key)
        assert s1 is s2
        return s1

    # Execute concurrent requests across threads
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(worker_access, keys[i % len(keys)]) for i in range(100)
        ]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]

    assert len(results) == 100
    assert len(ds._INSTANTIATED_SPECS) == len(keys)


def test_pydantic_validation_strictness():
    """Verify Pydantic field validation rules remain strict during model instantiation."""
    # Valid node
    valid_node = DiagramNode(id="n1", label="Node 1", url="https://example.com/doc")
    assert valid_node.url == "https://example.com/doc"

    # Unsafe javascript URI scheme must trigger ValueError / ValidationError
    with pytest.raises(ValidationError, match="Unsafe or invalid URI scheme"):
        DiagramNode(id="n2", label="Unsafe Node", url="javascript:alert('xss')")

    # Disallowed URI scheme must trigger ValueError / ValidationError
    with pytest.raises(ValidationError, match="Disallowed URI scheme"):
        DiagramNode(id="n3", label="Bad Scheme Node", url="customscheme://invalid")


def test_mermaid_compilation():
    """Verify hydrated diagram specs compile correctly to Mermaid markup string."""
    arch = ds.SYSTEM_DIAGRAM_SPECS["core_architecture"]
    mermaid_text = arch.to_mermaid()
    assert "flowchart TD" in mermaid_text
    assert "app_main" in mermaid_text

    seq = ds.SYSTEM_DIAGRAM_SPECS["architecture_async_processing"]
    seq_mermaid = seq.to_mermaid()
    assert "sequenceDiagram" in seq_mermaid
    assert "autonumber" in seq_mermaid

    state = ds.SYSTEM_DIAGRAM_SPECS["architecture_watchdog_state"]
    state_mermaid = state.to_mermaid()
    assert "stateDiagram-v2" in state_mermaid

    mover_state = ds.SYSTEM_DIAGRAM_SPECS["architecture_move_engine_state"]
    mover_mermaid = mover_state.to_mermaid()
    assert "stateDiagram-v2" in mover_mermaid
    assert "MOVING_CHUNK" in mover_mermaid

    pipeline_state = ds.SYSTEM_DIAGRAM_SPECS["architecture_pipeline_state"]
    pipeline_mermaid = pipeline_state.to_mermaid()
    assert "stateDiagram-v2" in pipeline_mermaid
    assert "ROLLING_BACK" in pipeline_mermaid

    session_state = ds.SYSTEM_DIAGRAM_SPECS["architecture_session_state"]
    session_mermaid = session_state.to_mermaid()
    assert "stateDiagram-v2" in session_mermaid
    assert "undo_snapshot" in session_mermaid

    download_state = ds.SYSTEM_DIAGRAM_SPECS["architecture_download_state"]
    download_mermaid = download_state.to_mermaid()
    assert "stateDiagram-v2" in download_mermaid
    assert "DOWNLOADING" in download_mermaid
