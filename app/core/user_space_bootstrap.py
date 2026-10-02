"""User-space precompiled bootstrapping module.

Identifies host platform, downloads/resolves precompiled native binaries to
a writable folder in the user's home directory, dynamically registers search
paths, and verifies database encryption.
"""

import logging
import os
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

        from app.core.crypto import CryptoManager

        # Test connection with an in-memory encrypted database
        conn = sqlite3.connect(":memory:")
        try:
            cursor = conn.cursor()
            bootstrap_key = CryptoManager.generate_bootstrap_key()
            cursor.execute(f"PRAGMA key = '{bootstrap_key}'")
            cursor.execute("CREATE TABLE test_encrypt (val TEXT)")
            cursor.execute("INSERT INTO test_encrypt VALUES ('secure_data')")
            cursor.execute("SELECT val FROM test_encrypt")
            row = cursor.fetchone()
            if not row or row[0] != "secure_data":
                raise RuntimeError("Pre-flight database read/write validation failed.")

            # Check cipher version is active
            cursor.execute("PRAGMA cipher_version;")
            ver = cursor.fetchone()
            if not ver or not ver[0]:
                raise RuntimeError("PRAGMA cipher_version is empty.")

            return True
        finally:
            conn.close()
    except Exception as e:
        logger.info(
            f"Pre-flight database encryption verification check returned non-active: {e}"
        )
        return False


