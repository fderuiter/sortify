"""Declarative Pydantic schema models for application and component diagrams."""

from typing import Dict, List, Literal, Optional

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
                elif s == "stadium":
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


# System Default Diagram Specifications
ARCHITECTURE_DATAFLOW_SPEC = ComponentDiagramSpec(
    id="architecture_dataflow",
    title="Data Flow: Directory Selection to Sorting Plan",
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

CORE_ARCHITECTURE_SPEC = ComponentDiagramSpec(
    id="core_architecture",
    title="Core Architecture Module Flow",
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

SYSTEM_DIAGRAM_SPECS: Dict[str, ComponentDiagramSpec] = {
    "architecture_dataflow": ARCHITECTURE_DATAFLOW_SPEC,
    "catalog_workflow": CATALOG_WORKFLOW_SPEC,
    "ui_component_hierarchy": UI_COMPONENT_HIERARCHY_SPEC,
    "core_architecture": CORE_ARCHITECTURE_SPEC,
}
