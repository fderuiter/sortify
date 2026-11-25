"""Declarative Pydantic schema models for application, sequence, and state diagrams."""

import ast
import os
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "file"}


_EDGE_LABEL_SPECIAL_CHARS = set("()[]{}|<>")


def _edge_label(label: str) -> str:
    """Quote a flowchart edge label when it contains Mermaid shape delimiters."""
    if any(ch in _EDGE_LABEL_SPECIAL_CHARS for ch in label):
        return '"' + label.replace('"', "#quot;") + '"'
    return label


class DiagramNode(BaseModel, defer_build=True):
    """Specification model for a single diagram node vertex."""

    id: str
    label: str
    shape: str | None = "rectangle"
    style: str | None = None
    url: str | None = None
    tooltip: str | None = None
    target: str | None = None
    step_number: int | None = None
    step_description: str | None = None

    @field_validator("url")
    @classmethod
    def validate_url_scheme(cls, v: str | None) -> str | None:
        """Validate URL target scheme to block invalid or unsafe URI schemes."""
        if not v:
            return v
        cleaned = v.strip()
        if not cleaned:
            return v

        lower_url = cleaned.lower()
        if lower_url.startswith(("javascript:", "data:", "vbscript:")):
            raise ValueError(f"Unsafe or invalid URI scheme in URL target: {v}")

        if ":" in cleaned and not cleaned.startswith("#"):
            scheme = cleaned.split(":", 1)[0].lower()
            if (
                scheme.isalpha()
                and len(scheme) > 1
                and "/" not in scheme
                and scheme not in ALLOWED_URL_SCHEMES
            ):
                raise ValueError(f"Disallowed URI scheme '{scheme}' in URL target: {v}")
        return v


class DiagramEdge(BaseModel, defer_build=True):
    """Specification model for a directional edge link between diagram nodes."""

    source: str
    target: str
    label: str | None = None
    arrow_type: str = "-->"


class DiagramSubgraph(BaseModel, defer_build=True):
    """Specification model for a subgraph boundary container in a diagram."""

    id: str
    title: str
    nodes: list[str] = Field(default_factory=list)


class ComponentDiagramSpec(BaseModel, defer_build=True):
    """Declarative specification model for component and system diagrams."""

    id: str
    title: str
    diagram_type: str = "graph"
    direction: Literal["TD", "LR", "BT", "RL"] = "TD"
    nodes: list[DiagramNode] = Field(default_factory=list)
    edges: list[DiagramEdge] = Field(default_factory=list)
    subgraphs: list[DiagramSubgraph] = Field(default_factory=list)

    def to_mermaid(self) -> str:
        """Compile diagram specification object into standard Mermaid markup string."""
        if self.diagram_type in ("graph", "flowchart"):
            lines = [f"{self.diagram_type} {self.direction}"]

            def format_node(node: DiagramNode) -> str:
                s = node.shape or "rectangle"
                lbl = node.label.replace('"', '\\"')
                if s in ("round", "rounded"):
                    return f'{node.id}("{lbl}")'
                elif s == "database":
                    return f'{node.id}[("{lbl}")]'
                elif s in ("rhombus", "decision"):
                    return f"{node.id}{{{lbl}}}"
                elif s in ("stadium",):
                    return f'{node.id}(["{lbl}"])'
                elif s == "subroutine":
                    return f'{node.id}[["{lbl}"]]'
                else:  # rectangle
                    return f'{node.id}["{lbl}"]'

            subgraph_node_ids = set()
            for sub in self.subgraphs:
                lines.append(f'    subgraph {sub.id} ["{sub.title}"]')
                for nid in sub.nodes:
                    subgraph_node_ids.add(nid)
                    node = next((n for n in self.nodes if n.id == nid), None)
                    if node:
                        lines.append(f"        {format_node(node)}")
                    else:
                        lines.append(f"        {nid}")
                lines.append("    end")

            for node in self.nodes:
                if node.id not in subgraph_node_ids:
                    lines.append(f"    {format_node(node)}")

            for edge in self.edges:
                lbl_part = f"|{_edge_label(edge.label)}|" if edge.label else ""
                lines.append(
                    f"    {edge.source} {edge.arrow_type}{lbl_part} {edge.target}"
                )

            for node in self.nodes:
                if node.style:
                    lines.append(f"    style {node.id} {node.style}")

            for node in self.nodes:
                tooltip_str = node.tooltip
                if not tooltip_str:
                    if node.step_number is not None and node.step_description:
                        tooltip_str = (
                            f"Step {node.step_number}: {node.step_description}"
                        )
                    elif node.step_description:
                        tooltip_str = node.step_description
                    elif node.step_number is not None:
                        tooltip_str = f"Step {node.step_number}"

                if node.url or tooltip_str:
                    if node.url:
                        clean_url = node.url.replace('"', '\\"')
                        if tooltip_str:
                            clean_tip = tooltip_str.replace('"', '\\"')
                            if node.target:
                                lines.append(
                                    f'    click {node.id} "{clean_url}" "{clean_tip}" {node.target}'
                                )
                            else:
                                lines.append(
                                    f'    click {node.id} "{clean_url}" "{clean_tip}"'
                                )
                        else:
                            if node.target:
                                lines.append(
                                    f'    click {node.id} "{clean_url}" {node.target}'
                                )
                            else:
                                lines.append(f'    click {node.id} "{clean_url}"')
                    elif tooltip_str:
                        clean_tip = tooltip_str.replace('"', '\\"')
                        lines.append(f'    click {node.id} tooltip "{clean_tip}"')

            return "\n".join(lines) + "\n"

        return f"{self.diagram_type}\n"


# Alias for backwards compatibility / alternate naming
DiagramSpec = ComponentDiagramSpec


# Sequence Diagram Models
class SequenceParticipant(BaseModel, defer_build=True):
    """Participant or actor node in a sequence diagram."""

    id: str
    label: str | None = None
    is_actor: bool = False


class SequenceMessage(BaseModel, defer_build=True):
    """Directional message between participants in a sequence diagram."""

    kind: Literal["message"] = "message"
    source: str
    target: str
    text: str
    arrow_type: str = "->>"


class SequenceActivation(BaseModel, defer_build=True):
    """Activation or deactivation of a participant lifeline."""

    kind: Literal["activation"] = "activation"
    target: str
    action: Literal["activate", "deactivate"]


class SequenceNote(BaseModel, defer_build=True):
    """Note block in a sequence diagram."""

    kind: Literal["note"] = "note"
    position: str = "over"  # "over", "left of", "right of"
    targets: list[str] = Field(default_factory=list)
    text: str


class SequenceLoop(BaseModel, defer_build=True):
    """Loop block in a sequence diagram."""

    kind: Literal["loop"] = "loop"
    label: str
    items: list["SequenceItem"] = Field(default_factory=list)


class SequenceOpt(BaseModel, defer_build=True):
    """Optional block in a sequence diagram."""

    kind: Literal["opt"] = "opt"
    label: str
    items: list["SequenceItem"] = Field(default_factory=list)


class SequenceAltBranch(BaseModel, defer_build=True):
    """Branch section in an alt/else block."""

    label: str
    items: list["SequenceItem"] = Field(default_factory=list)


class SequenceAlt(BaseModel, defer_build=True):
    """Alternative/Else block in a sequence diagram."""

    kind: Literal["alt"] = "alt"
    branches: list[SequenceAltBranch] = Field(default_factory=list)


SequenceItem = (
    SequenceMessage
    | SequenceActivation
    | SequenceNote
    | SequenceLoop
    | SequenceOpt
    | SequenceAlt
)

# deferred model rebuilds


class SequenceDiagramSpec(BaseModel, defer_build=True):
    """Declarative specification model for sequence diagrams."""

    id: str
    title: str
    autonumber: bool = False
    participants: list[SequenceParticipant] = Field(default_factory=list)
    items: list[SequenceItem] = Field(default_factory=list)

    def to_mermaid(self) -> str:
        """Compile sequence diagram specification object into standard Mermaid markup string."""
        lines = ["sequenceDiagram"]
        if self.autonumber:
            lines.append("    autonumber")

        for p in self.participants:
            kw = "actor" if p.is_actor else "participant"
            if p.label:
                lines.append(f"    {kw} {p.id} as {p.label}")
            else:
                lines.append(f"    {kw} {p.id}")

        def compile_items(item_list: list[SequenceItem], indent_level: int = 1) -> None:
            indent = "    " * indent_level
            for item in item_list:
                if isinstance(item, SequenceMessage):
                    lines.append(
                        f"{indent}{item.source}{item.arrow_type}{item.target}: {item.text}"
                    )
                elif isinstance(item, SequenceActivation):
                    lines.append(f"{indent}{item.action} {item.target}")
                elif isinstance(item, SequenceNote):
                    targets_str = ", ".join(item.targets)
                    lines.append(
                        f"{indent}note {item.position} {targets_str}: {item.text}"
                    )
                elif isinstance(item, SequenceLoop):
                    lines.append(f"{indent}loop {item.label}")
                    compile_items(item.items, indent_level + 1)
                    lines.append(f"{indent}end")
                elif isinstance(item, SequenceOpt):
                    lines.append(f"{indent}opt {item.label}")
                    compile_items(item.items, indent_level + 1)
                    lines.append(f"{indent}end")
                elif isinstance(item, SequenceAlt):
                    if item.branches:
                        lines.append(f"{indent}alt {item.branches[0].label}")
                        compile_items(item.branches[0].items, indent_level + 1)
                        for branch in item.branches[1:]:
                            lines.append(f"{indent}else {branch.label}")
                            compile_items(branch.items, indent_level + 1)
                        lines.append(f"{indent}end")

        compile_items(self.items)
        return "\n".join(lines) + "\n"


# State Diagram Models
class StateNode(BaseModel, defer_build=True):
    """Specification model for a state in a state diagram."""

    id: str
    label: str | None = None
    is_choice: bool = False


class StateTransition(BaseModel, defer_build=True):
    """Specification model for a state transition in a state diagram."""

    source: str
    target: str
    label: str | None = None


class StateComposite(BaseModel, defer_build=True):
    """Specification model for a composite state container."""

    id: str
    label: str | None = None
    states: list[StateNode] = Field(default_factory=list)
    transitions: list[StateTransition] = Field(default_factory=list)


class StateNote(BaseModel, defer_build=True):
    """Specification model for a note in a state diagram."""

    position: str = "left of"  # "left of", "right of"
    target: str
    text: str


class StateDiagramSpec(BaseModel, defer_build=True):
    """Declarative specification model for state transition diagrams."""

    id: str
    title: str
    diagram_type: str = "stateDiagram-v2"
    direction: Literal["TB", "LR", "BT", "RL"] | None = None
    states: list[StateNode] = Field(default_factory=list)
    transitions: list[StateTransition] = Field(default_factory=list)
    composite_states: list[StateComposite] = Field(default_factory=list)
    notes: list[StateNote] = Field(default_factory=list)

    def to_mermaid(self) -> str:
        """Compile state diagram specification object into standard Mermaid markup string."""
        lines = [self.diagram_type]
        if self.direction:
            lines.append(f"    direction {self.direction}")

        for state in self.states:
            if state.is_choice:
                lines.append(f"    state {state.id} <<choice>>")
            elif state.label and state.id != "[*]":
                lines.append(f"    {state.id}: {state.label}")

        for comp in self.composite_states:
            header = f"state {comp.id}"
            if comp.label:
                header += f" [{comp.label}]"
            lines.append(f"    {header} {{")
            for state in comp.states:
                if state.is_choice:
                    lines.append(f"        state {state.id} <<choice>>")
                elif state.label and state.id != "[*]":
                    lines.append(f"        {state.id}: {state.label}")
            for trans in comp.transitions:
                lbl = f": {trans.label}" if trans.label else ""
                lines.append(f"        {trans.source} --> {trans.target}{lbl}")
            lines.append("    }")

        for trans in self.transitions:
            lbl = f": {trans.label}" if trans.label else ""
            lines.append(f"    {trans.source} --> {trans.target}{lbl}")

        for note in self.notes:
            lines.append(f"    note {note.position} {note.target}: {note.text}")

        return "\n".join(lines) + "\n"


# Data Flow Diagram (DFD) Models
class DataStoreNode(DiagramNode):
    """Specification model for a data store node in a Data Flow Diagram."""

    shape: str | None = "database"


class ProcessNode(DiagramNode):
    """Specification model for a process transform node in a Data Flow Diagram."""

    shape: str | None = "stadium"


class ExternalEntityNode(DiagramNode):
    """Specification model for an external entity node in a Data Flow Diagram."""

    shape: str | None = "rectangle"


class DataStreamEdge(DiagramEdge):
    """Specification model for a data stream contract edge in a Data Flow Diagram."""

    contract: str | None = None


class DataFlowDiagramSpec(BaseModel):
    """Declarative specification model for Data Flow Diagrams (DFD)."""

    id: str
    title: str
    diagram_type: str = "flowchart"
    direction: Literal["TD", "LR", "BT", "RL"] = "TD"
    nodes: list[DataStoreNode | ProcessNode | ExternalEntityNode | DiagramNode] = Field(
        default_factory=list
    )
    edges: list[DataStreamEdge | DiagramEdge] = Field(default_factory=list)
    subgraphs: list[DiagramSubgraph] = Field(default_factory=list)

    def to_mermaid(self) -> str:
        """Compile DFD specification object into standard Mermaid flowchart markup string."""
        if self.diagram_type in ("graph", "flowchart"):
            lines = [f"{self.diagram_type} {self.direction}"]

            def format_node(
                node: DataStoreNode | ProcessNode | ExternalEntityNode | DiagramNode,
            ) -> str:
                s = node.shape or "rectangle"
                if isinstance(node, DataStoreNode) and not node.shape:
                    s = "database"
                elif isinstance(node, ProcessNode) and not node.shape:
                    s = "stadium"
                elif isinstance(node, ExternalEntityNode) and not node.shape:
                    s = "rectangle"

                lbl = node.label.replace('"', '\\"')
                if s in ("database", "cylinder"):
                    return f'{node.id}[("{lbl}")]'
                elif s in ("stadium", "process"):
                    return f'{node.id}(["{lbl}"])'
                elif s in ("round", "rounded"):
                    return f'{node.id}("{lbl}")'
                elif s in ("rhombus", "decision"):
                    return f"{node.id}{{{lbl}}}"
                elif s in ("subroutine",):
                    return f'{node.id}[["{lbl}"]]'
                else:  # rectangle / default / external_entity
                    return f'{node.id}["{lbl}"]'

            subgraph_node_ids = set()
            for sub in self.subgraphs:
                lines.append(f'    subgraph {sub.id} ["{sub.title}"]')
                for nid in sub.nodes:
                    subgraph_node_ids.add(nid)
                    node = next((n for n in self.nodes if n.id == nid), None)
                    if node:
                        lines.append(f"        {format_node(node)}")
                    else:
                        lines.append(f"        {nid}")
                lines.append("    end")

            for node in self.nodes:
                if node.id not in subgraph_node_ids:
                    lines.append(f"    {format_node(node)}")

            for edge in self.edges:
                lbl_text = edge.label
                if hasattr(edge, "contract") and edge.contract:
                    if lbl_text:
                        lbl_text = f"{lbl_text}: {edge.contract}"
                    else:
                        lbl_text = edge.contract
                lbl_part = f"|{_edge_label(str(lbl_text))}|" if lbl_text else ""
                lines.append(
                    f"    {edge.source} {edge.arrow_type}{lbl_part} {edge.target}"
                )

            for node in self.nodes:
                if node.style:
                    lines.append(f"    style {node.id} {node.style}")

            for node in self.nodes:
                tooltip_str = node.tooltip
                if not tooltip_str:
                    if node.step_number is not None and node.step_description:
                        tooltip_str = (
                            f"Step {node.step_number}: {node.step_description}"
                        )
                    elif node.step_description:
                        tooltip_str = node.step_description
                    elif node.step_number is not None:
                        tooltip_str = f"Step {node.step_number}"

                if node.url or tooltip_str:
                    if node.url:
                        clean_url = node.url.replace('"', '\\"')
                        if tooltip_str:
                            clean_tip = tooltip_str.replace('"', '\\"')
                            if node.target:
                                lines.append(
                                    f'    click {node.id} "{clean_url}" "{clean_tip}" {node.target}'
                                )
                            else:
                                lines.append(
                                    f'    click {node.id} "{clean_url}" "{clean_tip}"'
                                )
                        else:
                            if node.target:
                                lines.append(
                                    f'    click {node.id} "{clean_url}" {node.target}'
                                )
                            else:
                                lines.append(f'    click {node.id} "{clean_url}"')
                    elif tooltip_str:
                        clean_tip = tooltip_str.replace('"', '\\"')
                        lines.append(f'    click {node.id} tooltip "{clean_tip}"')

            return "\n".join(lines) + "\n"

        return f"{self.diagram_type}\n"


