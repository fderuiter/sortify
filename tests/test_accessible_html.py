"""Unit tests for app/core/accessible_html.py and accessibility report verification."""

import re
from pathlib import Path

from app.core.accessible_html import (
    COLOR_AMBER_ACCESSIBLE,
    COLOR_AMBER_HIGH_CONTRAST,
    COLOR_DANGER_ACCESSIBLE,
    COLOR_SUCCESS_ACCESSIBLE,
    AccessibleColorTokens,
    AccessibleHtmlBuilder,
    build_accessible_document,
    calculate_contrast_ratio,
    calculate_relative_luminance,
    render_accessible_input,
    render_accessible_select,
    render_table,
    render_table_header,
    render_warning_banner,
)
from app.core.simulation_exporter import SimulationExporter
from app.plugins.clinical_compliance.clinical_compliance import (
    ClinicalComplianceEngine,
)


def test_contrast_ratio_formula_and_wcag_thresholds():
    """Verify WCAG 2.2 SC 1.4.3 relative luminance and contrast ratio calculations."""
    assert calculate_relative_luminance("#ffffff") == 1.0
    assert calculate_relative_luminance("#000000") == 0.0
    assert calculate_contrast_ratio(COLOR_SUCCESS_ACCESSIBLE, "#ffffff") >= 3.0
    # Black vs White is 21.0
    bw_contrast = calculate_contrast_ratio("#000000", "#ffffff")
    assert round(bw_contrast, 1) == 21.0

    # Same color is 1.0
    same_contrast = calculate_contrast_ratio("#ffffff", "#ffffff")
    assert round(same_contrast, 1) == 1.0

    # 3-char shorthand hex support
    assert calculate_contrast_ratio("#000", "#fff") == bw_contrast

    # Defect verification: old amber #d97706 fails WCAG AA minimum contrast (< 4.5:1)
    old_amber_contrast = calculate_contrast_ratio("#d97706", "#ffffff")
    assert old_amber_contrast < 4.5, f"Expected < 4.5:1, got {old_amber_contrast}"

    # Resolved amber #b45309 meets WCAG AA (>= 4.5:1)
    new_amber_contrast = calculate_contrast_ratio(COLOR_AMBER_ACCESSIBLE, "#ffffff")
    assert new_amber_contrast >= 4.5, f"Expected >= 4.5:1, got {new_amber_contrast}"
    assert round(new_amber_contrast, 2) == 5.02

    # High contrast amber #92400e meets WCAG AAA (>= 7.0:1)
    high_amber_contrast = calculate_contrast_ratio(COLOR_AMBER_HIGH_CONTRAST, "#ffffff")
    assert high_amber_contrast >= 7.0, f"Expected >= 7.0:1, got {high_amber_contrast}"
    assert round(high_amber_contrast, 2) == 7.09

    # Danger color meets WCAG AA against white
    danger_contrast = calculate_contrast_ratio(COLOR_DANGER_ACCESSIBLE, "#ffffff")
    assert danger_contrast >= 4.5

    # AccessibleColorTokens namespace
    assert AccessibleColorTokens.AMBER == COLOR_AMBER_ACCESSIBLE
    assert AccessibleColorTokens.AMBER_HIGH_CONTRAST == COLOR_AMBER_HIGH_CONTRAST


def test_accessible_html_builder_document_landmarks_and_lang():
    """Verify WCAG 2.2 SC 1.3.1 landmark regions and H101 document wrapper."""
    builder = AccessibleHtmlBuilder(
        title="Test Report",
        lang="en",
        styles="body { margin: 0; }",
        scripts="console.log('test');",
    )
    builder.set_header("<h1>Report Title</h1>")
    builder.set_main("<p>Main report content goes here.</p>")

    rendered = builder.render()

    assert rendered.startswith("<!DOCTYPE html>")
    assert '<html lang="en">' in rendered
    assert '<meta charset="UTF-8">' in rendered
    assert "<title>Test Report</title>" in rendered
    assert ".sr-only {" in rendered
    assert "<header>\n<h1>Report Title</h1>\n    </header>" in rendered
    assert "<main>\n<p>Main report content goes here.</p>\n    </main>" in rendered
    assert "<script>\nconsole.log('test');\n    </script>" in rendered


def test_accessible_html_builder_with_container():
    """Verify builder wrapping landmarks within a semantic container."""
    doc = build_accessible_document(
        title="Contained Report",
        header_content="<h1>Header</h1>",
        main_content="<p>Content</p>",
        container_class="container",
        header_class="page-header",
    )
    assert '<div class="container">' in doc
    assert '<header class="page-header">' in doc
    assert "<main>" in doc
    # Confirm both landmarks are inside body and container
    assert doc.index('<div class="container">') < doc.index("<header")
    assert doc.index("<header") < doc.index("<main")


def test_render_accessible_input_attributes():
    """Verify form control helper provides sr-only label and explicit aria-label."""
    html_out = render_accessible_input(
        input_id="searchInput",
        label_text="Search file mappings",
        placeholder="Type to filter...",
        input_class="search-input",
        aria_label="Search file mappings",
    )

    assert (
        '<label for="searchInput" class="sr-only">Search file mappings</label>'
        in html_out
    )
    assert 'id="searchInput"' in html_out
    assert 'class="search-input"' in html_out
    assert 'placeholder="Type to filter..."' in html_out
    assert 'aria-label="Search file mappings"' in html_out


