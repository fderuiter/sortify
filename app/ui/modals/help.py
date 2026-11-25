"""Keyboard shortcut cheat sheet and offline troubleshooting help dialogs."""

import re
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, Markdown

from app.core.path_utils import get_base_path
from app.ui.heading import Heading
from app.ui.layout import ModalBox, ResponsiveModalScreen

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_IMAGE_LINE_RE = re.compile(r"^\s*!\[[^\]]*\]\([^)]+\)\s*$")


def slugify_heading(heading: str) -> str:
    """Slugify a Markdown heading text into a GitHub-compatible anchor."""
    slug = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return slug.replace(" ", "-")


def _fallback_section(anchor: str) -> tuple[str, str, bool]:
    title = (
        anchor.replace("-", " ").strip().title() if anchor else "Troubleshooting Guide"
    )
    body = (
        f"## {title}\n\n"
        "Local troubleshooting documentation could not be loaded for this topic.\n\n"
        "- Run `smart-autosorter --help` or `smart-autosorter undo --list` for CLI recovery options.\n"
        "- Select **Open in Browser** below if you want to view the online guide."
    )
    return title, body, False


def extract_troubleshooting_section(
    help_url: str | None = None,
    docs_path: Path | str | None = None,
) -> tuple[str, str, bool]:
    """Extract a Markdown section matching ``#anchor`` from bundled ``docs/troubleshooting.md``.

    Returns ``(section_title, section_markdown, found_exact)``.
    """
    anchor = ""
    if help_url and "#" in help_url:
        _, _, raw_anchor = help_url.partition("#")
        anchor = raw_anchor.strip().lower()

    resolved_path = (
        Path(docs_path)
        if docs_path is not None
        else Path(get_base_path()) / "docs" / "troubleshooting.md"
    )

    try:
        raw_text = resolved_path.read_text(encoding="utf-8")
    except OSError:
        return _fallback_section(anchor)

    lines = [line for line in raw_text.splitlines() if not _IMAGE_LINE_RE.match(line)]
    if not lines:
        return _fallback_section(anchor)

    if not anchor:
        doc_title = "Troubleshooting Guide"
        for line in lines:
            match = _HEADING_RE.match(line)
            if match and len(match.group(1)) == 1:
                doc_title = match.group(2).strip()
                break
        return doc_title, "\n".join(lines).strip(), True

    in_fence = False
    capturing = False
    target_level = 0
    section_title = ""
    captured: list[str] = []

    for line in lines:
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            if capturing:
                captured.append(line)
            continue

        if not in_fence:
            match = _HEADING_RE.match(line)
            if match:
                level = len(match.group(1))
                heading_text = match.group(2).strip()
                if capturing:
                    if level <= target_level:
                        break
                    captured.append(line)
                    continue
                if slugify_heading(heading_text) == anchor:
                    capturing = True
                    target_level = level
                    section_title = heading_text.strip('"')
                    captured.append(line)
                    continue

        if capturing:
            captured.append(line)

    if not capturing or not captured:
        return _fallback_section(anchor)

    return section_title, "\n".join(captured).strip(), True