BaseDiagramSpec = (
    ComponentDiagramSpec | SequenceDiagramSpec | StateDiagramSpec | DataFlowDiagramSpec
)


_CORE_SUBGRAPH_DEFINITIONS: tuple[tuple[str, str], ...] = (
    ("ui_layer", "Presentation & User Interfaces (app.tui)"),
    ("session_orchestration", "Session Orchestration & Lifecycle (app.core)"),
    ("ingestion_extraction", "Ingestion & Extraction Engine (app.core)"),
    ("analytics_intelligence", "Analytics & Machine Learning (app.core)"),
    ("plugin_extensibility", "Plugin Extensibility Architecture (app.core)"),
    ("memory_cache", "Caching & Memory Layer (app.core)"),
    ("concurrency_shared", "Concurrency & Shared Registry (app.core)"),
    ("storage_persistence", "Storage & Database Persistence (app.core)"),
    ("execution_operations", "Policy, Validation & File Operations (app.core)"),
    ("utilities_security", "Security & Utilities (app.core)"),
)

_CORE_SUBGRAPH_ASSIGNMENTS: dict[str, str] = {
    # Session Orchestration & Lifecycle
    "pipeline": "session_orchestration",
    "lifecycle": "session_orchestration",
    "folder_lock": "session_orchestration",
    "session": "session_orchestration",
    "services": "session_orchestration",
    "daemon": "session_orchestration",
    "ipc": "session_orchestration",
    "domain_contracts": "session_orchestration",
    # Ingestion & Extraction
    "extractor": "ingestion_extraction",
    "extractor_strategies": "ingestion_extraction",
    "forensic_scanner": "ingestion_extraction",
    "offline_loader": "ingestion_extraction",
    "downloader": "ingestion_extraction",
    # Analytics & ML
    "analyzer": "analytics_intelligence",
    "analyzer_strategies": "analytics_intelligence",
    "jev_classifier": "analytics_intelligence",
    "semantic_embeddings": "analytics_intelligence",
    # Plugin Extensibility
    "plugin_registry": "plugin_extensibility",
    # Caching & Memory
    "cache": "memory_cache",
    "db_conn": "memory_cache",
    "link_manager": "memory_cache",
    "hashes_registry": "memory_cache",
    # Concurrency & Shared Registry
    "shared_registry": "concurrency_shared",
    "model_registry": "concurrency_shared",
    "worker_pool": "concurrency_shared",
    "concurrency": "concurrency_shared",
    "network_guard": "concurrency_shared",
    "db_worker": "concurrency_shared",
    # Storage & Database Persistence
    "store": "storage_persistence",
    "db": "storage_persistence",
    "migrations": "storage_persistence",
    "moves": "storage_persistence",
    "ledger": "storage_persistence",
    "history": "storage_persistence",
    "session_catalog": "storage_persistence",
    # Policy, Validation & File Operations
    "policy_engine": "execution_operations",
    "rule_scopes": "execution_operations",
    "rule_templates": "execution_operations",
    "rule_importers": "execution_operations",
    "quarantine_interceptor": "execution_operations",
    "verifier": "execution_operations",
    "simulation_exporter": "execution_operations",
    "audit_reporter": "execution_operations",
    "accessible_html": "execution_operations",
    "mover": "execution_operations",
    "move_retry": "execution_operations",
    "file_renamer": "execution_operations",
    "pattern_formatter": "execution_operations",
    "resilient_file_ops": "execution_operations",
    "scanner": "execution_operations",
    "progress": "execution_operations",
    "metadata": "execution_operations",
}

_CORE_NODE_DISPLAY_OVERRIDES: dict[str, tuple[str, str]] = {
    "cache": ("app.core.cache (BoundedMemoryCache)", "database"),
    "shared_registry": ("app.core.shared_registry (re-exports)", "rectangle"),
    "worker_pool": ("app.core.worker_pool (SharedWorkerPool)", "rectangle"),
    "store": ("app.core.store", "database"),
    "db": ("app.core.db", "database"),
    "moves": ("app.core.moves", "database"),
    "ledger": ("app.core.ledger", "database"),
    "history": ("app.core.history", "database"),
    "policy_engine": ("app.core.policy_engine", "rhombus"),
    "verifier": ("app.core.verifier", "rhombus"),
}


def _classify_core_stem_subgraph(stem: str) -> str:
    """Assign an app/core/<stem>.py module to an architectural subgraph deterministically."""
    if stem in _CORE_SUBGRAPH_ASSIGNMENTS:
        return _CORE_SUBGRAPH_ASSIGNMENTS[stem]
    if stem.startswith(("extractor", "ingest", "download", "offline")):
        return "ingestion_extraction"
    if stem.startswith(("analyzer", "semantic", "embedding", "classifier", "ml_")):
        return "analytics_intelligence"
    if stem.startswith("plugin"):
        return "plugin_extensibility"
    if stem.startswith(("cache", "hash")):
        return "memory_cache"
    if stem.startswith(("worker", "concurren", "registry")):
        return "concurrency_shared"
    if stem.startswith(("db_", "store", "ledger", "history", "migrat")):
        return "storage_persistence"
    if stem.startswith(
        ("policy", "rule_", "quarantine", "move", "verif", "scan", "renam")
    ):
        return "execution_operations"
    if stem.startswith(
        ("session", "pipeline", "daemon", "ipc", "service", "lifecycle")
    ):
        return "session_orchestration"
    return "utilities_security"


def _source_node_id_for_path(norm_path: str, valid_core_stems: set[str]) -> str | None:
    """Map a normalized repository-relative Python path to its core_architecture node ID."""
    if norm_path == "app/main.py" or norm_path.startswith("app/cli/"):
        return "app_main"
    if norm_path.startswith("app/ui/"):
        return "app_tui"
    if norm_path.startswith("app/core/") and "/" not in norm_path[len("app/core/") :]:
        stem = Path(norm_path).stem
        if stem in valid_core_stems:
            return f"core_{stem}"
    return None


def _extract_app_import_edges_iterative(
    norm_path: str,
    tree: ast.Module,
    valid_core_stems: set[str],
) -> set[tuple[str, str]]:
    """Extract (source_node_id, target_node_id) dependency edges without calling ast.walk."""
    source_id = _source_node_id_for_path(norm_path, valid_core_stems)
    if source_id is None:
        return set()

    in_core_pkg = norm_path.startswith("app/core/")
    discovered: set[tuple[str, str]] = set()
    stack: list[ast.AST] = [tree]

    while stack:
        node = stack.pop()
        if isinstance(node, ast.Import):
            for alias in node.names:
                mod = alias.name
                if mod == "app.ui" or mod.startswith("app.ui."):
                    if source_id != "app_tui":
                        discovered.add((source_id, "app_tui"))
                elif mod.startswith("app.core."):
                    stem = mod.split(".")[2]
                    if stem in valid_core_stems and source_id != f"core_{stem}":
                        discovered.add((source_id, f"core_{stem}"))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mod = node.module
                if mod == "app.ui" or mod.startswith("app.ui."):
                    if source_id != "app_tui":
                        discovered.add((source_id, "app_tui"))
                elif mod == "app.core":
                    for alias in node.names:
                        stem = alias.name
                        if stem in valid_core_stems and source_id != f"core_{stem}":
                            discovered.add((source_id, f"core_{stem}"))
                elif mod.startswith("app.core."):
                    stem = mod.split(".")[2]
                    if stem in valid_core_stems and source_id != f"core_{stem}":
                        discovered.add((source_id, f"core_{stem}"))
            elif node.level == 1 and in_core_pkg:
                if node.module:
                    stem = node.module.split(".")[0]
                    if stem in valid_core_stems and source_id != f"core_{stem}":
                        discovered.add((source_id, f"core_{stem}"))
                else:
                    for alias in node.names:
                        stem = alias.name
                        if stem in valid_core_stems and source_id != f"core_{stem}":
                            discovered.add((source_id, f"core_{stem}"))
        stack.extend(ast.iter_child_nodes(node))

    return discovered


def _collect_default_app_asts() -> list[tuple[str, ast.Module]]:
    """Collect and parse app/ Python files from CWD when app/core exists."""
    parsed: list[tuple[str, ast.Module]] = []
    app_root = Path("app")
    if not (app_root / "core").is_dir():
        return parsed

    candidate_paths: list[str] = []
    for root, dirs, files in os.walk(str(app_root)):
        dirs[:] = sorted(
            d for d in dirs if d != "__pycache__" and not d.startswith(".")
        )
        for fname in sorted(files):
            if fname.endswith(".py"):
                rel = os.path.join(root, fname).replace("\\", "/")
                if rel == "app/main.py" or rel.startswith(
                    ("app/cli/", "app/ui/", "app/core/")
                ):
                    candidate_paths.append(rel)

    for rel in candidate_paths:
        try:
            source = Path(rel).read_text(encoding="utf-8")
            tree = ast.parse(source, filename=rel)
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            continue
        parsed.append((rel, tree))
    return parsed


# System Default Diagram Specifications
def _create_core_architecture_spec(
    parsed_app_files: Sequence[tuple[str, ast.Module]] | None = None,
) -> ComponentDiagramSpec:
    """Build core_architecture diagram specification dynamically from app/ AST imports."""
    core_dir = Path("app/core")
    core_stems_set: set[str] = set()

    if core_dir.is_dir():
        for py_path in core_dir.glob("*.py"):
            if py_path.name != "__init__.py" and not py_path.name.startswith("."):
                core_stems_set.add(py_path.stem)

    if parsed_app_files is not None:
        for filepath, _tree in parsed_app_files:
            norm = filepath.replace("\\", "/")
            while norm.startswith("./"):
                norm = norm[2:]
            if norm.startswith("app/core/") and "/" not in norm[len("app/core/") :]:
                stem = Path(norm).stem
                if stem != "__init__":
                    core_stems_set.add(stem)

    sorted_stems = sorted(core_stems_set)
    ast_entries = (
        list(parsed_app_files)
        if parsed_app_files is not None
        else (_collect_default_app_asts() if sorted_stems else [])
    )

    subgraph_buckets: dict[str, list[str]] = {
        sg_id: [] for sg_id, _title in _CORE_SUBGRAPH_DEFINITIONS
    }
    subgraph_buckets["ui_layer"].extend(["app_main", "app_tui"])

    nodes: list[DiagramNode] = [
        DiagramNode(id="app_main", label="app.main (CLI Entry Point)", shape="round"),
        DiagramNode(id="app_tui", label="app.tui (Textual Terminal Interface)"),
    ]

    for stem in sorted_stems:
        node_id = f"core_{stem}"
        sg_id = _classify_core_stem_subgraph(stem)
        subgraph_buckets[sg_id].append(node_id)
        label, shape = _CORE_NODE_DISPLAY_OVERRIDES.get(
            stem, (f"app.core.{stem}", "rectangle")
        )
        nodes.append(DiagramNode(id=node_id, label=label, shape=shape))

    subgraphs = [
        DiagramSubgraph(id=sg_id, title=title, nodes=subgraph_buckets[sg_id])
        for sg_id, title in _CORE_SUBGRAPH_DEFINITIONS
        if subgraph_buckets[sg_id]
    ]

    raw_edges: set[tuple[str, str]] = set()
    for filepath, tree in ast_entries:
        norm = filepath.replace("\\", "/")
        while norm.startswith("./"):
            norm = norm[2:]
        raw_edges.update(
            _extract_app_import_edges_iterative(norm, tree, core_stems_set)
        )

    if not raw_edges and not sorted_stems:
        raw_edges.add(("app_main", "app_tui"))

    node_order = {n.id: idx for idx, n in enumerate(nodes)}
    ordered_edges = sorted(
        raw_edges,
        key=lambda pair: (node_order.get(pair[0], 9999), node_order.get(pair[1], 9999)),
    )
    edges = [DiagramEdge(source=src, target=dst) for src, dst in ordered_edges]

    return ComponentDiagramSpec(
        id="core_architecture",
        title="Smart AutoSorter AI Pro Full System Architecture",
        diagram_type="flowchart",
        direction="TD",
        subgraphs=subgraphs,
        nodes=nodes,
        edges=edges,
    )


def _create_architecture_dataflow_spec() -> DataFlowDiagramSpec:
    """Build architecture_dataflow diagram specification."""
    return DataFlowDiagramSpec(
        id="architecture_dataflow",
        title="Data Flow: Directory Selection to Reorganization Plan",
        diagram_type="graph",
        direction="TD",
        nodes=[
            ExternalEntityNode(
                id="A",
                label="Directory Selection",
                url="docs/user_guide.md#first-run-steps--setup-wizard",
                tooltip="Directory Selection Step",
                step_number=1,
                step_description="Select target directory to organize",
            ),
            ProcessNode(
                id="B",
                label="File Extraction & Generator",
                url="docs/user_guide.md#supported-file-formats",
                tooltip="File Extraction Step",
                step_number=2,
                step_description="Extract files and prepare generator",
            ),
            ProcessNode(
                id="C",
                label="Chunked Yielding",
                tooltip="Chunked Yielding Step",
                step_number=3,
                step_description="Yield file chunks incrementally",
            ),
            ProcessNode(
                id="D",
                label="Incremental Analyzer (partial_fit)",
                tooltip="Analyzer Step",
                step_number=4,
                step_description="Run incremental analyzer",
            ),
            ProcessNode(
                id="E",
                label="TF-IDF / Embeddings + K-Means Clustering",
                url="docs/user_guide.md#ai-clustering-constraints",
                tooltip="Clustering Step",
                step_number=5,
                step_description="Cluster file features with TF-IDF or embeddings and MiniBatchKMeans",
            ),
            ProcessNode(
                id="F",
                label="Recursive Topic Grouping",
                tooltip="Topic Grouping Step",
                step_number=6,
                step_description="Recursively group file topics",
            ),
            DataStoreNode(
                id="G",
                label="Generate Sorting Plan",
                tooltip="Sorting Plan Step",
                step_number=7,
                step_description="Generate final file sorting plan",
            ),
            ExternalEntityNode(
                id="H",
                label="UI Tree Rendering",
                url="docs/ui.md#appuimixinstree_view",
                tooltip="UI Rendering Step",
                step_number=8,
                step_description="Render sorting plan in tree view",
            ),
        ],
        edges=[
            DataStreamEdge(source="A", target="B", contract="DirectoryPath"),
            DataStreamEdge(source="B", target="C", contract="FileStream"),
            DataStreamEdge(source="C", target="D", contract="ChunkBatch"),
            DataStreamEdge(source="D", target="E", contract="FeatureVectors"),
            DataStreamEdge(source="E", target="F", contract="TopicClusters"),
            DataStreamEdge(source="F", target="G", contract="SortingPlan"),
            DataStreamEdge(source="G", target="H", contract="PlanViewModel"),
        ],
    )


