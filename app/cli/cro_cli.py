"""CLI subcommand handler for CRO multi-study pipeline ingestion and regulatory manifest generation."""

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from app.config import AppSettings
from app.core.cro_multi_study_pipeline import CROMultiStudyPipeline


def register_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register cro subcommand group."""
    parser_cro = subparsers.add_parser(
        "cro",
        help="CRO multi-study forensic ingestion and regulatory binder generation",
    )
    cro_subparsers = parser_cro.add_subparsers(
        dest="cro_command", help="CRO subcommands"
    )

    def add_cro_common(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "-q",
            "--quiet",
            action="store_true",
            dest="quiet",
            default=argparse.SUPPRESS,
            help="Suppress informational prints",
        )
        p.add_argument(
            "--no-color",
            action="store_true",
            dest="no_color",
            default=argparse.SUPPRESS,
            help="Disable ANSI color formatting",
        )
        p.add_argument(
            "--json",
            action="store_true",
            help="Output response in structured JSON format",
        )

    # ingest
    p_ingest = cro_subparsers.add_parser(
        "ingest", help="Run CRO multi-study forensic ingestion pipeline"
    )
    add_cro_common(p_ingest)
    p_ingest.add_argument(
        "source_pos",
        nargs="?",
        default=None,
        metavar="SOURCE",
        help="Source directory containing clinical trial documents",
    )
    p_ingest.add_argument(
        "target_pos",
        nargs="?",
        default=None,
        metavar="TARGET",
        help="Target directory for study organization and regulatory binders",
    )
    p_ingest.add_argument(
        "--source",
        type=str,
        default=None,
        help="Source directory containing clinical trial documents",
    )
    p_ingest.add_argument(
        "--target",
        type=str,
        default=None,
        help="Target directory for study organization and regulatory binders",
    )
    p_ingest.add_argument(
        "--mode",
        type=str,
        choices=["tmf", "isf"],
        default="tmf",
        help="Regulatory taxonomy structure: Trial Master File (tmf) or Investigator Site File (isf)",
    )
    p_ingest.add_argument(
        "--smart-renaming",
        action="store_true",
        default=True,
        help="Enable standardized clinical document renaming",
    )
    p_ingest.add_argument(
        "--no-smart-renaming",
        action="store_false",
        dest="smart_renaming",
        help="Disable standardized clinical document renaming",
    )

    # manifest
    p_manifest = cro_subparsers.add_parser(
        "manifest", help="View or inspect regulatory chain-of-custody manifest"
    )
    add_cro_common(p_manifest)
    p_manifest.add_argument(
        "target_pos",
        nargs="?",
        default=None,
        metavar="TARGET",
        help="Target directory or chain-of-custody manifest JSON path",
    )
    p_manifest.add_argument(
        "--target",
        type=str,
        default=None,
        help="Target directory or manifest path",
    )
    p_manifest.add_argument(
        "--manifest-path",
        type=str,
        default=None,
        help="Direct path to chain_of_custody_manifest.json",
    )


def handle_cro_command(args: argparse.Namespace, settings: AppSettings) -> bool:
    """Handle cro subcommand execution. Returns True if handled."""
    if getattr(args, "subcommand", None) != "cro":
        return False

    cro_cmd = getattr(args, "cro_command", None)
    if not cro_cmd:
        print(
            "Error: Missing cro subcommand. Use 'ingest' or 'manifest'.",
            file=sys.stderr,
        )
        sys.exit(2)

    quiet = getattr(args, "quiet", False)
    is_json = getattr(args, "json", False)

    if cro_cmd == "ingest":
        source_dir = getattr(args, "source", None) or getattr(args, "source_pos", None)
        target_dir = getattr(args, "target", None) or getattr(args, "target_pos", None)

        if not source_dir or not target_dir:
            print(
                "Error: Both source and target directories are required. Usage: sortify cro ingest <source> <target> [--mode tmf]",
                file=sys.stderr,
            )
            sys.exit(2)

        source_path = Path(source_dir).resolve()
        target_path = Path(target_dir).resolve()

        if not source_path.exists() or not source_path.is_dir():
            print(
                f"Error: Source directory '{source_dir}' does not exist or is not a directory.",
                file=sys.stderr,
            )
            sys.exit(1)

        mode = getattr(args, "mode", "tmf")
        smart_renaming = getattr(args, "smart_renaming", True)

        pipeline = CROMultiStudyPipeline(mode=mode, smart_renaming=smart_renaming)

        def progress_cb(ratio: float, stage: str) -> None:
            if not quiet and not is_json:
                print(f"  [{int(ratio * 100)}%] {stage}", file=sys.stderr)

        try:
            result = pipeline.run_pipeline(
                source_root=str(source_path),
                target_root=str(target_path),
                progress_callback=progress_cb,
            )

            res_dict = dataclasses.asdict(result)
            res = {
                "status": "success",
                "pipeline_result": res_dict,
            }

            if is_json:
                sys.stdout.write(json.dumps(res, indent=2) + "\n")
                sys.stdout.flush()
            else:
                if not quiet:
                    print(
                        f"CRO Multi-Study Pipeline ingestion completed successfully for '{source_path}'.",
                        file=sys.stderr,
                    )
                    print(
                        f"Discovered Studies: {result.discovered_studies_count} | "
                        f"Scanned Files: {result.total_scanned_files} | "
                        f"Manifest: {result.chain_of_custody_manifest_path}",
                        file=sys.stderr,
                    )

            sys.exit(0)

        except Exception as e:
            print(
                f"Error during CRO multi-study pipeline ingestion: {e}", file=sys.stderr
            )
            sys.exit(1)

    elif cro_cmd == "manifest":
        manifest_path_input = (
            getattr(args, "manifest_path", None)
            or getattr(args, "target", None)
            or getattr(args, "target_pos", None)
        )

        if not manifest_path_input:
            print(
                "Error: Target directory or manifest path is required.", file=sys.stderr
            )
            sys.exit(2)

        p = Path(manifest_path_input).resolve()
        if p.is_dir():
            manifest_file = p / "chain_of_custody_manifest.json"
        else:
            manifest_file = p

        if not manifest_file.exists():
            print(
                f"Error: Chain-of-custody manifest not found at '{manifest_file}'.",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            with open(manifest_file, "r", encoding="utf-8") as f:
                manifest_data = json.load(f)

            res = {
                "status": "success",
                "manifest_path": str(manifest_file),
                "manifest": manifest_data,
            }

            if is_json:
                sys.stdout.write(json.dumps(res, indent=2) + "\n")
                sys.stdout.flush()
            else:
                print(f"Chain-of-Custody Manifest: {manifest_file}")
                print(json.dumps(manifest_data, indent=2))

            sys.exit(0)

        except Exception as e:
            print(
                f"Error reading manifest file '{manifest_file}': {e}", file=sys.stderr
            )
            sys.exit(1)

    return True
