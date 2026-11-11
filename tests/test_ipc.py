import asyncio
import json
import os
import sys
import time
from unittest import mock

import pytest

from app.core.daemon import ContinuousWatchdogDaemon
from app.core.ipc import (
    DaemonIPCClient,
    DaemonIPCServer,
    is_process_alive,
    read_pid_file,
)
from app.main import build_parser, handle_daemon_command


class DummySettings:
    def __init__(self):
        self.MAX_QUEUE_CAPACITY = 100
        self.MAX_WORKERS = 2
        self.DEDUP_WINDOW = 0.1
        self.RECONCILIATION_INTERVAL = 60.0
        self.LOG_FILE = "test.log"
        self.CONFLICT_POLICY = "rename"
        self.MAX_FOLDERS = 10
        self.STOP_WORDS = set()
        self.AI_CONSENT_GRANTED = False
        self.POLICIES = []

    def load(self):
        pass


def test_is_process_alive():
    current_pid = os.getpid()
    assert is_process_alive(current_pid) is True
    assert is_process_alive(999999) is False
    assert is_process_alive(-1) is False


@pytest.mark.anyio
async def test_ipc_server_lifecycle_and_lockfile(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    server = DaemonIPCServer(daemon)

    await server.start()

    try:
        assert server.is_running is True
        pid_file = tmp_path / ".autosorter.pid"
        assert pid_file.exists()

        pid_info = read_pid_file(str(tmp_path))
        assert pid_info is not None
        assert pid_info["pid"] == os.getpid()
        assert pid_info["base_dir"] == str(tmp_path)
        assert "socket_path" in pid_info

        if sys.platform != "win32":
            mode = os.stat(server.socket_path).st_mode & 0o777
            assert mode == 0o700
            pid_mode = os.stat(pid_file).st_mode & 0o777
            assert pid_mode == 0o600

        assert "auth_token" in pid_info
        assert isinstance(pid_info["auth_token"], str)
        assert len(pid_info["auth_token"]) == 64
        assert server.auth_token == pid_info["auth_token"]

        # Attempting to start another server for same directory should fail due to lockfile
        daemon2 = ContinuousWatchdogDaemon(settings, str(tmp_path))
        server2 = DaemonIPCServer(daemon2)
        with pytest.raises(RuntimeError, match="Daemon is already running"):
            await server2.start()

    finally:
        server.stop()
        assert not (tmp_path / ".autosorter.pid").exists()


@pytest.mark.anyio
async def test_ipc_stale_lockfile_cleanup(tmp_path):
    # Write a fake stale lockfile with non-existent PID
    stale_pid = 999999
    fake_sock = tmp_path / ".autosorter.sock"
    fake_sock.write_text("")
    pid_file = tmp_path / ".autosorter.pid"
    pid_file.write_text(
        json.dumps(
            {
                "pid": stale_pid,
                "socket_path": str(fake_sock),
                "base_dir": str(tmp_path),
                "start_time": time.time() - 100,
            }
        )
    )

    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    server = DaemonIPCServer(daemon)

    # Should clean up stale files and start successfully
    await server.start()
    try:
        pid_info = read_pid_file(str(tmp_path))
        assert pid_info["pid"] == os.getpid()
    finally:
        server.stop()


@pytest.mark.anyio
async def test_ipc_requests_status_health_metrics_pause_resume(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._start_time = time.time() - 10.0
    daemon._is_running = True

    server = DaemonIPCServer(daemon)
    await server.start()

    client = DaemonIPCClient(str(tmp_path))

    try:
        # Test status endpoint
        status_res = await client.send_command("status")
        assert status_res["status"] == "running"
        assert status_res["is_paused"] is False
        assert status_res["pid"] == os.getpid()
        assert status_res["base_dir"] == str(tmp_path)
        assert status_res["queue_depth"] == 0

        # Test health endpoint
        health_res = await client.send_command("health")
        assert health_res["status"] == "ok"
        assert health_res["queue_full"] is False

        # Test metrics endpoint
        metrics_res = await client.send_command("metrics")
        assert metrics_res["events_enqueued"] == 0
        assert metrics_res["events_processed"] == 0

        # Test pause endpoint
        pause_res = await client.send_command("pause")
        assert pause_res["status"] == "paused"
        assert daemon.is_paused is True

        status_after_pause = await client.send_command("status")
        assert status_after_pause["status"] == "paused"
        assert status_after_pause["is_paused"] is True

        # Test resume endpoint
        resume_res = await client.send_command("resume")
        assert resume_res["status"] == "resumed"
        assert daemon.is_paused is False

    finally:
        server.stop()


@pytest.mark.anyio
async def test_ipc_stop_endpoint(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    server = DaemonIPCServer(daemon)
    await server.start()

    client = DaemonIPCClient(str(tmp_path))

    with mock.patch.object(daemon, "stop") as mock_stop:
        stop_res = await client.send_command("stop")
        assert stop_res["status"] == "stopping"

        await asyncio.sleep(0.2)
        mock_stop.assert_called_once()

    server.stop()


def test_cli_daemon_control_commands(tmp_path, capsys):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon.start()

    try:
        # CLI: python -m app.main daemon status <dir>
        parser = build_parser()
        args = parser.parse_args(["daemon", "status", str(tmp_path)])
        handle_daemon_command(args, settings)

        out, _ = capsys.readouterr()
        assert "Daemon Status" in out
        assert "RUNNING" in out

        # CLI: python -m app.main daemon pause <dir>
        args = parser.parse_args(["daemon", "pause", str(tmp_path)])
        handle_daemon_command(args, settings)
        assert daemon.is_paused is True

        # CLI: python -m app.main daemon resume <dir>
        args = parser.parse_args(["daemon", "resume", str(tmp_path)])
        handle_daemon_command(args, settings)
        assert daemon.is_paused is False

        # CLI: python -m app.main daemon health <dir>
        args = parser.parse_args(["daemon", "health", str(tmp_path)])
        handle_daemon_command(args, settings)
        out, _ = capsys.readouterr()
        assert "Daemon Health Check" in out

        # CLI: python -m app.main daemon metrics <dir>
        args = parser.parse_args(["daemon", "metrics", str(tmp_path)])
        handle_daemon_command(args, settings)
        out, _ = capsys.readouterr()
        assert "Daemon Metrics" in out

    finally:
        daemon.stop()


@pytest.mark.anyio
async def test_ipc_drop_endpoint(tmp_path):
    settings = DummySettings()
    settings.PROTECTED_PATHS = [str(tmp_path / "protected")]
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    server = DaemonIPCServer(daemon)
    await server.start()

    client = DaemonIPCClient(str(tmp_path))

    try:
        # Create a sample file to drop
        drop_file = tmp_path / "sample.pdf"
        drop_file.write_text("sample document content")

        # Test drop endpoint with valid file
        drop_res = await client.send_command(
            "drop", {"paths": [str(drop_file)], "dry_run": True}
        )
        assert drop_res["status"] == "success"
        assert drop_res["processed_count"] == 1

        # Test drop endpoint with protected path
        prot_file = tmp_path / "protected" / "secret.txt"
        prot_file.parent.mkdir(parents=True, exist_ok=True)
        prot_file.write_text("secret")

        with pytest.raises(RuntimeError, match="Protected path blocked"):
            await client.send_command("drop", {"paths": [str(prot_file)]})

    finally:
        server.stop()


@pytest.mark.anyio
async def test_ipc_token_authentication_rejection(tmp_path):
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    server = DaemonIPCServer(daemon)
    await server.start()

    try:
        assert server.auth_token is not None
        assert len(server.auth_token) == 64

        # Helper to send a raw JSON-RPC string and read response
        async def send_raw_payload(payload_dict: dict) -> dict:
            if server.socket_path.startswith("tcp://"):
                host, port_str = server.socket_path[6:].split(":")
                reader, writer = await asyncio.open_connection(host, int(port_str))
            else:
                reader, writer = await asyncio.open_unix_connection(server.socket_path)

            try:
                msg = (json.dumps(payload_dict) + "\n").encode("utf-8")
                writer.write(msg)
                await writer.drain()
                line = await reader.readline()
                return json.loads(line.decode("utf-8"))
            finally:
                writer.close()
                await writer.wait_closed()

        # 1. Request without auth token should be rejected with -32001
        no_token_req = {
            "jsonrpc": "2.0",
            "id": 101,
            "method": "status",
            "params": {},
        }
        res = await send_raw_payload(no_token_req)
        assert "error" in res
        assert res["error"]["code"] in (-32001, -32600)
        assert res["id"] == 101

        # 2. Request with incorrect auth token should be rejected with -32001
        wrong_token_req = {
            "jsonrpc": "2.0",
            "id": 102,
            "method": "status",
            "params": {"auth_token": "0" * 64},
        }
        res2 = await send_raw_payload(wrong_token_req)
        assert "error" in res2
        assert res2["error"]["code"] in (-32001, -32600)
        assert res2["id"] == 102

        # 3. Request with valid token at top level should succeed
        valid_top_token_req = {
            "jsonrpc": "2.0",
            "id": 103,
            "method": "status",
            "auth_token": server.auth_token,
            "params": {},
        }
        res3 = await send_raw_payload(valid_top_token_req)
        assert "result" in res3
        assert res3["result"]["status"] == "running"
        assert res3["id"] == 103

        # 4. Request with valid token inside params should succeed
        valid_params_token_req = {
            "jsonrpc": "2.0",
            "id": 104,
            "method": "health",
            "params": {"auth_token": server.auth_token},
        }
        res4 = await send_raw_payload(valid_params_token_req)
        assert "result" in res4
        assert res4["result"]["status"] == "ok"
        assert res4["id"] == 104

        # 5. Client sending command with invalid explicit token should fail
        client = DaemonIPCClient(str(tmp_path))
        with pytest.raises(RuntimeError, match="Unauthorized"):
            await client.send_command("status", params={"auth_token": "invalid_token"})

    finally:
        server.stop()


@pytest.mark.anyio
async def test_ipc_umask_restoration(tmp_path):
    if sys.platform == "win32":
        pytest.skip("Umask test is POSIX only")

    # Set an initial known umask
    initial_umask = 0o022
    previous_umask = os.umask(initial_umask)

    try:
        settings = DummySettings()
        daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
        server = DaemonIPCServer(daemon)

        await server.start()
        try:
            # Check current umask after server.start() has completed
            current_umask = os.umask(0)
            os.umask(current_umask)  # restore immediately
            assert current_umask == initial_umask
        finally:
            server.stop()
    finally:
        os.umask(previous_umask)


@pytest.mark.anyio
async def test_ipc_tcp_loopback_authentication(tmp_path):
    """Test TCP loopback server behavior (Windows path) including token verification."""
    settings = DummySettings()
    daemon = ContinuousWatchdogDaemon(settings, str(tmp_path))
    daemon._is_running = True

    server = DaemonIPCServer(daemon)
    with mock.patch("sys.platform", "win32"):
        await server.start()

    try:
        assert server.socket_path.startswith("tcp://127.0.0.1:")
        assert server.auth_token is not None

        host, port_str = server.socket_path[6:].split(":")
        port = int(port_str)

        # 1. Unauthenticated request directly to TCP socket
        reader, writer = await asyncio.open_connection(host, port)
        try:
            req = {"jsonrpc": "2.0", "id": 1, "method": "status", "params": {}}
            writer.write((json.dumps(req) + "\n").encode("utf-8"))
            await writer.drain()
            line = await reader.readline()
            res = json.loads(line.decode("utf-8"))
            assert "error" in res
            assert res["error"]["code"] in (-32001, -32600)
        finally:
            writer.close()
            await writer.wait_closed()

        # 2. Authenticated client over TCP
        client = DaemonIPCClient(str(tmp_path))
        status_res = await client.send_command("status")
        assert status_res["status"] == "running"

        # 3. Unauthorized client over TCP
        with pytest.raises(RuntimeError, match="Unauthorized"):
            await client.send_command("status", params={"auth_token": "bad_token"})

    finally:
        server.stop()