def _create_cro_multi_study_pipeline_spec() -> ComponentDiagramSpec:
    """Build cro_multi_study_pipeline diagram specification."""
    return ComponentDiagramSpec(
        id="cro_multi_study_pipeline",
        title="CRO Forensic Multi-Study Ingestion Pipeline",
        diagram_type="flowchart",
        direction="TD",
        subgraphs=[
            DiagramSubgraph(
                id="scan_stage",
                title="Drive Ingestion & Scan",
                nodes=["forensic_scan", "study_disambiguation"],
            ),
            DiagramSubgraph(
                id="taxonomy_stage",
                title="Taxonomy & Compliance Rules",
                nodes=["clinical_taxonomy", "clinical_compliance"],
            ),
            DiagramSubgraph(
                id="binding_stage",
                title="TMF Binder Relocation",
                nodes=["clinical_renamer", "cro_pipeline_output"],
            ),
        ],
        nodes=[
            DiagramNode(
                id="forensic_scan",
                label="app.core.forensic_scanner",
                shape="subroutine",
            ),
            DiagramNode(
                id="study_disambiguation",
                label="app.core.study_disambiguator",
                shape="rhombus",
            ),
            DiagramNode(id="clinical_taxonomy", label="app.core.clinical_taxonomy"),
            DiagramNode(
                id="clinical_compliance",
                label="app.core.clinical_compliance",
                shape="rhombus",
            ),
            DiagramNode(id="clinical_renamer", label="app.core.clinical_renamer"),
            DiagramNode(
                id="cro_pipeline_output",
                label="TMF Clean Binders Output",
                shape="stadium",
            ),
        ],
        edges=[
            DiagramEdge(
                source="forensic_scan",
                target="study_disambiguation",
                label="Raw Drives",
            ),
            DiagramEdge(
                source="study_disambiguation",
                target="clinical_taxonomy",
                label="Protocols Disambiguated",
            ),
            DiagramEdge(
                source="clinical_taxonomy",
                target="clinical_compliance",
                label="Mapped Taxonomy",
            ),
            DiagramEdge(
                source="clinical_compliance",
                target="clinical_renamer",
                label="Compliance Validated",
            ),
            DiagramEdge(
                source="clinical_renamer",
                target="cro_pipeline_output",
                label="Compiled Binders",
            ),
        ],
    )


def _create_memory_cache_layers_spec() -> ComponentDiagramSpec:
    """Build memory_cache_layers diagram specification."""
    return ComponentDiagramSpec(
        id="memory_cache_layers",
        title="BoundedMemoryCache Centralized In-Memory Caching Architecture",
        diagram_type="flowchart",
        direction="LR",
        subgraphs=[
            DiagramSubgraph(
                id="core_cache_box",
                title="Centralized LRU Cache (app.core.cache)",
                nodes=["bounded_cache"],
            ),
            DiagramSubgraph(
                id="cache_clients",
                title="Subsystem Consumers",
                nodes=[
                    "db_conn_cache",
                    "link_manager_cache",
                    "semantic_cache",
                    "jev_cache",
                    "db_cache",
                ],
            ),
        ],
        nodes=[
            DiagramNode(
                id="bounded_cache", label="BoundedMemoryCache[K, V]", shape="database"
            ),
            DiagramNode(
                id="db_conn_cache", label="app.core.db_conn (_connection_cache)"
            ),
            DiagramNode(
                id="link_manager_cache", label="app.core.link_manager (_registry)"
            ),
            DiagramNode(
                id="semantic_cache",
                label="app.core.semantic_embeddings (_model_properties_cache)",
            ),
            DiagramNode(id="jev_cache", label="app.core.jev_classifier (memory_cache)"),
            DiagramNode(id="db_cache", label="app.core.db (doc_cache)"),
        ],
        edges=[
            DiagramEdge(
                source="db_conn_cache", target="bounded_cache", label="Max Size: 50"
            ),
            DiagramEdge(
                source="link_manager_cache",
                target="bounded_cache",
                label="Max Size: 10000",
            ),
            DiagramEdge(
                source="semantic_cache", target="bounded_cache", label="Max Size: 500"
            ),
            DiagramEdge(
                source="jev_cache", target="bounded_cache", label="Fast Path Cache"
            ),
            DiagramEdge(
                source="db_cache", target="bounded_cache", label="Max Size: 10000"
            ),
        ],
    )


def _create_worker_pool_concurrency_spec() -> ComponentDiagramSpec:
    """Build worker_pool_concurrency diagram specification."""
    return ComponentDiagramSpec(
        id="worker_pool_concurrency",
        title="SharedWorkerPool Multi-Threaded Concurrency Model",
        diagram_type="flowchart",
        direction="TD",
        subgraphs=[
            DiagramSubgraph(
                id="pool_singleton",
                title="Shared Singleton (app.core.worker_pool)",
                nodes=["shared_worker_pool"],
            ),
            DiagramSubgraph(
                id="pool_tasks",
                title="Concurrent Execution Callers",
                nodes=[
                    "session_tasks",
                    "mover_tasks",
                    "db_worker_tasks",
                    "extractor_tasks",
                    "analyzer_tasks",
                ],
            ),
        ],
        nodes=[
            DiagramNode(
                id="shared_worker_pool",
                label="SharedWorkerPool Singleton",
                shape="stadium",
            ),
            DiagramNode(
                id="session_tasks", label="app.core.session (Lifecycle Management)"
            ),
            DiagramNode(
                id="mover_tasks", label="app.core.mover (Parallel File Relocation)"
            ),
            DiagramNode(
                id="db_worker_tasks",
                label="app.core.db_worker (Background Heavy Tasks)",
            ),
            DiagramNode(
                id="extractor_tasks",
                label="app.core.extractor (Parallel Text Ingestion)",
            ),
            DiagramNode(
                id="analyzer_tasks", label="app.core.analyzer_strategies (Parallel NLP)"
            ),
        ],
        edges=[
            DiagramEdge(
                source="session_tasks",
                target="shared_worker_pool",
                label="get_instance() / shutdown()",
            ),
            DiagramEdge(
                source="mover_tasks",
                target="shared_worker_pool",
                label="submit() relocation tasks",
            ),
            DiagramEdge(
                source="db_worker_tasks",
                target="shared_worker_pool",
                label="offload VLM/OCR/GGUF",
            ),
            DiagramEdge(
                source="extractor_tasks",
                target="shared_worker_pool",
                label="parallel document extraction",
            ),
            DiagramEdge(
                source="analyzer_tasks",
                target="shared_worker_pool",
                label="offload embedding vector calculations",
            ),
        ],
    )


def _create_policy_evaluation_flow_spec() -> ComponentDiagramSpec:
    """Build policy_evaluation_flow diagram specification."""
    return ComponentDiagramSpec(
        id="policy_evaluation_flow",
        title="Administrator Policy Evaluation & Compliance Flowchart",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(id="A", label="Incoming Document", shape="round"),
            DiagramNode(
                id="B", label="Sort Rules by Priority High to Low", shape="rectangle"
            ),
            DiagramNode(id="C", label="Evaluate Next Rule", shape="rhombus"),
            DiagramNode(
                id="D", label="Route Document via Override Path", shape="rectangle"
            ),
            DiagramNode(
                id="E", label="Route Document via Keyword Category", shape="rectangle"
            ),
            DiagramNode(
                id="F", label="Route Document via Pattern Category", shape="rectangle"
            ),
            DiagramNode(
                id="G", label="Stop Processing & Halt Evaluation", shape="rectangle"
            ),
            DiagramNode(id="H", label="More Rules Remaining?", shape="rhombus"),
            DiagramNode(
                id="I",
                label="Proceed to General Classification / AI Sorting",
                shape="rectangle",
            ),
        ],
        edges=[
            DiagramEdge(source="A", target="B"),
            DiagramEdge(source="B", target="C"),
            DiagramEdge(source="C", target="D", label="Override Rule Match"),
            DiagramEdge(source="C", target="E", label="Keyword Rule Match"),
            DiagramEdge(source="C", target="F", label="Pattern Rule Match"),
            DiagramEdge(
                source="C", target="G", label="No Match & Halt on Mismatch Enabled"
            ),
            DiagramEdge(source="C", target="H", label="No Match & Halt Disabled"),
            DiagramEdge(source="H", target="C", label="Yes"),
            DiagramEdge(source="H", target="I", label="No"),
        ],
    )


def _create_setup_wizard_flow_spec() -> ComponentDiagramSpec:
    """Build setup_wizard_flow diagram specification."""
    return ComponentDiagramSpec(
        id="setup_wizard_flow",
        title="Setup Wizard Download & Offline Fallback Decision Flow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(id="A", label="Setup Wizard Download Triggered", shape="round"),
            DiagramNode(id="B", label="Network Connection OK?", shape="rhombus"),
            DiagramNode(
                id="C", label="Check Firewall & Disconnected Status", shape="rectangle"
            ),
            DiagramNode(
                id="D", label="Fallback to Offline Non-Semantic Mode", shape="rectangle"
            ),
            DiagramNode(
                id="E", label="Sufficient Disk Space >= 200MB?", shape="rhombus"
            ),
            DiagramNode(id="F", label="Clear Free Disk Space", shape="rectangle"),
            DiagramNode(
                id="G", label="Retry Model Download via Settings", shape="rectangle"
            ),
            DiagramNode(id="H", label="Download 80MB AI Model", shape="rectangle"),
            DiagramNode(id="I", label="Enable Semantic AI Sorting", shape="round"),
        ],
        edges=[
            DiagramEdge(source="A", target="B"),
            DiagramEdge(source="B", target="C", label="No"),
            DiagramEdge(source="C", target="D"),
            DiagramEdge(source="B", target="E", label="Yes"),
            DiagramEdge(source="E", target="F", label="No"),
            DiagramEdge(source="F", target="G"),
            DiagramEdge(source="G", target="B"),
            DiagramEdge(source="E", target="H", label="Yes"),
            DiagramEdge(source="H", target="I"),
        ],
    )


def _create_architecture_async_processing_spec() -> SequenceDiagramSpec:
    """Build architecture_async_processing diagram specification."""
    return SequenceDiagramSpec(
        id="architecture_async_processing",
        title="Asynchronous Scanning and Recalculation Sequence",
        autonumber=True,
        participants=[
            SequenceParticipant(id="User", label="User"),
            SequenceParticipant(id="UI", label="Application Window / UI Thread"),
            SequenceParticipant(
                id="Worker", label="Background Scan Worker (_scan_and_process_worker)"
            ),
            SequenceParticipant(
                id="ThreadPool", label="Thread Pool Worker (asyncio.to_thread)"
            ),
            SequenceParticipant(id="Analyzer", label="IncrementalAnalyzer"),
        ],
        items=[
            SequenceMessage(
                source="User", target="UI", text="Selects Directory / Triggers Analysis"
            ),
            SequenceMessage(
                source="UI",
                target="Worker",
                text="asyncio.create_task(_scan_and_process_worker())",
            ),
            SequenceActivation(target="Worker", action="activate"),
            SequenceMessage(
                source="Worker",
                target="ThreadPool",
                text="asyncio.to_thread(get_files_recursively)",
            ),
            SequenceMessage(
                source="ThreadPool",
                target="Worker",
                text="File list",
                arrow_type="-->>",
            ),
            SequenceLoop(
                label="For each item",
                items=[
                    SequenceMessage(
                        source="Worker",
                        target="ThreadPool",
                        text="asyncio.to_thread(partial_fit, chunk)",
                    ),
                    SequenceMessage(
                        source="ThreadPool",
                        target="Worker",
                        text="Model updated",
                        arrow_type="-->>",
                    ),
                    SequenceMessage(
                        source="Worker",
                        target="UI",
                        text="loop.call_soon_threadsafe(update_progress)",
                    ),
                ],
            ),
            SequenceMessage(
                source="Worker",
                target="ThreadPool",
                text="asyncio.to_thread(generate_sorting_plan)",
            ),
            SequenceMessage(
                source="ThreadPool",
                target="Worker",
                text="Initial Sorting Plan",
                arrow_type="-->>",
            ),
            SequenceMessage(source="Worker", target="UI", text="Render Tree"),
            SequenceActivation(target="Worker", action="deactivate"),
            SequenceNote(
                position="over",
                targets=["User", "Analyzer"],
                text="Debounced Plan Recalculation Flow",
            ),
            SequenceMessage(
                source="User",
                target="UI",
                text="Keyboard Reassign [R] / Lock Toggle",
            ),
            SequenceMessage(source="UI", target="UI", text="_rebuild_plan_async()"),
            SequenceOpt(
                label="Active recalc token or debounce task running",
                items=[
                    SequenceMessage(
                        source="UI",
                        target="UI",
                        text="token.set() & debounce_task.cancel()",
                    ),
                ],
            ),
            SequenceMessage(
                source="UI",
                target="UI",
                text="Create new threading.Event token & asyncio.create_task(delayed_run)",
            ),
            SequenceActivation(target="UI", action="activate"),
            SequenceMessage(
                source="UI", target="UI", text="asyncio.sleep(0.5) [Debounce Delay]"
            ),
            SequenceAlt(
                branches=[
                    SequenceAltBranch(
                        label="Task Cancelled During Sleep",
                        items=[
                            SequenceMessage(
                                source="UI",
                                target="User",
                                text="Abort Recalculation",
                                arrow_type="-->>",
                            ),
                        ],
                    ),
                    SequenceAltBranch(
                        label="Timer Expired",
                        items=[
                            SequenceMessage(
                                source="UI",
                                target="ThreadPool",
                                text="asyncio.to_thread(generate_sorting_plan, check_cancel)",
                            ),
                            SequenceActivation(target="ThreadPool", action="activate"),
                            SequenceLoop(
                                label="Periodically",
                                items=[
                                    SequenceMessage(
                                        source="ThreadPool",
                                        target="ThreadPool",
                                        text="check_cancel() -> token.is_set()",
                                    ),
                                ],
                            ),
                            SequenceMessage(
                                source="ThreadPool",
                                target="Analyzer",
                                text="generate_sorting_plan(...)",
                                arrow_type="-->>",
                            ),
                            SequenceMessage(
                                source="Analyzer",
                                target="ThreadPool",
                                text="Rebuilt Plan",
                                arrow_type="-->>",
                            ),
                            SequenceMessage(
                                source="ThreadPool",
                                target="UI",
                                text="Return Plan",
                                arrow_type="-->>",
                            ),
                            SequenceActivation(
                                target="ThreadPool", action="deactivate"
                            ),
                            SequenceMessage(
                                source="UI", target="UI", text="render_tree()"
                            ),
                        ],
                    ),
                ]
            ),
            SequenceActivation(target="UI", action="deactivate"),
        ],
    )


