"""Async IPC server and client for daemon status telemetry and process control."""

import asyncio
import contextlib
import json
import logging
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("app.ipc")

PID_FILE_NAME = ".autosorter.pid"
SOCKET_FILE_NAME = ".autosorter.sock"


def get_ipc_dir() -> str:
    """Resolve the user-scoped directory storing shared daemon IPC lock and socket files."""
    override = os.environ.get("AUTOSORTER_IPC_DIR")
    if override and override.strip():
        ipc_dir = os.path.abspath(os.path.expanduser(override.strip()))
    else:
        from app.config import get_app_dir

        ipc_dir = str(get_app_dir())
    os.makedirs(ipc_dir, exist_ok=True)
    return ipc_dir


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
            logger.debug("Could not query process %s", pid, exc_info=True)
            return False
    else:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def _read_pid_file_at(pid_path: str) -> Optional[dict[str, Any]]:
    if not os.path.exists(pid_path):
        return None
    try:
        with open(pid_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and "pid" in data:
                return data
    except (OSError, ValueError) as e:
        logger.warning(f"Failed to read or parse PID file at {pid_path}: {e}")
    return None


def read_pid_file(base_dir: Optional[str | Path] = None) -> Optional[dict[str, Any]]:
    """Read and parse the user-scoped daemon PID file (falling back to legacy folder PID file)."""
    shared_pid_path = os.path.join(get_ipc_dir(), PID_FILE_NAME)
    shared_data = _read_pid_file_at(shared_pid_path)
    if shared_data is not None:
        return shared_data

    if base_dir is not None:
        legacy_pid_path = os.path.join(os.path.abspath(str(base_dir)), PID_FILE_NAME)
        if os.path.normcase(os.path.abspath(legacy_pid_path)) != os.path.normcase(
            os.path.abspath(shared_pid_path)
        ):
            return _read_pid_file_at(legacy_pid_path)
    return None


class DaemonIPCServer:
    """Async IPC server hosting JSON-RPC status telemetry and control endpoints."""

    def __init__(self, daemon: Any, control_dir: Optional[str | Path] = None) -> None:
        self.daemon = daemon
        self.base_dir: Optional[str] = getattr(daemon, "base_dir", None)
        self.control_dir = (
            os.path.abspath(str(control_dir))
            if control_dir is not None
            else get_ipc_dir()
        )
        self.ipc_dir = self.control_dir
        self.server: Optional[asyncio.Server] = None
        self.socket_path: Optional[str] = None
        self.pid_path = os.path.join(self.control_dir, PID_FILE_NAME)
        self.auth_token: Optional[str] = None
        self.start_time: float = 0.0
        self.is_running = False

    def _watched_folders_list(self) -> list[str]:
        getter = getattr(self.daemon, "get_watched_folders", None)
        if callable(getter):
            res = getter()
            if isinstance(res, list):
                return [str(p) for p in res]
        raw = getattr(self.daemon, "watched_folders", None)
        if isinstance(raw, (list, tuple, set)):
            return [str(p) for p in raw]
        if self.base_dir:
            return [str(self.base_dir)]
        return []

    def _cleanup_legacy_folder_lockfiles(self) -> None:
        """Remove stale legacy per-folder lockfiles if no live process owns them."""
        for folder in self._watched_folders_list():
            if not folder or not os.path.isdir(folder):
                continue
            legacy_pid = os.path.join(folder, PID_FILE_NAME)
            if os.path.abspath(legacy_pid) == os.path.abspath(self.pid_path):
                continue
            info = _read_pid_file_at(legacy_pid)
            if info:
                lpid = info.get("pid")
                if isinstance(lpid, int) and is_process_alive(lpid):
                    raise RuntimeError(
                        f"Daemon is already running for directory {folder} (PID: {lpid})."
                    )
                lsock = info.get("socket_path")
                if (
                    isinstance(lsock, str)
                    and lsock
                    and not lsock.startswith("tcp://")
                    and os.path.exists(lsock)
                ):
                    with contextlib.suppress(OSError):
                        os.unlink(lsock)
                with contextlib.suppress(OSError):
                    os.unlink(legacy_pid)

    def _write_pid_lockfile(self) -> None:
        watched = self._watched_folders_list()
        primary_base = watched[0] if watched else self.base_dir
        pid_info = {
            "pid": os.getpid(),
            "socket_path": self.socket_path,
            "auth_token": self.auth_token,
            "base_dir": primary_base,
            "watched_folders": watched,
            "start_time": self.start_time,
        }
        try:
            os.makedirs(self.ipc_dir, exist_ok=True)
            with open(self.pid_path, "w", encoding="utf-8") as f:
                json.dump(pid_info, f, indent=2)
            with contextlib.suppress(OSError):
                os.chmod(self.pid_path, 0o600)
        except Exception as e:
            logger.error(
                f"Failed to write PID lockfile at {self.pid_path}: {e}", exc_info=True
            )

    def refresh_pid_file(self) -> None:
        """Rewrite the PID lockfile when watched folders change dynamically."""
        if self.is_running and self.auth_token and self.socket_path:
            self._write_pid_lockfile()

    async def start(self) -> None:
        """Start the shared user-scoped IPC server and write the .autosorter.pid lockfile."""
        existing_info = _read_pid_file_at(self.pid_path)
        if existing_info:
            existing_pid = existing_info.get("pid")
            if isinstance(existing_pid, int) and is_process_alive(existing_pid):
                watched = existing_info.get("watched_folders") or (
                    [existing_info["base_dir"]] if existing_info.get("base_dir") else []
                )
                watched_desc = ", ".join(str(p) for p in watched) or self.ipc_dir
                raise RuntimeError(
                    f"Daemon is already running (PID: {existing_pid}, watching: {watched_desc})."
                )
            logger.info(f"Removing stale lockfile and socket from PID {existing_pid}")
            stale_sock = existing_info.get("socket_path")
            if (
                isinstance(stale_sock, str)
                and stale_sock
                and os.path.exists(stale_sock)
                and not stale_sock.startswith("tcp://")
            ):
                try:
                    os.unlink(stale_sock)
                except OSError as e:
                    logger.warning(f"Failed to remove stale socket {stale_sock}: {e}")
            if os.path.exists(self.pid_path):
                try:
                    os.unlink(self.pid_path)
                except OSError as e:
                    logger.warning(
                        f"Failed to remove stale pid file {self.pid_path}: {e}"
                    )

        self._cleanup_legacy_folder_lockfiles()

        # Generate 256-bit secret token for server authentication
        self.auth_token = secrets.token_hex(32)
        self.start_time = time.time()

        if sys.platform == "win32":
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
            os.makedirs(self.ipc_dir, exist_ok=True)
            default_sock = os.path.join(self.ipc_dir, SOCKET_FILE_NAME)
            if len(default_sock) > 85:
                import hashlib

                h = hashlib.sha256(self.ipc_dir.encode("utf-8")).hexdigest()[:12]
                self.socket_path = os.path.join(
                    tempfile.gettempdir(), f"autosorter_{h}.sock"
                )
            else:
                self.socket_path = default_sock

            if os.path.exists(self.socket_path):
                with contextlib.suppress(OSError):
                    os.unlink(self.socket_path)

            start_unix_server = getattr(asyncio, "start_unix_server", None)
            if start_unix_server is None:
                raise RuntimeError(
                    "Unix domain sockets are not supported on this platform."
                )

            old_umask = os.umask(0o077)
            try:
                self.server = await start_unix_server(
                    self._handle_client, path=self.socket_path
                )
            finally:
                os.umask(old_umask)

            try:
                os.chmod(self.socket_path, 0o700)
            except OSError as e:
                logger.warning(f"Failed to set 0700 permissions on IPC socket: {e}")

        self.is_running = True
        self._write_pid_lockfile()
        logger.info(f"IPC server listening on {self.socket_path}")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle incoming line-delimited JSON-RPC requests."""
        while self.is_running:
            try:
                line = await reader.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue

                response = await self._process_rpc_line(line_str)
                response_bytes = (json.dumps(response) + "\n").encode("utf-8")
                writer.write(response_bytes)
                await writer.drain()
            except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
                break
            except Exception as e:
                logger.error(f"IPC request processing error: {e}", exc_info=True)
                break

        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            logger.debug("Failed to close IPC client connection", exc_info=True)

    async def _process_rpc_line(self, line_str: str) -> dict[str, Any]:
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

        token = data.get("auth_token") or data.get("token")
        if not token and isinstance(params, dict):
            token = params.get("auth_token") or params.get("token")

        if (
            not self.auth_token
            or not token
            or not isinstance(token, str)
            or not secrets.compare_digest(token, self.auth_token)
        ):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {
                    "code": -32001,
                    "message": "Unauthorized: Missing or invalid authentication token",
                },
            }

        if not method or not isinstance(method, str):
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32600, "message": "Invalid Request: Missing method"},
            }

        try:
            result = await self._dispatch_method(
                method.lower(), params if isinstance(params, dict) else {}
            )
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except Exception as e:
            logger.error(f"Method '{method}' execution failed: {e}", exc_info=True)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {str(e)}"},
            }

    @staticmethod
    def _extract_folders_param(params: dict[str, Any]) -> Optional[list[str]]:
        raw = params.get("folders")
        if raw is None and "folder" in params and params.get("folder") is not None:
            raw = [params["folder"]]
        if raw is None:
            return None
        if isinstance(raw, str):
            return [raw]
        if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
            return list(raw)
        raise ValueError("Parameter 'folders' must be a string or list of strings")

    async def _dispatch_method(
        self, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Dispatch JSON-RPC method to corresponding daemon telemetry/control handler."""
        folders = self._extract_folders_param(params)
        if folders is not None:
            folders = [os.path.abspath(p) for p in folders if p]

        if method == "status":
            telemetry = self.daemon.get_status_telemetry()
            if folders:
                watched_norm = {
                    os.path.normcase(os.path.abspath(p)): p
                    for p in telemetry.get("watched_folders", [])
                }
                paused_norm = {
                    os.path.normcase(os.path.abspath(p))
                    for p in telemetry.get("paused_folders", [])
                }
                telemetry = dict(telemetry)
                telemetry["requested_folders"] = folders
                telemetry["folder_states"] = {
                    f: (
                        "paused"
                        if os.path.normcase(f) in paused_norm
                        or telemetry.get("is_paused")
                        else (
                            "watching"
                            if os.path.normcase(f) in watched_norm
                            else "unwatched"
                        )
                    )
                    for f in folders
                }
            return telemetry
        elif method == "health":
            return self.daemon.get_health_telemetry()
        elif method == "metrics":
            return self.daemon.get_metrics_telemetry()
        elif method == "pause":
            if folders and hasattr(self.daemon, "pause_folders"):
                res = self.daemon.pause_folders(folders)
                return {
                    "status": res.get("status", "paused"),
                    "message": "Selected folder watch(es) paused",
                    **res,
                }
            self.daemon.pause()
            return {"status": "paused", "message": "Daemon triage pipeline paused"}
        elif method == "resume":
            if folders and hasattr(self.daemon, "resume_folders"):
                res = self.daemon.resume_folders(folders)
                return {
                    "status": res.get("status", "resumed"),
                    "message": "Selected folder watch(es) resumed",
                    **res,
                }
            self.daemon.resume()
            return {"status": "resumed", "message": "Daemon triage pipeline resumed"}
        elif method == "drop":
            return await self._handle_drop_rpc(params)
        elif method in ("reload", "update_watches"):
            return await asyncio.to_thread(self._handle_reload_rpc, method, params)
        elif method == "stop":
            if folders and hasattr(self.daemon, "stop_watching_folders"):
                stop_res = await asyncio.to_thread(
                    self.daemon.stop_watching_folders, folders
                )
                self.refresh_pid_file()
                if not stop_res.get("should_stop_daemon", False):
                    return stop_res
            loop = asyncio.get_running_loop()
            loop.call_soon(self._trigger_daemon_stop)
            return {"status": "stopping", "message": "Daemon shutting down"}
        else:
            raise ValueError(f"Method '{method}' not found")

    def _handle_reload_rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Reload daemon settings and dynamically synchronize watched folders."""
        explicit_folders = params.get("watched_folders")
        if explicit_folders is None and method == "update_watches":
            explicit_folders = self._extract_folders_param(params)
            if explicit_folders is None:
                raise ValueError("Missing required 'folders' parameter")

        add_folders = params.get("add_folders")
        remove_folders = params.get("remove_folders")

        if hasattr(self.daemon, "reload_watched_folders"):
            res = self.daemon.reload_watched_folders(
                watched_folders=explicit_folders,
                add_folders=add_folders,
                remove_folders=remove_folders,
            )
            self.refresh_pid_file()
            return res

        if explicit_folders is not None and hasattr(
            self.daemon, "update_watched_folders"
        ):
            res = self.daemon.update_watched_folders(explicit_folders)
            self.refresh_pid_file()
            return res

        return {
            "status": "reloaded",
            "watched_folders": self._watched_folders_list(),
        }

    async def _handle_drop_rpc(self, params: dict[str, Any]) -> dict[str, Any]:
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

        from app.core.pipeline import validate_drop_paths

        ignore_predicate = (
            self.daemon.should_ignore_path
            if hasattr(self.daemon, "should_ignore_path")
            else None
        )
        validated_paths, issues = validate_drop_paths(
            path_list,
            settings=getattr(self.daemon, "settings", None),
            ignore_predicate=ignore_predicate,
        )

        invalid_reasons = []
        for issue in issues:
            if issue.reason == "missing":
                invalid_reasons.append(f"Path does not exist: {issue.clean_path}")
            elif issue.reason == "ignored":
                invalid_reasons.append(f"Ignored path pattern: {issue.clean_path}")
            elif issue.reason == "protected":
                invalid_reasons.append(f"Protected path blocked: {issue.clean_path}")

        if not validated_paths:
            reasons_str = (
                "; ".join(invalid_reasons)
                if invalid_reasons
                else "No valid file paths supplied."
            )
            raise ValueError(f"Path validation failed: {reasons_str}")

        daemon_dict = getattr(self.daemon, "__dict__", {})
        if (
            hasattr(self.daemon, "_execute_dropped_items")
            and "process_dropped_items" not in daemon_dict
        ):
            res = await asyncio.to_thread(
                self.daemon._execute_dropped_items,
                validated_paths,
                dry_run=dry_run,
                dest_dir=dest_dir,
            )
            res["validated_paths"] = validated_paths
            return res
        if hasattr(self.daemon, "process_dropped_items"):
            res = await asyncio.to_thread(
                self.daemon.process_dropped_items,
                validated_paths,
                dry_run=dry_run,
                dest_dir=dest_dir,
            )
            res["validated_paths"] = validated_paths
            return res
        return {
            "status": "success",
            "processed_count": len(validated_paths),
            "validated_paths": validated_paths,
            "message": f"Successfully forwarded {len(validated_paths)} dropped items to triage pipeline",
        }

    def _trigger_daemon_stop(self) -> None:
        """Asynchronously trigger daemon stop in a background thread."""
        import threading

        t = threading.Thread(target=self.daemon.stop, daemon=True)
        t.start()

    def stop(self) -> None:
        """Stop the IPC server and remove socket and PID lockfiles."""
        self.is_running = False
        self.auth_token = None
        if self.server:
            try:
                self.server.close()
            except Exception:
                logger.debug("Failed to close IPC server", exc_info=True)
            self.server = None

        if self.socket_path and not self.socket_path.startswith("tcp://"):
            if os.path.exists(self.socket_path):
                try:
                    os.unlink(self.socket_path)
                except OSError as e:
                    logger.warning(
                        f"Failed to remove socket file {self.socket_path}: {e}"
                    )

        if os.path.exists(self.pid_path):
            try:
                os.unlink(self.pid_path)
            except OSError as e:
                logger.warning(f"Failed to remove PID lockfile {self.pid_path}: {e}")

        logger.info("IPC server stopped and lockfiles cleaned.")


class DaemonIPCClient:
    """Client for connecting to the shared local daemon IPC server over sockets."""

    def __init__(
        self,
        base_dir: Optional[str | Path] = None,
        control_dir: Optional[str | Path] = None,
    ) -> None:
        self.base_dir = os.path.abspath(str(base_dir)) if base_dir is not None else None
        self.control_dir = (
            os.path.abspath(str(control_dir)) if control_dir is not None else None
        )

    def _resolve_pid_info(self) -> Optional[dict[str, Any]]:
        if self.control_dir is not None:
            return _read_pid_file_at(os.path.join(self.control_dir, PID_FILE_NAME))
        return read_pid_file(self.base_dir)

    async def send_command(
        self,
        method: str,
        params: Optional[dict[str, Any]] = None,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        """Send a JSON-RPC method request to the daemon IPC server and return result dict."""
        pid_info = self._resolve_pid_info()
        target_label = self.control_dir or self.base_dir or get_ipc_dir()
        if not pid_info:
            raise RuntimeError(f"No active daemon lockfile found in '{target_label}'.")

        pid = pid_info.get("pid")
        socket_path = pid_info.get("socket_path")
        auth_token = pid_info.get("auth_token") or pid_info.get("token")

        if not isinstance(pid, int) or not is_process_alive(pid):
            raise RuntimeError(
                f"Daemon process (PID {pid}) is not running for '{target_label}'."
            )

        if not socket_path or not isinstance(socket_path, str):
            raise RuntimeError("Invalid socket path in daemon lockfile.")

        payload_params = dict(params) if isinstance(params, dict) else (params or {})
        explicit_token = None
        if isinstance(payload_params, dict):
            explicit_token = payload_params.get("auth_token") or payload_params.get(
                "token"
            )
            if not explicit_token and auth_token:
                payload_params["auth_token"] = auth_token

        req_payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": payload_params,
        }
        effective_token = explicit_token if explicit_token is not None else auth_token
        if effective_token:
            req_payload["auth_token"] = effective_token

        req_bytes = (json.dumps(req_payload) + "\n").encode("utf-8")

        async def _communicate() -> dict[str, Any]:
            if socket_path.startswith("tcp://"):
                host, port_str = socket_path[6:].split(":")
                reader, writer = await asyncio.open_connection(host, int(port_str))
            else:
                if not os.path.exists(socket_path):
                    raise RuntimeError(f"Socket file '{socket_path}' does not exist.")
                open_unix_connection = getattr(asyncio, "open_unix_connection", None)
                if open_unix_connection is None:
                    raise RuntimeError(
                        f"Unix domain sockets are not supported on this platform: '{socket_path}'"
                    )
                reader, writer = await open_unix_connection(socket_path)

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
                    logger.debug("Failed to close IPC client connection", exc_info=True)

        return await asyncio.wait_for(_communicate(), timeout=timeout)
