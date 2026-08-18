"""Offline Bundle Importer with SHA-256 integrity verification and network isolation."""

import hashlib
import json
import logging
import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

from app.core.shared_registry import (
    ContextPropagatingThread,
    SharedModelRegistry,
    block_external_network,
)

logger = logging.getLogger(__name__)


class OfflineImportError(Exception):
    """Base exception for offline import failures."""

    pass


class InvalidArchiveError(OfflineImportError):
    """Raised when an archive file or directory structure is invalid."""

    pass


class ModelVerificationError(OfflineImportError):
    """Raised when model configuration or checksum verification fails."""

    pass


class ImportCancelledError(OfflineImportError):
    """Raised when an import process is canceled by the user."""

    pass


class OfflineBundleImporter:
    """Singleton manager for importing offline model zip archives and uncompressed directories."""

    _instance = None
    _lock = threading.Lock()

    @classmethod
    def get_instance(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def __init__(self):
        self._state_lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._active_thread = None
        self.state = {
            "is_importing": False,
            "progress": 0.0,
            "status_text": "",
            "error": None,
            "success": False,
            "source_path": "",
            "import_type": None,  # "archive" or "directory"
        }

    def reset_state(self):
        with self._state_lock:
            self.state["is_importing"] = False
            self.state["progress"] = 0.0
            self.state["status_text"] = ""
            self.state["error"] = None
            self.state["success"] = False
            self.state["source_path"] = ""
            self.state["import_type"] = None
            self._cancel_event.clear()

    def update_state(self, **kwargs):
        with self._state_lock:
            for k, v in kwargs.items():
                self.state[k] = v

    def cancel_import(self):
        self._cancel_event.set()
        self.update_state(status_text="Canceling import...")

    def import_archive_async(
        self, zip_path: str, target_model_dir: str, settings=None, on_done=None
    ):
        """Asynchronously extract, verify, and import a model zip archive."""
        if self.state["is_importing"]:
            raise RuntimeError("An import operation is already in progress.")

        self.reset_state()
        self.update_state(
            is_importing=True,
            status_text="Starting archive import...",
            source_path=zip_path,
            import_type="archive",
        )

        def worker():
            err_to_report = None
            try:
                with block_external_network(reason="offline model bundle import"):
                    self._run_archive_import(zip_path, target_model_dir, settings)
            except Exception as e:
                logger.error(f"Offline archive import failed: {e}", exc_info=True)
                err_to_report = str(e)
                self.update_state(
                    is_importing=False,
                    error=err_to_report,
                    success=False,
                    status_text=f"Import failed: {err_to_report}",
                )
            else:
                self.update_state(
                    is_importing=False,
                    success=True,
                    progress=1.0,
                    status_text="Offline model archive imported successfully!",
                )

            if on_done:
                try:
                    on_done(self.state["success"], self.state["error"])
                except Exception as cb_err:
                    logger.error(f"Error in import on_done callback: {cb_err}")

        self._active_thread = ContextPropagatingThread(target=worker, daemon=True)
        self._active_thread.start()

    def import_directory_async(
        self, dir_path: str, target_model_dir: str, settings=None, on_done=None
    ):
        """Asynchronously verify and link/import an uncompressed model directory."""
        if self.state["is_importing"]:
            raise RuntimeError("An import operation is already in progress.")

        self.reset_state()
        self.update_state(
            is_importing=True,
            status_text="Starting directory import...",
            source_path=dir_path,
            import_type="directory",
        )

        def worker():
            err_to_report = None
            try:
                with block_external_network(reason="offline model directory import"):
                    self._run_directory_import(dir_path, target_model_dir, settings)
            except Exception as e:
                logger.error(f"Offline directory import failed: {e}", exc_info=True)
                err_to_report = str(e)
                self.update_state(
                    is_importing=False,
                    error=err_to_report,
                    success=False,
                    status_text=f"Import failed: {err_to_report}",
                )
            else:
                self.update_state(
                    is_importing=False,
                    success=True,
                    progress=1.0,
                    status_text="Offline model directory linked successfully!",
                )

            if on_done:
                try:
                    on_done(self.state["success"], self.state["error"])
                except Exception as cb_err:
                    logger.error(f"Error in import on_done callback: {cb_err}")

        self._active_thread = ContextPropagatingThread(target=worker, daemon=True)
        self._active_thread.start()

    def _check_canceled(self):
        if self._cancel_event.is_set():
            raise ImportCancelledError("Import process was canceled by the user.")

    def _find_model_root(self, search_dir: str) -> str:
        """Find directory containing config.json within search_dir."""
        if os.path.exists(os.path.join(search_dir, "config.json")):
            return search_dir

        found_dirs = []
        for root, _, files in os.walk(search_dir):
            if "config.json" in files:
                found_dirs.append(root)

        if not found_dirs:
            raise InvalidArchiveError(
                "Missing required configuration file: 'config.json' was not found in the model bundle."
            )

        found_dirs.sort(key=lambda p: len(Path(p).parts))
        return found_dirs[0]

    def _validate_config_file(self, model_root: str):
        """Validate presence and structure of required config files."""
        config_path = os.path.join(model_root, "config.json")
        if not os.path.exists(config_path):
            raise InvalidArchiveError(
                "Missing required model configuration file: 'config.json'."
            )

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or len(data) == 0:
                raise InvalidArchiveError(
                    "Invalid configuration structure: 'config.json' is empty or invalid JSON."
                )
        except json.JSONDecodeError as e:
            raise InvalidArchiveError(f"Corrupt configuration file 'config.json': {e}")
        except Exception as e:
            raise InvalidArchiveError(
                f"Failed to read configuration file 'config.json': {e}"
            )

    def _verify_hashes_and_integrity(
        self, model_root: str, progress_start=0.4, progress_end=0.8
    ):
        """Compute SHA-256 checksums and verify against registered expected hashes."""
        self._check_canceled()
        registry = SharedModelRegistry.get_instance()
        expected_hashes = {}
        for model_id in ("generative_naming", "model_download"):
            if model_id in registry._expected_hashes:
                expected_hashes.update(registry._expected_hashes[model_id])

        files_to_check = []
        for root, _, files in os.walk(model_root):
            for file in files:
                rel_path = os.path.relpath(os.path.join(root, file), model_root)
                abs_path = os.path.join(root, file)
                files_to_check.append((rel_path, abs_path, file))

        if not files_to_check:
            raise InvalidArchiveError("Model directory contains no files.")

        total_files = len(files_to_check)
        for idx, (rel_path, abs_path, filename) in enumerate(files_to_check):
            self._check_canceled()
            prog = progress_start + (progress_end - progress_start) * (
                idx / max(total_files, 1)
            )
            self.update_state(
                progress=prog,
                status_text=f"Verifying SHA-256 checksum for {filename} ({idx + 1}/{total_files})...",
            )

            hasher = hashlib.sha256()
            with open(abs_path, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    self._check_canceled()
                    hasher.update(chunk)
            actual_hash = hasher.hexdigest()

            expected_hash = expected_hashes.get(filename) or expected_hashes.get(
                rel_path
            )
            if expected_hash and actual_hash != expected_hash:
                raise ModelVerificationError(
                    f"SHA-256 checksum mismatch for '{filename}': expected {expected_hash}, got {actual_hash}"
                )

    def _run_archive_import(
        self, zip_path: str, target_model_dir: str, settings=None
    ):
        if not zip_path or not os.path.exists(zip_path):
            raise InvalidArchiveError(f"Zip archive file not found: {zip_path}")
        if not zipfile.is_zipfile(zip_path):
            raise InvalidArchiveError(f"File is not a valid zip archive: {zip_path}")

        temp_extract_dir = tempfile.mkdtemp(prefix="offline_import_")
        try:
            self.update_state(progress=0.05, status_text="Extracting archive...")
            with zipfile.ZipFile(zip_path, "r") as zf:
                members = zf.infolist()
                total_members = len(members)
                for idx, member in enumerate(members):
                    self._check_canceled()
                    zf.extract(member, temp_extract_dir)
                    prog = 0.05 + 0.35 * ((idx + 1) / max(total_members, 1))
                    self.update_state(
                        progress=prog,
                        status_text=f"Extracting {member.filename} ({idx + 1}/{total_members})...",
                    )

            self.update_state(
                progress=0.4, status_text="Validating extracted model files..."
            )
            model_root = self._find_model_root(temp_extract_dir)
            self._validate_config_file(model_root)

            self._verify_hashes_and_integrity(
                model_root, progress_start=0.4, progress_end=0.8
            )

            self.update_state(progress=0.85, status_text="Finalizing model files...")
            self._check_canceled()

            target_path = Path(target_model_dir)
            target_path.parent.mkdir(parents=True, exist_ok=True)

            if target_path.exists():
                if target_path.is_symlink():
                    target_path.unlink()
                else:
                    shutil.rmtree(target_path, ignore_errors=True)

            shutil.copytree(model_root, target_path)

            if settings is not None:
                try:
                    settings.AI_CONSENT_GRANTED = True
                except Exception:
                    pass
        finally:
            shutil.rmtree(temp_extract_dir, ignore_errors=True)

    def _run_directory_import(
        self, dir_path: str, target_model_dir: str, settings=None
    ):
        if not dir_path or not os.path.exists(dir_path):
            raise InvalidArchiveError(f"Directory not found: {dir_path}")
        if not os.path.isdir(dir_path):
            raise InvalidArchiveError(f"Path is not a directory: {dir_path}")

        self.update_state(progress=0.1, status_text="Validating model directory...")
        model_root = self._find_model_root(dir_path)
        self._validate_config_file(model_root)

        self._verify_hashes_and_integrity(
            model_root, progress_start=0.1, progress_end=0.7
        )

        self.update_state(progress=0.8, status_text="Linking model directory...")
        self._check_canceled()

        target_path = Path(target_model_dir)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        if target_path.exists():
            if target_path.is_symlink():
                target_path.unlink()
            else:
                shutil.rmtree(target_path, ignore_errors=True)

        try:
            os.symlink(
                os.path.abspath(model_root), target_path, target_is_directory=True
            )
        except (OSError, NotImplementedError):
            shutil.copytree(model_root, target_path)

        if settings is not None:
            try:
                settings.AI_CONSENT_GRANTED = True
            except Exception:
                pass
