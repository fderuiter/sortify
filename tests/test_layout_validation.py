"""Tests for layout helper functions."""

import re

from app.ui.dialog_helper import get_dialog_card_classes


def is_rigid_layout_class(cls_name: str) -> bool:
    return bool(re.match(r"^(w|h)-\d+$", cls_name))


def test_dialog_card_classes():
    classes_md = get_dialog_card_classes("md")
    assert "md" in classes_md or "w-" in classes_md or "max-w" in classes_md

    classes_lg = get_dialog_card_classes("lg")
    assert "lg" in classes_lg or "w-" in classes_lg or "max-w" in classes_lg
