#!/usr/bin/env python3
"""Standalone Component Catalog CLI entry point.

Launches the isolated NiceGUI component preview catalog environment or runs
headless accessibility verification checks.
"""

from app.ui.catalog import main

if __name__ == "__main__":
    main()
