"""Configuration settings for the autosorter application.

This module contains the AppSettings for managing dynamic configuration.
"""

import json
import logging
import os
import sys
import threading
from pathlib import Path

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def get_app_dir() -> Path:
    """Get the app configuration directory path."""
    app_dir = Path.home() / ".autosorter"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


class Settings(BaseSettings):
    """Application settings schema."""

    CONTEXTUAL_RENAMING: bool = Field(default=False)
    PRESERVE_HIERARCHY: bool = Field(default=False)
    MAX_FOLDERS: int = Field(default=12, gt=0, le=50)
    MAX_WORKERS: int = Field(default=4, gt=0, le=64)
    MAX_DEPTH: int = Field(default=5, gt=0, le=10)
    MAX_FEATURES: int = Field(default=3, gt=0, le=10)
    CLEANUP_EMPTY_FOLDERS: bool = Field(default=True)
    KEYWORD_RULES: dict = Field(default_factory=dict)
    LEARNED_RULES: dict = Field(default_factory=dict)

    @field_validator("KEYWORD_RULES", "LEARNED_RULES")
    @classmethod
    def validate_keyword_rules(cls, v: dict) -> dict:
        """Validate keyword rules to ensure correct format and valid characters."""
        illegal_chars = set('<>:"|?*')
        for keyword, target_path in v.items():
            if not isinstance(target_path, str):
                raise ValueError(
                    f"Target path for keyword '{keyword}' must be a string."
                )

            # Check for illegal OS characters
            if any(char in illegal_chars for char in target_path):
                raise ValueError(
                    f"Target path '{target_path}' contains illegal characters."
                )

            # Check for absolute path roots (/ or \)
            if target_path.startswith("/") or target_path.startswith("\\"):
                raise ValueError(f"Target path '{target_path}' is an absolute path.")

            # Check for directory traversal segments (..)
            segments = target_path.replace("\\", "/").split("/")
            if ".." in segments:
                raise ValueError(
                    f"Target path '{target_path}' contains directory traversal segments."
                )

        return v

    AI_CONSENT_GRANTED: bool | None = Field(default=None)
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

            for key, value in data.items():
                if hasattr(self._settings_model, key):
                    try:
                        setattr(self._settings_model, key, value)
                    except (ValueError, ValidationError) as e:
                        logging.warning(f"Invalid {key} in config, using default: {e}")

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
            data = self._settings_model.model_dump(mode="json")
        try:
            with open(self._filepath, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logging.error(f"Failed to save settings: {e}")

    def __getattr__(self, name):
        """Get attribute dynamically from the settings model."""
        if hasattr(self._settings_model, name):
            return getattr(self._settings_model, name)
        raise AttributeError(
            f"'{self.__class__.__name__}' object has no attribute '{name}'"
        )

    def __setattr__(self, name, value):
        """Set attribute dynamically and trigger a save."""
        if name in ("_filepath", "_lock", "_save_timer", "_settings_model"):
            super().__setattr__(name, value)
        else:
            setattr(self._settings_model, name, value)
            self._trigger_save()