def _resolve_platform_driver_paths() -> list:
    """Discover and register potential SQLCipher dynamic library search paths across Linux, macOS, and Windows."""
    import importlib.util

    dirs_to_add: list = []

    for pkg_name in ("sqlcipher3", "pysqlcipher3"):
        try:
            spec = importlib.util.find_spec(pkg_name)
            if spec and spec.submodule_search_locations:
                for loc in spec.submodule_search_locations:
                    if os.path.isdir(loc) and loc not in dirs_to_add:
                        dirs_to_add.append(loc)
        except Exception:
            pass

    venv_dirs = []
    v_env = os.environ.get("VIRTUAL_ENV")
    if v_env:
        venv_dirs.append(v_env)
    if sys.prefix and sys.prefix not in venv_dirs:
        venv_dirs.append(sys.prefix)

    py_ver = f"python{sys.version_info.major}.{sys.version_info.minor}"

    for vd in venv_dirs:
        sub_dirs = [
            ".",
            "Library/bin",
            "Scripts",
            "DLLs",
            "lib",
            "lib64",
            f"lib/{py_ver}/site-packages/sqlcipher3",
            f"lib/{py_ver}/site-packages/pysqlcipher3",
            f"lib/{py_ver}/site-packages/sqlcipher3.libs",
            "Lib/site-packages/sqlcipher3",
            "Lib/site-packages/pysqlcipher3",
            "Lib/site-packages/sqlcipher3.libs",
        ]
        for sub in sub_dirs:
            try:
                p = os.path.abspath(os.path.join(vd, sub))
                if os.path.isdir(p) and p not in dirs_to_add:
                    dirs_to_add.append(p)
            except Exception:
                pass

    system_dirs: list = []
    if sys.platform == "darwin":
        system_dirs = [
            "/opt/homebrew/lib",
            "/opt/homebrew/opt/sqlcipher/lib",
            "/usr/local/lib",
            "/usr/local/opt/sqlcipher/lib",
        ]
    elif sys.platform.startswith("linux"):
        system_dirs = [
            "/usr/lib",
            "/usr/local/lib",
            "/usr/lib/x86_64-linux-gnu",
            "/usr/lib/aarch64-linux-gnu",
        ]
    elif sys.platform == "win32":
        system_dirs = [
            "C:\\Program Files\\OpenSSL-Win64\\bin",
            "C:\\Program Files\\OpenSSL\\bin",
            "C:\\Program Files\\OpenSSL-Win64",
            "C:\\Program Files\\OpenSSL",
            "C:\\OpenSSL-Win64\\bin",
            "C:\\OpenSSL-Win64",
            "C:\\Program Files\\Common Files\\SSL",
        ]

    for sd in system_dirs:
        try:
            if os.path.isdir(sd) and sd not in dirs_to_add:
                dirs_to_add.append(sd)
        except Exception:
            pass

    for d in os.environ.get("PATH", "").split(os.pathsep):
        cleaned = d.strip().strip('"')
        if cleaned:
            try:
                cleaned_lower = cleaned.lower()
                is_candidate_dir = any(
                    k in cleaned_lower
                    for k in (
                        "openssl",
                        "ssl",
                        "sqlcipher",
                        "sqlite",
                        "git",
                        "python",
                        "venv",
                        "site-packages",
                    )
                )
                p_abs = os.path.abspath(cleaned).lower().replace("\\", "/")
                is_sys_dir = (
                    "system32" in p_abs
                    or "syswow64" in p_abs
                    or p_abs == "c:/windows"
                    or p_abs.startswith("c:/windows/")
                )
                if is_candidate_dir and not is_sys_dir and os.path.isdir(cleaned):
                    if cleaned not in dirs_to_add:
                        dirs_to_add.append(cleaned)
            except Exception:
                pass

    if sys.platform == "win32":
        for p in dirs_to_add:
            try:
                os.add_dll_directory(p)
            except Exception:
                pass
        current_path_dirs = [
            d.strip().strip('"')
            for d in os.environ.get("PATH", "").replace(os.pathsep, ";").split(";")
            if d.strip()
        ]
        current_path_dirs_normalized = set()
        for d in current_path_dirs:
            try:
                current_path_dirs_normalized.add(os.path.abspath(d).lower())
            except Exception:
                pass
        new_path_dirs = []
        for p in dirs_to_add:
            try:
                abs_p = os.path.abspath(p)
                if (
                    abs_p.lower() not in current_path_dirs_normalized
                    and abs_p.lower() not in [np.lower() for np in new_path_dirs]
                ):
                    new_path_dirs.append(abs_p)
            except Exception:
                pass
        if new_path_dirs:
            os.environ["PATH"] = (
                ";".join(new_path_dirs) + ";" + os.environ.get("PATH", "")
            )
    elif sys.platform == "darwin":
        current_dyld = os.environ.get("DYLD_LIBRARY_PATH", "")
        dyld_dirs = set(current_dyld.split(":"))
        new_dyld = [p for p in dirs_to_add if p not in dyld_dirs]
        if new_dyld:
            os.environ["DYLD_LIBRARY_PATH"] = ":".join(new_dyld) + (
                ":" + current_dyld if current_dyld else ""
            )
            current_fallback = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
            os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(new_dyld) + (
                ":" + current_fallback if current_fallback else ""
            )
    elif sys.platform.startswith("linux"):
        current_ld = os.environ.get("LD_LIBRARY_PATH", "")
        ld_dirs = set(current_ld.split(":"))
        new_ld = [p for p in dirs_to_add if p not in ld_dirs]
        if new_ld:
            os.environ["LD_LIBRARY_PATH"] = ":".join(new_ld) + (
                ":" + current_ld if current_ld else ""
            )

    return dirs_to_add