def test_render_accessible_select_attributes():
    """Verify select form control helper provides sr-only label, options, and aria-label."""
    options = [("all", "All Items"), ("held", "Holds Only")]
    html_out = render_accessible_select(
        select_id="filterSelect",
        label_text="Filter items",
        options=options,
        select_class="filter-select",
        aria_label="Filter items",
    )

    assert '<label for="filterSelect" class="sr-only">Filter items</label>' in html_out
    assert 'id="filterSelect"' in html_out
    assert 'class="filter-select"' in html_out
    assert 'aria-label="Filter items"' in html_out
    assert '<option value="all">All Items</option>' in html_out
    assert '<option value="held">Holds Only</option>' in html_out


def test_render_table_header_column_scopes():
    """Verify all <th> elements in thead receive scope='col' per WCAG 2.2 SC 1.3.1."""
    cols = ["Path", "Size", "Status"]
    th_html = render_table_header(cols)

    assert '<th scope="col">Path</th>' in th_html
    assert '<th scope="col">Size</th>' in th_html
    assert '<th scope="col">Status</th>' in th_html
    assert th_html.count('<th scope="col">') == 3
    assert th_html.count('scope="col"') == 3


def test_render_table_complete():
    """Verify render_table builds complete table with col scopes and preserved IDs."""
    cols = ["Source", "Target"]
    rows = "<tr><td>a</td><td>b</td></tr>"
    table = render_table(
        columns=cols,
        rows_html=rows,
        table_id="mappingsTable",
        table_class="data-table",
    )

    assert '<table id="mappingsTable" class="data-table">' in table
    assert '<th scope="col">Source</th>' in table
    assert '<th scope="col">Target</th>' in table
    assert (
        "<tbody>\n            <tr><td>a</td><td>b</td></tr>\n        </tbody>" in table
    )


def test_render_warning_banner_sequential_heading():
    """Verify warning banner enforces h2 heading directly under h1."""
    # Empty warnings produces empty string
    assert render_warning_banner([]) == ""

    warnings = ["Disk space low", "Duplicate path detected"]
    banner = render_warning_banner(warnings, heading_level=2)

    assert '<div class="warning-banner">' in banner
    assert "<h2>Simulation Warnings (2)</h2>" in banner
    assert "<h3>" not in banner
    assert "<li>Disk space low</li>" in banner
    assert "<li>Duplicate path detected</li>" in banner


def test_simulation_exporter_accessibility_compliance(tmp_path):
    """End-to-end audit of SimulationExporter output against WCAG 2.2 Level AA requirements."""
    plan = {
        "Docs": {
            "file.txt": {
                "__type__": "file",
                "relative_source": "file.txt",
                "target_filename": "file.txt",
                "sensitivity_rating": "MEDIUM",
                "confirmed": True,
            }
        }
    }
    simulation_results = {
        "success": True,
        "warnings": ["Warning 1", "Warning 2"],
    }
    exporter = SimulationExporter(
        plan, base_dir=str(tmp_path), simulation_results=simulation_results
    )
    html_content = exporter.export_html()

    # 1. Landmark regions: header and main landmarks inside body
    assert "<header>" in html_content
    assert "</header>" in html_content
    assert "<main>" in html_content
    assert "</main>" in html_content

    # 2. Sequential headings: Warning banner uses <h2> under <h1>
    assert "<h1>Sortify Dry-Run Simulation Report</h1>" in html_content
    assert "<h2>Simulation Warnings (2)</h2>" in html_content
    assert "<h3>Simulation Warnings" not in html_content

    # 3. Form controls have sr-only labels and aria-label attributes
    assert '<label for="searchInput" class="sr-only">' in html_content
    assert 'id="searchInput"' in html_content
    assert 'aria-label="Search paths, filenames, or sensitivity"' in html_content
    assert '<label for="filterSelect" class="sr-only">' in html_content
    assert 'id="filterSelect"' in html_content
    assert 'aria-label="Filter file mappings"' in html_content

    # 4. Table headers include scope="col"
    table_match = re.search(
        r'<table[^>]*id="mappingsTable"[^>]*>(.*?)</table>', html_content, re.DOTALL
    )
    assert table_match is not None
    table_content = table_match.group(1)
    th_count = len(re.findall(r"<th\b", table_content))
    scope_col_count = len(re.findall(r'<th\b[^>]*scope="col"', table_content))
    assert th_count == 6
    assert scope_col_count == 6


def test_clinical_compliance_exporter_accessibility_and_contrast(tmp_path):
    """End-to-end audit of ClinicalComplianceEngine output against WCAG 2.2 AA requirements."""
    engine = ClinicalComplianceEngine()
    # Partially compliant score (8 of 13 essential items found = 61.5%) triggering amber status
    artifacts = [item["artifact_id"] for item in engine.checklist]
    classified = {f"file_{i}.pdf": artifacts[i] for i in range(8)}
    all_files = list(classified.keys())
    res = engine.evaluate_compliance(classified, all_files)
    assert 60.0 <= res.compliance_score_percent < 90.0

    out_file = str(tmp_path / "compliance.html")
    engine.generate_html_report(res, out_file)
    html_text = Path(out_file).read_text(encoding="utf-8")

    # 1. Landmark regions
    assert '<header class="header">' in html_text
    assert "</header>" in html_text
    assert "<main>" in html_text
    assert "</main>" in html_text

    # 2. Table headers include scope="col" on both tables
    assert '<th scope="col">Document Requirement</th>' in html_text
    assert '<th scope="col">Document Classification</th>' in html_text
    all_ths = re.findall(r"<th\b[^>]*>", html_text)
    assert len(all_ths) == 8  # 4 cols in missing + 4 cols in found
    for th in all_ths:
        assert 'scope="col"' in th

    # 3. Amber color token replaces #d97706 with contrast >= 4.5:1
    assert "#d97706" not in html_text
    assert COLOR_AMBER_ACCESSIBLE in html_text  # #b45309
    contrast = calculate_contrast_ratio(COLOR_AMBER_ACCESSIBLE, "#ffffff")
    assert contrast >= 4.5
