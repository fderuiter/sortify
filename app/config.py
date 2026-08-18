"""Configuration settings for the autosorter application.

This module contains the AppSettings for managing dynamic configuration.
"""

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def get_app_dir() -> Path:
    """Get the app configuration directory path."""
    app_dir = Path.home() / ".autosorter"
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir


class Settings(BaseSettings):
    """Application settings schema."""

    CONTEXTUAL_RENAMING: bool = Field(default=False)
    AI_ASSISTED_NAMING: bool = Field(default=False)
    PRESERVE_HIERARCHY: bool = Field(default=False)
    MAX_FOLDERS: int = Field(default=12, gt=0, le=50)
    MAX_WORKERS: int = Field(default=4, gt=0, le=64)
    MAX_DEPTH: int = Field(default=5, gt=0, le=10)
    MAX_FEATURES: int = Field(default=3, gt=0, le=10)
    CLEANUP_EMPTY_FOLDERS: bool = Field(default=True)
    EXPLORER_INTEGRATION: bool = Field(default=False)
    KEYWORD_RULES: dict = Field(default_factory=dict)
    LEARNED_RULES: dict = Field(default_factory=dict)
    POLICIES: list[dict] = Field(default_factory=list)
    VISUAL_TIMEOUT: int = Field(default=30, gt=0)
    IMAGE_MAX_DIMENSION: int = Field(default=1000, gt=0)
    IMAGE_SKIP_THRESHOLD: int = Field(default=3000, gt=0)
    MODEL_THREADS: int = Field(default=2, ge=1, le=32)
    PROTECTED_PATHS: list[str] = Field(default_factory=list)
    PROXY: str = Field(default="")
    OCR_GPU_ENABLED: bool = Field(default=False)
    AUDIO_GPU_ENABLED: bool = Field(default=False)
    OCR_LANGUAGES: str = Field(default="en")
    CONFLICT_POLICY: Literal["skip", "rename"] = Field(default="rename")
    SORTING_STRATEGY: Literal[
        "default", "generative", "clinical_tmf", "clinical_isf"
    ] = Field(default="default")
    CLINICAL_SMART_RENAMING: bool = Field(default=False)
    CLINICAL_GENERATE_AUDIT_REPORT: bool = Field(default=True)
    COHERENCE_THRESHOLD: float = Field(default=0.5, ge=0.0, le=1.0)
    DEBOUNCE_DELAY: float = Field(default=0.6, gt=0.0)
    MAX_DEBOUNCE_DELAY: float = Field(default=5.0, gt=0.0)
    IGNORED_EXTENSIONS: list[str] = Field(default=[".crdownload", ".tmp", ".download"])

    @model_validator(mode="after")
    def validate_debounce_delays(self) -> "Settings":
        """Validate that DEBOUNCE_DELAY does not exceed MAX_DEBOUNCE_DELAY."""
        if self.DEBOUNCE_DELAY > self.MAX_DEBOUNCE_DELAY:
            raise ValueError(
                f"DEBOUNCE_DELAY ({self.DEBOUNCE_DELAY}) cannot be greater than "
                f"MAX_DEBOUNCE_DELAY ({self.MAX_DEBOUNCE_DELAY})."
            )
        return self

    @field_validator("CONFLICT_POLICY")
    @classmethod
    def validate_conflict_policy(cls, v: str) -> str:
        """Validate that CONFLICT_POLICY is either 'skip' or 'rename'."""
        if not isinstance(v, str):
            raise ValueError("CONFLICT_POLICY must be a string.")
        if v not in ("skip", "rename"):
            raise ValueError("CONFLICT_POLICY must be either 'skip' or 'rename'.")
        return v

    @field_validator("OCR_LANGUAGES")
    @classmethod
    def validate_ocr_languages(cls, v: str) -> str:
        """Validate that OCR_LANGUAGES is a valid comma-separated string of non-empty language codes."""
        if not isinstance(v, str):
            raise ValueError("OCR_LANGUAGES must be a string.")
        parts = [p.strip() for p in v.split(",") if p.strip()]
        if not parts:
            raise ValueError("OCR_LANGUAGES must contain at least one language code.")
        raw_parts = v.split(",")
        for part in raw_parts:
            part_stripped = part.strip()
            if not part_stripped:
                raise ValueError("OCR_LANGUAGES cannot contain empty language codes.")
            if not all(c.isalnum() or c == "_" for c in part_stripped):
                raise ValueError(
                    f"Invalid language code: '{part_stripped}'. Only alphanumeric characters and underscores are allowed."
                )
        return v

    @field_validator("PROTECTED_PATHS")
    @classmethod
    def validate_protected_paths(cls, v: list[str]) -> list[str]:
        """Validate that each path is an absolute literal directory path."""
        for path in v:
            if not isinstance(path, str):
                raise ValueError("Each protected path must be a string.")
            if not os.path.isabs(path):
                raise ValueError(f"Protected path must be an absolute path: '{path}'")
            if any(char in path for char in ("*", "?", "[", "]")):
                raise ValueError(
                    f"Wildcard patterns or glob characters are not allowed in protected path: '{path}'"
                )
        return v

    @field_validator("KEYWORD_RULES", "LEARNED_RULES")
    @classmethod
    def validate_keyword_rules(cls, v: dict) -> dict:
        """Validate that keyword routing rules and learned rules have valid target paths."""
        from app.core.path_utils import validate_target_path

        for keyword, target_path in v.items():
            validate_target_path(target_path, keyword=keyword)
        return v

    @field_validator("POLICIES")
    @classmethod
    def validate_policies(cls, v: list[dict]) -> list[dict]:
        """Validate that unified policies have valid target paths, types, expression, and priority, and check for overlaps."""
        from app.core.path_utils import validate_target_path

        for rule in v:
            if not isinstance(rule, dict):
                raise ValueError("Each policy must be a dictionary.")
            rule_type = rule.get("type")
            if rule_type not in ("keyword", "pattern", "override"):
                raise ValueError(
                    f"Invalid policy type: {rule_type}. Must be keyword, pattern, or override."
                )

            expression = rule.get("expression")
            if not isinstance(expression, str) or not expression.strip():
                raise ValueError("Policy expression must be a non-empty string.")

            target_path = rule.get("target_path")
            validate_target_path(target_path, keyword=expression)

            if "priority" not in rule or not isinstance(rule["priority"], int):
                raise ValueError("Policy must have an integer priority.")

            if "halting" in rule and not isinstance(rule["halting"], bool):
                raise ValueError("Policy halting property must be a boolean.")

        # Overlap check
        def is_masked_by(higher_rule, lower_rule) -> bool:
            ha_type = higher_rule.get("type", "").lower()
            lo_type = lower_rule.get("type", "").lower()
            ha_expr = higher_rule.get("expression", "").lower()
            lo_expr = lower_rule.get("expression", "").lower()

            if not ha_expr or not lo_expr:
                return False

            if ha_expr in lo_expr:
                if ha_type == "keyword":
                    return True
                if ha_type == "pattern":
                    if lo_type in ("pattern", "override"):
                        return True
                if ha_type == "override" and lo_type == "override":
                    return True
            return False

        sorted_policies = sorted(v, key=lambda x: x.get("priority", 0), reverse=True)
        for i, lower_rule in enumerate(sorted_policies):
            for higher_rule in sorted_policies[:i]:
                if is_masked_by(higher_rule, lower_rule):
                    msg = (
                        f"Rule overlap detected: policy of type '{lower_rule['type']}' with expression "
                        f"'{lower_rule['expression']}' (priority {lower_rule['priority']}) is fully masked/shadowed by "
                        f"higher-priority policy of type '{higher_rule['type']}' with expression "
                        f"'{higher_rule['expression']}' (priority {higher_rule['priority']})."
                    )
                    logging.warning(msg)
                    print(f"WARNING: {msg}", file=sys.stderr)
                    break

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
        self._raw_encrypted_proxy = None
        self._validation_errors = []

        try:
            self._settings_model = Settings()
        except ValidationError as e:
            print(f"Configuration error: {e}", file=sys.stderr)
            sys.exit(1)

        self.load()

    def load(self):
        """Load settings from the configuration file."""
        self._validation_errors = []
        if not os.path.exists(self._filepath):
            self._trigger_save()
            return

        has_validation_errors = False
        needs_migration = False
        try:
            with open(self._filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            # Decrypt PROXY setting if encrypted, or mark for migration if legacy plaintext
            if isinstance(data, dict) and "PROXY" in data:
                proxy_val = data["PROXY"]
                if proxy_val:
                    if proxy_val.startswith("enc:"):
                        self._raw_encrypted_proxy = proxy_val
                        try:
                            from app.core.path_utils import resolve_db_crypto

                            crypto = resolve_db_crypto(self._filepath)
                            decrypted_val = crypto.decrypt_text(proxy_val[4:])
                            data["PROXY"] = decrypted_val
                        except Exception as e:
                            logging.warning(
                                f"Failed to decrypt proxy settings, replacing with placeholder: {e}"
                            )
                            data["PROXY"] = "<DECRYPTION_FAILED>"
                    else:
                        needs_migration = True

            # Validate against static schema file if it exists
            schema_path = Path(__file__).parent / "config_schema.json"
            if schema_path.exists():
                import jsonschema

                with open(schema_path, "r", encoding="utf-8") as sf:
                    schema = json.load(sf)
                validator = jsonschema.Draft202012Validator(schema)
                errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
                for error in errors:
                    has_validation_errors = True
                    path = (
                        ".".join([str(p) for p in error.path]) if error.path else "root"
                    )
                    logging.warning(
                        f"Configuration validation failed for field '{path}': {error.message}. Using default value."
                    )
                    self._validation_errors.append(
                        {"field": path, "message": error.message}
                    )

            data_keys = list(data.keys())
            if "MAX_DEBOUNCE_DELAY" in data_keys and "DEBOUNCE_DELAY" in data_keys:
                data_keys.remove("MAX_DEBOUNCE_DELAY")
                data_keys.insert(0, "MAX_DEBOUNCE_DELAY")

            for key in data_keys:
                value = data[key]
                if hasattr(self._settings_model, key):
                    try:
                        setattr(self._settings_model, key, value)
                    except (ValueError, ValidationError) as e:
                        if not has_validation_errors:
                            logging.warning(
                                f"Invalid {key} in config, using default: {e}"
                            )
                        has_validation_errors = True
                        self._validation_errors.append(
                            {"field": key, "message": str(e)}
                        )

            if has_validation_errors:
                # Do not allow saving to overwrite the invalid user settings
                self._has_validation_errors = True
            else:
                self._has_validation_errors = False

            if needs_migration and not has_validation_errors:
                self._trigger_save()

        except Exception as e:
            logging.warning(f"Failed to load settings, using defaults: {e}")
            # If JSON is corrupted, we don't want to overwrite either
            self._has_validation_errors = True
            self._validation_errors.append(
                {"field": "json", "message": f"Failed to load settings file: {e}"}
            )

    def _trigger_save(self):
        if getattr(self, "_has_validation_errors", False):
            logging.warning(
                "Skipping save to prevent overwriting invalid user configuration."
            )
            return
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
            # Encrypt PROXY setting if present and not already encrypted
            proxy_val = data.get("PROXY", "")
            if proxy_val == "<DECRYPTION_FAILED>":
                if self._raw_encrypted_proxy:
                    data["PROXY"] = self._raw_encrypted_proxy
            elif proxy_val and not proxy_val.startswith("enc:"):
                try:
                    from app.core.path_utils import resolve_db_crypto

                    crypto = resolve_db_crypto(self._filepath)
                    encrypted_val = crypto.encrypt_text(proxy_val)
                    if isinstance(encrypted_val, bytes):
                        encrypted_val = encrypted_val.decode("utf-8")
                    data["PROXY"] = f"enc:{encrypted_val}"
                except Exception as e:
                    logging.error(f"Failed to encrypt proxy string during save: {e}")

            with open(self._filepath, "w", encoding="utf-8") as f:
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
        if name in (
            "_filepath",
            "_lock",
            "_save_timer",
            "_settings_model",
            "_raw_encrypted_proxy",
            "_validation_errors",
            "_has_validation_errors",
        ):
            super().__setattr__(name, value)
        else:
            if name == "PROXY" and value != "<DECRYPTION_FAILED>":
                super().__setattr__("_raw_encrypted_proxy", None)
            setattr(self._settings_model, name, value)
            self._trigger_save()
