import re
from pathlib import Path

from app.ui.notifications import TROUBLESHOOTING_URL

REPO = Path(__file__).resolve().parents[1]


def _github_anchor(heading: str) -> str:
    """Slug a Markdown heading the way GitHub does for in-page links."""
    slug = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return slug.replace(" ", "-")


def test_every_help_link_anchor_has_a_troubleshooting_section():
    page = (REPO / "docs" / "troubleshooting.md").read_text(encoding="utf-8")
    anchors = {
        _github_anchor(line.lstrip("#"))
        for line in page.splitlines()
        if line.startswith("#")
    }

    used = set()
    for source in (REPO / "app").rglob("*.py"):
        used.update(
            re.findall(r"TROUBLESHOOTING_URL\}#([\w-]+)", source.read_text("utf-8"))
        )

    assert used, "no help links found; the pattern above is out of date"
    assert used <= anchors, f"help links without a section: {sorted(used - anchors)}"


def test_help_links_point_at_this_repository():
    assert TROUBLESHOOTING_URL.startswith("https://github.com/fderuiter/sortify/")
    assert TROUBLESHOOTING_URL.endswith("docs/troubleshooting.md")
