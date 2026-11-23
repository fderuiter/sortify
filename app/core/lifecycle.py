"""Unified lifecycle state machine and telemetry mixin.

Provides explicit state transition validation support, thread-safe locking,
telemetry hook and listener registration, and sanitized event broadcasting
across asynchronous background workflows.
"""

from __future__ import annotations

import inspect
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from app.core.text_utils import sanitize_secret_patterns

logger = logging.getLogger(__name__)

# Key substrings that indicate sensitive credentials or tokens
_SENSITIVE_KEY_SUBSTRINGS = (
    "password",
    "secret",
    "token",
    "api_key",
    "credential",
    "auth",
    "private_key",
)

# Keys that are benign despite containing sensitive substrings (e.g. keywords, sort key)
_EXEMPT_KEY_NAMES = {
    "keyword",
    "keywords",
    "sort_key",
    "group_key",
    "primary_key",
    "priority_key",
    "event_key",
    "cache_key",
    "dict_key",
    "key_count",
}


def sanitize_telemetry_payload(data: Any) -> Any:
    """Recursively scrub sensitive keys, tokens, and raw credential strings from telemetry payloads."""
    if isinstance(data, dict):
        sanitized: dict[str, Any] = {}
        for k, v in data.items():
            k_str = str(k)
            lower_k = k_str.lower()
            if lower_k not in _EXEMPT_KEY_NAMES and any(
                sub in lower_k for sub in _SENSITIVE_KEY_SUBSTRINGS
            ):
                sanitized[k_str] = "[REDACTED]"
                continue
            sanitized[k_str] = sanitize_telemetry_payload(v)
        return sanitized
    if isinstance(data, (list, tuple, set)):
        sanitized_list = [sanitize_telemetry_payload(item) for item in data]
        return (
            type(data)(sanitized_list) if not isinstance(data, set) else sanitized_list
        )
    if isinstance(data, str):
        return sanitize_secret_patterns(data, replacement="[REDACTED]")
    return data


