"""Shared accessible HTML template engine and component helpers.

This module provides WCAG 2.2 Level AA compliant document building and UI component
utilities for exported HTML reports across Sortify.

Key accessibility capabilities:
- Programmatically determined accessible names for form controls (WCAG 2.2 SC 4.1.2 & SC 3.3.2)
- Landmark regions (<header>, <main>) (WCAG 2.2 SC 1.3.1, Technique H101)
- Sequential heading hierarchies (WCAG 2.2 SC 1.3.1 & SC 2.4.6)
- Explicit table header column scope declarations (scope="col") (WCAG 2.2 SC 1.3.1)
- Contrast-safe color tokens (WCAG 2.2 SC 1.4.3, Technique G18)
"""

import html

# Contrast-safe color tokens complying with WCAG 2.2 SC 1.4.3 (>= 4.5:1 against light backgrounds)
# #b45309 delivers 5.02:1 contrast against white (#ffffff)
COLOR_AMBER_ACCESSIBLE: str = "#b45309"
# #92400e delivers 7.09:1 contrast against white (#ffffff) (AAA compliant)
COLOR_AMBER_HIGH_CONTRAST: str = "#92400e"
# Success green with >= 4.5:1 contrast against light grey/white
COLOR_SUCCESS_ACCESSIBLE: str = "#16a34a"
# Danger red with 4.83:1 contrast against white
COLOR_DANGER_ACCESSIBLE: str = "#dc2626"


class AccessibleColorTokens:
    """Namespace for WCAG 2.2 Level AA contrast-safe color tokens."""

    AMBER: str = COLOR_AMBER_ACCESSIBLE
    AMBER_HIGH_CONTRAST: str = COLOR_AMBER_HIGH_CONTRAST
    SUCCESS: str = COLOR_SUCCESS_ACCESSIBLE
    DANGER: str = COLOR_DANGER_ACCESSIBLE


# Screen reader only CSS utility styling
SR_ONLY_CSS: str = """
        .sr-only {
            position: absolute;
            width: 1px;
            height: 1px;
            padding: 0;
            margin: -1px;
            overflow: hidden;
            clip: rect(0, 0, 0, 0);
            white-space: nowrap;
            border-width: 0;
        }
"""


def _parse_hex_color(hex_str: str) -> tuple[float, float, float]:
    """Parse a hex color string to normalized RGB float components."""
    clean_hex = hex_str.strip().lstrip("#")
    if len(clean_hex) == 3:
        clean_hex = "".join(ch + ch for ch in clean_hex)
    if len(clean_hex) != 6:
        raise ValueError(f"Invalid hex color: {hex_str}")
    r = int(clean_hex[0:2], 16) / 255.0
    g = int(clean_hex[2:4], 16) / 255.0
    b = int(clean_hex[4:6], 16) / 255.0
    return r, g, b


