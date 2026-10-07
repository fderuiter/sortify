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
            assert mode == 0o600

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
