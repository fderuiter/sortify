"""Accessibility mixin and audit helpers shared by every TUI screen."""

import logging
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from textual import on
from textual.app import App
from textual.binding import Binding
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Input, Select

from app.core.path_utils import scrub_user_home_paths
from app.core.text_utils import sanitize_secret_patterns
from app.ui.heading import Heading
from app.ui.notifications import TROUBLESHOOTING_URL

logger = logging.getLogger(__name__)


class A11yMixin:
    """Mixin providing WCAG 2.1 screen reader announcements, accessibility attributes, and audit hooks."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.announcements: list[dict[str, Any]] = []
        self.last_announcement: Optional[str] = None
        self._speech_thread: Optional[threading.Thread] = None

    def _resolve_app_instance(self) -> Optional[Any]:
        """Return the owning Textual App instance if mounted or self is an App."""
        if isinstance(self, App):
            return self
        try:
            return getattr(self, "_app", None) or getattr(self, "app", None)
        except Exception:
            return None

    def _is_character_input_focused(self) -> bool:
        """Return True if an Input or Select widget (or Select overlay) holds active focus."""
        app_obj = self._resolve_app_instance()
        focused = getattr(self, "focused", None) or getattr(app_obj, "focused", None)
        if focused is None:
            return False
        if isinstance(focused, (Input, Select)):
            return True
        ancestors = getattr(focused, "ancestors_with_self", ())
        return any(isinstance(anc, (Input, Select)) for anc in ancestors)

    def _character_shortcuts_enabled(self) -> bool:
        """Return True if single-character keyboard shortcuts are enabled."""
        app_obj = self._resolve_app_instance()
        settings = getattr(self, "settings", None) or getattr(app_obj, "settings", None)
        if settings is None:
            return True
        return bool(getattr(settings, "ENABLE_CHARACTER_KEY_SHORTCUTS", True))

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Gate character-triggered help overlay bindings on shortcut setting."""
        if action == "open_help_overlay" and parameters and parameters[0] is True:
            if not self._character_shortcuts_enabled():
                return False
        check_fn = getattr(super(), "check_action", None)
        if callable(check_fn):
            return check_fn(action, parameters)
        return True

    def action_open_help_overlay(self, from_character: bool = False) -> None:
        """Open keyboard shortcut cheat sheet overlay lazily from any screen or modal."""
        if from_character and (
            self._is_character_input_focused()
            or not self._character_shortcuts_enabled()
        ):
            return
        from app.ui.modals.help import ShortcutCheatSheetModal

        app_obj = self._resolve_app_instance()
        if app_obj is None:
            return
        if isinstance(getattr(app_obj, "screen", None), ShortcutCheatSheetModal):
            return
        push_fn = getattr(app_obj, "push_screen", None)
        if callable(push_fn):
            push_fn(ShortcutCheatSheetModal())

    def update_status(self, text: str) -> None:
        """Update visual status region on self or parent application."""
        try:
            app_obj = getattr(self, "app", None)
            update_fn = (
                getattr(app_obj, "update_status", None)
                if app_obj and app_obj is not self
                else None
            )
            if callable(update_fn):
                update_fn(text)
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
        app_obj = getattr(self, "app", None)
        join_fn = (
            getattr(app_obj, "join_speech_thread", None)
            if app_obj and app_obj is not self
            else None
        )
        if callable(join_fn):
            try:
                join_fn(timeout=timeout)
            except Exception:
                pass

    def on_unmount(self) -> None:
        """Lifecycle hook called when component is unmounted from Textual app."""
        self.join_speech_thread(timeout=0.5)
        unmount_fn = getattr(super(), "on_unmount", None)
        if callable(unmount_fn):
            try:
                unmount_fn()
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

        full_msg = (
            f"{message} [Help: {help_url}]"
            if help_url and help_url not in message
            else message
        )
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
                                kwargs: dict[str, Any] = {
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

    def audit_a11y_compliance(self) -> dict[str, Any]:
        """Audit WCAG 2.1 accessibility compliance for this TUI component.

        Verifies:
        1. Interactive controls have tooltips or explicit accessibility labels.
        2. Modal screens define Escape key bindings for keyboard dismissal.
        3. Screen reader announcement logging capability exists.
        4. Root application defines visual status region capabilities and speech binary fallback handling.
        """
        violations = []

        try:
            query_fn = getattr(self, "query", None)
            widgets = list(query_fn("*")) if callable(query_fn) else []
            for widget in widgets:
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
                            if w_type not in ("DensitySlider", "Input")
                            else None
                        )
                        or getattr(widget, "placeholder", None)
                        or getattr(widget, "prompt", None)
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
            query_fn = getattr(self, "query", None)
            nodes = list(query_fn("*")) if callable(query_fn) else []
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
                elif isinstance(self, (App, Screen)):
                    has_h1 = any(getattr(h, "level", None) == 1 for h in headings)
                    if not has_h1:
                        violations.append(
                            {
                                "rule": "A11Y_MISSING_ROOT_H1_HEADING",
                                "message": f"Root screen '{type(self).__name__}' lacks a top-level semantic h1 heading.",
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

    HELP_URL: str = TROUBLESHOOTING_URL

    def _call_ui(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> None:
        """Safely dispatch a UI callback onto the main Textual thread."""
        app_obj = None
        try:
            app_obj = getattr(self, "app", None)
        except Exception:
            app_obj = None

        if (
            app_obj is not None
            and app_obj is not self
            and hasattr(app_obj, "call_from_thread")
        ):
            thread_id = getattr(app_obj, "_thread_id", None)
            if not isinstance(thread_id, int) or thread_id != threading.get_ident():
                try:
                    app_obj.call_from_thread(func, *args, **kwargs)
                    return
                except Exception as exc:
                    logger.debug(f"call_from_thread failed: {exc}")

        try:
            func(*args, **kwargs)
        except Exception as exc:
            logger.debug(f"Direct UI function call failed: {exc}")

    def _show_help_button(self, help_url: str) -> None:
        """Show the Help button and record contextual help_url."""
        self.help_url = help_url

        def _unhide() -> None:
            try:
                query_one_fn = getattr(self, "query_one", None)
                if callable(query_one_fn):
                    btn = query_one_fn("#btn-help", Button)
                    btn.remove_class("hidden")
            except Exception:
                pass

        self._call_ui(_unhide)

    @on(Button.Pressed, "#btn-help")
    def action_help(self) -> None:
        """Open offline contextual troubleshooting help modal for this screen."""
        url = (
            getattr(self, "help_url", None)
            or getattr(self, "HELP_URL", None)
            or TROUBLESHOOTING_URL
        )
        self.open_help_section(url)

    def open_help_section(self, help_url: Optional[str] = None) -> None:
        """Open local offline troubleshooting help modal for ``help_url``."""
        from app.ui.modals.help import TroubleshootingHelpModal

        target_url = (
            help_url
            or getattr(self, "help_url", None)
            or getattr(self, "HELP_URL", None)
            or TROUBLESHOOTING_URL
        )
        self.announce(f"Opened documentation link: {target_url}")

        app_obj = self._resolve_app_instance()
        push_fn = getattr(app_obj, "push_screen", None) if app_obj is not None else None
        if callable(push_fn):
            try:
                push_fn(TroubleshootingHelpModal(help_url=target_url))
            except Exception:
                logger.debug("Failed pushing TroubleshootingHelpModal", exc_info=True)


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


def inspect_tui_component(component: Any) -> list[A11yViolation]:
    """Inspect a TUI App or Modal component for WCAG 2.1 accessibility compliance.

    Leverages the component's internal audit hook method 'audit_a11y_compliance'
    to perform programmatic verification.
    """
    violations: list[A11yViolation] = []
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
