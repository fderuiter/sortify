"""User-space precompiled bootstrapping module.

Identifies host platform, downloads/resolves precompiled native binaries to
a writable folder in the user's home directory, dynamically registers search
paths, and verifies database encryption.
"""

import os
import sys
import shutil
import logging
from pathlib import Path
import importlib.util

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
        # Dynamically import sqlcipher3 from the modified search paths
        if "sqlcipher3" in sys.modules:
            import importlib

            try:
                importlib.reload(sys.modules["sqlcipher3"])
            except Exception:
                pass

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


def inject_bootstrap_paths():
    """Dynamically modify search paths to include the user-space binaries folder."""
    bin_dir = get_bootstrap_bin_dir()
    sqlcipher3_path = bin_dir / "sqlcipher3"

    if bin_dir.exists():
        bin_dir_str = str(bin_dir)
        if bin_dir_str not in sys.path:
            sys.path.insert(0, bin_dir_str)

        if sys.platform == "win32":
            try:
                os.add_dll_directory(str(bin_dir))
            except Exception:
                pass
            try:
                os.add_dll_directory(str(sqlcipher3_path))
            except Exception:
                pass

            paths = [str(bin_dir), str(sqlcipher3_path)]
            os.environ["PATH"] = (
                ";".join(paths) + ";" + os.environ.get("PATH", "")
            )


def bootstrap_binaries(force_download: bool = False) -> bool:
    """Identify host platform, download or copy binaries, and register paths."""
    bin_dir = get_bootstrap_bin_dir()
    sqlcipher3_path = bin_dir / "sqlcipher3"

    # 1. Check if native binaries are already present (Subsequent launches bypass)
    if sqlcipher3_path.exists() and not force_download:
        inject_bootstrap_paths()
        if verify_sqlcipher_encryption():
            logger.info(
                "Subsequent launch: cached native binaries found and verified. Bypassing download phase."
            )
            return True
        else:
            logger.warning(
                "Cached native binaries failed verification. Re-bootstrapping..."
            )

    # Ensure parent directories exist (always in user-space, avoiding read-only app folders)
    bin_dir.mkdir(parents=True, exist_ok=True)

    system_platform = sys.platform
    logger.info(f"Identifying host platform: {system_platform}")

    # 2. Attempt to download matching precompiled native binaries
    download_success = False
    if check_internet_connection():
        platform_name = (
            "windows"
            if system_platform == "win32"
            else ("macos" if system_platform == "darwin" else "linux")
        )
        url = f"https://assets.autosorter.com/binaries/{platform_name}/sqlcipher3.zip"

        logger.info(
            f"Downloading precompiled binaries for {platform_name} from {url}..."
        )
        try:
            import io
            import urllib.request
            import zipfile

            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
                },
            )
            with urllib.request.urlopen(req, timeout=10.0) as response:
                zip_data = response.read()

            temp_extract_dir = bin_dir / "temp_extract"
            if temp_extract_dir.exists():
                shutil.rmtree(temp_extract_dir)
            temp_extract_dir.mkdir(parents=True, exist_ok=True)

            with zipfile.ZipFile(io.BytesIO(zip_data)) as zip_ref:
                zip_ref.extractall(temp_extract_dir)

            extracted_sqlcipher_dir = temp_extract_dir / "sqlcipher3"
            if (
                extracted_sqlcipher_dir.exists()
                and extracted_sqlcipher_dir.is_dir()
            ):
                if sqlcipher3_path.exists():
                    shutil.rmtree(sqlcipher3_path)
                shutil.move(str(extracted_sqlcipher_dir), str(sqlcipher3_path))
            else:
                if sqlcipher3_path.exists():
                    shutil.rmtree(sqlcipher3_path)
                shutil.move(str(temp_extract_dir), str(sqlcipher3_path))

            if temp_extract_dir.exists():
                shutil.rmtree(temp_extract_dir, ignore_errors=True)

            download_success = True
            logger.info("Precompiled binaries successfully downloaded.")
        except Exception as e:
            logger.warning(
                f"Download of precompiled binaries failed: {e}. Falling back to cached local libraries."
            )
            download_success = False

    # 3. Fallback to cached local native libraries if offline or download failed
    if not download_success:
        logger.info("Falling back to cached local native libraries...")
        spec = importlib.util.find_spec("sqlcipher3")
        if spec and spec.submodule_search_locations:
            local_sqlcipher3_dir = Path(spec.submodule_search_locations[0])

            if sqlcipher3_path.exists():
                if sqlcipher3_path.is_dir():
                    shutil.rmtree(sqlcipher3_path)
                else:
                    sqlcipher3_path.unlink()

            shutil.copytree(local_sqlcipher3_dir, sqlcipher3_path)
            logger.info(
                f"Successfully copied cached local native libraries from {local_sqlcipher3_dir} to {sqlcipher3_path}"
            )
        else:
            raise RuntimeError(
                "SQLCipher local native library is missing and internet connection is unavailable."
            )

    # 4. Dynamically inject the paths
    inject_bootstrap_paths()

    # 5. Execute pre-flight verification
    if verify_sqlcipher_encryption():
        logger.info(
            "Startup pre-flight database encryption verification successful!"
        )
        return True
    else:
        raise RuntimeError(
            "Startup verification failed: pre-flight database encryption is not active or error-free."
        )
