# /// script
# requires-python = ">=3.12"
# ///
"""Utility script to prepare an offline deployment bundle.

Vulnerability database and package index endpoints used for dependency auditing:
- https://pypi.org
- https://api.osv.dev
- https://api.github.com/advisories
"""

import argparse
import ast
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

PYTORCH_CPU_INDEX_URL = "https://download.pytorch.org/whl/cpu"


def _normalize_pkg_name(name: str) -> str:
    """Normalize a Python package name per PEP 503."""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def _extract_req_name(req: str) -> str:
    """Extract and normalize the package name from a PEP 508 requirement string."""
    match = re.match(r"^\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)", req)
    if not match:
        raise ValueError(f"Invalid requirement specification: {req!r}")
    return _normalize_pkg_name(match.group(1))


def _version_tuple(version_str: str) -> tuple[int, ...]:
    """Convert a dotted version prefix into an integer tuple for marker comparisons."""
    nums: list[int] = []
    for part in version_str.split("."):
        digits = re.match(r"^(\d+)", part)
        if not digits:
            break
        nums.append(int(digits.group(1)))
    return tuple(nums)


def _marker_environment() -> dict[str, str]:
    """Build the current PEP 508 marker evaluation environment."""
    impl_ver_info = sys.implementation.version
    impl_version = f"{impl_ver_info.major}.{impl_ver_info.minor}.{impl_ver_info.micro}"
    if impl_ver_info.releaselevel != "final":
        impl_version += f"{impl_ver_info.releaselevel[0]}{impl_ver_info.serial}"
    py_full = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    return {
        "implementation_name": sys.implementation.name,
        "implementation_version": impl_version,
        "os_name": os.name,
        "platform_machine": platform.machine(),
        "platform_python_implementation": platform.python_implementation(),
        "platform_release": platform.release(),
        "platform_system": platform.system(),
        "platform_version": platform.version(),
        "python_full_version": py_full,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "sys_platform": sys.platform,
    }


