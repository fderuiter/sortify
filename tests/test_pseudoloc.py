"""Unit tests for pseudolocalization logic and monkey-patching."""

from nicegui import Client, ui

from tests.pseudoloc import (
    apply_pseudolocalization,
    pseudolocalize_text,
    remove_pseudolocalization,
)


def test_pseudolocalize_text_basic():
    text = "Setup Wizard Title"
    result = pseudolocalize_text(text)
    assert result.startswith("[")
    assert result.endswith("]")
    # Verify accented character conversion
    assert "Šèţùþ" in result or "Š" in result
    # Verify 40% length expansion (excluding brackets)
    inner = result[1:-1]
    assert len(inner) >= int(len(text) * 1.4)


def test_pseudolocalize_text_edge_cases():
    assert pseudolocalize_text("") == ""
    assert pseudolocalize_text(None) is None

    html = "<div class='title'>Hello</div>"
    assert pseudolocalize_text(html) == html

    url = "http://localhost:8080"
    assert pseudolocalize_text(url) == url


def test_apply_pseudolocalization_nicegui():
    apply_pseudolocalization()
    try:
        with Client(None):
            label = ui.label("Application Settings")
            assert label._text.startswith("[")
            assert label._text.endswith("]")

            btn = ui.button("Decline")
            assert btn._props.get("label", "").startswith("[")

            mk = ui.markdown("User Guide")
            assert mk.___content.startswith("[")
    finally:
        remove_pseudolocalization()
