"""Async IPC server and client for daemon status telemetry and process control."""

import asyncio
import json
import logging
import os
import sys
import tempfile
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("app.ipc")

PID_FILE_NAME = ".autosorter.pid"
SOCKET_FILE_NAME = ".autosorter.sock"


def is_process_alive(pid: int) -> bool:
    """Check if a process with given PID is currently running."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            SYNCHRONIZE = 0x00100000
            handle = kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid
            )
            if handle == 0:
                return False
            exit_code = ctypes.c_ulong()
            if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                STILL_ACTIVE = 259
                is_alive = exit_code.value == STILL_ACTIVE
            else:
                is_alive = False
            kernel32.CloseHandle(handle)
            return is_alive
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def read_pid_file(base_dir: str) -> Optional[Dict[str, Any]]:
    """Read and parse the daemon PID file from base_dir."""
    pid_path = os.path.join(base_dir, PID_FILE_NAME)
    if not os.path.exists(pid_path):
        return None
    try:
        with open(pid_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "pid" in data:
                return data
    except Exception as e:
        logger.warning(f"Failed to read or parse PID file at {pid_path}: {e}")
    return None


class DaemonIPCServer:
    """Async IPC server hosting JSON-RPC status telemetry and control endpoints."""

    def __init__(self, daemon):
        self.daemon = daemon
        self.base_dir = daemon.base_dir
        self.server: Optional[asyncio.Server] = None
        self.socket_path: Optional[str] = None
        self.pid_path = os.path.join(self.base_dir, PID_FILE_NAME)
        self.is_running = False

    async def start(self):
        """Start the IPC server and write the .autosorter.pid lockfile."""
        # Step 1: Check for existing lockfile / process
        existing_info = read_pid_file(self.base_dir)
        if existing_info:
            existing_pid = existing_info.get("pid")
            if existing_pid and is_process_alive(existing_pid):
                raise RuntimeError(
                    f"Daemon is already running for directory {self.base_dir} (PID: {existing_pid})."
                )
            logger.info(f"Removing stale lockfile and socket from PID {existing_pid}")
            stale_sock = existing_info.get("socket_path")
            if stale_sock and os.path.exists(stale_sock) and not stale_sock.startswith("tcp://"):
                try:
                    os.unlink(stale_sock)
                except Exception as e:
                    logger.warning(f"Failed to remove stale socket {stale_sock}: {e}")
            if os.path.exists(self.pid_path):
                try:
                    os.unlink(self.pid_path)
                except Exception as e:
                    logger.warning(f"Failed to remove stale pid file {self.pid_path}: {e}")

        # Step 2: Determine socket path
        if sys.platform == "win32":
            # On Windows, use a local TCP server on loopback interface
            self.server = await asyncio.start_server(
                self._handle_client, host="127.0.0.1", port=0
            )
            sockets = self.server.sockets
            if sockets:
                port = sockets[0].getsockname()[1]
                self.socket_path = f"tcp://127.0.0.1:{port}"
            else:
                raise RuntimeError("Failed to bind local loopback port on Windows")
        else:
            default_sock = os.path.join(self.base_dir, SOCKET_FILE_NAME)
            # Ensure socket path length is within OS limits (~100 chars on Linux/macOS)
            if len(default_sock) > 85:
                import hashlib

                h = hashlib.md5(self.base_dir.encode("utf-8")).hexdigest()[:8]
                self.socket_path = os.path.join(
                    tempfile.gettempdir(), f"autosorter_{h}.sock"
                )
            else:
                self.socket_path = default_sock

            if os.path.exists(self.socket_path):
                try:
                    os.unlink(self.socket_path)
                except Exception:
                    pass

            self.server = await asyncio.start_unix_server(
                self._handle_client, path=self.socket_path
            )

            # Restrict socket permissions on POSIX
            try:
                os.chmod(self.socket_path, 0o600)
            except Exception as e:
                logger.warning(f"Failed to set 0600 permissions on IPC socket: {e}")

        self.is_running = True

        # Step 3: Write PID lockfile
        pid_info = {
            "pid": os.getpid(),
            "socket_path": self.socket_path,
            "base_dir": self.base_dir,
            "start_time": time.time(),
        }
        try:
            with open(self.pid_path, "w", encoding="utf-8") as f:
                json.dump(pid_info, f, indent=2)
            try:
                os.chmod(self.pid_path, 0o600)
            except Exception:
                pass
        except Exception as e:
            logger.error(f"Failed to write PID lockfile at {self.pid_path}: {e}")

        logger.info(f"IPC server listening on {self.socket_path}")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Handle incoming line-delimited JSON-RPC requests."""
        while self.is_running:
            try:
                line = await reader.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                response = self._process_rpc_line(line_str)
                response_bytes = (json.dumps(response) + "\n").encode("utf-8")
                writer.write(response_bytes)
                await writer.drain()
            except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
                break
            except Exception as e:
                logger.error(f"IPC request processing error: {e}")
                break

        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    def _process_rpc_line(self, line_str: str) -> Dict[str, Any]:
        """Parse line string as JSON-RPC 2.0 and return response dictionary."""
        try:
            data = json.loads(line_str)
        except json.JSONDecodeError:
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error: Invalid JSON"},
            }

        if not isinstance(data, dict):
            return {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32600, "message": "Invalid Request: Must be object"},
            }

        req_id = data.get("id", 1)
        method = data.get("method")
        params = data.get("params") or {}

        if not method or not isinstance(method, str):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32600, "message": "Invalid Request: Missing method"},
            }

        try:
            result = self._dispatch_method(method.lower(), params)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as e:
            logger.error(f"Method '{method}' execution failed: {e}")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {str(e)}"},
            }

    def _dispatch_method(self, method: str, params: dict) -> Dict[str, Any]:
        """Dispatch JSON-RPC method to corresponding daemon telemetry/control handler."""
        if method == "status":
            return self.daemon.get_status_telemetry()
        elif method == "health":
            return self.daemon.get_health_telemetry()
        elif method == "metrics":
            return self.daemon.get_metrics_telemetry()
        elif method == "pause":
            self.daemon.pause()
            return {"status": "paused", "message": "Daemon triage pipeline paused"}
        elif method == "resume":
            self.daemon.resume()
            return {"status": "resumed", "message": "Daemon triage pipeline resumed"}
        elif method == "drop":
            return self._handle_drop_rpc(params)
        elif method == "stop":
            # Schedule daemon shutdown after response is sent
            loop = asyncio.get_running_loop()
            loop.call_soon(self._trigger_daemon_stop)
            return {"status": "stopping", "message": "Daemon shutting down"}
        else:
            raise ValueError(f"Method '{method}' not found")

    def _handle_drop_rpc(self, params: dict) -> Dict[str, Any]:
        """Handle 'drop' JSON-RPC method request forwarding payloads to active triage pipeline."""
        if not isinstance(params, dict):
            raise ValueError("Invalid parameters: params must be a JSON object")

        raw_paths = params.get("paths")
        if raw_paths is None:
            raw_paths = params.get("path")

        if not raw_paths:
            raise ValueError("Missing required 'paths' or 'path' parameter")

        if isinstance(raw_paths, str):
            path_list = [raw_paths]
        elif isinstance(raw_paths, list):
            path_list = raw_paths
        else:
            raise ValueError("Parameter 'paths' must be a string or array of strings")

        dry_run = bool(params.get("dry_run", False))
        dest_dir = params.get("dest_dir")

        validated_paths = []
        invalid_reasons = []

        for p in path_list:
            if not p or not isinstance(p, str):
                continue
            clean_p = p.strip().strip("'\"")
            if not clean_p:
                continue
            abs_p = os.path.abspath(clean_p)
            if not os.path.exists(abs_p):
                invalid_reasons.append(f"Path does not exist: {clean_p}")
                continue

            if hasattr(self.daemon, "should_ignore_path") and self.daemon.should_ignore_path(abs_p):
                invalid_reasons.append(f"Ignored path pattern: {clean_p}")
                continue

            settings = getattr(self.daemon, "settings", None)
            if settings:
                protected = getattr(settings, "PROTECTED_PATHS", [])
                from app.core.mover import is_subpath_or_equal

                is_prot = False
                for prot in protected:
                    if prot and is_subpath_or_equal(abs_p, prot):
                        is_prot = True
                        break
                if is_prot:
                    invalid_reasons.append(f"Protected path blocked: {clean_p}")
                    continue

            validated_paths.append(abs_p)

        if not validated_paths:
            reasons_str = "; ".join(invalid_reasons) if invalid_reasons else "No valid file paths supplied."
            raise ValueError(f"Path validation failed: {reasons_str}")

        if hasattr(self.daemon, "process_dropped_items"):
            res = self.daemon.process_dropped_items(validated_paths, dry_run=dry_run, dest_dir=dest_dir)
            res["validated_paths"] = validated_paths
            return res
        else:
            return {
                "status": "success",
                "processed_count": len(validated_paths),
                "validated_paths": validated_paths,
                "message": f"Successfully forwarded {len(validated_paths)} dropped items to triage pipeline",
            }

    def _trigger_daemon_stop(self):
        """Asynchronously trigger daemon stop in a background thread."""
        import threading

        t = threading.Thread(target=self.daemon.stop, daemon=True)
        t.start()

    def stop(self):
        """Stop the IPC server and remove socket and PID lockfiles."""
        self.is_running = False
        if self.server:
            try:
                self.server.close()
            except Exception:
                pass
            self.server = None

        if self.socket_path and not self.socket_path.startswith("tcp://"):
            if os.path.exists(self.socket_path):
                try:
                    os.unlink(self.socket_path)
                except Exception as e:
                    logger.warning(f"Failed to remove socket file {self.socket_path}: {e}")

        if os.path.exists(self.pid_path):
            try:
                os.unlink(self.pid_path)
            except Exception as e:
                logger.warning(f"Failed to remove PID lockfile {self.pid_path}: {e}")

        logger.info("IPC server stopped and lockfiles cleaned.")


