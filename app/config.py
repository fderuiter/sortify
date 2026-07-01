"""Configuration settings for the autosorter application.

This module contains various settings and constants used throughout the
application, including max folders, thread workers, ML parameters, and
stop words.
"""

import sys

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings schema."""

    MAX_FOLDERS: int = 12
    MAX_WORKERS: int = 15
    LOG_FILE: str = "autosorter.log"
    STOP_WORDS: set[str] = {
        "the", "and", "for", "this", "that", "with", "from", "inc", "com",
        "pdf", "docx", "txt", "csv", "xlsx", "xls", "site", "team", "page",
        "nan", "unnamed", "your", "have", "will", "are", "not", "can", "all",
        "was", "has", "but", "what", "there", "out", "about", "get", "would",
        "like", "which", "their", "when", "who", "some", "how", "these", "into",
        "other", "could", "than", "only", "also", "over", "well", "because",
        "through", "don", "should", "been", "much", "where",
    }

    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')


try:
    settings = Settings()
except ValidationError as e:
    print(f"Configuration error: {e}", file=sys.stderr)
    sys.exit(1)