def _evaluate_marker(marker_str: str) -> bool:
    """Safely evaluate a PEP 508 environment marker expression without eval()."""
    env = _marker_environment()
    version_keys = {
        "python_version",
        "python_full_version",
        "implementation_version",
    }

    def _eval_node(node: ast.AST) -> str | bool:
        if isinstance(node, ast.Expression):
            return _eval_node(node.body)
        if isinstance(node, ast.BoolOp):
            if isinstance(node.op, ast.And):
                return all(bool(_eval_node(v)) for v in node.values)
            if isinstance(node.op, ast.Or):
                return any(bool(_eval_node(v)) for v in node.values)
            raise ValueError(f"Unsupported boolean operator in marker: {marker_str!r}")
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return not bool(_eval_node(node.operand))
        if isinstance(node, ast.Compare):
            left_node = node.left
            left_val = _eval_node(left_node)
            for op, comp_node in zip(node.ops, node.comparators):
                right_val = _eval_node(comp_node)
                is_ver_cmp = (
                    isinstance(left_node, ast.Name) and left_node.id in version_keys
                ) or (isinstance(comp_node, ast.Name) and comp_node.id in version_keys)
                if isinstance(op, ast.Eq):
                    ok = left_val == right_val
                elif isinstance(op, ast.NotEq):
                    ok = left_val != right_val
                elif isinstance(op, ast.In):
                    ok = str(left_val) in str(right_val)
                elif isinstance(op, ast.NotIn):
                    ok = str(left_val) not in str(right_val)
                elif isinstance(op, (ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
                    if is_ver_cmp:
                        lv = _version_tuple(str(left_val))
                        rv = _version_tuple(str(right_val))
                        if isinstance(op, ast.Lt):
                            ok = lv < rv
                        elif isinstance(op, ast.LtE):
                            ok = lv <= rv
                        elif isinstance(op, ast.Gt):
                            ok = lv > rv
                        else:
                            ok = lv >= rv
                    else:
                        ls = str(left_val)
                        rs = str(right_val)
                        if isinstance(op, ast.Lt):
                            ok = ls < rs
                        elif isinstance(op, ast.LtE):
                            ok = ls <= rs
                        elif isinstance(op, ast.Gt):
                            ok = ls > rs
                        else:
                            ok = ls >= rs
                else:
                    raise ValueError(
                        f"Unsupported comparison operator in marker: {marker_str!r}"
                    )
                if not ok:
                    return False
                left_node = comp_node
                left_val = right_val
            return True
        if isinstance(node, ast.Name):
            if node.id not in env:
                raise ValueError(
                    f"Unknown marker variable {node.id!r} in {marker_str!r}"
                )
            return env[node.id]
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        raise ValueError(f"Unsupported marker syntax: {marker_str!r}")

    tree = ast.parse(marker_str, mode="eval")
    return bool(_eval_node(tree))


def _load_required_project_packages(
    pyproject_path: Path | None = None,
) -> tuple[set[str], set[str]]:
    """Load normalized base and `ml` optional package names from pyproject.toml."""
    candidate = pyproject_path or Path("pyproject.toml")
    if not candidate.is_file():
        candidate = Path(__file__).resolve().parent.parent / "pyproject.toml"
    data = tomllib.loads(candidate.read_bytes().decode("utf-8"))
    project = data.get("project", {})
    base_deps = {_extract_req_name(dep) for dep in project.get("dependencies", [])}
    ml_deps = {
        _extract_req_name(dep)
        for dep in project.get("optional-dependencies", {}).get("ml", [])
    }
    return base_deps, ml_deps


def _parse_manifest_entries(content: str) -> list[dict[str, object]]:
    """Parse logical package entries, markers, and hashes from requirements.txt."""
    logical_lines: list[str] = []
    current_parts: list[str] = []

    for raw_line in content.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        has_continuation = raw_line.rstrip().endswith("\\")
        part = stripped[:-1].strip() if stripped.endswith("\\") else stripped
        if part:
            current_parts.append(part)
        if not has_continuation and current_parts:
            logical_lines.append(" ".join(current_parts))
            current_parts = []

    if current_parts:
        logical_lines.append(" ".join(current_parts))

    parsed: list[dict[str, object]] = []
    for entry in logical_lines:
        hashes = re.findall(r"--hash=(\S+)", entry)
        req_and_marker = re.split(r"\s+--", entry, maxsplit=1)[0].strip()
        if ";" in req_and_marker:
            spec_str, marker_str = (
                s.strip() for s in req_and_marker.split(";", maxsplit=1)
            )
        else:
            spec_str, marker_str = req_and_marker, None

        pin_match = re.match(
            r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?:\[[^\]]*\])?==([^\s;]+)$",
            spec_str,
        )
        if not pin_match:
            raise ValueError(
                f"Unpinned or malformed requirement entry in requirements.txt: {entry}"
            )
        pkg_name = _normalize_pkg_name(pin_match.group(1))
        version_str = pin_match.group(2)
        is_active = _evaluate_marker(marker_str) if marker_str else True
        parsed.append(
            {
                "raw": entry,
                "spec": f"{pkg_name}=={version_str}",
                "name": pkg_name,
                "version": version_str,
                "marker": marker_str,
                "active": is_active,
                "hashes": hashes,
            }
        )
    return parsed


