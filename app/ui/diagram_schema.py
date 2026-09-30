"""Declarative Pydantic schema models for application, sequence, and state diagrams."""

from typing import Dict, List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator

ALLOWED_URL_SCHEMES = {"http", "https", "mailto", "file"}


class DiagramNode(BaseModel):
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


class DiagramEdge(BaseModel):
    """Specification model for a directional edge link between diagram nodes."""

    source: str
    target: str
    label: Optional[str] = None
    arrow_type: str = "-->"


class DiagramSubgraph(BaseModel):
    """Specification model for a subgraph boundary container in a diagram."""

    id: str
    title: str
    nodes: List[str] = Field(default_factory=list)


class ComponentDiagramSpec(BaseModel):
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
class SequenceParticipant(BaseModel):
    """Participant or actor node in a sequence diagram."""

    id: str
    label: Optional[str] = None
    is_actor: bool = False


class SequenceMessage(BaseModel):
    """Directional message between participants in a sequence diagram."""

    kind: Literal["message"] = "message"
    source: str
    target: str
    text: str
    arrow_type: str = "->>"


class SequenceActivation(BaseModel):
    """Activation or deactivation of a participant lifeline."""

    kind: Literal["activation"] = "activation"
    target: str
    action: Literal["activate", "deactivate"]


class SequenceNote(BaseModel):
    """Note block in a sequence diagram."""

    kind: Literal["note"] = "note"
    position: str = "over"  # "over", "left of", "right of"
    targets: List[str] = Field(default_factory=list)
    text: str


class SequenceLoop(BaseModel):
    """Loop block in a sequence diagram."""

    kind: Literal["loop"] = "loop"
    label: str
    items: List["SequenceItem"] = Field(default_factory=list)


class SequenceOpt(BaseModel):
    """Optional block in a sequence diagram."""

    kind: Literal["opt"] = "opt"
    label: str
    items: List["SequenceItem"] = Field(default_factory=list)


class SequenceAltBranch(BaseModel):
    """Branch section in an alt/else block."""

    label: str
    items: List["SequenceItem"] = Field(default_factory=list)


class SequenceAlt(BaseModel):
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

SequenceLoop.model_rebuild()
SequenceOpt.model_rebuild()
SequenceAltBranch.model_rebuild()


class SequenceDiagramSpec(BaseModel):
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
class StateNode(BaseModel):
    """Specification model for a state in a state diagram."""

    id: str
    label: Optional[str] = None
    is_choice: bool = False


class StateTransition(BaseModel):
    """Specification model for a state transition in a state diagram."""

    source: str
    target: str
    label: Optional[str] = None


class StateComposite(BaseModel):
    """Specification model for a composite state container."""

    id: str
    label: Optional[str] = None
    states: List[StateNode] = Field(default_factory=list)
    transitions: List[StateTransition] = Field(default_factory=list)


class StateNote(BaseModel):
    """Specification model for a note in a state diagram."""

    position: str = "left of"  # "left of", "right of"
    target: str
    text: str


class StateDiagramSpec(BaseModel):
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


BaseDiagramSpec = Union[ComponentDiagramSpec, SequenceDiagramSpec, StateDiagramSpec]