def _create_architecture_watchdog_state_spec() -> StateDiagramSpec:
    """Build architecture_watchdog_state diagram specification."""
    return StateDiagramSpec(
        id="architecture_watchdog_state",
        title="Watchdog File Event State Machine",
        diagram_type="stateDiagram-v2",
        transitions=[
            StateTransition(
                source="[*]", target="Monitoring", label="Active Watchdog Service"
            ),
            StateTransition(
                source="Monitoring",
                target="FilterTransient",
                label="Directory File Modification",
            ),
            StateTransition(
                source="FilterTransient",
                target="Monitoring",
                label="Ignore (.crdownload, .tmp, .download)",
            ),
            StateTransition(
                source="FilterTransient",
                target="DebounceActive",
                label="Valid File Event Received",
            ),
            StateTransition(
                source="DebounceActive",
                target="DebounceActive",
                label="Reset Timer on Rapid Writes (0.6s)",
            ),
            StateTransition(
                source="DebounceActive",
                target="DispatchPipeline",
                label="Standard Debounce Timeout (0.6s)",
            ),
            StateTransition(
                source="DebounceActive",
                target="DispatchPipeline",
                label="Max Delay Cap Reached (5.0s)",
            ),
            StateTransition(
                source="DispatchPipeline",
                target="Monitoring",
                label="Pipeline Executed & UI Refreshed",
            ),
        ],
    )


def _create_architecture_move_engine_state_spec() -> StateDiagramSpec:
    """Build architecture_move_engine_state diagram specification for MoveEngineState."""
    return StateDiagramSpec(
        id="architecture_move_engine_state",
        title="Async Move Engine Lifecycle State Machine",
        diagram_type="stateDiagram-v2",
        transitions=[
            StateTransition(source="[*]", target="IDLE", label="Engine Initialized"),
            StateTransition(
                source="IDLE", target="PREPARING", label="Start Move Operations"
            ),
            StateTransition(
                source="PREPARING", target="MOVING_CHUNK", label="Begin Chunk Transfers"
            ),
            StateTransition(
                source="PREPARING",
                target="RESOLVING_LINKS",
                label="No File Chunks - Link Phase",
            ),
            StateTransition(
                source="PREPARING",
                target="CLEANING_DIRS",
                label="No Moves - Directory Cleanup",
            ),
            StateTransition(
                source="PREPARING", target="COMPLETED", label="Zero-Op Plan Execution"
            ),
            StateTransition(
                source="PREPARING", target="CANCELLING", label="Cancel Requested"
            ),
            StateTransition(
                source="PREPARING", target="CANCELLED", label="Direct Cancellation"
            ),
            StateTransition(
                source="PREPARING", target="FAILED", label="Pre-flight Validation Error"
            ),
            StateTransition(
                source="MOVING_CHUNK",
                target="MOVING_CHUNK",
                label="Transfer Next Batch",
            ),
            StateTransition(
                source="MOVING_CHUNK",
                target="RESOLVING_LINKS",
                label="All Chunks Moved",
            ),
            StateTransition(
                source="MOVING_CHUNK",
                target="CLEANING_DIRS",
                label="No Links to Resolve",
            ),
            StateTransition(
                source="MOVING_CHUNK",
                target="COMPLETED",
                label="Moves Finished - Skip Cleanup",
            ),
            StateTransition(
                source="MOVING_CHUNK", target="CANCELLING", label="Cancel Requested"
            ),
            StateTransition(
                source="MOVING_CHUNK", target="CANCELLED", label="Direct Cancellation"
            ),
            StateTransition(
                source="MOVING_CHUNK", target="FAILED", label="IO or Permission Error"
            ),
            StateTransition(
                source="RESOLVING_LINKS", target="CLEANING_DIRS", label="Links Resolved"
            ),
            StateTransition(
                source="RESOLVING_LINKS",
                target="COMPLETED",
                label="Resolution Complete - No Cleanup",
            ),
            StateTransition(
                source="RESOLVING_LINKS", target="CANCELLING", label="Cancel Requested"
            ),
            StateTransition(
                source="RESOLVING_LINKS",
                target="CANCELLED",
                label="Direct Cancellation",
            ),
            StateTransition(
                source="RESOLVING_LINKS", target="FAILED", label="Broken Link Error"
            ),
            StateTransition(
                source="CLEANING_DIRS",
                target="COMPLETED",
                label="Empty Directories Pruned",
            ),
            StateTransition(
                source="CLEANING_DIRS", target="CANCELLING", label="Cancel Requested"
            ),
            StateTransition(
                source="CLEANING_DIRS", target="CANCELLED", label="Direct Cancellation"
            ),
            StateTransition(
                source="CLEANING_DIRS", target="FAILED", label="Prune Directory Error"
            ),
            StateTransition(
                source="CANCELLING",
                target="CANCELLED",
                label="Cleanup Completed on Cancel",
            ),
            StateTransition(
                source="CANCELLING", target="FAILED", label="Error During Cancellation"
            ),
            StateTransition(
                source="CANCELLED", target="IDLE", label="Reset Engine State"
            ),
            StateTransition(
                source="CANCELLED", target="PREPARING", label="Restart Execution"
            ),
            StateTransition(source="CANCELLED", target="[*]", label="Engine Shutdown"),
            StateTransition(
                source="COMPLETED", target="IDLE", label="Reset Engine State"
            ),
            StateTransition(
                source="COMPLETED", target="PREPARING", label="New Move Operation"
            ),
            StateTransition(source="COMPLETED", target="[*]", label="Engine Shutdown"),
            StateTransition(
                source="FAILED",
                target="ROLLING_BACK",
                label="Automatic or Manual Rollback",
            ),
            StateTransition(source="FAILED", target="IDLE", label="Reset Engine State"),
            StateTransition(
                source="FAILED", target="PREPARING", label="Retry Failed Move"
            ),
            StateTransition(
                source="ROLLING_BACK",
                target="ROLLED_BACK",
                label="Rollback Completed Successfully",
            ),
            StateTransition(
                source="ROLLING_BACK", target="FAILED", label="Rollback Operation Fault"
            ),
            StateTransition(
                source="ROLLED_BACK", target="IDLE", label="Reset Engine State"
            ),
            StateTransition(
                source="ROLLED_BACK", target="PREPARING", label="New Move Operation"
            ),
            StateTransition(
                source="ROLLED_BACK", target="[*]", label="Engine Shutdown"
            ),
        ],
    )


def _create_architecture_pipeline_state_spec() -> StateDiagramSpec:
    """Build architecture_pipeline_state diagram specification for PipelineLifecycleState."""
    return StateDiagramSpec(
        id="architecture_pipeline_state",
        title="Sort Pipeline Operational Lifecycle State Machine",
        diagram_type="stateDiagram-v2",
        transitions=[
            StateTransition(
                source="[*]", target="CREATED", label="Instantiate SortPipeline"
            ),
            StateTransition(
                source="CREATED", target="PLANNING", label="Start Plan Generation"
            ),
            StateTransition(
                source="CREATED", target="PLANNED", label="Direct Plan Injection"
            ),
            StateTransition(
                source="CREATED", target="ROLLING_BACK", label="Abort and Rollback"
            ),
            StateTransition(
                source="PLANNING",
                target="PLANNED",
                label="Clustering and Planning Succeeded",
            ),
            StateTransition(
                source="PLANNING",
                target="FAILED",
                label="Analysis or Planning Exception",
            ),
            StateTransition(
                source="PLANNING", target="CANCELLED", label="User Cancellation"
            ),
            StateTransition(
                source="PLANNED", target="EXECUTING", label="Execute Sorting Plan"
            ),
            StateTransition(
                source="PLANNED", target="PLANNING", label="Recalculate or Re-plan"
            ),
            StateTransition(
                source="PLANNED", target="CANCELLED", label="Cancel Plan Execution"
            ),
            StateTransition(
                source="PLANNED",
                target="ROLLING_BACK",
                label="Rollback Planned Session",
            ),
            StateTransition(
                source="EXECUTING", target="COMPLETED", label="Execution Succeeded"
            ),
            StateTransition(
                source="EXECUTING",
                target="CANCELLED",
                label="Cancel During Move Operations",
            ),
            StateTransition(
                source="EXECUTING",
                target="ROLLING_BACK",
                label="Automatic Rollback on Move Failure",
            ),
            StateTransition(
                source="EXECUTING",
                target="FAILED",
                label="Unrecoverable Execution Fault",
            ),
            StateTransition(
                source="ROLLING_BACK",
                target="ROLLED_BACK",
                label="Rollback Completed Successfully",
            ),
            StateTransition(
                source="ROLLING_BACK",
                target="FAILED",
                label="Error During Rollback Steps",
            ),
            StateTransition(
                source="COMPLETED", target="PLANNING", label="Re-plan Pipeline"
            ),
            StateTransition(
                source="COMPLETED",
                target="ROLLING_BACK",
                label="Undo Completed Session",
            ),
            StateTransition(
                source="COMPLETED", target="[*]", label="Pipeline Finished"
            ),
            StateTransition(
                source="FAILED", target="PLANNING", label="Retry Pipeline Planning"
            ),
            StateTransition(
                source="FAILED", target="ROLLING_BACK", label="Revert Failed State"
            ),
            StateTransition(source="FAILED", target="[*]", label="Pipeline Terminated"),
            StateTransition(
                source="CANCELLED", target="PLANNING", label="Re-run Pipeline Planning"
            ),
            StateTransition(
                source="CANCELLED",
                target="ROLLING_BACK",
                label="Cleanup Cancelled State",
            ),
            StateTransition(
                source="CANCELLED", target="[*]", label="Pipeline Terminated"
            ),
            StateTransition(
                source="ROLLED_BACK", target="PLANNING", label="Re-plan After Rollback"
            ),
            StateTransition(
                source="ROLLED_BACK", target="ROLLING_BACK", label="Re-attempt Rollback"
            ),
            StateTransition(
                source="ROLLED_BACK", target="[*]", label="Pipeline Terminated"
            ),
        ],
    )


def _create_architecture_session_state_spec() -> StateDiagramSpec:
    """Build architecture_session_state diagram specification for SessionStateMachine."""
    return StateDiagramSpec(
        id="architecture_session_state",
        title="Session History Transaction State Machine",
        diagram_type="stateDiagram-v2",
        transitions=[
            StateTransition(
                source="[*]", target="active", label="Begin Session Transaction"
            ),
            StateTransition(
                source="[*]", target="uncommitted", label="Stage Uncommitted Session"
            ),
            StateTransition(
                source="uncommitted", target="completed", label="Commit Staged Session"
            ),
            StateTransition(
                source="uncommitted",
                target="rolled_back",
                label="Discard Staged Session",
            ),
            StateTransition(
                source="uncommitted",
                target="partially_rolled_back",
                label="Partial Staging Discard",
            ),
            StateTransition(
                source="uncommitted", target="failed", label="Staging Commit Fault"
            ),
            StateTransition(
                source="uncommitted", target="abandoned", label="Staging Orphaned"
            ),
            StateTransition(
                source="active", target="completed", label="Execution Succeeded"
            ),
            StateTransition(source="active", target="failed", label="Execution Error"),
            StateTransition(
                source="active", target="rolled_back", label="Full Immediate Rollback"
            ),
            StateTransition(
                source="active",
                target="partially_rolled_back",
                label="Partial Immediate Rollback",
            ),
            StateTransition(
                source="active", target="undo_snapshot", label="Capture Undo Snapshot"
            ),
            StateTransition(
                source="active",
                target="abandoned",
                label="Heartbeat Lost or Process Killed",
            ),
            StateTransition(
                source="completed", target="rolled_back", label="Full Session Rollback"
            ),
            StateTransition(
                source="completed",
                target="partially_rolled_back",
                label="Partial Session Rollback",
            ),
            StateTransition(
                source="completed", target="failed", label="Rollback Exception"
            ),
            StateTransition(
                source="completed", target="abandoned", label="Session Tombstoned"
            ),
            StateTransition(
                source="partially_rolled_back",
                target="rolled_back",
                label="Resume and Complete Undo",
            ),
            StateTransition(
                source="partially_rolled_back",
                target="failed",
                label="Undo Continuation Failed",
            ),
            StateTransition(
                source="partially_rolled_back",
                target="abandoned",
                label="Partial Undo Abandoned",
            ),
            StateTransition(
                source="undo_snapshot",
                target="rolled_back",
                label="Restore Undo Snapshot",
            ),
            StateTransition(
                source="undo_snapshot", target="failed", label="Snapshot Restore Failed"
            ),
            StateTransition(
                source="undo_snapshot",
                target="abandoned",
                label="Snapshot Expired or Discarded",
            ),
            StateTransition(
                source="failed", target="active", label="Retry Session Execution"
            ),
            StateTransition(
                source="failed",
                target="completed",
                label="Manual Reconciliation Override",
            ),
            StateTransition(
                source="failed",
                target="rolled_back",
                label="Revert Incomplete Operations",
            ),
            StateTransition(
                source="failed",
                target="partially_rolled_back",
                label="Partial Incomplete Revert",
            ),
            StateTransition(
                source="failed", target="abandoned", label="Mark Unrecoverable Session"
            ),
            StateTransition(
                source="abandoned",
                target="rolled_back",
                label="Reconcile and Revert Orphan",
            ),
            StateTransition(
                source="abandoned",
                target="partially_rolled_back",
                label="Partial Orphan Cleanup",
            ),
            StateTransition(
                source="abandoned", target="failed", label="Reconciliation Error"
            ),
            StateTransition(source="rolled_back", target="[*]", label="Terminal State"),
        ],
    )


def _create_architecture_download_state_spec() -> StateDiagramSpec:
    """Build architecture_download_state diagram specification for DownloadState."""
    return StateDiagramSpec(
        id="architecture_download_state",
        title="Model Downloader Lifecycle State Machine",
        diagram_type="stateDiagram-v2",
        transitions=[
            StateTransition(
                source="[*]", target="IDLE", label="Downloader Initialized"
            ),
            StateTransition(
                source="IDLE", target="DOWNLOADING", label="Start Model Download"
            ),
            StateTransition(
                source="DOWNLOADING",
                target="COMPLETED",
                label="Download and Checksum Verified",
            ),
            StateTransition(
                source="DOWNLOADING",
                target="FAILED",
                label="Network Timeout or Checksum Mismatch",
            ),
            StateTransition(
                source="DOWNLOADING",
                target="CANCELLED",
                label="User Cancellation Token",
            ),
            StateTransition(
                source="COMPLETED",
                target="DOWNLOADING",
                label="Re-verify or Redownload",
            ),
            StateTransition(
                source="COMPLETED", target="IDLE", label="Reset Downloader State"
            ),
            StateTransition(
                source="COMPLETED", target="[*]", label="Model Ready in Cache"
            ),
            StateTransition(
                source="FAILED", target="DOWNLOADING", label="Retry Failed Download"
            ),
            StateTransition(
                source="FAILED", target="IDLE", label="Clear Error and Reset"
            ),
            StateTransition(source="FAILED", target="[*]", label="Download Terminated"),
            StateTransition(
                source="CANCELLED",
                target="DOWNLOADING",
                label="Resume or Restart Download",
            ),
            StateTransition(
                source="CANCELLED", target="IDLE", label="Reset Downloader State"
            ),
            StateTransition(source="CANCELLED", target="[*]", label="Download Aborted"),
        ],
    )


