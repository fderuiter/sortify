"""User-space precompiled bootstrapping module.

Identifies host platform, resolves database encryption binaries locally,
dynamically registers search paths, and verifies database encryption.
"""

import importlib.util
import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def get_bootstrap_bin_dir() -> Path:
    """Get the writable user-space binaries directory."""
    from app.config import get_app_dir

    app_dir = get_app_dir()
    bin_dir = app_dir / "binaries"
    return bin_dir


def check_internet_connection(timeout: float = 2.0) -> bool:
    """Check if we have an active internet connection by trying to reach a reliable host."""
    import urllib.request

    try:
        urllib.request.urlopen("https://www.google.com", timeout=timeout)
        return True
    except Exception:
        return False


def verify_sqlcipher_encryption() -> bool:
    """Run automated verification check to confirm database encryption is active and error-free."""
    try:
        from sqlcipher3 import dbapi2 as sqlite3

        # Test connection with an in-memory encrypted database
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA key = 'test_bootstrap_key'")
            cursor.execute("CREATE TABLE test_encrypt (val TEXT)")
            cursor.execute("INSERT INTO test_encrypt VALUES ('secure_data')")
            cursor.execute("SELECT val FROM test_encrypt")
            row = cursor.fetchone()
            if not row or row[0] != "secure_data":
                return False

            # Check cipher version is active
            cursor.execute("PRAGMA cipher_version;")
            ver = cursor.fetchone()
            if not ver or not ver[0]:
                return False

            return True
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"Pre-flight database encryption verification failed: {e}")
        return False


def validate_local_binaries(local_dir: Path) -> bool:
    """Validate that local directory contains compatible binaries for the host platform and architecture."""
    import platform
    system = sys.platform
    machine = platform.machine().lower()

    is_64bit = sys.maxsize > 2**32

    # Search recursively inside local_dir for compiled extensions
    binaries = []
    for root, _, files in os.walk(local_dir):
        for f in files:
            if f.endswith((".so", ".pyd", ".dll", ".dylib")):
                binaries.append((Path(root) / f, f.lower()))

    if not binaries:
        logger.error(f"No native binary files found in local directory {local_dir}")
        return False

    compat_found = False
    for path, name in binaries:
        # Check OS compatibility
        os_compat = False
        if system == "win32":
            if name.endswith((".pyd", ".dll")):
                os_compat = True
        elif system == "darwin":
            if name.endswith((".so", ".dylib")):
                os_compat = True
        else:  # linux or others
            if name.endswith(".so"):
                os_compat = True

        if not os_compat:
            continue

        # Check architecture/python version tags if present in name
        has_other_py_ver = False
        for major in [3]:
            for minor in range(5, 15):
                if minor == sys.version_info.minor:
                    continue
                tag1 = f"cp{major}{minor}"
                tag2 = f"cpython-{major}{minor}"
                if tag1 in name or tag2 in name:
                    has_other_py_ver = True
                    break
        if has_other_py_ver:
            continue

        arch_compat = True
        if "x86_64" in name or "amd64" in name:
            if not (machine in ("x86_64", "amd64", "x64") and is_64bit):
                arch_compat = False
        elif "arm64" in name or "aarch64" in name:
            if not ("arm" in machine or "aarch" in machine):
                arch_compat = False
        elif "win32" in name and "amd64" not in name:
            if is_64bit or system != "win32":
                arch_compat = False

        if arch_compat:
            compat_found = True
            logger.info(f"Validated compatible local binary: {path} for platform {system} ({machine})")
            break

    if not compat_found:
        logger.error(f"None of the found native binaries in {local_dir} are compatible with the current platform {system} ({machine})")
        return False

    return True


def inject_bootstrap_paths():
    """Dynamically modify search paths to include the user-space binaries folder."""
    bin_dir = get_bootstrap_bin_dir()

    if bin_dir.exists():
        bin_dir_str = str(bin_dir)
        if bin_dir_str not in sys.path:
            sys.path.insert(0, bin_dir_str)

        if sys.platform == "win32":
            try:
                os.add_dll_directory(str(bin_dir))
            except Exception:
                pass

            paths_to_add = [str(bin_dir)]
            for root, dirs, _ in os.walk(str(bin_dir)):
                for d in dirs:
                    subdir_path = os.path.abspath(os.path.join(root, d))
                    paths_to_add.append(subdir_path)
                    try:
                        os.add_dll_directory(subdir_path)
                    except Exception:
                        pass

            # Also add standard active Python/virtualenv DLL paths to guarantee resolution in development/CI
            search_dirs = [
                sys.prefix,
                sys.base_prefix,
                os.path.dirname(sys.executable),
            ]
            for sd in list(search_dirs):
                if sd:
                    for sub in ["Library/bin", "DLLs", "Scripts"]:
                        sub_dir = os.path.join(sd, sub.replace("/", os.sep))
                        if os.path.isdir(sub_dir) and sub_dir not in search_dirs:
                            search_dirs.append(sub_dir)
            for p in sys.path:
                if p and os.path.isdir(p) and p not in search_dirs:
                    search_dirs.append(p)

            for s_dir in search_dirs:
                if s_dir and os.path.isdir(s_dir):
                    paths_to_add.append(s_dir)
                    try:
                        os.add_dll_directory(s_dir)
                    except Exception:
                        pass

            unique_paths = []
            for p in paths_to_add:
                if p not in unique_paths and os.path.isdir(p):
                    unique_paths.append(p)

            os.environ["PATH"] = ";".join(unique_paths) + ";" + os.environ.get("PATH", "")


