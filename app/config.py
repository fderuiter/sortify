"""Configuration settings for the autosorter application.

This module contains the AppSettings for managing dynamic configuration.
"""

import inspect
import json
import logging
import os
import sys
import threading
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, Literal, NotRequired, TypedDict

from pydantic import (
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
    with_config,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.policy_engine import is_masked_by

_SCHEMA_VALIDATOR = None
_SCHEMA_VALIDATOR_LOCK = threading.Lock()


def _get_schema_validator():
    global _SCHEMA_VALIDATOR
    if _SCHEMA_VALIDATOR is None:
        with _SCHEMA_VALIDATOR_LOCK:
            if _SCHEMA_VALIDATOR is None:
                schema_path = Path(__file__).parent / "config_schema.json"
                if schema_path.exists():
                    try:
                        import jsonschema

                        with open(schema_path, "r", encoding="utf-8") as sf:
                            schema = json.load(sf)
                        _SCHEMA_VALIDATOR = jsonschema.Draft202012Validator(schema)
                    except Exception as e:
                        logging.error(
                            f"Failed to load config schema: {e}", exc_info=True
                        )
    return _SCHEMA_VALIDATOR


def _dispatch_observer_callback(cb: Any, key: str, value: Any) -> None:
    try:
        sig = inspect.signature(cb)
        params = [
            p
            for p in sig.parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        ]
        has_var_args = any(p.kind == p.VAR_POSITIONAL for p in sig.parameters.values())

        if len(params) == 0 and not has_var_args:
            cb()
        elif len(params) == 1:
            cb(value)
        elif len(params) == 2:
            cb(key, value)
        elif has_var_args:
            cb(value)
        else:
            cb(value)
    except Exception as e:
        logging.error(
            f"Error executing config observer callback for key '{key}': {e}",
            exc_info=True,
        )


def _resolve_app_dir() -> Path:
    """Resolve the app configuration directory path without creating it."""
    override = os.environ.get("AUTOSORTER_APP_DIR")
    if override:
        app_dir = Path(override)
    else:
        app_dir = Path.home() / ".autosorter"
    return app_dir.resolve()


def get_app_dir() -> Path:
    """Get the app configuration directory path."""
    app_dir = _resolve_app_dir()
    app_dir.mkdir(parents=True, exist_ok=True)
    return app_dir.resolve()


def get_debug_log_dir() -> Path:
    """Get the dedicated sandboxed debug log directory."""
    log_dir = get_app_dir() / "logs" / "debug"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir.resolve()


@with_config(ConfigDict(extra="allow"))
class PolicyRule(TypedDict):
    """Typed dictionary schema for unified compliance and routing policy rules."""

    type: Annotated[
        Literal[
            "keyword",
            "pattern",
            "override",
            "action",
            "compliance",
            "unarchive",
            "duplicate",
            "age",
            "modified_date",
            "created_date",
            "exif_date",
            "size",
        ],
        Field(
            description=(
                "Rule evaluation strategy: 'keyword', 'action', 'compliance', or 'unarchive' "
                "match filenames and document text; 'pattern' matches lowercase filenames only; "
                "'override' matches lowercase filenames or full file paths; "
                "'duplicate' matches byte-verified duplicate files; 'age', 'modified_date', "
                "'created_date', 'exif_date', and 'size' evaluate file timestamps, EXIF/PDF headers, and byte sizes."
            )
        ),
    ]
    expression: Annotated[
        str,
        Field(
            min_length=1,
            description=(
                "Non-empty case-insensitive substring, wildcard pattern, or metadata comparison "
                "expression (such as '> 30d', '< 2025-01-01', or '> 10MB') to match."
            ),
        ),
    ]
    target_path: Annotated[
        str,
        Field(
            description=(
                "Relative destination folder path (supporting optional placeholders such as "
                "'{year}/{month}', '{date}', '{day}', '{category}', '{seq}', '{extension}', and '{stem}') "
                "validated against path safety rules (no leading slashes, '..' traversal segments, "
                "OS-reserved device names, illegal path characters, or trailing spaces/periods; "
                "may be empty when an action handles placement)."
            )
        ),
    ]
    priority: Annotated[
        int,
        Field(
            description="Integer evaluation priority; rules execute sequentially from highest priority to lowest."
        ),
    ]
    halting: NotRequired[
        Annotated[
            bool,
            Field(
                description="When true, stop evaluating lower-priority rules immediately if this rule does not match."
            ),
        ]
    ]
    enabled: NotRequired[
        Annotated[
            bool,
            Field(
                description="Whether this policy rule is active during evaluation (defaults to true when omitted)."
            ),
        ]
    ]
    rename_pattern: NotRequired[
        Annotated[
            str,
            Field(
                description=(
                    "Optional filename rename template applied when this policy matches "
                    "(supports {date}, {year}, {month}, {day}, {category}, {seq}, {sequence}, "
                    "{extension}, {stem}, and {original} placeholders)."
                )
            ),
        ]
    ]
    action: NotRequired[
        Annotated[
            Literal[
                "archive",
                "retain",
                "quarantine",
                "redact",
                "unarchive",
                "trash",
            ],
            Field(
                description="Optional lifecycle directive executed when the policy matches."
            ),
        ]
    ]
    detect_original_by: NotRequired[
        Annotated[
            Literal[
                "first_seen",
                "name",
                "created",
                "lastmodified",
                "-first_seen",
                "-name",
                "-created",
                "-lastmodified",
            ],
            Field(
                description=(
                    "Optional original-file selection mode for 'duplicate' policy rules "
                    "('first_seen', 'name', 'created', 'lastmodified', or '-' reverse variants)."
                )
            ),
        ]
    ]
    post_unarchive_action: NotRequired[
        Annotated[
            Literal[
                "delete",
                "remove",
                "cleanup",
                "move",
                "archive",
                "relocate",
                "retain",
                "keep",
            ],
            Field(
                description=(
                    "Source archive handling after 'unarchive' extraction: delete ('delete', "
                    "'remove', 'cleanup'), relocate ('move', 'archive', 'relocate'), or keep "
                    "in place ('retain', 'keep'). Legacy aliases 'post_action' and "
                    "'archive_action' are also accepted when loading settings.json."
                )
            ),
        ]
    ]


class Settings(BaseSettings):
    """Application settings schema."""

    CONTEXTUAL_RENAMING: bool = Field(
        default=False,
        description=(
            "Whether to rename sorted files using extracted document context and dates "
            "during plan generation."
        ),
    )
    AI_ASSISTED_NAMING: bool = Field(
        default=False,
        description=(
            "Whether to use local semantic embeddings and/or the generative LLM model to "
            "synthesize descriptive folder and file names (requires AI consent and the optional 'ml' extra)."
        ),
    )
    RENAME_PATTERN: str = Field(
        default="",
        description=(
            "Optional default rename pattern template combining extracted document or file dates "
            "with descriptive terms (for example '{date}_{stem}'; supports {date}, {year}, {month}, "
            "{day}, {category}, {seq}, {sequence}, {extension}, {stem}, and {original} placeholders)."
        ),
    )
    PRESERVE_HIERARCHY: bool = Field(
        default=False,
        description=(
            "Whether to retain relative source subfolder structure beneath generated "
            "destination category directories."
        ),
    )
    SCAN_MAX_DEPTH: int = Field(
        default=0,
        ge=0,
        le=20,
        description=(
            "Maximum subfolder recursion depth when scanning directories "
            "(integer from 0 to 20 levels; 0 scans top-level files only)."
        ),
    )
    MAX_FOLDERS: int = Field(
        default=12,
        gt=0,
        le=50,
        description=(
            "Maximum number of category folders generated per clustering level "
            "(integer from 1 to 50 folders)."
        ),
    )
    MAX_WORKERS: int = Field(
        default=4,
        gt=0,
        le=64,
        description=(
            "Maximum number of concurrent worker threads or processes used for file "
            "extraction and move operations (integer from 1 to 64 workers)."
        ),
    )
    MAX_DEPTH: int = Field(
        default=5,
        gt=0,
        le=10,
        description=(
            "Maximum directory recursion depth for hierarchical clustering and folder "
            "generation (integer from 1 to 10 levels)."
        ),
    )
    MAX_FEATURES: int = Field(
        default=3,
        gt=0,
        le=10,
        description=(
            "Maximum number of top TF-IDF keyword terms combined when naming generated "
            "cluster folders (integer from 1 to 10 terms)."
        ),
    )
    CLEANUP_EMPTY_FOLDERS: bool = Field(
        default=True,
        description=(
            "Whether to automatically remove empty source subdirectories after file "
            "moves complete."
        ),
    )
    SPEAK_ANNOUNCEMENTS: bool = Field(
        default=False,
        description=(
            "Whether to emit spoken accessibility announcements in the terminal UI "
            "when screen-reader feedback is enabled."
        ),
    )
    ENABLE_CHARACTER_KEY_SHORTCUTS: bool = Field(
        default=True,
        description=(
            "Whether single-character keyboard shortcuts (such as 'R' to reassign) are "
            "active in the terminal UI when text inputs are not focused."
        ),
    )
    KEYWORD_RULES: dict = Field(
        default_factory=dict,
        description=(
            "Mapping of lowercase keyword strings to relative target folder paths. "
            "Target paths are validated to forbid absolute paths, '..' traversal segments, "
            'OS-reserved device names, illegal characters (<, >, :, ", |, ?, *), and trailing spaces or periods.'
        ),
    )
    DISABLED_KEYWORD_RULES: dict = Field(
        default_factory=dict,
        description=(
            "Mapping of temporarily disabled keyword strings to relative target folder paths, "
            "preserving path mappings while inactive and enforcing the same relative target path validation as KEYWORD_RULES."
        ),
    )
    LEARNED_RULES: dict = Field(
        default_factory=dict,
        description=(
            "Mapping of user-corrected keyword patterns to relative target folder paths "
            "learned from interactive reassignments, validated with the same relative path rules as KEYWORD_RULES."
        ),
    )
    EXTENSION_CATEGORIES: dict = Field(
        default_factory=dict,
        description=(
            "Custom mapping of category folder names to lists of file extensions or "
            "extension overrides used for fallback file-type routing."
        ),
    )
    POLICIES: list[PolicyRule] = Field(
        default_factory=list,
        description=(
            "Ordered list of typed PolicyRule dictionaries evaluated from highest integer "
            "priority to lowest across 12 rule types ('keyword', 'pattern', 'override', 'action', "
            "'compliance', 'unarchive', 'duplicate', 'age', 'modified_date', 'created_date', 'exif_date', "
            "'size'), optional rename_pattern templates, 6 lifecycle actions ('archive', 'retain', "
            "'quarantine', 'redact', 'unarchive', 'trash'), and 8 post-unarchive actions; warns "
            "when higher-priority rules shadow lower-priority rules."
        ),
    )
    VISUAL_TIMEOUT: int = Field(
        default=30,
        gt=0,
        description=(
            "Per-file timeout in seconds (> 0) for visual OCR and vision model extraction "
            "before aborting visual analysis on a document."
        ),
    )
    IMAGE_MAX_DIMENSION: int = Field(
        default=1000,
        gt=0,
        description=(
            "Maximum width or height in pixels (> 0) to which images are downscaled "
            "while preserving aspect ratio prior to OCR or vision inference."
        ),
    )
    IMAGE_SKIP_THRESHOLD: int = Field(
        default=3000,
        gt=0,
        description=(
            "Dimension threshold in pixels (> 0) above which oversized images trigger "
            "pre-processing downscaling or fast-path handling."
        ),
    )
    PDF_TEXT_DENSITY_THRESHOLD: int = Field(
        default=50,
        ge=0,
        description=(
            "Minimum average extracted character count per PDF page (>= 0 characters) "
            "below which a PDF is treated as scanned and routed to OCR fallback."
        ),
    )
    MODEL_THREADS: int = Field(
        default=2,
        ge=1,
        le=32,
        description=(
            "Number of CPU threads allocated per local ML/ONNX/GGUF model inference "
            "worker (integer from 1 to 32 threads)."
        ),
    )
    GRAMMAR_CACHE_SIZE: int = Field(
        default=16,
        gt=0,
        le=256,
        description=(
            "Maximum number of compiled GBNF grammar definitions cached in memory for "
            "constrained generative naming (integer from 1 to 256 entries)."
        ),
    )
    SNAPSHOT_RETENTION_DAYS: int = Field(
        default=30,
        gt=0,
        description=(
            "Number of days (> 0) to retain historical sorting session records and "
            "rollback snapshots before automatic pruning."
        ),
    )
    PROTECTED_PATHS: list[str] = Field(
        default_factory=list,
        description=(
            "List of absolute literal directory paths protected from scanning, moving, "
            "or modification. Relative paths and wildcard or glob characters ('*', '?', '[', ']') are rejected."
        ),
    )
    AUTO_QUARANTINE_RATINGS: list[str] = Field(
        default_factory=list,
        description=(
            "Opt-in sensitivity severity ratings (such as 'CRITICAL' and 'HIGH') that "
            "automatically route detected files into compliance quarantine staging. "
            "Defaults to an empty list so automatic sensitivity quarantine is disabled until enabled."
        ),
    )
    QUARANTINE_DIR_NAME: str = Field(
        default="_Compliance_Quarantine",
        description=(
            "Directory name created inside the workspace for staging quarantined "
            "compliance files."
        ),
    )
    PROXY: str = Field(
        default="",
        description=(
            "Outbound network proxy URI used for model downloads and security advisory "
            "checks. Kept cleartext in memory during runtime, encrypted at rest in settings.json "
            "with an 'enc:' prefix (auto-migrating legacy plaintext values), and replaced with "
            "'<DECRYPTION_FAILED>' in memory if decryption fails without overwriting persisted ciphertext."
        ),
    )
    SECURITY_ADVISORY_ENDPOINT: str = Field(
        default="https://huggingface.co/api/models",
        description=(
            "HTTPS endpoint queried for model repository security metadata and "
            "published advisories before downloading or loading weights (defaults to "
            "Hugging Face Hub models API; supports custom HTTPS mirrors)."
        ),
    )
    TRUSTED_MODEL_HOSTS: list[str] = Field(
        default_factory=list,
        description=(
            "Additional trusted HTTPS hostnames permitted for model downloads and "
            "security advisory mirror queries alongside 'huggingface.co' and 'hf.co'. "
            "IP literals, private or loopback addresses, and non-HTTPS schemes remain blocked."
        ),
    )
    SECURITY_ADVISORY_CACHE_TTL: int = Field(
        default=3600,
        ge=1,
        description=(
            "Time-to-live in seconds (>= 1 second) for cached security advisory "
            "responses before re-querying the advisory endpoint."
        ),
    )
    SECURITY_ADVISORY_FALLBACK_POLICY: Literal["strict", "permissive"] = Field(
        default="strict",
        description=(
            "Fallback behavior when the security advisory endpoint is unreachable: "
            "'strict' refuses to load unverified models, whereas 'permissive' allows loading."
        ),
    )
    OCR_GPU_ENABLED: bool = Field(
        default=False,
        description=(
            "Whether to enable GPU hardware acceleration for EasyOCR and vision "
            "extraction workloads when a compatible accelerator is available."
        ),
    )
    AUDIO_GPU_ENABLED: bool = Field(
        default=False,
        description=(
            "Whether to enable GPU hardware acceleration for Whisper audio "
            "transcription workloads."
        ),
    )
    AUDIO_MAX_WORKERS: int = Field(
        default=2,
        gt=0,
        le=64,
        description=(
            "Maximum number of concurrent worker processes dedicated to audio "
            "transcription tasks (integer from 1 to 64 workers; aliased as MAX_AUDIO_WORKERS)."
        ),
    )

    @property
    def MAX_AUDIO_WORKERS(self) -> int:
        """Alias property getter for AUDIO_MAX_WORKERS."""
        return self.AUDIO_MAX_WORKERS

    @MAX_AUDIO_WORKERS.setter
    def MAX_AUDIO_WORKERS(self, val: int) -> None:
        self.AUDIO_MAX_WORKERS = val

    PLUGINS: list[str] = Field(
        default_factory=list,
        description=(
            "List of enabled plugin Python module identifiers. Entries must be valid "
            "dotted Python identifiers without path or shell characters; explicit dotted module "
            "paths must start with 'app.plugins.' or 'sortify_plugin_', and Python standard library "
            "or system modules are forbidden."
        ),
    )

    @field_validator("PLUGINS", mode="before")
    @classmethod
    def validate_plugins(cls, v: list[str]) -> list[str]:
        """Validate and sanitize plugin module path identifiers."""
        if not isinstance(v, (list, tuple, set)):
            raise ValueError("PLUGINS must be a list of strings.")
        validated = []
        stdlib_modules: set[str] = set(getattr(sys, "stdlib_module_names", set()))

        for entry in v:
            if not isinstance(entry, str):
                raise ValueError("Plugin entry must be a string.")
            cleaned = entry.strip()
            if not cleaned:
                raise ValueError("Plugin entry cannot be blank or whitespace-only.")

            if any(
                char in cleaned
                for char in ("/", "\\", "*", "?", "<", ">", "|", ":", ";", " ", "\t")
            ):
                raise ValueError(
                    f"Invalid characters in plugin identifier: '{cleaned}'"
                )

            parts = cleaned.split(".")
            if not all(part.isidentifier() for part in parts):
                raise ValueError(
                    f"Plugin entry '{cleaned}' is not a valid Python module identifier path."
                )

            if "." in cleaned and not cleaned.startswith(
                ("app.plugins.", "sortify_plugin_")
            ):
                raise ValueError(
                    f"Forbidden plugin module path '{cleaned}'. "
                    "Explicit plugin module paths must start with 'app.plugins.' or 'sortify_plugin_'."
                )

            first_part = parts[0]
            if cleaned in stdlib_modules or first_part in (
                "os",
                "sys",
                "subprocess",
                "builtins",
                "importlib",
                "shutil",
                "ctypes",
                "socket",
                "http",
            ):
                if not cleaned.startswith(("app.plugins.", "sortify_plugin_")):
                    raise ValueError(
                        f"Forbidden plugin module path '{cleaned}': standard library or system modules cannot be configured as plugins."
                    )

            if cleaned not in validated:
                validated.append(cleaned)
        return validated

    OCR_LANGUAGES: str = Field(
        default="en",
        description=(
            "Comma-separated list of non-empty OCR language codes (alphanumeric "
            "characters and underscores only, such as 'en' or 'ch_sim,en')."
        ),
    )
    VISION_ENGINE: Literal["easyocr", "florence-2"] = Field(
        default="easyocr",
        description=(
            "Visual text and caption extraction backend: 'easyocr' for standard OCR "
            "or 'florence-2' for vision-language document understanding."
        ),
    )
    CONFLICT_POLICY: Literal["skip", "rename", "overwrite"] = Field(
        default="rename",
        description=(
            "Resolution strategy when a destination file already exists: 'skip' leaves "
            "the source untouched, 'rename' appends a numeric suffix, and 'overwrite' replaces the target."
        ),
    )
    DUPLICATE_ACTION: Literal["off", "review", "skip", "stage", "trash"] = Field(
        default="off",
        description=(
            "Handling mode for exact byte-identical duplicate clusters during scan and sort: "
            "'off' treats copies normally, 'review' prompts in interactive TUI, 'skip' excludes "
            "redundant copies, 'stage' displaces copies into .sortify_duplicates/<session_id>/ "
            "for undo, and 'trash' stages verified copies for OS Trash disposal."
        ),
    )
    DETECT_ORIGINAL_BY: Literal[
        "first_seen",
        "name",
        "created",
        "lastmodified",
        "-first_seen",
        "-name",
        "-created",
        "-lastmodified",
    ] = Field(
        default="first_seen",
        description=(
            "Selector used to choose the surviving original file in each byte-identical duplicate "
            "cluster ('first_seen', 'name', 'created', 'lastmodified', or '-' reverse order variants)."
        ),
    )
    SORTING_STRATEGY: str = Field(
        default="default",
        description=(
            "Document clustering and taxonomy strategy identifier (for example 'default' "
            "TF-IDF/embedding clustering, 'generative', 'clinical_tmf', or 'clinical_isf')."
        ),
    )
    CLINICAL_SMART_RENAMING: bool = Field(
        default=False,
        description=(
            "Whether clinical sorting strategies standardize filenames using Trial "
            "Master File (TMF/ISF) study, artifact, and date metadata."
        ),
    )
    CLINICAL_GENERATE_AUDIT_REPORT: bool = Field(
        default=True,
        description=(
            "Whether clinical compliance runs automatically generate a regulatory "
            "audit trail report upon completion."
        ),
    )
    CLINICAL_SUPPRESS_DUPLICATES: bool = Field(
        default=False,
        description=(
            "Whether clinical multi-study ingestion skips copying byte-verified duplicate "
            "artifacts instead of creating numbered _1 copies while preserving manifest lineage."
        ),
    )
    COHERENCE_THRESHOLD: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description=(
            "Minimum semantic similarity coherence score (float from 0.0 to 1.0) "
            "required to group documents into a topic cluster before falling back to Miscellaneous."
        ),
    )
    DEBOUNCE_DELAY: float = Field(
        default=0.6,
        gt=0.0,
        description=(
            "Initial filesystem event debounce delay in seconds (> 0.0 and must be "
            "<= MAX_DEBOUNCE_DELAY) before the background daemon processes newly modified files."
        ),
    )
    MAX_DEBOUNCE_DELAY: float = Field(
        default=5.0,
        gt=0.0,
        description=(
            "Maximum ceiling in seconds (> 0.0 and must be >= DEBOUNCE_DELAY) that "
            "bursty filesystem events can defer daemon batch processing."
        ),
    )
    MAX_QUEUE_CAPACITY: int = Field(
        default=1000,
        gt=0,
        description=(
            "Maximum number of pending filesystem events (> 0 items) buffered in the "
            "background daemon priority queue before backpressure applies."
        ),
    )
    DEDUP_WINDOW: float = Field(
        default=0.5,
        ge=0.0,
        description=(
            "Time window in seconds (>= 0.0 seconds) used by the daemon watcher to "
            "coalesce duplicate filesystem events for the same path."
        ),
    )
    RECONCILIATION_INTERVAL: float = Field(
        default=30.0,
        gt=0.0,
        description=(
            "Periodic interval in seconds (> 0.0 seconds) between background daemon "
            "sweeps for missed files and incomplete ledger transactions."
        ),
    )
    WORKER_TIMEOUT: float = Field(
        default=300.0,
        ge=0.0,
        description=(
            "Maximum execution time in seconds (>= 0.0 seconds) allowed for background "
            "extraction, clustering, or forensic quarantine scan workers before timing out."
        ),
    )
    TABULAR_MAX_SHEETS: int = Field(
        default=10,
        gt=0,
        description=(
            "Maximum number of workbook sheets (> 0 sheets) sampled per spreadsheet "
            "during tabular text extraction (aliased as MAX_SPREADSHEET_SHEETS)."
        ),
    )
    TABULAR_MAX_ROWS: int = Field(
        default=10000,
        gt=0,
        description=(
            "Maximum number of rows (> 0 rows) read per sheet or CSV file during "
            "tabular text extraction (aliased as MAX_SPREADSHEET_ROWS)."
        ),
    )
    TABULAR_MAX_CHARACTERS: int = Field(
        default=50000,
        gt=0,
        description=(
            "Maximum total extracted character budget (> 0 characters) retained per "
            "tabular document (aliased as MAX_SPREADSHEET_CHARACTERS)."
        ),
    )

    @property
    def MAX_SPREADSHEET_SHEETS(self) -> int:
        """Alias for TABULAR_MAX_SHEETS."""
        return self.TABULAR_MAX_SHEETS

    @property
    def MAX_SPREADSHEET_ROWS(self) -> int:
        """Alias for TABULAR_MAX_ROWS."""
        return self.TABULAR_MAX_ROWS

    @property
    def MAX_SPREADSHEET_CHARACTERS(self) -> int:
        """Alias for TABULAR_MAX_CHARACTERS."""
        return self.TABULAR_MAX_CHARACTERS

    IGNORED_EXTENSIONS: list[Annotated[str, Field(min_length=1, pattern=r"^\..+")]] = (
        Field(
            default=[".crdownload", ".tmp", ".download"],
            description=(
                "List of temporary or incomplete file extensions skipped during scanning "
                "and daemon watching (non-empty strings normalized with a leading '.', such as '.tmp')."
            ),
        )
    )

    @field_validator("IGNORED_EXTENSIONS", mode="before")
    @classmethod
    def validate_ignored_extensions(cls, v: list[str]) -> list[str]:
        """Validate that IGNORED_EXTENSIONS contains non-empty strings and format leading dots."""
        if not isinstance(v, list):
            raise ValueError("IGNORED_EXTENSIONS must be a list.")
        validated = []
        for ext in v:
            if not isinstance(ext, str):
                raise ValueError("Ignored extension entry must be a string.")
            cleaned = ext.strip()
            if not cleaned or cleaned == ".":
                raise ValueError(
                    "Ignored extension entry cannot be blank or whitespace-only."
                )
            if not cleaned.startswith("."):
                cleaned = f".{cleaned}"
            if cleaned not in validated:
                validated.append(cleaned)
        return validated

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
        """Validate that CONFLICT_POLICY is 'skip', 'rename', or 'overwrite'."""
        if not isinstance(v, str):
            raise ValueError("CONFLICT_POLICY must be a string.")
        if v not in ("skip", "rename", "overwrite"):
            raise ValueError(
                "CONFLICT_POLICY must be 'skip', 'rename', or 'overwrite'."
            )
        return v

    @field_validator("DUPLICATE_ACTION", mode="before")
    @classmethod
    def validate_duplicate_action(cls, v: str) -> str:
        """Validate that DUPLICATE_ACTION is a supported duplicate handling mode."""
        if not isinstance(v, str):
            raise ValueError("DUPLICATE_ACTION must be a string.")
        v_clean = v.strip().lower()
        if v_clean not in ("off", "review", "skip", "stage", "trash"):
            raise ValueError(
                "DUPLICATE_ACTION must be 'off', 'review', 'skip', 'stage', or 'trash'."
            )
        return v_clean

    @field_validator("DETECT_ORIGINAL_BY", mode="before")
    @classmethod
    def validate_detect_original_by(cls, v: str) -> str:
        """Validate that DETECT_ORIGINAL_BY is a supported original selector."""
        from app.core.resilient_file_ops import DETECT_ORIGINAL_BY_MODES

        if not isinstance(v, str):
            raise ValueError("DETECT_ORIGINAL_BY must be a string.")
        v_clean = v.strip().lower()
        if v_clean not in DETECT_ORIGINAL_BY_MODES:
            raise ValueError(
                f"DETECT_ORIGINAL_BY must be one of {list(DETECT_ORIGINAL_BY_MODES)}."
            )
        return v_clean

    @field_validator("SECURITY_ADVISORY_FALLBACK_POLICY")
    @classmethod
    def validate_advisory_fallback_policy(cls, v: str) -> str:
        """Validate that SECURITY_ADVISORY_FALLBACK_POLICY is either 'strict' or 'permissive'."""
        if not isinstance(v, str):
            raise ValueError("SECURITY_ADVISORY_FALLBACK_POLICY must be a string.")
        v_clean = v.strip().lower()
        if v_clean not in ("strict", "permissive"):
            raise ValueError(
                "SECURITY_ADVISORY_FALLBACK_POLICY must be either 'strict' or 'permissive'."
            )
        return v_clean

    @field_validator("VISION_ENGINE", mode="before")
    @classmethod
    def validate_vision_engine(cls, v: str) -> str:
        """Validate that VISION_ENGINE is either 'easyocr' or 'florence-2'."""
        if not isinstance(v, str):
            raise ValueError("VISION_ENGINE must be a string.")
        v_clean = v.strip().lower()
        if v_clean not in ("easyocr", "florence-2"):
            raise ValueError("VISION_ENGINE must be either 'easyocr' or 'florence-2'.")
        return v_clean

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

    @field_validator("RENAME_PATTERN")
    @classmethod
    def validate_rename_pattern_setting(cls, v: str) -> str:
        """Validate that RENAME_PATTERN is a safe filename template without path separators or illegal chars."""
        from app.core.path_utils import ILLEGAL_PATH_CHARS_SET

        if not isinstance(v, str):
            raise ValueError("RENAME_PATTERN must be a string.")
        cleaned = v.strip()
        if not cleaned:
            return ""
        if "/" in cleaned or "\\" in cleaned or ".." in cleaned or "\x00" in cleaned:
            raise ValueError(
                "RENAME_PATTERN cannot contain path separators, '..', or null bytes."
            )
        if any(ch in ILLEGAL_PATH_CHARS_SET for ch in cleaned):
            raise ValueError("RENAME_PATTERN contains illegal filename characters.")
        return cleaned

    @field_validator("KEYWORD_RULES", "LEARNED_RULES", "DISABLED_KEYWORD_RULES")
    @classmethod
    def validate_keyword_rules(cls, v: dict) -> dict:
        """Validate that keyword routing rules and learned rules have valid target paths."""
        from app.core.path_utils import validate_target_path
        from app.core.pattern_formatter import PatternTokenFormatter

        for keyword, target_path in v.items():
            validate_target_path(target_path, keyword=keyword)
            if isinstance(target_path, str) and "{" in target_path:
                PatternTokenFormatter.format_target_path(
                    target_path,
                    metadata={
                        "date": "2025-01-15",
                        "category": "Category",
                        "stem": "sample",
                        "extension": ".txt",
                    },
                    seq=1,
                )
        return v

    @field_validator("POLICIES", mode="before")
    @classmethod
    def validate_policies(cls, v: list[dict]) -> list[dict]:
        """Validate that unified policies have valid target paths, types, expression, and priority, and check for overlaps."""
        from app.core.path_utils import ILLEGAL_PATH_CHARS_SET, validate_target_path
        from app.core.pattern_formatter import PatternTokenFormatter
        from app.core.policy_engine import (
            METADATA_POLICY_TYPES,
            PolicyEngine,
            validate_metadata_expression,
        )

        if not isinstance(v, list):
            raise ValueError("POLICIES must be a list.")

        valid_rule_types = (
            "keyword",
            "pattern",
            "override",
            "action",
            "compliance",
            "unarchive",
            "duplicate",
            "age",
            "modified_date",
            "created_date",
            "exif_date",
            "size",
        )

        normalized_policies = []
        for rule in v:
            if not isinstance(rule, dict):
                raise ValueError("Each policy must be a dictionary.")
            rule_type = rule.get("type")
            if rule_type not in valid_rule_types:
                raise ValueError(
                    f"Invalid policy type: {rule_type}. Must be one of {', '.join(valid_rule_types)}."
                )

            expression = rule.get("expression")
            defaulted_expression = False
            if rule_type == "duplicate" and (
                expression is None
                or (isinstance(expression, str) and not expression.strip())
            ):
                expression = "*"
                defaulted_expression = True
            elif not isinstance(expression, str) or not expression.strip():
                raise ValueError("Policy expression must be a non-empty string.")

            if rule_type in METADATA_POLICY_TYPES:
                validate_metadata_expression(rule_type, expression)

            action = rule.get("action")
            action_lower = action.lower() if isinstance(action, str) else None
            allows_empty_target = action_lower == "trash" or rule_type == "duplicate"

            target_path = rule.get("target_path")
            defaulted_target = False
            if target_path is None and allows_empty_target:
                target_path = ""
                defaulted_target = True
            if target_path is None or not isinstance(target_path, str):
                raise ValueError("Policy target_path must be a string.")
            if target_path != "" or not allows_empty_target:
                validate_target_path(target_path, keyword=expression)
            if target_path != "" and "{" in target_path:
                PatternTokenFormatter.format_target_path(
                    target_path,
                    metadata={
                        "date": "2025-01-15",
                        "category": "Category",
                        "stem": "sample",
                        "extension": ".txt",
                    },
                    seq=1,
                )

            rename_pat = rule.get("rename_pattern")
            if rename_pat is not None:
                if not isinstance(rename_pat, str):
                    raise ValueError("Policy rename_pattern must be a string.")
                rp_clean = rename_pat.strip()
                if rp_clean:
                    if (
                        "/" in rp_clean
                        or "\\" in rp_clean
                        or ".." in rp_clean
                        or "\x00" in rp_clean
                        or any(ch in ILLEGAL_PATH_CHARS_SET for ch in rp_clean)
                    ):
                        raise ValueError(
                            f"Invalid policy rename_pattern '{rename_pat}': path traversal or illegal filename characters are forbidden."
                        )

            if (
                "priority" not in rule
                or not isinstance(rule["priority"], int)
                or isinstance(rule["priority"], bool)
            ):
                raise ValueError("Policy must have an integer priority.")

            if "halting" in rule and not isinstance(rule["halting"], bool):
                raise ValueError("Policy halting property must be a boolean.")

            if "enabled" in rule and not isinstance(rule["enabled"], bool):
                raise ValueError("Policy enabled property must be a boolean.")

            if action is not None:
                if not isinstance(
                    action, str
                ) or not PolicyEngine.validate_policy_action(action):
                    raise ValueError(
                        f"Invalid policy action: {action}. Must be one of {sorted(PolicyEngine.SUPPORTED_ACTIONS)}."
                    )

            detect_by = rule.get("detect_original_by")
            if detect_by is not None:
                from app.core.resilient_file_ops import DETECT_ORIGINAL_BY_MODES

                if (
                    not isinstance(detect_by, str)
                    or detect_by.strip().lower() not in DETECT_ORIGINAL_BY_MODES
                ):
                    raise ValueError(
                        f"Invalid detect_original_by: {detect_by}. Must be one of {list(DETECT_ORIGINAL_BY_MODES)}."
                    )

            post_act = (
                rule.get("post_unarchive_action")
                or rule.get("post_action")
                or rule.get("archive_action")
            )
            if post_act is not None:
                if not isinstance(post_act, str) or post_act.lower() not in (
                    "delete",
                    "remove",
                    "cleanup",
                    "move",
                    "archive",
                    "relocate",
                    "retain",
                    "keep",
                ):
                    raise ValueError(f"Invalid post_unarchive_action: {post_act}.")

            needs_copy = (
                defaulted_expression
                or defaulted_target
                or ("action" in rule and rule["action"] is None)
                or (isinstance(action, str) and action != action.lower())
                or ("rename_pattern" in rule and rule["rename_pattern"] is None)
                or (
                    isinstance(detect_by, str)
                    and detect_by != detect_by.strip().lower()
                )
                or (
                    "post_unarchive_action" in rule
                    and rule["post_unarchive_action"] is None
                )
                or (
                    isinstance(rule.get("post_unarchive_action"), str)
                    and rule["post_unarchive_action"]
                    != rule["post_unarchive_action"].lower()
                )
            )
            if needs_copy:
                updated_rule = dict(rule)
                if defaulted_expression:
                    updated_rule["expression"] = expression
                if defaulted_target:
                    updated_rule["target_path"] = target_path
                if "action" in updated_rule:
                    if updated_rule["action"] is None:
                        updated_rule.pop("action")
                    elif isinstance(updated_rule["action"], str):
                        updated_rule["action"] = updated_rule["action"].lower()
                if (
                    "rename_pattern" in updated_rule
                    and updated_rule["rename_pattern"] is None
                ):
                    updated_rule.pop("rename_pattern")
                if isinstance(updated_rule.get("detect_original_by"), str):
                    updated_rule["detect_original_by"] = (
                        updated_rule["detect_original_by"].strip().lower()
                    )
                if "post_unarchive_action" in updated_rule:
                    if updated_rule["post_unarchive_action"] is None:
                        updated_rule.pop("post_unarchive_action")
                    elif isinstance(updated_rule["post_unarchive_action"], str):
                        updated_rule["post_unarchive_action"] = updated_rule[
                            "post_unarchive_action"
                        ].lower()
                normalized_policies.append(updated_rule)
            else:
                normalized_policies.append(rule)

        # Overlap check
        sorted_policies = sorted(
            normalized_policies, key=lambda x: x.get("priority", 0), reverse=True
        )

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

        return normalized_policies

    AI_CONSENT_GRANTED: bool | None = Field(
        default=None,
        description=(
            "User consent state for local AI model downloads and semantic vector "
            "indexing: true (granted), false (declined; unloads models and scrubs vectors), "
            "or null (not yet prompted)."
        ),
    )
    LOG_FILE: str = Field(
        default_factory=lambda: str(_resolve_app_dir() / "autosorter.log"),
        json_schema_extra={"default": "autosorter.log"},
        min_length=1,
        description=(
            "Filesystem path to the primary rotating application log file "
            "(non-empty string; defaults to 'autosorter.log' inside the app directory)."
        ),
    )
    STOP_WORDS: set[str] = Field(
        default={
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
        },
        description=(
            "Set of common noise and file-extension tokens excluded during TF-IDF "
            "keyword extraction and cluster folder naming."
        ),
    )

    # Only SORTIFY_-prefixed variables apply, and `.env` is read from the app
    # directory (see `app_env_file()`), never from the working directory.
    model_config = SettingsConfigDict(
        env_prefix="SORTIFY_",
        env_file=None,
        env_file_encoding="utf-8",
        extra="ignore",
        validate_assignment=True,
    )


