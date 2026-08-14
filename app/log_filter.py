"""Global log filter to scrub sensitive user paths from log output."""

import logging
import traceback


class LogScrubbingFilter(logging.Filter):
    """Filter that removes user home directory paths from all log records."""

    def __init__(self, home_dir: str):
        super().__init__()
        self.home_dir_fwd = home_dir.replace("\\", "/")
        self.home_dir_back = home_dir.replace("/", "\\")

    def _scrub(self, text: str) -> str:
        if not isinstance(text, str):
            return text
        text = text.replace(self.home_dir_fwd, "<USER_HOME>")
        text = text.replace(self.home_dir_back, "<USER_HOME>")
        return text

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
