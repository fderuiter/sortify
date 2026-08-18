"""Pseudolocalization utility and injection helper for visual snapshot tests."""

import functools
import logging
from typing import Any, Optional

from nicegui import ui

logger = logging.getLogger(__name__)

CHAR_MAP = {
    "a": "à", "b": "ƀ", "c": "ç", "d": "ð", "e": "è", "f": "ƒ", "g": "ğ",
    "h": "ĥ", "i": "ì", "j": "ĵ", "k": "ķ", "l": "ĺ", "m": "ɱ", "n": "ñ",
    "o": "ò", "p": "þ", "q": "ʠ", "r": "ŕ", "s": "š", "t": "ţ", "u": "ù",
    "v": "ṽ", "w": "ŵ", "x": "ẋ", "y": "ý", "z": "ž",
    "A": "À", "B": "Ɓ", "C": "Ç", "D": "Ð", "E": "È", "F": "Ƒ", "G": "Ğ",
    "H": "Ĥ", "I": "Ì", "J": "Ĵ", "K": "Ķ", "L": "Ĺ", "M": "Ṁ", "N": "Ñ",
    "O": "Ò", "P": "Þ", "Q": "Ϙ", "R": "Ŕ", "S": "Š", "T": "Ţ", "U": "Ù",
    "V": "Ṽ", "W": "Ŵ", "X": "Ẋ", "Y": "Ý", "Z": "Ž",
}

EXPANSION_CHARS = "àèìòùáéíóúâêîôû"


def pseudolocalize_text(text: Any) -> Any:
    """Expand text by 40%, convert characters to accented versions, and add test markers.

    Parameters
    ----------
    text : Any
        The input text string to transform.

    Returns
    -------
    Any
        The pseudolocalized text if input is a string, otherwise original value.
    """
    if not isinstance(text, str) or not text:
        return text

    # Do not transform raw HTML tags, URLs, icon names, or CSS
    stripped = text.strip()
    if (
        stripped.startswith("<")
        or stripped.startswith("http")
        or stripped.startswith("font-family:")
        or stripped.startswith("aria-label=")
    ):
        return text

    # Transform characters to accented versions
    accented = "".join(CHAR_MAP.get(c, c) for c in text)

    # Calculate 40% expansion
    orig_len = len(text)
    target_len = int(orig_len * 1.4)
    needed = target_len - len(accented)

    if needed > 0:
        expansion = (EXPANSION_CHARS * ((needed // len(EXPANSION_CHARS)) + 1))[:needed]
        expanded = f"{accented} {expansion}" if len(accented.strip()) > 0 else accented + expansion
    else:
        expanded = accented

    return f"[{expanded}]"


def apply_pseudolocalization() -> None:
    """Monkey-patch NiceGUI elements to automatically pseudolocalize display strings at runtime."""

    # Patch ui.label
    orig_label_init = ui.label.__init__

    @functools.wraps(orig_label_init)
    def patched_label_init(self, text: str = "", *args, **kwargs):
        orig_label_init(self, pseudolocalize_text(text), *args, **kwargs)

    ui.label.__init__ = patched_label_init

    orig_label_set_text = ui.label.set_text

    @functools.wraps(orig_label_set_text)
    def patched_label_set_text(self, text: str):
        return orig_label_set_text(self, pseudolocalize_text(text))

    ui.label.set_text = patched_label_set_text

    # Patch ui.button
    orig_button_init = ui.button.__init__

    @functools.wraps(orig_button_init)
    def patched_button_init(self, text: str = "", *args, **kwargs):
        orig_button_init(self, pseudolocalize_text(text), *args, **kwargs)

    ui.button.__init__ = patched_button_init

    # Patch ui.markdown
    orig_markdown_init = ui.markdown.__init__

    @functools.wraps(orig_markdown_init)
    def patched_markdown_init(self, content: str = "", *args, **kwargs):
        orig_markdown_init(self, pseudolocalize_text(content), *args, **kwargs)

    ui.markdown.__init__ = patched_markdown_init

    # Patch ui.checkbox
    orig_checkbox_init = ui.checkbox.__init__

    @functools.wraps(orig_checkbox_init)
    def patched_checkbox_init(self, text: str = "", *args, **kwargs):
        orig_checkbox_init(self, pseudolocalize_text(text), *args, **kwargs)

    ui.checkbox.__init__ = patched_checkbox_init

    # Patch ui.switch
    orig_switch_init = ui.switch.__init__

    @functools.wraps(orig_switch_init)
    def patched_switch_init(self, text: str = "", *args, **kwargs):
        orig_switch_init(self, pseudolocalize_text(text), *args, **kwargs)

    ui.switch.__init__ = patched_switch_init

    # Patch ui.badge
    orig_badge_init = ui.badge.__init__

    @functools.wraps(orig_badge_init)
    def patched_badge_init(self, text: str = "", *args, **kwargs):
        orig_badge_init(self, pseudolocalize_text(text), *args, **kwargs)

    ui.badge.__init__ = patched_badge_init

    # Patch ui.tab
    orig_tab_init = ui.tab.__init__

    @functools.wraps(orig_tab_init)
    def patched_tab_init(self, name: str, label: Optional[str] = None, *args, **kwargs):
        if label is None:
            label = pseudolocalize_text(name)
        else:
            label = pseudolocalize_text(label)
        orig_tab_init(self, name, label, *args, **kwargs)

    ui.tab.__init__ = patched_tab_init

    # Patch ui.input
    orig_input_init = ui.input.__init__

    @functools.wraps(orig_input_init)
    def patched_input_init(
        self, label: Optional[str] = None, placeholder: Optional[str] = None, *args, **kwargs
    ):
        if label:
            label = pseudolocalize_text(label)
        if placeholder:
            placeholder = pseudolocalize_text(placeholder)
        orig_input_init(self, label=label, placeholder=placeholder, *args, **kwargs)

    ui.input.__init__ = patched_input_init
