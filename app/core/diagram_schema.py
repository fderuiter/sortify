"""Declarative Pydantic schema models for application, sequence, and state diagrams."""

import threading
from typing import Any, Callable, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "file"}


class DiagramNode(BaseModel, defer_build=True):
    """Specification model for a single diagram node vertex."""

    id: str
    label: str
    shape: Optional[str] = "rectangle"
    style: Optional[str] = None
    url: Optional[str] = None
    tooltip: Optional[str] = None
    target: Optional[str] = None
    step_number: Optional[int] = None
    step_description: Optional[str] = None

    @field_validator("url")
    @classmethod
    def validate_url_scheme(cls, v: Optional[str]) -> Optional[str]:
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
    label: Optional[str] = None
    arrow_type: str = "-->"


class DiagramSubgraph(BaseModel, defer_build=True):
    """Specification model for a subgraph boundary container in a diagram."""

    id: str
    title: str
    nodes: List[str] = Field(default_factory=list)


class ComponentDiagramSpec(BaseModel, defer_build=True):
    """Declarative specification model for component and system diagrams."""

    id: str
    title: str
    diagram_type: str = "graph"
    direction: Literal["TD", "LR", "BT", "RL"] = "TD"
    nodes: List[DiagramNode] = Field(default_factory=list)
    edges: List[DiagramEdge] = Field(default_factory=list)
    subgraphs: List[DiagramSubgraph] = Field(default_factory=list)

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
                lbl_part = f"|{edge.label}|" if edge.label else ""
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
    label: Optional[str] = None
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
    targets: List[str] = Field(default_factory=list)
    text: str


class SequenceLoop(BaseModel, defer_build=True):
    """Loop block in a sequence diagram."""

    kind: Literal["loop"] = "loop"
    label: str
    items: List["SequenceItem"] = Field(default_factory=list)


class SequenceOpt(BaseModel, defer_build=True):
    """Optional block in a sequence diagram."""

    kind: Literal["opt"] = "opt"
    label: str
    items: List["SequenceItem"] = Field(default_factory=list)


class SequenceAltBranch(BaseModel, defer_build=True):
    """Branch section in an alt/else block."""

    label: str
    items: List["SequenceItem"] = Field(default_factory=list)


class SequenceAlt(BaseModel, defer_build=True):
    """Alternative/Else block in a sequence diagram."""

    kind: Literal["alt"] = "alt"
    branches: List[SequenceAltBranch] = Field(default_factory=list)


SequenceItem = Union[
    SequenceMessage,
    SequenceActivation,
    SequenceNote,
    SequenceLoop,
    SequenceOpt,
    SequenceAlt,
]

# deferred model rebuilds


class SequenceDiagramSpec(BaseModel, defer_build=True):
    """Declarative specification model for sequence diagrams."""

    id: str
    title: str
    autonumber: bool = False
    participants: List[SequenceParticipant] = Field(default_factory=list)
    items: List[SequenceItem] = Field(default_factory=list)

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

        def compile_items(item_list: List[SequenceItem], indent_level: int = 1) -> None:
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
    label: Optional[str] = None
    is_choice: bool = False


class StateTransition(BaseModel, defer_build=True):
    """Specification model for a state transition in a state diagram."""

    source: str
    target: str
    label: Optional[str] = None


class StateComposite(BaseModel, defer_build=True):
    """Specification model for a composite state container."""

    id: str
    label: Optional[str] = None
    states: List[StateNode] = Field(default_factory=list)
    transitions: List[StateTransition] = Field(default_factory=list)


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
    direction: Optional[Literal["TB", "LR", "BT", "RL"]] = None
    states: List[StateNode] = Field(default_factory=list)
    transitions: List[StateTransition] = Field(default_factory=list)
    composite_states: List[StateComposite] = Field(default_factory=list)
    notes: List[StateNote] = Field(default_factory=list)

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

    shape: Optional[str] = "database"


class ProcessNode(DiagramNode):
    """Specification model for a process transform node in a Data Flow Diagram."""

    shape: Optional[str] = "stadium"


class ExternalEntityNode(DiagramNode):
    """Specification model for an external entity node in a Data Flow Diagram."""

    shape: Optional[str] = "rectangle"