class ShortcutCheatSheetModal(ResponsiveModalScreen[None]):
    """Modal dialog displaying categorized TUI keyboard shortcuts cheat sheet."""

    BINDINGS = [
        Binding("escape", "close", "Close dialog", show=True),
        Binding("question_mark", "close", "Close dialog", show=False),
        Binding("f1", "close", "Close dialog", show=False),
    ]

    MODAL_CSS = """
    .section-title {
        text-style: bold;
        color: $primary-lighten-2;
        margin-top: 1;
        margin-bottom: 0;
    }
    .shortcut-row {
        margin-bottom: 0;
    }
    .button-row {
        min-height: 3;
    }
    Button:focus, Input:focus, Select:focus {
        border: heavy $accent;
        text-style: bold;
    }
    ShortcutCheatSheetModal {
        &.compact .section-title {
            margin-top: 0;
        }
    }
    """

    def compose(self) -> ComposeResult:
        """Compose shortcut cheat sheet modal children."""
        with ModalBox(classes="modal-box"):
            with Vertical(classes="modal-body"):
                yield Heading(
                    "Keyboard Shortcuts Cheat Sheet [? / F1]",
                    level=1,
                    classes="modal-title",
                    id="modal-title",
                )

                yield Heading(
                    "Navigation & Tree Controls:", level=2, classes="section-title"
                )
                yield Label(
                    "  j / k       - Move selection down / up (Vim motions)",
                    classes="shortcut-row",
                )
                yield Label(
                    "  h / l       - Collapse / expand folder or go to parent/child",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+L      - Toggle file lock state", classes="shortcut-row"
                )
                yield Label(
                    "  Ctrl+R      - Open active rule manager", classes="shortcut-row"
                )
                yield Label(
                    "  F2          - Rename selected node", classes="shortcut-row"
                )
                yield Label(
                    "  Ctrl+N      - Create new target folder", classes="shortcut-row"
                )
                yield Label(
                    "  + / -       - Rate classification quality (+ / -)",
                    classes="shortcut-row",
                )

                yield Heading("Selecting Files:", level=2, classes="section-title")
                yield Label(
                    "  Space       - Check / uncheck the selected file or folder",
                    classes="shortcut-row",
                )
                yield Label(
                    "  a           - Check or uncheck everything",
                    classes="shortcut-row",
                )
                yield Label(
                    "  v           - Range select from the last checked node",
                    classes="shortcut-row",
                )
                yield Label(
                    "  x / i       - Exclude / include the checked files",
                    classes="shortcut-row",
                )
                yield Label(
                    "  r           - Move the checked files to another folder",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+F      - Search the plan tree",
                    classes="shortcut-row",
                )

                yield Heading(
                    "Application Operations:", level=2, classes="section-title"
                )
                yield Label(
                    "  Ctrl+S      - Scan directory and generate plan",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+Y      - Session history & audit log export",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+U      - Open Quarantine Vault dialog",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+E      - Execute sorting plan", classes="shortcut-row"
                )
                yield Label(
                    "  Ctrl+Z      - Undo last sorting session", classes="shortcut-row"
                )
                yield Label(
                    "  Ctrl+B      - Choose the folder to sort",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+O      - Open application settings", classes="shortcut-row"
                )
                yield Label(
                    "  Ctrl+G      - Open AI Model Manager dialog",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+W      - Open model onboarding wizard",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+X      - Launch PDF extraction strategy preview",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+D      - Open DropZone to sort dropped files",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+T / p  - Toggle background daemon pause / resume",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+A      - Open the audit report of the last run",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+K      - Open command palette overlay",
                    classes="shortcut-row",
                )
                yield Label(
                    "  G           - Create a sample dataset to try sorting on",
                    classes="shortcut-row",
                )

                yield Heading("Help & System:", level=2, classes="section-title")
                yield Label(
                    "  ? / F1      - Open shortcut cheat sheet", classes="shortcut-row"
                )
                yield Label(
                    "  Ctrl+Alt+K  - Toggle single-character keyboard shortcuts",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Escape      - Close modal dialog", classes="shortcut-row"
                )
                yield Label("  Ctrl+Q      - Quit application", classes="shortcut-row")

                yield Heading("Active Rule Manager:", level=2, classes="section-title")
                yield Label("  Space       - Toggle Rule", classes="shortcut-row")
                yield Label("  Enter       - Edit Rule", classes="shortcut-row")
                yield Label("  Delete      - Delete Rule", classes="shortcut-row")
                yield Label("  Ctrl+I / i  - Import Rules", classes="shortcut-row")
                yield Label("  Ctrl+E / e  - Export Rules", classes="shortcut-row")
                yield Label("  Ctrl+B / b  - Browse Templates", classes="shortcut-row")

                yield Heading("Audit Report:", level=2, classes="section-title")
                yield Label(
                    "  Ctrl+R      - Retry Selected Failed Item",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+T      - Retry All Failed Items",
                    classes="shortcut-row",
                )
                yield Label(
                    "  Ctrl+J      - Export JSON Report", classes="shortcut-row"
                )

                yield Heading("Command Palette:", level=2, classes="section-title")
                yield Label("  UP          - Move selection up", classes="shortcut-row")
                yield Label(
                    "  Down        - Move selection down", classes="shortcut-row"
                )

                yield Heading("Quarantine Vault:", level=2, classes="section-title")
                yield Label("  r           - Release file", classes="shortcut-row")
                yield Label("  d           - Dismiss item", classes="shortcut-row")
                yield Label("  i           - Re-inspect item", classes="shortcut-row")
                yield Label("  Ctrl+R      - Refresh vault", classes="shortcut-row")

                yield Heading("Rollback Confirm:", level=2, classes="section-title")
                yield Label("  Space       - Toggle selection", classes="shortcut-row")

            with Horizontal(classes="button-row"):
                btn_close = Button("Close", id="btn-close", variant="primary")
                btn_close.tooltip = "Close keyboard shortcut cheat sheet modal dialog"
                yield btn_close

    def on_mount(self) -> None:
        """Focus close button on mount and emit screen reader announcement."""
        super().on_mount()
        self.query_one("#btn-close", Button).focus()
        self.announce("Opened keyboard shortcut cheat sheet dialog.")

    @on(Button.Pressed, "#btn-close")
    def action_close(self) -> None:
        """Close shortcut cheat sheet modal."""
        self.announce("Closed keyboard shortcut cheat sheet dialog.")
        self.dismiss(None)