def _load_lockfile_runtime_ml_expectation(
    uv_lock_path: Path | None = None,
) -> tuple[set[str], dict[tuple[str, str], set[str]]] | None:
    """Load expected runtime+`ml` package set and locked SHA-256 digests from uv.lock."""
    candidate = uv_lock_path if uv_lock_path is not None else Path("uv.lock")
    if not candidate.is_file():
        return None

    lock_data = tomllib.loads(candidate.read_bytes().decode("utf-8"))
    packages = lock_data.get("package", [])
    if not isinstance(packages, list) or not packages:
        return None

    by_name: dict[str, list[dict[str, object]]] = {}
    lock_hashes: dict[tuple[str, str], set[str]] = {}
    root_name: str | None = None

    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        raw_name = str(pkg.get("name", ""))
        if not raw_name:
            continue
        norm_name = _normalize_pkg_name(raw_name)
        version = str(pkg.get("version", ""))
        by_name.setdefault(norm_name, []).append(pkg)

        source = pkg.get("source")
        if isinstance(source, dict) and (
            source.get("virtual") == "." or source.get("editable") == "."
        ):
            root_name = norm_name

        digests: set[str] = lock_hashes.setdefault((norm_name, version), set())
        sdist = pkg.get("sdist")
        if isinstance(sdist, dict) and sdist.get("hash"):
            digests.add(str(sdist["hash"]))
        wheels = pkg.get("wheels", [])
        if isinstance(wheels, list):
            for whl in wheels:
                if isinstance(whl, dict) and whl.get("hash"):
                    digests.add(str(whl["hash"]))

    if root_name is None and "smart-autosorter" in by_name:
        root_name = "smart-autosorter"

    reachable_pkgs: set[str] = set()
    if root_name is not None:
        queue: list[tuple[str, str | None]] = [(root_name, None), (root_name, "ml")]
        seen: set[tuple[str, str | None]] = set()
        while queue:
            cur_name, cur_extra = queue.pop()
            if (cur_name, cur_extra) in seen:
                continue
            seen.add((cur_name, cur_extra))
            if cur_name != root_name:
                reachable_pkgs.add(cur_name)
            for entry in by_name.get(cur_name, []):
                if cur_extra is None:
                    deps_raw = entry.get("dependencies", [])
                else:
                    opt_deps = entry.get("optional-dependencies", {})
                    deps_raw = (
                        opt_deps.get(cur_extra, [])
                        if isinstance(opt_deps, dict)
                        else []
                    )
                if not isinstance(deps_raw, list):
                    continue
                for dep in deps_raw:
                    if not isinstance(dep, dict) or not dep.get("name"):
                        continue
                    dep_name = _normalize_pkg_name(str(dep["name"]))
                    queue.append((dep_name, None))
                    extras = dep.get("extra", [])
                    if isinstance(extras, list):
                        for ex in extras:
                            queue.append((dep_name, str(ex)))

    return reachable_pkgs, lock_hashes


def export_offline_requirements(reqs_file: Path, cwd: Path | None = None) -> None:
    """Export exact locked runtime + `ml` requirements with hashes from uv.lock."""
    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--format",
            "requirements.txt",
            "--extra",
            "ml",
            "--no-dev",
            "--no-emit-project",
            "-o",
            str(reqs_file),
        ],
        cwd=cwd,
        check=True,
    )


