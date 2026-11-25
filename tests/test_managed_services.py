"""Real-writer tests for shared service ownership and exception cleanup."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.core.services import acquire_services
from app.core.store import Store

pytestmark = pytest.mark.real_db_worker


def test_overlapping_leases_share_writer_and_release_independently(tmp_path):
    store = Store(tmp_path, tmp_path / "sortify.db")
    with acquire_services(store) as first:
        worker = first.worker
        with acquire_services(
            Store(tmp_path, tmp_path / "sub" / ".." / "sortify.db")
        ) as second:
            assert first.db is second.db
            assert first.history is second.history
            assert first.worker is second.worker
            assert first.db.db_path == first.history.db_path == first.cache.db_path
        assert worker.thread.is_alive()
        assert worker.execute_write(lambda: 42) == 42
    assert not worker.thread.is_alive()
    first.close()


def test_concurrent_consumers_acquire_one_writer(tmp_path):
    store = Store(tmp_path, tmp_path / "sortify.db")
    barrier = threading.Barrier(4)

    def use_store(_):
        with acquire_services(store) as lease:
            barrier.wait(timeout=10)
            identity = id(lease.worker)
            assert (
                lease.worker.execute_write(lambda: threading.current_thread())
                is lease.worker.thread
            )
            barrier.wait(timeout=10)
            return identity

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(use_store, range(4)))) == 1


def test_failure_and_system_exit_release_leases(tmp_path):
    store = Store(tmp_path, tmp_path / "sortify.db")
    with acquire_services(store) as parent:
        with pytest.raises(SystemExit):
            with acquire_services(store):
                raise SystemExit(1)
        assert parent.worker.execute_write(lambda: "still active") == "still active"
    with acquire_services(store) as reopened:
        assert reopened.worker is not parent.worker


def test_custom_paths_are_distinct_and_queues_drain(tmp_path):
    completed = []
    custom_db = tmp_path / "one.db"
    sorted_dir = tmp_path / "sorted"
    sorted_dir.mkdir()
    with acquire_services(db_path=custom_db) as one:
        worker = one.worker
        assert one.history is not None
        assert one.cache is not None
        assert one.db.db_path == one.history.db_path == one.cache.db_path
        sid = one.history.start_session(str(sorted_dir))
        seq = one.history.moves.record_intent(
            sid, "/src/a.txt", "/dst/a.txt", src_sha256="abc123"
        )
        one.history.moves.mark_done(sid, seq, "/dst/a.txt")
        with acquire_services(db_path=tmp_path / "two.db") as two:
            assert one.worker is not two.worker
            assert two.history is not None
        one.worker.execute_write_async(lambda: completed.append("written"))
    assert completed == ["written"]
    assert not worker.thread.is_alive()


def test_failed_construction_does_not_poison_registry(tmp_path, monkeypatch):
    store = Store(tmp_path, tmp_path / "sortify.db")
    real = Store.database

    def fail(self, worker):
        raise ValueError("schema construction failed")

    monkeypatch.setattr(Store, "database", fail)
    with pytest.raises(ValueError, match="schema construction failed"):
        acquire_services(store)
    monkeypatch.setattr(Store, "database", real)
    with acquire_services(store) as lease:
        assert lease.worker.thread.is_alive()


def test_reopening_waits_for_the_previous_writer_to_drain(tmp_path):
    store = Store(tmp_path, tmp_path / "sortify.db")
    first = acquire_services(store)
    entered = threading.Event()
    finish = threading.Event()

    def pending_write():
        entered.set()
        assert finish.wait(timeout=10)

    first.worker.execute_write_async(pending_write)
    assert entered.wait(timeout=10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        closing = pool.submit(first.close)
        # Wait for close to enqueue the drain marker, not an arbitrary sleep.
        from app.core import services

        with services._condition:
            services._condition.wait_for(lambda: first._services.closing, timeout=10)
        opening = pool.submit(acquire_services, store)
        finish.set()
        closing.result(timeout=10)
        with opening.result(timeout=10) as reopened:
            assert not first.worker.thread.is_alive()
            assert reopened.worker is not first.worker


def test_live_session_and_history_export_share_services(tmp_path):
    from app.config import AppSettings
    from app.core.history import open_session_history
    from app.core.session import AppSession

    store = Store(tmp_path, tmp_path / "sortify.db")
    settings = AppSettings(str(tmp_path / "settings.json"))
    session = AppSession(settings, str(tmp_path), store=store)
    try:
        with open_session_history(store) as history:
            assert history is session.history_manager
            assert history.db.worker is session.db_worker
        assert session.db_worker.execute_write(lambda: True)
    finally:
        session.close()
