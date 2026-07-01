"""Configuration settings for the autosorter application.

This module contains the SettingsRegistry for managing dynamic configuration.
"""
import json
import logging
import os
import threading
from typing import Set, Union


class SettingsRegistry:
    """A registry for application settings that provides persistence and validation."""

    def __init__(self, filepath="settings.json"):
        self._filepath = filepath
        self._save_timer = None
        self._lock = threading.Lock()
        
        # Defaults
        self._MAX_FOLDERS = 12
        self._MAX_WORKERS = 15
        self._MIN_DF = 2
        self._MAX_DF = 0.85
        self._LOG_FILE = "autosorter.log"
        self._STOP_WORDS = {
            "the", "and", "for", "this", "that", "with", "from", "inc", "com", "pdf", 
            "docx", "txt", "csv", "xlsx", "xls", "site", "team", "page", "nan", "unnamed", 
            "your", "have", "will", "are", "not", "can", "all", "was", "has", "but", "what", 
            "there", "out", "about", "get", "would", "like", "which", "their", "when", 
            "who", "some", "how", "these", "into", "other", "could", "than", "only", 
            "also", "over", "well", "because", "through", "don", "should", "been", 
            "much", "where"
        }
        
        self.load()

    def load(self):
        """Load settings from the configuration file."""
        if not os.path.exists(self._filepath):
            self._trigger_save()
            return
            
        try:
            with open(self._filepath, "r") as f:
                data = json.load(f)
                
            for key in ["MAX_FOLDERS", "MAX_WORKERS", "MIN_DF", "MAX_DF", "LOG_FILE"]:
                if key in data:
                    try:
                        setattr(self, key, data[key])
                    except ValueError as e:
                        logging.warning(f"Invalid {key} in config, using default: {e}")
                        
            if "STOP_WORDS" in data:
                try:
                    self.STOP_WORDS = set(data["STOP_WORDS"])
                except ValueError as e:
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
                "MAX_FOLDERS": self._MAX_FOLDERS,
                "MAX_WORKERS": self._MAX_WORKERS,
                "MIN_DF": self._MIN_DF,
                "MAX_DF": self._MAX_DF,
                "LOG_FILE": self._LOG_FILE,
                "STOP_WORDS": list(self._STOP_WORDS)
            }
        try:
            with open(self._filepath, "w") as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logging.error(f"Failed to save settings: {e}")

    @property
    def MAX_FOLDERS(self) -> int:
        """Get the maximum number of folders."""
        return self._MAX_FOLDERS
        
    @MAX_FOLDERS.setter
    def MAX_FOLDERS(self, value: int):
        if not isinstance(value, int) or value <= 0:
            raise ValueError("MAX_FOLDERS must be a positive integer")
        self._MAX_FOLDERS = value
        self._trigger_save()
        
    @property
    def MAX_WORKERS(self) -> int:
        """Get the maximum number of worker threads."""
        return self._MAX_WORKERS
        
    @MAX_WORKERS.setter
    def MAX_WORKERS(self, value: int):
        if not isinstance(value, int) or value <= 0:
            raise ValueError("MAX_WORKERS must be a positive integer")
        self._MAX_WORKERS = value
        self._trigger_save()
        
    @property
    def MIN_DF(self) -> Union[int, float]:
        """Get the minimum document frequency."""
        return self._MIN_DF
        
    @MIN_DF.setter
    def MIN_DF(self, value: Union[int, float]):
        if not isinstance(value, (int, float)) or value < 0:
            raise ValueError("MIN_DF must be a non-negative number")
        self._MIN_DF = value
        self._trigger_save()
        
    @property
    def MAX_DF(self) -> float:
        """Get the maximum document frequency."""
        return self._MAX_DF
        
    @MAX_DF.setter
    def MAX_DF(self, value: Union[int, float]):
        if not isinstance(value, (int, float)) or not (0 <= value <= 1):
            raise ValueError("MAX_DF must be a float between 0 and 1")
        self._MAX_DF = float(value)
        self._trigger_save()
        
    @property
    def LOG_FILE(self) -> str:
        """Get the central log file path."""
        return self._LOG_FILE
        
    @LOG_FILE.setter
    def LOG_FILE(self, value: str):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("LOG_FILE must be a non-empty string")
        self._LOG_FILE = value
        self._trigger_save()
        
    @property
    def STOP_WORDS(self) -> Set[str]:
        """Get the set of stop words to filter out."""
        return self._STOP_WORDS
        
    @STOP_WORDS.setter
    def STOP_WORDS(self, value: Set[str]):
        if not isinstance(value, set):
            try:
                value = set(value)
            except Exception:
                raise ValueError("STOP_WORDS must be a set of strings")
        if not all(isinstance(x, str) for x in value):
            raise ValueError("STOP_WORDS must be a set of strings")
        self._STOP_WORDS = value
        self._trigger_save()

settings = SettingsRegistry()
