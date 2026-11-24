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


def test_extract_troubleshooting_section_resolves_every_used_anchor():
    from app.ui.modals.help import extract_troubleshooting_section

    used = set()
    for source in (REPO / "app").rglob("*.py"):
        used.update(
            re.findall(r"TROUBLESHOOTING_URL\}#([\w-]+)", source.read_text("utf-8"))
        )

    assert used
    for anchor in sorted(used):
        title, content, found = extract_troubleshooting_section(
            f"{TROUBLESHOOTING_URL}#{anchor}"
        )
        assert found is True, f"Failed to resolve anchor #{anchor}"
        assert title
        assert content.startswith("##")

    # Verify sibling sections do not bleed into subsequent headings
    sr_title, sr_md, sr_found = extract_troubleshooting_section(
        f"{TROUBLESHOOTING_URL}#session-recovery"
    )
    assert sr_found is True
    assert sr_title == "Session recovery"
    assert "## DropZone errors" not in sr_md

    msr_title, msr_md, msr_found = extract_troubleshooting_section(
        f"{TROUBLESHOOTING_URL}#multi-session-recovery"
    )
    assert msr_found is True
    assert msr_title == "Multi-session recovery"
    assert "## Session recovery" not in msr_md
    assert "## DropZone errors" not in msr_md


def test_extract_troubleshooting_section_full_doc_and_missing_fallbacks(tmp_path):
    from app.ui.modals.help import extract_troubleshooting_section

    full_title, full_md, full_found = extract_troubleshooting_section(
        TROUBLESHOOTING_URL
    )
    assert full_found is True
    assert "Troubleshooting" in full_title
    assert "## Session recovery" in full_md

    missing_title, missing_md, missing_found = extract_troubleshooting_section(
        f"{TROUBLESHOOTING_URL}#unknown-nonexistent-topic"
    )
    assert missing_found is False
    assert "Unknown Nonexistent Topic" in missing_title
    assert "smart-autosorter" in missing_md

    no_file_title, no_file_md, no_file_found = extract_troubleshooting_section(
        f"{TROUBLESHOOTING_URL}#session-recovery",
        docs_path=tmp_path / "missing_troubleshooting.md",
    )
    assert no_file_found is False
    assert "Session Recovery" in no_file_title
    assert "Local troubleshooting documentation could not be loaded" in no_file_md