# System Default Diagram Specifications
CORE_ARCHITECTURE_SPEC = ComponentDiagramSpec(
    id="core_architecture",
    title="Smart AutoSorter AI Pro Full System Architecture",
    diagram_type="flowchart",
    direction="TD",
    subgraphs=[
        DiagramSubgraph(
            id="ui_layer",
            title="Presentation & User Interfaces (app.ui)",
            nodes=[
                "app_main",
                "ui_app",
                "ui_catalog",
                "ui_wizard",
                "ui_dialog_helper",
                "ui_a11y_runner",
                "ui_diagram_schema",
            ],
        ),
        DiagramSubgraph(
            id="session_orchestration",
            title="Session Orchestration & Lifecycle (app.core)",
            nodes=[
                "core_session",
                "core_user_space_bootstrap",
                "core_daemon",
                "core_integration",
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
            id="clinical_pipeline",
            title="Clinical Trial & Multi-Study Pipeline (app.core)",
            nodes=[
                "core_cro_multi_study_pipeline",
                "core_study_disambiguator",
                "core_clinical_taxonomy",
                "core_clinical_compliance",
                "core_clinical_renamer",
                "core_clinical_strategy",
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
                "core_quarantine_interceptor",
                "core_domain_contracts",
                "core_verifier",
                "core_domain_contracts",
                "core_mover",
                "core_file_renamer",
                "core_resilient_file_ops",
                "core_scanner",
                "core_progress",
                "core_metadata",
                "core_domain_contracts",
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
                "core_domain_contracts",
                "core_exceptions",
                "core_domain_contracts",
            ],
        ),
    ],
    nodes=[
        # Presentation
        DiagramNode(id="app_main", label="app.main (CLI Entry Point)", shape="round"),
        DiagramNode(id="ui_app", label="app.ui.app (AutoSorterApp)"),
        DiagramNode(id="ui_catalog", label="app.ui.catalog (Component Catalog)"),
        DiagramNode(id="ui_wizard", label="app.ui.wizard (Setup Wizard)"),
        DiagramNode(id="ui_dialog_helper", label="app.ui.dialog_helper"),
        DiagramNode(id="ui_a11y_runner", label="app.ui.a11y_runner"),
        DiagramNode(id="ui_diagram_schema", label="app.ui.diagram_schema"),
        # Orchestration
        DiagramNode(id="core_session", label="app.core.session"),
        DiagramNode(id="core_user_space_bootstrap", label="app.core.user_space_bootstrap"),
        DiagramNode(id="core_daemon", label="app.core.daemon"),
        DiagramNode(id="core_integration", label="app.core.integration"),
        # Ingestion & Extraction
        DiagramNode(id="core_extractor", label="app.core.extractor"),
        DiagramNode(id="core_extractor_strategies", label="app.core.extractor_strategies"),
        DiagramNode(id="core_forensic_scanner", label="app.core.forensic_scanner"),
        DiagramNode(id="core_offline_loader", label="app.core.offline_loader"),
        DiagramNode(id="core_downloader", label="app.core.downloader"),
        # Analytics & ML
        DiagramNode(id="core_analyzer", label="app.core.analyzer"),
        DiagramNode(id="core_analyzer_strategies", label="app.core.analyzer_strategies"),
        DiagramNode(id="core_jev_classifier", label="app.core.jev_classifier"),
        DiagramNode(id="core_semantic_embeddings", label="app.core.semantic_embeddings"),
        # Clinical
        DiagramNode(id="core_cro_multi_study_pipeline", label="app.core.cro_multi_study_pipeline"),
        DiagramNode(id="core_study_disambiguator", label="app.core.study_disambiguator"),
        DiagramNode(id="core_clinical_taxonomy", label="app.core.clinical_taxonomy"),
        DiagramNode(id="core_clinical_compliance", label="app.core.clinical_compliance"),
        DiagramNode(id="core_clinical_renamer", label="app.core.clinical_renamer"),
        DiagramNode(id="core_clinical_strategy", label="app.core.clinical_strategy"),
        # Memory & Cache
        DiagramNode(id="core_cache", label="app.core.cache (BoundedMemoryCache)", shape="database"),
        DiagramNode(id="core_db_conn", label="app.core.db_conn"),
        DiagramNode(id="core_link_manager", label="app.core.link_manager"),
        DiagramNode(id="core_hashes_registry", label="app.core.hashes_registry"),
        # Concurrency & Shared
        DiagramNode(id="core_shared_registry", label="app.core.shared_registry (SharedWorkerPool)"),
        DiagramNode(id="core_db_worker", label="app.core.db_worker"),
        # Storage
        DiagramNode(id="core_db", label="app.core.db", shape="database"),
        DiagramNode(id="core_ledger", label="app.core.ledger", shape="database"),
        DiagramNode(id="core_history", label="app.core.history", shape="database"),
        # Policy & Execution
        DiagramNode(id="core_policy_engine", label="app.core.policy_engine", shape="rhombus"),
        DiagramNode(id="core_quarantine_interceptor", label="app.core.quarantine_interceptor"),
        DiagramNode(id="core_domain_contracts", label="app.core.domain_contracts"),
        DiagramNode(id="core_verifier", label="app.core.verifier", shape="rhombus"),
        DiagramNode(id="core_domain_contracts", label="app.core.domain_contracts"),
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
        DiagramNode(id="core_domain_contracts", label="app.core.domain_contracts"),
        DiagramNode(id="core_exceptions", label="app.core.exceptions"),
        DiagramNode(id="core_domain_contracts", label="app.core.domain_contracts"),
    ],
    edges=[
        DiagramEdge(source="app_main", target="ui_app"),
        DiagramEdge(source="app_main", target="core_session"),
        DiagramEdge(source="ui_app", target="core_session"),
        DiagramEdge(source="ui_app", target="ui_wizard"),
        DiagramEdge(source="ui_app", target="ui_dialog_helper"),
        DiagramEdge(source="ui_catalog", target="ui_diagram_schema"),
        DiagramEdge(source="ui_a11y_runner", target="ui_catalog"),
        DiagramEdge(source="core_session", target="core_user_space_bootstrap"),
        DiagramEdge(source="core_session", target="core_scanner"),
        DiagramEdge(source="core_session", target="core_extractor"),
        DiagramEdge(source="core_session", target="core_analyzer"),
        DiagramEdge(source="core_session", target="core_policy_engine"),
        DiagramEdge(source="core_session", target="core_verifier"),
        DiagramEdge(source="core_session", target="core_mover"),
        DiagramEdge(source="core_daemon", target="core_session"),
        DiagramEdge(source="core_integration", target="core_session"),
        DiagramEdge(source="core_extractor", target="core_extractor_strategies"),
        DiagramEdge(source="core_extractor", target="core_forensic_scanner"),
        DiagramEdge(source="core_extractor", target="core_offline_loader"),
        DiagramEdge(source="core_downloader", target="core_env_helper"),
        DiagramEdge(source="core_analyzer", target="core_analyzer_strategies"),
        DiagramEdge(source="core_analyzer", target="core_jev_classifier"),
        DiagramEdge(source="core_analyzer", target="core_semantic_embeddings"),
        DiagramEdge(source="core_cro_multi_study_pipeline", target="core_study_disambiguator"),
        DiagramEdge(source="core_cro_multi_study_pipeline", target="core_clinical_taxonomy"),
        DiagramEdge(source="core_cro_multi_study_pipeline", target="core_clinical_compliance"),
        DiagramEdge(source="core_cro_multi_study_pipeline", target="core_clinical_renamer"),
        DiagramEdge(source="core_clinical_renamer", target="core_clinical_strategy"),
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
        DiagramEdge(source="core_policy_engine", target="core_quarantine_interceptor"),
        DiagramEdge(source="core_quarantine_interceptor", target="core_domain_contracts"),
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

ARCHITECTURE_DATAFLOW_SPEC = ComponentDiagramSpec(
    id="architecture_dataflow",
    title="Data Flow: Directory Selection to Reorganization Plan",
    diagram_type="graph",
    direction="TD",
    nodes=[
        DiagramNode(
            id="A",
            label="Directory Selection",
            shape="round",
            url="docs/user_guide.md#first-run-steps--setup-wizard",
            tooltip="Directory Selection Step",
            step_number=1,
            step_description="Select target directory to organize",
        ),
        DiagramNode(
            id="B",
            label="File Extraction & Generator",
            shape="rectangle",
            url="docs/user_guide.md#supported-file-formats",
            tooltip="File Extraction Step",
            step_number=2,
            step_description="Extract files and prepare generator",
        ),
        DiagramNode(
            id="C",
            label="Chunked Yielding",
            shape="rectangle",
            tooltip="Chunked Yielding Step",
            step_number=3,
            step_description="Yield file chunks incrementally",
        ),
        DiagramNode(
            id="D",
            label="Incremental Analyzer (partial_fit)",
            shape="rectangle",
            tooltip="Analyzer Step",
            step_number=4,
            step_description="Run incremental analyzer",
        ),
        DiagramNode(
            id="E",
            label="TF-IDF & NMF Clustering",
            shape="rectangle",
            url="docs/user_guide.md#ai-clustering-constraints",
            tooltip="Clustering Step",
            step_number=5,
            step_description="Cluster file features with TF-IDF & NMF",
        ),
        DiagramNode(
            id="F",
            label="Recursive Topic Grouping",
            shape="rectangle",
            tooltip="Topic Grouping Step",
            step_number=6,
            step_description="Recursively group file topics",
        ),
        DiagramNode(
            id="G",
            label="Generate Sorting Plan",
            shape="rectangle",
            tooltip="Sorting Plan Step",
            step_number=7,
            step_description="Generate final file sorting plan",
        ),
        DiagramNode(
            id="H",
            label="UI Tree Rendering",
            shape="round",
            url="docs/ui.md#appuiplan_treeview",
            tooltip="UI Rendering Step",
            step_number=8,
            step_description="Render sorting plan in tree view",
        ),
    ],
    edges=[
        DiagramEdge(source="A", target="B"),
        DiagramEdge(source="B", target="C"),
        DiagramEdge(source="C", target="D"),
        DiagramEdge(source="D", target="E"),
        DiagramEdge(source="E", target="F"),
        DiagramEdge(source="F", target="G"),
        DiagramEdge(source="G", target="H"),
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
        DiagramNode(id="forensic_scan", label="app.core.forensic_scanner", shape="subroutine"),
        DiagramNode(id="study_disambiguation", label="app.core.study_disambiguator", shape="rhombus"),
        DiagramNode(id="clinical_taxonomy", label="app.core.clinical_taxonomy"),
        DiagramNode(id="clinical_compliance", label="app.core.clinical_compliance", shape="rhombus"),
        DiagramNode(id="clinical_renamer", label="app.core.clinical_renamer"),
        DiagramNode(id="cro_pipeline_output", label="TMF Clean Binders Output", shape="stadium"),
    ],
    edges=[
        DiagramEdge(source="forensic_scan", target="study_disambiguation", label="Raw Drives"),
        DiagramEdge(source="study_disambiguation", target="clinical_taxonomy", label="Protocols Disambiguated"),
        DiagramEdge(source="clinical_taxonomy", target="clinical_compliance", label="Mapped Taxonomy"),
        DiagramEdge(source="clinical_compliance", target="clinical_renamer", label="Compliance Validated"),
        DiagramEdge(source="clinical_renamer", target="cro_pipeline_output", label="Compiled Binders"),
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
        DiagramNode(id="bounded_cache", label="BoundedMemoryCache[K, V]", shape="database"),
        DiagramNode(id="db_conn_cache", label="app.core.db_conn (_connection_cache)"),
        DiagramNode(id="link_manager_cache", label="app.core.link_manager (_registry)"),
        DiagramNode(id="semantic_cache", label="app.core.semantic_embeddings (_model_properties_cache)"),
        DiagramNode(id="jev_cache", label="app.core.jev_classifier (memory_cache)"),
        DiagramNode(id="db_cache", label="app.core.db (doc_cache)"),
    ],
    edges=[
        DiagramEdge(source="db_conn_cache", target="bounded_cache", label="Max Size: 50"),
        DiagramEdge(source="link_manager_cache", target="bounded_cache", label="Max Size: 10000"),
        DiagramEdge(source="semantic_cache", target="bounded_cache", label="Max Size: 500"),
        DiagramEdge(source="jev_cache", target="bounded_cache", label="Fast Path Cache"),
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
        DiagramNode(id="shared_worker_pool", label="SharedWorkerPool Singleton", shape="stadium"),
        DiagramNode(id="session_tasks", label="app.core.session (Lifecycle Management)"),
        DiagramNode(id="mover_tasks", label="app.core.mover (Parallel File Relocation)"),
        DiagramNode(id="db_worker_tasks", label="app.core.db_worker (Background Heavy Tasks)"),
        DiagramNode(id="extractor_tasks", label="app.core.extractor (Parallel Text Ingestion)"),
        DiagramNode(id="analyzer_tasks", label="app.core.analyzer_strategies (Parallel NLP)"),
    ],
    edges=[
        DiagramEdge(source="session_tasks", target="shared_worker_pool", label="get_instance() / shutdown()"),
        DiagramEdge(source="mover_tasks", target="shared_worker_pool", label="submit() relocation tasks"),
        DiagramEdge(source="db_worker_tasks", target="shared_worker_pool", label="offload VLM/OCR/GGUF"),
        DiagramEdge(source="extractor_tasks", target="shared_worker_pool", label="parallel document extraction"),
        DiagramEdge(source="analyzer_tasks", target="shared_worker_pool", label="offload embedding vector calculations"),
    ],
)

POLICY_EVALUATION_FLOW_SPEC = ComponentDiagramSpec(
    id="policy_evaluation_flow",
    title="Administrator Policy Evaluation & Compliance Flowchart",
    diagram_type="flowchart",
    direction="TD",
    nodes=[
        DiagramNode(id="A", label="Incoming Document", shape="round"),
        DiagramNode(id="B", label="Sort Rules by Priority High to Low", shape="rectangle"),
        DiagramNode(id="C", label="Evaluate Next Rule", shape="rhombus"),
        DiagramNode(id="D", label="Route Document via Override Path", shape="rectangle"),
        DiagramNode(id="E", label="Route Document via Keyword Category", shape="rectangle"),
        DiagramNode(id="F", label="Route Document via Pattern Category", shape="rectangle"),
        DiagramNode(id="G", label="Stop Processing & Halt Evaluation", shape="rectangle"),
        DiagramNode(id="H", label="More Rules Remaining?", shape="rhombus"),
        DiagramNode(id="I", label="Proceed to General Classification / AI Sorting", shape="rectangle"),
    ],
    edges=[
        DiagramEdge(source="A", target="B"),
        DiagramEdge(source="B", target="C"),
        DiagramEdge(source="C", target="D", label="Override Rule Match"),
        DiagramEdge(source="C", target="E", label="Keyword Rule Match"),
        DiagramEdge(source="C", target="F", label="Pattern Rule Match"),
        DiagramEdge(source="C", target="G", label="No Match & Halt on Mismatch Enabled"),
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
        DiagramNode(id="C", label="Check Firewall & Disconnected Status", shape="rectangle"),
        DiagramNode(id="D", label="Fallback to Offline Non-Semantic Mode", shape="rectangle"),
        DiagramNode(id="E", label="Sufficient Disk Space >= 200MB?", shape="rhombus"),
        DiagramNode(id="F", label="Clear Free Disk Space", shape="rectangle"),
        DiagramNode(id="G", label="Retry Model Download via Settings", shape="rectangle"),
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

ADMIN_POLICY_EVALUATION_SPEC = ComponentDiagramSpec(
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

SYSTEM_DIAGRAM_SPECS: Dict[str, BaseDiagramSpec] = {
    "core_architecture": CORE_ARCHITECTURE_SPEC,
    "architecture_dataflow": ARCHITECTURE_DATAFLOW_SPEC,
    "architecture_async_processing": ARCHITECTURE_ASYNC_PROCESSING_SPEC,
    "architecture_watchdog_state": ARCHITECTURE_WATCHDOG_STATE_SPEC,
    "cro_multi_study_pipeline": CRO_MULTI_STUDY_PIPELINE_SPEC,
    "memory_cache_layers": MEMORY_CACHE_LAYERS_SPEC,
    "worker_pool_concurrency": WORKER_POOL_CONCURRENCY_SPEC,
    "policy_evaluation_flow": POLICY_EVALUATION_FLOW_SPEC,
    "setup_wizard_flow": SETUP_WIZARD_FLOW_SPEC,
    "catalog_workflow": CATALOG_WORKFLOW_SPEC,
    "ui_component_hierarchy": UI_COMPONENT_HIERARCHY_SPEC,
    "core_text_extraction": CORE_TEXT_EXTRACTION_SPEC,
    "contributor_onboarding": CONTRIBUTOR_ONBOARDING_SPEC,
    "troubleshooting_setup_wizard": TROUBLESHOOTING_SETUP_WIZARD_SPEC,
    "ml_analyzer_clustering": ML_ANALYZER_CLUSTERING_SPEC,
    "multi_format_text_extraction_seq": MULTI_FORMAT_TEXT_EXTRACTION_SEQ_SPEC,
    "virtual_sorting_verification_seq": VIRTUAL_SORTING_VERIFICATION_SEQ_SPEC,
    "user_guide_setup_wizard": USER_GUIDE_SETUP_WIZARD_SPEC,
    "user_guide_watchdog_state": USER_GUIDE_WATCHDOG_STATE_SPEC,
    "api_core_architecture": API_CORE_ARCHITECTURE_SPEC,
    "admin_policy_evaluation": ADMIN_POLICY_EVALUATION_SPEC,
}