class DataStreamEdge(DiagramEdge):
    """Specification model for a data stream contract edge in a Data Flow Diagram."""

    contract: Optional[str] = None


class DataFlowDiagramSpec(BaseModel):
    """Declarative specification model for Data Flow Diagrams (DFD)."""

    id: str
    title: str
    diagram_type: str = "flowchart"
    direction: Literal["TD", "LR", "BT", "RL"] = "TD"
    nodes: List[Union[DataStoreNode, ProcessNode, ExternalEntityNode, DiagramNode]] = (
        Field(default_factory=list)
    )
    edges: List[Union[DataStreamEdge, DiagramEdge]] = Field(default_factory=list)
    subgraphs: List[DiagramSubgraph] = Field(default_factory=list)

    def to_mermaid(self) -> str:
        """Compile DFD specification object into standard Mermaid flowchart markup string."""
        if self.diagram_type in ("graph", "flowchart"):
            lines = [f"{self.diagram_type} {self.direction}"]

            def format_node(
                node: Union[
                    DataStoreNode, ProcessNode, ExternalEntityNode, DiagramNode
                ],
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
                lbl_part = f"|{lbl_text}|" if lbl_text else ""
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


BaseDiagramSpec = Union[
    ComponentDiagramSpec, SequenceDiagramSpec, StateDiagramSpec, DataFlowDiagramSpec
]


# System Default Diagram Specifications
def _create_core_architecture_spec() -> ComponentDiagramSpec:
    """Build core_architecture diagram specification."""
    return ComponentDiagramSpec(
        id="core_architecture",
        title="Smart AutoSorter AI Pro Full System Architecture",
        diagram_type="flowchart",
        direction="TD",
        subgraphs=[
            DiagramSubgraph(
                id="ui_layer",
                title="Presentation & User Interfaces (app.tui)",
                nodes=[
                    "app_main",
                    "app_tui",
                ],
            ),
            DiagramSubgraph(
                id="session_orchestration",
                title="Session Orchestration & Lifecycle (app.core)",
                nodes=[
                    "core_session",
                    "core_user_space_bootstrap",
                    "core_daemon",
                    "core_ipc",
                    "core_integration",
                    "core_diagram_schema",
                    "core_domain_contracts",
                ],
            ),
            DiagramSubgraph(
                id="ingestion_extraction",
                title="Ingestion & Extraction Engine (app.core)",
                nodes=[
                    "core_extractor",
                    "core_extractor_strategies",
                    "core_forensic_scanner",
                    "core_offline_loader",
                    "core_downloader",
                ],
            ),
            DiagramSubgraph(
                id="analytics_intelligence",
                title="Analytics & Machine Learning (app.core)",
                nodes=[
                    "core_analyzer",
                    "core_analyzer_strategies",
                    "core_jev_classifier",
                    "core_semantic_embeddings",
                ],
            ),
            DiagramSubgraph(
                id="plugin_extensibility",
                title="Plugin Extensibility Architecture (app.core)",
                nodes=[
                    "core_plugin_registry",
                ],
            ),
            DiagramSubgraph(
                id="memory_cache",
                title="Caching & Memory Layer (app.core)",
                nodes=[
                    "core_cache",
                    "core_db_conn",
                    "core_link_manager",
                    "core_hashes_registry",
                ],
            ),
            DiagramSubgraph(
                id="concurrency_shared",
                title="Concurrency & Shared Registry (app.core)",
                nodes=[
                    "core_shared_registry",
                    "core_db_worker",
                ],
            ),
            DiagramSubgraph(
                id="storage_persistence",
                title="Storage & Database Persistence (app.core)",
                nodes=[
                    "core_db",
                    "core_ledger",
                    "core_history",
                ],
            ),
            DiagramSubgraph(
                id="execution_operations",
                title="Policy, Validation & File Operations (app.core)",
                nodes=[
                    "core_policy_engine",
                    "core_rule_templates",
                    "core_quarantine_interceptor",
                    "core_domain_contracts",
                    "core_verifier",
                    "core_simulation_exporter",
                    "core_mover",
                    "core_file_renamer",
                    "core_resilient_file_ops",
                    "core_scanner",
                    "core_progress",
                    "core_metadata",
                ],
            ),
            DiagramSubgraph(
                id="utilities_security",
                title="Security & Utilities (app.core)",
                nodes=[
                    "core_text_utils",
                    "core_path_utils",
                    "core_env_helper",
                    "core_crypto",
                    "core_security",
                    "core_domain_contracts",
                    "core_exceptions",
                    "core_domain_contracts",
                ],
            ),
        ],
        nodes=[
            # Presentation
            DiagramNode(id="app_main", label="app.main (CLI Entry Point)", shape="round"),
            DiagramNode(id="app_tui", label="app.tui (Textual Terminal Interface)"),
            # Orchestration
            DiagramNode(id="core_session", label="app.core.session"),
            DiagramNode(id="core_diagram_schema", label="app.core.diagram_schema"),
            DiagramNode(
                id="core_user_space_bootstrap", label="app.core.user_space_bootstrap"
            ),
            DiagramNode(id="core_daemon", label="app.core.daemon"),
            DiagramNode(id="core_ipc", label="app.core.ipc"),
            DiagramNode(id="core_integration", label="app.core.integration"),
            DiagramNode(id="core_domain_contracts", label="app.core.domain_contracts"),
            # Ingestion & Extraction
            DiagramNode(id="core_extractor", label="app.core.extractor"),
            DiagramNode(
                id="core_extractor_strategies", label="app.core.extractor_strategies"
            ),
            DiagramNode(id="core_forensic_scanner", label="app.core.forensic_scanner"),
            DiagramNode(id="core_offline_loader", label="app.core.offline_loader"),
            DiagramNode(id="core_downloader", label="app.core.downloader"),
            # Analytics & ML
            DiagramNode(id="core_analyzer", label="app.core.analyzer"),
            DiagramNode(
                id="core_analyzer_strategies", label="app.core.analyzer_strategies"
            ),
            DiagramNode(id="core_jev_classifier", label="app.core.jev_classifier"),
            DiagramNode(
                id="core_semantic_embeddings", label="app.core.semantic_embeddings"
            ),
            # Plugin Registry
            DiagramNode(
                id="core_plugin_registry",
                label="app.core.plugin_registry",
            ),
            # Memory & Cache
            DiagramNode(
                id="core_cache",
                label="app.core.cache (BoundedMemoryCache)",
                shape="database",
            ),
            DiagramNode(id="core_db_conn", label="app.core.db_conn"),
            DiagramNode(id="core_link_manager", label="app.core.link_manager"),
            DiagramNode(id="core_hashes_registry", label="app.core.hashes_registry"),
            # Concurrency & Shared
            DiagramNode(
                id="core_shared_registry",
                label="app.core.shared_registry (SharedWorkerPool)",
            ),
            DiagramNode(id="core_db_worker", label="app.core.db_worker"),
            # Storage
            DiagramNode(id="core_db", label="app.core.db", shape="database"),
            DiagramNode(id="core_ledger", label="app.core.ledger", shape="database"),
            DiagramNode(id="core_history", label="app.core.history", shape="database"),
            # Policy & Execution
            DiagramNode(
                id="core_policy_engine", label="app.core.policy_engine", shape="rhombus"
            ),
            DiagramNode(id="core_rule_templates", label="app.core.rule_templates"),
            DiagramNode(
                id="core_quarantine_interceptor", label="app.core.quarantine_interceptor"
            ),
            DiagramNode(id="core_domain_contracts", label="app.core.domain_contracts"),
            DiagramNode(id="core_verifier", label="app.core.verifier", shape="rhombus"),
            DiagramNode(id="core_simulation_exporter", label="app.core.simulation_exporter"),
            DiagramNode(id="core_mover", label="app.core.mover"),
            DiagramNode(id="core_file_renamer", label="app.core.file_renamer"),
            DiagramNode(id="core_resilient_file_ops", label="app.core.resilient_file_ops"),
            DiagramNode(id="core_scanner", label="app.core.scanner"),
            DiagramNode(id="core_progress", label="app.core.progress"),
            DiagramNode(id="core_metadata", label="app.core.metadata"),
            DiagramNode(id="core_domain_contracts", label="app.core.domain_contracts"),
            # Utilities
            DiagramNode(id="core_text_utils", label="app.core.text_utils"),
            DiagramNode(id="core_path_utils", label="app.core.path_utils"),
            DiagramNode(id="core_env_helper", label="app.core.env_helper"),
            DiagramNode(id="core_crypto", label="app.core.crypto"),
            DiagramNode(id="core_security", label="app.core.security"),
            DiagramNode(id="core_domain_contracts", label="app.core.domain_contracts"),
            DiagramNode(id="core_exceptions", label="app.core.exceptions"),
            DiagramNode(id="core_domain_contracts", label="app.core.domain_contracts"),
        ],
        edges=[
            DiagramEdge(source="app_main", target="app_tui"),
            DiagramEdge(source="app_main", target="core_session"),
            DiagramEdge(source="app_tui", target="core_session"),
            DiagramEdge(source="core_session", target="core_user_space_bootstrap"),
            DiagramEdge(source="core_session", target="core_scanner"),
            DiagramEdge(source="core_session", target="core_extractor"),
            DiagramEdge(source="core_session", target="core_analyzer"),
            DiagramEdge(source="core_session", target="core_policy_engine"),
            DiagramEdge(source="core_session", target="core_verifier"),
            DiagramEdge(source="core_session", target="core_mover"),
            DiagramEdge(source="core_daemon", target="core_session"),
            DiagramEdge(source="core_daemon", target="core_ipc"),
            DiagramEdge(source="core_integration", target="core_session"),
            DiagramEdge(source="core_extractor", target="core_extractor_strategies"),
            DiagramEdge(source="core_extractor", target="core_forensic_scanner"),
            DiagramEdge(source="core_extractor", target="core_offline_loader"),
            DiagramEdge(source="core_downloader", target="core_env_helper"),
            DiagramEdge(source="core_analyzer", target="core_analyzer_strategies"),
            DiagramEdge(source="core_analyzer", target="core_jev_classifier"),
            DiagramEdge(source="core_analyzer", target="core_semantic_embeddings"),
            DiagramEdge(source="core_session", target="core_plugin_registry"),
            DiagramEdge(
                source="core_quarantine_interceptor", target="core_plugin_registry"
            ),
            DiagramEdge(
                source="core_analyzer_strategies", target="core_plugin_registry"
            ),
            DiagramEdge(source="core_db_conn", target="core_cache"),
            DiagramEdge(source="core_link_manager", target="core_cache"),
            DiagramEdge(source="core_semantic_embeddings", target="core_cache"),
            DiagramEdge(source="core_jev_classifier", target="core_cache"),
            DiagramEdge(source="core_hashes_registry", target="core_crypto"),
            DiagramEdge(source="core_shared_registry", target="core_mover"),
            DiagramEdge(source="core_shared_registry", target="core_extractor"),
            DiagramEdge(source="core_db_worker", target="core_shared_registry"),
            DiagramEdge(source="core_db", target="core_cache"),
            DiagramEdge(source="core_ledger", target="core_db"),
            DiagramEdge(source="core_history", target="core_db"),
            DiagramEdge(
                source="core_policy_engine", target="core_quarantine_interceptor"
            ),
            DiagramEdge(
                source="core_quarantine_interceptor", target="core_domain_contracts"
            ),
            DiagramEdge(source="core_policy_engine", target="core_mover"),
            DiagramEdge(source="core_verifier", target="core_mover"),
            DiagramEdge(source="core_mover", target="core_file_renamer"),
            DiagramEdge(source="core_mover", target="core_resilient_file_ops"),
            DiagramEdge(source="core_mover", target="core_progress"),
            DiagramEdge(source="core_mover", target="core_metadata"),
            DiagramEdge(source="core_text_utils", target="core_exceptions"),
            DiagramEdge(source="core_path_utils", target="core_exceptions"),
        ],
    )

ARCHITECTURE_DATAFLOW_SPEC = DataFlowDiagramSpec(
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
            label="TF-IDF & NMF Clustering",
            url="docs/user_guide.md#ai-clustering-constraints",
            tooltip="Clustering Step",
            step_number=5,
            step_description="Cluster file features with TF-IDF & NMF",
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
            url="docs/ui.md#appuiplan_treeview",
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

CRO_MULTI_STUDY_PIPELINE_SPEC = ComponentDiagramSpec(
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
            id="forensic_scan", label="app.core.forensic_scanner", shape="subroutine"
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
            id="cro_pipeline_output", label="TMF Clean Binders Output", shape="stadium"
        ),
    ],
    edges=[
        DiagramEdge(
            source="forensic_scan", target="study_disambiguation", label="Raw Drives"
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

MEMORY_CACHE_LAYERS_SPEC = ComponentDiagramSpec(
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
        DiagramNode(id="db_conn_cache", label="app.core.db_conn (_connection_cache)"),
        DiagramNode(id="link_manager_cache", label="app.core.link_manager (_registry)"),
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
            source="link_manager_cache", target="bounded_cache", label="Max Size: 10000"
        ),
        DiagramEdge(
            source="semantic_cache", target="bounded_cache", label="Max Size: 500"
        ),
        DiagramEdge(
            source="jev_cache", target="bounded_cache", label="Fast Path Cache"
        ),
        DiagramEdge(source="db_cache", target="bounded_cache", label="Max Size: 10000"),
    ],
)

WORKER_POOL_CONCURRENCY_SPEC = ComponentDiagramSpec(
    id="worker_pool_concurrency",
    title="SharedWorkerPool Multi-Threaded Concurrency Model",
    diagram_type="flowchart",
    direction="TD",
    subgraphs=[
        DiagramSubgraph(
            id="pool_singleton",
            title="Shared Singleton (app.core.shared_registry)",
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
            id="shared_worker_pool", label="SharedWorkerPool Singleton", shape="stadium"
        ),
        DiagramNode(
            id="session_tasks", label="app.core.session (Lifecycle Management)"
        ),
        DiagramNode(
            id="mover_tasks", label="app.core.mover (Parallel File Relocation)"
        ),
        DiagramNode(
            id="db_worker_tasks", label="app.core.db_worker (Background Heavy Tasks)"
        ),
        DiagramNode(
            id="extractor_tasks", label="app.core.extractor (Parallel Text Ingestion)"
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

POLICY_EVALUATION_FLOW_SPEC = ComponentDiagramSpec(
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

SETUP_WIZARD_FLOW_SPEC = ComponentDiagramSpec(
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
        DiagramNode(id="E", label="Sufficient Disk Space >= 200MB?", shape="rhombus"),
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

ARCHITECTURE_ASYNC_PROCESSING_SPEC = SequenceDiagramSpec(
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
            source="ThreadPool", target="Worker", text="File list", arrow_type="-->>"
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
            source="User", target="UI", text="Drag & Drop Move / Lock Toggle"
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
                        SequenceActivation(target="ThreadPool", action="deactivate"),
                        SequenceMessage(source="UI", target="UI", text="render_tree()"),
                    ],
                ),
            ]
        ),
        SequenceActivation(target="UI", action="deactivate"),
    ],
)

ARCHITECTURE_WATCHDOG_STATE_SPEC = StateDiagramSpec(
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

CATALOG_WORKFLOW_SPEC = ComponentDiagramSpec(
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

UI_COMPONENT_HIERARCHY_SPEC = ComponentDiagramSpec(
    id="ui_component_hierarchy",
    title="UI Component Catalog Structure",
    diagram_type="graph",
    direction="LR",
    subgraphs=[
        DiagramSubgraph(
            id="nav",
            title="Navigation Components",
            nodes=["header_bar", "toolbar"],
        ),
        DiagramSubgraph(
            id="inputs",
            title="Selection & Controls",
            nodes=["directory_selection", "settings_modal", "setup_wizard"],
        ),
        DiagramSubgraph(
            id="views",
            title="Display & Audits",
            nodes=["plan_treeview", "cro_forensic_dialog", "status_progress_panel"],
        ),
    ],
    nodes=[
        DiagramNode(
            id="header_bar",
            label="Application Header Bar",
            url="docs/ui.md#appuiheader_bar",
            tooltip="Header Bar Component",
        ),
        DiagramNode(
            id="toolbar",
            label="Top Action Toolbar",
            url="docs/ui.md",
            tooltip="Top Action Toolbar",
        ),
        DiagramNode(
            id="directory_selection",
            label="Directory Selection Card",
            url="docs/ui.md#appuidirectory_selection",
            tooltip="Directory Selection Component",
        ),
        DiagramNode(
            id="settings_modal",
            label="Settings Dialog View",
            url="docs/ui.md#appuisettings_modal",
            tooltip="Settings Dialog View Component",
        ),
        DiagramNode(
            id="setup_wizard",
            label="AI Model Setup Wizard",
            url="docs/ui.md#appuisetup_wizard",
            tooltip="AI Model Setup Wizard Component",
        ),
        DiagramNode(
            id="plan_treeview",
            label="Proposed Reorganization Plan",
            url="docs/ui.md#appuiplan_treeview",
            tooltip="Proposed Reorganization Plan Component",
        ),
        DiagramNode(
            id="cro_forensic_dialog",
            label="CRO Forensic View",
            url="docs/ui.md#appuicro_forensic_dialog",
            tooltip="CRO Forensic View Component",
        ),
        DiagramNode(
            id="status_progress_panel",
            label="Status & Progress Panel",
            url="docs/ui.md#appuistatus_progress_panel",
            tooltip="Status & Progress Panel Component",
        ),
    ],
    edges=[
        DiagramEdge(source="header_bar", target="directory_selection"),
        DiagramEdge(source="directory_selection", target="plan_treeview"),
        DiagramEdge(source="plan_treeview", target="status_progress_panel"),
        DiagramEdge(source="settings_modal", target="setup_wizard"),
        DiagramEdge(source="cro_forensic_dialog", target="status_progress_panel"),
    ],
)


CORE_TEXT_EXTRACTION_SPEC = SequenceDiagramSpec(
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
            source="FS", target="EX", text="Scan target directory for supported files"
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

CONTRIBUTOR_ONBOARDING_SPEC = ComponentDiagramSpec(
    id="contributor_onboarding",
    title="Contributor Onboarding & Validation Workflow",
    diagram_type="flowchart",
    direction="TD",
    nodes=[
        DiagramNode(id="A", label="Clone Repository"),
        DiagramNode(id="B", label="Sync Environment: uv sync"),
        DiagramNode(id="C", label="Install Hooks: uv run pre-commit install"),
        DiagramNode(id="D", label="Run Test Suite: uv run pytest"),
        DiagramNode(id="E", label="Run CLI Demo: uv run smart-autosorter --demo"),
        DiagramNode(id="F", label="Run Docs Check: uv run docs --check"),
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

TROUBLESHOOTING_SETUP_WIZARD_SPEC = ComponentDiagramSpec(
    id="troubleshooting_setup_wizard",
    title="Setup Wizard Troubleshooting & Fallback Decision Flow",
    diagram_type="flowchart",
    direction="TD",
    nodes=[
        DiagramNode(id="A", label="Setup Wizard Download Triggered", shape="rectangle"),
        DiagramNode(id="B", label="Network Connection OK?", shape="rhombus"),
        DiagramNode(
            id="C", label="Check Firewall & Disconnected Status", shape="rectangle"
        ),
        DiagramNode(
            id="D", label="Fallback to Offline Non-Semantic Mode", shape="rectangle"
        ),
        DiagramNode(id="E", label="Sufficient Disk Space >= 200MB?", shape="rhombus"),
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

ML_ANALYZER_CLUSTERING_SPEC = ComponentDiagramSpec(
    id="ml_analyzer_clustering",
    title="ML Analyzer & Hierarchical Clustering Pipeline",
    diagram_type="flowchart",
    direction="TD",
    nodes=[
        DiagramNode(id="A", label="Extracted Corpus"),
        DiagramNode(id="B", label="TF-IDF Vectorizer"),
        DiagramNode(id="C", label="Incremental Ingestion: partial_fit"),
        DiagramNode(id="D", label="NMF Topic Modeling / Vector Embeddings"),
        DiagramNode(id="E", label="Recursive KMeans Clustering"),
        DiagramNode(id="F", label="Generate Dynamic Folder Structure & Sorting Plan"),
    ],
    edges=[
        DiagramEdge(source="A", target="B"),
        DiagramEdge(source="B", target="C"),
        DiagramEdge(source="C", target="D"),
        DiagramEdge(source="D", target="E"),
        DiagramEdge(source="E", target="F"),
    ],
)

MULTI_FORMAT_TEXT_EXTRACTION_SEQ_SPEC = SequenceDiagramSpec(
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
            source="FS", target="EX", text="Scan target directory for supported files"
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

VIRTUAL_SORTING_VERIFICATION_SEQ_SPEC = SequenceDiagramSpec(
    id="virtual_sorting_verification_seq",
    title="Virtual Sorting Plan Verification Sequence",
    autonumber=True,
    participants=[
        SequenceParticipant(id="UI", label="User Interface"),
        SequenceParticipant(id="VE", label="Verification Engine"),
        SequenceParticipant(id="FS", label="Local Filesystem Check"),
    ],
    items=[
        SequenceMessage(source="UI", target="VE", text="Submit proposed sorting plan"),
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

USER_GUIDE_SETUP_WIZARD_SPEC = ComponentDiagramSpec(
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
    ],
    edges=[
        DiagramEdge(source="A", target="B"),
        DiagramEdge(source="B", target="C", label="Accept & Download"),
        DiagramEdge(source="C", target="D", label="Download Successful"),
        DiagramEdge(source="C", target="E", label="Network Error or Offline"),
        DiagramEdge(source="B", target="E", label="Decline"),
        DiagramEdge(source="B", target="F", label="Help"),
    ],
)

USER_GUIDE_WATCHDOG_STATE_SPEC = StateDiagramSpec(
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

API_CORE_ARCHITECTURE_SPEC = ComponentDiagramSpec(
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
                label="TF-IDF & NMF Clustering",
                url="docs/user_guide.md#ai-clustering-constraints",
                tooltip="Clustering Step",
                step_number=5,
                step_description="Cluster file features with TF-IDF & NMF",
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
                url="docs/ui.md#appuiplan_treeview",
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
                title="Shared Singleton (app.core.shared_registry)",
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
                source="User", target="UI", text="Drag & Drop Move / Lock Toggle"
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
                title="Navigation Components",
                nodes=["header_bar", "toolbar"],
            ),
            DiagramSubgraph(
                id="inputs",
                title="Selection & Controls",
                nodes=["directory_selection", "settings_modal", "setup_wizard"],
            ),
            DiagramSubgraph(
                id="views",
                title="Display & Audits",
                nodes=["plan_treeview", "cro_forensic_dialog", "status_progress_panel"],
            ),
        ],
        nodes=[
            DiagramNode(
                id="header_bar",
                label="Application Header Bar",
                url="docs/ui.md#appuiheader_bar",
                tooltip="Header Bar Component",
            ),
            DiagramNode(
                id="toolbar",
                label="Top Action Toolbar",
                url="docs/ui.md",
                tooltip="Top Action Toolbar",
            ),
            DiagramNode(
                id="directory_selection",
                label="Directory Selection Card",
                url="docs/ui.md#appuidirectory_selection",
                tooltip="Directory Selection Component",
            ),
            DiagramNode(
                id="settings_modal",
                label="Settings Dialog View",
                url="docs/ui.md#appuisettings_modal",
                tooltip="Settings Dialog View Component",
            ),
            DiagramNode(
                id="setup_wizard",
                label="AI Model Setup Wizard",
                url="docs/ui.md#appuisetup_wizard",
                tooltip="AI Model Setup Wizard Component",
            ),
            DiagramNode(
                id="plan_treeview",
                label="Proposed Reorganization Plan",
                url="docs/ui.md#appuiplan_treeview",
                tooltip="Proposed Reorganization Plan Component",
            ),
            DiagramNode(
                id="cro_forensic_dialog",
                label="CRO Forensic View",
                url="docs/ui.md#appuicro_forensic_dialog",
                tooltip="CRO Forensic View Component",
            ),
            DiagramNode(
                id="status_progress_panel",
                label="Status & Progress Panel",
                url="docs/ui.md#appuistatus_progress_panel",
                tooltip="Status & Progress Panel Component",
            ),
        ],
        edges=[
            DiagramEdge(source="header_bar", target="directory_selection"),
            DiagramEdge(source="directory_selection", target="plan_treeview"),
            DiagramEdge(source="plan_treeview", target="status_progress_panel"),
            DiagramEdge(source="settings_modal", target="setup_wizard"),
            DiagramEdge(source="cro_forensic_dialog", target="status_progress_panel"),
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
            DiagramNode(id="C", label="Install Hooks: uv run pre-commit install"),
            DiagramNode(id="D", label="Run Test Suite: uv run pytest"),
            DiagramNode(id="E", label="Run CLI Demo: uv run smart-autosorter --demo"),
            DiagramNode(id="F", label="Run Docs Check: uv run docs --check"),
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
            DiagramNode(id="D", label="NMF Topic Modeling / Vector Embeddings"),
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
        ],
        edges=[
            DiagramEdge(source="A", target="B"),
            DiagramEdge(source="B", target="C", label="Accept & Download"),
            DiagramEdge(source="C", target="D", label="Download Successful"),
            DiagramEdge(source="C", target="E", label="Network Error or Offline"),
            DiagramEdge(source="B", target="E", label="Decline"),
            DiagramEdge(source="B", target="F", label="Help"),
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


_SPEC_FACTORIES: Dict[str, Callable[[], BaseDiagramSpec]] = {
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
}

CONSTANT_TO_KEY_MAP: Dict[str, str] = {
    "CORE_ARCHITECTURE_SPEC": "core_architecture",
    "ARCHITECTURE_DATAFLOW_SPEC": "architecture_dataflow",
    "CRO_MULTI_STUDY_PIPELINE_SPEC": "cro_multi_study_pipeline",
    "MEMORY_CACHE_LAYERS_SPEC": "memory_cache_layers",
    "WORKER_POOL_CONCURRENCY_SPEC": "worker_pool_concurrency",
    "POLICY_EVALUATION_FLOW_SPEC": "policy_evaluation_flow",
    "SETUP_WIZARD_FLOW_SPEC": "setup_wizard_flow",
    "ARCHITECTURE_ASYNC_PROCESSING_SPEC": "architecture_async_processing",
    "ARCHITECTURE_WATCHDOG_STATE_SPEC": "architecture_watchdog_state",
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
}

_SPEC_LOCK = threading.RLock()
_INSTANTIATED_SPECS: Dict[str, BaseDiagramSpec] = {}


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


def reset_diagram_specs_cache() -> None:
    """Reset the thread-safe memoization cache of instantiated diagram specs."""
    with _SPEC_LOCK:
        _INSTANTIATED_SPECS.clear()


class LazyDiagramSpecsDict(dict):
    """Lazy evaluation dictionary mapping diagram spec keys to Pydantic diagram specs."""

    def __getitem__(self, key: str) -> BaseDiagramSpec:
        """Get spec by key lazily."""
        return get_diagram_spec(key)

    def get(self, key: str, default: Any = None) -> Any:
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

    def copy(self) -> Dict[str, BaseDiagramSpec]:
        """Return dictionary copy with all specs instantiated."""
        return {k: get_diagram_spec(k) for k in _SPEC_FACTORIES}

    def __repr__(self) -> str:
        """Return string representation of lazy specs dict."""
        return f"<LazyDiagramSpecsDict with {len(_SPEC_FACTORIES)} specs ({len(_INSTANTIATED_SPECS)} hydrated)>"


_LAZY_SYSTEM_DIAGRAM_SPECS = LazyDiagramSpecsDict()


def get_all_diagram_specs() -> Dict[str, BaseDiagramSpec]:
    """Return dictionary of all registered system diagram specifications."""
    return dict(_LAZY_SYSTEM_DIAGRAM_SPECS)


def __getattr__(name: str) -> Any:
    """Intercept module attribute access to lazily resolve diagram specifications."""
    if name == "SYSTEM_DIAGRAM_SPECS":
        return _LAZY_SYSTEM_DIAGRAM_SPECS
    if name in CONSTANT_TO_KEY_MAP:
        return get_diagram_spec(CONSTANT_TO_KEY_MAP[name])
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def __dir__() -> List[str]:
    """Return list of module attribute names including lazy spec constants."""
    return (
        list(globals().keys())
        + ["SYSTEM_DIAGRAM_SPECS"]
        + list(CONSTANT_TO_KEY_MAP.keys())
    )
