"""Reject new legacy diagnostics without exempting entire files or directories."""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

ROOT = Path(__file__).resolve().parent.parent


def type_counts(root: Path) -> dict[str, dict[str, int]]:
    """Budget warnings from ty SARIF while keeping every error fatal."""
    result = subprocess.run(
        [sys.executable, "-m", "ty", "check", "--output-format", "sarif"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise ValueError(f"ty could not run: {result.stderr}")
    data = json.loads(result.stdout)
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    errors = []
    for run in data["runs"]:
        for diagnostic in run.get("results", []):
            uri = diagnostic["locations"][0]["physicalLocation"]["artifactLocation"][
                "uri"
            ]
            parsed = urlparse(uri)
            filename = (
                url2pathname(parsed.path) if parsed.scheme == "file" else unquote(uri)
            )
            path = Path(filename)
            if not path.is_absolute():
                path = root / path
            source = path.resolve().relative_to(root.resolve()).as_posix()
            rule = diagnostic["ruleId"]
            level = diagnostic.get("level", "warning")
            if level == "error":
                errors.append(f"{source}: {rule}: {diagnostic['message']['text']}")
            elif level == "warning":
                counts[source][rule] += 1
    if errors:
        raise ValueError("Type errors cannot be baselined:\n" + "\n".join(errors))
    if result.returncode:
        raise ValueError("ty failed; refusing to accept incomplete diagnostic output")
    return {path: dict(rules) for path, rules in sorted(counts.items())}


def exception_counts(root: Path) -> dict[str, dict[str, int]]:
    """Measure exception findings while ignoring legacy per-file exemptions."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "app",
            "--isolated",
            "--select",
            "BLE001,S110,SIM105",
            "--output-format",
            "json",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode not in (0, 1):
        raise ValueError(f"Ruff could not run: {result.stderr}")
    counts: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in json.loads(result.stdout):
        path = Path(item["filename"]).resolve().relative_to(root.resolve()).as_posix()
        counts[path][item["code"]] += 1
    return {path: dict(rules) for path, rules in sorted(counts.items())}


def read_baseline(text: str, tool: str) -> dict[str, dict[str, int]]:
    """Validate baseline shape and reject invalid or unsafe budget entries."""
    data = json.loads(text)
    if (
        not isinstance(data, dict)
        or data.get("schema") != 1
        or data.get("tool") != tool
    ):
        raise ValueError("Invalid quality baseline schema or tool")
    counts = data.get("counts")
    if not isinstance(counts, dict):
        raise ValueError("Baseline counts must be an object")
    for path, rules in counts.items():
        if not path.startswith("app/") or ".." in Path(path).parts or "\\" in path:
            raise ValueError(f"Invalid baseline source path: {path}")
        if not isinstance(rules, dict):
            raise ValueError("Baseline rule budgets must be objects")
        for rule, count in rules.items():
            if not rule or type(count) is not int or count <= 0:
                raise ValueError("Rule budgets must be positive integers")
    return counts


def increases(current: dict, baseline: dict) -> list[str]:
    """List increases independently for every path and rule; new paths start at zero."""
    return [
        f"{path}: {rule}: budget {baseline.get(path, {}).get(rule, 0)}, actual {count}"
        for path, rules in sorted(current.items())
        for rule, count in sorted(rules.items())
        if count > baseline.get(path, {}).get(rule, 0)
    ]


def baseline_at_ref(root: Path, ref: str, path: Path, tool: str) -> dict | None:
    """Read the merge-base budget so editing JSON cannot raise accepted debt."""
    subprocess.run(
        ["git", "rev-parse", "--verify", ref], cwd=root, check=True, capture_output=True
    )
    name = path.resolve().relative_to(root.resolve()).as_posix()
    obj = f"{ref}:{name}"
    exists = subprocess.run(
        ["git", "cat-file", "-e", obj], cwd=root, capture_output=True, check=False
    )
    if exists.returncode:
        return None  # The PR introducing this gate has no previous baseline.
    text = subprocess.check_output(["git", "show", obj], cwd=root, text=True)
    return read_baseline(text, tool)


def main(argv: list[str] | None = None) -> int:
    """Check or shrink budgets; initialization is explicit and never overwrites one."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tool", choices=["exceptions", "types"])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--base-ref", help="Git ref whose committed budgets must not increase"
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--update", action="store_true", help="Shrink the existing baseline"
    )
    actions.add_argument(
        "--initialize", action="store_true", help="Create the first baseline only"
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    path = root / "scripts" / "baselines" / f"{args.tool}.json"
    try:
        current = (
            exception_counts(root) if args.tool == "exceptions" else type_counts(root)
        )
        if args.initialize:
            if path.exists():
                raise ValueError("Baseline already exists; use --update to shrink it")
        else:
            baseline = read_baseline(path.read_text(), args.tool)
            if args.base_ref:
                previous = baseline_at_ref(root, args.base_ref, path, args.tool)
                if previous is not None and (raised := increases(baseline, previous)):
                    raise ValueError(
                        "Committed baseline budgets increased:\n" + "\n".join(raised)
                    )
            if raised := increases(current, baseline):
                raise ValueError("New diagnostics:\n" + "\n".join(raised))
        if args.update or args.initialize:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"schema": 1, "tool": args.tool, "counts": current},
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            )
        print(
            f"{args.tool} ratchet passed: {sum(sum(r.values()) for r in current.values())} existing findings"
        )
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