def _create_catalog_workflow_spec() -> ComponentDiagramSpec:
    """Build catalog_workflow diagram specification."""
    return ComponentDiagramSpec(
        id="catalog_workflow",
        title="Component Catalog Interactive Workbench Workflow",
        diagram_type="graph",
        direction="TD",
        nodes=[
            DiagramNode(
                id="workbench",
                label="Catalog Workbench",
                shape="round",
                url="docs/ui.md",
                tooltip="Catalog Workbench",
            ),
            DiagramNode(
                id="selector",
                label="Component Selector",
                shape="rectangle",
                url="docs/ui.md",
                tooltip="Component Selector",
            ),
            DiagramNode(
                id="viewport",
                label="Viewport Controller",
                shape="rectangle",
                url="docs/ui.md",
                tooltip="Viewport Controller",
            ),
            DiagramNode(
                id="state_var",
                label="State Variant Switcher",
                shape="rectangle",
                url="docs/ui.md",
                tooltip="State Variant Switcher",
            ),
            DiagramNode(
                id="renderer",
                label="Component Renderer",
                shape="subroutine",
                url="docs/ui.md",
                tooltip="Component Renderer",
            ),
            DiagramNode(
                id="a11y_scan",
                label="Accessibility Auditor",
                shape="rhombus",
                url="docs/admin_guide.md",
                tooltip="Accessibility Auditor",
            ),
            DiagramNode(
                id="preview",
                label="Interactive Viewport Frame",
                shape="round",
                url="docs/ui.md",
                tooltip="Interactive Viewport Frame",
            ),
        ],
        edges=[
            DiagramEdge(source="workbench", target="selector"),
            DiagramEdge(source="workbench", target="viewport"),
            DiagramEdge(source="workbench", target="state_var"),
            DiagramEdge(source="selector", target="renderer"),
            DiagramEdge(source="viewport", target="preview"),
            DiagramEdge(source="state_var", target="renderer"),
            DiagramEdge(source="renderer", target="preview"),
            DiagramEdge(source="renderer", target="a11y_scan"),
        ],
    )


def _create_ui_component_hierarchy_spec() -> ComponentDiagramSpec:
    """Build ui_component_hierarchy diagram specification."""
    return ComponentDiagramSpec(
        id="ui_component_hierarchy",
        title="UI Component Catalog Structure",
        diagram_type="graph",
        direction="LR",
        subgraphs=[
            DiagramSubgraph(
                id="nav",
                title="Navigation & Layout",
                nodes=["tui_app", "heading"],
            ),
            DiagramSubgraph(
                id="inputs",
                title="Selection & Controls",
                nodes=["vim_tree", "density_slider", "path_diff"],
            ),
            DiagramSubgraph(
                id="views",
                title="Display & Modals",
                nodes=["edit_modal", "settings_modal", "setup_wizard"],
            ),
        ],
        nodes=[
            DiagramNode(
                id="tui_app",
                label="Textual Application Shell",
                url="docs/ui.md#appuitui",
                tooltip="Main Terminal UI Container",
            ),
            DiagramNode(
                id="heading",
                label="Heading Component",
                url="docs/ui.md#appuiheading",
                tooltip="Header & Title Bar",
            ),
            DiagramNode(
                id="vim_tree",
                label="VimTree Plan View",
                url="docs/ui.md#appuiwidgetsvim_tree",
                tooltip="Interactive Directory Plan Tree",
            ),
            DiagramNode(
                id="density_slider",
                label="Density Slider Widget",
                url="docs/ui.md#appuiwidgetsdensity_slider",
                tooltip="Layout Density Control",
            ),
            DiagramNode(
                id="path_diff",
                label="Path Diff Comparison",
                url="docs/ui.md#appuiwidgetspath_diff",
                tooltip="Path Diff Preview Component",
            ),
            DiagramNode(
                id="edit_modal",
                label="Edit & Reassign Modal",
                url="docs/ui.md#appuimodalsedit",
                tooltip="File Reassignment and Node Edit Dialog",
            ),
            DiagramNode(
                id="settings_modal",
                label="Settings Modal",
                url="docs/ui.md#appuimodalssettings",
                tooltip="Application Settings Dialog",
            ),
            DiagramNode(
                id="setup_wizard",
                label="Setup Wizard Modal",
                url="docs/ui.md#appuimodalswizard",
                tooltip="First-Run Configuration Wizard",
            ),
        ],
        edges=[
            DiagramEdge(source="tui_app", target="heading"),
            DiagramEdge(source="tui_app", target="vim_tree"),
            DiagramEdge(source="vim_tree", target="edit_modal"),
            DiagramEdge(source="vim_tree", target="path_diff"),
            DiagramEdge(source="tui_app", target="settings_modal"),
            DiagramEdge(source="settings_modal", target="setup_wizard"),
        ],
    )


def _create_core_text_extraction_spec() -> SequenceDiagramSpec:
    """Build core_text_extraction diagram specification."""
    return SequenceDiagramSpec(
        id="core_text_extraction",
        title="Multi-Format Text Extraction Flow",
        autonumber=True,
        participants=[
            SequenceParticipant(id="FS", label="FileScanner"),
            SequenceParticipant(id="EX", label="Extractor Engine"),
            SequenceParticipant(id="SN", label="Text Sanitizer"),
            SequenceParticipant(id="CG", label="Corpus Generator"),
        ],
        items=[
            SequenceMessage(
                source="FS",
                target="EX",
                text="Scan target directory for supported files",
            ),
            SequenceActivation(target="EX", action="activate"),
            SequenceMessage(
                source="EX", target="EX", text="Extract raw text payload per format"
            ),
            SequenceMessage(source="EX", target="SN", text="Pass raw text payload"),
            SequenceActivation(target="SN", action="activate"),
            SequenceMessage(
                source="SN", target="SN", text="Sanitize text and filter stop words"
            ),
            SequenceMessage(
                source="SN",
                target="EX",
                text="Return sanitized text yield",
                arrow_type="-->>",
            ),
            SequenceActivation(target="SN", action="deactivate"),
            SequenceMessage(
                source="EX",
                target="CG",
                text="Yield document text chunk",
                arrow_type="-->>",
            ),
            SequenceActivation(target="EX", action="deactivate"),
        ],
    )


def _create_contributor_onboarding_spec() -> ComponentDiagramSpec:
    """Build contributor_onboarding diagram specification."""
    return ComponentDiagramSpec(
        id="contributor_onboarding",
        title="Contributor Onboarding & Validation Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(id="A", label="Clone Repository"),
            DiagramNode(id="B", label="Sync Environment: uv sync"),
            DiagramNode(
                id="C",
                label="Install Hooks: uv run python scripts/install_git_hooks.py",
            ),
            DiagramNode(id="D", label="Run Test Suite: uv run pytest"),
            DiagramNode(id="E", label="Run CLI Demo: uv run smart-autosorter --demo"),
            DiagramNode(
                id="F",
                label="Run Docs Check: uv run python scripts/generate_docs.py --check",
            ),
            DiagramNode(id="G", label="Submit Pull Request"),
        ],
        edges=[
            DiagramEdge(source="A", target="B"),
            DiagramEdge(source="B", target="C"),
            DiagramEdge(source="C", target="D"),
            DiagramEdge(source="D", target="E"),
            DiagramEdge(source="E", target="F"),
            DiagramEdge(source="F", target="G"),
        ],
    )


def _create_troubleshooting_setup_wizard_spec() -> ComponentDiagramSpec:
    """Build troubleshooting_setup_wizard diagram specification."""
    return ComponentDiagramSpec(
        id="troubleshooting_setup_wizard",
        title="Setup Wizard Troubleshooting & Fallback Decision Flow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="A", label="Setup Wizard Download Triggered", shape="rectangle"
            ),
            DiagramNode(id="B", label="Network Connection OK?", shape="rhombus"),
            DiagramNode(
                id="C", label="Check Firewall & Disconnected Status", shape="rectangle"
            ),
            DiagramNode(
                id="D", label="Fallback to Offline Non-Semantic Mode", shape="rectangle"
            ),
            DiagramNode(
                id="E", label="Sufficient Disk Space >= 200MB?", shape="rhombus"
            ),
            DiagramNode(id="F", label="Clear Free Disk Space", shape="rectangle"),
            DiagramNode(
                id="G", label="Retry Model Download via Settings", shape="rectangle"
            ),
            DiagramNode(id="H", label="Download 80MB AI Model", shape="rectangle"),
            DiagramNode(id="I", label="Enable Semantic AI Sorting", shape="rectangle"),
        ],
        edges=[
            DiagramEdge(source="A", target="B"),
            DiagramEdge(source="B", target="C", label="No"),
            DiagramEdge(source="C", target="D"),
            DiagramEdge(source="B", target="E", label="Yes"),
            DiagramEdge(source="E", target="F", label="No"),
            DiagramEdge(source="F", target="G"),
            DiagramEdge(source="E", target="H", label="Yes"),
            DiagramEdge(source="H", target="I"),
            DiagramEdge(source="G", target="B"),
        ],
    )


def _create_ml_analyzer_clustering_spec() -> ComponentDiagramSpec:
    """Build ml_analyzer_clustering diagram specification."""
    return ComponentDiagramSpec(
        id="ml_analyzer_clustering",
        title="ML Analyzer & Hierarchical Clustering Pipeline",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(id="A", label="Extracted Corpus"),
            DiagramNode(id="B", label="TF-IDF Vectorizer"),
            DiagramNode(id="C", label="Incremental Ingestion: partial_fit"),
            DiagramNode(id="D", label="Optional ONNX Sentence Embeddings"),
            DiagramNode(id="E", label="Recursive KMeans Clustering"),
            DiagramNode(
                id="F", label="Generate Dynamic Folder Structure & Sorting Plan"
            ),
        ],
        edges=[
            DiagramEdge(source="A", target="B"),
            DiagramEdge(source="B", target="C"),
            DiagramEdge(source="C", target="D"),
            DiagramEdge(source="D", target="E"),
            DiagramEdge(source="E", target="F"),
        ],
    )


def _create_multi_format_text_extraction_seq_spec() -> SequenceDiagramSpec:
    """Build multi_format_text_extraction_seq diagram specification."""
    return SequenceDiagramSpec(
        id="multi_format_text_extraction_seq",
        title="Multi-Format Text Extraction Sequence",
        autonumber=True,
        participants=[
            SequenceParticipant(id="FS", label="FileScanner"),
            SequenceParticipant(id="EX", label="Extractor Engine"),
            SequenceParticipant(id="SN", label="Text Sanitizer"),
            SequenceParticipant(id="CG", label="Corpus Generator"),
        ],
        items=[
            SequenceMessage(
                source="FS",
                target="EX",
                text="Scan target directory for supported files",
            ),
            SequenceActivation(target="EX", action="activate"),
            SequenceMessage(
                source="EX", target="EX", text="Extract raw content per format"
            ),
            SequenceMessage(source="EX", target="SN", text="Pass raw text payload"),
            SequenceActivation(target="SN", action="activate"),
            SequenceMessage(
                source="SN",
                target="SN",
                text="Sanitize text payload and filter stop words",
            ),
            SequenceMessage(
                source="SN",
                target="EX",
                text="Return sanitized text yield",
                arrow_type="-->>",
            ),
            SequenceActivation(target="SN", action="deactivate"),
            SequenceMessage(
                source="EX",
                target="CG",
                text="Yield processed text chunk to corpus generator",
                arrow_type="-->>",
            ),
            SequenceActivation(target="EX", action="deactivate"),
        ],
    )


def _create_virtual_sorting_verification_seq_spec() -> SequenceDiagramSpec:
    """Build virtual_sorting_verification_seq diagram specification."""
    return SequenceDiagramSpec(
        id="virtual_sorting_verification_seq",
        title="Virtual Sorting Plan Verification Sequence",
        autonumber=True,
        participants=[
            SequenceParticipant(id="UI", label="User Interface"),
            SequenceParticipant(id="VE", label="Verification Engine"),
            SequenceParticipant(id="FS", label="Local Filesystem Check"),
        ],
        items=[
            SequenceMessage(
                source="UI", target="VE", text="Submit proposed sorting plan"
            ),
            SequenceActivation(target="VE", action="activate"),
            SequenceMessage(
                source="VE", target="VE", text="Verify disk space across target volumes"
            ),
            SequenceMessage(
                source="VE", target="VE", text="Check path length restrictions"
            ),
            SequenceMessage(
                source="VE",
                target="FS",
                text="Verify source file accessibility and locks",
            ),
            SequenceMessage(
                source="FS", target="VE", text="Return file status", arrow_type="-->>"
            ),
            SequenceAlt(
                branches=[
                    SequenceAltBranch(
                        label="Verification Succeeded",
                        items=[
                            SequenceMessage(
                                source="VE",
                                target="UI",
                                text="Return verified status (Safe to Execute)",
                                arrow_type="-->>",
                            ),
                        ],
                    ),
                    SequenceAltBranch(
                        label="Verification Failed",
                        items=[
                            SequenceMessage(
                                source="VE",
                                target="UI",
                                text="Return error list and halt execution",
                                arrow_type="-->>",
                            ),
                        ],
                    ),
                ]
            ),
            SequenceActivation(target="VE", action="deactivate"),
        ],
    )


def _create_user_guide_setup_wizard_spec() -> ComponentDiagramSpec:
    """Build user_guide_setup_wizard diagram specification."""
    return ComponentDiagramSpec(
        id="user_guide_setup_wizard",
        title="First-Run Setup Wizard Decision Flow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="A",
                label="Launch Application",
                url="#first-run-steps--setup-wizard",
                tooltip="Launch Application Step",
            ),
            DiagramNode(
                id="B",
                label="First-Run Setup Wizard",
                shape="rhombus",
                url="#first-run-steps--setup-wizard",
                tooltip="First-Run Setup Wizard",
            ),
            DiagramNode(
                id="C",
                label="Download 80MB Model from Hugging Face",
                url="https://huggingface.co",
                tooltip="Hugging Face Model Download",
                target="_blank",
            ),
            DiagramNode(
                id="D",
                label="Enable Semantic AI Sorting",
                url="#privacy-configurations",
                tooltip="Privacy & Semantic AI Settings",
            ),
            DiagramNode(
                id="E",
                label="Fallback to Offline Non-Semantic Mode",
                url="#offline-non-semantic-mode",
                tooltip="Offline Mode Information",
            ),
            DiagramNode(
                id="F",
                label="Open User Guide",
                url="#first-run-steps--setup-wizard",
                tooltip="User Guide Reference",
            ),
            DiagramNode(
                id="G",
                label="Enter Workspace Directory Path",
                url="#first-run-steps--setup-wizard",
                tooltip="Workspace Directory Input Field (#input-dir)",
            ),
            DiagramNode(
                id="H",
                label="Select Quick Preset (Demo / Downloads / Documents)",
                url="#first-run-steps--setup-wizard",
                tooltip="Quick Preset Buttons",
            ),
            DiagramNode(
                id="I",
                label="Validate Directory Path",
                shape="rhombus",
                url="#first-run-steps--setup-wizard",
                tooltip="Directory Path Validation",
            ),
            DiagramNode(
                id="J",
                label="Display Inline Error Banner",
                url="#first-run-steps--setup-wizard",
                tooltip="Inline Error Banner (#lbl-error)",
            ),
            DiagramNode(
                id="K",
                label="Begin Directory Scan",
                url="#first-run-steps--setup-wizard",
                tooltip="Scan and Organize Workspace",
            ),
        ],
        edges=[
            DiagramEdge(source="A", target="B"),
            DiagramEdge(source="B", target="C", label="Accept & Download"),
            DiagramEdge(source="C", target="D", label="Download Successful"),
            DiagramEdge(source="C", target="E", label="Network Error or Offline"),
            DiagramEdge(source="B", target="E", label="Decline"),
            DiagramEdge(source="B", target="F", label="Help"),
            DiagramEdge(source="B", target="G", label="Enter Path"),
            DiagramEdge(source="B", target="H", label="Select Preset"),
            DiagramEdge(source="H", target="G", label="Populate Path"),
            DiagramEdge(source="G", target="I", label="Finish & Save"),
            DiagramEdge(source="I", target="J", label="Invalid Path"),
            DiagramEdge(source="J", target="G", label="Correct Path"),
            DiagramEdge(source="I", target="K", label="Valid Path"),
        ],
    )


