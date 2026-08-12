"""Policy Engine module for evaluating compliance policies and validating lock paths."""

import os

from app.core.path_utils import validate_target_path


class PolicyEngine:
    """Standalone evaluation engine for enforcing compliance overrides on folder locks."""

    @staticmethod
    def match_policy(
        rule: dict, file_path: str, doc_text: str, status_match: str
    ) -> bool:
        """Evaluate a single compliance policy rule against a file."""
        rule_type = rule.get("type", "").lower()
        expression = rule.get("expression", "").lower()

        fn_only = os.path.basename(file_path).lower()
        dl_lower = doc_text.lower() if doc_text else ""

        if rule_type == "keyword":
            text_to_search = fn_only if status_match else (fn_only + " " + dl_lower)
            return expression in text_to_search
        elif rule_type == "pattern":
            return expression in fn_only
        elif rule_type == "override":
            return (
                expression == fn_only
                or expression in fn_only
                or expression in file_path.lower()
            )
        return False

    @classmethod
    def evaluate_policies(
        cls,
        file_path: str,
        doc_text: str,
        status_match: str,
        policies: list[dict],
        return_halting: bool = False,
    ) -> dict | None | tuple[dict | None, bool]:
        """Find the highest priority matching compliance policy rule for a file."""
        if not policies:
            return (None, False) if return_halting else None
        sorted_policies = sorted(
            policies, key=lambda x: x.get("priority", 0), reverse=True
        )
        matched_policy = None
        halt_evaluation = False
        for rule in sorted_policies:
            if cls.match_policy(rule, file_path, doc_text, status_match):
                matched_policy = rule
                break
            else:
                if rule.get("halting", False):
                    halt_evaluation = True
                    break
        if return_halting:
            return matched_policy, halt_evaluation
        return matched_policy

    @staticmethod
    def validate_lock_path(lock_path: str, file_path: str = None) -> None:
        """Validate a folder lock path against the established policy schema (path rules).

        Raises ValueError if invalid.
        """
        if not isinstance(lock_path, str):
            raise ValueError(f"Lock path must be a string. Got {type(lock_path)}")
        # Use established target path validator
        validate_target_path(
            lock_path, keyword=os.path.basename(file_path) if file_path else None
        )