def bootstrap_binaries(force_download: bool = False) -> bool:
    """Identify host platform, resolve database encryption binaries locally, and register paths."""
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                dirs_to_add = [
                    meipass,
                    os.path.join(meipass, "sqlcipher3"),
                    os.path.join(meipass, "_internal"),
                    os.path.join(meipass, "_internal", "sqlcipher3"),
                ]
                for p in dirs_to_add:
                    if os.path.isdir(p):
                        try:
                            os.add_dll_directory(p)
                        except Exception:
                            pass
                        if p not in os.environ.get("PATH", ""):
                            os.environ["PATH"] = p + ";" + os.environ.get("PATH", "")
        if verify_sqlcipher_encryption():
            logger.info("SQLCipher verified active inside frozen PyInstaller environment.")
            return True
        else:
            raise RuntimeError(
                "SQLCipher database encryption failed to verify inside frozen package environment."
            )

    bin_dir = get_bootstrap_bin_dir()
    sqlcipher3_path = bin_dir / "sqlcipher3"

    # 0. Check if sqlcipher3 is already fully functional in the host environment without bootstrapping
    # Since we are offline-first, if force_download is True, we treat it as forcing re-copy of local files.
    if not force_download:
        if verify_sqlcipher_encryption():
            logger.info(
                "Host environment has fully functional SQLCipher active. Skipping bootstrapping."
            )
            return True

    # 1. Check if native binaries are already present (Subsequent launches bypass)
    if sqlcipher3_path.exists() and not force_download:
        inject_bootstrap_paths()
        if validate_local_binaries(sqlcipher3_path) and verify_sqlcipher_encryption():
            logger.info(
                "Subsequent launch: cached native binaries found, validated, and verified. Bypassing copy phase."
            )
            return True
        else:
            logger.warning(
                "Cached native binaries failed compatibility or verification. Re-bootstrapping..."
            )

    # Ensure parent directories exist (always in user-space, avoiding read-only app folders)
    bin_dir.mkdir(parents=True, exist_ok=True)

    system_platform = sys.platform
    logger.info(f"Identifying host platform: {system_platform}")

    # 2. Resolve required database encryption binary dependencies exclusively from local installation directories
    logger.info("Resolving database encryption binary dependencies exclusively from local installation directories...")
    spec = importlib.util.find_spec("sqlcipher3")
    if spec and spec.submodule_search_locations:
        local_sqlcipher3_dir = Path(spec.submodule_search_locations[0])

        # Validate local binary compatibility and architecture-specific paths
        if not validate_local_binaries(local_sqlcipher3_dir):
            raise RuntimeError(
                f"SQLCipher local native library at {local_sqlcipher3_dir} is incompatible with current platform/architecture."
            )

        if sqlcipher3_path.exists():
            if sqlcipher3_path.is_dir():
                shutil.rmtree(sqlcipher3_path)
            else:
                sqlcipher3_path.unlink()

        try:
            shutil.copytree(local_sqlcipher3_dir, sqlcipher3_path)
            logger.info(
                f"Successfully copied cached local native libraries from {local_sqlcipher3_dir} to {sqlcipher3_path}"
            )
        except Exception as copy_err:
            raise RuntimeError(
                f"Failed to copy local database encryption binaries: {copy_err}"
            ) from copy_err

        # On Windows, also find and copy dependent OpenSSL/SQLCipher DLLs from active environment to user-space
        if sys.platform == "win32":
            search_dirs = [
                sys.prefix,
                sys.base_prefix,
                os.path.dirname(sys.executable),
            ]
            for sd in list(search_dirs):
                if sd:
                    for sub in ["Library/bin", "DLLs", "Scripts"]:
                        sub_dir = os.path.join(sd, sub.replace("/", os.sep))
                        if os.path.isdir(sub_dir) and sub_dir not in search_dirs:
                            search_dirs.append(sub_dir)
            for p in sys.path:
                if p and os.path.isdir(p) and p not in search_dirs:
                    search_dirs.append(p)

            dll_patterns = ["libcrypto", "libssl", "sqlcipher", "libsqlcipher"]
            found_dlls = set()
            for s_dir in search_dirs:
                if not s_dir or not os.path.isdir(s_dir):
                    continue
                try:
                    for entry in os.scandir(s_dir):
                        if entry.is_file() and entry.name.lower().endswith(".dll"):
                            name_lower = entry.name.lower()
                            if any(pat in name_lower for pat in dll_patterns):
                                dll_path = os.path.abspath(entry.path)
                                if dll_path not in found_dlls:
                                    found_dlls.add(dll_path)
                                    logger.info(f"Copying Windows dependency DLL: {dll_path}")
                                    try:
                                        shutil.copy2(dll_path, bin_dir)
                                        shutil.copy2(dll_path, sqlcipher3_path)
                                    except Exception as copy_dll_err:
                                        logger.warning(f"Failed to copy DLL {dll_path}: {copy_dll_err}")
                except Exception as scan_err:
                    logger.debug(f"Could not scan directory {s_dir} for DLLs: {scan_err}")
    else:
        raise RuntimeError(
            "SQLCipher local native library/wheels are missing. Please install offline packages manually."
        )

    # 3. Dynamically inject the paths
    inject_bootstrap_paths()

    # Clear sys.modules of sqlcipher3 and _sqlite3 to force reload from the newly injected paths
    if "sqlcipher3" in sys.modules or "_sqlite3" in sys.modules:
        for k in list(sys.modules.keys()):
            if k in ("sqlcipher3", "_sqlite3") or k.startswith("sqlcipher3."):
                sys.modules.pop(k, None)

    # 4. Execute pre-flight verification
    if verify_sqlcipher_encryption():
        logger.info("Startup pre-flight database encryption verification successful!")
        return True
    else:
        raise RuntimeError(
            "Startup verification failed: pre-flight database encryption is not active or error-free."
        )
