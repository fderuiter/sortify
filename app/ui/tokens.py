"""Centralized Python design token architecture for the Smart AutoSorter UI framework.

Provides semantic tokens for spacing, sizing boundaries, control heights, component layouts,
and color schemes across dashboard views, settings panels, and overlay dialogs.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class SpacingTokens:
    """Semantic spacing scale for padding, margins, and flex gaps."""

    XS: str = "p-1"
    SM: str = "p-2"
    MD: str = "p-4"
    LG: str = "p-5"
    XL: str = "p-6"

    GAP_XS: str = "gap-1"
    GAP_SM: str = "gap-2"
    GAP_MD: str = "gap-3"
    GAP_LG: str = "gap-4"
    GAP_XL: str = "gap-6"

    PADDING_CARD: str = "p-5"
    PADDING_DIALOG: str = "p-6"
    PADDING_PANEL: str = "p-4"


@dataclass(frozen=True)
class SizingTokens:
    """Fluid boundaries, max widths, min touch targets, and modal viewports."""

    MIN_WIDTH_CARD: str = "min-w-[320px]"
    MAX_WIDTH_SM: str = "max-w-sm"
    MAX_WIDTH_MD: str = "max-w-md"
    MAX_WIDTH_LG: str = "max-w-lg"
    MAX_WIDTH_XL: str = "max-w-4xl"
    MAX_WIDTH_CONTAINER: str = "max-w-5xl"
    MAX_WIDTH_PROGRESS: str = "max-w-xl"

    CONTROL_HEIGHT_SM: str = "min-h-[32px]"
    CONTROL_HEIGHT_MD: str = "min-h-[40px]"
    TOUCH_TARGET_MIN: str = "min-h-[36px]"

    MAX_HEIGHT_DIALOG: str = "max-h-[85vh]"
    VIEWPORT_DIALOG_HEIGHT: str = "h-[80vh]"
    TREE_CONTAINER_CLASSES: str = "w-full flex-1 min-h-[200px] max-h-[60vh] p-2"


@dataclass(frozen=True)
class ColorTokens:
    """Semantic color palette for backgrounds, borders, text, and alert states."""

    # Neutral & Surface
    SURFACE_BG: str = "bg-white"
    PAGE_BG: str = "bg-slate-50"
    HEADER_BG: str = "bg-slate-900"
    BORDER_DEFAULT: str = "border-slate-200"
    BORDER_LIGHT: str = "border-slate-100"
    TEXT_PRIMARY: str = "text-slate-800"
    TEXT_SECONDARY: str = "text-slate-600"
    TEXT_MUTED: str = "text-slate-500"

    # Primary & Clinical
    PRIMARY_BG: str = "bg-blue-600"
    PRIMARY_TEXT: str = "text-blue-600"
    PRIMARY_LIGHT_BG: str = "bg-blue-50"
    PRIMARY_LIGHT_BORDER: str = "border-blue-100"
    PRIMARY_LIGHT_TEXT: str = "text-blue-900"

    # Success / Emerald
    SUCCESS_BG: str = "bg-green-50"
    SUCCESS_BORDER: str = "border-green-200"
    SUCCESS_TEXT: str = "text-green-800"
    SUCCESS_BTN: str = "bg-emerald-600"

    # Warning / Alert
    WARNING_BG: str = "bg-amber-50"
    WARNING_BORDER: str = "border-amber-200"
    WARNING_TEXT: str = "text-amber-800"
    WARNING_BTN: str = "bg-amber-600"

    # Error / Danger
    ERROR_BG: str = "bg-red-50"
    ERROR_BORDER: str = "border-red-200"
    ERROR_TEXT: str = "text-red-800"
    ERROR_BTN: str = "bg-red-500"


@dataclass(frozen=True)
class ComponentTokens:
    """Standardized component class presets."""

    CARD_BASE: str = (
        "w-full p-5 bg-white rounded-xl shadow-sm border border-slate-200"
    )

    DIALOG_CARD_MD: str = "w-full min-w-[320px] max-w-md p-6"
    DIALOG_CARD_LG: str = "w-full min-w-[320px] max-w-lg p-6 gap-4"
    DIALOG_CARD_XL: str = "w-full min-w-[320px] max-w-4xl p-6"

    PANEL_ERROR: str = "bg-red-50 border-red-200 border p-4 mb-4 w-full rounded-lg"
    PANEL_WARNING: str = (
        "bg-amber-50 border-amber-200 border p-4 mb-4 w-full rounded-lg"
    )
    PANEL_SUCCESS: str = (
        "bg-green-50 border-green-200 border p-4 mb-4 w-full rounded-lg"
    )
    PANEL_INFO: str = (
        "bg-blue-50 border-blue-100 border p-3 rounded-xl w-full"
    )


@dataclass(frozen=True)
class UIThemeTokens:
    """Master registry aggregating all design token schemas."""

    SPACING: SpacingTokens = SpacingTokens()
    SIZING: SizingTokens = SizingTokens()
    COLORS: ColorTokens = ColorTokens()
    COMPONENTS: ComponentTokens = ComponentTokens()


# Top-level singleton instance for convenient import
TOKENS = UIThemeTokens()

# Explicit exported constants for direct access
STANDARD_DIALOG_CARD_MD = TOKENS.COMPONENTS.DIALOG_CARD_MD
STANDARD_DIALOG_CARD_LG = TOKENS.COMPONENTS.DIALOG_CARD_LG
STANDARD_DIALOG_CARD_XL = TOKENS.COMPONENTS.DIALOG_CARD_XL