def _create_user_guide_watchdog_state_spec() -> StateDiagramSpec:
    """Build user_guide_watchdog_state diagram specification."""
    return StateDiagramSpec(
        id="user_guide_watchdog_state",
        title="Watchdog Event Aggregation & Debounce State Machine",
        diagram_type="stateDiagram-v2",
        transitions=[
            StateTransition(source="[*]", target="Idle", label="Watchdog Active"),
            StateTransition(
                source="Idle", target="EventDetected", label="Directory Change Event"
            ),
            StateTransition(
                source="EventDetected",
                target="CheckTransient",
                label="Inspect File Extension",
            ),
            StateTransition(
                source="CheckTransient",
                target="TransientIgnored",
                label="Extension in (.crdownload, .tmp, .download)",
            ),
            StateTransition(
                source="TransientIgnored", target="Idle", label="Drop Transient Event"
            ),
            StateTransition(
                source="CheckTransient",
                target="StartDebounce",
                label="Valid File Extension",
            ),
            StateTransition(
                source="StartDebounce",
                target="AggregatingEvents",
                label="Standard Debounce Timer (0.6s)",
            ),
            StateTransition(
                source="AggregatingEvents",
                target="AggregatingEvents",
                label="New Event Received (Reset 0.6s Timer)",
            ),
            StateTransition(
                source="AggregatingEvents",
                target="TriggerSorting",
                label="Debounce Timer Expires (0.6s)",
            ),
            StateTransition(
                source="AggregatingEvents",
                target="TriggerSorting",
                label="Max Debounce Limit Reached (5.0s)",
            ),
            StateTransition(
                source="TriggerSorting", target="Idle", label="Execute Sorting Pipeline"
            ),
        ],
    )


def _create_api_core_architecture_spec() -> ComponentDiagramSpec:
    """Build api_core_architecture diagram specification."""
    return ComponentDiagramSpec(
        id="api_core_architecture",
        title="Core Module Architecture Flow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="A",
                label="app.main",
                url="docs/api_reference.md#appmain",
                tooltip="CLI Entrypoint Module",
            ),
            DiagramNode(
                id="B",
                label="app.core.session",
                url="docs/api_reference.md#appcoresession",
                tooltip="Session Management Module",
            ),
            DiagramNode(
                id="C",
                label="app.core.extractor",
                url="docs/api_reference.md#appcoreextractor",
                tooltip="Multi-format Text Extractor Module",
            ),
            DiagramNode(
                id="D",
                label="app.core.analyzer",
                url="docs/api_reference.md#appcoreanalyzer",
                tooltip="Document Analyzer Module",
            ),
            DiagramNode(
                id="E",
                label="app.core.verifier",
                url="docs/api_reference.md#appcoreverifier",
                tooltip="Virtual Sorting Verifier Module",
            ),
            DiagramNode(
                id="F",
                label="app.core.sanitizer",
                url="docs/api_reference.md#appcoresanitizer",
                tooltip="Path & Input Sanitizer Module",
            ),
            DiagramNode(
                id="G",
                label="app.core.analyzer_strategies",
                url="docs/api_reference.md#appcoreanalyzer_strategies",
                tooltip="Analysis Strategy Implementations",
            ),
        ],
        edges=[
            DiagramEdge(source="A", target="B"),
            DiagramEdge(source="B", target="C"),
            DiagramEdge(source="B", target="D"),
            DiagramEdge(source="B", target="E"),
            DiagramEdge(source="C", target="F"),
            DiagramEdge(source="D", target="G"),
        ],
    )


def _create_admin_policy_evaluation_spec() -> ComponentDiagramSpec:
    """Build admin_policy_evaluation diagram specification."""
    return ComponentDiagramSpec(
        id="admin_policy_evaluation",
        title="Policy Evaluation Flowchart",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(id="A", label="Incoming Document"),
            DiagramNode(id="B", label="Sort Rules by Priority High to Low"),
            DiagramNode(id="C", label="Evaluate Next Rule", shape="rhombus"),
            DiagramNode(id="D", label="Route Document via Override Path"),
            DiagramNode(id="E", label="Route Document via Keyword Category"),
            DiagramNode(id="F", label="Route Document via Pattern Category"),
            DiagramNode(id="G", label="Stop Processing & Halt Evaluation"),
            DiagramNode(id="H", label="More Rules Remaining?", shape="rhombus"),
            DiagramNode(id="I", label="Proceed to General Classification / AI Sorting"),
        ],
        edges=[
            DiagramEdge(source="A", target="B"),
            DiagramEdge(source="B", target="C"),
            DiagramEdge(source="C", target="D", label="Override Rule Match"),
            DiagramEdge(source="C", target="E", label="Keyword Rule Match"),
            DiagramEdge(source="C", target="F", label="Pattern Rule Match"),
            DiagramEdge(
                source="C", target="G", label="No Match & Halt on Mismatch Enabled"
            ),
            DiagramEdge(source="C", target="H", label="No Match & Halt Disabled"),
            DiagramEdge(source="H", target="C", label="Yes"),
            DiagramEdge(source="H", target="I", label="No"),
        ],
    )


def _create_core_quarantine_interceptor_spec() -> ComponentDiagramSpec:
    """Build core_quarantine_interceptor diagram specification."""
    return ComponentDiagramSpec(
        id="core_quarantine_interceptor",
        title="Quarantine Interceptor Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="eval",
                label="Quarantine Evaluation",
                shape="rhombus",
                url="docs/api_reference.md#appcorequarantine_interceptor",
                tooltip="Quarantine Evaluation Node",
            ),
            DiagramNode(
                id="policy",
                label="Compliance Policy Check",
                shape="rectangle",
                url="docs/admin_guide.md",
                tooltip="Compliance Policy Check",
            ),
            DiagramNode(
                id="isolate",
                label="Isolate Quarantine Record",
                shape="rectangle",
                url="docs/api_reference.md#appcorequarantine_interceptor",
                tooltip="Isolate File Record",
            ),
            DiagramNode(
                id="notify",
                label="Audit Trail Event Logger",
                shape="database",
                url="docs/api_reference.md#appcoreaudit_reporter",
                tooltip="Logger Node",
            ),
            DiagramNode(
                id="bypass",
                label="Safe Path Release",
                shape="stadium",
                tooltip="Bypass Release",
            ),
        ],
        edges=[
            DiagramEdge(source="eval", target="policy", label="Flagged Violation"),
            DiagramEdge(source="policy", target="isolate", label="Confirm Isolation"),
            DiagramEdge(source="isolate", target="notify", label="Log Audit Record"),
            DiagramEdge(source="eval", target="bypass", label="Clean File"),
        ],
    )


def _create_core_offline_loader_spec() -> ComponentDiagramSpec:
    """Build core_offline_loader diagram specification."""
    return ComponentDiagramSpec(
        id="core_offline_loader",
        title="Offline Bundle Loader & Hydration Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="check",
                label="Offline Bundle Inspection",
                shape="rhombus",
                url="docs/user_guide.md#offline-non-semantic-mode",
                tooltip="Offline Bundle Inspection",
            ),
            DiagramNode(
                id="load",
                label="Load Pre-packaged Weights",
                shape="subroutine",
                url="docs/api_reference.md#appcoreoffline_loader",
                tooltip="Load Weights",
            ),
            DiagramNode(
                id="verify",
                label="Integrity Checksum Validation",
                shape="rhombus",
                url="docs/api_reference.md#appcorecrypto",
                tooltip="Verify Checksum",
            ),
            DiagramNode(
                id="hydrate",
                label="Hydrate FastEmbed Engine",
                shape="stadium",
                url="docs/api_reference.md#appcoresemantic_embeddings",
                tooltip="Hydrate Embeddings Engine",
            ),
            DiagramNode(
                id="fallback",
                label="Fallback Non-Semantic Parser",
                shape="rectangle",
                tooltip="Fallback Mode",
            ),
        ],
        edges=[
            DiagramEdge(source="check", target="load", label="Bundle Present"),
            DiagramEdge(source="load", target="verify", label="Validate SHA256"),
            DiagramEdge(source="verify", target="hydrate", label="Valid Checksum"),
            DiagramEdge(source="check", target="fallback", label="Bundle Missing"),
            DiagramEdge(source="verify", target="fallback", label="Checksum Mismatch"),
        ],
    )


def _create_core_resilient_file_ops_spec() -> ComponentDiagramSpec:
    """Build core_resilient_file_ops diagram specification."""
    return ComponentDiagramSpec(
        id="core_resilient_file_ops",
        title="Resilient File Operations Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="req",
                label="Atomic File Transaction Request",
                shape="round",
                url="docs/api_reference.md#appcoreresilient_file_ops",
                tooltip="Atomic Transaction Request",
            ),
            DiagramNode(
                id="lock",
                label="Acquire Cross-process File Lock",
                shape="rectangle",
                tooltip="Acquire Lock",
            ),
            DiagramNode(
                id="transfer",
                label="Chunked Stream Transfer",
                shape="subroutine",
                tooltip="Stream Transfer",
            ),
            DiagramNode(
                id="hash",
                label="Post-transfer SHA256 Check",
                shape="rhombus",
                tooltip="Hash Check",
            ),
            DiagramNode(
                id="rollback",
                label="Automatic Rollback & Cleanup",
                shape="rectangle",
                tooltip="Rollback Handler",
            ),
            DiagramNode(
                id="complete",
                label="Transaction Committed",
                shape="stadium",
                tooltip="Transaction Complete",
            ),
        ],
        edges=[
            DiagramEdge(source="req", target="lock"),
            DiagramEdge(source="lock", target="transfer", label="Lock Granted"),
            DiagramEdge(source="transfer", target="hash"),
            DiagramEdge(source="hash", target="complete", label="Hash Match"),
            DiagramEdge(source="hash", target="rollback", label="Hash Mismatch"),
            DiagramEdge(source="rollback", target="req", label="Retry"),
        ],
    )


def _create_core_simulation_exporter_spec() -> ComponentDiagramSpec:
    """Build core_simulation_exporter diagram specification."""
    return ComponentDiagramSpec(
        id="core_simulation_exporter",
        title="Simulation Exporter Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="plan",
                label="Reorganization Plan Input",
                shape="rectangle",
                url="docs/api_reference.md#appcoresimulation_exporter",
                tooltip="Plan Input",
            ),
            DiagramNode(
                id="selector",
                label="Export Format Selector",
                shape="rhombus",
                tooltip="Format Selector",
            ),
            DiagramNode(
                id="json",
                label="JSON Audit Export",
                shape="rectangle",
                tooltip="JSON Export",
            ),
            DiagramNode(
                id="csv",
                label="CSV Summary Export",
                shape="rectangle",
                tooltip="CSV Export",
            ),
            DiagramNode(
                id="html",
                label="Interactive HTML Preview",
                shape="rectangle",
                tooltip="HTML Export",
            ),
            DiagramNode(
                id="writer",
                label="Disk Output Writer",
                shape="database",
                tooltip="Output Writer",
            ),
        ],
        edges=[
            DiagramEdge(source="plan", target="selector"),
            DiagramEdge(source="selector", target="json", label="Format: JSON"),
            DiagramEdge(source="selector", target="csv", label="Format: CSV"),
            DiagramEdge(source="selector", target="html", label="Format: HTML"),
            DiagramEdge(source="json", target="writer"),
            DiagramEdge(source="csv", target="writer"),
            DiagramEdge(source="html", target="writer"),
        ],
    )


def _create_core_forensic_scanner_spec() -> ComponentDiagramSpec:
    """Build core_forensic_scanner diagram specification."""
    return ComponentDiagramSpec(
        id="core_forensic_scanner",
        title="Forensic Scanner Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="scan",
                label="Forensic Drive Scan",
                shape="round",
                url="docs/api_reference.md#appcoreforensic_scanner",
                tooltip="Drive Scan",
            ),
            DiagramNode(
                id="header",
                label="Magic Byte Header Inspection",
                shape="rhombus",
                tooltip="Header Inspection",
            ),
            DiagramNode(
                id="entropy",
                label="Entropy & Hash Analysis",
                shape="subroutine",
                tooltip="Entropy Analysis",
            ),
            DiagramNode(
                id="hidden",
                label="Hidden Stream Detection",
                shape="rectangle",
                tooltip="Hidden Detection",
            ),
            DiagramNode(
                id="report",
                label="Forensic Evidence Manifest",
                shape="database",
                tooltip="Evidence Manifest",
            ),
        ],
        edges=[
            DiagramEdge(source="scan", target="header"),
            DiagramEdge(source="header", target="entropy", label="Header Extracted"),
            DiagramEdge(source="entropy", target="hidden"),
            DiagramEdge(source="hidden", target="report", label="Emit Manifest"),
        ],
    )


def _create_core_jev_classifier_spec() -> ComponentDiagramSpec:
    """Build core_jev_classifier diagram specification."""
    return ComponentDiagramSpec(
        id="core_jev_classifier",
        title="JEV Classifier Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="features",
                label="Extracted Feature Matrix",
                shape="rectangle",
                url="docs/api_reference.md#appcorejev_classifier",
                tooltip="Feature Matrix",
            ),
            DiagramNode(
                id="fastpath",
                label="Heuristic Rule Fast-Path",
                shape="rhombus",
                tooltip="Fast Path Check",
            ),
            DiagramNode(
                id="model",
                label="Joint Embedding Classifier",
                shape="subroutine",
                tooltip="JEV Model Exec",
            ),
            DiagramNode(
                id="confidence",
                label="Confidence Threshold Guard",
                shape="rhombus",
                tooltip="Confidence Guard",
            ),
            DiagramNode(
                id="category",
                label="Assigned Category Label",
                shape="stadium",
                tooltip="Output Category",
            ),
        ],
        edges=[
            DiagramEdge(source="features", target="fastpath"),
            DiagramEdge(source="fastpath", target="category", label="Certain Match"),
            DiagramEdge(source="fastpath", target="model", label="Uncertain Match"),
            DiagramEdge(source="model", target="confidence"),
            DiagramEdge(source="confidence", target="category", label="Score >= 0.8"),
        ],
    )


def _create_core_semantic_embeddings_spec() -> ComponentDiagramSpec:
    """Build core_semantic_embeddings diagram specification."""
    return ComponentDiagramSpec(
        id="core_semantic_embeddings",
        title="Semantic Embeddings Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="text",
                label="Sanitized Document Text",
                shape="rectangle",
                url="docs/api_reference.md#appcoresemantic_embeddings",
                tooltip="Document Text Input",
            ),
            DiagramNode(
                id="cache",
                label="ONNX Model Property Cache",
                shape="database",
                url="docs/api_reference.md#appcorecache",
                tooltip="Model Cache",
            ),
            DiagramNode(
                id="engine",
                label="FastEmbed Embedding Engine",
                shape="subroutine",
                tooltip="Embedding Engine",
            ),
            DiagramNode(
                id="vector",
                label="Dense Vector Embedding Output",
                shape="stadium",
                tooltip="Vector Output [384]",
            ),
        ],
        edges=[
            DiagramEdge(source="text", target="cache", label="Check Initialized"),
            DiagramEdge(source="cache", target="engine", label="Pass Weights"),
            DiagramEdge(source="engine", target="vector", label="Emit Vector"),
        ],
    )


def _create_core_mover_spec() -> ComponentDiagramSpec:
    """Build core_mover diagram specification."""
    return ComponentDiagramSpec(
        id="core_mover",
        title="File Mover Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="queue",
                label="Batch Move Task Queue",
                shape="rectangle",
                url="docs/api_reference.md#appcoremover",
                tooltip="Move Task Queue",
            ),
            DiagramNode(
                id="pool",
                label="SharedWorkerPool Dispatcher",
                shape="subroutine",
                url="docs/api_reference.md#appcoreworker_pool",
                tooltip="Worker Dispatcher",
            ),
            DiagramNode(
                id="ops",
                label="Resilient Transfer Execution",
                shape="rectangle",
                url="docs/api_reference.md#appcoreresilient_file_ops",
                tooltip="Resilient Ops",
            ),
            DiagramNode(
                id="history",
                label="Transaction History Logger",
                shape="database",
                url="docs/api_reference.md#appcorehistory",
                tooltip="History Logger",
            ),
        ],
        edges=[
            DiagramEdge(source="queue", target="pool", label="Enqueue Tasks"),
            DiagramEdge(source="pool", target="ops", label="Parallel Exec"),
            DiagramEdge(source="ops", target="history", label="Record Event"),
        ],
    )