def _validate_manifest(
    content: str, is_cpu: bool, uv_lock_path: Path | None = None
) -> None:
    """Validate hashes, uv.lock parity, base + `ml` coverage, and CPU PyTorch pins."""
    try:
        entries = _parse_manifest_entries(content)
    except ValueError as exc:
        print(f"Error: {exc}")
        sys.exit(1)

    if not entries:
        print("Error: offline_bundle/requirements.txt contains no package entries.")
        sys.exit(1)

    for item in entries:
        hashes = item["hashes"]
        spec = item["spec"]
        if not isinstance(hashes, list) or not hashes:
            print(
                f"Error: Package '{spec}' in requirements.txt is missing --hash=sha256:... pins."
            )
            sys.exit(1)
        for digest in hashes:
            if not str(digest).startswith("sha256:") or len(str(digest)) <= len(
                "sha256:"
            ):
                print(
                    f"Error: Package '{spec}' in requirements.txt specifies non-sha256 or empty hash '{digest}'."
                )
                sys.exit(1)

    base_deps, ml_deps = _load_required_project_packages()
    active_names = {str(item["name"]) for item in entries if item["active"]}

    missing_base = sorted(base_deps - active_names)
    missing_ml = sorted(ml_deps - active_names)
    if missing_base or missing_ml:
        details: list[str] = []
        if missing_base:
            details.append(f"base packages ({', '.join(missing_base)})")
        if missing_ml:
            details.append(f"ml extra packages ({', '.join(missing_ml)})")
        print(
            "Error: offline_bundle/requirements.txt is incomplete; missing "
            + " and ".join(details)
            + ". Run 'uv run python scripts/prepare_offline.py --cpu' to regenerate it."
        )
        sys.exit(1)

    lock_expectation = _load_lockfile_runtime_ml_expectation(uv_lock_path)
    if lock_expectation is not None:
        expected_lock_pkgs, lock_hashes = lock_expectation
        manifest_names = {str(item["name"]) for item in entries}

        drifted_pins: list[str] = []
        drifted_hashes: list[str] = []
        for item in entries:
            name = str(item["name"])
            version = str(item["version"])
            spec = str(item["spec"])
            key = (name, version)
            if key not in lock_hashes:
                locked_versions = sorted(
                    v for (pkg_n, v) in lock_hashes if pkg_n == name
                )
                expected_str = (
                    ", ".join(locked_versions) if locked_versions else "not in uv.lock"
                )
                drifted_pins.append(f"{spec} (uv.lock has {expected_str})")
                continue

            expected_digests = lock_hashes[key]
            raw_item_hashes = item["hashes"]
            item_hashes = (
                {str(h) for h in raw_item_hashes}
                if isinstance(raw_item_hashes, list)
                else set()
            )
            if expected_digests and not item_hashes.issubset(expected_digests):
                drifted_hashes.append(spec)

        missing_from_manifest = sorted(expected_lock_pkgs - manifest_names)
        unexpected_in_manifest = (
            sorted(manifest_names - expected_lock_pkgs) if expected_lock_pkgs else []
        )

        if (
            drifted_pins
            or drifted_hashes
            or missing_from_manifest
            or unexpected_in_manifest
        ):
            problems: list[str] = []
            if drifted_pins:
                problems.append(f"pin drift ({'; '.join(drifted_pins[:5])})")
            if drifted_hashes:
                problems.append(f"hash mismatch ({', '.join(drifted_hashes[:5])})")
            if missing_from_manifest:
                problems.append(
                    f"missing locked packages ({', '.join(missing_from_manifest[:5])})"
                )
            if unexpected_in_manifest:
                problems.append(
                    f"unexpected packages not in runtime/ml lock graph ({', '.join(unexpected_in_manifest[:5])})"
                )
            print(
                "Error: offline_bundle/requirements.txt diverges from uv.lock: "
                + "; ".join(problems)
                + ". Run 'uv run python scripts/prepare_offline.py --cpu' to synchronize it."
            )
            sys.exit(1)

    if is_cpu:
        for item in entries:
            name = str(item["name"])
            spec = str(item["spec"])
            if (
                name.startswith(("nvidia-", "cuda-"))
                or "+cu" in spec
                or "cuda" in spec.lower()
            ):
                print(f"Error: CUDA/GPU dependency found in requirements.txt: {spec}")
                sys.exit(1)

        active_torch_entries = [
            item
            for item in entries
            if item["active"] and str(item["name"]).startswith("torch")
        ]
        active_core_torch = [
            item for item in active_torch_entries if item["name"] == "torch"
        ]
        if not active_core_torch:
            print(
                f"Error: Zero active PyTorch ('torch') entries found in requirements.txt for platform '{sys.platform}'."
            )
            sys.exit(1)

        if sys.platform in ("win32", "linux"):
            for item in active_torch_entries:
                spec = str(item["spec"])
                if "+cpu" not in spec:
                    print(
                        f"Error: Non-CPU PyTorch dependency found in requirements.txt: {spec}"
                    )
                    sys.exit(1)