def inject_bootstrap_paths(platform_binaries_dir: Path = None):
    """Dynamically modify search paths to include the local binaries folder."""
    if platform_binaries_dir is None:
        platform_binaries_dir = get_bootstrap_bin_dir()

    sqlcipher3_path = platform_binaries_dir / "sqlcipher3"

    if platform_binaries_dir.exists():
        platform_binaries_dir_str = str(platform_binaries_dir)
        if platform_binaries_dir_str not in sys.path:
            sys.path.insert(0, platform_binaries_dir_str)

        paths = [platform_binaries_dir_str, str(sqlcipher3_path)]
        if hasattr(sys, "_MEIPASS"):
            paths.append(sys._MEIPASS)
            internal_dir = os.path.join(sys._MEIPASS, "_internal")
            if os.path.isdir(internal_dir):
                paths.append(internal_dir)
                paths.append(os.path.join(internal_dir, "sqlcipher3"))
                paths.append(
                    os.path.join(
                        internal_dir, "app", "binaries", "windows", "sqlcipher3"
                    )
                )
                paths.append(
                    os.path.join(
                        internal_dir, "app", "binaries", "macos", "sqlcipher3"
                    )
                )
                paths.append(
                    os.path.join(
                        internal_dir, "app", "binaries", "linux", "sqlcipher3"
                    )
                )

        if sys.platform == "win32":
            if hasattr(sys, "_MEIPASS"):
                try:
                    os.add_dll_directory(sys._MEIPASS)
                except Exception:
                    pass
                internal_dir = os.path.join(sys._MEIPASS, "_internal")
                if os.path.isdir(internal_dir):
                    try:
                        os.add_dll_directory(internal_dir)
                    except Exception:
                        pass
                    try:
                        os.add_dll_directory(os.path.join(internal_dir, "sqlcipher3"))
                    except Exception:
                        pass
                    try:
                        os.add_dll_directory(
                            os.path.join(
                                internal_dir,
                                "app",
                                "binaries",
                                "windows",
                                "sqlcipher3",
                            )
                        )
                    except Exception:
                        pass
            try:
                os.add_dll_directory(platform_binaries_dir_str)
            except Exception:
                pass
            try:
                os.add_dll_directory(str(sqlcipher3_path))
            except Exception:
                pass

            current_path_dirs = [
                d.strip().strip('"')
                for d in os.environ.get("PATH", "").replace(os.pathsep, ";").split(";")
                if d.strip()
            ]
            current_path_dirs_normalized = set()
            for d in current_path_dirs:
                try:
                    current_path_dirs_normalized.add(os.path.abspath(d).lower())
                except Exception:
                    pass
            new_path_dirs = []
            for p in paths:
                try:
                    abs_p = os.path.abspath(p)
                    if (
                        abs_p.lower() not in current_path_dirs_normalized
                        and abs_p.lower() not in [np.lower() for np in new_path_dirs]
                    ):
                        new_path_dirs.append(abs_p)
                except Exception:
                    pass
            if new_path_dirs:
                os.environ["PATH"] = (
                    ";".join(new_path_dirs) + ";" + os.environ.get("PATH", "")
                )
        elif sys.platform == "darwin":
            current_dyld = os.environ.get("DYLD_LIBRARY_PATH", "")
            dyld_dirs = set(current_dyld.split(":"))
            new_dyld = [p for p in paths if p not in dyld_dirs]
            if new_dyld:
                os.environ["DYLD_LIBRARY_PATH"] = ":".join(new_dyld) + (
                    ":" + current_dyld if current_dyld else ""
                )
                current_fallback = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
                os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = ":".join(new_dyld) + (
                    ":" + current_fallback if current_fallback else ""
                )
        elif sys.platform.startswith("linux"):
            current_ld = os.environ.get("LD_LIBRARY_PATH", "")
            ld_dirs = set(current_ld.split(":"))
            new_ld = [p for p in paths if p not in ld_dirs]
            if new_ld:
                os.environ["LD_LIBRARY_PATH"] = ":".join(new_ld) + (
                    ":" + current_ld if current_ld else ""
                )