def _create_core_verifier_spec() -> ComponentDiagramSpec:
    """Build core_verifier diagram specification."""
    return ComponentDiagramSpec(
        id="core_verifier",
        title="Plan Verifier Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="plan",
                label="Proposed Reorganization Plan",
                shape="rectangle",
                url="docs/api_reference.md#appcoreverifier",
                tooltip="Plan Spec",
            ),
            DiagramNode(
                id="space",
                label="Target Volume Space Check",
                shape="rhombus",
                tooltip="Space Check",
            ),
            DiagramNode(
                id="pathlen",
                label="OS Path Length Constraints Check",
                shape="rhombus",
                tooltip="Path Length Check",
            ),
            DiagramNode(
                id="lock",
                label="Source File Access Lock Check",
                shape="rhombus",
                tooltip="Lock Check",
            ),
            DiagramNode(
                id="pass_node",
                label="Plan Verified Safe",
                shape="stadium",
                tooltip="Verification Passed",
            ),
            DiagramNode(
                id="fail_node",
                label="Verification Error Manifest",
                shape="rectangle",
                tooltip="Verification Failed",
            ),
        ],
        edges=[
            DiagramEdge(source="plan", target="space"),
            DiagramEdge(source="space", target="pathlen", label="Space OK"),
            DiagramEdge(source="space", target="fail_node", label="Disk Space Error"),
            DiagramEdge(source="pathlen", target="lock", label="Length OK"),
            DiagramEdge(source="pathlen", target="fail_node", label="Length Exceeded"),
            DiagramEdge(source="lock", target="pass_node", label="No Locks"),
            DiagramEdge(source="lock", target="fail_node", label="File Locked"),
        ],
    )


def _create_core_policy_engine_spec() -> ComponentDiagramSpec:
    """Build core_policy_engine diagram specification."""
    return ComponentDiagramSpec(
        id="core_policy_engine",
        title="Policy Engine Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="doc",
                label="Target Document Metadata",
                shape="rectangle",
                url="docs/api_reference.md#appcorepolicy_engine",
                tooltip="Document Input",
            ),
            DiagramNode(
                id="templates",
                label="Rule Templates Engine",
                shape="database",
                url="docs/api_reference.md#appcorerule_templates",
                tooltip="Templates Engine",
            ),
            DiagramNode(
                id="sort",
                label="Sort Policy Rules High-to-Low",
                shape="rectangle",
                tooltip="Sort Priority",
            ),
            DiagramNode(
                id="eval",
                label="Evaluate Next Policy Rule",
                shape="rhombus",
                tooltip="Evaluate Rule",
            ),
            DiagramNode(
                id="quarantine",
                label="Quarantine Interceptor",
                shape="rectangle",
                url="docs/api_reference.md#appcorequarantine_interceptor",
                tooltip="Quarantine Trigger",
            ),
            DiagramNode(
                id="route",
                label="Assigned Destination Category",
                shape="stadium",
                tooltip="Destination Category",
            ),
        ],
        edges=[
            DiagramEdge(source="doc", target="sort"),
            DiagramEdge(source="sort", target="templates", label="Fetch Rules"),
            DiagramEdge(source="templates", target="eval"),
            DiagramEdge(source="eval", target="quarantine", label="Violation Match"),
            DiagramEdge(source="eval", target="route", label="Rule Matched"),
        ],
    )


def _create_core_daemon_spec() -> ComponentDiagramSpec:
    """Build core_daemon diagram specification."""
    return ComponentDiagramSpec(
        id="core_daemon",
        title="Daemon Service Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="start",
                label="Daemon Service Initializer",
                shape="round",
                url="docs/api_reference.md#appcoredaemon",
                tooltip="Daemon Initializer",
            ),
            DiagramNode(
                id="ipc",
                label="IPC Server Listener",
                shape="subroutine",
                url="docs/api_reference.md#appcoreipc",
                tooltip="IPC Server",
            ),
            DiagramNode(
                id="watchdog",
                label="Background Watchdog Observer",
                shape="rectangle",
                tooltip="Watchdog Observer",
            ),
            DiagramNode(
                id="session",
                label="Session Controller Instance",
                shape="rectangle",
                url="docs/api_reference.md#appcoresession",
                tooltip="Session Instance",
            ),
            DiagramNode(
                id="stop",
                label="Clean Shutdown Handler",
                shape="stadium",
                tooltip="Shutdown Handler",
            ),
        ],
        edges=[
            DiagramEdge(source="start", target="ipc"),
            DiagramEdge(source="ipc", target="session", label="RPC Calls"),
            DiagramEdge(source="watchdog", target="session", label="FS Events"),
            DiagramEdge(source="session", target="stop", label="Termination Signal"),
        ],
    )


def _create_core_ipc_spec() -> ComponentDiagramSpec:
    """Build core_ipc diagram specification."""
    return ComponentDiagramSpec(
        id="core_ipc",
        title="IPC Transport Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="client",
                label="IPC Client Connection",
                shape="round",
                url="docs/api_reference.md#appcoreipc",
                tooltip="IPC Client",
            ),
            DiagramNode(
                id="pipe",
                label="Named Pipe Stream",
                shape="rectangle",
                tooltip="Named Pipe",
            ),
            DiagramNode(
                id="serializer",
                label="JSON-RPC Message Serializer",
                shape="subroutine",
                tooltip="Message Serializer",
            ),
            DiagramNode(
                id="handler",
                label="Daemon Command Handler",
                shape="rectangle",
                url="docs/api_reference.md#appcoredaemon",
                tooltip="Command Handler",
            ),
        ],
        edges=[
            DiagramEdge(source="client", target="pipe"),
            DiagramEdge(source="pipe", target="serializer", label="Send Payload"),
            DiagramEdge(source="serializer", target="handler", label="Dispatch Event"),
        ],
    )


def _create_core_plugin_registry_spec() -> ComponentDiagramSpec:
    """Build core_plugin_registry diagram specification."""
    return ComponentDiagramSpec(
        id="core_plugin_registry",
        title="Plugin Registry Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="discover",
                label="Discover Extension Modules",
                shape="rectangle",
                url="docs/api_reference.md#appcoreplugin_registry",
                tooltip="Discover Extensions",
            ),
            DiagramNode(
                id="validate",
                label="Plugin Manifest Integrity Check",
                shape="rhombus",
                tooltip="Manifest Validation",
            ),
            DiagramNode(
                id="hooks",
                label="Register Analysis Hooks",
                shape="subroutine",
                tooltip="Register Hooks",
            ),
            DiagramNode(
                id="pool",
                label="Active Plugin Registry Pool",
                shape="database",
                tooltip="Plugin Pool",
            ),
        ],
        edges=[
            DiagramEdge(source="discover", target="validate"),
            DiagramEdge(source="validate", target="hooks", label="Valid Signature"),
            DiagramEdge(source="hooks", target="pool", label="Loaded"),
        ],
    )


def _create_core_audit_reporter_spec() -> ComponentDiagramSpec:
    """Build core_audit_reporter diagram specification."""
    return ComponentDiagramSpec(
        id="core_audit_reporter",
        title="Audit Reporter Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="events",
                label="In-Memory Audit Events Stream",
                shape="rectangle",
                url="docs/api_reference.md#appcoreaudit_reporter",
                tooltip="Audit Stream",
            ),
            DiagramNode(
                id="formatter",
                label="Report Formatter (HTML/PDF/JSON)",
                shape="subroutine",
                tooltip="Report Formatter",
            ),
            DiagramNode(
                id="signature",
                label="Cryptographic Signature Generator",
                shape="rhombus",
                url="docs/api_reference.md#appcorecrypto",
                tooltip="Signature Generator",
            ),
            DiagramNode(
                id="output",
                label="Audit Report Output File",
                shape="database",
                tooltip="Audit Output File",
            ),
        ],
        edges=[
            DiagramEdge(source="events", target="formatter"),
            DiagramEdge(source="formatter", target="signature", label="Generate Hash"),
            DiagramEdge(source="signature", target="output", label="Write File"),
        ],
    )


def _create_core_crypto_spec() -> ComponentDiagramSpec:
    """Build core_crypto diagram specification."""
    return ComponentDiagramSpec(
        id="core_crypto",
        title="Cryptographic Services Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="req",
                label="Cryptographic Operation Request",
                shape="round",
                url="docs/api_reference.md#appcorecrypto",
                tooltip="Crypto Request",
            ),
            DiagramNode(
                id="key",
                label="Master Key Provider",
                shape="database",
                tooltip="Key Provider",
            ),
            DiagramNode(
                id="engine",
                label="SHA256 / AES-256 Engine",
                shape="subroutine",
                tooltip="Crypto Engine",
            ),
            DiagramNode(
                id="res",
                label="Encrypted Payload / Digest",
                shape="stadium",
                tooltip="Crypto Result",
            ),
        ],
        edges=[
            DiagramEdge(source="req", target="key"),
            DiagramEdge(source="key", target="engine", label="Supply Key Material"),
            DiagramEdge(source="engine", target="res"),
        ],
    )


def _create_core_db_spec() -> ComponentDiagramSpec:
    """Build core_db diagram specification."""
    return ComponentDiagramSpec(
        id="core_db",
        title="Database Persistence Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="query",
                label="Database Transaction Request",
                shape="round",
                url="docs/api_reference.md#appcoredb",
                tooltip="DB Transaction Request",
            ),
            DiagramNode(
                id="cache",
                label="Query Cache Layer",
                shape="database",
                url="docs/api_reference.md#appcorecache",
                tooltip="Query Cache",
            ),
            DiagramNode(
                id="pool",
                label="SQLite Connection Pool",
                shape="subroutine",
                url="docs/api_reference.md#appcoredb_conn",
                tooltip="Connection Pool",
            ),
            DiagramNode(
                id="storage",
                label="On-Disk Encrypted Database",
                shape="database",
                tooltip="Encrypted Storage",
            ),
        ],
        edges=[
            DiagramEdge(source="query", target="cache", label="Cache Check"),
            DiagramEdge(source="cache", target="pool", label="Cache Miss"),
            DiagramEdge(source="pool", target="storage", label="Read / Write"),
        ],
    )


def _create_core_history_spec() -> ComponentDiagramSpec:
    """Build core_history diagram specification."""
    return ComponentDiagramSpec(
        id="core_history",
        title="History & Reverse-Replay Undo Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="moves",
                label="Canonical MoveLedger (moves)",
                shape="database",
                url="docs/api_reference.md#appcoremoves",
                tooltip="Canonical Move Ledger",
            ),
            DiagramNode(
                id="replay",
                label="Reverse Move Replay Engine",
                shape="subroutine",
                url="docs/api_reference.md#appcorehistory",
                tooltip="Backwards Move Replay",
            ),
            DiagramNode(
                id="db",
                label="Encrypted Document Metadata Sync",
                shape="subroutine",
                url="docs/api_reference.md#appcoredb",
                tooltip="DB Path Sync",
            ),
            DiagramNode(
                id="audit",
                label="Session History & Audit Export",
                shape="stadium",
                url="docs/api_reference.md#appcorehistory",
                tooltip="History & Audit View",
            ),
        ],
        edges=[
            DiagramEdge(source="moves", target="replay", label="Replay Verified/Done"),
            DiagramEdge(source="replay", target="db", label="Restore Paths"),
            DiagramEdge(source="moves", target="audit", label="Query & Export"),
        ],
    )


def _create_core_ledger_spec() -> ComponentDiagramSpec:
    """Build core_ledger diagram specification."""
    return ComponentDiagramSpec(
        id="core_ledger",
        title="Key Lifecycle Audit & Redaction Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="crypto_event",
                label="Key Export / Rotation Event",
                shape="round",
                url="docs/api_reference.md#appcorecrypto",
                tooltip="Key Lifecycle Event",
            ),
            DiagramNode(
                id="sanitizer",
                label="Secret Metadata Redactor",
                shape="rhombus",
                url="docs/api_reference.md#appcoreledger",
                tooltip="sanitize_audit_metadata",
            ),
            DiagramNode(
                id="audit_table",
                label="Append-Only key_audit_ledger",
                shape="database",
                url="docs/api_reference.md#appcoreledger",
                tooltip="Key Audit Table",
            ),
            DiagramNode(
                id="audit_query",
                label="Key Audit Trail Query",
                shape="stadium",
                url="docs/api_reference.md#appcoreledger",
                tooltip="get_key_audit_events",
            ),
        ],
        edges=[
            DiagramEdge(
                source="crypto_event", target="sanitizer", label="Redact Secrets"
            ),
            DiagramEdge(source="sanitizer", target="audit_table", label="Commit Event"),
            DiagramEdge(source="audit_table", target="audit_query"),
        ],
    )


def _create_core_session_spec() -> ComponentDiagramSpec:
    """Build core_session diagram specification."""
    return ComponentDiagramSpec(
        id="core_session",
        title="Session Lifecycle Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="start",
                label="Initialize Application Session",
                shape="round",
                url="docs/api_reference.md#appcoresession",
                tooltip="Session Start",
            ),
            DiagramNode(
                id="scanner",
                label="Directory Scanner Instance",
                shape="rectangle",
                url="docs/api_reference.md#appcorescanner",
                tooltip="Scanner Instance",
            ),
            DiagramNode(
                id="analyzer",
                label="Document Analyzer Instance",
                shape="rectangle",
                url="docs/api_reference.md#appcoreanalyzer",
                tooltip="Analyzer Instance",
            ),
            DiagramNode(
                id="state",
                label="Active Session Model",
                shape="database",
                tooltip="Session Model State",
            ),
        ],
        edges=[
            DiagramEdge(source="start", target="scanner"),
            DiagramEdge(source="start", target="analyzer"),
            DiagramEdge(source="scanner", target="state"),
            DiagramEdge(source="analyzer", target="state"),
        ],
    )


def _create_core_downloader_spec() -> ComponentDiagramSpec:
    """Build core_downloader diagram specification."""
    return ComponentDiagramSpec(
        id="core_downloader",
        title="Model Downloader Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="req",
                label="Model Download Request",
                shape="round",
                url="docs/api_reference.md#appcoredownloader",
                tooltip="Download Request",
            ),
            DiagramNode(
                id="net",
                label="Network Connectivity Test",
                shape="rhombus",
                url="docs/api_reference.md#appcoreenv_helper",
                tooltip="Connectivity Check",
            ),
            DiagramNode(
                id="progress",
                label="Progress Tracker",
                shape="rectangle",
                url="docs/api_reference.md#appcoreprogress",
                tooltip="Progress Tracker",
            ),
            DiagramNode(
                id="bundle",
                label="Save Model Weights Bundle",
                shape="database",
                url="docs/api_reference.md#appcoreoffline_loader",
                tooltip="Save Bundle",
            ),
            DiagramNode(
                id="fallback",
                label="Fallback to Offline Mode",
                shape="stadium",
                tooltip="Offline Fallback",
            ),
        ],
        edges=[
            DiagramEdge(source="req", target="net"),
            DiagramEdge(source="net", target="progress", label="Online"),
            DiagramEdge(source="net", target="fallback", label="Offline"),
            DiagramEdge(source="progress", target="bundle", label="Complete"),
        ],
    )


