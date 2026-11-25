"""Validation helpers for built MkDocs HTML and sitemap artifacts."""

from __future__ import annotations

import os
from html.parser import HTMLParser
from pathlib import Path


class _MdLogoAnchorParser(HTMLParser):
    """Extract the first ``<a class="... md-logo ...">`` ``href`` attribute."""

    def __init__(self) -> None:
        super().__init__()
        self.logo_href: str | None = None
        self.found_logo_anchor: bool = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.found_logo_anchor or tag.lower() != "a":
            return
        attr_map = {k.lower(): v for k, v in attrs}
        raw_class = attr_map.get("class") or ""
        classes = set(raw_class.split())
        if "md-logo" not in classes:
            return
        self.found_logo_anchor = True
        self.logo_href = attr_map.get("href")


def extract_md_logo_href(html_text: str) -> tuple[bool, str | None]:
    """Return ``(found_anchor, href)`` for the primary ``md-logo`` link in *html_text*."""
    parser = _MdLogoAnchorParser()
    parser.feed(html_text)
    return parser.found_logo_anchor, parser.logo_href


def _is_external_or_scheme_href(href: str) -> bool:
    """Return True when *href* uses a URL scheme or protocol-relative prefix."""
    if href.startswith("//"):
        return True
    colon_idx = href.find(":")
    slash_idx = href.find("/")
    return colon_idx > 0 and (slash_idx == -1 or colon_idx < slash_idx)


def validate_mkdocs_site_artifacts(
    site_dir: str | os.PathLike[str] = "site",
    *,
    canonical_site_url: str,
) -> list[str]:
    """Validate built MkDocs site artifacts (root index, ``md-logo`` links, and canonical ``sitemap.xml``)."""
    from defusedxml import ElementTree as ET
    from defusedxml.common import DefusedXmlException

    site_path = Path(site_dir)
    errors: list[str] = []
    canonical_prefix = canonical_site_url.rstrip("/") + "/"

    index_path = site_path / "index.html"
    root_index_valid = False
    if not index_path.is_file():
        errors.append(f"Missing MkDocs root page: {index_path.as_posix()}")
    else:
        try:
            index_content = index_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(
                f"Could not read MkDocs root page {index_path.as_posix()}: {exc}"
            )
        else:
            if not index_content.strip():
                errors.append(f"Empty MkDocs root page: {index_path.as_posix()}")
            else:
                root_index_valid = True

    index_pages = sorted(site_path.rglob("index.html")) if root_index_valid else []
    resolved_root_index = index_path.resolve() if root_index_valid else None
    for page_path in index_pages:
        try:
            page_html = page_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            errors.append(f"Could not read MkDocs page {page_path.as_posix()}: {exc}")
            continue

        found_logo, raw_href = extract_md_logo_href(page_html)
        if not found_logo or raw_href is None:
            errors.append(f"Missing md-logo anchor in {page_path.as_posix()}")
            continue

        stripped_href = raw_href.strip()
        if not stripped_href:
            errors.append(f"Empty md-logo href in {page_path.as_posix()}")
            continue

        path_part = stripped_href.split("#", 1)[0].split("?", 1)[0]
        if _is_external_or_scheme_href(path_part):
            errors.append(
                f"Logo link '{raw_href}' in {page_path.as_posix()} must be a relative local path, "
                f"expected target {index_path.as_posix()}"
            )
            continue

        resolved = (
            page_path.resolve()
            if not path_part
            else (page_path.parent / path_part).resolve()
        )
        target_index = resolved / "index.html" if resolved.is_dir() else resolved
        if target_index != resolved_root_index:
            errors.append(
                f"Logo link '{raw_href}' in {page_path.as_posix()} resolved to "
                f"{target_index.as_posix()}, expected {index_path.as_posix()}"
            )

    sitemap_path = site_path / "sitemap.xml"
    if not sitemap_path.is_file():
        errors.append(f"Missing MkDocs sitemap: {sitemap_path.as_posix()}")
    else:
        try:
            sitemap_content = sitemap_path.read_text(encoding="utf-8")
            root = ET.fromstring(sitemap_content)
        except (OSError, UnicodeDecodeError, ET.ParseError, DefusedXmlException) as exc:
            errors.append(
                f"Invalid XML in MkDocs sitemap {sitemap_path.as_posix()}: {exc}"
            )
        else:
            loc_entries = [
                (elem.text or "").strip()
                for elem in root.iter()
                if (elem.tag == "loc" or elem.tag.endswith("}loc"))
                and (elem.text or "").strip()
            ]
            if not loc_entries:
                errors.append(
                    f"MkDocs sitemap contains no <loc> entries: {sitemap_path.as_posix()}"
                )
            else:
                locs = set(loc_entries)
                if canonical_prefix not in locs:
                    errors.append(
                        f"MkDocs sitemap missing canonical root URL '{canonical_prefix}': {sitemap_path.as_posix()}"
                    )
                for url in loc_entries:
                    if not url.startswith(canonical_prefix):
                        errors.append(
                            f"Non-canonical MkDocs sitemap <loc> URL '{url}' in {sitemap_path.as_posix()} "
                            f"(expected prefix '{canonical_prefix}')"
                        )
                for page_path in index_pages:
                    rel_dir = page_path.parent.relative_to(site_path)
                    expected_url = (
                        canonical_prefix
                        if rel_dir == Path(".")
                        else f"{canonical_prefix}{rel_dir.as_posix()}/"
                    )
                    if expected_url not in locs:
                        errors.append(
                            f"MkDocs sitemap missing canonical page URL '{expected_url}': {sitemap_path.as_posix()}"
                        )

    return errors
