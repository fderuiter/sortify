"""Unit tests for accessible settings sliders, ARIA range attributes, and keyboard step handlers."""

from unittest.mock import MagicMock, patch

from app.config import AppSettings
from app.ui.a11y_runner import inspect_element_tree
from app.ui.settings import create_accessible_slider, show_settings


def test_create_accessible_slider_aria_attributes():
    """Verify that create_accessible_slider binds required ARIA range attributes."""
    slider_instance = MagicMock()
    slider_instance._props = {}

    with patch("app.ui.settings.ui.slider", return_value=slider_instance):
        slider = create_accessible_slider(
            min=1,
            max=64,
            value=4,
            step=1,
            aria_label="Worker Concurrency Limit",
            value_formatter=lambda v: f"{int(v)}",
        )

    props = slider._props
    assert props.get("role") == "slider"
    assert props.get("aria-valuemin") == "1"
    assert props.get("aria-valuemax") == "64"
    assert props.get("aria-valuenow") == "4"
    assert props.get("aria-valuetext") == "4"
    assert props.get("aria-label") == "Worker Concurrency Limit"


def test_create_accessible_slider_dynamic_aria_updates():
    """Verify that changing slider value updates aria-valuenow and aria-valuetext dynamically."""
    on_change_called = []

    def mock_on_change(e):
        on_change_called.append(e.value)

    slider_instance = MagicMock()
    slider_instance._props = {}

    with patch("app.ui.settings.ui.slider", return_value=slider_instance):
        slider = create_accessible_slider(
            min=0.0,
            max=1.0,
            value=0.5,
            step=0.01,
            on_change=mock_on_change,
            aria_label="Coherence Threshold",
            value_formatter=lambda v: f"{float(v):.2f}",
        )

        slider._handle_keydown({"key": "ArrowRight"})

    assert slider_instance._props["aria-valuenow"] == "0.51"
    assert slider_instance._props["aria-valuetext"] == "0.51"
    assert len(on_change_called) == 1
    assert abs(on_change_called[0] - 0.51) < 1e-6


def test_keyboard_navigation_step_handlers():
    """Verify keyboard step adjustments: Arrow keys, Home, End, PageUp, PageDown."""
    on_change_values = []

    def on_change(e):
        on_change_values.append(e.value)

    slider_instance = MagicMock()
    slider_instance._props = {}

    with patch("app.ui.settings.ui.slider", return_value=slider_instance):
        slider = create_accessible_slider(
            min=1,
            max=100,
            value=50,
            step=1,
            on_change=on_change,
            aria_label="Test Slider",
        )

        # 1. ArrowRight -> +1
        slider._handle_keydown("ArrowRight")
        assert slider.value == 51
        assert slider_instance._props["aria-valuenow"] == "51"

        # 2. ArrowUp -> +1
        slider._handle_keydown("ArrowUp")
        assert slider.value == 52

        # 3. ArrowLeft -> -1
        slider._handle_keydown("ArrowLeft")
        assert slider.value == 51

        # 4. ArrowDown -> -1
        slider._handle_keydown("ArrowDown")
        assert slider.value == 50

        # 5. Home -> min (1)
        slider._handle_keydown("Home")
        assert slider.value == 1
        assert slider_instance._props["aria-valuenow"] == "1"

        # 6. End -> max (100)
        slider._handle_keydown("End")
        assert slider.value == 100
        assert slider_instance._props["aria-valuenow"] == "100"

        # 7. PageDown -> -10 (large_step = 10)
        slider._handle_keydown("PageDown")
        assert slider.value == 90

        # 8. PageUp -> +10 (clamped to max=100)
        slider._handle_keydown("PageUp")
        assert slider.value == 100

    assert len(on_change_values) == 8


def test_a11y005_slider_rule_inspection():
    """Verify that A11Y005_SLIDER_RANGE_ATTRIBUTES detects missing ARIA attributes."""
    # Valid element
    valid_slider = MagicMock()
    valid_slider._props = {
        "role": "slider",
        "aria-valuemin": "1",
        "aria-valuemax": "64",
        "aria-valuenow": "4",
        "aria-valuetext": "4",
    }
    valid_slider.slots = {}
    valid_slider._classes = []
    valid_slider._text = ""
    type(valid_slider).__name__ = "Slider"

    violations = inspect_element_tree(
        valid_slider, [], "settings", "Settings", "desktop", 1280
    )
    slider_violations = [
        v for v in violations if v.rule_id == "A11Y005_SLIDER_RANGE_ATTRIBUTES"
    ]
    assert len(slider_violations) == 0

    # Invalid element (missing aria-valuenow and aria-valuetext)
    invalid_slider = MagicMock()
    invalid_slider._props = {
        "role": "slider",
        "aria-valuemin": "1",
        "aria-valuemax": "64",
    }
    invalid_slider.slots = {}
    invalid_slider._classes = []
    invalid_slider._text = ""
    type(invalid_slider).__name__ = "Slider"

    violations = inspect_element_tree(
        invalid_slider, [], "settings", "Settings", "desktop", 1280
    )
    slider_violations = [
        v for v in violations if v.rule_id == "A11Y005_SLIDER_RANGE_ATTRIBUTES"
    ]
    assert len(slider_violations) == 1
    assert "aria-valuenow" in slider_violations[0].message
    assert "aria-valuetext" in slider_violations[0].message


def test_all_9_settings_sliders_have_aria_attributes():
    """Verify that show_settings creates all 9 sliders with ARIA range attributes."""
    parent_app = MagicMock()
    settings = AppSettings()

    with patch("app.ui.settings.ui") as mock_ui:
        sliders_created = []

        def mock_slider(*args, **kwargs):
            s = MagicMock()
            s._props = {}
            s.classes.return_value = s
            s.props.side_effect = lambda str_props: (
                s._props.update({"_last_props": str_props}) or s
            )
            sliders_created.append((s, kwargs))
            return s

        mock_ui.slider.side_effect = mock_slider

        show_settings(parent_app, settings)

        assert len(sliders_created) == 9
        for slider_obj, kwargs in sliders_created:
            props = slider_obj._props
            assert props.get("role") == "slider"
            assert "aria-valuemin" in props
            assert "aria-valuemax" in props
            assert "aria-valuenow" in props
            assert "aria-valuetext" in props
