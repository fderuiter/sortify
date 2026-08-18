"""Automated continuous integration accessibility and responsive layout runner.

Scans standalone component catalog entries across desktop and mobile viewports
for WCAG accessibility violations, missing ARIA attributes, rigid layout sizes,
and label overflow defects without requiring external Node.js dependencies.
"""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class A11yViolation:
    """Represents a single accessibility or responsive layout violation."""

    rule_id: str
    component_id: str
    component_name: str
    viewport_name: str
    viewport_width: int
    locator: str
    message: str


# Configured responsive viewports for automated continuous integration scanning
DEFAULT_CONFIGURED_VIEWPORTS: List[Tuple[str, int]] = [
    ("desktop", 1280),
    ("tablet", 768),
    ("mobile", 375),
    ("narrow_mobile", 320),
]


def is_rigid_width_class(cls_name: str, viewport_width: int) -> bool:
    """Determine if a CSS class specifies a rigid fixed width that causes overflow on narrow viewports."""
    if viewport_width > 600:
        return False

    # Exempt fluid and boundary classes
    if (
        cls_name.startswith("min-w-")
        or cls_name.startswith("max-w-")
        or cls_name.startswith("min-h-")
        or cls_name.startswith("max-h-")
    ):
        return False

    if cls_name in ("w-full", "w-auto", "w-screen") or "/" in cls_name:
        return False

    # Arbitrary pixel bracket sizes (e.g. w-[800px], w-[500px])
    if cls_name.startswith("w-[") and "px]" in cls_name:
        try:
            val_str = cls_name.split("w-[")[1].split("px]")[0]
            val = int(val_str)
            if val > viewport_width:
                return True
        except ValueError:
            return True

    # Tailwind width classes (e.g. w-96 = 384px, w-80 = 320px)
    fixed_pixel_widths = {
        "w-64": 256,
        "w-72": 288,
        "w-80": 320,
        "w-96": 384,
    }
    if cls_name in fixed_pixel_widths:
        if fixed_pixel_widths[cls_name] >= viewport_width:
            return True

    return False


def get_element_type_name(element: Any) -> str:
    """Extract readable type name of a NiceGUI element."""
    return type(element).__name__


def build_element_locator(element: Any, ancestor_path: List[str]) -> str:
    """Construct a deterministic element locator selector string."""
    type_name = get_element_type_name(element)
    props = getattr(element, "_props", {})
    text_raw = getattr(element, "_text", "")
    text = str(text_raw).strip() if text_raw is not None else ""

    identifiers = []
    if "icon" in props and props["icon"]:
        identifiers.append(f"icon='{props['icon']}'")
    if "aria-label" in props and props["aria-label"]:
        identifiers.append(f"aria-label='{props['aria-label']}'")
    elif "label" in props and props["label"]:
        identifiers.append(f"label='{props['label']}'")
    elif text:
        truncated = text[:20] + "..." if len(text) > 20 else text
        identifiers.append(f"text='{truncated}'")

    attr_str = f"[{', '.join(identifiers)}]" if identifiers else ""
    current_selector = f"ui.{type_name.lower()}{attr_str}"
    full_path = ancestor_path + [current_selector]
    return " > ".join(full_path)


