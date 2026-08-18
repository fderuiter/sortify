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

        # Find all python files except ui and binaries
        py_files = glob.glob(os.path.join(app_dir, "**", "*.py"), recursive=True)
        py_files = [
            p
            for p in py_files
            if not p.endswith("__init__.py")
            and "/ui/" not in p
            and "\\ui\\" not in p
            and "/binaries/" not in p
            and "\\binaries\\" not in p
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

        f.write("## Air-Gapped Local AI Deployment & Offline Model Bundles\n\n")
        f.write(
            "Smart AutoSorter supports air-gapped enterprise deployments without requiring internet access or manual setup UI configuration.\n\n"
        )
        f.write("### Automated Bundle Detection\n")
        f.write(
            "Upon launch, the application performs dynamic system checks (<500ms) to scan local filesystem paths for pre-installed model weight bundles and PyTorch dependencies. If a valid offline model bundle is detected, air-gapped local AI categorization is automatically activated with `AI_CONSENT_GRANTED = True`.\n\n"
        )
        f.write("### Offline Model Bundle Directory Locations\n")
        f.write(
            "Administrators can deploy model weight files into any of the following resolved search directories (evaluated in order of precedence):\n\n"
        )
        f.write("1. **Environment Variable**: Path defined in `${MODEL_ID}_PATH` (e.g., `MODEL_PATH`).\n")
        f.write("2. **Packaged Execution Bundle**: `offline_bundle/{model_id}` within PyInstaller executable context (`_MEIPASS`).\n")
        f.write("3. **Workspace Path**: `offline_bundle/{model_id}` relative to working directory or application base path.\n")
        f.write("4. **Application Settings Path**: `~/.autosorter/{model_id}` or `~/.autosorter/offline_bundle/{model_id}`.\n")
        f.write("5. **User Home Fallback**: `~/.smart-autosorter/offline_bundle/{model_id}`.\n\n")
        f.write("### Dependency Expectations & Network Sandboxing\n")
        f.write(
            "Air-gapped AI categorization expects local PyTorch dependencies (`import torch`). All local inference tasks execute within a strict socket-level network sandbox that blocks outgoing connections, ensuring 100% offline data privacy.\n\n"
        )

        f.write("## Compliance Policies & Routing Rules\n\n")
        f.write("### Rule Syntax & Types\n\n")
        f.write(
            "Compliance policies categorize and sort documents based on three rule types:\n\n"
        )
        f.write(
            "- **Keyword Rules**: Search for files containing a specific word or phrase anywhere in their text contents (for example, 'invoice' or 'billing').\n"
        )
        f.write(
            "- **Pattern Rules**: Match files using structured formatting or text sequences (such as a standard format of letters followed by numbers) to match specific document types.\n"
        )
        f.write(
            "- **Override Rules**: Check for exact text matches, taking precedent to bypass standard classification rules.\n\n"
        )

        f.write("### Sequential Execution & Priority\n\n")
        f.write(
            "Rules are evaluated sequentially, starting from the highest priority value down to the lowest. "
        )
        f.write(
            "Because rules are checked in priority order, if a higher-priority rule matches, it will be executed first. "
        )
        f.write(
            "This can sometimes result in 'shadowing', where a lower-priority rule never runs because a higher-priority rule has already matched the same conditions. "
        )
        f.write(
            "To resolve overlaps, adjust rule priority numbers or make matching conditions more specific.\n\n"
        )

        f.write("### Halting Parameters\n\n")
        f.write("Each policy includes a 'Halt on mismatch' setting. ")
        f.write(
            "When active, if a document fails to meet this rule's criteria, the system will immediately stop evaluating any remaining lower-priority rules. "
        )
        f.write(
            "This halting behavior is crucial for enforcing strict sequential checks and ensuring that files do not proceed to general classification or AI-based sorting if they fail compliance conditions.\n\n"
        )

        f.write("### Path Validation Rules\n\n")
        f.write(
            "To ensure system security, stability, and compatibility across operating systems, all target paths must comply with the following strict validation rules:\n\n"
        )
        f.write(
            "- **No Absolute Paths**: All target paths must be relative paths and cannot start with leading slashes (such as `/` or `\\`).\n"
        )
        f.write(
            "- **No Directory Traversal**: Paths are blocked from using directory traversal segments (such as `..`) to prevent files from being moved outside of the designated folders.\n"
        )
        f.write(
            '- **No Illegal Characters**: Target paths must not contain any prohibited characters, including `<`, `>`, `:`, `"`, `|`, `?`, or `*`.\n\n'
        )

        f.write("### Configuration Recovery & Troubleshooting\n\n")
        f.write(
            "If the system detects invalid fields or syntax errors in the configuration file (`settings.json`), automatic saves are locked. "
        )
        f.write(
            "This safeguard prevents overwriting and potentially corrupting your existing settings. "
        )
        f.write(
            "While saves are suspended, the application will use temporary default values to prevent crashes.\n\n"
        )
        f.write("To resolve a blocked-save state, follow these recovery options:\n\n")
        f.write(
            "1. **Check Warning Banners**: Review the detailed list of validation errors displayed under the warning banner in the application settings dialog.\n"
        )
        f.write(
            "2. **Manually Edit Settings**: Open the local configuration file (`~/.autosorter/settings.json`) and correct the invalid values or formats.\n"
        )
        f.write(
            "3. **Reset Configuration**: Delete the invalid `settings.json` file or click the reset button to restore default, valid configuration values, which will immediately re-enable automatic saving.\n\n"
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


def update_security_md():
    """Scan for network dependencies and update SECURITY.md."""
    import concurrent.futures
    import re
    from urllib.parse import urlparse

    from scripts.validate_links import DEFAULT_BYPASS_DOMAINS, URL_REGEX, validate_url

    network_deps = []
    urls_to_validate = set()

    config_path = "network_rules.json"
    valid_rules = []
    if os.path.exists(config_path):
        import json

        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
        raw_rules = config.get("rules", [])
        for rule in raw_rules:
            if (
                rule.get("pattern")
                and rule.get("match_type")
                and rule.get("description")
            ):
                valid_rules.append(rule)

    import_regex = re.compile(
        r"^\s*(?:import|from)\s+(urllib|requests|httpx|aiohttp|socket|ftplib|http\.client)(?:\s|\.|$)"
    )

    for root, dirs, files in os.walk("."):
        # Exclude common external dependency dirs
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".")
            and d not in ("venv", "env", "__pycache__", "node_modules", "site-packages")
        ]
        for file in files:
            if not file.endswith(".py"):
                continue

            filepath = os.path.join(root, file)
            # Avoid scanning the script itself
            if "generate_docs.py" in filepath.replace("\\", "/"):
                continue

            with open(filepath, "r", encoding="utf-8") as f:
                try:
                    file_content = f.read()
                except UnicodeDecodeError:
                    continue

            rel_path = os.path.relpath(filepath, ".").replace("\\", "/")

            # Check rules
            for rule in valid_rules:
                pattern = rule.get("pattern")
                match_type = rule.get("match_type")
                desc = rule.get("description")

                match_found = False
                matched_str = ""

                if match_type == "substring" and pattern in file_content:
                    match_found = True
                    matched_str = pattern
                elif match_type == "regex":
                    try:
                        m = re.search(pattern, file_content)
                        if m:
                            match_found = True
                            matched_str = m.group(0)
                    except re.error:
                        pass

                if match_found:
                    network_deps.append(f"- `{matched_str}` (via `{rel_path}`): {desc}")

            # Find URLs
            for match in URL_REGEX.finditer(file_content):
                url = match.group(0).rstrip(".,;)'\"")
                urls_to_validate.add(url)

                try:
                    domain = urlparse(url).netloc
                except Exception:
                    domain = url

                if domain:
                    network_deps.append(
                        f"- `{domain}` (via `{rel_path}`): Auto-discovered network URL"
                    )

            # Find Imports
            for line in file_content.splitlines():
                m = import_regex.search(line)
                if m:
                    module = m.group(1)
                    network_deps.append(
                        f"- `{module}` (via `{rel_path}`): Auto-discovered network import"
                    )

    # Deduplicate and sort
    network_deps = sorted(list(set(network_deps)))

    has_critical_error = False
    print(f"Discovered {len(urls_to_validate)} unique network URLs to validate.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        # Request timeouts of no more than 5 seconds per domain handled by validate_url?
        # Wait, validate_url in validate_links.py uses TIMEOUT = 3.0 which is <= 5s.
        future_to_url = {
            executor.submit(validate_url, url, DEFAULT_BYPASS_DOMAINS): url
            for url in urls_to_validate
        }
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            try:
                success, msg, is_critical = future.result()
                if not success and is_critical:
                    print(f"[FAIL] {url} - {msg}")
                    has_critical_error = True
                elif not success:
                    print(f"[WARN] {url} - {msg}")
                else:
                    print(f"[PASS] {url} - {msg}")
            except Exception as e:
                print(f"[WARN] {url} generated an exception: {e}")

    if has_critical_error:
        raise Exception(
            "One or more network dependencies failed connectivity validation."
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


def main():
    import argparse
    import subprocess
    import traceback

    parser = argparse.ArgumentParser(
        description="Unified Documentation Pipeline Manager"
    )
    parser.add_argument(
        "--check",
        "--verify",
        action="store_true",
        help="Detect and report unsynced markdown reference files.",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=True,
        help="Enforce strict validation mode on MkDocs build (default).",
    )
    parser.add_argument(
        "--no-strict",
        dest="strict",
        action="store_false",
        help="Disable strict validation mode on MkDocs build.",
    )

    args = parser.parse_args()

    # 1. Identify files to check
    generated_files = [
        os.path.join("docs", "api_reference.md"),
        os.path.join("docs", "ui.md"),
        os.path.join("docs", "admin_guide.md"),
        "SECURITY.md",
    ]

    initial_contents = {}
    if args.check:
        for filepath in generated_files:
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        initial_contents[filepath] = f.read()
                except Exception:
                    initial_contents[filepath] = None
            else:
                initial_contents[filepath] = None

    # 2. Run documentation generation
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
        sys.stderr.write(
            "Documentation generation encountered errors in the following modules:\n\n"
        )
        for name, exc, exc_info in errors:
            sys.stderr.write(f"--- Error in {name} ---\n")
            traceback.print_exception(*exc_info, file=sys.stderr)
            sys.stderr.write("\n")
        sys.exit(1)

    # 3. Check for unsynced files if requested
    changed_files = []
    if args.check:
        for filepath in generated_files:
            new_content = None
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        new_content = f.read()
                except Exception:
                    pass
            if new_content != initial_contents.get(filepath):
                changed_files.append(filepath)

    # 4. Run MkDocs build (and preserve strict build flags)
    build_cmd = [sys.executable, "-m", "mkdocs", "build"]
    if args.strict:
        build_cmd.append("--strict")

    print(f"Running MkDocs build: {' '.join(build_cmd)}")
    result = subprocess.run(build_cmd, capture_output=False)
    if result.returncode != 0:
        sys.stderr.write(
            f"Error: MkDocs build failed with exit code {result.returncode}\n"
        )
        sys.exit(result.returncode)

    # 5. Report if files were out of sync
    if args.check and changed_files:
        sys.stderr.write(
            "\nError: The following auto-generated documentation files are out of sync or have modified contents:\n"
        )
        for f in changed_files:
            sys.stderr.write(f"  - {f}\n")
        sys.stderr.write("\nPlease commit the updated documentation files.\n")
        sys.exit(1)

    print("Documentation generation and validation completed successfully.")


if __name__ == "__main__":
    main()
