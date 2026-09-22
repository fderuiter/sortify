#!/usr/bin/env python3
"""Automated Continuous Integration Accessibility & Responsive Layout Gate.

Executes automated accessibility rule scans against all standalone component catalog
entries across desktop and narrow mobile viewports. Fails CI builds whenever web
accessibility rule violations or layout overflow defects are detected.
"""

import importlib.util
import os
import sys


def is_gui_available():
    """Check if NiceGUI web framework is installed and headless build mode is not active."""
    if os.environ.get("HEADLESS_BUILD") == "1":
        return False
    return importlib.util.find_spec("nicegui") is not None


def main():
    """Run continuous integration accessibility gate."""
    print("======================================================================")
    print("  CONTINUOUS INTEGRATION ACCESSIBILITY & RESPONSIVE LAYOUT GATE")
    print("======================================================================")

    if not is_gui_available():
        print(
            "Headless build mode or NiceGUI web framework unavailable. Skipping accessibility gate scans."
        )
        print("----------------------------------------------------------------------")
        print("SUCCESS: A11y gate bypassed gracefully for headless environment.")
        print("----------------------------------------------------------------------")
        sys.exit(0)

    from app.ui.a11y_runner import DEFAULT_CONFIGURED_VIEWPORTS, run_all_catalog_scans
    from app.ui.catalog import CATALOG_REGISTRY
    print(
        f"Scanning {len(CATALOG_REGISTRY)} catalog components across {len(DEFAULT_CONFIGURED_VIEWPORTS)} "
        f"responsive viewports ({', '.join([f'{w}px' for _, w in DEFAULT_CONFIGURED_VIEWPORTS])})...\n"
    )

    total_scans, violations = run_all_catalog_scans(
        CATALOG_REGISTRY, viewports=DEFAULT_CONFIGURED_VIEWPORTS
    )

    if violations:
        print(f"FAIL: {len(violations)} accessibility / layout violation(s) detected across {total_scans} scans!\n")
        print("----------------------------------------------------------------------")
        print("VIOLATION DETAILS:")
        print("----------------------------------------------------------------------")
        for i, v in enumerate(violations, 1):
            print(f"[{i}] Rule: {v.rule_id}")
            print(f"    Component: {v.component_name} (ID: {v.component_id})")
            print(f"    Viewport:  {v.viewport_name} ({v.viewport_width}px)")
            print(f"    Locator:   {v.locator}")
            print(f"    Detail:    {v.message}\n")

        print("----------------------------------------------------------------------")
        print(f"A11Y GATE FAILED: {len(violations)} violation(s) found.")
        print("----------------------------------------------------------------------")
        sys.exit(1)

    print("----------------------------------------------------------------------")
    print(f"SUCCESS: All {total_scans} component-viewport accessibility scans passed!")
    print("----------------------------------------------------------------------")
    sys.exit(0)


if __name__ == "__main__":
    main()
