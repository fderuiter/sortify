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


def inject_bootstrap_paths(platform_binaries_dir: Path = None):
    """Dynamically modify search paths to include the local binaries folder."""
    if platform_binaries_dir is None:
        platform_binaries_dir = get_bootstrap_bin_dir()

    sqlcipher3_path = platform_binaries_dir / "sqlcipher3"

    if platform_binaries_dir.exists():
        platform_binaries_dir_str = str(platform_binaries_dir)
        if platform_binaries_dir_str not in sys.path:
            sys.path.insert(0, platform_binaries_dir_str)

        if sys.platform == "win32":
            # Add PyInstaller temporary directories to DLL search path if running in a frozen bundle
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
                os.add_dll_directory(platform_binaries_dir_str)
            except Exception:
                pass
            try:
                os.add_dll_directory(str(sqlcipher3_path))
            except Exception:
                pass

            paths = [platform_binaries_dir_str, str(sqlcipher3_path)]
            if hasattr(sys, "_MEIPASS"):
                paths.append(sys._MEIPASS)
                internal_dir = os.path.join(sys._MEIPASS, "_internal")
                if os.path.isdir(internal_dir):
                    paths.append(internal_dir)
                    paths.append(os.path.join(internal_dir, "sqlcipher3"))
            os.environ["PATH"] = ";".join(paths) + ";" + os.environ.get("PATH", "")


def bootstrap_binaries(force_download: bool = False) -> bool:
    """Identify host platform, verify and load local precompiled SQLCipher libraries directly from the installation path."""
    # 0. Check if sqlcipher3 is already fully functional in the host environment without bootstrapping
    if not force_download:
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

    for rel_path_str, expected_hash in expected_files.items():
        file_path = platform_binaries_dir / rel_path_str
        if not file_path.exists():
            raise RuntimeError(
                f"Startup validation failed: local packaged binary file {rel_path_str} is missing."
            )

        # Calculate SHA256 of the file
        import hashlib

        hasher = hashlib.sha256()
        try:
            is_text_file = file_path.suffix in (".py", ".pyi", ".typed")
            if is_text_file:
                with open(file_path, "r", encoding="utf-8-sig", newline=None) as fh:
                    content = fh.read()
                # Normalize all line endings to LF (\n)
                normalized_bytes = content.replace("\r\n", "\n").encode("utf-8")
                hasher.update(normalized_bytes)
            else:
                with open(file_path, "rb") as fh:
                    while chunk := fh.read(8192):
                        hasher.update(chunk)
            actual_hash = hasher.hexdigest()
        except Exception as e:
            raise RuntimeError(
                f"Startup validation failed: could not verify integrity of {rel_path_str}. Error: {e}"
            )

        if actual_hash != expected_hash:
            raise RuntimeError(
                f"Startup validation failed: local packaged binary file {rel_path_str} has been modified."
            )

    # 5. Inject paths directly from the installation directory
    inject_bootstrap_paths(platform_binaries_dir)

    # 6. Clear sys.modules of sqlcipher3, _sqlite3, and sqlite3 to force reload from the newly injected paths
    for k in list(sys.modules.keys()):
        if k in ("sqlcipher3", "_sqlite3", "sqlite3") or k.startswith("sqlcipher3.") or k.startswith("sqlite3."):
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
