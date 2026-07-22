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

        try:
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
        except subprocess.CalledProcessError as e:
            f.write(f"Error capturing help: {e}\n")
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
            "The offline bundle must be extracted into the application's local configuration directory (e.g., `~/.autosorter/`). The directory structure must exactly match the following:\n\n"
        )
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
    
    config_path = "network_rules.json"
    if os.path.exists(config_path):
        try:
            import json
            import re
            
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
                
            raw_rules = config.get("rules", [])
            targets = config.get("targets", [])
            
            valid_rules = []
            for rule in raw_rules:
                if not rule.get("pattern") or not rule.get("match_type") or not rule.get("description"):
                    print(f"Warning: Malformed rule {rule}")
                else:
                    valid_rules.append(rule)
            
            # Helper to check matching files
            for target in targets:
                target_path = os.path.join(".", target)
                if not os.path.exists(target_path):
                    continue
                for root, dirs, files in os.walk(target_path):
                    for file in files:
                        if not file.endswith(".py") and not file.endswith(".yml") and not file.endswith(".yaml"):
                            continue
                        filepath = os.path.join(root, file)
                        # Avoid scanning the generate_docs script itself to prevent self-matching
                        if "generate_docs.py" in filepath:
                            continue
                        
                        try:
                            with open(filepath, "r", encoding="utf-8") as f:
                                content = f.read()
                                
                            for rule in valid_rules:
                                pattern = rule.get("pattern")
                                match_type = rule.get("match_type")
                                desc = rule.get("description")
                                
                                match_found = False
                                matched_str = ""
                                
                                if match_type == "substring":
                                    if pattern in content:
                                        match_found = True
                                        matched_str = pattern
                                elif match_type == "regex":
                                    try:
                                        m = re.search(pattern, content)
                                        if m:
                                            match_found = True
                                            matched_str = m.group(0)
                                    except re.error as e:
                                        print(f"Warning: Invalid regex pattern '{pattern}': {e}")
                                else:
                                    print(f"Warning: Unknown match_type '{match_type}' in rule {rule}")
                                    
                                if match_found:
                                    # Normalize path for cross-platform consistency
                                    rel_path = os.path.relpath(filepath, ".").replace("\\", "/")
                                    dep_entry = f"- `{matched_str}` (via `{rel_path}`): {desc}"
                                    if dep_entry not in network_deps:
                                        network_deps.append(dep_entry)
                        except Exception as e:
                            print(f"Warning: Error reading {filepath}: {e}")
        except Exception as e:
            print(f"Warning: Failed to process network_rules.json: {e}")

    # Deduplicate and sort to ensure consistent output
    network_deps = sorted(list(set(network_deps)))

    sec_file = "SECURITY.md"
    try:
        with open(sec_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        print(f"Warning: Failed to read {sec_file}: {e}")
        return

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

    try:
        with open(sec_file, "w", encoding="utf-8", newline="\n") as f:
            f.writelines(out_lines)
    except Exception as e:
        print(f"Warning: Failed to write {sec_file}: {e}")


if __name__ == "__main__":
    generate_api_docs()
    generate_ui_docs()
    generate_admin_guide()
    update_security_md()
