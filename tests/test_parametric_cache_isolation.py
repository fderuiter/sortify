import sqlite3
import threading

import pytest

from app.core.db_conn import (
    _cache_lock,
    _connection_cache,
    clear_connection_cache,
    get_db_connection,
)


def test_parametric_cache_isolation_selective_vs_global(tmp_path):
    # Create two temporary database files
    db_path_1 = str(tmp_path / "test1.db")
    db_path_2 = str(tmp_path / "test2.db")

    # Clear connection cache first
    clear_connection_cache(only_current_and_inactive=False)

    # 1. Main thread connection
    main_conn = get_db_connection(db_path_1)

    # 2. Simulate another active thread's connection
    # Let's start an active background thread that keeps running
    active_thread_conn_ref = []
    active_thread_ready = threading.Event()
    active_thread_stop = threading.Event()

    def run_active_thread():
        # Open connection in this active thread
        conn = get_db_connection(db_path_2)
        active_thread_conn_ref.append(conn)
        active_thread_ready.set()
        # Keep the thread alive until told to stop
        active_thread_stop.wait()

    t_active = threading.Thread(target=run_active_thread, daemon=True)
    t_active.start()
    active_thread_ready.wait()

    # 3. Simulate an inactive/dead thread connection
    # We put an entry with a dead thread ID into the connection cache
    fake_dead_thread_id = 999999
    # Ensure this thread ID is indeed not active
    active_thread_ids = {t.ident for t in threading.enumerate()}
    assert fake_dead_thread_id not in active_thread_ids

    # Create a real connection to be cached for the dead thread
    dead_thread_db_path = str(tmp_path / "dead.db")
    # Open connection to prepare it, then store it under the dead thread ID
    conn_for_dead = sqlite3.connect(dead_thread_db_path)
    with _cache_lock:
        _connection_cache[(dead_thread_db_path, fake_dead_thread_id)] = conn_for_dead

    # Now we have:
    # - main_conn (belonging to current calling thread)
    # - active_thread_conn_ref[0] (belonging to an active, non-calling thread)
    # - conn_for_dead (belonging to an inactive/dead thread)

    # Perform selective cleanup (only_current_and_inactive=True) from the main thread
    # This should close:
    # - main_conn (calling thread)
    # - conn_for_dead (dead thread)
    # And should PRESERVE:
    # - active_thread_conn_ref[0] (active, non-calling thread)

    clear_connection_cache(only_current_and_inactive=True)

    # Assert:
    # main_conn should be closed
    with pytest.raises(Exception):
        main_conn.execute("SELECT 1")

    # conn_for_dead should be closed
    with pytest.raises(Exception):
        conn_for_dead.execute("SELECT 1")

    # active_thread_conn_ref[0] should still be open and working
    # Since it belongs to the background thread, let's check its state or let the background thread run a query on it
    query_success = threading.Event()
    query_failed = threading.Event()

    def run_query_on_active_thread():
        try:
            active_thread_conn_ref[0].execute("SELECT 1")
            query_success.set()
        except Exception:
            query_failed.set()

    t_query = threading.Thread(target=run_query_on_active_thread, daemon=True)
    t_query.start()
    t_query.join()
    assert query_success.is_set()
    assert not query_failed.is_set()

    # Clean up the active background thread
    active_thread_stop.set()
    t_active.join()

    # Clear everything globally now
    clear_connection_cache(only_current_and_inactive=False)


def test_db_worker_termination_preserves_main_connections(tmp_path):
    from app.core.db_conn import get_db_connection
    from app.core.db_worker import DBWorker

    db_path = str(tmp_path / "worker_test.db")

    # 1. Main thread opens a connection
    main_conn = get_db_connection(db_path)
    # Verify main connection is operational
    main_conn.execute("SELECT 1")

    # 2. Launch a DBWorker
    worker = DBWorker()

    # Submit some task to the worker to ensure it initializes its database connection
    def worker_task():
        # Open connection in the worker thread
        conn = get_db_connection(db_path)
        conn.execute("CREATE TABLE t (id INT)")
        conn.execute("INSERT INTO t VALUES (1)")

    worker.execute_write(worker_task)

    # 3. Stop/terminate the background DBWorker
    worker.stop()

    # 4. Verify that the main thread's connection is STILL fully open and operational!
    main_conn.execute("SELECT * FROM t")

    # 5. Clean up
    clear_connection_cache(only_current_and_inactive=False)


def test_global_clear_connection_cache_closes_all_connections(tmp_path):
    from app.core.db_conn import clear_connection_cache, get_db_connection

    db_path = str(tmp_path / "global_test.db")

    # Clear connection cache first
    clear_connection_cache(only_current_and_inactive=False)

    # Main thread connection
    main_conn = get_db_connection(db_path)

    # Start a running thread and open a connection there
    running_thread_conn_ref = []
    ready = threading.Event()
    stop_event = threading.Event()

    def thread_run():
        conn = get_db_connection(db_path)
        running_thread_conn_ref.append(conn)
        ready.set()
        stop_event.wait()

    t = threading.Thread(target=thread_run, daemon=True)
    t.start()
    ready.wait()

    # Globally clear connections
    clear_connection_cache(only_current_and_inactive=False)

    # Verify both connections are closed
    with pytest.raises(Exception):
        main_conn.execute("SELECT 1")

    # Stop the running thread
    stop_event.set()
    t.join()

    # Verify running thread connection is also closed
    with pytest.raises(Exception):
        running_thread_conn_ref[0].execute("SELECT 1")
