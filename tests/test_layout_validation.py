import pytest
from nicegui import Client, context, ui
from nicegui.elements.card import Card
from nicegui.elements.dialog import Dialog

from app.ui.app import AutoSorterApp
from app.ui.dialog_helper import get_dialog_card_classes
from app.ui.settings import show_settings
from app.ui.wizard import show_wizard


class MockSettings:
    def __init__(self):
        self.CONTEXTUAL_RENAMING = False
        self.PRESERVE_HIERARCHY = False
        self.EXPLORER_INTEGRATION = False
        self.CLEANUP_EMPTY_FOLDERS = False
        self.PROTECTED_PATHS = []
        self.MAX_DEPTH = 10
        self.MAX_FOLDERS = 100
        self.MAX_WORKERS = 4
        self.VISUAL_TIMEOUT = 10
        self.PROXY = ""
        self.KEYWORD_RULES = {}
        self.POLICIES = []
        self.MODEL_THREADS = 4
        self.IMAGE_MAX_DIMENSION = 1024
        self.IMAGE_SKIP_THRESHOLD = 500
        self.AI_CONSENT_GRANTED = False
        self.STOP_WORDS = {"the", "and", "or"}
        self.DEBOUNCE_DELAY = 0.6
        self.MAX_DEBOUNCE_DELAY = 5.0
        self.IGNORED_EXTENSIONS = [".crdownload", ".tmp", ".download"]


def is_rigid_layout_class(cls_name: str) -> bool:
    """Determine if a CSS/Tailwind class is a rigid absolute/hardcoded height or width.

    Allows fluid/percentage dimensions (e.g., w-full, w-1/2, w-3/4) and
    boundary limits (e.g., min-w-..., max-w-..., min-h-..., max-h-...).
    """
    # Exclude min/max limit and flex boundaries
    if (
        cls_name.startswith("min-w-")
        or cls_name.startswith("max-w-")
        or cls_name.startswith("min-h-")
        or cls_name.startswith("max-h-")
    ):
        return False

    # Check for hardcoded arbitrary value brackets (e.g. w-[500px], h-[200px])
    if cls_name.startswith("w-[") or cls_name.startswith("h-["):
        return True

    # Check for width classes (e.g. w-96, w-48) but allow fluid fraction/percentage and keywords
    if cls_name.startswith("w-"):
        # Allow w-full, w-auto, w-screen
        if cls_name in ("w-full", "w-auto", "w-screen"):
            return False
        # Allow fluid fraction sizes like w-1/2, w-3/4, w-11/12
        if "/" in cls_name:
            return False
        return True

    # Check for height classes (e.g. h-96, h-48) but allow fluid keywords/fractions
    if cls_name.startswith("h-"):
        if cls_name in ("h-full", "h-auto", "h-screen"):
            return False
        if "/" in cls_name:
            return False
        return True

    return False


def inspect_runtime_elements():
    """Inspect all instantiated elements in context.client.elements and return any rigid layout violations."""
    violations = []
    # Fetch all elements from context
    elements = list(context.client.elements.values())
    for element in elements:
        # Check if it's a Card element or has 'nicegui-card' in classes, or is a Dialog/dialog-related element
        is_card_or_dialog = (
            isinstance(element, Card)
            or isinstance(element, Dialog)
            or "nicegui-card" in element._classes
            or "nicegui-dialog" in element._classes
        )
        if is_card_or_dialog:
            # Check its applied classes
            for cls in element._classes:
                if is_rigid_layout_class(cls):
                    violations.append(
                        f"Rigid layout class '{cls}' used in {type(element).__name__} element at runtime."
                    )
    return violations


def test_no_rigid_sizes_in_dialog_cards():
    """Headless unit test to assert that dialog card classes do not use rigid height/width classes."""
    with Client(None):
        # 1. Clear elements to have a clean starting point
        context.client.elements.clear()

        # 2. Instantiate all the main application UI components to inspect their runtime properties
        settings = MockSettings()
        app = AutoSorterApp(settings)

        # Instantiate main UI
        app.build_ui()

        # Instantiate setup wizard
        show_wizard(app, settings)

        # Instantiate settings view
        show_settings(app, settings)

        # Instantiate specific warning and recovery dialog methods
        app.show_ml_warning_dialog("test_feature")
        app.show_rollback_recovery_dialog({"base_dir": "/mock/dir"})

        # 3. Check for any rigid layout violations in the instantiated components
        violations = inspect_runtime_elements()

        if violations:
            pytest.fail("\n".join(violations))


def test_rigid_and_responsive_runtime_detection():
    """Verify that the runtime layout checker programmatically blocks rigid width/height classes

    and successfully passes when valid responsive CSS classes are used.
    """
    with Client(None):
        # Test valid responsive configurations
        context.client.elements.clear()

        # These should pass successfully (responsive sizes)
        ui.card().classes(get_dialog_card_classes("md"))
        ui.card().classes(get_dialog_card_classes("lg"))
        ui.card().classes(get_dialog_card_classes("xl"))
        ui.card().classes("w-full max-w-md min-w-[320px] p-6")

        violations = inspect_runtime_elements()
        assert len(violations) == 0, (
            f"Expected zero violations for responsive classes, got: {violations}"
        )

        # Test rigid width configurations
        context.client.elements.clear()
        ui.card().classes("w-96")
        violations = inspect_runtime_elements()
        assert len(violations) > 0, (
            "Expected a violation for rigid width 'w-96', but none was detected."
        )

        # Test rigid height configurations
        context.client.elements.clear()
        ui.card().classes("h-48")
        violations = inspect_runtime_elements()
        assert len(violations) > 0, (
            "Expected a violation for rigid height 'h-48', but none was detected."
        )

        # Test rigid arbitrary value bracket configurations
        context.client.elements.clear()
        ui.card().classes("w-[500px]")
        violations = inspect_runtime_elements()
        assert len(violations) > 0, (
            "Expected a violation for rigid arbitrary width 'w-[500px]', but none was detected."
        )


def test_is_rigid_layout_class_validation():
    """Verify that is_rigid_layout_class correctly flags rigid sizes and allows fluid/boundary sizes."""
    # Rigid / disallowed classes
    assert is_rigid_layout_class("w-96") is True
    assert is_rigid_layout_class("w-48") is True
    assert is_rigid_layout_class("w-[500px]") is True
    assert is_rigid_layout_class("h-96") is True
    assert is_rigid_layout_class("h-[250px]") is True

    # Fluid / allowed classes
    assert is_rigid_layout_class("w-full") is False
    assert is_rigid_layout_class("w-auto") is False
    assert is_rigid_layout_class("w-1/2") is False
    assert is_rigid_layout_class("w-3/4") is False
    assert is_rigid_layout_class("h-full") is False
    assert is_rigid_layout_class("h-auto") is False
    assert is_rigid_layout_class("max-w-md") is False
    assert is_rigid_layout_class("min-w-[320px]") is False
    assert is_rigid_layout_class("max-h-screen") is False
    assert is_rigid_layout_class("min-h-[100px]") is False
    assert is_rigid_layout_class("p-6") is False
    assert is_rigid_layout_class("gap-4") is False
