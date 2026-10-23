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
            settings._save()

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
        except OSError:
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
            logger.warning(
                f"Failed to parse plist file '{file_path}': {exc}", exc_info=True
            )
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
        except OSError as exc:
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
        except (ET.ParseError, ValueError) as exc:
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
        except ValueError as exc:
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


class TagSpacesRuleImporter(BaseRuleImporter):
    """Importer strategy for TagSpaces taggroup.json taxonomy files and smart rules."""

    def can_import(self, file_path: Union[str, Path]) -> bool:
        """Return True if file is a valid TagSpaces taggroup.json export or tag taxonomy JSON file."""
        p = Path(file_path)
        name_lower = p.name.lower()

        if (
            name_lower in ("taggroup.json", "taggroups.json")
            or "taggroup" in name_lower
            or "tagspaces" in name_lower
        ):
            return True

        if p.suffix.lower() != ".json":
            return False

        if not p.exists():
            return False

        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return False

        return self._is_tagspaces_data(data)

    def _is_tagspaces_data(self, data: Any) -> bool:
        """Check if parsed JSON data matches TagSpaces structure."""
        if isinstance(data, dict):
            for key in (
                "tagGroups",
                "taggroups",
                "tagGroup",
                "taggroup",
                "taggroupVersion",
                "tagspaces",
                "smartRules",
                "smart_rules",
            ):
                if key in data:
                    return True

            if any(
                k in data for k in ("Pattern", "Destination", "Association", "Profile")
            ):
                return False

            if ("title" in data or "name" in data) and (
                "children" in data or "tags" in data or "subgroups" in data
            ):
                return True

        elif isinstance(data, list):
            if not data:
                return False
            first = data[0]
            if isinstance(first, dict):
                if any(
                    k in first
                    for k in ("Pattern", "Destination", "Association", "Profile")
                ):
                    return False
                if ("title" in first or "name" in first) and (
                    "children" in first
                    or "tags" in first
                    or "color" in first
                    or "textcolor" in first
                    or "icon" in first
                    or "isSmart" in first
                ):
                    return True
                if ("name" in first or "title" in first) and (
                    "conditions" in first or "matchers" in first
                ):
                    return True

        return False

    def parse_file(self, file_path: Union[str, Path]) -> ImportResult:
        """Parse TagSpaces taggroup.json or smart rule JSON file into Sortify rules."""
        result = ImportResult(source_format="TagSpaces (taggroup.json)")
        p = Path(file_path)

        if not p.exists():
            result.warnings.append(f"File not found: '{file_path}'")
            return result

        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            logger.warning(
                f"Failed to parse TagSpaces JSON file '{file_path}': {exc}",
                exc_info=True,
            )
            result.warnings.append(
                f"Failed to parse TagSpaces JSON file '{p.name}': {exc}"
            )
            return result

        if not data:
            result.warnings.append(f"TagSpaces rule package '{p.name}' is empty.")
            return result

        # 1. Process Tag Groups / Taxonomy Hierarchy
        tag_groups: List[Any] = []
        if isinstance(data, dict):
            tag_groups = (
                data.get("tagGroups")
                or data.get("taggroups")
                or data.get("tagGroup")
                or data.get("taggroup")
                or data.get("groups")
                or []
            )
            if not tag_groups and (
                "children" in data
                or "tags" in data
                or "title" in data
                or "name" in data
            ):
                tag_groups = [data]
        elif isinstance(data, list):
            tag_groups = [
                item
                for item in data
                if isinstance(item, dict)
                and (
                    "children" in item
                    or "tags" in item
                    or "color" in item
                    or "textcolor" in item
                )
            ]
            if (
                not tag_groups
                and data
                and all(
                    isinstance(item, dict) and ("title" in item or "name" in item)
                    for item in data
                )
            ):
                tag_groups = data

        for group in tag_groups:
            if isinstance(group, dict):
                self._traverse_tag_group(group, [], result)

        # 2. Process Smart Rules
        smart_rules: List[Any] = []
        if isinstance(data, dict):
            smart_rules = (
                data.get("smartRules")
                or data.get("smart_rules")
                or data.get("rules")
                or []
            )
        elif isinstance(data, list):
            smart_rules = [
                item
                for item in data
                if isinstance(item, dict)
                and (
                    "conditions" in item
                    or "matchers" in item
                    or "actions" in item
                    or "destination" in item
                    or "target" in item
                )
            ]

        for idx, rule in enumerate(smart_rules):
            if isinstance(rule, dict):
                self._process_smart_rule(rule, idx, result)

        if not result.keyword_rules and not result.policies and not result.warnings:
            result.warnings.append(
                f"No convertible TagSpaces tag groups or smart rules found in '{p.name}'."
            )

        return result

    def _traverse_tag_group(
        self,
        node: Dict[str, Any],
        parent_path: List[str],
        result: ImportResult,
    ) -> None:
        """Recursively traverse nested tag group node and map tags to path hierarchies."""
        node_title = str(
            node.get("title")
            or node.get("name")
            or node.get("text")
            or node.get("label")
            or ""
        ).strip()

        current_path = list(parent_path)
        if node_title and node_title.lower() not in (
            "taggroups",
            "tag_groups",
            "root",
        ):
            current_path.append(node_title)

        target_dir = self._sanitize_path("/".join(current_path))

        raw_tags = node.get("tags") or []
        if isinstance(raw_tags, (str, dict)):
            raw_tags = [raw_tags]

        raw_children = (
            node.get("children")
            or node.get("subgroups")
            or node.get("groups")
            or node.get("items")
            or []
        )
        if isinstance(raw_children, (str, dict)):
            raw_children = [raw_children]

        for tag_item in raw_tags:
            if isinstance(tag_item, str):
                self._add_tag_rule(tag_item, target_dir, result)
            elif isinstance(tag_item, dict):
                if any(
                    k in tag_item for k in ("children", "subgroups", "groups", "tags")
                ):
                    self._traverse_tag_group(tag_item, current_path, result)
                else:
                    tag_name = (
                        tag_item.get("title")
                        or tag_item.get("name")
                        or tag_item.get("text")
                        or tag_item.get("tag")
                        or tag_item.get("label")
                        or ""
                    )
                    if tag_name:
                        self._add_tag_rule(str(tag_name), target_dir, result)

        for child in raw_children:
            if isinstance(child, str):
                self._add_tag_rule(child, target_dir, result)
            elif isinstance(child, dict):
                if (
                    any(k in child for k in ("children", "subgroups", "groups", "tags"))
                    or "color" in child
                    or "textcolor" in child
                ):
                    self._traverse_tag_group(child, current_path, result)
                else:
                    tag_name = (
                        child.get("title")
                        or child.get("name")
                        or child.get("text")
                        or child.get("tag")
                        or child.get("label")
                        or ""
                    )
                    if tag_name:
                        self._add_tag_rule(str(tag_name), target_dir, result)

    def _add_tag_rule(
        self, tag_str: str, target_dir: str, result: ImportResult
    ) -> None:
        """Map a tag string to a keyword rule or policy based on single-word vs multi-word/pattern."""
        tag_clean = tag_str.strip()
        if not tag_clean:
            return

        if not target_dir:
            result.warnings.append(
                f"Tag '{tag_clean}' in group has no target directory; omitted auto-routing."
            )
            result.unsupported.append(
                {"tag": tag_clean, "reason": "Missing target destination path"}
            )
            return

        if "*" in tag_clean or "?" in tag_clean or tag_clean.startswith("."):
            result.policies.append(
                {
                    "type": "pattern",
                    "expression": tag_clean,
                    "target_path": target_dir,
                    "priority": 75,
                    "halting": False,
                }
            )
        else:
            if len(tag_clean.split()) == 1 and all(
                c.isalnum() or c in "_-" for c in tag_clean
            ):
                result.keyword_rules[tag_clean.lower()] = target_dir
            else:
                result.policies.append(
                    {
                        "type": "keyword",
                        "expression": tag_clean,
                        "target_path": target_dir,
                        "priority": 75,
                        "halting": False,
                    }
                )

    def _process_smart_rule(
        self, rule: Dict[str, Any], idx: int, result: ImportResult
    ) -> None:
        """Process a TagSpaces smart rule entry, detecting condition matchers and filtering script actions."""
        rule_name = (
            rule.get("name")
            or rule.get("title")
            or rule.get("ruleName")
            or f"TagSpaces Smart Rule #{idx + 1}"
        )

        actions = rule.get("actions") or rule.get("action") or []
        if isinstance(actions, (str, dict)):
            actions = [actions]

        has_script = False
        for act in actions:
            if self._is_script_action(act):
                has_script = True
                break
        if not has_script:
            has_script = self._is_script_action(rule)

        if has_script:
            result.unsupported.append(
                {
                    "rule": rule_name,
                    "reason": "External script/command action omitted.",
                }
            )
            result.warnings.append(
                f"TagSpaces rule '{rule_name}' contains command/script execution action which was omitted for security."
            )

        dest = (
            rule.get("destination")
            or rule.get("target")
            or rule.get("folder")
            or rule.get("directory")
            or rule.get("targetPath")
            or ""
        )
        if not dest and actions:
            for act in actions:
                if isinstance(act, dict):
                    dest = (
                        act.get("target")
                        or act.get("destination")
                        or act.get("folder")
                        or act.get("path")
                        or ""
                    )
                    if dest:
                        break

        target_dir = self._sanitize_path(str(dest))
        if not target_dir:
            result.warnings.append(
                f"TagSpaces rule '{rule_name}' has no target directory; omitted auto-routing."
            )
            result.unsupported.append(
                {"rule": rule_name, "reason": "Missing target destination path"}
            )
            return

        conditions = (
            rule.get("conditions")
            or rule.get("condition")
            or rule.get("matchers")
            or rule.get("pattern")
            or []
        )
        if isinstance(conditions, (str, dict)):
            conditions = [conditions]

        for cond in conditions:
            if isinstance(cond, str):
                self._add_smart_rule_condition(cond, target_dir, result)
            elif isinstance(cond, dict):
                cond_type = str(
                    cond.get("field")
                    or cond.get("type")
                    or cond.get("attribute")
                    or cond.get("target")
                    or ""
                ).lower()
                val = str(
                    cond.get("value")
                    or cond.get("pattern")
                    or cond.get("expression")
                    or cond.get("val")
                    or ""
                ).strip()

                if not val:
                    continue

                if "ext" in cond_type or "extension" in cond_type:
                    expr = val if val.startswith("*.") else f"*.{val.lstrip('.')}"
                    result.policies.append(
                        {
                            "type": "pattern",
                            "expression": expr,
                            "target_path": target_dir,
                            "priority": 85,
                            "halting": False,
                        }
                    )
                else:
                    self._add_smart_rule_condition(val, target_dir, result, priority=85)

    def _add_smart_rule_condition(
        self,
        val_str: str,
        target_dir: str,
        result: ImportResult,
        priority: int = 85,
    ) -> None:
        """Add smart rule condition matcher to keyword rules or policy list."""
        val_clean = val_str.strip()
        if not val_clean:
            return

        if "*" in val_clean or "?" in val_clean or val_clean.startswith("."):
            result.policies.append(
                {
                    "type": "pattern",
                    "expression": val_clean,
                    "target_path": target_dir,
                    "priority": priority,
                    "halting": False,
                }
            )
        elif len(val_clean.split()) == 1 and all(
            c.isalnum() or c in "_-" for c in val_clean
        ):
            result.keyword_rules[val_clean.lower()] = target_dir
        else:
            result.policies.append(
                {
                    "type": "keyword",
                    "expression": val_clean,
                    "target_path": target_dir,
                    "priority": priority,
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
    tagspaces_importer = TagSpacesRuleImporter()
    hazel_importer = HazelRuleImporter()
    dropit_importer = DropItRuleImporter()

    if tagspaces_importer.can_import(p):
        return tagspaces_importer

    if hazel_importer.can_import(p):
        return hazel_importer

    if dropit_importer.can_import(p):
        return dropit_importer

    # Fallback extension detection
    ext = p.suffix.lower()
    if (
        p.name.lower() in ("taggroup.json", "taggroups.json")
        or "taggroup" in p.name.lower()
    ):
        return tagspaces_importer
    elif ext in (".hazelrules", ".plist"):
        return hazel_importer
    elif ext in (".xml", ".json", ".dropit"):
        return dropit_importer

    raise ValueError(
        f"Unsupported rule file format for '{p.name}'. Expected .hazelrules, .plist, .xml, or .json."
    )


