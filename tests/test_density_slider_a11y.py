"""Unit tests for DensitySlider accessibility mixin integration and rate limiting."""

import time

from app.ui.a11y import A11yMixin
from app.ui.widgets.density_slider import DensitySlider


def test_density_slider_inherits_a11y_mixin():
    """Verify DensitySlider inherits from A11yMixin."""
    assert issubclass(DensitySlider, A11yMixin)
    slider = DensitySlider(value=50)
    assert hasattr(slider, "announcements")
    assert hasattr(slider, "announce")
    assert hasattr(slider, "update_status")


def test_density_slider_default_announcement_and_status_update():
    """Verify DensitySlider announces value changes and updates status."""
    status_updates = []

    class TrackedDensitySlider(DensitySlider):
        def update_status(self, text: str) -> None:
            status_updates.append(text)
            super().update_status(text)

    slider = TrackedDensitySlider(value=50)
    # Trigger value change
    slider.value = 75

    assert len(slider.announcements) == 1
    assert "Density threshold set to 75" in slider.announcements[0]["message"]
    assert "Density threshold set to 75" in status_updates


def test_density_slider_rate_limiting(monkeypatch):
    """Verify announcements are throttled to 1 per 250ms while update_status fires on every change."""
    status_updates = []
    fake_time = 1000.0

    def mock_time():
        return fake_time

    monkeypatch.setattr(time, "time", mock_time)

    class TrackedDensitySlider(DensitySlider):
        def update_status(self, text: str) -> None:
            status_updates.append(text)
            super().update_status(text)

    slider = TrackedDensitySlider(value=50)

    # First change at t=1000.0s -> Should announce
    slider.value = 55
    assert len(slider.announcements) == 1
    assert len(status_updates) == 1

    # Rapid change at t=1000.1s (+100ms) -> Throttled announcement, but update_status fires
    fake_time = 1000.1
    slider.value = 60
    assert len(slider.announcements) == 1
    assert len(status_updates) == 2
    assert status_updates[-1] == "Density threshold set to 60"

    # Rapid change at t=1000.2s (+200ms) -> Throttled announcement, update_status fires
    fake_time = 1000.2
    slider.value = 65
    assert len(slider.announcements) == 1
    assert len(status_updates) == 3
    assert status_updates[-1] == "Density threshold set to 65"

    # Time advances to t=1000.3s (+300ms > 250ms) -> Announcement fires
    fake_time = 1000.3
    slider.value = 70
    assert len(slider.announcements) == 2
    assert "Density threshold set to 70" in slider.announcements[1]["message"]
    assert len(status_updates) == 4


def test_density_slider_custom_announce_formatter():
    """Verify DensitySlider accepts and uses a custom announce_formatter callable."""

    def formatter(val: int) -> str:
        return f"Custom density ratio: {val}%"

    slider = DensitySlider(value=50, announce_formatter=formatter)

    slider.value = 80
    assert len(slider.announcements) == 1
    assert "Custom density ratio: 80%" in slider.announcements[0]["message"]


def test_audit_a11y_compliance_inspects_density_slider():
    """Verify audit_a11y_compliance checks DensitySlider for label/tooltip compliance."""

    class TestContainer(A11yMixin):
        def __init__(self, slider: DensitySlider):
            super().__init__()
            self._slider = slider

        def query(self, selector="*"):
            return [self._slider]

    # Slider with tooltip -> Compliant
    valid_slider = DensitySlider(value=50)
    valid_slider.tooltip = "Adjust density"
    container = TestContainer(valid_slider)

    audit = container.audit_a11y_compliance()
    assert audit["compliant"] is True
    assert audit["violations_count"] == 0

    # Slider lacking tooltip/label -> Violation
    invalid_slider = DensitySlider(value=50)
    invalid_slider.tooltip = None
    container_invalid = TestContainer(invalid_slider)

    audit_invalid = container_invalid.audit_a11y_compliance()
    violations = audit_invalid["violations"]
    missing_label_rules = [v for v in violations if v["rule"] == "A11Y001_MISSING_LABEL"]
    assert len(missing_label_rules) == 1
    assert missing_label_rules[0]["widget_id"] == "DensitySlider"


def test_extraction_preview_modal_slider_announcement():
    """Verify ExtractionPreviewModal formats contextual announcements with page status totals."""
    from app.ui.modals.reports import ExtractionPreviewModal

    class MockSettings:
        PDF_TEXT_DENSITY_THRESHOLD = 50

    modal = ExtractionPreviewModal(file_path="sample.pdf", settings=MockSettings())
    modal.page_data = [
        {"page_index": 1, "char_count": 100, "has_vector": False},  # Standard at threshold <= 100
        {"page_index": 2, "char_count": 30, "has_vector": False},   # Rasterized at threshold 50
    ]

    announcement = modal._format_slider_announcement(50)
    assert "Density threshold set to 50" in announcement
    assert "2 total pages" in announcement
    assert "1 standard, 1 rasterized" in announcement
