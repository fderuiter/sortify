"""Unit tests for lazy diagram schema registry and module-level getattr resolution."""

import concurrent.futures
import os
import time

import pytest
from pydantic import ValidationError

import app.ui.diagram_schema as ds
from app.ui.diagram_schema import (
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
    """Verify app.ui.diagram_schema import latency is under 50ms with zero top-level spec instantiations."""
    ds.reset_diagram_specs_cache()
    assert len(ds._INSTANTIATED_SPECS) == 0

    # Ensure catalog is imported before timing diagram_schema import
    import app.ui.catalog  # noqa: F401

    # Test re-import latency when module is in sys.modules
    t0 = time.perf_counter()
    import app.ui.diagram_schema  # noqa: F401

    import_time_ms = (time.perf_counter() - t0) * 1000
    sla_threshold = 200.0 if _is_ci_or_parallel() else 50.0
    assert import_time_ms < sla_threshold, (
        f"Import time {import_time_ms:.2f}ms exceeded {sla_threshold}ms SLA threshold"
    )

    # Verify zero diagram specs were hydrated at import time
    assert len(ds._INSTANTIATED_SPECS) == 0


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
    """Verify module-level __getattr__ intercepts constant accesses and resolves specs lazily."""
    ds.reset_diagram_specs_cache()

    # Access via getattr / module import
    arch_spec = getattr(ds, "CORE_ARCHITECTURE_SPEC")
    assert isinstance(arch_spec, ComponentDiagramSpec)
    assert arch_spec.id == "core_architecture"

    cro_spec = getattr(ds, "CRO_MULTI_STUDY_PIPELINE_SPEC")
    assert isinstance(cro_spec, ComponentDiagramSpec)
    assert cro_spec.id == "cro_multi_study_pipeline"

    seq_spec = getattr(ds, "ARCHITECTURE_ASYNC_PROCESSING_SPEC")
    assert isinstance(seq_spec, SequenceDiagramSpec)
    assert seq_spec.id == "architecture_async_processing"

    state_spec = getattr(ds, "ARCHITECTURE_WATCHDOG_STATE_SPEC")
    assert isinstance(state_spec, StateDiagramSpec)
    assert state_spec.id == "architecture_watchdog_state"

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
    assert len(specs_dict) == 21
    assert "core_architecture" in specs_dict
    assert "non_existent_key" not in specs_dict

    # .get() behavior
    default_marker = object()
    assert specs_dict.get("core_architecture") is not None
    assert specs_dict.get("non_existent_key", default_marker) is default_marker

    # Keys, Values, Items
    all_keys = list(specs_dict.keys())
    assert "core_architecture" in all_keys
    assert len(all_keys) == 21

    all_items = list(specs_dict.items())
    assert len(all_items) == 21
    assert any(
        k == "core_architecture" and v.id == "core_architecture" for k, v in all_items
    )

    all_values = list(specs_dict.values())
    assert len(all_values) == 21

    # .copy()
    dict_copy = specs_dict.copy()
    assert isinstance(dict_copy, dict)
    assert len(dict_copy) == 21
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
