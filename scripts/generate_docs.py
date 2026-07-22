"""Automated documentation generator."""

import glob
import os
import sys
from pathlib import Path

# Add project root to sys.path so we can import app modules
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def get_python_executable():
    """Dynamically determine the best Python command/executable."""
    return sys.executable


def generate_api_docs():
    """Generate API reference markdown from python modules."""
    app_dir = "app"
    output_file = os.path.join("docs", "api_reference.md")

    with open(output_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("# API Reference\n\n")
        f.write("This document is automatically generated. Do not edit manually.\n\n")

        # Find all python files except ui
        py_files = glob.glob(os.path.join(app_dir, "**", "*.py"), recursive=True)
        py_files = [
            p
            for p in py_files
            if not p.endswith("__init__.py") and "/ui/" not in p and "\\ui\\" not in p
        ]
        py_files.sort(key=lambda p: Path(p).parts)

        for file_path in py_files:
            parts = Path(file_path).with_suffix("").parts
            module_name = ".".join(parts)
            f.write(f"## `{module_name}`\n\n")
            f.write(f"::: {module_name}\n\n")


def generate_ui_docs():
    """Generate UI Reference from app/ui/*.py."""
    app_dir = os.path.join("app", "ui")
    output_file = os.path.join("docs", "ui.md")

    with open(output_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("# UI API Reference\n\n")
        f.write("This document is automatically generated. Do not edit manually.\n\n")

        py_files = glob.glob(os.path.join(app_dir, "*.py"))
        py_files = [p for p in py_files if not p.endswith("__init__.py")]
        py_files.sort(key=lambda p: Path(p).parts)

        for file_path in py_files:
            parts = Path(file_path).with_suffix("").parts
            module_name = ".".join(parts)
            f.write(f"## `{module_name}`\n\n")
            f.write(f"::: {module_name}\n\n")


def generate_admin_guide():
    """Generate Admin Guide from config and scripts."""
    output_file = os.path.join("docs", "admin_guide.md")

    # Import config safely
    from app.config import Settings

    with open(output_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Administrator Guide\n\n")
        f.write("This document is automatically generated. Do not edit manually.\n\n")

        f.write("## Configuration Parameters\n\n")
        f.write(
            "The following parameters are extracted directly from the application's configuration schema (`app.config.Settings`).\n\n"
        )

        for name, field in Settings.model_fields.items():
            default_val = field.default
            if isinstance(default_val, set):
                default_val = sorted(list(default_val))
            elif isinstance(default_val, str):
                try:
                    rel_path = Path(default_val).relative_to(Path.home())
                    default_val = f"~/{rel_path.as_posix()}"
                except ValueError:
                    pass

            f.write(f"### `{name}`\n")
            f.write(f"- **Default**: `{default_val}`\n")
            f.write(f"- **Required**: `{field.is_required()}`\n\n")

        f.write("## Precedence Rules\n\n")
        f.write(
            "The application evaluates configuration parameters using a strict precedence hierarchy to determine how settings interact. The priority is applied as follows, from highest to lowest:\n\n"
        )
        f.write(
            "1. **Local Settings File (`~/.autosorter/settings.json`):** This local configuration file takes absolute priority. Any parameters defined here will override environment variables and default properties.\n"
        )
        f.write(
            "2. **Environment Variables (or `.env` file):** Variables configured in the environment take precedence over default parameters.\n"
        )
        f.write(
            "3. **Default Parameters:** Base defaults are used as fallbacks if a setting is not explicitly defined in the local file or environment.\n\n"
        )

        f.write("## Dynamic Configuration Saves\n\n")
        f.write(
            "System settings modified during runtime are dynamically saved to the local JSON configuration file (`~/.autosorter/settings.json`) located in the user's home directory. To ensure stability and prevent excessive disk writes, these dynamic changes are saved with a short debounced delay of 0.5 seconds.\n\n"
        )

        f.write("## Maintenance Scripts and CLI Commands\n\n")

        # sandbox_cli.py
        f.write("### `sandbox_cli.py`\n")
        import sandbox_cli

        f.write(f"{sandbox_cli.__doc__}\n\n")
        f.write("#### Usage\n```text\n")
        import subprocess

        env = os.environ.copy()
        env["COLUMNS"] = "80"
        result = subprocess.run(
            ["uv", "run", "python", "sandbox_cli.py", "--help"],
            capture_output=True,
            text=True,
            check=True,
            env=env,
        )
        f.write(result.stdout.replace("\r\n", "\n"))
        f.write("```\n\n")

        # scripts/prepare_offline.py
        f.write("### `scripts/prepare_offline.py`\n")
        import scripts.prepare_offline as prepare_offline

        f.write(f"{prepare_offline.__doc__}\n\n")

        # scripts/install_offline.py
        f.write("### `scripts/install_offline.py`\n")
        import scripts.install_offline as install_offline

        f.write(f"{install_offline.__doc__}\n\n")

        # Offline Sideloading
        f.write("## Offline Sideloading Deployment\n\n")
        f.write(
            "To deploy the application in a completely offline environment, you must sideload the model bundle. The application will bypass the setup download prompt and activate semantic local sorting automatically if it detects a valid local bundle on startup.\n\n"
        )
        f.write("### Folder Layout Requirements\n\n")
        f.write(
            "The offline bundle must be extracted into either the application's local project directory (e.g., `offline_bundle/`) or the user configuration directory (e.g., `~/.autosorter/`). If bundles exist in both locations, the local project directory takes priority. The directory structure must exactly match one of the following:\n\n"
        )
        f.write("Local Project Directory:\n")
        f.write("```text\n")
        f.write("offline_bundle/\n")
        f.write("├── model/\n")
        f.write("│   ├── config.json\n")
        f.write("│   ├── pytorch_model.bin\n")
        f.write("│   ├── tokenizer.json\n")
        f.write("│   └── ... (other model weights)\n")
        f.write("└── model_manifest.json\n")
        f.write("```\n\n")
        f.write("User Configuration Directory:\n")
        f.write("```text\n")
        f.write("~/.autosorter/\n")
        f.write("├── model/\n")
        f.write("│   ├── config.json\n")
        f.write("│   ├── pytorch_model.bin\n")
        f.write("│   ├── tokenizer.json\n")
        f.write("│   └── ... (other model weights)\n")
        f.write("└── model_manifest.json\n")
        f.write("```\n\n")
        f.write("### JSON Manifest Structure\n\n")
        f.write(
            "The `model_manifest.json` file must reside in the same parent directory as the `model/` folder. It maps the relative file paths of the model weights to their SHA256 checksums to guarantee integrity. The structure must be:\n\n"
        )
        f.write("```json\n")
        f.write("{\n")
        f.write('  "config.json": "e56f4d...",\n')
        f.write('  "pytorch_model.bin": "a1b2c3..."\n')
        f.write("}\n")
        f.write("```\n\n")


def update_security_md():
    """Scan for network dependencies and update SECURITY.md."""
    network_deps = []

    # scan scripts/install_offline.py
    with open(os.path.join("scripts", "install_offline.py"), "r") as f:
        content = f.read()
        if "https://astral.sh" in content:
            network_deps.append(
                "- `https://astral.sh` (via `scripts/install_offline.py`): Bootstrapping the `uv` package manager."
            )

    # scan scripts/prepare_offline.py
    with open(os.path.join("scripts", "prepare_offline.py"), "r") as f:
        content = f.read()
        if "huggingface_hub" in content or "huggingface-hub" in content:
            network_deps.append(
                "- `huggingface-hub` (via `scripts/prepare_offline.py`): Model acquisition (e.g., `sentence-transformers/all-MiniLM-L6-v2`)."
            )
        if "download.pytorch.org" in content:
            network_deps.append(
                "- `https://download.pytorch.org` (via `scripts/prepare_offline.py`): Fetching CPU-optimized PyTorch wheels."
            )

    sec_file = "SECURITY.md"
    with open(sec_file, "r", encoding="utf-8") as f:
        lines = f.readlines()

    out_lines = []
    in_network_section = False
    for line in lines:
        if line.startswith("## Network Dependencies"):
            in_network_section = True
            out_lines.append(line)
            out_lines.append(
                "\nThis section is automatically generated by scanning setup and data acquisition scripts.\n\n"
            )
            for dep in network_deps:
                out_lines.append(dep + "\n")
            out_lines.append("\n")
            continue

        if in_network_section and line.startswith("## "):
            in_network_section = False

        if not in_network_section:
            out_lines.append(line)

    with open(sec_file, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(out_lines)


if __name__ == "__main__":
    import traceback

    tasks = [
        ("generate_api_docs", generate_api_docs),
        ("generate_ui_docs", generate_ui_docs),
        ("generate_admin_guide", generate_admin_guide),
        ("update_security_md", update_security_md),
    ]

    errors = []

    for name, task in tasks:
        try:
            task()
        except Exception as e:
            errors.append((name, e, sys.exc_info()))

    if errors:
        sys.stderr.write("Documentation generation encountered errors in the following modules:\n\n")
        for name, exc, exc_info in errors:
            sys.stderr.write(f"--- Error in {name} ---\n")
            traceback.print_exception(*exc_info, file=sys.stderr)
            sys.stderr.write("\n")
        
        sys.exit(1)

