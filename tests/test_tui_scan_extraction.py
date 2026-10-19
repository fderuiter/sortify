"""Regression tests for the TUI scan worker building a plan from document text."""

from unittest.mock import patch

import pytest

from app.config import AppSettings
from app.ui.tui import AutoSorterTUI

pytestmark = pytest.mark.xdist_group(name="tui")

FINANCE = "Quarterly revenue budget invoice tax ledger profit expense accounting balance sheet fiscal report"
TECH = "Python kubernetes docker server deployment API database backend microservice cluster code"


def _plan_files(node):
    """Return the file names found anywhere in a nested plan dictionary."""
    found = []
    for name, value in node.items():
        if isinstance(value, dict):
            if value.get("__type__") == "file":
                found.append(name)
            else:
                found.extend(_plan_files(value))
    return found


def test_scan_worker_extracts_text_and_plans_documents(tmp_path):
    """Ctrl+S must extract text files and cluster them, not return an empty plan."""
    for i in range(1, 5):
        (tmp_path / f"fin{i}.txt").write_text(f"{FINANCE} {i}\n")
        (tmp_path / f"tech{i}.txt").write_text(f"{TECH} {i}\n")

    settings = AppSettings()
    settings._settings_model.AI_CONSENT_GRANTED = True
    app = AutoSorterTUI(settings=settings, base_dir=str(tmp_path))
    announcements = []

    with (
        patch.object(app, "call_from_thread", return_value=None),
        patch.object(app, "announce", side_effect=announcements.append),
    ):
        app.run_scan_worker.__wrapped__(app)

    try:
        planned = sorted(_plan_files(app.plan))
        assert planned == sorted(
            [f"fin{i}.txt" for i in range(1, 5)] + [f"tech{i}.txt" for i in range(1, 5)]
        )
    finally:
        if app.app_session:
            app.app_session.close()