def _collect_sdist_build_requirements(sdist_path: Path) -> list[str]:
    """Read PEP 517 [build-system].requires (and dynamic CMake/Ninja needs) from an sdist."""
    pyproject_bytes: bytes | None = None
    lower_name = sdist_path.name.lower()

    if lower_name.endswith((".tar.gz", ".tgz")):
        with tarfile.open(sdist_path, "r:*") as tf:
            for member in tf.getmembers():
                parts = Path(member.name).parts
                if len(parts) == 2 and parts[1] == "pyproject.toml":
                    extracted = tf.extractfile(member)
                    if extracted is not None:
                        pyproject_bytes = extracted.read()
                    break
    elif lower_name.endswith(".zip"):
        with zipfile.ZipFile(sdist_path, "r") as zf:
            for member_name in zf.namelist():
                parts = Path(member_name).parts
                if len(parts) == 2 and parts[1] == "pyproject.toml":
                    pyproject_bytes = zf.read(member_name)
                    break

    if pyproject_bytes is None:
        return ["setuptools", "wheel"]

    data = tomllib.loads(pyproject_bytes.decode("utf-8"))
    build_system = data.get("build-system", {})
    requires = list(build_system.get("requires") or ["setuptools", "wheel"])

    req_names = {_extract_req_name(r) for r in requires}
    if "scikit-build-core" in req_names or "scikit-build" in req_names:
        if "cmake" not in req_names:
            requires.append("cmake>=3.15")
        if "ninja" not in req_names:
            requires.append("ninja>=1.5")

    return requires


def _download_sdist_build_wheels(pip_path: str, wheels_dir: Path) -> None:
    """Download PEP 517 build-system binary wheels for any source distributions in wheels_dir."""
    if not wheels_dir.exists():
        return

    sdists = [
        p
        for p in wheels_dir.glob("*")
        if p.name.lower().endswith((".tar.gz", ".tgz", ".zip"))
    ]
    if not sdists:
        return

    build_reqs: list[str] = []
    for sdist in sdists:
        for req in _collect_sdist_build_requirements(sdist):
            if req not in build_reqs:
                build_reqs.append(req)

    if not build_reqs:
        return

    print(
        f"Downloading PEP 517 build wheels for {len(sdists)} source distribution(s): {', '.join(build_reqs)}"
    )
    subprocess.run(
        [
            pip_path,
            "download",
            "--only-binary=:all:",
            "--find-links",
            str(wheels_dir),
            "-d",
            str(wheels_dir),
            *build_reqs,
        ],
        check=True,
    )


