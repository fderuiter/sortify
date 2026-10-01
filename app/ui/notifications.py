"""Unified context-aware notification bus for NiceGUI, Textual TUI, and CLI runtime environments."""

import logging
import sys
import threading
from enum import Enum
from typing import Any, Callable, Dict, Optional

from app.core.path_utils import scrub_user_home_paths
from app.core.text_utils import sanitize_secret_patterns

logger = logging.getLogger(__name__)


class NotificationMode(str, Enum):
    """Runtime environment modes for notification dispatching."""

    AUTO = "auto"
    GUI = "gui"
    TUI = "tui"
    CLI = "cli"


class NotificationManager:
    """Central dispatcher for environment-aware notifications."""

    _instance: Optional["NotificationManager"] = None
    _lock = threading.RLock()

    def __init__(self):
        self._mode: NotificationMode = NotificationMode.AUTO
        self._active_tui_app: Optional[Any] = None
        self._output_stream = sys.stderr
        self._custom_handlers: list[Callable[..., Any]] = []

    @classmethod
    def get_instance(cls) -> "NotificationManager":
        """Retrieve the singleton NotificationManager instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """Reset singleton instance (useful for unit tests)."""
        with cls._lock:
            cls._instance = None

    def set_mode(self, mode: NotificationMode | str) -> None:
        """Set active notification mode explicitly."""
        if isinstance(mode, str):
            mode = NotificationMode(mode.lower())
        self._mode = mode

    def get_mode(self) -> NotificationMode:
        """Get currently configured notification mode."""
        return self._mode

    def register_tui(self, app: Any) -> None:
        """Register active Textual TUI app instance."""
        self._active_tui_app = app

    def unregister_tui(self, app: Any = None) -> None:
        """Unregister active Textual TUI app instance."""
        if app is None or self._active_tui_app is app:
            self._active_tui_app = None

    def set_output_stream(self, stream: Any) -> None:
        """Set output stream for CLI/terminal notifications (defaults to sys.stderr)."""
        self._output_stream = stream

    def register_handler(self, handler: Callable[..., Any]) -> None:
        """Register a custom notification handler callback."""
        if handler not in self._custom_handlers:
            self._custom_handlers.append(handler)

    def detect_mode(self) -> NotificationMode:
        """Detect current active runtime environment mode."""
        if self._mode != NotificationMode.AUTO:
            return self._mode

        # Check for registered or active Textual TUI app
        tui_app = self._get_active_tui_app()
        if tui_app is not None:
            return NotificationMode.TUI

        # Check for active NiceGUI environment
        if self._is_nicegui_active():
            return NotificationMode.GUI

        return NotificationMode.CLI

    def _get_active_tui_app(self) -> Optional[Any]:
        if self._active_tui_app is not None:
            return self._active_tui_app
        try:
            from textual._context import active_app
            app = active_app.get(None)
            if app is not None:
                return app
        except Exception:
            pass
        return None

    def _is_nicegui_active(self) -> bool:
        try:
            if "nicegui" in sys.modules:
                from unittest.mock import MagicMock

                import nicegui.ui as nicegui_ui
                if not isinstance(nicegui_ui, MagicMock):
                    if getattr(nicegui_ui, "context", None) and getattr(nicegui_ui.context, "client", None):
                        return True
        except Exception:
            pass
        return False

    def notify(
        self,
        message: Any,
        type: str = "info",
        caption: Optional[str] = None,
        timeout: Optional[int] = None,
        **kwargs: Any
    ) -> Dict[str, Any]:
        """Dispatch a notification based on the active runtime environment mode.

        Args:
            message: Message text or object to display.
            type: Notification type ('info', 'positive', 'negative', 'warning').
            caption: Optional subtitle or secondary text.
            timeout: Display timeout in milliseconds or seconds.
            **kwargs: Additional parameters passed to target UI handler.

        Returns
        -------
            Dict containing event summary details.
        """
        msg_str = str(message) if message is not None else ""
        mode = self.detect_mode()

        event = {
            "message": msg_str,
            "type": type,
            "caption": caption,
            "timeout": timeout,
            "mode": mode.value,
            "kwargs": kwargs,
        }

        # Dispatch to custom handlers first
        for handler in list(self._custom_handlers):
            try:
                handler(event)
            except Exception as e:
                logger.debug(f"Error in custom notification handler: {e}")

        if mode == NotificationMode.GUI:
            self._dispatch_gui(msg_str, type=type, caption=caption, timeout=timeout, **kwargs)
        elif mode == NotificationMode.TUI:
            self._dispatch_tui(msg_str, type=type, caption=caption, timeout=timeout, **kwargs)
        else:
            self._dispatch_cli(msg_str, type=type, caption=caption, **kwargs)

        return event

    def _dispatch_gui(
        self,
        message: str,
        type: str = "info",
        caption: Optional[str] = None,
        timeout: Optional[int] = None,
        **kwargs: Any
    ) -> None:
        try:
            import nicegui.ui as nicegui_ui
            nicegui_ui.notify(message, type=type, caption=caption, timeout=timeout, **kwargs)
        except Exception as e:
            logger.warning(f"GUI notification fallback due to error: {e}")
            self._dispatch_cli(message, type=type, caption=caption, **kwargs)

    def _dispatch_tui(
        self,
        message: str,
        type: str = "info",
        caption: Optional[str] = None,
        timeout: Optional[int] = None,
        **kwargs: Any
    ) -> None:
        tui_app = self._get_active_tui_app()
        if tui_app is None:
            self._dispatch_cli(message, type=type, caption=caption, **kwargs)
            return

        type_lower = (type or "info").lower()
        if type_lower in ("positive", "success"):
            severity = "information"
        elif type_lower in ("negative", "error"):
            severity = "error"
        elif type_lower in ("warning", "warn"):
            severity = "warning"
        else:
            severity = "information"

        title = caption or "Notification"
        toast_timeout = float(timeout / 1000.0) if timeout and timeout > 100 else (timeout or 5)

        def _do_notify():
            try:
                tui_app.notify(message, title=title, severity=severity, timeout=toast_timeout)
            except Exception as ex:
                logger.debug(f"Textual toast notification failed: {ex}")

            try:
                from textual.widgets import Log
                log_widget = tui_app.query_one("#tui-log", Log)
                log_widget.write_line(f"[{type.upper()}] {message}")
            except Exception:
                pass

        try:
            if threading.current_thread() is getattr(tui_app, "_thread", None) or getattr(tui_app, "is_headless", False):
                _do_notify()
            elif hasattr(tui_app, "call_from_thread"):
                tui_app.call_from_thread(_do_notify)
            else:
                _do_notify()
        except Exception as ex:
            logger.debug(f"TUI dispatch failed: {ex}")
            self._dispatch_cli(message, type=type, caption=caption, **kwargs)

    def _dispatch_cli(
        self,
        message: str,
        type: str = "info",
        caption: Optional[str] = None,
        **kwargs: Any
    ) -> None:
        clean_msg = sanitize_secret_patterns(scrub_user_home_paths(message))
        full_msg = f"{caption}: {clean_msg}" if caption else clean_msg
        tag = (type or "info").upper()

        formatted = f"[NOTIFICATION] [{tag}] {full_msg}\n"

        if tag in ("NEGATIVE", "ERROR"):
            logger.error(full_msg)
        elif tag in ("WARNING", "WARN"):
            logger.warning(full_msg)
        else:
            logger.info(full_msg)

        try:
            if self._output_stream and hasattr(self._output_stream, "write"):
                self._output_stream.write(formatted)
                if hasattr(self._output_stream, "flush"):
                    self._output_stream.flush()
        except Exception as e:
            logger.debug(f"Failed writing notification to stream: {e}")


def notify(
    message: Any,
    type: str = "info",
    caption: Optional[str] = None,
    timeout: Optional[int] = None,
    **kwargs: Any
) -> Dict[str, Any]:
    """Top-level convenience function for sending notifications."""
    return NotificationManager.get_instance().notify(
        message, type=type, caption=caption, timeout=timeout, **kwargs
    )
