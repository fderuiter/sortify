"""Shared offline bundle, target virtualenv, and model hash registry utilities."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

OFFLINE_BUNDLE_DIRNAME = "offline_bundle"
OFFLINE_REQUIREMENTS_FILENAME = "requirements.txt"
APP_WHEEL_MANIFEST_FILENAME = "app_wheel.txt"
OFFLINE_REQUIREMENTS_REL_PATH = (
    f"{OFFLINE_BUNDLE_DIRNAME}/{OFFLINE_REQUIREMENTS_FILENAME}"
)
APP_WHEEL_MANIFEST_REL_PATH = f"{OFFLINE_BUNDLE_DIRNAME}/{APP_WHEEL_MANIFEST_FILENAME}"
APP_DIST_NAME = "smart-autosorter"
APP_WHEEL_PREFIX = "smart_autosorter-"
APP_WHEEL_GLOB = "smart_autosorter-*.whl"
CHECKOUT_OFFLINE_VENV = Path(".venv-offline")
CLEAN_HOST_VENV = Path(".venv")


def cleanup_stale_file(path: Path) -> None:
    """Remove a stale generated artifact if it exists."""
    if path.exists():
        try:
            path.unlink()
        except OSError as exc:
            print(f"Warning: could not delete {path}: {exc}")


def build_and_pin_app_wheel(
    wheels_dir: Path,
    app_wheel_manifest: Path,
    *,
    run_cmd: Callable[..., Any] = subprocess.run,
) -> Path:
    """Build the `smart-autosorter` wheel into `wheels_dir` and write its SHA-256 requirement pin."""
    print("Building smart-autosorter wheel for offline bundle...")
    run_cmd(
        ["uv", "build", "--wheel", "--out-dir", str(wheels_dir)],
        check=True,
    )

    if not wheels_dir.exists():
        print(f"Error: Wheels directory not found at {wheels_dir}.")
        sys.exit(1)

    app_wheels = sorted(
        [
            whl
            for whl in wheels_dir.glob(APP_WHEEL_GLOB)
            if whl.name.startswith(APP_WHEEL_PREFIX)
            and whl.name.lower().endswith(".whl")
        ],
        key=lambda p: p.name,
    )
    if not app_wheels:
        print("Error: smart_autosorter-*.whl was not built into offline_bundle/wheels.")
        sys.exit(1)
    if len(app_wheels) > 1:
        names = ", ".join(w.name for w in app_wheels)
        print(
            f"Error: Expected exactly one smart_autosorter-*.whl in offline_bundle/wheels, found: {names}"
        )
        sys.exit(1)

    app_wheel = app_wheels[0]
    wheel_parts = app_wheel.name[:-4].split("-")
    if len(wheel_parts) < 5 or not wheel_parts[1]:
        print(f"Error: Malformed wheel filename for smart-autosorter: {app_wheel.name}")
        sys.exit(1)
    wheel_version = wheel_parts[1]
    wheel_sha256 = hashlib.sha256(app_wheel.read_bytes()).hexdigest()

    with open(app_wheel_manifest, "w", encoding="utf-8") as f:
        f.write(f"{APP_DIST_NAME}=={wheel_version} --hash=sha256:{wheel_sha256}\n")

    return app_wheel


def is_repo_checkout(
    cwd: Path | None = None,
    *,
    path_exists: Callable[[str], bool] = os.path.exists,
) -> bool:
    """Return True when running inside a git or Python project checkout."""
    if cwd is not None:
        return path_exists(str(cwd / ".git")) or path_exists(
            str(cwd / "pyproject.toml")
        )
    return path_exists(".git") or path_exists("pyproject.toml")


def resolve_target_venv(
    venv_dir: str | os.PathLike[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    path_exists: Callable[[str], bool] = os.path.exists,
) -> Path:
    """Resolve the target virtual environment directory for offline installation."""
    if venv_dir is not None:
        venv_str = os.fspath(venv_dir).strip()
        if venv_str:
            return Path(venv_str)

    env = environ if environ is not None else os.environ
    uv_project_env = env.get("UV_PROJECT_ENVIRONMENT", "").strip()
    if uv_project_env:
        return Path(uv_project_env)

    in_checkout = is_repo_checkout(cwd=cwd, path_exists=path_exists)
    virtual_env = env.get("VIRTUAL_ENV", "").strip()
    if virtual_env:
        # `uv run --no-project` or an active contributor shell exports
        # VIRTUAL_ENV=<checkout>/.venv; ignore any implicit `<checkout>/.venv`
        # so offline-install never clobbers dev/type-check dependencies unless
        # explicitly requested via `--venv` or `UV_PROJECT_ENVIRONMENT`.
        base_cwd = cwd if cwd is not None else Path.cwd()
        resolved_venv = Path(virtual_env).resolve()
        is_implicit_checkout_venv = (
            in_checkout and resolved_venv == (base_cwd / ".venv").resolve()
        ) or (
            resolved_venv.name == ".venv"
            and is_repo_checkout(cwd=resolved_venv.parent, path_exists=path_exists)
        )
        if not is_implicit_checkout_venv:
            return Path(virtual_env)

    if in_checkout:
        return CHECKOUT_OFFLINE_VENV
    return CLEAN_HOST_VENV


def venv_python_path(
    venv_dir: Path,
    *,
    platform_name: str | None = None,
    os_name: str | None = None,
) -> Path:
    """Return the Python interpreter path inside `venv_dir`."""
    plat = platform_name if platform_name is not None else sys.platform
    runtime_os = os_name if os_name is not None else os.name
    if plat == "win32" or runtime_os == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def format_hashes_registry(
    registry_data: Mapping[str, Mapping[str, str]],
) -> str:
    """Render `app/core/hashes_registry.py` in canonical Ruff-formatted style."""
    header = (
        '"""Registry containing expected hashes of the embedded model weights."""\n\n'
        "# This file is generated during the build process.\n"
    )
    if not registry_data:
        return f"{header}HASHES = {{}}\n"

    lines = [f"{header}HASHES = {{"]
    for category, file_hashes in registry_data.items():
        cat_literal = json.dumps(str(category))
        if not file_hashes:
            lines.append(f"    {cat_literal}: {{}},")
            continue
        lines.append(f"    {cat_literal}: {{")
        for filename, digest in file_hashes.items():
            lines.append(
                f"        {json.dumps(str(filename))}: {json.dumps(str(digest))},"
            )
        lines.append("    },")
    lines.append("}\n")
    return "\n".join(lines)


def _coerce_hashes_mapping(raw: object) -> dict[str, dict[str, str]] | None:
    if not isinstance(raw, dict):
        return None
    result: dict[str, dict[str, str]] = {}
    for category, entries in raw.items():
        if not isinstance(category, str) or not isinstance(entries, dict):
            return None
        typed_entries: dict[str, str] = {}
        for filename, digest in entries.items():
            if not isinstance(filename, str) or not isinstance(digest, str):
                return None
            typed_entries[filename] = digest
        result[category] = typed_entries
    return result


def read_existing_hashes(
    hashes_registry_path: str | os.PathLike[str],
) -> dict[str, dict[str, str]] | None:
    """Parse `HASHES` from an existing `hashes_registry.py` file when present."""
    registry_file = Path(hashes_registry_path)
    if not registry_file.is_file():
        return None
    try:
        with open(registry_file, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=str(registry_file))
    except (OSError, SyntaxError, ValueError):
        return None

    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "HASHES":
                    try:
                        parsed = ast.literal_eval(node.value)
                    except (SyntaxError, ValueError):
                        return None
                    return _coerce_hashes_mapping(parsed)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "HASHES"
            and node.value is not None
        ):
            try:
                parsed = ast.literal_eval(node.value)
            except (SyntaxError, ValueError):
                return None
            return _coerce_hashes_mapping(parsed)
    return None


def sync_hashes_registry(
    hashes_registry_path: str | os.PathLike[str],
    registry_data: Mapping[str, Mapping[str, str]],
) -> bool:
    """Write `hashes_registry.py` only when parsed `HASHES` differs from `registry_data`."""
    registry_file = Path(hashes_registry_path)
    normalized: dict[str, dict[str, str]] = {
        str(cat): {str(k): str(v) for k, v in entries.items()}
        for cat, entries in registry_data.items()
    }
    if read_existing_hashes(registry_file) == normalized:
        return False
    registry_file.parent.mkdir(parents=True, exist_ok=True)
    with open(registry_file, "w", encoding="utf-8") as f:
        f.write(format_hashes_registry(normalized))
    return True
