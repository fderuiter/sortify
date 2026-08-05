import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.config import AppSettings
from app.core.analyzer_strategies import cooperative_join, cooperative_queue_get
from app.ui.app import AutoSorterApp


def test_cooperative_queue_get_success():
    """Verify that cooperative_queue_get successfully retrieves an item."""
    q = queue.Queue()
    q.put("success_item")
    res = cooperative_queue_get(q, timeout=1.0)
    assert res == "success_item"


def test_cooperative_queue_get_timeout():
    """Verify that cooperative_queue_get raises queue.Empty on timeout."""
    q = queue.Queue()
    with pytest.raises(queue.Empty):
        cooperative_queue_get(q, timeout=0.05)


def test_cooperative_join_thread():
    """Verify cooperative_join joins a thread successfully."""

    def dummy():
        time.sleep(0.01)

    t = threading.Thread(target=dummy)
    t.start()
    assert t.is_alive()
    cooperative_join(t, timeout=1.0)
    assert not t.is_alive()


@pytest.mark.anyio
async def test_verify_current_plan_async():
    """Verify that verify_current_plan is asynchronous and offloads the heavy work."""
    settings = AppSettings()
    app = AutoSorterApp(settings)
    app.base_dir = "/dummy/path"
    app.plan = {"file.txt": None}

    with patch(
        "app.core.verifier.VerificationEngine.verify_plan_integrity"
    ) as mock_verify:
        mock_verify.return_value = {"success": True, "warnings": []}

        # Call the async verify_current_plan
        await app.verify_current_plan()

        mock_verify.assert_called_once_with("/dummy/path", {"file.txt": None})


@pytest.mark.anyio
async def test_update_ai_warning_async():
    """Verify that update_ai_warning runs on a background task non-blockingly."""
    settings = AppSettings()
    app = AutoSorterApp(settings)
    app.ai_warnings_label = MagicMock()

    with patch("app.core.verifier.check_ai_status") as mock_check_ai:
        mock_check_ai.return_value = (True, "")

        # Call update_ai_warning which triggers a background task
        app.update_ai_warning()

        # Yield control briefly to let the task run
        import asyncio

        for _ in range(100):
            if mock_check_ai.called:
                break
            await asyncio.sleep(0.02)

        mock_check_ai.assert_called_once_with(settings)
        app.ai_warnings_label.set_text.assert_called_with("")
        app.ai_warnings_label.set_visibility.assert_called_with(False)
