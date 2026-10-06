"""Simulation Exporter for dry-run reports in JSON and HTML formats."""

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
    """Engine to serialize sorting plans and dry-run simulation results into JSON and HTML reports."""

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

    def export_html(
        self, output_path: Optional[Union[str, Path]] = None
    ) -> str:
        """Serialize simulation report into standalone HTML format and write to output_path if specified."""
        data = self.generate_report_data()
        summary = data["summary"]
        move_mappings = data["move_mappings"]
        warnings = data["simulation_warnings"]

        safety_status = summary["safety_status"]
        if safety_status == "PASSED_SAFETY_CHECK":
            badge_class = "status-passed"
            badge_text = "SAFE TO EXECUTE"
        elif safety_status == "WARNINGS_DETECTED":
            badge_class = "status-warning"
            badge_text = "WARNINGS DETECTED"
        else:
            badge_class = "status-danger"
            badge_text = "SAFETY HOLD"

        # Build warning banners
        warning_items_html = [f"<li>{w}</li>" for w in warnings]
        warning_banner_html = (
            f"""
            <div class="warning-banner">
                <h3>Simulation Warnings ({len(warnings)})</h3>
                <ul>{''.join(warning_items_html)}</ul>
            </div>
            """
            if warnings
            else ""
        )

        # Build table rows with optimized row template
        row_template = (
            '<tr data-collision="{col}" data-hold="{hold}" data-confirmed="{conf}">'
            '<td class="code-cell">{src}</td>'
            '<td class="code-cell">{tgt}</td>'
            '<td>{fn}</td>'
            '<td><span class="badge {sens_cls}">{sens}</span>{hold_badge}</td>'
            '<td>{col_badge}</td>'
            '<td>{conf_badge}</td>'
            '</tr>'
        )

        badge_ok = '<span class="badge badge-success">OK</span>'
        badge_collision = '<span class="badge badge-danger">COLLISION</span>'
        badge_confirmed = '<span class="badge badge-info">Confirmed</span>'
        badge_unconfirmed = '<span class="badge badge-muted">Unconfirmed</span>'
        badge_quarantine_hold = ' <span class="badge badge-danger">QUARANTINE HOLD</span>'

        table_rows = []
        for m in move_mappings:
            sens = m["sensitivity_rating"]
            if sens in ("CRITICAL", "HIGH", "RESTRICTED", "QUARANTINE"):
                sens_badge_class = "badge-danger"
            elif sens == "MEDIUM":
                sens_badge_class = "badge-warning"
            else:
                sens_badge_class = "badge-success"

            is_col = m["collision_flag"]
            is_conf = m["confirmation_status"] == "Confirmed"
            is_hold = m.get("quarantine_hold", False)

            table_rows.append(
                row_template.format(
                    col="true" if is_col else "false",
                    hold="true" if is_hold else "false",
                    conf="true" if is_conf else "false",
                    src=m["source_path"],
                    tgt=m["target_path"],
                    fn=m["target_filename"],
                    sens_cls=sens_badge_class,
                    sens=sens,
                    hold_badge=badge_quarantine_hold if is_hold else "",
                    col_badge=badge_collision if is_col else badge_ok,
                    conf_badge=badge_confirmed if is_conf else badge_unconfirmed,
                )
            )

        table_rows_html = "".join(table_rows)

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sortify - Dry-Run Simulation Report</title>
    <style>
        :root {{
            --bg-color: #0d1117;
            --card-bg: #161b22;
            --border-color: #30363d;
            --text-color: #c9d1d9;
            --text-muted: #8b949e;
            --accent-blue: #58a6ff;
            --accent-green: #238636;
            --accent-warning: #d29922;
            --accent-danger: #da3633;
            --font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            --mono-font: SFMono-Regular, Consolas, "Liberation Mono", Menlo, monospace;
        }}
        body {{
            background-color: var(--bg-color);
            color: var(--text-color);
            font-family: var(--font-family);
            margin: 0;
            padding: 24px;
            line-height: 1.5;
        }}
        .header-container {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 16px;
            margin-bottom: 24px;
        }}
        .header-title h1 {{
            margin: 0 0 8px 0;
            font-size: 24px;
            color: #ffffff;
        }}
        .header-meta {{
            color: var(--text-muted);
            font-size: 14px;
        }}
        .status-badge {{
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: bold;
            font-size: 14px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        .status-passed {{
            background-color: rgba(35, 134, 54, 0.2);
            border: 1px solid var(--accent-green);
            color: #3fb950;
        }}
        .status-warning {{
            background-color: rgba(210, 153, 34, 0.2);
            border: 1px solid var(--accent-warning);
            color: #d29922;
        }}
        .status-danger {{
            background-color: rgba(218, 54, 51, 0.2);
            border: 1px solid var(--accent-danger);
            color: #f85149;
        }}
        .metrics-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .metric-card {{
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 16px;
        }}
        .metric-value {{
            font-size: 28px;
            font-weight: bold;
            color: #ffffff;
            margin-bottom: 4px;
        }}
        .metric-label {{
            color: var(--text-muted);
            font-size: 13px;
            text-transform: uppercase;
        }}
        .warning-banner {{
            background-color: rgba(210, 153, 34, 0.1);
            border: 1px solid var(--accent-warning);
            border-radius: 8px;
            padding: 16px;
            margin-bottom: 24px;
        }}
        .warning-banner h3 {{
            margin: 0 0 12px 0;
            color: var(--accent-warning);
        }}
        .warning-banner ul {{
            margin: 0;
            padding-left: 20px;
        }}
        .controls-row {{
            display: flex;
            gap: 12px;
            margin-bottom: 16px;
            flex-wrap: wrap;
        }}
        .search-input {{
            flex: 1;
            min-width: 250px;
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            color: var(--text-color);
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 14px;
        }}
        .filter-select {{
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            color: var(--text-color);
            padding: 8px 12px;
            border-radius: 6px;
            font-size: 14px;
        }}
        .data-table {{
            width: 100%;
            border-collapse: collapse;
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            overflow: hidden;
        }}
        .data-table th, .data-table td {{
            padding: 10px 14px;
            text-align: left;
            border-bottom: 1px solid var(--border-color);
            font-size: 13px;
        }}
        .data-table th {{
            background-color: #21262d;
            color: #ffffff;
            font-weight: 600;
        }}
        .code-cell {{
            font-family: var(--mono-font);
            font-size: 12px;
            word-break: break-all;
        }}
        .badge {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 11px;
            font-weight: 600;
        }}
        .badge-success {{ background: rgba(35, 134, 54, 0.2); color: #3fb950; border: 1px solid var(--accent-green); }}
        .badge-warning {{ background: rgba(210, 153, 34, 0.2); color: #d29922; border: 1px solid var(--accent-warning); }}
        .badge-danger {{ background: rgba(218, 54, 51, 0.2); color: #f85149; border: 1px solid var(--accent-danger); }}
        .badge-info {{ background: rgba(88, 166, 255, 0.2); color: #58a6ff; border: 1px solid var(--accent-blue); }}
        .badge-muted {{ background: rgba(139, 148, 158, 0.2); color: var(--text-muted); border: 1px solid var(--text-muted); }}
    </style>
</head>
<body>
    <div class="header-container">
        <div class="header-title">
            <h1>Sortify Dry-Run Simulation Report</h1>
            <div class="header-meta">
                Generated: {data['generated_at']} | Target Directory: <code>{data['base_directory']}</code>
            </div>
        </div>
        <div>
            <span class="status-badge {badge_class}">{badge_text}</span>
        </div>
    </div>

    <div class="metrics-grid">
        <div class="metric-card">
            <div class="metric-value">{summary['total_files']}</div>
            <div class="metric-label">Total Files Analyzed</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{summary['collisions_count']}</div>
            <div class="metric-label">Path Collisions</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{summary['sensitivity_holds_count']}</div>
            <div class="metric-label">Sensitivity Quarantine Holds</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{summary['broken_links_count']}</div>
            <div class="metric-label">Broken Symlinks / Links</div>
        </div>
        <div class="metric-card">
            <div class="metric-value">{summary['total_warnings']}</div>
            <div class="metric-label">Total Warnings</div>
        </div>
    </div>

    {warning_banner_html}

    <div class="controls-row">
        <input type="text" id="searchInput" class="search-input" placeholder="Search paths, filenames, or sensitivity...">
        <select id="filterSelect" class="filter-select">
            <option value="all">All File Mappings</option>
            <option value="collisions">Collisions Only</option>
            <option value="holds">Quarantine Holds Only</option>
            <option value="confirmed">Confirmed Only</option>
            <option value="unconfirmed">Unconfirmed Only</option>
        </select>
    </div>

    <table class="data-table" id="mappingsTable">
        <thead>
            <tr>
                <th>Source Path</th>
                <th>Target Path</th>
                <th>Filename</th>
                <th>Sensitivity Rating</th>
                <th>Collision Flag</th>
                <th>Confirmation Status</th>
            </tr>
        </thead>
        <tbody>
            {table_rows_html}
        </tbody>
    </table>

    <script>
        document.addEventListener('DOMContentLoaded', function() {{
            const searchInput = document.getElementById('searchInput');
            const filterSelect = document.getElementById('filterSelect');
            const tableRows = document.querySelectorAll('#mappingsTable tbody tr');

            function filterTable() {{
                const query = searchInput.value.toLowerCase();
                const filterVal = filterSelect.value;

                tableRows.forEach(row => {{
                    const text = row.textContent.toLowerCase();
                    const matchesSearch = text.includes(query);

                    const isCollision = row.getAttribute('data-collision') === 'true';
                    const isHold = row.getAttribute('data-hold') === 'true';
                    const isConfirmed = row.getAttribute('data-confirmed') === 'true';

                    let matchesFilter = true;
                    if (filterVal === 'collisions') matchesFilter = isCollision;
                    else if (filterVal === 'holds') matchesFilter = isHold;
                    else if (filterVal === 'confirmed') matchesFilter = isConfirmed;
                    else if (filterVal === 'unconfirmed') matchesFilter = !isConfirmed;

                    row.style.display = (matchesSearch && matchesFilter) ? '' : 'none';
                }});
            }}

            searchInput.addEventListener('input', filterTable);
            filterSelect.addEventListener('change', filterTable);
        }});
    </script>
</body>
</html>
"""

        if output_path is not None:
            p = Path(output_path)
            if p.parent and not p.parent.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(html_content, encoding="utf-8")

        return html_content
