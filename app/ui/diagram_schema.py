"""Declarative Pydantic schema models for application and component diagrams."""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class DiagramNode(BaseModel):
    """Specification model for a single diagram node vertex."""

    id: str
    label: str
    shape: Optional[str] = "rectangle"
    style: Optional[str] = None


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
        DiagramNode(id="A", label="Directory Selection", shape="round"),
        DiagramNode(id="B", label="File Extraction & Generator", shape="rectangle"),
        DiagramNode(id="C", label="Chunked Yielding", shape="rectangle"),
        DiagramNode(
            id="D", label="Incremental Analyzer (partial_fit)", shape="rectangle"
        ),
        DiagramNode(id="E", label="TF-IDF & NMF Clustering", shape="rectangle"),
        DiagramNode(id="F", label="Recursive Topic Grouping", shape="rectangle"),
        DiagramNode(id="G", label="Generate Sorting Plan", shape="rectangle"),
        DiagramNode(id="H", label="UI Tree Rendering", shape="round"),
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
        DiagramNode(id="workbench", label="Catalog Workbench", shape="round"),
        DiagramNode(id="selector", label="Component Selector", shape="rectangle"),
        DiagramNode(id="viewport", label="Viewport Controller", shape="rectangle"),
        DiagramNode(id="state_var", label="State Variant Switcher", shape="rectangle"),
        DiagramNode(id="renderer", label="Component Renderer", shape="subroutine"),
        DiagramNode(id="a11y_scan", label="Accessibility Auditor", shape="rhombus"),
        DiagramNode(id="preview", label="Interactive Viewport Frame", shape="round"),
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
        DiagramNode(id="header_bar", label="Application Header Bar"),
        DiagramNode(id="toolbar", label="Top Action Toolbar"),
        DiagramNode(id="directory_selection", label="Directory Selection Card"),
        DiagramNode(id="settings_modal", label="Settings Dialog View"),
        DiagramNode(id="setup_wizard", label="AI Model Setup Wizard"),
        DiagramNode(id="plan_treeview", label="Proposed Reorganization Plan"),
        DiagramNode(id="cro_forensic_dialog", label="CRO Forensic View"),
        DiagramNode(id="status_progress_panel", label="Status & Progress Panel"),
    ],
    edges=[
        DiagramEdge(source="header_bar", target="directory_selection"),
        DiagramEdge(source="directory_selection", target="plan_treeview"),
        DiagramEdge(source="plan_treeview", target="status_progress_panel"),
        DiagramEdge(source="settings_modal", target="setup_wizard"),
        DiagramEdge(source="cro_forensic_dialog", target="status_progress_panel"),
    ],
)

SYSTEM_DIAGRAM_SPECS: Dict[str, ComponentDiagramSpec] = {
    "architecture_dataflow": ARCHITECTURE_DATAFLOW_SPEC,
    "catalog_workflow": CATALOG_WORKFLOW_SPEC,
    "ui_component_hierarchy": UI_COMPONENT_HIERARCHY_SPEC,
}
