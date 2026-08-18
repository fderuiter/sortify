"""Regulatory compliance gap analysis and audit report generation for clinical trial documents."""

import html
import json
import os
from typing import Any, Dict, List, Set

from app.core.clinical_taxonomy import ICH_GCP_ESSENTIAL_CHECKLIST


class ClinicalComplianceEngine:
    """Evaluates clinical document collections against ICH-GCP regulatory essential document checklists."""

    def __init__(self):
        self.checklist = ICH_GCP_ESSENTIAL_CHECKLIST

    def evaluate_compliance(
        self,
        classified_artifacts: Dict[str, str],  # filename -> artifact_id
        all_filenames: List[str],
        base_dir: str = "",
    ) -> Dict[str, Any]:
        """Perform gap analysis and compute regulatory compliance metrics."""
        found_artifact_ids: Set[str] = set(classified_artifacts.values())

        # Map artifact_id -> list of filenames
        artifact_to_files: Dict[str, List[str]] = {}
        for fn, art_id in classified_artifacts.items():
            artifact_to_files.setdefault(art_id, []).append(fn)

        found_items = []
        missing_items = []

        for item in self.checklist:
            req_art_id = item["artifact_id"]
            if req_art_id in found_artifact_ids:
                matched_files = artifact_to_files.get(req_art_id, [])
                found_items.append(
                    {
                        "key": item["key"],
                        "title": item["title"],
                        "artifact_id": req_art_id,
                        "importance": item["importance"],
                        "gcp_ref": item["gcp_ref"],
                        "files": matched_files,
                        "count": len(matched_files),
                    }
                )
            else:
                missing_items.append(
                    {
                        "key": item["key"],
                        "title": item["title"],
                        "artifact_id": req_art_id,
                        "importance": item["importance"],
                        "gcp_ref": item["gcp_ref"],
                    }
                )

        total_req = len(self.checklist)
        total_found = len(found_items)
        compliance_pct = (
            round((total_found / total_req) * 100.0, 1) if total_req > 0 else 100.0
        )

        # Identify ancillary and unclassified
        ancillary_files = [
            fn for fn, art_id in classified_artifacts.items() if art_id == "99.01.01"
        ]
        unclassified_files = [
            fn
            for fn, art_id in classified_artifacts.items()
            if art_id == "unclassified"
        ]

        result = {
            "total_files_scanned": len(all_filenames),
            "total_essential_required": total_req,
            "total_essential_found": total_found,
            "total_essential_missing": len(missing_items),
            "compliance_score_percent": compliance_pct,
            "audit_readiness_status": (
                "AUDIT_READY"
                if compliance_pct >= 90
                else "GAPS_DETECTED"
                if compliance_pct >= 60
                else "NON_COMPLIANT"
            ),
            "found_essential_documents": found_items,
            "missing_essential_documents": missing_items,
            "ancillary_documents": ancillary_files,
            "unclassified_documents": unclassified_files,
            "base_dir": base_dir,
        }
        return result

    def export_json_report(
        self, compliance_data: Dict[str, Any], output_path: str
    ) -> str:
        """Export compliance audit analysis as a JSON report."""
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(compliance_data, f, indent=2)
        return output_path

    def generate_html_report(
        self, compliance_data: Dict[str, Any], output_path: str
    ) -> str:
        """Generate a standalone HTML compliance audit dossier."""
        score = compliance_data["compliance_score_percent"]
        status = compliance_data["audit_readiness_status"]
        status_color = (
            "#16a34a" if score >= 90 else "#d97706" if score >= 60 else "#dc2626"
        )

        found_rows = ""
        for item in compliance_data["found_essential_documents"]:
            file_list = "<br>".join(
                f"<code>{html.escape(f)}</code>" for f in item["files"]
            )
            found_rows += f"""
            <tr style="border-bottom: 1px solid #e5e7eb;">
                <td style="padding: 12px; font-weight: 600; color: #111827;">{html.escape(item["title"])}</td>
                <td style="padding: 12px; color: #4b5563;">{html.escape(item["gcp_ref"])}</td>
                <td style="padding: 12px;"><span style="background: #dcfce7; color: #15803d; padding: 4px 8px; border-radius: 9999px; font-size: 12px; font-weight: bold;">FOUND ({item["count"]})</span></td>
                <td style="padding: 12px; color: #374151; font-size: 13px;">{file_list}</td>
            </tr>
            """

        missing_rows = ""
        for item in compliance_data["missing_essential_documents"]:
            missing_rows += f"""
            <tr style="border-bottom: 1px solid #fee2e2; background-color: #fef2f2;">
                <td style="padding: 12px; font-weight: 600; color: #991b1b;">{html.escape(item["title"])}</td>
                <td style="padding: 12px; color: #7f1d1d;">{html.escape(item["gcp_ref"])}</td>
                <td style="padding: 12px;"><span style="background: #fee2e2; color: #b91c1c; padding: 4px 8px; border-radius: 9999px; font-size: 12px; font-weight: bold;">MISSING</span></td>
                <td style="padding: 12px; color: #b91c1c; font-size: 13px; font-weight: 500;">Action Required: Collect before trial initiation / audit</td>
            </tr>
            """

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Clinical Research Compliance Audit Report</title>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f8fafc; color: #1e293b; margin: 0; padding: 32px; }}
        .container {{ max-width: 960px; margin: 0 auto; background: #ffffff; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1); padding: 32px; }}
        .header {{ display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #f1f5f9; padding-bottom: 20px; }}
        .title {{ font-size: 24px; font-weight: 700; color: #0f172a; margin: 0; }}
        .badge {{ background: {status_color}; color: white; padding: 6px 14px; border-radius: 20px; font-size: 14px; font-weight: 700; text-transform: uppercase; }}
        .metrics-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin: 24px 0; }}
        .metric-card {{ background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; text-align: center; }}
        .metric-val {{ font-size: 28px; font-weight: 700; color: #0f172a; }}
        .metric-label {{ font-size: 12px; color: #64748b; text-transform: uppercase; margin-top: 4px; font-weight: 600; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
        th {{ background: #f1f5f9; text-align: left; padding: 12px; font-size: 13px; font-weight: 600; color: #475569; }}
        .section-title {{ font-size: 18px; font-weight: 700; margin-top: 32px; color: #1e293b; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div>
                <h1 class="title">Clinical Trial Regulatory Compliance Audit Dossier</h1>
                <p style="color: #64748b; margin: 4px 0 0 0; font-size: 14px;">Standards: ICH-GCP E6(R2), FDA 21 CFR 312 / 812, DIA TMF Reference Model</p>
            </div>
            <div class="badge">{status}</div>
        </div>

        <div class="metrics-grid">
            <div class="metric-card">
                <div class="metric-val" style="color: {status_color};">{score}%</div>
                <div class="metric-label">Compliance Score</div>
            </div>
            <div class="metric-card">
                <div class="metric-val">{compliance_data["total_essential_found"]} / {compliance_data["total_essential_required"]}</div>
                <div class="metric-label">Essential Docs Found</div>
            </div>
            <div class="metric-card">
                <div class="metric-val" style="color: #dc2626;">{compliance_data["total_essential_missing"]}</div>
                <div class="metric-label">Missing Gaps</div>
            </div>
            <div class="metric-card">
                <div class="metric-val">{compliance_data["total_files_scanned"]}</div>
                <div class="metric-label">Total Files Evaluated</div>
            </div>
        </div>

        <h2 class="section-title">Regulatory Gap Analysis (Missing Essential Items)</h2>
        {"<p style='color: #16a34a; font-weight: 500;'>All required ICH-GCP regulatory essential documents are present and accounted for!</p>" if not missing_rows else f"<table><thead><tr><th>Document Requirement</th><th>Regulatory Citation</th><th>Status</th><th>Recommended Remediation</th></tr></thead><tbody>{missing_rows}</tbody></table>"}

        <h2 class="section-title">Verified Essential Documents (Audited & Mapped)</h2>
        <table>
            <thead>
                <tr>
                    <th>Document Classification</th>
                    <th>Regulatory Citation</th>
                    <th>Audit Status</th>
                    <th>Mapped Source Documents</th>
                </tr>
            </thead>
            <tbody>
                {found_rows}
            </tbody>
        </table>
    </div>
</body>
</html>"""
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(html_content)
        return output_path
