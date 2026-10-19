"""Extensible modular rule importer framework for Hazel and DropIt rule packages.

Provides strategy classes HazelRuleImporter and DropItRuleImporter to parse external
rule definitions and safely translate them into Sortify KEYWORD_RULES and POLICIES.
"""

import abc
import json
import logging
import plistlib
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Tuple, Union

logger = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Data container holding extracted keyword rules, policies, and warnings."""

    keyword_rules: Dict[str, str] = field(default_factory=dict)
    policies: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    unsupported: List[Dict[str, Any]] = field(default_factory=list)
    source_format: str = "Unknown"

    def merge_into_settings(
        self, settings: Any
    ) -> Tuple[Dict[str, str], List[Dict[str, Any]]]:
        """Safely merge extracted rules into settings without overwriting custom rules.

        Args:
            settings: The AppSettings configuration object.

        Returns
        -------
            Tuple of updated (KEYWORD_RULES dict, POLICIES list).
        """
        existing_keywords = dict(getattr(settings, "KEYWORD_RULES", {}) or {})
        existing_policies = list(getattr(settings, "POLICIES", []) or [])

        new_keywords = dict(existing_keywords)
        new_policies = [dict(p) for p in existing_policies]

        # Map existing lower-case keywords to preserve existing custom rules
        existing_kw_lower = {k.lower(): k for k in existing_keywords.keys()}

        for kw, target_path in self.keyword_rules.items():
            kw_clean = str(kw).strip()
            if not kw_clean:
                continue
            kw_lower = kw_clean.lower()
            if kw_lower not in existing_kw_lower and kw_clean not in new_keywords:
                new_keywords[kw_clean] = target_path

        for policy in self.policies:
            p_type = str(policy.get("type", "")).strip().lower()
            p_expr = str(policy.get("expression", "")).strip().lower()

            if not p_expr:
                continue

            is_duplicate = False
            for existing_p in new_policies:
                ex_type = str(existing_p.get("type", "")).strip().lower()
                ex_expr = str(existing_p.get("expression", "")).strip().lower()
                if ex_type == p_type and ex_expr == p_expr:
                    is_duplicate = True
                    break

            if not is_duplicate:
                new_policies.append(dict(policy))

        setattr(settings, "KEYWORD_RULES", new_keywords)
        setattr(settings, "POLICIES", new_policies)

        if hasattr(settings, "_save"):
            try:
                settings._save()
            except Exception as e:
                logger.debug(f"Save trigger post rule import failed: {e}")

        return new_keywords, new_policies


class BaseRuleImporter(abc.ABC):
    """Abstract base class for rule strategy importers."""

    @abc.abstractmethod
    def parse_file(self, file_path: Union[str, Path]) -> ImportResult:
        """Parse rule package file and return extracted ImportResult."""
        pass

    @abc.abstractmethod
    def can_import(self, file_path: Union[str, Path]) -> bool:
        """Check if this importer handles the specified file path or content."""
        pass

    def _sanitize_path(self, path_str: str) -> str:
        """Normalize target folder path strings for Sortify configuration."""
        if not path_str:
            return ""
        clean = str(path_str).strip().replace("\\", "/")
        clean = clean.strip("\"'")
        return clean

    def _is_script_action(self, action_data: Any) -> bool:
        """Detect embedded scripts or command executions to ensure safe parsing."""
        if isinstance(action_data, str):
            lower = action_data.lower()
            return any(
                k in lower
                for k in (
                    "script",
                    "applescript",
                    "shell",
                    "bash",
                    "python",
                    "cmd.exe",
                    "exec",
                )
            )
        elif isinstance(action_data, dict):
            act_type = str(
                action_data.get("type", "")
                or action_data.get("action", "")
                or action_data.get("NSRuleActionType", "")
                or action_data.get("Action", "")
            ).lower()
            return any(
                k in act_type
                for k in (
                    "script",
                    "applescript",
                    "shell",
                    "bash",
                    "python",
                    "cmd",
                    "exec",
                    "run",
                )
            )
        return False


class HazelRuleImporter(BaseRuleImporter):
    """Importer strategy for Hazel .hazelrules XML/plist packages."""

    def can_import(self, file_path: Union[str, Path]) -> bool:
        """Return True if file extension is .hazelrules/.plist or header resembles plist."""
        p = Path(file_path)
        if p.suffix.lower() in (".hazelrules", ".plist"):
            return True
        try:
            with open(p, "rb") as f:
                header = f.read(100)
                return b"plist" in header or b"bplist" in header
        except Exception:
            return False

    def parse_file(self, file_path: Union[str, Path]) -> ImportResult:
        """Parse Hazel .hazelrules plist file into Sortify rules."""
        result = ImportResult(source_format="Hazel (.hazelrules)")
        p = Path(file_path)

        if not p.exists():
            result.warnings.append(f"File not found: '{file_path}'")
            return result

        plist_data = None
        try:
            with open(p, "rb") as f:
                plist_data = plistlib.load(f)
        except Exception as exc:
            logger.warning(f"Failed to parse plist file '{file_path}': {exc}")
            result.warnings.append(
                f"Failed to parse Hazel plist file '{p.name}': {exc}"
            )
            return result

        if not plist_data:
            result.warnings.append(f"Hazel rule package '{p.name}' is empty.")
            return result

        rules_list: List[Any] = []
        if isinstance(plist_data, dict):
            rules_list = (
                plist_data.get("Rules")
                or plist_data.get("HazelRules")
                or plist_data.get("rules")
                or []
            )
            if not rules_list and ("NSRuleName" in plist_data or "Name" in plist_data):
                rules_list = [plist_data]
        elif isinstance(plist_data, list):
            rules_list = plist_data

        if not isinstance(rules_list, list):
            result.warnings.append(
                f"Unrecognized Hazel rule package structure in '{p.name}'."
            )
            return result

        for idx, rule in enumerate(rules_list):
            if not isinstance(rule, dict):
                continue

            rule_name = (
                rule.get("NSRuleName")
                or rule.get("Name")
                or rule.get("title")
                or f"Rule #{idx + 1}"
            )

            # Check for script actions in NSRuleActions / Actions
            actions = (
                rule.get("NSRuleActions")
                or rule.get("Actions")
                or rule.get("NSActions")
                or []
            )

            target_dir = ""
            has_script_action = False

            if isinstance(actions, list):
                for act in actions:
                    if self._is_script_action(act):
                        has_script_action = True
                        result.unsupported.append(
                            {
                                "rule": rule_name,
                                "reason": "Embedded script action omitted for security.",
                            }
                        )
                        result.warnings.append(
                            f"Hazel rule '{rule_name}' contains embedded script action which was omitted for security."
                        )

                    if isinstance(act, dict):
                        act_target = (
                            act.get("DestinationPath")
                            or act.get("Destination")
                            or act.get("target")
                            or act.get("folder")
                            or act.get("path")
                            or act.get("TargetFolder")
                        )
                        if act_target and isinstance(act_target, str):
                            target_dir = self._sanitize_path(act_target)
                    elif isinstance(act, str) and not has_script_action:
                        if "/" in act or "\\" in act:
                            target_dir = self._sanitize_path(act)

            if not target_dir:
                # Top level destination fallback
                top_target = (
                    rule.get("DestinationPath")
                    or rule.get("Destination")
                    or rule.get("TargetFolder")
                )
                if top_target and isinstance(top_target, str):
                    target_dir = self._sanitize_path(top_target)

            if not target_dir:
                result.warnings.append(
                    f"Hazel rule '{rule_name}' has no target destination folder; omitted auto-routing."
                )
                result.unsupported.append(
                    {"rule": rule_name, "reason": "Missing target destination path"}
                )
                continue

            conditions = (
                rule.get("NSRuleConditions")
                or rule.get("Conditions")
                or rule.get("NSConditions")
                or []
            )

            if not isinstance(conditions, list) or not conditions:
                # If no explicit conditions, map rule name as keyword if valid
                if rule_name and len(rule_name.split()) == 1:
                    result.keyword_rules[rule_name.lower()] = target_dir
                else:
                    result.policies.append(
                        {
                            "type": "keyword",
                            "expression": rule_name,
                            "target_path": target_dir,
                            "priority": 75,
                            "halting": False,
                        }
                    )
                continue

            for cond in conditions:
                if not isinstance(cond, dict):
                    continue

                attribute = str(
                    cond.get("target")
                    or cond.get("attribute")
                    or cond.get("NSRuleConditionTarget")
                    or ""
                ).lower()

                _operator = str(
                    cond.get("operator")
                    or cond.get("qualifier")
                    or cond.get("NSRuleConditionOperator")
                    or ""
                ).lower()

                val = (
                    cond.get("value")
                    or cond.get("pattern")
                    or cond.get("NSRuleConditionValue")
                    or cond.get("string")
                )

                if val is None:
                    continue

                val_str = str(val).strip()
                if not val_str:
                    continue

                if "extension" in attribute or "pattern" in attribute or "*" in val_str:
                    # Pattern policy rule
                    expr = (
                        val_str
                        if val_str.startswith("*") or "." in val_str
                        else f"*.{val_str}"
                    )
                    result.policies.append(
                        {
                            "type": "pattern",
                            "expression": expr,
                            "target_path": target_dir,
                            "priority": 75,
                            "halting": False,
                        }
                    )
                else:
                    # Simple single-word keyword vs phrase policy
                    if len(val_str.split()) == 1 and all(
                        c.isalnum() or c in "_-" for c in val_str
                    ):
                        result.keyword_rules[val_str.lower()] = target_dir
                    else:
                        result.policies.append(
                            {
                                "type": "keyword",
                                "expression": val_str,
                                "target_path": target_dir,
                                "priority": 75,
                                "halting": False,
                            }
                        )

        return result


class DropItRuleImporter(BaseRuleImporter):
    """Importer strategy for DropIt XML and JSON profile export packages."""

    def can_import(self, file_path: Union[str, Path]) -> bool:
        """Return True if file extension or content matches DropIt export formats."""
        p = Path(file_path)
        ext = p.suffix.lower()
        if ext in (".xml", ".json", ".dropit"):
            return True
        return False

    def parse_file(self, file_path: Union[str, Path]) -> ImportResult:
        """Parse DropIt XML or JSON profile export file into Sortify rules."""
        result = ImportResult(source_format="DropIt Export")
        p = Path(file_path)

        if not p.exists():
            result.warnings.append(f"File not found: '{file_path}'")
            return result

        content = ""
        try:
            with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
                content = f.read()
        except Exception as exc:
            logger.warning(f"Failed to read file '{file_path}': {exc}")
            result.warnings.append(f"Failed to read file '{p.name}': {exc}")
            return result

        content_strip = content.strip()
        if not content_strip:
            result.warnings.append(f"DropIt export file '{p.name}' is empty.")
            return result

        if content_strip.startswith("<") or p.suffix.lower() == ".xml":
            return self._parse_xml(content_strip, p.name, result)
        else:
            return self._parse_json(content_strip, p.name, result)

    def _parse_xml(
        self, xml_content: str, file_name: str, result: ImportResult
    ) -> ImportResult:
        """Parse DropIt XML profile representation."""
        result.source_format = "DropIt XML Profile"
        try:
            root = ET.fromstring(xml_content)
        except Exception as exc:
            logger.warning(f"Failed to parse XML in '{file_name}': {exc}")
            result.warnings.append(
                f"Failed to parse XML in DropIt profile '{file_name}': {exc}"
            )
            return result

        items = []
        # Support various DropIt XML tags: Association, Rule, Profile, Item
        for elem in root.iter():
            tag_lower = elem.tag.lower()
            if tag_lower in ("association", "rule", "item", "profileitem"):
                items.append(elem)

        if not items and root.tag.lower() in ("association", "rule", "item"):
            items = [root]

        for idx, item in enumerate(items):
            rule_name = ""
            pattern = ""
            dest = ""
            action = ""

            for child in item:
                ctag = child.tag.lower()
                cval = (child.text or "").strip()
                if ctag in ("name", "title"):
                    rule_name = cval
                elif ctag in ("rules", "pattern", "filter", "mask"):
                    pattern = cval
                elif ctag in ("destination", "directory", "folder", "target"):
                    dest = cval
                elif ctag in ("action", "type"):
                    action = cval

            if not rule_name:
                rule_name = f"DropIt Rule #{idx + 1}"

            if self._is_script_action(action):
                result.unsupported.append(
                    {
                        "rule": rule_name,
                        "reason": f"External script/command action '{action}' omitted.",
                    }
                )
                result.warnings.append(
                    f"DropIt rule '{rule_name}' contains command/script execution action which was omitted for security."
                )

            target_dir = self._sanitize_path(dest)
            if not target_dir:
                result.warnings.append(
                    f"DropIt rule '{rule_name}' has no target directory; omitted auto-routing."
                )
                result.unsupported.append(
                    {"rule": rule_name, "reason": "Missing target destination path"}
                )
                continue

            self._process_pattern_and_dest(rule_name, pattern, target_dir, result)

        return result

    def _parse_json(
        self, json_content: str, file_name: str, result: ImportResult
    ) -> ImportResult:
        """Parse DropIt JSON profile representation."""
        result.source_format = "DropIt JSON Profile"
        try:
            data = json.loads(json_content)
        except Exception as exc:
            logger.warning(f"Failed to parse JSON in '{file_name}': {exc}")
            result.warnings.append(
                f"Failed to parse JSON in DropIt profile '{file_name}': {exc}"
            )
            return result

        rules_list = []
        if isinstance(data, list):
            rules_list = data
        elif isinstance(data, dict):
            rules_list = (
                data.get("rules")
                or data.get("associations")
                or data.get("profile")
                or data.get("items")
                or []
            )
            if not rules_list and (
                "Pattern" in data or "Rules" in data or "Name" in data
            ):
                rules_list = [data]

        if not isinstance(rules_list, list):
            result.warnings.append(
                f"Unrecognized DropIt JSON profile structure in '{file_name}'."
            )
            return result

        for idx, rule in enumerate(rules_list):
            if not isinstance(rule, dict):
                continue

            rule_name = (
                rule.get("Name")
                or rule.get("title")
                or rule.get("name")
                or f"DropIt Rule #{idx + 1}"
            )

            pattern = (
                rule.get("Pattern")
                or rule.get("Rules")
                or rule.get("pattern")
                or rule.get("filter")
                or ""
            )

            dest = (
                rule.get("Destination")
                or rule.get("Directory")
                or rule.get("folder")
                or rule.get("target")
                or rule.get("destination")
                or ""
            )

            action = rule.get("Action") or rule.get("action") or ""

            if self._is_script_action(action):
                result.unsupported.append(
                    {
                        "rule": rule_name,
                        "reason": f"External script/command action '{action}' omitted.",
                    }
                )
                result.warnings.append(
                    f"DropIt rule '{rule_name}' contains command/script execution action which was omitted for security."
                )

            target_dir = self._sanitize_path(dest)
            if not target_dir:
                result.warnings.append(
                    f"DropIt rule '{rule_name}' has no target directory; omitted auto-routing."
                )
                result.unsupported.append(
                    {"rule": rule_name, "reason": "Missing target destination path"}
                )
                continue

            self._process_pattern_and_dest(rule_name, str(pattern), target_dir, result)

        return result

    def _process_pattern_and_dest(
        self,
        rule_name: str,
        pattern: str,
        target_dir: str,
        result: ImportResult,
    ) -> None:
        """Convert DropIt pattern mask string into keyword rules or policy rules."""
        if not pattern:
            # Fallback to rule name
            pattern = rule_name

        # DropIt pattern masks use ';' or '|' or ',' as mask separators
        masks = [m.strip() for m in re.split(r"[;|,]", pattern) if m.strip()]

        for mask in masks:
            if "*" in mask or "?" in mask or mask.startswith("."):
                result.policies.append(
                    {
                        "type": "pattern",
                        "expression": mask,
                        "target_path": target_dir,
                        "priority": 75,
                        "halting": False,
                    }
                )
            else:
                if len(mask.split()) == 1 and all(
                    c.isalnum() or c in "_-" for c in mask
                ):
                    result.keyword_rules[mask.lower()] = target_dir
                else:
                    result.policies.append(
                        {
                            "type": "keyword",
                            "expression": mask,
                            "target_path": target_dir,
                            "priority": 75,
                            "halting": False,
                        }
                    )


def get_rule_importer(file_path: Union[str, Path]) -> BaseRuleImporter:
    """Return auto-detected rule importer strategy for the given file path.

    Args:
        file_path: Path to rule package file.

    Returns
    -------
        An instance of BaseRuleImporter strategy class.

    Raises
    ------
        ValueError: If file format is not supported.
    """
    p = Path(file_path)
    hazel_importer = HazelRuleImporter()
    dropit_importer = DropItRuleImporter()

    if hazel_importer.can_import(p):
        return hazel_importer

    if dropit_importer.can_import(p):
        return dropit_importer

    # Fallback extension detection
    ext = p.suffix.lower()
    if ext in (".hazelrules", ".plist"):
        return hazel_importer
    elif ext in (".xml", ".json", ".dropit"):
        return dropit_importer

    raise ValueError(
        f"Unsupported rule file format for '{p.name}'. Expected .hazelrules, .plist, .xml, or .json."
    )


def import_and_merge_rules(file_path: Union[str, Path], settings: Any) -> ImportResult:
    """Parse external rule package and merge into settings.

    Args:
        file_path: Path to external rule package.
        settings: Application settings object.

    Returns
    -------
        ImportResult object containing parsed rules and merge outcome.
    """
    importer = get_rule_importer(file_path)
    result = importer.parse_file(file_path)
    result.merge_into_settings(settings)
    return result