def bootstrap_binaries(force_download: bool = False) -> bool:
    """Identify host platform, verify and load local precompiled SQLCipher libraries directly from the installation path."""
    # 0. Check if sqlcipher3 is already fully functional in the host environment without bootstrapping
    if not force_download:
        if sys.platform == "win32" and not hasattr(sys, "_MEIPASS"):
            _resolve_platform_driver_paths()
            if verify_sqlcipher_encryption():
                logger.info(
                    "Host environment has fully functional SQLCipher active. Skipping bootstrapping."
                )
                try:
                    from sqlcipher3 import dbapi2 as sqlite3

                    sys.modules["sqlite3"] = sqlite3
                except Exception:
                    pass
                return True
        elif verify_sqlcipher_encryption():
            logger.info(
                "Host environment has fully functional SQLCipher active. Skipping bootstrapping."
            )
            try:
                from sqlcipher3 import dbapi2 as sqlite3

                sys.modules["sqlite3"] = sqlite3
            except Exception:
                pass
            return True
        elif not hasattr(sys, "_MEIPASS"):
            _resolve_platform_driver_paths()
            if verify_sqlcipher_encryption():
                logger.info(
                    "Host environment has fully functional SQLCipher active after driver path resolution."
                )
                try:
                    from sqlcipher3 import dbapi2 as sqlite3

                    sys.modules["sqlite3"] = sqlite3
                except Exception:
                    pass
                return True

    # 1. Locate the packaged binaries directory (installation path)
    if hasattr(sys, "_MEIPASS"):
        # Support PyInstaller 6+ where data files are bundled in the _internal subdirectory
        internal_bin_root = Path(sys._MEIPASS) / "_internal" / "app" / "binaries"
        if internal_bin_root.exists():
            local_binaries_root = internal_bin_root
        else:
            local_binaries_root = Path(sys._MEIPASS) / "app" / "binaries"
    else:
        local_binaries_root = Path(__file__).resolve().parent.parent / "binaries"

    # 2. Determine current platform
    system_platform = sys.platform
    if system_platform == "win32":
        platform_key = "windows"
    elif system_platform == "darwin":
        platform_key = "macos"
    else:
        platform_key = "linux"

    # 3. Read and verify manifest
    manifest_path = local_binaries_root / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(
            "Startup validation failed: local binaries manifest is missing."
        )

    try:
        import json

        with open(manifest_path, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    except Exception as e:
        raise RuntimeError(
            f"Startup validation failed: manifest could not be read. Error: {e}"
        )

    # 4. Verify all files for this platform are present and unmodified
    platform_binaries_dir = local_binaries_root / platform_key
    if not platform_binaries_dir.exists():
        raise RuntimeError(
            f"Startup validation failed: local precompiled libraries for {platform_key} are missing."
        )

    expected_files = manifest.get(platform_key, {})
    if not expected_files:
        raise RuntimeError(
            f"Startup validation failed: manifest has no entries for platform {platform_key}."
        )

    # Inject paths directly from the installation directory first so DLLs are accessible
    inject_bootstrap_paths(platform_binaries_dir)

    for rel_path_str, expected_hash in expected_files.items():
        file_path = platform_binaries_dir / rel_path_str
        if not file_path.exists():
            if hasattr(sys, "_MEIPASS"):
                # Check candidate locations in PyInstaller bundle
                meipass = Path(sys._MEIPASS)
                rel_base = rel_path_str.replace("sqlcipher3/", "")
                candidates = [
                    meipass / "_internal" / rel_path_str,
                    meipass / rel_path_str,
                    meipass / "_internal" / "sqlcipher3" / rel_base,
                    meipass / "sqlcipher3" / rel_base,
                    meipass / "_internal" / rel_base,
                    meipass / rel_base,
                    meipass / "_internal" / "app" / "binaries" / platform_key / rel_path_str,
                    meipass / "app" / "binaries" / platform_key / rel_path_str,
                ]
                found = False
                for cand in candidates:
                    if cand.exists():
                        file_path = cand
                        found = True
                        break
                if not found:
                    raise RuntimeError(
                        f"Startup validation failed: local packaged binary file {rel_path_str} is missing."
                    )
            else:
                raise RuntimeError(
                    f"Startup validation failed: local packaged binary file {rel_path_str} is missing."
                )

        # Calculate SHA256 of the file
        from app.core.resilient_file_ops import resilient_file_hash

        try:
            is_text_file = file_path.suffix in (".py", ".pyi", ".typed")
            actual_hash = resilient_file_hash(file_path, normalize_text=is_text_file)
        except Exception as e:
            raise RuntimeError(
                f"Startup validation failed: could not verify integrity of {rel_path_str}. Error: {e}"
            )

        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Startup validation failed: local packaged binary file {rel_path_str} has been modified."
            )

    # 6. Clear sys.modules of sqlcipher3, _sqlite3, and sqlite3 to force reload from the newly injected paths
    for k in list(sys.modules.keys()):
        if (
            k in ("sqlcipher3", "_sqlite3", "sqlite3")
            or k.startswith("sqlcipher3.")
            or k.startswith("sqlite3.")
        ):
            sys.modules.pop(k, None)

    # 7. Execute pre-flight verification
    if verify_sqlcipher_encryption():
        logger.info("Startup pre-flight database encryption verification successful!")
        try:
            from sqlcipher3 import dbapi2 as sqlite3

            sys.modules["sqlite3"] = sqlite3
        except Exception:
            pass
        return True
    else:
        raise RuntimeError(
            "Startup verification failed: pre-flight database encryption is not active or error-free."
        )
