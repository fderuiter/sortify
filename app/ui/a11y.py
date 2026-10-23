"""Accessibility mixin and audit helpers shared by every TUI screen."""

import logging
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Button

from app.core.path_utils import scrub_user_home_paths
from app.core.text_utils import sanitize_secret_patterns
from app.ui.heading import Heading

logger = logging.getLogger(__name__)


class A11yMixin:
    """Mixin providing WCAG 2.1 screen reader announcements, accessibility attributes, and audit hooks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.announcements: List[Dict[str, Any]] = []
        self.last_announcement: Optional[str] = None
        self._speech_thread: Optional[threading.Thread] = None

    def update_status(self, text: str) -> None:
        """Update visual status region on self or parent application."""
        try:
            if (
                hasattr(self, "app")
                and self.app
                and self.app is not self
                and hasattr(self.app, "update_status")
            ):
                self.app.update_status(text)
        except Exception:
            pass

    def join_speech_thread(self, timeout: float = 1.0) -> None:
        """Wait for active speech synthesis thread to finish."""
        speech_thread = self._speech_thread
        if speech_thread is not None:
            if speech_thread.is_alive():
                try:
                    speech_thread.join(timeout=timeout)
                except Exception:
                    pass
            if not speech_thread.is_alive():
                self._speech_thread = None
        if (
            hasattr(self, "app")
            and self.app
            and self.app is not self
            and hasattr(self.app, "join_speech_thread")
        ):
            try:
                self.app.join_speech_thread(timeout=timeout)
            except Exception:
                pass

    def on_unmount(self) -> None:
        """Lifecycle hook called when component is unmounted from Textual app."""
        self.join_speech_thread(timeout=0.5)
        if hasattr(super(), "on_unmount"):
            try:
                super().on_unmount()  # type: ignore[misc]
            except Exception:
                pass

    def _speech_enabled(self) -> bool:
        """Return whether the user opted in to spoken status messages."""
        settings = getattr(self, "settings", None)
        try:
            return bool(getattr(settings, "SPEAK_ANNOUNCEMENTS", False))
        except Exception:
            return False

    def _get_speech_binary(self) -> Optional[str]:
        """Resolve available speech synthesis executable based on host platform.

        On macOS, returns native 'say' command path. On Linux, returns 'spd-say' or
        'say' path. On Windows (win32), returns None as Linux/macOS speech binaries
        do not exist natively, defaulting to visual status live region fallback.
        """
        try:
            if sys.platform == "darwin":
                return shutil.which("say")
            elif sys.platform == "win32":
                return None
            else:
                return shutil.which("spd-say") or shutil.which("say")
        except (FileNotFoundError, OSError):
            return None

    def announce(
        self,
        message: str,
        priority: str = "polite",
        help_url: Optional[str] = None,
    ) -> str:
        """Emit auditory screen reader announcement and log accessibility event."""
        if message is None:
            message = ""
        else:
            try:
                s_msg = str(message)
                message = (
                    sanitize_secret_patterns(scrub_user_home_paths(s_msg))
                    if s_msg
                    else ""
                )
            except Exception:
                try:
                    message = str(message)
                except Exception:
                    message = ""

        full_msg = f"{message} [Help: {help_url}]" if help_url else message
        entry = {
            "message": full_msg,
            "priority": priority,
            "timestamp": time.time(),
            "help_url": help_url,
        }
        self.announcements.append(entry)
        self.last_announcement = full_msg

        forwarded = False
        # Forward to parent app if available
        try:
            app_obj = getattr(self, "_app", None) or getattr(self, "app", None)
            if app_obj and app_obj is not self and hasattr(app_obj, "announce"):
                app_obj.announce(message, priority=priority, help_url=help_url)
                forwarded = True
        except Exception:
            pass

        if not forwarded:
            # Speaking aloud is opt-in: screen-reader users already hear the
            # status bar, and everyone else should not hear every message.
            speech_bin = self._get_speech_binary() if self._speech_enabled() else None

            if speech_bin:
                speech_thread = self._speech_thread
                if speech_thread is None or not speech_thread.is_alive():
                    try:

                        def _speak():
                            try:
                                kwargs: Dict[str, Any] = {
                                    "timeout": 1.0,
                                    "stdin": subprocess.DEVNULL,
                                    "stdout": subprocess.DEVNULL,
                                    "stderr": subprocess.DEVNULL,
                                    "check": False,
                                }
                                if sys.platform == "win32":
                                    kwargs["creationflags"] = getattr(
                                        subprocess, "CREATE_NO_WINDOW", 0
                                    )
                                subprocess.run(
                                    [speech_bin, message],
                                    **kwargs,
                                )
                            except BaseException as exc:
                                logger.debug(
                                    f"Speech synthesis execution failed: {exc}"
                                )
                            finally:
                                if (
                                    getattr(self, "_speech_thread", None)
                                    is threading.current_thread()
                                ):
                                    self._speech_thread = None

                        t = threading.Thread(target=_speak, daemon=True)
                        t.start()
                        self._speech_thread = t
                    except Exception as e:
                        logger.debug(f"Speech binary execution failed: {e}")

            # Fallback and update visual status region for every invocation
            if hasattr(self, "update_status") and callable(self.update_status):
                try:
                    self.update_status(message)
                except Exception:
                    pass

        return message

    def get_last_announcement(self) -> Optional[str]:
        """Get the most recent screen reader announcement text."""
        return self.last_announcement

    def audit_a11y_compliance(self) -> Dict[str, Any]:
        """Audit WCAG 2.1 accessibility compliance for this TUI component.

        Verifies:
        1. Interactive controls have tooltips or explicit accessibility labels.
        2. Modal screens define Escape key bindings for keyboard dismissal.
        3. Screen reader announcement logging capability exists.
        4. Root application defines visual status region capabilities and speech binary fallback handling.
        """
        violations = []

        try:
            for widget in self.query("*"):
                w_type = type(widget).__name__
                if w_type in (
                    "Input",
                    "Button",
                    "Select",
                    "Switch",
                    "Tree",
                    "Log",
                    "DensitySlider",
                ):
                    has_tooltip = bool(getattr(widget, "tooltip", None))
                    has_label = bool(
                        getattr(widget, "label", None)
                        or getattr(widget, "_text", None)
                        or (
                            getattr(widget, "value", None)
                            if w_type != "DensitySlider"
                            else None
                        )
                        or getattr(widget, "placeholder", None)
                    )
                    if not (has_tooltip or has_label):
                        violations.append(
                            {
                                "rule": "A11Y001_MISSING_LABEL",
                                "widget_id": getattr(widget, "id", None) or w_type,
                                "message": f"Interactive control '{w_type}' lacks explicit tooltip or accessible text label.",
                            }
                        )
        except Exception:
            pass

        if isinstance(self, ModalScreen):
            bindings = getattr(self, "BINDINGS", [])
            has_escape = any(
                getattr(b, "key", None) == "escape"
                or (isinstance(b, Binding) and b.key == "escape")
                for b in bindings
            )
            if not has_escape:
                violations.append(
                    {
                        "rule": "A11Y_MISSING_ESCAPE_BINDING",
                        "message": f"Modal screen '{type(self).__name__}' lacks Escape key binding for accessibility dismissal.",
                    }
                )

        if not hasattr(self, "announce") or not hasattr(self, "announcements"):
            violations.append(
                {
                    "rule": "A11Y_MISSING_ANNOUNCER",
                    "message": f"Component '{type(self).__name__}' lacks screen reader announcement handler.",
                }
            )

        status_bar_available = hasattr(self, "update_status") or (
            hasattr(self, "app") and self.app and hasattr(self.app, "update_status")
        )
        if not status_bar_available:
            violations.append(
                {
                    "rule": "A11Y_MISSING_STATUS_REGION",
                    "message": f"Component '{type(self).__name__}' or root application lacks visual status region capability.",
                }
            )

        try:
            nodes = list(self.query("*"))
            if nodes:
                headings = [
                    w for w in nodes if isinstance(w, Heading) or hasattr(w, "level")
                ]
                if isinstance(self, ModalScreen):
                    has_h1 = any(getattr(h, "level", None) == 1 for h in headings)
                    if not has_h1:
                        violations.append(
                            {
                                "rule": "A11Y_MISSING_MODAL_H1_HEADING",
                                "message": f"Modal screen '{type(self).__name__}' lacks a top-level semantic h1 heading.",
                            }
                        )
                levels = [
                    getattr(h, "level", 1) for h in headings if hasattr(h, "level")
                ]
                prev_level = 0
                for lvl in levels:
                    if lvl > prev_level + 1 and prev_level > 0:
                        violations.append(
                            {
                                "rule": "A11Y_HEADING_HIERARCHY_GAP",
                                "message": f"Heading hierarchy gap detected in '{type(self).__name__}': jumped from h{prev_level} to h{lvl}.",
                            }
                        )
                    prev_level = lvl
        except Exception:
            pass

        speech_binary = self._get_speech_binary()

        return {
            "component": type(self).__name__,
            "compliant": len(violations) == 0,
            "violations_count": len(violations),
            "violations": violations,
            "speech_binary_available": speech_binary is not None,
            "speech_binary": speech_binary,
            "speech_binary_fallback_ready": True,
            "status_bar_available": status_bar_available,
        }

    def _show_help_button(self, help_url: str) -> None:
        """Show the Help button and record contextual help_url."""
        self.help_url = help_url

        def _unhide() -> None:
            try:
                btn = self.query_one("#btn-help", Button)
                btn.remove_class("hidden")
            except Exception:
                pass

        app_obj = getattr(self, "app", None)
        if app_obj and hasattr(app_obj, "call_from_thread"):
            try:
                app_obj.call_from_thread(_unhide)
            except Exception:
                _unhide()
        else:
            _unhide()


@dataclass
class A11yViolation:
    """Representation of an accessibility violation in a TUI component."""

    rule_id: str
    component_id: str
    component_name: str
    viewport_name: str
    viewport_width: int
    locator: str
    message: str


def inspect_tui_component(component: Any) -> List[A11yViolation]:
    """Inspect a TUI App or Modal component for WCAG 2.1 accessibility compliance.

    Leverages the component's internal audit hook method 'audit_a11y_compliance'
    to perform programmatic verification.
    """
    violations: List[A11yViolation] = []
    comp_name = type(component).__name__

    if hasattr(component, "audit_a11y_compliance") and callable(
        component.audit_a11y_compliance
    ):
        res = component.audit_a11y_compliance()
        for v in res.get("violations", []):
            violations.append(
                A11yViolation(
                    rule_id=v.get("rule", "A11Y_UNKNOWN"),
                    component_id=comp_name,
                    component_name=comp_name,
                    viewport_name="terminal",
                    viewport_width=80,
                    locator=v.get("widget_id", comp_name),
                    message=v.get("message", "A11y violation detected"),
                )
            )

    return violations
