"""Global log filter to scrub sensitive user paths from log output."""

import logging
import os
import re
import traceback
from pathlib import Path


def scrub_diagnostic_text(text: str, home_dir: str = None) -> str:
    """Scrub user home directory paths and sensitive credential tokens from diagnostic text."""
    if not isinstance(text, str) or not text:
        return text

    if home_dir is None:
        try:
            home_dir = str(Path.home())
        except Exception:
            home_dir = None

    if home_dir and home_dir not in ("/", "\\", ""):
        home_dirs = [home_dir]
        try:
            expanded = os.path.expanduser("~")
            if expanded and expanded not in home_dirs:
                home_dirs.append(expanded)
        except Exception:
            pass

        for h in home_dirs:
            if not h or h in ("/", "\\"):
                continue
            fwd = h.replace("\\", "/")
            back = h.replace("/", "\\")
            dbl_back = back.replace("\\", "\\\\")
            text = text.replace(dbl_back, "<USER_HOME>")
            text = text.replace(back, "<USER_HOME>")
            text = text.replace(fwd, "<USER_HOME>")

    # Strip sensitive credential tokens and prefixes
    # 1. Remove encrypted credential tokens and prefixes starting with 'enc:'
    text = re.sub(r"\benc:[^\s,;'\"]*", "", text)
    text = re.sub(r"enc:[^\s,;'\"]*", "", text)

    # 2. Strip/mask Bearer tokens and sensitive key=value pairs (passwords, tokens, secrets, api keys)
    text = re.sub(r"(?i)\b(bearer)\s+[a-zA-Z0-9._~+/-]+=*", "", text)
    text = re.sub(
        r"(?i)\b(password|passwd|secret|api_key|apikey|access_token|auth_token)\s*=\s*[^\s,;'\"]+",
        r"\1=[REDACTED]",
        text,
    )

    return text


class LogScrubbingFilter(logging.Filter):
    """Filter that removes user home directory paths from all log records."""

    def __init__(self, home_dir: str):
        super().__init__()
        self.home_dir = home_dir
        self.home_dir_fwd = home_dir.replace("\\", "/")
        self.home_dir_back = home_dir.replace("/", "\\")

    def _scrub(self, text: str) -> str:
        if not isinstance(text, str):
            return text
        return scrub_diagnostic_text(text, self.home_dir)

    def _scrub_arg(self, arg):
        if isinstance(arg, str):
            return self._scrub(arg)
        elif hasattr(arg, "__fspath__"):
            return self._scrub(str(arg))
        return arg

    def _has_encrypted_credentials(self, text: str) -> bool:
        if not isinstance(text, str):
            return False
        return "enc:" in text

    def filter(self, record: logging.LogRecord) -> bool:
        """Filter and scrub the given log record."""
        # Dynamically filter out any log lines matching the standard encrypted credential prefix "enc:"
        if isinstance(record.msg, str) and self._has_encrypted_credentials(record.msg):
            return False

        try:
            formatted_msg = record.getMessage()
            if self._has_encrypted_credentials(formatted_msg):
                return False
        except Exception:
            pass

        if isinstance(record.args, tuple):
            if any(self._has_encrypted_credentials(str(arg)) for arg in record.args):
                return False
        elif isinstance(record.args, dict):
            if any(
                self._has_encrypted_credentials(str(v)) for v in record.args.values()
            ):
                return False

        if record.stack_info and self._has_encrypted_credentials(record.stack_info):
            return False

        # Pre-format exception info and check it
        if record.exc_info and not record.exc_text:
            try:
                record.exc_text = "".join(traceback.format_exception(*record.exc_info))
            except Exception:
                pass

        if record.exc_text and self._has_encrypted_credentials(record.exc_text):
            return False

        # Scrub message
        if isinstance(record.msg, str):
            record.msg = self._scrub(record.msg)

        # Scrub args
        if isinstance(record.args, tuple):
            record.args = tuple(self._scrub_arg(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {k: self._scrub_arg(v) for k, v in record.args.items()}

        # Scrub stack info
        if record.stack_info:
            record.stack_info = self._scrub(record.stack_info)

        # Scrub pre-formatted exception text
        if record.exc_text:
            record.exc_text = self._scrub(record.exc_text)

        return True
