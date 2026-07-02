"""Configuration settings for the autosorter application.

This module contains the AppSettings for managing dynamic configuration.
"""

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Set

from pydantic import Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


def get_app_dir() -> Path:
    app_dir = Path.home() / ".autosorter"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


class Settings(BaseSettings):
    """Application settings schema."""

    CONTEXTUAL_RENAMING: bool = Field(default=False)
    PRESERVE_HIERARCHY: bool = Field(default=False)
    MAX_FOLDERS: int = Field(default=12, gt=0)
    MAX_WORKERS: int = Field(default=15, gt=0)
    MAX_DEPTH: int = Field(default=5, gt=0)
    MAX_FEATURES: int = Field(default=3, gt=0)
    LOG_FILE: str = Field(default=str(get_app_dir() / "autosorter.log"), min_length=1)
    STOP_WORDS: set[str] = {
        "the",
        "and",
        "for",
        "this",
        "that",
        "with",
        "from",
        "inc",
        "com",
        "pdf",
        "docx",
        "txt",
        "csv",
        "xlsx",
        "xls",
        "site",
        "team",
        "page",
        "nan",
        "unnamed",
        "your",
        "have",
        "will",
        "are",
        "not",
        "can",
        "all",
        "was",
        "has",
        "but",
        "what",
        "there",
        "out",
        "about",
        "get",
        "would",
        "like",
        "which",
        "their",
        "when",
        "who",
        "some",
        "how",
        "these",
        "into",
        "other",
        "could",
        "than",
        "only",
        "also",
        "over",
        "well",
        "because",
        "through",
        "don",
        "should",
        "been",
        "much",
        "where",
    }

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_assignment=True,
    )


class AppSettings:
    """A registry for application settings that provides persistence and validation."""

    def __init__(self, filepath=None):
        self._filepath = filepath or str(get_app_dir() / "settings.json")
        self._save_timer = None
        self._lock = threading.Lock()

        try:
            self._settings_model = Settings()
        except ValidationError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            sys.exit(1)

        self.load()

    def load(self):
        """Load settings from the configuration file."""
        if not os.path.exists(self._filepath):
            self._trigger_save()
            return

        try:
            with open(self._filepath, "r") as f:
                data = json.load(f)

            for key in [
                "MAX_FOLDERS",
                "MAX_WORKERS",
                "MAX_DEPTH",
                "MAX_FEATURES",
                "LOG_FILE",
                "CONTEXTUAL_RENAMING",
                "PRESERVE_HIERARCHY",
            ]:
                if key in data:
                    try:
                        setattr(self._settings_model, key, data[key])
                    except (ValueError, ValidationError) as e:
                        logging.warning(f"Invalid {key} in config, using default: {e}")

            if "STOP_WORDS" in data:
                try:
                    self._settings_model.STOP_WORDS = set(data["STOP_WORDS"])
                except (ValueError, ValidationError) as e:
                    logging.warning(f"Invalid STOP_WORDS in config, using default: {e}")

        except Exception as e:
            logging.warning(f"Failed to load settings, using defaults: {e}")
            self._trigger_save()

    def _trigger_save(self):
        with self._lock:
            if self._save_timer is not None:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(0.5, self._save)
            # Ensure background thread doesn't block app exit
            self._save_timer.daemon = True
            self._save_timer.start()

    def _save(self):
        with self._lock:
            data = {
                "CONTEXTUAL_RENAMING": self._settings_model.CONTEXTUAL_RENAMING,
                "PRESERVE_HIERARCHY": self._settings_model.PRESERVE_HIERARCHY,
                "MAX_FOLDERS": self._settings_model.MAX_FOLDERS,
                "MAX_WORKERS": self._settings_model.MAX_WORKERS,
                "MAX_DEPTH": self._settings_model.MAX_DEPTH,
                "MAX_FEATURES": self._settings_model.MAX_FEATURES,
                "LOG_FILE": self._settings_model.LOG_FILE,
                "STOP_WORDS": list(self._settings_model.STOP_WORDS),
            }
        try:
            with open(self._filepath, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logging.error(f"Failed to save settings: {e}")

    @property
    def CONTEXTUAL_RENAMING(self) -> bool:
        """Get the contextual renaming flag."""
        return self._settings_model.CONTEXTUAL_RENAMING

    @CONTEXTUAL_RENAMING.setter
    def CONTEXTUAL_RENAMING(self, value: bool):
        self._settings_model.CONTEXTUAL_RENAMING = value
        self._trigger_save()

    @property
    def PRESERVE_HIERARCHY(self) -> bool:
        """Get the preserve hierarchy flag."""
        return self._settings_model.PRESERVE_HIERARCHY

    @PRESERVE_HIERARCHY.setter
    def PRESERVE_HIERARCHY(self, value: bool):
        self._settings_model.PRESERVE_HIERARCHY = value
        self._trigger_save()

    @property
    def MAX_FOLDERS(self) -> int:
        """Get the maximum number of folders."""
        return self._settings_model.MAX_FOLDERS

    @MAX_FOLDERS.setter
    def MAX_FOLDERS(self, value: int):
        self._settings_model.MAX_FOLDERS = value
        self._trigger_save()

    @property
    def MAX_WORKERS(self) -> int:
        """Get the maximum number of worker threads."""
        return self._settings_model.MAX_WORKERS

    @MAX_WORKERS.setter
    def MAX_WORKERS(self, value: int):
        self._settings_model.MAX_WORKERS = value
        self._trigger_save()

    @property
    def MAX_DEPTH(self) -> int:
        """Get the maximum recursion depth for sorting."""
        return self._settings_model.MAX_DEPTH

    @MAX_DEPTH.setter
    def MAX_DEPTH(self, value: int):
        self._settings_model.MAX_DEPTH = value
        self._trigger_save()

    @property
    def MAX_FEATURES(self) -> int:
        """Get the max features for clustering."""
        return self._settings_model.MAX_FEATURES

    @MAX_FEATURES.setter
    def MAX_FEATURES(self, value: int):
        self._settings_model.MAX_FEATURES = value
        self._trigger_save()

    @property
    def LOG_FILE(self) -> str:
        """Get the central log file path."""
        return self._settings_model.LOG_FILE

    @LOG_FILE.setter
    def LOG_FILE(self, value: str):
        self._settings_model.LOG_FILE = value
        self._trigger_save()

    @property
    def STOP_WORDS(self) -> Set[str]:
        """Get the set of stop words to filter out."""
        return self._settings_model.STOP_WORDS

    @STOP_WORDS.setter
    def STOP_WORDS(self, value: Set[str]):
        if not isinstance(value, set):
            try:
                value = set(value)
            except Exception:
                raise ValueError("STOP_WORDS must be a set of strings")
        self._settings_model.STOP_WORDS = value
        self._trigger_save()