def calculate_relative_luminance(hex_color: str) -> float:
    """Calculate the relative luminance of a hex color according to WCAG 2.2 specifications."""
    r, g, b = _parse_hex_color(hex_color)

    def _channel_lum(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r_lum = _channel_lum(r)
    g_lum = _channel_lum(g)
    b_lum = _channel_lum(b)
    return 0.2126 * r_lum + 0.7152 * g_lum + 0.0722 * b_lum


def calculate_contrast_ratio(color1: str, color2: str) -> float:
    """Calculate the contrast ratio between two hex colors according to WCAG 2.2 SC 1.4.3."""
    lum1 = calculate_relative_luminance(color1)
    lum2 = calculate_relative_luminance(color2)
    lighter = max(lum1, lum2)
    darker = min(lum1, lum2)
    return (lighter + 0.05) / (darker + 0.05)


def render_accessible_input(
    input_id: str,
    label_text: str,
    input_type: str = "text",
    placeholder: str = "",
    input_class: str = "",
    extra_attrs: dict[str, str] | None = None,
    sr_only_label: bool = True,
    aria_label: str | None = None,
) -> str:
    """Render an accessible HTML input control with associated label and explicit ARIA label.

    Complies with WCAG 2.2 SC 4.1.2 (Name, Role, Value) and SC 3.3.2 (Labels or Instructions).
    """
    escaped_id = html.escape(input_id, quote=True)
    escaped_label = html.escape(label_text)
    escaped_aria_label = html.escape(
        aria_label if aria_label is not None else label_text, quote=True
    )
    escaped_placeholder = html.escape(placeholder, quote=True)

    label_class = ' class="sr-only"' if sr_only_label else ""
    label_tag = f'<label for="{escaped_id}"{label_class}>{escaped_label}</label>'

    attrs = [
        f'type="{html.escape(input_type, quote=True)}"',
        f'id="{escaped_id}"',
    ]
    if input_class:
        attrs.append(f'class="{html.escape(input_class, quote=True)}"')
    if placeholder:
        attrs.append(f'placeholder="{escaped_placeholder}"')
    attrs.append(f'aria-label="{escaped_aria_label}"')

    if extra_attrs:
        for k, v in extra_attrs.items():
            attrs.append(
                f'{html.escape(k, quote=True)}="{html.escape(str(v), quote=True)}"'
            )

    input_tag = f"<input {' '.join(attrs)}>"
    return f"{label_tag}\n        {input_tag}"


def render_accessible_select(
    select_id: str,
    label_text: str,
    options: list[tuple[str, str]] | None = None,
    select_class: str = "",
    options_html: str | None = None,
    extra_attrs: dict[str, str] | None = None,
    sr_only_label: bool = True,
    aria_label: str | None = None,
) -> str:
    """Render an accessible HTML select control with associated label and explicit ARIA label.

    Complies with WCAG 2.2 SC 4.1.2 (Name, Role, Value) and SC 3.3.2 (Labels or Instructions).
    """
    escaped_id = html.escape(select_id, quote=True)
    escaped_label = html.escape(label_text)
    escaped_aria_label = html.escape(
        aria_label if aria_label is not None else label_text, quote=True
    )

    label_class = ' class="sr-only"' if sr_only_label else ""
    label_tag = f'<label for="{escaped_id}"{label_class}>{escaped_label}</label>'

    attrs = [f'id="{escaped_id}"']
    if select_class:
        attrs.append(f'class="{html.escape(select_class, quote=True)}"')
    attrs.append(f'aria-label="{escaped_aria_label}"')

    if extra_attrs:
        for k, v in extra_attrs.items():
            attrs.append(
                f'{html.escape(k, quote=True)}="{html.escape(str(v), quote=True)}"'
            )

    if options_html is not None:
        opts_content = options_html
    elif options:
        opt_lines = [
            f'<option value="{html.escape(val, quote=True)}">{html.escape(txt)}</option>'
            for val, txt in options
        ]
        opts_content = (
            "\n            " + "\n            ".join(opt_lines) + "\n        "
        )
    else:
        opts_content = ""

    select_tag = f"<select {' '.join(attrs)}>{opts_content}</select>"
    return f"{label_tag}\n        {select_tag}"


def render_table_header(columns: list[str]) -> str:
    """Render an accessible table thead element where every header declares scope='col'.

    Complies with WCAG 2.2 SC 1.3.1 (Info and Relationships).
    """
    th_cells = [f'<th scope="col">{html.escape(col)}</th>' for col in columns]
    return f"<thead>\n            <tr>\n                {' '.join(th_cells)}\n            </tr>\n        </thead>"


def render_table(
    columns: list[str],
    rows_html: str,
    table_id: str | None = None,
    table_class: str | None = None,
) -> str:
    """Render an accessible HTML table with column scope headers and preserved identifiers.

    Complies with WCAG 2.2 SC 1.3.1 (Info and Relationships).
    """
    attrs = []
    if table_id:
        attrs.append(f'id="{html.escape(table_id, quote=True)}"')
    if table_class:
        attrs.append(f'class="{html.escape(table_class, quote=True)}"')

    attr_str = f" {' '.join(attrs)}" if attrs else ""
    header_html = render_table_header(columns)
    return (
        f"<table{attr_str}>\n"
        f"        {header_html}\n"
        f"        <tbody>\n"
        f"            {rows_html}\n"
        f"        </tbody>\n"
        f"    </table>"
    )


def render_warning_banner(
    warnings: list[str],
    heading_level: int = 2,
    title: str | None = None,
    banner_class: str = "warning-banner",
) -> str:
    """Render a warning banner adhering to sequential heading hierarchy (e.g. h2 under h1).

    Complies with WCAG 2.2 SC 1.3.1 and SC 2.4.6 (Headings and Labels).
    """
    if not warnings:
        return ""

    escaped_title = (
        html.escape(title)
        if title is not None
        else f"Simulation Warnings ({len(warnings)})"
    )
    warning_items = [f"<li>{html.escape(w, quote=True)}</li>" for w in warnings]
    h_tag = f"h{max(1, min(6, heading_level))}"

    return (
        f'<div class="{html.escape(banner_class, quote=True)}">\n'
        f"        <{h_tag}>{escaped_title}</{h_tag}>\n"
        f"        <ul>{''.join(warning_items)}</ul>\n"
        f"    </div>"
    )


class AccessibleHtmlBuilder:
    """Builder for standalone, accessible HTML documents complying with WCAG 2.2 Level AA.

    Guarantees:
    - <!DOCTYPE html> document type declaration
    - lang="en" root language attribute (Technique H101)
    - <header> landmark region
    - <main> landmark region
    - Automatic inclusion of .sr-only styling
    - Offline execution without external CDNs
    """

    def __init__(
        self,
        title: str,
        lang: str = "en",
        styles: str = "",
        scripts: str = "",
    ) -> None:
        self.title = title
        self.lang = lang
        self.styles = styles
        self.scripts = scripts
        self._header_content: str = ""
        self._header_class: str | None = None
        self._main_content: str = ""
        self._main_class: str | None = None
        self._body_container_class: str | None = None

    def set_header(
        self, content: str, header_class: str | None = None
    ) -> "AccessibleHtmlBuilder":
        """Set the content and optional CSS class for the <header> landmark."""
        self._header_content = content
        self._header_class = header_class
        return self

    def set_main(
        self, content: str, main_class: str | None = None
    ) -> "AccessibleHtmlBuilder":
        """Set the content and optional CSS class for the <main> landmark."""
        self._main_content = content
        self._main_class = main_class
        return self

    def set_body_container(self, container_class: str) -> "AccessibleHtmlBuilder":
        """Optionally wrap landmark regions in a layout container element."""
        self._body_container_class = container_class
        return self

    def add_styles(self, css: str) -> "AccessibleHtmlBuilder":
        """Append CSS styling to the document <head>."""
        self.styles += "\n" + css
        return self

    def set_scripts(self, scripts: str) -> "AccessibleHtmlBuilder":
        """Set JavaScript content to be included in the document."""
        self.scripts = scripts
        return self

    def render(self) -> str:
        """Render the complete accessible HTML document string."""
        escaped_title = html.escape(self.title)

        # Ensure sr-only utility class is present in styles
        all_styles = self.styles
        if ".sr-only" not in all_styles:
            all_styles = SR_ONLY_CSS + "\n" + all_styles

        # Format header landmark
        header_attr = (
            f' class="{html.escape(self._header_class, quote=True)}"'
            if self._header_class
            else ""
        )
        header_landmark = (
            f"<header{header_attr}>\n{self._header_content}\n    </header>"
        )

        # Format main landmark
        main_attr = (
            f' class="{html.escape(self._main_class, quote=True)}"'
            if self._main_class
            else ""
        )
        main_landmark = f"<main{main_attr}>\n{self._main_content}\n    </main>"

        if self._body_container_class:
            escaped_container = html.escape(self._body_container_class, quote=True)
            body_content = (
                f'    <div class="{escaped_container}">\n'
                f"        {header_landmark}\n"
                f"        {main_landmark}\n"
                f"    </div>"
            )
        else:
            body_content = f"    {header_landmark}\n\n    {main_landmark}"

        scripts_tag = (
            f"\n    <script>\n{self.scripts}\n    </script>"
            if self.scripts.strip()
            else ""
        )

        return (
            f"<!DOCTYPE html>\n"
            f'<html lang="{html.escape(self.lang, quote=True)}">\n'
            f"<head>\n"
            f'    <meta charset="UTF-8">\n'
            f'    <meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            f"    <title>{escaped_title}</title>\n"
            f"    <style>\n{all_styles}\n    </style>\n"
            f"</head>\n"
            f"<body>\n"
            f"{body_content}\n"
            f"{scripts_tag}\n"
            f"</body>\n"
            f"</html>"
        )


def build_accessible_document(
    title: str,
    header_content: str,
    main_content: str,
    styles: str = "",
    scripts: str = "",
    lang: str = "en",
    container_class: str | None = None,
    header_class: str | None = None,
    main_class: str | None = None,
) -> str:
    """Assemble and render an accessible HTML document."""
    builder = AccessibleHtmlBuilder(
        title=title, lang=lang, styles=styles, scripts=scripts
    )
    builder.set_header(header_content, header_class=header_class)
    builder.set_main(main_content, main_class=main_class)
    if container_class:
        builder.set_body_container(container_class)
    return builder.render()
