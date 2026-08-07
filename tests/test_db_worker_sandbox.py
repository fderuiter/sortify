import socket
from unittest.mock import MagicMock, patch

import pytest

from app.core.db_worker import DBWorker


@pytest.fixture(autouse=True)
def mock_socket_class():
    with patch("socket.socket") as mock_sock_cls:

        def create_mock_socket(*args, **kwargs):
            from app.core.shared_registry import safe_connect, safe_connect_ex

            mock_inst = MagicMock()
            mock_inst.connect.side_effect = lambda addr: safe_connect(mock_inst, addr)
            mock_inst.connect_ex.side_effect = lambda addr: safe_connect_ex(
                mock_inst, addr
            )
            return mock_inst

        mock_sock_cls.side_effect = create_mock_socket
        yield mock_sock_cls


def test_db_worker_sandbox_blocks_external_connections():
    """Verify that the background DBWorker thread blocks external connection attempts and raises a permission error."""
    db_worker = DBWorker()
    try:
        with patch("app.core.shared_registry._original_connect") as mock_orig_connect:

            def connect_external():
                s = socket.socket()
                try:
                    s.connect(("8.8.8.8", 80))
                finally:
                    s.close()

            # Submitting a connection to an external address must raise PermissionError
            with pytest.raises(
                PermissionError, match="External network connections are blocked"
            ):
                db_worker.execute_write(connect_external)

            # Ensure the original connect was never invoked for the external target
            mock_orig_connect.assert_not_called()
    finally:
        db_worker.stop()


def test_db_worker_sandbox_permits_local_connections():
    """Verify that local database/network operations (loopback and private/local subnets) complete successfully."""
    db_worker = DBWorker()
    try:
        with (
            patch("app.core.shared_registry._original_connect") as mock_orig_connect,
            patch(
                "app.core.shared_registry._original_connect_ex"
            ) as mock_orig_connect_ex,
        ):

            def connect_local():
                s1 = socket.socket()
                try:
                    s1.connect(("127.0.0.1", 8080))
                finally:
                    s1.close()

                s2 = socket.socket()
                try:
                    s2.connect_ex(("localhost", 5432))
                finally:
                    s2.close()

                s3 = socket.socket()
                try:
                    s3.connect(("my-local-db.local", 3306))
                finally:
                    s3.close()

                return "local_ok"

            res = db_worker.execute_write(connect_local)
            assert res == "local_ok"

            # Ensure original socket connect operations were permitted and called
            assert mock_orig_connect.call_count >= 2
            assert mock_orig_connect_ex.call_count >= 1
    finally:
        db_worker.stop()


def test_db_worker_sandbox_fails_safely_without_crashing():
    """Verify that blocked external connections fail safely without crashing the background daemon thread."""
    db_worker = DBWorker()
    try:

        def connect_bad():
            s = socket.socket()
            try:
                s.connect(("example.com", 80))
            finally:
                s.close()

        # This should fail with PermissionError
        with pytest.raises(
            PermissionError, match="External network connections are blocked"
        ):
            db_worker.execute_write(connect_bad)

        # The thread must remain alive and able to execute subsequent tasks
        assert db_worker.thread.is_alive()

        res = db_worker.execute_write(lambda: "still_alive")
        assert res == "still_alive"
    finally:
        db_worker.stop()


def test_db_worker_sandbox_active_from_moment_of_initialization():
    """Verify that the thread restriction is active from the very first task and remains permanent."""
    db_worker = DBWorker()
    try:

        def check_sandbox_flag():
            from app.core.shared_registry import _thread_local

            return (
                getattr(_thread_local, "sandboxed", False),
                getattr(_thread_local, "reason", ""),
            )

        # First query must run inside the sandbox immediately
        sandboxed, reason = db_worker.execute_write(check_sandbox_flag)
        assert sandboxed is True
        assert reason == "database worker execution"

        # Subsequent query must also be permanently restricted
        sandboxed_again, reason_again = db_worker.execute_write(check_sandbox_flag)
        assert sandboxed_again is True
        assert reason_again == "database worker execution"
    finally:
        db_worker.stop()
