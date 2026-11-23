"""Policy Engine module for evaluating compliance policies and validating lock paths."""

import fnmatch
import os
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.core.path_utils import validate_target_path


def match_pattern_expression(expression: str, filename: str) -> bool:
    """Evaluate a pattern expression against a filename using wildcards, extension masks, or substring matching.

    Parameters
    ----------
    expression : str
        Pattern rule expression (glob mask such as ``*.pdf``, extension mask such as ``.pdf``,
        or plain substring text).
    filename : str
        File path or basename to test against the pattern expression.

    Returns
    -------
    bool
        True if the filename matches the pattern expression, False otherwise.
    """
    if not expression or not filename:
        return False
    expr_lower = str(expression).strip().lower()
    if not expr_lower:
        return False
    fn_only = os.path.basename(str(filename)).lower()
    if not fn_only:
        return False

    has_wildcard = (
        "*" in expr_lower
        or "?" in expr_lower
        or ("[" in expr_lower and "]" in expr_lower)
    )
    if has_wildcard:
        return fnmatch.fnmatchcase(fn_only, expr_lower)

    if expr_lower.startswith(".") and "/" not in expr_lower and "\\" not in expr_lower:
        return fnmatch.fnmatchcase(fn_only, f"*{expr_lower}")

    return expr_lower in fn_only


class PolicyEvaluationResult(BaseModel):
    """Structured Pydantic model for policy evaluation result."""

    type: str = ""
    expression: str = ""
    target_path: str = ""
    priority: int = 0
    halting: bool = False
    halt_evaluation: bool = False

    model_config = ConfigDict(extra="allow")

    def dict(self, *args, **kwargs) -> dict[str, Any]:
        """Backward compatibility method for legacy Pydantic v1 callers."""
        return self.model_dump(*args, **kwargs)

    def __getitem__(self, item: str) -> Any:
        """Support item lookup via bracket syntax for dictionary compatibility."""
        if hasattr(self, item):
            return getattr(self, item)
        extra = getattr(self, "__pydantic_extra__", None)
        if extra and item in extra:
            return extra[item]
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        """Support dictionary get method."""
        try:
            return self[item]
        except KeyError:
            return default

    def __contains__(self, item: str) -> bool:
        """Check if key exists in attributes or extra fields."""
        extra = getattr(self, "__pydantic_extra__", None) or {}
        return hasattr(self, item) or item in extra


