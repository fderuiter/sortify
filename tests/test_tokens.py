"""Unit tests for the centralized UI token architecture."""

import pytest

from app.ui.dialog_helper import get_dialog_card_classes
from app.ui.tokens import (
    STANDARD_DIALOG_CARD_LG,
    STANDARD_DIALOG_CARD_MD,
    STANDARD_DIALOG_CARD_XL,
    TOKENS,
    ColorTokens,
    ComponentTokens,
    SizingTokens,
    SpacingTokens,
    UIThemeTokens,
)


def test_token_registry_structure():
    """Verify that the master token registry contains all required token sub-schemas."""
    assert isinstance(TOKENS, UIThemeTokens)
    assert isinstance(TOKENS.SPACING, SpacingTokens)
    assert isinstance(TOKENS.SIZING, SizingTokens)
    assert isinstance(TOKENS.COLORS, ColorTokens)
    assert isinstance(TOKENS.COMPONENTS, ComponentTokens)


def test_token_immutability():
    """Verify that token schema dataclasses are frozen and immutable."""
    with pytest.raises(Exception):
        TOKENS.SPACING.MD = "p-10"

    with pytest.raises(Exception):
        TOKENS.COLORS.PRIMARY_BG = "bg-red-600"


def test_dialog_card_classes_integration():
    """Verify that get_dialog_card_classes correctly utilizes component tokens."""
    assert get_dialog_card_classes("md") == TOKENS.COMPONENTS.DIALOG_CARD_MD
    assert get_dialog_card_classes("lg") == TOKENS.COMPONENTS.DIALOG_CARD_LG
    assert get_dialog_card_classes("xl") == TOKENS.COMPONENTS.DIALOG_CARD_XL

    assert STANDARD_DIALOG_CARD_MD == TOKENS.COMPONENTS.DIALOG_CARD_MD
    assert STANDARD_DIALOG_CARD_LG == TOKENS.COMPONENTS.DIALOG_CARD_LG
    assert STANDARD_DIALOG_CARD_XL == TOKENS.COMPONENTS.DIALOG_CARD_XL


def test_no_rigid_sizes_in_tokens():
    """Verify that component tokens and dialog presets do not use rigid width or height classes."""
    from tests.test_layout_validation import is_rigid_layout_class

    component_tokens = [
        TOKENS.COMPONENTS.CARD_BASE,
        TOKENS.COMPONENTS.DIALOG_CARD_MD,
        TOKENS.COMPONENTS.DIALOG_CARD_LG,
        TOKENS.COMPONENTS.DIALOG_CARD_XL,
        TOKENS.COMPONENTS.PANEL_ERROR,
        TOKENS.COMPONENTS.PANEL_WARNING,
        TOKENS.COMPONENTS.PANEL_SUCCESS,
        TOKENS.COMPONENTS.PANEL_INFO,
    ]

    for token_str in component_tokens:
        for cls in token_str.split():
            assert not is_rigid_layout_class(cls), (
                f"Token class '{cls}' in '{token_str}' is a rigid layout violation."
            )
