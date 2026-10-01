"""Automated continuous integration accessibility and responsive layout runner.

Scans standalone component catalog entries across desktop and mobile viewports
for WCAG accessibility violations, missing ARIA attributes, rigid layout sizes,
and label overflow defects without requiring external Node.js dependencies.
"""

import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock


def parse_prop_str(prop_str: str) -> Dict[str, Any]:
    """Parse space-delimited prop string containing key="val", key='val', key=val or boolean flags."""
    props = {}
    pattern = r'([a-zA-Z0-9_\-]+)(?:=(?:"([^"]*)"|\'([^\']*)\'|(\S+)))?'
    for match in re.finditer(pattern, prop_str):
        key = match.group(1)
        val = (
            match.group(2)
            if match.group(2) is not None
            else (
                match.group(3)
                if match.group(3) is not None
                else (match.group(4) if match.group(4) is not None else True)
            )
        )
        props[key] = val
    return props


class MockSlot:
    """Mock container slot holding child elements."""

    def __init__(self):
        self.children = []


class MockElement:
    """Base mock NiceGUI element capturing attributes, properties, CSS classes, and child slots."""

    def __init__(self, tag: str, *args, **kwargs):
        self._tag = tag
        self._type_name = tag.capitalize() if isinstance(tag, str) and tag else "Element"
        self._props: Dict[str, Any] = {}
        self._classes: List[str] = []
        self._text: Optional[str] = None
        self.slots = {"default": MockSlot()}
        self._harness = kwargs.pop("_harness", None)

        if args:
            arg0 = args[0]
            if isinstance(arg0, str):
                if tag in ("button", "label", "switch", "checkbox", "markdown"):
                    self._text = arg0
                elif tag == "icon":
                    self._props["icon"] = arg0
                elif tag in ("input", "select"):
                    self._props["label"] = arg0
                elif tag == "image":
                    self._props["source"] = arg0
                else:
                    self._text = arg0

        for k, v in kwargs.items():
            if k == "value":
                if tag in ("switch", "checkbox"):
                    pass
                elif tag in ("input", "select", "linear_progress"):
                    self._props["value"] = v
            elif k in (
                "label",
                "placeholder",
                "icon",
                "alt",
                "src",
                "aria-label",
                "aria-labelledby",
                "aria-hidden",
            ):
                self._props[k] = v
            elif k == "options":
                self._props["options"] = v
            else:
                self._props[k] = v

    def classes(self, *class_names):
        """Append CSS classes to element."""
        for item in class_names:
            if isinstance(item, str):
                for c in item.split():
                    if c and c not in self._classes:
                        self._classes.append(c)
            elif isinstance(item, (list, tuple)):
                for c in item:
                    if isinstance(c, str):
                        for sub_c in c.split():
                            if sub_c and sub_c not in self._classes:
                                self._classes.append(sub_c)
        return self

    def props(self, *prop_args, **prop_kwargs):
        """Update property dictionary."""
        for arg in prop_args:
            if isinstance(arg, str):
                parsed = parse_prop_str(arg)
                self._props.update(parsed)
        for k, v in prop_kwargs.items():
            self._props[k] = str(v) if v is not None else ""
        return self

    def __enter__(self):
        """Enter context manager block pushing element to harness stack."""
        if self._harness:
            self._harness.push(self)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager block popping element from harness stack."""
        if self._harness:
            self._harness.pop(self)


_CLASS_CACHE: Dict[str, type] = {}


def get_mock_element_class(tag: str) -> type:
    """Dynamically get or create a MockElement subclass matching the NiceGUI component type name."""
    if tag not in _CLASS_CACHE:
        parts = tag.split("_")
        class_name = "_".join(p.capitalize() for p in parts)
        _CLASS_CACHE[tag] = type(class_name, (MockElement,), {})
    return _CLASS_CACHE[tag]


class MockUIHarness:
    """Mock NiceGUI ui builder harness intercepting element construction calls during scanning."""

    def __init__(self):
        self.stack: List[MockElement] = []

    def push(self, element: MockElement):
        """Push container element onto harness stack."""
        self.stack.append(element)

    def pop(self, element: Optional[MockElement] = None):
        """Pop container element from harness stack."""
        if self.stack:
            if element is None or self.stack[-1] is element:
                self.stack.pop()
            elif element in self.stack:
                while self.stack and self.stack[-1] is not element:
                    self.stack.pop()
                if self.stack:
                    self.stack.pop()

    @property
    def active_container(self) -> Optional[MockElement]:
        """Get current active container element at top of stack."""
        return self.stack[-1] if self.stack else None

    @contextmanager
    def active_context(self, root_element: MockElement):
        """Context manager establishing active container root element on stack."""
        prev_stack = list(self.stack)
        self.stack = [root_element]
        try:
            yield
        finally:
            self.stack = prev_stack

    def create_element(self, tag: str, *args, **kwargs) -> MockElement:
        """Instantiate and attach new mock element to current active container."""
        cls = get_mock_element_class(tag)
        elem = cls(tag, *args, _harness=self, **kwargs)
        parent = self.active_container
        if parent:
            parent.slots["default"].children.append(elem)
        return elem

    def element(self, tag: str, *args, **kwargs) -> MockElement:
        """Create mock element for given tag name."""
        return self.create_element(tag, *args, **kwargs)

    def __getattr__(self, name: str):
        """Dynamically construct element builder for unknown element tag names."""
        if name.startswith("_"):
            raise AttributeError(name)

        def builder(*args, **kwargs):
            return self.create_element(name, *args, **kwargs)

        return builder


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
    type_name = getattr(element, "_type_name", None)
    if isinstance(type_name, str) and type_name:
        return type_name
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
    """Recursively inspect a UI element and its children for accessibility rule failures."""
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

    # Rule A11Y005: Slider Range Attributes Verification
    if type_name in ("Slider", "slider"):
        role = props.get("role")
        vmin = props.get("aria-valuemin")
        vmax = props.get("aria-valuemax")
        vnow = props.get("aria-valuenow")
        vtext = props.get("aria-valuetext")

        missing_attrs = []
        if role != "slider":
            missing_attrs.append("role='slider'")
        if vmin is None or str(vmin).strip() == "":
            missing_attrs.append("aria-valuemin")
        if vmax is None or str(vmax).strip() == "":
            missing_attrs.append("aria-valuemax")
        if vnow is None or str(vnow).strip() == "":
            missing_attrs.append("aria-valuenow")
        if vtext is None or str(vtext).strip() == "":
            missing_attrs.append("aria-valuetext")

        if missing_attrs:
            violations.append(
                A11yViolation(
                    rule_id="A11Y005_SLIDER_RANGE_ATTRIBUTES",
                    component_id=component_id,
                    component_name=component_name,
                    viewport_name=viewport_name,
                    viewport_width=viewport_width,
                    locator=locator,
                    message=(
                        f"Slider component 'ui.slider' is missing required ARIA range attributes: "
                        f"{', '.join(missing_attrs)}."
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


class _MockElement:
    def __init__(self, type_name, text="", parent=None):
        self._type_name = type_name
        self._props = {}
        self._classes = []
        self._text = text
        self.children = []
        self.slots = {"default": self}

        if parent is not None and hasattr(parent, "children"):
            parent.children.append(self)

    @property
    def value(self):
        return self._props.get("value")

    @value.setter
    def value(self, val):
        self._props["value"] = val

    def props(self, *args, **kwargs):
        if args and isinstance(args[0], str):
            import re

            props_str = args[0]
            for match in re.finditer(
                r'([a-zA-Z0-9_-]+)(?:=["\']([^"\']*)["\']|=(\S+))?', props_str
            ):
                k = match.group(1)
                v = (
                    match.group(2)
                    if match.group(2) is not None
                    else (match.group(3) if match.group(3) is not None else True)
                )
                self._props[k] = v
        for k, v in kwargs.items():
            self._props[k] = v
        return self

    def classes(self, *args, **kwargs):
        if args and isinstance(args[0], str):
            self._classes.extend(args[0].split())
        return self

    def tooltip(self, text):
        self._props["tooltip"] = text
        return self

    def set_visibility(self, vis):
        self._props["visible"] = vis
        return self

    def bind_text_from(self, *args, **kwargs):
        return self

    def disable(self):
        self._props["disabled"] = True
        return self

    def enable(self):
        self._props["disabled"] = False
        return self

    def __enter__(self):
        _MockUI.stack.append(self)
        return self

    def __exit__(self, *args):
        if _MockUI.stack and _MockUI.stack[-1] is self:
            _MockUI.stack.pop()


class _MockUI:
    stack: List[_MockElement] = []

    @classmethod
    def current_parent(cls):
        return cls.stack[-1] if cls.stack else None

    @classmethod
    def card(cls, *args, **kwargs):
        return _MockElement("Card", parent=cls.current_parent())

    @classmethod
    def row(cls, *args, **kwargs):
        return _MockElement("Row", parent=cls.current_parent())

    @classmethod
    def column(cls, *args, **kwargs):
        return _MockElement("Column", parent=cls.current_parent())

    @classmethod
    def label(cls, text="", *args, **kwargs):
        return _MockElement("Label", text=text, parent=cls.current_parent())

    @classmethod
    def button(cls, text="", *args, **kwargs):
        elem = _MockElement("Button", text=text, parent=cls.current_parent())
        if "aria-label" in kwargs:
            elem._props["aria-label"] = kwargs["aria-label"]
        return elem

    @classmethod
    def icon(cls, name="", *args, **kwargs):
        return _MockElement("Icon", text=name, parent=cls.current_parent())

    @classmethod
    def switch(cls, text="", *args, **kwargs):
        elem = _MockElement("Switch", text=text, parent=cls.current_parent())
        if "value" in kwargs:
            elem._props["value"] = kwargs["value"]
        return elem

    @classmethod
    def slider(cls, min=0, max=100, value=0, step=1, **kwargs):
        elem = _MockElement("Slider", parent=cls.current_parent())
        elem._props["min"] = min
        elem._props["max"] = max
        elem._props["value"] = value
        elem._props["step"] = step
        return elem

    @classmethod
    def input(cls, label="", placeholder="", *args, **kwargs):
        elem = _MockElement("Input", text=label, parent=cls.current_parent())
        if placeholder:
            elem._props["placeholder"] = placeholder
        return elem

    @classmethod
    def select(cls, options=None, value=None, label="", **kwargs):
        return _MockElement("Select", text=label, parent=cls.current_parent())


def scan_catalog_component(
    component_entry: Dict[str, Any],
    viewport_name: str,
    viewport_width: int,
    state: str = "default",
) -> List[A11yViolation]:
    """Render a catalog component in an isolated slot context and scan for accessibility rule failures."""
    component_id = component_entry.get("id", "unknown_component")
    component_name = component_entry.get("name", component_id)
    render_func = component_entry.get("render_func")

    if not render_func or not callable(render_func):
        return []

    harness = MockUIHarness()
    root_container = harness.create_element("container")

    import app.ui.catalog as catalog_module

    ui_obj = getattr(catalog_module, "ui", None)
    has_proxy = not isinstance(ui_obj, MagicMock) and hasattr(ui_obj, "set_target")
    old_target = getattr(ui_obj, "_target", None) if has_proxy else None

    if has_proxy:
        ui_obj.set_target(harness)
    else:
        catalog_module.ui = harness

    nicegui_ui = None
    if "nicegui" in sys.modules and hasattr(sys.modules["nicegui"], "ui"):
        nicegui_ui = sys.modules["nicegui"].ui
        sys.modules["nicegui"].ui = harness

    try:
        with harness.active_context(root_container):
            try:
                render_func(root_container, state=state, viewport_width=viewport_width)
            except TypeError:
                try:
                    render_func(root_container, state=state)
                except TypeError:
                    render_func(root_container)
    finally:
        if has_proxy and ui_obj is not None:
            ui_obj.set_target(old_target)
        elif ui_obj is not None:
            catalog_module.ui = ui_obj
        if nicegui_ui is not None:
            sys.modules["nicegui"].ui = nicegui_ui

    violations: List[A11yViolation] = []
    for child in root_container.slots["default"].children:
        violations.extend(
            inspect_element_tree(
                child,
                ancestor_path=[],
                component_id=component_id,
                component_name=component_name,
                viewport_name=viewport_name,
                viewport_width=viewport_width,
            )
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


def inspect_tui_component(component: Any) -> List[A11yViolation]:
    """Inspect a TUI App or Modal component for WCAG 2.1 accessibility compliance.

    Leverages the component's internal audit hook method 'audit_a11y_compliance'
    to perform programmatic verification.
    """
    violations: List[A11yViolation] = []
    comp_name = type(component).__name__

    if hasattr(component, "audit_a11y_compliance") and callable(
        component.audit_a11y_compliance
    ):
        res = component.audit_a11y_compliance()
        for v in res.get("violations", []):
            violations.append(
                A11yViolation(
                    rule_id=v.get("rule", "A11Y_UNKNOWN"),
                    component_id=comp_name,
                    component_name=comp_name,
                    viewport_name="terminal",
                    viewport_width=80,
                    locator=v.get("widget_id", comp_name),
                    message=v.get("message", "A11y violation detected"),
                )
            )

    return violations
