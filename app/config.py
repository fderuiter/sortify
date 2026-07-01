"""Configuration settings for the autosorter application.

This module contains various settings and constants used throughout the
application, including max folders, thread workers, ML parameters, and
stop words.
"""

class AppSettings:
    def __init__(self):
        self.MAX_FOLDERS = 12
        self.MAX_WORKERS = 15

        # ML Settings
        self.MIN_DF = 2
        self.MAX_DF = 0.85

        # Centralized Logging File
        self.LOG_FILE = "autosorter.log"

        self.STOP_WORDS = {
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