def inspect_element_tree(
    element: Any,
    ancestor_path: List[str],
    component_id: str,
    component_name: str,
    viewport_name: str,
    viewport_width: int,
) -> List[A11yViolation]:
    """Recursively inspect a NiceGUI element and its children for accessibility rule failures."""
    violations: List[A11yViolation] = []
    type_name = get_element_type_name(element)
    props = getattr(element, "_props", {})
    classes = getattr(element, "_classes", [])
    text = str(getattr(element, "_text", "") or "").strip()

    locator = build_element_locator(element, ancestor_path)

    # Rule A11Y001: Missing Label / ARIA Name on Interactive Controls
    interactive_types = {"Button", "Input", "Select", "Switch", "Checkbox", "Slider"}
    if type_name in interactive_types:
        has_text = bool(text)
        has_aria_label = bool(props.get("aria-label"))
        has_aria_labelledby = bool(props.get("aria-labelledby"))
        has_props_label = bool(props.get("label"))
        has_placeholder = bool(props.get("placeholder"))

        if not (
            has_text
            or has_aria_label
            or has_aria_labelledby
            or has_props_label
            or has_placeholder
        ):
            violations.append(
                A11yViolation(
                    rule_id="A11Y001_MISSING_LABEL",
                    component_id=component_id,
                    component_name=component_name,
                    viewport_name=viewport_name,
                    viewport_width=viewport_width,
                    locator=locator,
                    message=(
                        f"Interactive element 'ui.{type_name.lower()}' lacks an explicit "
                        f"text label, 'aria-label', or associated input label."
                    ),
                )
            )

    # Rule A11Y002: Missing Alt or ARIA label on Standalone Images/Icons
    if type_name == "Image":
        has_alt = bool(props.get("alt"))
        has_aria_label = bool(props.get("aria-label"))
        is_hidden = props.get("aria-hidden") == "true"
        if not (has_alt or has_aria_label or is_hidden):
            violations.append(
                A11yViolation(
                    rule_id="A11Y002_MISSING_ALT",
                    component_id=component_id,
                    component_name=component_name,
                    viewport_name=viewport_name,
                    viewport_width=viewport_width,
                    locator=locator,
                    message=(
                        "Image component is missing an 'alt' attribute or 'aria-label'."
                    ),
                )
            )

    # Rule A11Y003: Rigid Width Layout Overflow on Narrow Viewports
    for cls in classes:
        if is_rigid_width_class(cls, viewport_width):
            # Check if there is max-w-full override
            if "max-w-full" not in classes and "w-full" not in classes:
                violations.append(
                    A11yViolation(
                        rule_id="A11Y003_RIGID_LAYOUT",
                        component_id=component_id,
                        component_name=component_name,
                        viewport_name=viewport_name,
                        viewport_width=viewport_width,
                        locator=locator,
                        message=(
                            f"Rigid width class '{cls}' used on narrow viewport ({viewport_width}px) "
                            f"without fluid or max-width container bounds, causing layout clipping."
                        ),
                    )
                )

    # Rule A11Y004: Label Overflow Handling on Narrow Viewports
    # Check if a long text string on a narrow viewport lacks flex-wrap or text truncation/break bounds
    if viewport_width <= 375 and len(text) > 40:
        has_wrap = any(
            c in classes
            for c in (
                "flex-wrap",
                "truncate",
                "break-words",
                "break-all",
                "overflow-hidden",
                "whitespace-normal",
                "text-wrap",
            )
        )
        # Check if current classes include flex-wrap/break-words/truncate
        if not has_wrap:
            violations.append(
                A11yViolation(
                    rule_id="A11Y004_LABEL_OVERFLOW",
                    component_id=component_id,
                    component_name=component_name,
                    viewport_name=viewport_name,
                    viewport_width=viewport_width,
                    locator=locator,
                    message=(
                        f"Long text content ({len(text)} chars) on narrow viewport ({viewport_width}px) "
                        f"lacks explicit overflow wrapping classes ('flex-wrap', 'break-words', 'truncate')."
                    ),
                )
            )

    # Recurse through children slots / elements
    current_path = ancestor_path + [f"ui.{type_name.lower()}"]
    slots = getattr(element, "slots", {})
    if isinstance(slots, dict):
        for slot in slots.values():
            children = getattr(slot, "children", [])
            for child in children:
                violations.extend(
                    inspect_element_tree(
                        child,
                        current_path,
                        component_id,
                        component_name,
                        viewport_name,
                        viewport_width,
                    )
                )

    return violations


def scan_catalog_component(
    component_entry: Dict[str, Any],
    viewport_name: str,
    viewport_width: int,
    state: str = "default",
) -> List[A11yViolation]:
    """Render a catalog component in a isolated slot context and scan for accessibility rule failures."""
    from nicegui.element import Element

    component_id = component_entry["id"]
    component_name = component_entry["name"]
    render_func = component_entry["render_func"]

    # Create an isolated container element for rendering
    container = Element("div").classes("w-full h-full p-2")
    with container:
        try:
            render_func(container, state=state, viewport_width=viewport_width)
        except Exception as err:
            return [
                A11yViolation(
                    rule_id="A11Y999_RENDER_ERROR",
                    component_id=component_id,
                    component_name=component_name,
                    viewport_name=viewport_name,
                    viewport_width=viewport_width,
                    locator=f"ComponentCatalog > {component_id}",
                    message=f"Component rendering raised exception: {err}",
                )
            ]

    # Inspect element tree of the container
    ancestor = [f"ComponentCatalog[{component_id}]"]
    violations = inspect_element_tree(
        container,
        ancestor,
        component_id,
        component_name,
        viewport_name,
        viewport_width,
    )

    return violations


def run_all_catalog_scans(
    catalog_registry: List[Dict[str, Any]],
    viewports: Optional[List[Tuple[str, int]]] = None,
) -> Tuple[int, List[A11yViolation]]:
    """Scan all components in the catalog registry across configured responsive viewports.

    Returns (total_scans_conducted, violations_list).
    """
    if viewports is None:
        viewports = DEFAULT_CONFIGURED_VIEWPORTS

    total_scans = 0
    all_violations: List[A11yViolation] = []

    for comp in catalog_registry:
        sample_states = comp.get("sample_states", ["default"])
        for v_name, v_width in viewports:
            for st in sample_states:
                total_scans += 1
                violations = scan_catalog_component(comp, v_name, v_width, state=st)
                all_violations.extend(violations)

    return total_scans, all_violations
