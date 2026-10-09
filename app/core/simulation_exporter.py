"""Simulation Exporter for dry-run reports in JSON and text formats."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

from app.core.domain_contracts import _get_val, _make_json_serializable
from app.core.path_utils import scrub_user_home_paths
from app.core.text_utils import sanitize_secret_patterns
from app.core.verifier import VerificationEngine


class SimulationExporter:
    """Engine to serialize sorting plans and dry-run simulation results into JSON and text reports."""

    def __init__(
        self,
        plan: Any,
        base_dir: str = "",
        simulation_results: Optional[Dict[str, Any]] = None,
    ):
        self.plan = plan
        self.base_dir = os.path.normpath(base_dir) if base_dir else ""
        if simulation_results is not None:
            self.simulation_results = simulation_results
        else:
            self.simulation_results = VerificationEngine.verify_plan_integrity(
                self.base_dir, self.plan
            )

        self._cached_report_data: Optional[Dict[str, Any]] = None
        self._scrub_cache: Dict[str, str] = {}

        # Build compiled regex for user home paths and secret triggers
        home_dirs = []
        try:
            ph = str(Path.home())
            if ph and len(ph.strip("\\/ ")) > 2:
                home_dirs.append(ph)
        except Exception:
            pass
        try:
            eu = os.path.expanduser("~")
            if eu and len(eu.strip("\\/ ")) > 2 and eu not in home_dirs:
                home_dirs.append(eu)
        except Exception:
            pass
        for env_var in ("USERPROFILE", "HOME", "HOMEPATH"):
            val = os.environ.get(env_var)
            if val and len(val.strip("\\/ ")) > 2 and val not in home_dirs:
                home_dirs.append(val)
        self._home_dir_strs = home_dirs

        if self._home_dir_strs:
            sorted_homes = sorted(self._home_dir_strs, key=len, reverse=True)
            pattern = "|".join(re.escape(h) for h in sorted_homes)
            self._home_re: Optional[re.Pattern] = re.compile(pattern, re.IGNORECASE)
        else:
            self._home_re = None

        self._secret_trigger_re: re.Pattern = re.compile(
            r"enc:|password|passwd|secret|api_key|apikey|access_token|auth_token|sk_|gh|AKIA|ASIA|Bearer|eyJ|BEGIN|KEY",
            re.IGNORECASE,
        )

    def _scrub(self, text: Optional[str]) -> str:
        """Scrub user home paths and sensitive credentials from text string with fast-path optimization."""
        if not text or not isinstance(text, str):
            return "" if text is None else str(text)

        if len(text) <= 2:
            return text

        cached = self._scrub_cache.get(text)
        if cached is not None:
            return cached

        orig_text = text
        if self._home_re and self._home_re.search(text):
            text = scrub_user_home_paths(text)

        if self._secret_trigger_re.search(text):
            text = sanitize_secret_patterns(text)

        self._scrub_cache[orig_text] = text
        return text

    def _scrub_structure(self, data: Any) -> Any:
        """Recursively scrub strings inside dictionary and list structures."""
        if isinstance(data, str):
            return self._scrub(data)
        elif isinstance(data, dict):
            return {
                (self._scrub(k) if isinstance(k, str) and ("/" in k or "\\" in k or "_" in k) else k): self._scrub_structure(v)
                for k, v in data.items()
            }
        elif isinstance(data, (list, tuple)):
            return [self._scrub_structure(item) for item in data]
        return data

    def _extract_move_mappings(self) -> List[Dict[str, Any]]:
        """Extract flat move mappings with metadata, sensitivity ratings, collision flags, and confirmation status."""
        moves: List[Dict[str, Any]] = []

        raw_plan = (
            self.plan.plan
            if hasattr(self.plan, "plan") and isinstance(self.plan.plan, dict)
            else self.plan
        )

        collision_paths = set()
        for col in self.simulation_results.get("collisions", []):
            if "path" in col:
                collision_paths.add(os.path.normpath(col["path"]))
            if "source" in col:
                collision_paths.add(os.path.normpath(col["source"]))

        circular_paths = set()
        for circ in self.simulation_results.get("circular_renames", []):
            if "path" in circ:
                circular_paths.add(os.path.normpath(circ["path"]))

        def _traverse(node: Any, current_dest: str = ""):
            curr_dict = (
                node.plan
                if hasattr(node, "plan") and isinstance(node.plan, dict)
                else node
            )
            if not isinstance(curr_dict, dict):
                return

            for key, content in curr_dict.items():
                node_type = _get_val(content, "node_type") or _get_val(
                    content, "__type__"
                )
                is_file = node_type == "file" or (
                    isinstance(content, dict)
                    and (
                        "filepath" in content
                        or "relative_source" in content
                        or "target_filename" in content
                        or "sensitivity_rating" in content
                    )
                )

                if is_file:
                    rel_src = _get_val(content, "relative_source") or key
                    if self.base_dir and not os.path.isabs(rel_src):
                        source_path = os.path.normpath(
                            os.path.join(self.base_dir, rel_src)
                        )
                    else:
                        source_path = os.path.normpath(rel_src)

                    raw_tf = str(_get_val(content, "target_filename") or key)
                    target_fn = raw_tf.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]

                    if current_dest:
                        dest_dir = (
                            os.path.normpath(os.path.join(self.base_dir, current_dest))
                            if self.base_dir
                            else current_dest
                        )
                    else:
                        dest_dir = self.base_dir

                    target_path = (
                        os.path.normpath(os.path.join(dest_dir, target_fn))
                        if dest_dir
                        else target_fn
                    )

                    sens_rating = str(
                        _get_val(content, "sensitivity_rating", "LOW") or "LOW"
                    ).upper()
                    sens_score = float(
                        _get_val(content, "sensitivity_score", 0.0) or 0.0
                    )

                    policy_action = str(
                        _get_val(content, "policy_action", "") or ""
                    ).lower()

                    status_val = str(_get_val(content, "status", "") or "")
                    is_confirmed = bool(
                        _get_val(content, "confirmed")
                        or _get_val(content, "is_confirmed")
                        or _get_val(content, "user_confirmed")
                        or status_val in ("Confirmed", "Locked")
                    )

                    has_collision = (
                        source_path in collision_paths
                        or target_path in collision_paths
                        or source_path in circular_paths
                        or target_path in circular_paths
                    )

                    is_quarantine_hold = (
                        sens_rating in ("HIGH", "CRITICAL", "RESTRICTED", "QUARANTINE")
                        or policy_action in ("sensitivity_hold", "quarantine")
                    )

                    moves.append(
                        {
                            "source_path": self._scrub(source_path),
                            "target_path": self._scrub(target_path),
                            "target_filename": self._scrub(target_fn),
                            "sensitivity_rating": sens_rating,
                            "sensitivity_score": sens_score,
                            "collision_flag": has_collision,
                            "confirmation_status": "Confirmed"
                            if is_confirmed
                            else "Unconfirmed",
                            "quarantine_hold": is_quarantine_hold,
                            "status": "QUARANTINED"
                            if is_quarantine_hold
                            else ("COLLISION" if has_collision else "PLANNED"),
                        }
                    )
                elif isinstance(content, dict) and node_type != "file":
                    _traverse(content, os.path.join(current_dest, key))

        _traverse(raw_plan)
        return moves

    def generate_report_data(self) -> Dict[str, Any]:
        """Generate structured simulation report dictionary with caching."""
        if self._cached_report_data is not None:
            return self._cached_report_data

        serializable_plan = _make_json_serializable(self.plan)
        scrubbed_plan = self._scrub_structure(serializable_plan)

        move_mappings = self._extract_move_mappings()

        collisions = self._scrub_structure(
            self.simulation_results.get("collisions", [])
        )
        circular_renames = self._scrub_structure(
            self.simulation_results.get("circular_renames", [])
        )
        broken_links = self._scrub_structure(
            self.simulation_results.get("broken_links", [])
        )
        long_paths = self._scrub_structure(
            self.simulation_results.get("long_paths", [])
        )
        invalid_renames = self._scrub_structure(
            self.simulation_results.get("invalid_renames", [])
        )
        unconfirmed_renames = self._scrub_structure(
            self.simulation_results.get("unconfirmed_renames", [])
        )

        all_warnings = self._scrub_structure(
            self.simulation_results.get("warnings", [])
        )

        sensitivity_holds_count = sum(
            1 for m in move_mappings if m.get("quarantine_hold")
        )
        unconfirmed_count = sum(
            1 for m in move_mappings if m.get("confirmation_status") == "Unconfirmed"
        )

        success = self.simulation_results.get("success", False)
        if not success or len(collisions) > 0 or len(circular_renames) > 0:
            safety_status = "SAFETY_HOLD"
        elif len(all_warnings) > 0 or sensitivity_holds_count > 0:
            safety_status = "WARNINGS_DETECTED"
        else:
            safety_status = "PASSED_SAFETY_CHECK"

        now_str = datetime.now(timezone.utc).isoformat()

        res = {
            "title": "Sortify Dry-Run Simulation Report",
            "generated_at": now_str,
            "base_directory": self._scrub(self.base_dir),
            "summary": {
                "total_files": len(move_mappings),
                "total_moves": len(move_mappings),
                "collisions_count": len(collisions),
                "circular_renames_count": len(circular_renames),
                "broken_links_count": len(broken_links),
                "long_paths_count": len(long_paths),
                "invalid_renames_count": len(invalid_renames),
                "unconfirmed_renames_count": unconfirmed_count,
                "sensitivity_holds_count": sensitivity_holds_count,
                "total_warnings": len(all_warnings),
                "success": success,
                "safety_status": safety_status,
            },
            "simulation_warnings": all_warnings,
            "collisions": collisions,
            "circular_renames": circular_renames,
            "broken_links": broken_links,
            "long_paths": long_paths,
            "invalid_renames": invalid_renames,
            "unconfirmed_renames": unconfirmed_renames,
            "move_mappings": move_mappings,
            "plan_hierarchy": scrubbed_plan,
        }
        self._cached_report_data = res
        return res

    def export_json(
        self, output_path: Optional[Union[str, Path]] = None
    ) -> str:
        """Serialize simulation report into JSON format and write to output_path if specified."""
        data = self.generate_report_data()
        json_content = json.dumps(data, indent=2)

        if output_path is not None:
            p = Path(output_path)
            if p.parent and not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json_content, encoding="utf-8")

        return json_content

    def export_text(
        self, output_path: Optional[Union[str, Path]] = None
    ) -> str:
        """Serialize simulation report into human-readable plain text format and write to output_path if specified."""
        data = self.generate_report_data()
        summary = data["summary"]
        move_mappings = data["move_mappings"]
        warnings = data["simulation_warnings"]

        lines = [
            "=" * 80,
            "                    SORTIFY DRY-RUN SIMULATION REPORT",
            "=" * 80,
            f"Generated At: {data['generated_at']}",
            f"Base Directory: {data['base_directory']}",
            f"Safety Status: {summary['safety_status']}",
            "",
            "SUMMARY METRICS:",
            "-" * 80,
            f"Total Files Analyzed:       {summary['total_files']}",
            f"Path Collisions:            {summary['collisions_count']}",
            f"Quarantine Holds:           {summary['sensitivity_holds_count']}",
            f"Broken Symlinks / Links:    {summary['broken_links_count']}",
            f"Long Paths:                 {summary['long_paths_count']}",
            f"Invalid Renames:            {summary['invalid_renames_count']}",
            f"Unconfirmed Renames:        {summary['unconfirmed_renames_count']}",
            f"Total Warnings:             {summary['total_warnings']}",
            f"Execution Safe:             {summary['success']}",
            "",
        ]

        if warnings:
            lines.append(f"SIMULATION WARNINGS ({len(warnings)}):")
            lines.append("-" * 80)
            for w in warnings:
                lines.append(f"- {w}")
            lines.append("")

        lines.append(f"MOVE MAPPINGS ({len(move_mappings)} files):")
        lines.append("-" * 80)

        if not move_mappings:
            lines.append("No file moves scheduled.")
        else:
            for idx, m in enumerate(move_mappings, start=1):
                lines.append(f"[{idx}] Source: {m['source_path']}")
                lines.append(f"    Target: {m['target_path']}")
                lines.append(f"    Filename: {m['target_filename']}")
                lines.append(
                    f"    Sensitivity: {m['sensitivity_rating']} (Score: {m['sensitivity_score']})"
                )
                lines.append(f"    Status: {m['status']}")
                lines.append(f"    Confirmation: {m['confirmation_status']}")
                lines.append("-" * 80)

        text_content = "\n".join(lines) + "\n"

        if output_path is not None:
            p = Path(output_path)
            if p.parent and not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text_content, encoding="utf-8")

        return text_content