def app_env_file() -> Path:
    """Return the optional `.env` file that may hold SORTIFY_ settings."""
    return _resolve_app_dir() / ".env"


def _env_sourced_keys(env_file: Path) -> set[str]:
    """Return the setting names that the environment or `env_file` provides."""
    from pydantic_settings import DotEnvSettingsSource, EnvSettingsSource

    keys = set(EnvSettingsSource(Settings)().keys())
    if env_file.is_file():
        keys |= set(DotEnvSettingsSource(Settings, env_file=env_file)().keys())
    return {k for k in keys if k in Settings.model_fields}


class AppSettings:
    """A registry for application settings that provides persistence and validation."""

    _class_observers: dict[Any, list[Any]] = defaultdict(list)
    _class_observer_lock = threading.Lock()
    _file_locks: dict[str, threading.RLock] = {}
    _active_file_timers: dict[str, threading.Timer] = {}
    _file_registry_lock = threading.Lock()

    @classmethod
    def _get_file_lock(cls, filepath: str) -> threading.RLock:
        norm_path = os.path.abspath(filepath)
        with cls._file_registry_lock:
            lock = cls._file_locks.get(norm_path)
            if lock is None:
                lock = threading.RLock()
                cls._file_locks[norm_path] = lock
            return lock

    @classmethod
    def _cancel_file_timer(cls, filepath: str, current_timer: Any = None) -> None:
        norm_path = os.path.abspath(filepath)
        with cls._file_registry_lock:
            existing = cls._active_file_timers.get(norm_path)
            if existing is not None and existing is not current_timer:
                existing.cancel()
            if existing is not None:
                cls._active_file_timers.pop(norm_path, None)

    def __init__(self, filepath=None):
        self._filepath = filepath or str(get_app_dir() / "settings.json")
        self._save_timer = None
        self._lock = threading.Lock()
        # Per-run overrides (CLI flags): name -> the persisted JSON value that
        # saves write instead, so a one-off flag never becomes the new default.
        self._transient_values: dict[str, Any] = {}
        self._raw_encrypted_proxy = None
        self._validation_errors = []
        self._observers = defaultdict(list)
        self._observer_lock = threading.Lock()

        env_file = app_env_file()
        try:
            self._settings_model = Settings(_env_file=env_file)
            # Environment values apply each run but are never written to
            # settings.json, unless the setting is explicitly changed.
            self._env_sourced_keys = _env_sourced_keys(env_file)
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
        file_lock = self._get_file_lock(self._filepath)
        try:
            with file_lock:
                with open(self._filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)

            # Decrypt PROXY setting if encrypted, or mark for migration if legacy plaintext
            if isinstance(data, dict) and "PROXY" in data:
                proxy_val = data["PROXY"]
                if proxy_val:
                    from app.core.crypto import CryptoManager

                    if CryptoManager.is_encrypted_proxy(proxy_val):
                        self._raw_encrypted_proxy = proxy_val
                        try:
                            from app.core.path_utils import resolve_db_crypto

                            crypto = resolve_db_crypto(self._filepath)
                            decrypted_val = CryptoManager.decrypt_proxy_setting(
                                proxy_val, crypto=crypto
                            )
                            data["PROXY"] = decrypted_val
                        except Exception as e:
                            logging.warning(
                                f"Failed to decrypt proxy settings, replacing with placeholder: {e}",
                                exc_info=True,
                            )
                            data["PROXY"] = "<DECRYPTION_FAILED>"
                    else:
                        needs_migration = True

            # Validate against static schema file if it exists
            validator = _get_schema_validator()
            if validator is not None:
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
                        self._env_sourced_keys.discard(key)
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

            if hasattr(self._settings_model, "PROXY"):
                self._notify_observers("PROXY", getattr(self._settings_model, "PROXY"))

        except Exception as e:
            logging.warning(
                f"Failed to load settings, using defaults: {e}", exc_info=True
            )
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
            self._cancel_file_timer(self._filepath)
            timer = threading.Timer(0.5, self._save)
            # Ensure background thread doesn't block app exit
            timer.daemon = True
            self._save_timer = timer
            with self._file_registry_lock:
                self._active_file_timers[os.path.abspath(self._filepath)] = timer
            timer.start()

    def flush(self) -> None:
        """Write a pending debounced save now.

        The debounce timer is a daemon thread, so a process that exits right
        after changing a setting would otherwise lose the change.
        """
        with self._lock:
            timer = self._save_timer
            self._save_timer = None
        if timer is not None:
            timer.cancel()
            self._save()

    def override(self, name: str, value: Any) -> None:
        """Apply a value for this run only, without persisting it.

        A later explicit assignment (``settings.NAME = value``) persists as usual.
        """
        if name not in self._transient_values:
            self._transient_values[name] = self._settings_model.model_dump(
                mode="json"
            ).get(name)
        setattr(self._settings_model, name, value)
        self._notify_observers(name, getattr(self._settings_model, name, value))

    def _save(self):
        current_thread = threading.current_thread()
        with self._lock:
            if self._save_timer is not None and self._save_timer is not current_thread:
                self._save_timer.cancel()
            self._save_timer = None
            self._cancel_file_timer(self._filepath, current_timer=current_thread)
            data = self._settings_model.model_dump(mode="json")
            data.update(self._transient_values)
            for key in self._env_sourced_keys:
                data.pop(key, None)
        try:
            # Encrypt PROXY setting if present and not already encrypted
            proxy_val = data.get("PROXY", "")
            if proxy_val == "<DECRYPTION_FAILED>":
                if self._raw_encrypted_proxy:
                    data["PROXY"] = self._raw_encrypted_proxy
                else:
                    data.pop("PROXY", None)
            elif proxy_val:
                from app.core.crypto import CryptoManager

                if not CryptoManager.is_encrypted_proxy(proxy_val):
                    try:
                        from app.core.path_utils import resolve_db_crypto

                        crypto = resolve_db_crypto(self._filepath)
                        data["PROXY"] = CryptoManager.encrypt_proxy_setting(
                            proxy_val, crypto=crypto
                        )
                    except Exception:  # noqa: BLE001 - any key/encryption failure must keep credentials off disk and out of logs
                        if self._raw_encrypted_proxy:
                            data["PROXY"] = self._raw_encrypted_proxy
                        else:
                            data.pop("PROXY", None)
                        logging.warning(
                            "Proxy encryption failed; the current proxy was not "
                            "persisted. It remains available for this session."
                        )

            file_lock = self._get_file_lock(self._filepath)
            with file_lock:
                parent_dir = os.path.dirname(self._filepath)
                if parent_dir and not os.path.exists(parent_dir):
                    os.makedirs(parent_dir, exist_ok=True)

                with open(self._filepath, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=4)
                    f.flush()
            # Keep the last persisted ciphertext even when an edited proxy
            # cannot be encrypted. Clearing the proxy also clears this fallback.
            persisted_proxy = data.get("PROXY", "")
            self._raw_encrypted_proxy = (
                persisted_proxy if persisted_proxy.startswith("enc:") else None
            )
        except Exception as e:
            logging.error(f"Failed to save settings: {e}", exc_info=True)
            raise

    def revalidate(self) -> bool:
        """Re-check all current settings values against JSON schema and Pydantic validation.

        If re-validation succeeds, unlocks background saving and triggers a save.
        Returns True if valid, False if errors remain.
        """
        errors = []
        data = self._settings_model.model_dump(mode="json")

        # Validate against static schema file if it exists
        validator = _get_schema_validator()
        if validator is not None:
            try:
                schema_errors = sorted(
                    validator.iter_errors(data), key=lambda e: e.path
                )
                for error in schema_errors:
                    path = (
                        ".".join([str(p) for p in error.path]) if error.path else "root"
                    )
                    errors.append({"field": path, "message": error.message})
            except Exception as e:
                # Fail closed: an unexpected validator crash counts as an error.
                logging.warning("JSON schema validation raised", exc_info=True)
                errors.append({"field": "schema", "message": str(e)})

        # Validate against Settings Pydantic model
        try:
            Settings(**data)
        except (ValueError, ValidationError) as e:
            if isinstance(e, ValidationError):
                for err in e.errors():
                    loc = err.get("loc", [])
                    path = ".".join([str(p) for p in loc]) if loc else "root"
                    msg = err.get("msg", str(err))
                    if not any(
                        item["field"] == path and item["message"] == msg
                        for item in errors
                    ):
                        errors.append({"field": path, "message": msg})
            else:
                errors.append({"field": "model", "message": str(e)})

        if not errors:
            self._has_validation_errors = False
            self._validation_errors = []
            self._trigger_save()
            return True
        else:
            self._has_validation_errors = True
            self._validation_errors = errors
            return False

    @classmethod
    def add_observer(cls, key: str, callback: Callable) -> None:
        """Register a callback to be notified when setting `key` changes."""
        with cls._class_observer_lock:
            if callback not in cls._class_observers[key]:
                cls._class_observers[key].append(callback)

    @classmethod
    def remove_observer(cls, key: str, callback: Callable) -> None:
        """Unregister a setting change callback."""
        with cls._class_observer_lock:
            if callback in cls._class_observers[key]:
                cls._class_observers[key].remove(callback)

    @classmethod
    def clear_observers(cls, key: str | None = None) -> None:
        """Clear all registered observers or observers for a specific key."""
        with cls._class_observer_lock:
            if key is None:
                cls._class_observers.clear()
            elif key in cls._class_observers:
                cls._class_observers[key].clear()

    def _notify_observers(self, key: str, value: Any) -> None:
        """Notify registered setting change observers for `key`."""
        if key == "AI_CONSENT_GRANTED" and value is not True:
            try:
                from app.core.shared_registry import SharedModelRegistry

                SharedModelRegistry.get_instance().unload_all_models()
            except Exception as e:
                logging.debug(
                    f"Failed to unload in-memory models on consent change: {e}",
                    exc_info=True,
                )
            try:
                from app.core.db import Database

                Database.scrub_all_vectors()
            except Exception as e:
                logging.debug(
                    f"Failed to scrub database vectors on consent change: {e}",
                    exc_info=True,
                )

        callbacks = []
        if hasattr(self, "_observer_lock") and hasattr(self, "_observers"):
            with self._observer_lock:
                callbacks.extend(self._observers.get(key, []))
                callbacks.extend(self._observers.get("*", []))
        with AppSettings._class_observer_lock:
            callbacks.extend(AppSettings._class_observers.get(key, []))
            callbacks.extend(AppSettings._class_observers.get("*", []))

        seen = set()
        unique_callbacks = []
        for cb in callbacks:
            cb_id = id(cb)
            if cb_id not in seen:
                seen.add(cb_id)
                unique_callbacks.append(cb)

        for cb in unique_callbacks:
            try:
                self._invoke_observer_callback(cb, key, value)
            except Exception as e:
                logging.error(
                    f"Error executing setting observer callback for '{key}': {e}",
                    exc_info=True,
                )

    def _invoke_observer_callback(
        self, callback: Callable, key: str, value: Any
    ) -> None:
        import inspect

        try:
            sig = inspect.signature(callback)
            params_count = len(sig.parameters)
            if params_count == 0:
                callback()
            elif params_count == 1:
                callback(value)
            else:
                callback(key, value)
        except Exception:
            logging.debug(
                f"Observer callback for '{key}' failed; retrying other arities",
                exc_info=True,
            )
            try:
                callback(value)
            except TypeError:
                try:
                    callback(key, value)
                except TypeError:
                    callback()

    def __getattr__(self, name):
        """Get attribute dynamically from the settings model."""
        if name == "MAX_AUDIO_WORKERS":
            return getattr(self._settings_model, "AUDIO_MAX_WORKERS", 2)
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
            "_observers",
            "_observer_lock",
            "_transient_values",
            "_env_sourced_keys",
        ):
            super().__setattr__(name, value)
        else:
            if name == "MAX_AUDIO_WORKERS":
                name = "AUDIO_MAX_WORKERS"
            self._transient_values.pop(name, None)
            self._env_sourced_keys.discard(name)
            setattr(self._settings_model, name, value)
            self.revalidate()
            new_val = getattr(self._settings_model, name, value)
            self._notify_observers(name, new_val)


def _on_ai_consent_changed_core(key: str, value: Any) -> None:
    """Core observer callback: unload all in-memory AI models and scrub database vectors when consent is revoked or disabled."""
    if value is not True:
        try:
            from app.core.shared_registry import SharedModelRegistry

            SharedModelRegistry.get_instance().unload_all_models()
        except Exception as e:
            logging.debug(
                f"Failed to unload in-memory models on consent change: {e}",
                exc_info=True,
            )

        try:
            from app.core.db import Database

            Database.scrub_all_vectors()
        except Exception as e:
            logging.debug(
                f"Failed to scrub database vectors on consent change: {e}",
                exc_info=True,
            )


AppSettings.add_observer("AI_CONSENT_GRANTED", _on_ai_consent_changed_core)