def main():
    """Prepare an offline bundle by downloading dependencies and model weights."""
    parser = argparse.ArgumentParser(
        description="Prepare and validate the Smart AutoSorter offline deployment bundle."
    )
    parser.add_argument(
        "--cpu",
        action="store_true",
        help="Compile/validate CPU-only PyTorch dependencies and wheels.",
    )
    parser.add_argument(
        "--frozen",
        action="store_true",
        help="Skip export and validate/download from the tracked offline_bundle/requirements.txt.",
    )
    parser.add_argument(
        "--verify-lockfile",
        action="store_true",
        help="Validate that offline_bundle/requirements.txt matches uv.lock and exit without downloading wheels.",
    )
    args = parser.parse_args()
    is_cpu = args.cpu
    is_frozen = args.frozen
    verify_lockfile_only = args.verify_lockfile

    print("Preparing offline bundle...")
    bundle_dir = Path("offline_bundle")
    bundle_dir.mkdir(exist_ok=True)
    reqs_file = bundle_dir / "requirements.txt"

    if (is_frozen or verify_lockfile_only) and not reqs_file.exists():
        print(
            f"Error: --frozen/--verify-lockfile requires an existing manifest at {reqs_file}."
        )
        sys.exit(1)

    if verify_lockfile_only:
        with open(reqs_file, "r", encoding="utf-8") as f:
            manifest_content = f.read()
        _validate_manifest(manifest_content, is_cpu=is_cpu)
        print(f"Verified {reqs_file} is synchronized with uv.lock.")
        return

    # Clean up previous ZIP output if any
    zip_output = Path("offline_bundle.zip")
    if zip_output.exists():
        try:
            zip_output.unlink()
        except OSError as e:
            print(f"Warning: could not delete {zip_output}: {e}")

    # Clean up wheels directory specifically
    wheels_dir = bundle_dir / "wheels"
    if wheels_dir.exists():
        shutil.rmtree(wheels_dir)
    wheels_dir.mkdir()

    # 1. Export exact locked requirements from uv.lock with `ml` extras (unless --frozen)
    if is_frozen:
        print(f"Using frozen manifest at {reqs_file}...")
    else:
        print("Exporting locked requirements from uv.lock...")
        export_offline_requirements(reqs_file)

    # Validate manifest hashes, base + `ml` coverage, and CPU torch entries before downloading
    if not reqs_file.exists():
        print(f"Error: Requirements manifest not found at {reqs_file}.")
        sys.exit(1)
    with open(reqs_file, "r", encoding="utf-8") as f:
        manifest_content = f.read()
    _validate_manifest(manifest_content, is_cpu=is_cpu)

    # 2. Download wheels (and PEP 517 build wheels for any sdists)
    print("Downloading Python dependencies...")
    subprocess.run(["uv", "venv", "--seed", ".tmp_seed_venv"], check=True)

    pip_path = (
        ".tmp_seed_venv/bin/pip"
        if os.name != "nt"
        else r".tmp_seed_venv\Scripts\pip.exe"
    )

    try:
        subprocess.run(
            [
                pip_path,
                "download",
                "-r",
                str(reqs_file),
                "--require-hashes",
                "--no-deps",
                "--extra-index-url",
                PYTORCH_CPU_INDEX_URL,
                "-d",
                str(wheels_dir),
            ],
            check=True,
        )
        _download_sdist_build_wheels(pip_path, wheels_dir)
    finally:
        shutil.rmtree(".tmp_seed_venv")

    # 3. Validate CPU wheels
    if is_cpu:
        print(
            "Validating that only CPU-specific PyTorch wheels were compiled and downloaded..."
        )
        if not wheels_dir.exists():
            print(f"Error: Wheels directory not found at {wheels_dir}.")
            sys.exit(1)

        downloaded_wheels = [
            whl for whl in wheels_dir.glob("*.whl") if whl.name.lower().endswith(".whl")
        ]
        core_torch_wheels = [
            whl for whl in downloaded_wheels if whl.name.lower().startswith("torch-")
        ]
        if not core_torch_wheels:
            print(
                "Error: Zero PyTorch ('torch') wheels were downloaded into offline_bundle/wheels."
            )
            sys.exit(1)

        for whl in downloaded_wheels:
            name_lower = whl.name.lower()
            if "torch" in name_lower:
                if sys.platform in ("win32", "linux"):
                    if "cpu" not in name_lower:
                        print(
                            f"Error: Non-CPU PyTorch wheel found in downloads: {whl.name}"
                        )
                        sys.exit(1)
                if "cu" in name_lower or "cuda" in name_lower:
                    print(
                        f"Error: CUDA/GPU PyTorch wheel found in downloads: {whl.name}"
                    )
                    sys.exit(1)
        print(
            "Validation complete: Only CPU-specific PyTorch wheels are present in the offline bundle."
        )

    # 5. Package bundle
    print("Zipping bundle...")
    shutil.make_archive("offline_bundle", "zip", "offline_bundle")

    print("Done! Transfer offline_bundle.zip to the isolated environment.")


if __name__ == "__main__":
    main()