class PolicyEngine:
    """Standalone evaluation engine for enforcing compliance overrides on folder locks and lifecycle policy actions."""

    SUPPORTED_ACTIONS = {
        "archive",
        "retain",
        "quarantine",
        "redact",
        "unarchive",
        "copy",
        "ignore",
    }

    @staticmethod
    def validate_policy_action(action: str) -> bool:
        """Validate whether an action directive is supported by the extended policy engine."""
        if not action or not isinstance(action, str):
            return False
        return action.lower() in PolicyEngine.SUPPORTED_ACTIONS

    @staticmethod
    def resolve_effective_action(rule: Any) -> str | None:
        """Resolve the normalized lifecycle action for a policy rule dictionary or model."""
        if rule is None:
            return None
        raw_action = (
            rule.get("action")
            if isinstance(rule, dict)
            else getattr(rule, "action", None)
        )
        if raw_action is None and hasattr(rule, "get"):
            raw_action = rule.get("action")
        if isinstance(raw_action, str) and raw_action.strip():
            act = raw_action.strip().lower()
            if act in PolicyEngine.SUPPORTED_ACTIONS:
                return act
        raw_type = (
            rule.get("type", "")
            if isinstance(rule, dict) or hasattr(rule, "get")
            else getattr(rule, "type", "")
        )
        if isinstance(raw_type, str):
            t_low = raw_type.strip().lower()
            if t_low in ("unarchive", "copy", "ignore"):
                return t_low
        return None

    @staticmethod
    def contains_secrets(text: str) -> bool:
        """Check whether text contains secret tokens or credentials."""
        from app.core.text_utils import contains_secrets

        return contains_secrets(text)

    @staticmethod
    def match_policy(
        rule: dict, file_path: str, doc_text: str, status_match: str | None
    ) -> bool:
        """Evaluate a single compliance policy rule against a file."""
        rule_type = (
            rule.get("type", "").lower()
            if isinstance(rule, dict)
            else getattr(rule, "type", "").lower()
        )
        expression = (
            rule.get("expression", "").lower()
            if isinstance(rule, dict)
            else getattr(rule, "expression", "").lower()
        )

        fn_only = os.path.basename(file_path).lower()
        doc_text_str = str(doc_text) if doc_text else ""
        dl_lower = doc_text_str.lower()

        if rule_type in ("secret", "credential") or expression in (
            "secret",
            "credential",
        ):
            return PolicyEngine.contains_secrets(
                file_path
            ) or PolicyEngine.contains_secrets(doc_text_str)

        if rule_type in ("unarchive", "copy", "ignore"):
            if expression:
                if match_pattern_expression(expression, fn_only):
                    return True
                text_to_search = fn_only if status_match else (fn_only + " " + dl_lower)
                return expression in text_to_search
            return bool(PolicyEngine.resolve_effective_action(rule))
        if rule_type in ("keyword", "action", "compliance", ""):
            if expression:
                text_to_search = fn_only if status_match else (fn_only + " " + dl_lower)
                return expression in text_to_search
            # If no expression is given but an action directive exists, match unconditionally
            return bool(rule.get("action"))
        elif rule_type == "pattern":
            return match_pattern_expression(expression, fn_only)
        elif rule_type == "override":
            return (
                expression == fn_only
                or expression in fn_only
                or expression in file_path.lower()
            )
        return False

    @staticmethod
    def sort_policies(policies: list[dict] | None) -> list[dict]:
        """Sort policy rule list by priority descending."""
        if not policies:
            return []
        return sorted(
            policies,
            key=lambda x: (
                x.get("priority", 0)
                if isinstance(x, dict)
                else getattr(x, "priority", 0)
            ),
            reverse=True,
        )

    @classmethod
    def evaluate_policies(
        cls,
        file_path: str,
        doc_text: str,
        status_match: str | None,
        policies: list[dict],
        return_halting: bool = False,
        is_sorted: bool = False,
    ) -> PolicyEvaluationResult | None | tuple[PolicyEvaluationResult | None, bool]:
        """Find the highest priority matching compliance policy rule for a file."""
        if not policies:
            return (None, False) if return_halting else None
        if is_sorted:
            sorted_policies = policies
        else:
            sorted_policies = cls.sort_policies(policies)
        matched_policy = None
        halt_evaluation = False
        for rule in sorted_policies:
            is_enabled = (
                rule.get("enabled", True)
                if isinstance(rule, dict)
                else getattr(rule, "enabled", True)
            )
            if not is_enabled:
                continue
            if cls.match_policy(rule, file_path, doc_text, status_match):
                matched_policy = rule
                break
            else:
                is_halting = (
                    rule.get("halting", False)
                    if isinstance(rule, dict)
                    else getattr(rule, "halting", False)
                )
                if is_halting:
                    halt_evaluation = True
                    break

        matched_result = None
        if matched_policy:
            raw_data = (
                dict(matched_policy)
                if isinstance(matched_policy, dict)
                else matched_policy.model_dump()
            )
            raw_data["halt_evaluation"] = halt_evaluation
            matched_result = PolicyEvaluationResult.model_validate(raw_data)

        if return_halting:
            return matched_result, halt_evaluation
        return matched_result

    @staticmethod
    def validate_lock_path_safe(
        lock_path: Any, file_path: str | None = None
    ) -> tuple[bool, str | None]:
        """Validate a folder lock path safely without raising exceptions.

        Returns
        -------
            tuple[bool, str | None]: (is_valid, error_message)
            (True, None) if valid, (False, error_message) if invalid.
        """
        if not isinstance(lock_path, str):
            return False, f"Lock path must be a string. Got {type(lock_path)}"
        try:
            validate_target_path(
                lock_path, keyword=os.path.basename(file_path) if file_path else None
            )
            return True, None
        except ValueError as err:
            return False, str(err)

    @staticmethod
    def validate_lock_path(lock_path: str, file_path: str | None = None) -> None:
        """Validate a folder lock path against the established policy schema (path rules).

        Raises ValueError if invalid.
        """
        is_valid, error_msg = PolicyEngine.validate_lock_path_safe(
            lock_path, file_path=file_path
        )
        if not is_valid:
            raise ValueError(error_msg)


def is_masked_by(higher_rule: Any, lower_rule: Any) -> bool:
    """Check if a lower priority rule expression is masked by a higher priority rule expression."""
    ha_type = (
        higher_rule.get("type", "").lower()
        if hasattr(higher_rule, "get")
        else getattr(higher_rule, "type", "").lower()
    )
    lo_type = (
        lower_rule.get("type", "").lower()
        if hasattr(lower_rule, "get")
        else getattr(lower_rule, "type", "").lower()
    )
    ha_expr = (
        higher_rule.get("expression", "").lower()
        if hasattr(higher_rule, "get")
        else getattr(higher_rule, "expression", "").lower()
    )
    lo_expr = (
        lower_rule.get("expression", "").lower()
        if hasattr(lower_rule, "get")
        else getattr(lower_rule, "expression", "").lower()
    )

    if not ha_expr or not lo_expr:
        return False

    if ha_expr in lo_expr:
        if ha_type == "keyword":
            return True
        if ha_type == "pattern":
            if lo_type in ("pattern", "override"):
                return True
        if ha_type == "override" and lo_type == "override":
            return True
    return False