class LifecycleTelemetryMixin:
    """Reusable mixin providing thread-safe lifecycle hooks, listeners, and telemetry dispatch."""

    @property
    def _lifecycle_lock(self) -> threading.RLock:
        """Resolve a dedicated re-entrant lock for lifecycle telemetry registration and dispatch."""
        if not hasattr(self, "_lifecycle_listener_lock"):
            object.__setattr__(self, "_lifecycle_listener_lock", threading.RLock())
        return getattr(self, "_lifecycle_listener_lock")

    def _get_transition_listeners(self) -> list[Callable]:
        if not hasattr(self, "_listeners"):
            self._listeners = []
        return self._listeners

    def _get_state_hooks(self) -> list[Callable]:
        if not hasattr(self, "_state_hooks"):
            self._state_hooks = []
        return self._state_hooks

    def _get_telemetry_hooks(self) -> list[Callable]:
        if not hasattr(self, "_telemetry_hooks"):
            self._telemetry_hooks = []
        return self._telemetry_hooks

    def add_transition_listener(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> None:
        """Register a callback to receive structured telemetry events on status change."""
        with self._lifecycle_lock:
            listeners = self._get_transition_listeners()
            if callback not in listeners:
                listeners.append(callback)

    def remove_transition_listener(
        self, callback: Callable[[dict[str, Any]], None]
    ) -> None:
        """Unregister a telemetry event callback."""
        with self._lifecycle_lock:
            listeners = self._get_transition_listeners()
            if callback in listeners:
                listeners.remove(callback)

    def add_state_change_hook(self, callback: Callable) -> None:
        """Subscribe to lifecycle state transition notifications."""
        with self._lifecycle_lock:
            hooks = self._get_state_hooks()
            if callback not in hooks:
                hooks.append(callback)

    def remove_state_change_hook(self, callback: Callable) -> None:
        """Unsubscribe from lifecycle state transition notifications."""
        with self._lifecycle_lock:
            hooks = self._get_state_hooks()
            if callback in hooks:
                hooks.remove(callback)

    def add_telemetry_hook(self, callback: Callable) -> None:
        """Subscribe to pipeline telemetry event notifications."""
        with self._lifecycle_lock:
            hooks = self._get_telemetry_hooks()
            if callback not in hooks:
                hooks.append(callback)

    def remove_telemetry_hook(self, callback: Callable) -> None:
        """Unsubscribe from pipeline telemetry event notifications."""
        with self._lifecycle_lock:
            hooks = self._get_telemetry_hooks()
            if callback in hooks:
                hooks.remove(callback)

    def emit_telemetry(
        self, event: str, data: dict | None = None, **kwargs: Any
    ) -> None:
        """Safely broadcast structured telemetry event to registered subscribers."""
        payload = dict(data or {})
        payload.update(kwargs)

        # Include current state if available and not already specified
        if "state" not in payload and "status" not in payload:
            current_state = getattr(self, "state", None) or getattr(
                self, "status", None
            )
            if current_state is not None:
                payload["state"] = getattr(current_state, "value", str(current_state))

        sanitized_payload = sanitize_telemetry_payload(payload)

        event_payload = dict(sanitized_payload)
        event_payload.setdefault("event_type", event)
        event_payload.setdefault("timestamp", time.time())

        with self._lifecycle_lock:
            hooks = list(self._get_telemetry_hooks())
            listeners = list(self._get_transition_listeners())

        for listener in listeners:
            try:
                sig = inspect.signature(listener)
                params = [
                    p
                    for p in sig.parameters.values()
                    if p.kind
                    in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    )
                ]
                has_varargs = any(
                    p.kind == inspect.Parameter.VAR_POSITIONAL
                    for p in sig.parameters.values()
                )
                if has_varargs or len(params) == 1:
                    listener(event_payload)
            except Exception as e:
                logger.warning(
                    f"Error executing listener {listener} on emit_telemetry: {e}",
                    exc_info=True,
                )

        for hook in hooks:
            try:
                sig = inspect.signature(hook)
                params = [
                    p
                    for p in sig.parameters.values()
                    if p.kind
                    in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    )
                ]
                has_varargs = any(
                    p.kind == inspect.Parameter.VAR_POSITIONAL
                    for p in sig.parameters.values()
                )
                if has_varargs or len(params) >= 2:
                    hook(event, sanitized_payload)
                elif len(params) == 1:
                    hook(event_payload)
                else:
                    hook()
            except Exception as e:
                logger.warning(f"Error in telemetry hook {hook}: {e}", exc_info=True)

    def _notify_lifecycle_listeners(
        self,
        old_state: Any,
        new_state: Any,
        context: dict | None = None,
        event_type: str | None = None,
        **extra_payload: Any,
    ) -> None:
        """Broadcast structured lifecycle transition event to registered listeners and hooks."""
        old_val = (
            getattr(old_state, "value", str(old_state))
            if old_state is not None
            else None
        )
        new_val = (
            getattr(new_state, "value", str(new_state))
            if new_state is not None
            else None
        )

        now = time.time()
        base_payload: dict[str, Any] = {
            "event_type": event_type or "STATE_CHANGED",
            "timestamp": now,
            "old_state": old_val,
            "new_state": new_val,
            "previous_state": old_val,
            "old_status": old_val,
            "new_status": new_val,
            "previous_status": old_val,
            "metadata": sanitize_telemetry_payload(context or {}),
        }
        if extra_payload:
            base_payload.update(sanitize_telemetry_payload(extra_payload))

        with self._lifecycle_lock:
            listeners = list(self._get_transition_listeners())
            state_hooks = list(self._get_state_hooks())
            telemetry_hooks = list(self._get_telemetry_hooks())

        # 1. Notify transition listeners (e.g. SessionStateMachine, ledger listeners)
        for listener in listeners:
            try:
                sig = inspect.signature(listener)
                param_count = len(
                    [
                        p
                        for p in sig.parameters.values()
                        if p.kind
                        in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        )
                    ]
                )
                if param_count == 2:
                    listener(old_state, new_state)
                elif param_count >= 3:
                    listener(old_state, new_state, context or {})
                else:
                    listener(base_payload)
            except Exception as e:
                logger.warning(
                    f"Error executing transition listener {listener}: {e}",
                    exc_info=True,
                )

        # 2. Notify state change hooks (SortPipeline style)
        for hook in state_hooks:
            try:
                sig = inspect.signature(hook)
                param_count = len(
                    [
                        p
                        for p in sig.parameters.values()
                        if p.kind
                        in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        )
                    ]
                )
                if param_count == 1:
                    hook(new_state)
                elif param_count == 2:
                    hook(old_state, new_state)
                else:
                    hook(old_state, new_state, context or {})
            except Exception as e:
                logger.warning(f"Error in state change hook {hook}: {e}", exc_info=True)

        # 3. Notify telemetry hooks about the state transition
        for t_hook in telemetry_hooks:
            try:
                sig = inspect.signature(t_hook)
                params = [
                    p
                    for p in sig.parameters.values()
                    if p.kind
                    in (
                        inspect.Parameter.POSITIONAL_ONLY,
                        inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    )
                ]
                has_varargs = any(
                    p.kind == inspect.Parameter.VAR_POSITIONAL
                    for p in sig.parameters.values()
                )
                if has_varargs or len(params) >= 2:
                    t_hook(event_type or "state_change", base_payload)
                elif len(params) == 1:
                    t_hook(base_payload)
                else:
                    t_hook()
            except Exception as e:
                logger.warning(f"Error in telemetry hook {t_hook}: {e}", exc_info=True)