def _create_core_extractor_spec() -> ComponentDiagramSpec:
    """Build core_extractor diagram specification."""
    return ComponentDiagramSpec(
        id="core_extractor",
        title="Document Extractor Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="file",
                label="Target Document File",
                shape="rectangle",
                url="docs/api_reference.md#appcoreextractor",
                tooltip="Document File Input",
            ),
            DiagramNode(
                id="router",
                label="Extractor Strategies Dispatcher",
                shape="rhombus",
                url="docs/api_reference.md#appcoreextractor_strategies",
                tooltip="Strategy Router",
            ),
            DiagramNode(
                id="extractor",
                label="Structured Text Extractor",
                shape="subroutine",
                tooltip="Text Extractor Exec",
            ),
            DiagramNode(
                id="forensic",
                label="Forensic Scanner Fallback",
                shape="rectangle",
                url="docs/api_reference.md#appcoreforensic_scanner",
                tooltip="Forensic Fallback",
            ),
            DiagramNode(
                id="text",
                label="Extracted Text Payload",
                shape="stadium",
                tooltip="Extracted Text Output",
            ),
        ],
        edges=[
            DiagramEdge(source="file", target="router"),
            DiagramEdge(source="router", target="extractor", label="Standard Format"),
            DiagramEdge(
                source="router", target="forensic", label="Unrecognized Format"
            ),
            DiagramEdge(source="extractor", target="text"),
            DiagramEdge(source="forensic", target="text"),
        ],
    )


def _create_core_file_renamer_spec() -> ComponentDiagramSpec:
    """Build core_file_renamer diagram specification."""
    return ComponentDiagramSpec(
        id="core_file_renamer",
        title="File Renamer Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="rename",
                label="Target File Rename Spec",
                shape="rectangle",
                url="docs/api_reference.md#appcorefile_renamer",
                tooltip="Rename Task Spec",
            ),
            DiagramNode(
                id="fmt",
                label="Pattern Formatter Engine",
                shape="subroutine",
                url="docs/api_reference.md#appcorepattern_formatter",
                tooltip="Pattern Formatter",
            ),
            DiagramNode(
                id="sanitizer",
                label="Filename Path Sanitizer",
                shape="rhombus",
                url="docs/api_reference.md#appcorepath_utils",
                tooltip="Path Sanitizer",
            ),
            DiagramNode(
                id="ops",
                label="Resilient File Operation",
                shape="rectangle",
                url="docs/api_reference.md#appcoreresilient_file_ops",
                tooltip="Resilient Move Exec",
            ),
        ],
        edges=[
            DiagramEdge(source="rename", target="fmt"),
            DiagramEdge(source="fmt", target="sanitizer", label="Formatted Name"),
            DiagramEdge(source="sanitizer", target="ops", label="Sanitized Target"),
        ],
    )


def _create_core_scanner_spec() -> ComponentDiagramSpec:
    """Build core_scanner diagram specification."""
    return ComponentDiagramSpec(
        id="core_scanner",
        title="Directory Scanner Subsystem Workflow",
        diagram_type="flowchart",
        direction="TD",
        nodes=[
            DiagramNode(
                id="dir_node",
                label="Target Search Directory",
                shape="rectangle",
                url="docs/api_reference.md#appcorescanner",
                tooltip="Search Directory",
            ),
            DiagramNode(
                id="walk",
                label="Recursive Directory Walk",
                shape="subroutine",
                tooltip="Recursive Walk Exec",
            ),
            DiagramNode(
                id="filter_node",
                label="Ignore & Transient Filter",
                shape="rhombus",
                url="docs/api_reference.md#appcorepath_utils",
                tooltip="Transient Filter",
            ),
            DiagramNode(
                id="stream",
                label="Discovered File Stream",
                shape="stadium",
                tooltip="File Stream Yield",
            ),
        ],
        edges=[
            DiagramEdge(source="dir_node", target="walk"),
            DiagramEdge(source="walk", target="filter_node"),
            DiagramEdge(source="filter_node", target="stream", label="Valid Files"),
        ],
    )


_SPEC_FACTORIES: dict[str, Callable[[], BaseDiagramSpec]] = {
    "core_architecture": _create_core_architecture_spec,
    "architecture_dataflow": _create_architecture_dataflow_spec,
    "cro_multi_study_pipeline": _create_cro_multi_study_pipeline_spec,
    "memory_cache_layers": _create_memory_cache_layers_spec,
    "worker_pool_concurrency": _create_worker_pool_concurrency_spec,
    "policy_evaluation_flow": _create_policy_evaluation_flow_spec,
    "setup_wizard_flow": _create_setup_wizard_flow_spec,
    "architecture_async_processing": _create_architecture_async_processing_spec,
    "architecture_watchdog_state": _create_architecture_watchdog_state_spec,
    "catalog_workflow": _create_catalog_workflow_spec,
    "ui_component_hierarchy": _create_ui_component_hierarchy_spec,
    "core_text_extraction": _create_core_text_extraction_spec,
    "contributor_onboarding": _create_contributor_onboarding_spec,
    "troubleshooting_setup_wizard": _create_troubleshooting_setup_wizard_spec,
    "ml_analyzer_clustering": _create_ml_analyzer_clustering_spec,
    "multi_format_text_extraction_seq": _create_multi_format_text_extraction_seq_spec,
    "virtual_sorting_verification_seq": _create_virtual_sorting_verification_seq_spec,
    "user_guide_setup_wizard": _create_user_guide_setup_wizard_spec,
    "user_guide_watchdog_state": _create_user_guide_watchdog_state_spec,
    "api_core_architecture": _create_api_core_architecture_spec,
    "admin_policy_evaluation": _create_admin_policy_evaluation_spec,
    "core_quarantine_interceptor": _create_core_quarantine_interceptor_spec,
    "core_offline_loader": _create_core_offline_loader_spec,
    "core_resilient_file_ops": _create_core_resilient_file_ops_spec,
    "core_simulation_exporter": _create_core_simulation_exporter_spec,
    "core_forensic_scanner": _create_core_forensic_scanner_spec,
    "core_jev_classifier": _create_core_jev_classifier_spec,
    "core_semantic_embeddings": _create_core_semantic_embeddings_spec,
    "core_mover": _create_core_mover_spec,
    "core_verifier": _create_core_verifier_spec,
    "core_policy_engine": _create_core_policy_engine_spec,
    "core_daemon": _create_core_daemon_spec,
    "core_ipc": _create_core_ipc_spec,
    "core_plugin_registry": _create_core_plugin_registry_spec,
    "core_audit_reporter": _create_core_audit_reporter_spec,
    "core_crypto": _create_core_crypto_spec,
    "core_db": _create_core_db_spec,
    "core_history": _create_core_history_spec,
    "core_ledger": _create_core_ledger_spec,
    "core_session": _create_core_session_spec,
    "core_downloader": _create_core_downloader_spec,
    "core_extractor": _create_core_extractor_spec,
    "core_file_renamer": _create_core_file_renamer_spec,
    "core_scanner": _create_core_scanner_spec,
    "architecture_move_engine_state": _create_architecture_move_engine_state_spec,
    "architecture_pipeline_state": _create_architecture_pipeline_state_spec,
    "architecture_session_state": _create_architecture_session_state_spec,
    "architecture_download_state": _create_architecture_download_state_spec,
}

CONSTANT_TO_KEY_MAP: dict[str, str] = {
    "CORE_ARCHITECTURE_SPEC": "core_architecture",
    "ARCHITECTURE_DATAFLOW_SPEC": "architecture_dataflow",
    "CRO_MULTI_STUDY_PIPELINE_SPEC": "cro_multi_study_pipeline",
    "MEMORY_CACHE_LAYERS_SPEC": "memory_cache_layers",
    "WORKER_POOL_CONCURRENCY_SPEC": "worker_pool_concurrency",
    "POLICY_EVALUATION_FLOW_SPEC": "policy_evaluation_flow",
    "SETUP_WIZARD_FLOW_SPEC": "setup_wizard_flow",
    "ARCHITECTURE_ASYNC_PROCESSING_SPEC": "architecture_async_processing",
    "ARCHITECTURE_WATCHDOG_STATE_SPEC": "architecture_watchdog_state",
    "ARCHITECTURE_MOVE_ENGINE_STATE_SPEC": "architecture_move_engine_state",
    "MOVE_ENGINE_STATE_SPEC": "architecture_move_engine_state",
    "ARCHITECTURE_PIPELINE_STATE_SPEC": "architecture_pipeline_state",
    "PIPELINE_LIFECYCLE_STATE_SPEC": "architecture_pipeline_state",
    "ARCHITECTURE_SESSION_STATE_SPEC": "architecture_session_state",
    "SESSION_STATE_MACHINE_SPEC": "architecture_session_state",
    "ARCHITECTURE_DOWNLOAD_STATE_SPEC": "architecture_download_state",
    "DOWNLOAD_STATE_SPEC": "architecture_download_state",
    "CATALOG_WORKFLOW_SPEC": "catalog_workflow",
    "UI_COMPONENT_HIERARCHY_SPEC": "ui_component_hierarchy",
    "CORE_TEXT_EXTRACTION_SPEC": "core_text_extraction",
    "CONTRIBUTOR_ONBOARDING_SPEC": "contributor_onboarding",
    "TROUBLESHOOTING_SETUP_WIZARD_SPEC": "troubleshooting_setup_wizard",
    "ML_ANALYZER_CLUSTERING_SPEC": "ml_analyzer_clustering",
    "MULTI_FORMAT_TEXT_EXTRACTION_SEQ_SPEC": "multi_format_text_extraction_seq",
    "VIRTUAL_SORTING_VERIFICATION_SEQ_SPEC": "virtual_sorting_verification_seq",
    "USER_GUIDE_SETUP_WIZARD_SPEC": "user_guide_setup_wizard",
    "USER_GUIDE_WATCHDOG_STATE_SPEC": "user_guide_watchdog_state",
    "API_CORE_ARCHITECTURE_SPEC": "api_core_architecture",
    "ADMIN_POLICY_EVALUATION_SPEC": "admin_policy_evaluation",
    "CORE_QUARANTINE_INTERCEPTOR_SPEC": "core_quarantine_interceptor",
    "CORE_OFFLINE_LOADER_SPEC": "core_offline_loader",
    "CORE_RESILIENT_FILE_OPS_SPEC": "core_resilient_file_ops",
    "CORE_SIMULATION_EXPORTER_SPEC": "core_simulation_exporter",
    "CORE_FORENSIC_SCANNER_SPEC": "core_forensic_scanner",
    "CORE_JEV_CLASSIFIER_SPEC": "core_jev_classifier",
    "CORE_SEMANTIC_EMBEDDINGS_SPEC": "core_semantic_embeddings",
    "CORE_MOVER_SPEC": "core_mover",
    "CORE_VERIFIER_SPEC": "core_verifier",
    "CORE_POLICY_ENGINE_SPEC": "core_policy_engine",
    "CORE_DAEMON_SPEC": "core_daemon",
    "CORE_IPC_SPEC": "core_ipc",
    "CORE_PLUGIN_REGISTRY_SPEC": "core_plugin_registry",
    "CORE_AUDIT_REPORTER_SPEC": "core_audit_reporter",
    "CORE_CRYPTO_SPEC": "core_crypto",
    "CORE_DB_SPEC": "core_db",
    "CORE_HISTORY_SPEC": "core_history",
    "CORE_LEDGER_SPEC": "core_ledger",
    "CORE_SESSION_SPEC": "core_session",
    "CORE_DOWNLOADER_SPEC": "core_downloader",
    "CORE_EXTRACTOR_SPEC": "core_extractor",
    "CORE_FILE_RENAMER_SPEC": "core_file_renamer",
    "CORE_SCANNER_SPEC": "core_scanner",
}

_SPEC_LOCK = threading.RLock()
_INSTANTIATED_SPECS: dict[str, BaseDiagramSpec] = {}


def get_diagram_spec(key: str) -> BaseDiagramSpec:
    """Retrieve or lazily instantiate a diagram specification model by key."""
    if key in _INSTANTIATED_SPECS:
        return _INSTANTIATED_SPECS[key]
    with _SPEC_LOCK:
        if key in _INSTANTIATED_SPECS:
            return _INSTANTIATED_SPECS[key]
        factory = _SPEC_FACTORIES.get(key)
        if factory is None:
            raise KeyError(f"Unknown diagram specification key: '{key}'")
        spec = factory()
        _INSTANTIATED_SPECS[key] = spec
        return spec


def hydrate_core_architecture_spec(
    parsed_app_files: Sequence[tuple[str, ast.Module]] | None = None,
) -> ComponentDiagramSpec:
    """Hydrate and memoize core_architecture using pre-parsed app/ ASTs when available."""
    spec = _create_core_architecture_spec(parsed_app_files=parsed_app_files)
    with _SPEC_LOCK:
        _INSTANTIATED_SPECS["core_architecture"] = spec
    return spec


def reset_diagram_specs_cache() -> None:
    """Reset the thread-safe memoization cache of instantiated diagram specs."""
    with _SPEC_LOCK:
        _INSTANTIATED_SPECS.clear()


class LazyDiagramSpecsDict(dict):
    """Lazy evaluation dictionary mapping diagram spec keys to Pydantic diagram specs."""

    def __getitem__(self, key: str) -> BaseDiagramSpec:
        """Get spec by key lazily."""
        return get_diagram_spec(key)

    def get(self, key: str, default: Any = None, /) -> Any:  # ty: ignore[incompatible-method-override]
        """Get spec by key lazily or return default."""
        if key in _SPEC_FACTORIES:
            return get_diagram_spec(key)
        return default

    def __contains__(self, key: object) -> bool:
        """Check if key exists in spec factories."""
        return isinstance(key, str) and key in _SPEC_FACTORIES

    def __len__(self) -> int:
        """Return count of registered diagram spec factories."""
        return len(_SPEC_FACTORIES)

    def __iter__(self):
        """Iterate over registered diagram spec keys."""
        return iter(_SPEC_FACTORIES.keys())

    def keys(self):
        """Return keys of registered diagram specs."""
        return _SPEC_FACTORIES.keys()

    def values(self):
        """Return lazily instantiated diagram spec values."""
        return [get_diagram_spec(k) for k in _SPEC_FACTORIES]

    def items(self):
        """Return lazily instantiated key-spec item pairs."""
        return [(k, get_diagram_spec(k)) for k in _SPEC_FACTORIES]

    def copy(self) -> dict[str, BaseDiagramSpec]:
        """Return dictionary copy with all specs instantiated."""
        return {k: get_diagram_spec(k) for k in _SPEC_FACTORIES}

    def __repr__(self) -> str:
        """Return string representation of lazy specs dict."""
        return f"<LazyDiagramSpecsDict with {len(_SPEC_FACTORIES)} specs ({len(_INSTANTIATED_SPECS)} hydrated)>"


_LAZY_SYSTEM_DIAGRAM_SPECS = LazyDiagramSpecsDict()


def get_all_diagram_specs() -> dict[str, BaseDiagramSpec]:
    """Return dictionary of all registered system diagram specifications."""
    return dict(_LAZY_SYSTEM_DIAGRAM_SPECS)


def __getattr__(name: str) -> Any:
    """Intercept module attribute access to lazily resolve diagram specifications."""
    if name == "SYSTEM_DIAGRAM_SPECS":
        return _LAZY_SYSTEM_DIAGRAM_SPECS
    if name in CONSTANT_TO_KEY_MAP:
        return get_diagram_spec(CONSTANT_TO_KEY_MAP[name])
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def __dir__() -> list[str]:
    """Return list of module attribute names including lazy spec constants."""
    return (
        list(globals().keys())
        + ["SYSTEM_DIAGRAM_SPECS"]
        + list(CONSTANT_TO_KEY_MAP.keys())
    )
