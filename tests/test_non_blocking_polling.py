import queue
import threading
import time

import pytest

from app.core.analyzer_strategies import cooperative_join, cooperative_queue_get


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