class TroubleshootingHelpModal(ResponsiveModalScreen[None]):
    """Offline contextual troubleshooting viewer backed by bundled ``docs/troubleshooting.md``."""

    BINDINGS = [
        Binding("escape", "close", "Close dialog", show=True),
        Binding("question_mark", "open_help_overlay(True)", "Help", show=False),
        Binding("f1", "open_help_overlay(False)", "Help", show=False),
    ]

    MODAL_CSS = """
    .modal-box {
        max-width: 84;
    }
    .button-row {
        min-height: 3;
    }
    .button-row Button {
        margin-left: 1;
    }
    Button:focus {
        border: heavy $accent;
        text-style: bold;
    }
    TroubleshootingHelpModal {
        background: rgba(0, 0, 0, 0.65);
        &.compact .modal-box, &.narrow .modal-box {
            max-height: 96%;
        }
        &.narrow .button-row Button {
            min-width: 0;
            padding: 0 1;
        }
    }
    """

    def __init__(
        self,
        help_url: str | None = None,
        docs_path: Path | str | None = None,
        anchor: str | None = None,
    ) -> None:
        super().__init__()
        from app.ui.notifications import TROUBLESHOOTING_URL

        if help_url is None and anchor:
            clean_anchor = anchor.lstrip("#").strip()
            resolved_url = f"{TROUBLESHOOTING_URL}#{clean_anchor}"
        else:
            resolved_url = help_url or TROUBLESHOOTING_URL

        self.help_url: str = resolved_url
        self.docs_path: Path | str | None = docs_path
        _, _, raw_anchor = self.help_url.partition("#")
        self.section_anchor: str = raw_anchor.strip().lower()
        (
            self.section_title,
            self.section_markdown,
            self.section_found,
        ) = extract_troubleshooting_section(self.help_url, docs_path=docs_path)

    def compose(self) -> ComposeResult:
        """Compose local troubleshooting section viewer and optional browser button."""
        with ModalBox(classes="modal-box"):
            yield Heading(
                f"Troubleshooting: {self.section_title}",
                level=1,
                classes="modal-title",
                id="modal-title",
            )
            with Vertical(classes="modal-body"):
                yield Markdown(self.section_markdown, id="help-markdown")
            with Horizontal(classes="button-row"):
                if self.help_url and self.help_url.startswith(("http:", "https:")):
                    btn_browser = Button(
                        "Open in Browser",
                        id="btn-open-browser",
                        variant="default",
                    )
                    btn_browser.tooltip = (
                        "Open online troubleshooting guide in external web browser"
                    )
                    yield btn_browser
                btn_close = Button("Close", id="btn-close", variant="primary")
                btn_close.tooltip = "Close troubleshooting help dialog"
                yield btn_close

    def on_mount(self) -> None:
        """Focus close button on mount and emit screen reader announcement."""
        super().on_mount()
        self.query_one("#btn-close", Button).focus()
        self.announce(f"Opened troubleshooting help: {self.section_title}.")

    @on(Button.Pressed, "#btn-open-browser")
    def action_open_browser(self) -> None:
        """Open online troubleshooting URL in user's default browser when requested."""
        open_url_fn = getattr(self.app, "open_url", None)
        if callable(open_url_fn) and self.help_url:
            open_url_fn(self.help_url)
        self.announce(f"Opened troubleshooting guide in browser: {self.help_url}")

    @on(Button.Pressed, "#btn-close")
    def action_close(self) -> None:
        """Close troubleshooting help modal."""
        self.announce("Closed troubleshooting help dialog.")
        self.dismiss(None)