class DaemonIPCClient:
    """Client for connecting to local daemon IPC server over sockets."""

    def __init__(self, base_dir: str):
        self.base_dir = os.path.abspath(base_dir)

    async def send_command(
        self, method: str, params: Optional[dict] = None, timeout: float = 5.0
    ) -> Dict[str, Any]:
        """Send a JSON-RPC method request to the daemon IPC server and return result dict."""
        pid_info = read_pid_file(self.base_dir)
        if not pid_info:
            raise RuntimeError(f"No active daemon lockfile found in '{self.base_dir}'.")

        pid = pid_info.get("pid")
        socket_path = pid_info.get("socket_path")

        if not pid or not is_process_alive(pid):
            raise RuntimeError(
                f"Daemon process (PID {pid}) is not running for '{self.base_dir}'."
            )

        if not socket_path:
            raise RuntimeError("Invalid socket path in daemon lockfile.")

        req_payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        }
        req_bytes = (json.dumps(req_payload) + "\n").encode("utf-8")

        async def _communicate():
            if socket_path.startswith("tcp://"):
                host, port_str = socket_path[6:].split(":")
                reader, writer = await asyncio.open_connection(host, int(port_str))
            else:
                if not os.path.exists(socket_path):
                    raise RuntimeError(f"Socket file '{socket_path}' does not exist.")
                reader, writer = await asyncio.open_unix_connection(socket_path)

            try:
                writer.write(req_bytes)
                await writer.drain()

                line = await reader.readline()
                if not line:
                    raise RuntimeError("IPC server closed connection unexpectedly.")

                res_data = json.loads(line.decode("utf-8", errors="replace"))
                if "error" in res_data:
                    err_msg = res_data["error"].get("message", "Unknown IPC error")
                    raise RuntimeError(f"Daemon IPC error: {err_msg}")
                if "result" not in res_data:
                    raise RuntimeError("Malformed IPC response: missing result field")
                return res_data["result"]
            finally:
                try:
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass

        return await asyncio.wait_for(_communicate(), timeout=timeout)
