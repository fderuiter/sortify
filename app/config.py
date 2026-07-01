"""Configuration settings for the autosorter application.

This module contains various settings and constants used throughout the
application, including max folders, thread workers, ML parameters, and
stop words.
"""


import json
import logging
import os

MAX_FOLDERS = 12
MAX_WORKERS = 15

# ML Settings
MIN_DF = 2
MAX_DF = 0.85

# Centralized Logging File
LOG_FILE = "autosorter.log"

SETTINGS_FILE = "settings.json"

DEFAULT_STOP_WORDS = {
    "the", "and", "for", "this", "that", "with", "from", "inc", "com",
    "pdf", "docx", "txt", "csv", "xlsx", "xls", "site", "team", "page",
    "nan", "unnamed", "your", "have", "will", "are", "not", "can", "all",
    "was", "has", "but", "what", "there", "out", "about", "get", "would",
    "like", "which", "their", "when", "who", "some", "how", "these", "into",
    "other", "could", "than", "only", "also", "over", "well", "because",
    "through", "don", "should", "been", "much", "where",
}

STOP_WORDS = set(DEFAULT_STOP_WORDS)

def load_settings():
    """Load settings from the persistent settings file."""
    global STOP_WORDS
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, 'r') as f:
                data = json.load(f)
                if "stop_words" in data:
                    STOP_WORDS.clear()
                    STOP_WORDS.update(data["stop_words"])
        except Exception as e:
            logging.error(f"Failed to load settings: {e}")

def save_settings():
    """Save current settings to the persistent settings file."""
    try:
        with open(SETTINGS_FILE, 'w') as f:
            json.dump({"stop_words": list(STOP_WORDS)}, f)
    except Exception as e:
        logging.error(f"Failed to save settings: {e}")

load_settings()

