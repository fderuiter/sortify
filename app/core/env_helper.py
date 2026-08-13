"""Centralized helper for child process environment sanitization and safe spawning.

Ensures that child processes spawned on Windows run without infinite subprocess
loops, use a clean environment stripped of PyInstaller reference variables,
can resolve decoupled modules from the local cache, and do not pop up terminal windows.
"""

import os
import subprocess
import sys

from app.config import get_app_dir

MACOS_SANDBOX_PROFILE = (
    "(version 1)\n"
    "(allow default)\n"
    "(deny network-outbound)\n"
    '(allow network-outbound (remote ip "127.0.0.1"))\n'
    '(allow network-outbound (remote ip "::1"))\n'
)


def check_linux_sandbox_support() -> bool:
    """Verify that unshare is available and can run successfully at startup."""
    if sys.platform != "linux":
        return False
    try:
        res = subprocess.run(
            ["unshare", "-n", "-r", "true"], capture_output=True, text=True, timeout=2
        )
        return res.returncode == 0
    except Exception:
        return False


def check_macos_sandbox_support() -> bool:
    """Verify that sandbox-exec is available and works with our profile at startup."""
    if sys.platform != "darwin":
        return False
    try:
        res = subprocess.run(
            ["sandbox-exec", "-p", MACOS_SANDBOX_PROFILE, "true"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return res.returncode == 0
    except Exception:
        return False


def check_windows_sandbox_support() -> bool:
    """Verify that required Win32 APIs for restricted tokens are available."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        advapi32 = ctypes.windll.advapi32
        _ = advapi32.CreateRestrictedToken
        _ = advapi32.ConvertStringSidToSidW
        _ = advapi32.DuplicateTokenEx
        _ = advapi32.CreateProcessAsUserW
        return True
    except Exception:
        return False


# OS Detection & Capability Check at Startup
SANDBOX_SUPPORTED = False

if sys.platform == "linux":
    SANDBOX_SUPPORTED = check_linux_sandbox_support()
elif sys.platform == "darwin":
    SANDBOX_SUPPORTED = check_macos_sandbox_support()
elif sys.platform == "win32":
    SANDBOX_SUPPORTED = check_windows_sandbox_support()


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("Sid", wintypes.LPVOID),
            ("Attributes", wintypes.DWORD),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    def create_unicode_env_block(env_dict):
        if not env_dict:
            return None
        block_elements = []
        for k, v in env_dict.items():
            block_elements.append(f"{k}={v}\0")
        block_str = "".join(block_elements) + "\0"
        return ctypes.create_unicode_buffer(block_str)

    class RestrictedPopen(subprocess.Popen):
        """A subclass of Popen that executes child processes under a restricted token on Windows."""

        def _execute_child(
            self,
            args,
            executable,
            preexec_fn,
            close_fds,
            pass_fds,
            cwd,
            env,
            startupinfo,
            creationflags,
            shell,
            p2cread,
            p2cwrite,
            c2pread,
            c2pwrite,
            errread,
            errwrite,
            unused_restore_signals=None,
            unused_gid=None,
            unused_gids=None,
            unused_uid=None,
            unused_umask=None,
            unused_start_new_session=None,
            *extra_args,
            **extra_kwargs,
        ):

            advapi32 = ctypes.windll.advapi32
            kernel32 = ctypes.windll.kernel32

            # Configure ctypes function signatures
            advapi32.ConvertStringSidToSidW.argtypes = [
                wintypes.LPCWSTR,
                ctypes.POINTER(wintypes.LPVOID),
            ]
            advapi32.ConvertStringSidToSidW.restype = wintypes.BOOL

            advapi32.CreateRestrictedToken.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(SID_AND_ATTRIBUTES),
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(SID_AND_ATTRIBUTES),
                ctypes.POINTER(wintypes.HANDLE),
            ]
            advapi32.CreateRestrictedToken.restype = wintypes.BOOL

            advapi32.DuplicateTokenEx.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPVOID,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.POINTER(wintypes.HANDLE),
            ]
            advapi32.DuplicateTokenEx.restype = wintypes.BOOL

            advapi32.CreateProcessAsUserW.argtypes = [
                wintypes.HANDLE,
                wintypes.LPCWSTR,
                wintypes.LPWSTR,
                wintypes.LPVOID,
                wintypes.LPVOID,
                wintypes.BOOL,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.LPCWSTR,
                ctypes.POINTER(STARTUPINFOW),
                ctypes.POINTER(PROCESS_INFORMATION),
            ]
            advapi32.CreateProcessAsUserW.restype = wintypes.BOOL

            # 1. Open current process token
            current_process = kernel32.GetCurrentProcess()
            token_handle = wintypes.HANDLE()
            # TOKEN_DUPLICATE (0x0002) | TOKEN_QUERY (0x0008)
            if not advapi32.OpenProcessToken(
                current_process, 0x0002 | 0x0008, ctypes.byref(token_handle)
            ):
                raise ctypes.WinError()

            # 2. Create restricting SID for S-1-5-12
            p_sid = wintypes.LPVOID()
            if not advapi32.ConvertStringSidToSidW("S-1-5-12", ctypes.byref(p_sid)):
                kernel32.CloseHandle(token_handle)
                raise ctypes.WinError()

            restricting_sid = SID_AND_ATTRIBUTES()
            restricting_sid.Sid = p_sid
            restricting_sid.Attributes = 0

            # 3. Call CreateRestrictedToken with DISABLE_MAX_PRIVILEGE (0x1)
            restricted_token = wintypes.HANDLE()
            if not advapi32.CreateRestrictedToken(
                token_handle,
                1,  # DISABLE_MAX_PRIVILEGE
                0,
                None,
                0,
                None,
                1,
                ctypes.byref(restricting_sid),
                ctypes.byref(restricted_token),
            ):
                kernel32.CloseHandle(token_handle)
                kernel32.LocalFree(p_sid)
                raise ctypes.WinError()

            # Duplicate the token as a primary token for CreateProcessAsUserW
            primary_token = wintypes.HANDLE()
            # SecurityImpersonation = 2, TokenPrimary = 1
            if not advapi32.DuplicateTokenEx(
                restricted_token,
                0xF00FF,  # TOKEN_ALL_ACCESS
                None,
                2,
                1,
                ctypes.byref(primary_token),
            ):
                kernel32.CloseHandle(restricted_token)
                kernel32.CloseHandle(token_handle)
                kernel32.LocalFree(p_sid)
                raise ctypes.WinError()

            # Clean up temporary handles
            kernel32.CloseHandle(restricted_token)
            kernel32.CloseHandle(token_handle)
            kernel32.LocalFree(p_sid)

            # 4. Build STARTUPINFOW
            si = STARTUPINFOW()
            si.cb = ctypes.sizeof(STARTUPINFOW)
            si.dwFlags = 0
            if startupinfo:
                si.dwFlags = startupinfo.dwFlags
                si.wShowWindow = startupinfo.wShowWindow

            # Redirect handles
            if p2cread is not None and p2cread != -1:
                si.hStdInput = int(p2cread)
                si.dwFlags |= 0x00000100  # STARTF_USESTDHANDLES
            elif startupinfo and startupinfo.hStdInput is not None:
                si.hStdInput = int(startupinfo.hStdInput)
                si.dwFlags |= 0x00000100

            if c2pwrite is not None and c2pwrite != -1:
                si.hStdOutput = int(c2pwrite)
                si.dwFlags |= 0x00000100
            elif startupinfo and startupinfo.hStdOutput is not None:
                si.hStdOutput = int(startupinfo.hStdOutput)
                si.dwFlags |= 0x00000100

            if errwrite is not None and errwrite != -1:
                si.hStdError = int(errwrite)
                si.dwFlags |= 0x00000100
            elif startupinfo and startupinfo.hStdError is not None:
                si.hStdError = int(startupinfo.hStdError)
                si.dwFlags |= 0x00000100

            inherit_handles = True if (si.dwFlags & 0x00000100) else False

            # Convert cmd line
            if isinstance(args, list):
                cmd_line = subprocess.list2cmdline(args)
            else:
                cmd_line = args

            # 5. Convert env and pass CREATE_UNICODE_ENVIRONMENT
            env_block = create_unicode_env_block(env)
            creationflags |= 0x00000400  # CREATE_UNICODE_ENVIRONMENT

            # 6. Execute CreateProcessAsUserW
            pi = PROCESS_INFORMATION()
            success = advapi32.CreateProcessAsUserW(
                primary_token,
                executable,
                cmd_line,
                None,
                None,
                inherit_handles,
                creationflags,
                env_block,
                cwd,
                ctypes.byref(si),
                ctypes.byref(pi),
            )

            # Close primary token as we no longer need it to spawn
            kernel32.CloseHandle(primary_token)

            if not success:
                raise ctypes.WinError()

            # 7. Handle process handle extraction, close thread handle, and close parent/child pipes
            try:
                import _winapi

                if hasattr(_winapi, "handle"):
                    self._handle = _winapi.handle(pi.hProcess)
                else:
                    self._handle = pi.hProcess
            except Exception:
                self._handle = pi.hProcess

            self.pid = pi.dwProcessId
            if pi.hThread:
                kernel32.CloseHandle(pi.hThread)


else:

    class RestrictedPopen(object):
        pass


def get_cleaned_env(env: dict = None) -> dict:
    """Return a copy of the environment dictionary with PyInstaller-specific variables removed.

    This also explicitly injects the local cache directory into PYTHONPATH.
    """
    if env is None:
        env = os.environ.copy()
    else:
        env = env.copy()

    # Remove package-specific variables (such as _MEIPASS and PYTHONPATH)
    env.pop("_MEIPASS", None)
    env.pop("PYTHONPATH", None)

    # Explicitly inject the local cache directory into the search path (PYTHONPATH)
    cache_dir = get_app_dir() / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    env["PYTHONPATH"] = str(cache_dir)

    return env


def get_subprocess_startupinfo():
    """Return a STARTUPINFO object that hides the console window on Windows."""
    if sys.platform == "win32":
        if hasattr(subprocess, "STARTUPINFO"):
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 0  # SW_HIDE
            return startupinfo
    return None


def spawn_background_process(cmd, sandbox: bool = True, **kwargs):
    """Spawn a background process asynchronously with platform-specific sandboxing if sandbox=True."""
    if sandbox:
        if not SANDBOX_SUPPORTED:
            raise PermissionError(
                "Subprocess sandboxing is enabled but not supported or failed to initialize on this platform."
            )

        if sys.platform == "linux":
            cmd = [
                "unshare",
                "-n",
                "-r",
                "sh",
                "-c",
                'ip link set lo up && exec "$@"',
                "--",
            ] + list(cmd)
        elif sys.platform == "darwin":
            cmd = ["sandbox-exec", "-p", MACOS_SANDBOX_PROFILE] + list(cmd)
        elif sys.platform == "win32":
            if "env" not in kwargs:
                kwargs["env"] = get_cleaned_env()
            else:
                kwargs["env"] = get_cleaned_env(kwargs["env"])

            if "startupinfo" not in kwargs:
                kwargs["startupinfo"] = get_subprocess_startupinfo()

            return RestrictedPopen(cmd, **kwargs)

    # Un-sandboxed execution (or fallback / non-win32 non-sandbox)
    if "env" not in kwargs:
        kwargs["env"] = get_cleaned_env()
    else:
        kwargs["env"] = get_cleaned_env(kwargs["env"])

    if "startupinfo" not in kwargs and sys.platform == "win32":
        kwargs["startupinfo"] = get_subprocess_startupinfo()

    return subprocess.Popen(cmd, **kwargs)


def run_background_process(cmd, sandbox: bool = True, **kwargs):
    """Run a background process synchronously with platform-specific sandboxing if sandbox=True."""
    if sandbox:
        if not SANDBOX_SUPPORTED:
            raise PermissionError(
                "Subprocess sandboxing is enabled but not supported or failed to initialize on this platform."
            )

        if sys.platform == "linux":
            cmd = [
                "unshare",
                "-n",
                "-r",
                "sh",
                "-c",
                'ip link set lo up && exec "$@"',
                "--",
            ] + list(cmd)
        elif sys.platform == "darwin":
            cmd = ["sandbox-exec", "-p", MACOS_SANDBOX_PROFILE] + list(cmd)
        elif sys.platform == "win32":
            if "env" not in kwargs:
                kwargs["env"] = get_cleaned_env()
            else:
                kwargs["env"] = get_cleaned_env(kwargs["env"])

            if "startupinfo" not in kwargs:
                kwargs["startupinfo"] = get_subprocess_startupinfo()

            orig_popen = subprocess.Popen
            try:
                subprocess.Popen = RestrictedPopen
                return subprocess.run(cmd, **kwargs)
            finally:
                subprocess.Popen = orig_popen

    # Un-sandboxed execution
    if "env" not in kwargs:
        kwargs["env"] = get_cleaned_env()
    else:
        kwargs["env"] = get_cleaned_env(kwargs["env"])

    if "startupinfo" not in kwargs and sys.platform == "win32":
        kwargs["startupinfo"] = get_subprocess_startupinfo()

    return subprocess.run(cmd, **kwargs)


def is_cuda_available() -> bool:
    """Check if PyTorch CUDA capability is available."""
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def is_mps_available() -> bool:
    """Check if PyTorch MPS capability is available."""
    try:
        import torch
        return hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
    except Exception:
        return False

