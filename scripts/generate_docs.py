"""Automated documentation generator."""

import contextlib
import functools
import glob
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_CHECK_STAGE_DIR: Path | None = None
MKDOCS_CONFIG_PATH = "mkdocs.yml"
ALLOWED_MKDOCS_YAML_TAGS = frozenset(
    {"tag:yaml.org,2002:python/name:pymdownx.superfences.fence_code_format"}
)


def _resolve_output_path(rel_path: str) -> str:
    """Resolve output path, redirecting into _CHECK_STAGE_DIR during read-only --check."""
    if _CHECK_STAGE_DIR is not None:
        staged = _CHECK_STAGE_DIR / rel_path
        staged.parent.mkdir(parents=True, exist_ok=True)
        return str(staged)
    return rel_path


def get_python_executable():
    """Dynamically determine the best Python command/executable."""
    return sys.executable


def generate_tutorial_docs():
    """Ensure tutorial documentation directory exists."""
    tutorials_dir = Path(_resolve_output_path(os.path.join("docs", "tutorials")))
    tutorials_dir.mkdir(parents=True, exist_ok=True)


def _init_defines_dunder_all(source_path: str) -> bool:
    """Return True if an __init__.py file defines `__all__` at module scope."""
    import ast

    try:
        with open(source_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=source_path)
    except Exception:
        return False

    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == "__all__"
                for target in node.targets
            ):
                return True
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name) and node.target.id == "__all__":
                return True
    return False


def _module_parts_for_doc_path(file_path: str) -> tuple[str, ...]:
    """Return dotted module path parts for a Python file or package __init__.py."""
    path_obj = Path(file_path)
    if path_obj.name == "__init__.py":
        return path_obj.parent.parts
    return path_obj.with_suffix("").parts


def generate_api_docs():
    """Generate API reference markdown from python modules."""
    app_dir = "app"
    output_file = _resolve_output_path(os.path.join("docs", "api_reference.md"))

    with open(output_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("# API Reference\n\n")
        f.write("This document is automatically generated. Do not edit manually.\n\n")

        f.write("## Core Architecture Diagram\n\n")
        f.write(
            "![Core Architecture Diagram](assets/diagrams/core_architecture.svg)\n\n"
        )
        f.write('<a id="appcoresanitizer"></a>\n\n')

        # Find all python files except ui and binaries; include __init__.py modules defining __all__
        py_files = glob.glob(os.path.join(app_dir, "**", "*.py"), recursive=True)
        py_files = [
            p
            for p in py_files
            if (not p.endswith("__init__.py") or _init_defines_dunder_all(p))
            and "/ui/" not in p
            and "\\ui\\" not in p
            and not p.endswith(("app/ui/__init__.py", "app\\ui\\__init__.py"))
            and "/binaries/" not in p
            and "\\binaries\\" not in p
        ]
        py_files.sort(key=_module_parts_for_doc_path)

        for file_path in py_files:
            parts = _module_parts_for_doc_path(file_path)
            module_name = ".".join(parts)
            f.write(f"## `{module_name}`\n\n")
            f.write(f"::: {module_name}\n\n")


def compile_diagram_assets():
    """Compile diagram specifications into SVG/PNG visual assets and Mermaid files."""
    from scripts.diagram_toolchain import build_diagrams, reset_markdown_anchors_cache

    reset_markdown_anchors_cache()
    if not build_diagrams(force=False, verify_only=_CHECK_STAGE_DIR is not None):
        raise RuntimeError(
            "One or more diagrams failed schema validation or rendering; "
            "see the messages above."
        )


def sync_config_schema():
    """Synchronize application configuration JSON schema."""
    from scripts.validate_config import sync_config_schema as _sync_schema

    if _CHECK_STAGE_DIR is not None:
        staged_schema = Path(
            _resolve_output_path(os.path.join("app", "config_schema.json"))
        )
        _sync_schema(schema_path=staged_schema)
    else:
        _sync_schema()


generate_config_schema = sync_config_schema


def generate_ui_docs():
    """Generate UI Reference from app/ui/*.py."""
    app_dir = os.path.join("app", "ui")
    output_file = _resolve_output_path(os.path.join("docs", "ui.md"))

    with open(output_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("# UI API Reference\n\n")
        f.write("This document is automatically generated. Do not edit manually.\n\n")

        f.write("## Component Architecture & Catalog Diagrams\n\n")

        f.write("### Catalog Interactive Workbench Workflow\n\n")
        f.write(
            "![Component Catalog Workflow](assets/diagrams/catalog_workflow.svg)\n\n"
        )

        f.write("### UI Component Hierarchy\n\n")
        f.write(
            "![UI Component Hierarchy](assets/diagrams/ui_component_hierarchy.svg)\n\n"
        )

        py_files = glob.glob(os.path.join(app_dir, "**", "*.py"), recursive=True)
        py_files = [p for p in py_files if not p.endswith("__init__.py")]
        py_files.sort(key=lambda p: Path(p).parts)

        for file_path in py_files:
            parts = Path(file_path).with_suffix("").parts
            module_name = ".".join(parts)
            f.write(f"## `{module_name}`\n\n")
            f.write(f"::: {module_name}\n\n")


_KEY_NAMES = {
    "escape": "Esc",
    "question_mark": "?",
    "space": "Space",
    "plus": "+",
    "minus": "-",
    "enter": "Enter",
    "tab": "Tab",
}


def _format_key(key: str) -> str:
    """Render a Textual key name (e.g. ``ctrl+s``) the way users type it."""
    if len(key) == 1:
        return f"Shift+{key}" if key.isupper() else key
    parts = key.split("+")
    rendered = [
        _KEY_NAMES.get(p, p.upper() if len(p) <= 2 else p.title()) for p in parts
    ]
    return "+".join(rendered)


def _collect_bindings(source_path: str) -> dict:
    """Map class name to ``(title, [(keys, description)])`` from ``BINDINGS``."""
    import ast

    with open(source_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    found = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not (
                isinstance(stmt, ast.Assign)
                and any(getattr(t, "id", None) == "BINDINGS" for t in stmt.targets)
                and isinstance(stmt.value, (ast.List, ast.Tuple))
            ):
                continue
            rows = {}
            for call in stmt.value.elts:
                if not (isinstance(call, ast.Call) and len(call.args) >= 3):
                    continue
                key, _action, desc = (
                    a.value if isinstance(a, ast.Constant) else None
                    for a in call.args[:3]
                )
                if not (isinstance(key, str) and isinstance(desc, str)):
                    continue
                # Several keys can share one description (e.g. ? and F1).
                keys = rows.setdefault(desc, [])
                label = _format_key(key)
                if label not in keys:
                    keys.append(label)
            title = re.sub(r"(?<!^)(?=[A-Z])", " ", node.name.removesuffix("Modal"))
            found[node.name] = (title, [(keys, desc) for desc, keys in rows.items()])
    return found


def generate_keyboard_docs():
    """Generate the keyboard shortcut reference from the TUI ``BINDINGS``."""
    bindings = {}
    ui_files = glob.glob(os.path.join("app", "ui", "**", "*.py"), recursive=True)
    for source_path in sorted(ui_files):
        bindings.update(_collect_bindings(source_path))
    output_file = _resolve_output_path(os.path.join("docs", "keyboard_shortcuts.md"))

    def table(rows):
        lines = ["| Keys | Action |", "|---|---|"]
        for keys, desc in rows:
            lines.append(f"| {' / '.join(f'`{k}`' for k in keys)} | {desc} |")
        return "\n".join(lines) + "\n\n"

    with open(output_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Keyboard Shortcuts\n\n")
        f.write(
            "This page is generated from the terminal UI's key bindings "
            "(`app/ui/`). Do not edit it manually.\n\n"
        )
        f.write("Press `?` or `F1` in the app to see these shortcuts.\n\n")

        main = bindings.pop("AutoSorterTUI", None)
        if main:
            f.write("## Main Window\n\n")
            f.write(table(main[1]))

        f.write("## Dialogs\n\n")
        f.write("`Esc` closes or cancels any dialog.\n\n")
        for name in sorted(bindings):
            title, rows = bindings[name]
            # Dialogs whose only keys are close/help are covered by the line above.
            extra = [r for r in rows if not set(r[0]) <= {"Esc", "?", "F1"}]
            if not extra:
                continue
            f.write(f"### {title}\n\n")
            f.write(table(extra))


def _describe_type(annotation) -> str:
    """Render a settings annotation compactly (``int``, ``list[str]``, ...)."""
    import typing

    if typing.get_origin(annotation) is typing.Literal:
        return "str"
    if typing.get_args(annotation):
        text = str(annotation)
    else:
        text = getattr(annotation, "__name__", None) or str(annotation)
    text = text.replace("typing.", "").replace("app.config.", "")
    # Annotated item types (e.g. constrained strings) add noise, not meaning:
    # keep the base type and drop the bracketed metadata.
    while "Annotated[" in text:
        start = text.index("Annotated[")
        depth, end = 0, start + len("Annotated")
        for end in range(end, len(text)):
            depth += {"[": 1, "]": -1}.get(text[end], 0)
            if depth == 0:
                break
        base = text[start + len("Annotated[") : end].split(",")[0]
        text = text[:start] + base + text[end + 1 :]
    return text


@contextlib.contextmanager
def _isolated_docs_env(pin_columns: str | None = "80"):
    """Temporarily clear host config env vars and pin COLUMNS for deterministic docs."""
    had_columns = "COLUMNS" in os.environ
    prev_columns = os.environ.get("COLUMNS")
    saved_env = {
        key: os.environ.pop(key)
        for key in list(os.environ)
        if key == "AUTOSORTER_APP_DIR" or key.startswith("SORTIFY_")
    }
    if pin_columns is not None:
        os.environ["COLUMNS"] = pin_columns
    try:
        yield
    finally:
        if pin_columns is not None:
            if had_columns and prev_columns is not None:
                os.environ["COLUMNS"] = prev_columns
            else:
                os.environ.pop("COLUMNS", None)
        for key in [
            k
            for k in list(os.environ)
            if k == "AUTOSORTER_APP_DIR" or k.startswith("SORTIFY_")
        ]:
            os.environ.pop(key, None)
        os.environ.update(saved_env)


def _describe_default(field) -> str:
    """Render a field default, calling factories and summarizing long values."""
    default = field.get_default(call_default_factory=True)
    if isinstance(default, set):
        default = sorted(default)
    if isinstance(default, str):
        for home_candidate in (Path.home(), Path.home().resolve()):
            try:
                default = f"~/{Path(default).relative_to(home_candidate).as_posix()}"
                break
            except ValueError:
                continue
    if isinstance(default, (list, dict)) and len(repr(default)) > 80:
        return f"{len(default)} built-in entries (see `app/config.py`)"
    return f"`{default!r}`" if isinstance(default, str) else f"`{default}`"


def _describe_bounds(field) -> str:
    """Render numeric bounds and literal choices as a short phrase."""
    import typing

    if typing.get_origin(field.annotation) is typing.Literal:
        return ", ".join(f"`{v}`" for v in typing.get_args(field.annotation))
    symbols = {"gt": ">", "ge": ">=", "lt": "<", "le": "<="}
    parts = []
    for meta in field.metadata:
        for attr, symbol in symbols.items():
            value = getattr(meta, attr, None)
            if value is not None:
                parts.append(f"{symbol} {value}")
    return ", ".join(parts)


def generate_admin_guide():
    """Generate Admin Guide from config and scripts."""
    with _isolated_docs_env(pin_columns="80"):
        _generate_admin_guide_impl()


def _generate_admin_guide_impl():
    """Write docs/admin_guide.md assuming isolated environment variables."""
    output_file = _resolve_output_path(os.path.join("docs", "admin_guide.md"))

    # Import config safely
    from app.config import Settings

    with open(output_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("# Administrator Guide\n\n")
        f.write("This document is automatically generated. Do not edit manually.\n\n")

        f.write("## Configuration Parameters\n\n")
        f.write(
            "The following parameters are extracted directly from the application's "
            "configuration schema (`app.config.Settings`). Set them with "
            "`smart-autosorter config --set NAME VALUE`, in "
            "`~/.autosorter/settings.json`, or as an environment variable with "
            "the `SORTIFY_` prefix (for example `SORTIFY_MAX_FOLDERS`). "
            "For recovering interrupted sorting sessions, reconciling headless transaction ledgers, or resolving stale lock files, see the [Ledger Reconciliation Runbook](runbooks/ledger_reconciliation.md).\n\n"
        )

        for name, field in Settings.model_fields.items():
            f.write(f"### `{name}`\n")
            f.write(f"- **Type**: `{_describe_type(field.annotation)}`\n")
            f.write(f"- **Default**: {_describe_default(field)}\n")
            allowed = _describe_bounds(field)
            if allowed:
                f.write(f"- **Allowed**: {allowed}\n")
            if field.description:
                f.write(f"\n{field.description}\n")
            f.write("\n")

        f.write("## Precedence Rules\n\n")
        f.write(
            "The application evaluates configuration parameters using a strict precedence hierarchy to determine how settings interact. The priority is applied as follows, from highest to lowest:\n\n"
        )
        f.write(
            "1. **Command-Line Flags:** Flags such as `--max-folders`, `--strategy` and `--conflict-policy` override every other source for that run only.\n"
            "2. **Local Settings File (`~/.autosorter/settings.json`):** Parameters defined here override environment variables and default properties.\n"
        )
        f.write(
            "3. **Environment Variables (or the app directory's `.env` file):** Configuration schema fields in `Settings` use the `SORTIFY_` prefix (for example `SORTIFY_MAX_FOLDERS=9`), while dedicated runtime control variables (such as `SORTIFY_AI_CONSENT`, `AUTOSORTER_APP_DIR`, and `NON_INTERACTIVE`; see [Runtime Environment Variables](#runtime-environment-variables)) control process startup, UI guards, and offline model paths. A `.env` file is read only from the app directory (`~/.autosorter/.env`, or `$AUTOSORTER_APP_DIR/.env`), never from the current working directory. Values from the environment apply to each run but are not written to `settings.json`, so they keep working until the setting is changed explicitly.\n"
        )
        f.write(
            "4. **Default Parameters:** Base defaults are used as fallbacks if a setting is not explicitly defined in the local file or environment.\n\n"
        )

        f.write("### Runtime Environment Variables\n\n")
        f.write(
            "In addition to `SORTIFY_<SETTING>` overrides for every field in `Settings`, the runtime inspects the following environment variables:\n\n"
            "| Variable | Source | Behavior |\n"
            "|---|---|---|\n"
            "| `SORTIFY_<SETTING>` | `app/config.py` | Per-run override for any `Settings` schema field (for example `SORTIFY_MAX_FOLDERS=9` or `SORTIFY_MAX_WORKERS=8`), read from the environment or `$AUTOSORTER_APP_DIR/.env`. |\n"
            "| `SORTIFY_AI_CONSENT` | `app/main.py`, `app/ui/tui.py` | Per-run AI consent override (`1`/`true`/`yes`/`on` grants consent; `0`/`false`/`no`/`off` declines consent). CLI `--accept-ai-consent` and `--decline-ai-consent` flags take higher precedence. |\n"
            "| `AUTOSORTER_APP_DIR` | `app/config.py`, `app/core/path_utils.py` | Overrides the application configuration and state directory (default `~/.autosorter`), relocating `settings.json`, `.env`, SQLite databases, and logs. |\n"
            "| `NON_INTERACTIVE` | `app/main.py`, `app/ui/tui.py` | When set to `1`, `true`, `yes`, or `on`, enables headless non-interactive mode, bypasses the onboarding wizard, and defaults unset `AI_CONSENT_GRANTED` to `False`. |\n"
            "| `NO_COLOR` | `app/main.py` | When non-empty, disables ANSI color codes and rich terminal styling across CLI output (equivalent to `--no-color`). |\n"
            "| `FORCE_TUI` | `app/ui/tui.py` | When non-empty, bypasses the interactive TTY check (`isatty()`) when launching the Textual TUI in non-TTY streams. |\n"
            "| `IGNORE_TERMINAL_SIZE` | `app/ui/tui.py` | When non-empty, bypasses the minimum `80x24` terminal dimension check during TUI startup. |\n"
            "| `LITE_BUILD` | `app/core/verifier.py` | When set to `1`, marks packaged or sandboxed execution as a lite distribution so missing optional ML dependencies (`torch`, `easyocr`) fall back cleanly instead of failing startup verification. |\n"
            "| `EASYOCR_MODULE_PATH` | `app/core/verifier.py`, `app/core/model_registry.py` | Custom base directory containing the `model/` folder for offline EasyOCR weights (`craft_mlt_25k.pth`, `english_g2.pth`) when `offline_bundle/easyocr` is absent. |\n"
            "| `SMART_AUTOSORTER_MODELS_ZIP` | `app/core/offline_loader.py` | Explicit file path override pointing to a sidecar `smart-autosorter-models.zip` offline model archive. |\n"
            "| `MODELS_ZIP_PATH` | `app/core/offline_loader.py` | Alternate environment variable pointing to a sidecar `smart-autosorter-models.zip` offline model archive. |\n"
            "| `<MODEL_ID>_PATH` / `MODEL_PATH` | `app/core/offline_loader.py` | Highest-precedence directory override for resolving a specific offline model bundle (for example `GENERATIVE_NAMING_PATH` or `MODEL_PATH`). |\n"
            "| `DEBUG` / `PROMPT_DUMP_FILE` | `app/core/analyzer_strategies.py` | Diagnostic controls enabling debug mode (`1`/`true`/`yes`/`on`) and optional generative prompt dumping when `DEBUG` is active. |\n\n"
        )

        f.write("## Dynamic Configuration Saves\n\n")
        f.write(
            "System settings modified during runtime are dynamically saved to the local JSON configuration file (`~/.autosorter/settings.json`) located in the user's home directory. To ensure stability and prevent excessive disk writes, these dynamic changes are saved with a short debounced delay of 0.5 seconds.\n\n"
        )

        f.write("## Compliance Policies & Routing Rules\n\n")
        f.write("### Policy Evaluation Flowchart\n\n")
        f.write(
            "![Policy Evaluation Flowchart](assets/diagrams/policy_evaluation_flow.svg)\n\n"
        )
        f.write("### Rule Syntax & Types\n\n")
        f.write(
            "Each entry in `POLICIES` follows the typed `PolicyRule` schema in `app/config_schema.json`:\n\n"
            "- **Required keys**: `type` (`str`), `expression` (non-empty `str`), `target_path` (relative `str`), and `priority` (`int`).\n"
            "- **Optional keys**: `halting` (`bool`, default `false`), `enabled` (`bool`, default `true`), `action` (`str`), and `post_unarchive_action` (`str`, with legacy aliases `post_action` and `archive_action` accepted when loading existing `settings.json` files).\n\n"
            "Compliance and routing policies support all **8** rule `type` values:\n\n"
            "- **`keyword`**: Case-insensitive substring match against the filename and extracted document text (or the filename alone when a preliminary status match is present; expressions `'secret'` and `'credential'` invoke credential/secret scanning across the path and text).\n"
            "- **`pattern`**: Case-insensitive substring match against the **lowercase filename (`os.path.basename(file_path).lower()`) only**—does not inspect extracted document text.\n"
            "- **`override`**: Case-insensitive match against the **lowercase filename or full file path (`file_path.lower()`)**—does not inspect extracted document text, allowing deterministic path-level routing overrides.\n"
            "- **`action`**: Lifecycle directive rule evaluated via case-insensitive substring matching against the filename and extracted document text.\n"
            "- **`compliance`**: Regulatory and sensitivity rule evaluated via case-insensitive substring matching against the filename and extracted document text (plus secret detection when `expression` is `'secret'` or `'credential'`).\n"
            "- **`unarchive`**: Archive ingestion rule evaluated via glob/extension masks or case-insensitive substring matching against the filename and extracted document text to trigger archive extraction workflows.\n"
            "- **`copy`**: Non-destructive copy rule evaluated via glob/extension masks or case-insensitive substring matching against the filename and extracted document text to write a verified copy to `target_path` while preserving the original.\n"
            "- **`ignore`**: Exclusion rule evaluated via glob/extension masks or case-insensitive substring matching against the filename and extracted document text to leave matching files untouched at origin.\n\n"
            "### Lifecycle Actions & Post-Unarchive Directives\n\n"
            "When a policy rule specifies an optional `action` key (`PolicyEngine.SUPPORTED_ACTIONS`), it must be one of the **7** supported lifecycle actions:\n\n"
            "- **`archive`**: Package matching files into verified `.zip` archives inside `target_path` before removing the original.\n"
            "- **`retain`**: Relocate matching files under read-only retention governance (`0o444` POSIX mode) at `target_path`.\n"
            "- **`quarantine`**: Intercept and stage matching files in compliance quarantine for forensic review before release.\n"
            "- **`redact`**: Scrub PII and secrets in verified UTF-8 text files before moving to `target_path`, or hold binary documents (`.pdf`, `.docx`) intact in compliance quarantine.\n"
            "- **`unarchive`**: Safely extract supported `.zip` and `.tar`/`.tar.gz`/`.tgz` archives into `target_path` with zip-bomb/size safety checks while rejecting unsupported `.7z`/`.rar` formats without deleting source archives.\n"
            "- **`copy`**: Copy matching files to `target_path` with SHA-256 verification while keeping the source file intact.\n"
            "- **`ignore`**: Leave matching files completely untouched at their original location (`target_path` may be empty).\n\n"
            "When `action` is set to `'unarchive'`, `post_unarchive_action` controls how the original archive file is handled after extraction and supports **8** values across three behaviors:\n\n"
            "- **Delete source archive** (default): `'delete'`, `'remove'`, `'cleanup'`.\n"
            "- **Relocate source archive** (for example to `post_unarchive_target`): `'move'`, `'archive'`, `'relocate'`.\n"
            "- **Retain source archive in place**: `'retain'`, `'keep'`.\n\n"
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
            "3. **Reset Configuration**: Delete the invalid `settings.json` file or click the reset button to restore default, valid configuration values, which will immediately re-enable automatic saving.\n"
        )
        f.write(
            "4. **Ledger & Session Recovery**: For recovering interrupted batch sorting sessions or resolving stale transaction records, refer to the dedicated [Ledger Reconciliation Runbook](runbooks/ledger_reconciliation.md).\n\n"
        )

        f.write("## Maintenance Scripts and CLI Commands\n\n")

        # app/main.py
        f.write("### `app/main.py`\n")
        import argparse

        from app.main import build_parser

        main_parser = build_parser()
        f.write("Main command-line interface for Smart AutoSorter AI Pro.\n\n")
        f.write("#### Top-Level Usage\n```text\n")
        f.write(main_parser.format_help().replace("\r\n", "\n"))
        f.write("```\n\n")

        f.write("#### Non-Interactive / Headless Execution Guard\n\n")
        f.write(
            "When executing `app/main.py` in non-interactive or detached standard input streams (such as CI automation pipelines or script redirections using `< /dev/null` where `sys.stdin.isatty()` returns `False`):\n\n"
        )
        f.write(
            "- **Target Path Auto-Routing**: If a positional target directory path is supplied without an explicit subcommand (e.g. `python3 app/main.py /tmp/docs < /dev/null`), execution is routed to headless batch document sorting (`handle_sort_command`) as a **dry run**: the plan is printed and no files are moved, because nobody is present to confirm it. Use the explicit `sort` subcommand (e.g. `python3 app/main.py sort /tmp/docs`) to move files from scripts or cron.\n"
        )
        f.write(
            "- **CLI Guidance & Exit Code 2**: If no target directory path or subcommand is supplied (e.g. `python3 app/main.py < /dev/null`), the application outputs CLI usage guidance to `sys.stderr` and exits cleanly with exit code `2`. This prevents non-interactive streams from hanging on terminal UI initialization.\n"
        )
        f.write(
            "- **Explicit Interface Flag Overrides**: If explicit UI or daemon flags (`--tui`, `--interactive`, `--demo`, `--daemon`) are invoked, the requested interface or background service launcher is executed as explicitly requested.\n\n"
        )

        f.write("#### Structured JSON Output, Error Payloads, and Exit Codes\n\n")
        f.write(
            "Automation pipelines invoking CLI commands with `--json` can rely on deterministic stream separation, error envelopes, and exit codes:\n\n"
            "- **Stream Routing (`stdout` vs `stderr`)**: When `--json` is passed, **both** structured command results and structured error payloads (`emit_cli_error(..., json_mode=True)` and `SortifyArgumentParser.error()`) are emitted to **`sys.stdout`** as UTF-8 JSON objects so callers can parse `stdout` unconditionally. Without `--json`, human-readable errors prefixed with `Error: ` and progress messages are written to **`sys.stderr`**.\n"
            '- **Error Payload Schema**: JSON errors emit `{"status": "error", "message": "<clean message>"}` (stripping any duplicate leading `\'Error: \'` prefix) plus command-specific context fields when available (such as `"job_id"` when `quarantine inspect` cannot find a record).\n'
            "- **Exit Codes**:\n"
            "  - `0`: Command succeeded (including `quarantine` batch commands where `status` is `'success'` or `'partial_success'`).\n"
            "  - `1`: Operational, validation, or runtime failure (missing directories, invalid configuration values, missing records, or `quarantine` batch runs where all jobs failed).\n"
            "  - `2`: CLI usage or argument validation failure (unrecognized flags, invalid option choices, missing nested `ledger` or `quarantine` subcommand, missing required `<job_id>` / `--job-ids`, or invoking `smart-autosorter` with no target path or subcommand on a non-interactive `stdin` stream). Passing `--json` anywhere on the command line formats exit-code `2` parser errors as JSON on `sys.stdout`.\n"
            '- **Quarantine Batch Status Envelope (`quarantine process`, `release`, `dismiss`)**: Batch commands return `{"status": "<batch_status>", "count": N, "results": [...]}` (plus top-level convenience keys `"job_id"` and `"status_code"` when `count == 1`), where `status` is one of:\n'
            "  - `'success'`: Every requested job ID completed its target transition (`exit code 0`).\n"
            "  - `'partial_success'`: At least one job succeeded and at least one job failed (`exit code 0`).\n"
            "  - `'failed'`: Zero requested jobs succeeded (`exit code 1`).\n\n"
        )

        def _write_subparsers_docs(
            parser_obj: argparse.ArgumentParser,
            prefix: str = "",
            heading_level: int = 5,
        ) -> None:
            nested_action = None
            for act in parser_obj._actions:
                if isinstance(act, argparse._SubParsersAction):
                    nested_action = act
                    break
            if not nested_action:
                return
            hashes = "#" * min(heading_level, 6)
            for sub_name, sub_parser in nested_action.choices.items():
                full_cmd = f"{prefix} {sub_name}".strip()
                f.write(f"{hashes} `{full_cmd}`\n```text\n")
                f.write(sub_parser.format_help().replace("\r\n", "\n"))
                f.write("```\n\n")
                if full_cmd == "ledger":
                    f.write(
                        "For comprehensive operational procedures, headless transaction reconciliation workflows, state transition handling, and stale daemon lock cleanup, refer to the dedicated [Ledger Reconciliation Runbook](runbooks/ledger_reconciliation.md).\n\n"
                    )
                _write_subparsers_docs(
                    sub_parser, prefix=full_cmd, heading_level=heading_level + 1
                )

        has_top_subparsers = any(
            isinstance(action, argparse._SubParsersAction)
            for action in main_parser._actions
        )
        if has_top_subparsers:
            f.write("#### Subcommands\n\n")
            _write_subparsers_docs(main_parser, prefix="", heading_level=5)

        # sandbox_cli.py
        f.write("### `sandbox_cli.py`\n")
        import sandbox_cli

        f.write(f"{sandbox_cli.__doc__}\n\n")
        f.write("#### Usage\n```text\n")
        f.write(sandbox_cli.build_parser().format_help().replace("\r\n", "\n"))
        f.write("```\n\n")

        # scripts/prepare_offline.py
        f.write("### `scripts/prepare_offline.py`\n")
        import scripts.prepare_offline as prepare_offline

        f.write(f"{prepare_offline.__doc__}\n\n")

        # scripts/install_offline.py
        f.write("### `scripts/install_offline.py`\n")
        import scripts.install_offline as install_offline

        f.write(f"{install_offline.__doc__}\n\n")


def _get_expected_security_fallback_policy() -> str:
    """Retrieve expected default advisory fallback policy from AppSettings or Settings."""
    from app.config import AppSettings, Settings

    if hasattr(AppSettings, "SECURITY_ADVISORY_FALLBACK_POLICY"):
        val = getattr(AppSettings, "SECURITY_ADVISORY_FALLBACK_POLICY")
        if isinstance(val, str):
            return val.strip().lower()

    field = getattr(Settings, "model_fields", {}).get(
        "SECURITY_ADVISORY_FALLBACK_POLICY"
    )
    if field is not None and field.default is not None:
        return str(field.default).strip().lower()

    try:
        inst = AppSettings(filepath="/tmp/sortify_nonexistent_settings.json")
        val = getattr(inst, "SECURITY_ADVISORY_FALLBACK_POLICY", None)
        if isinstance(val, str):
            return val.strip().lower()
    except Exception:
        pass

    return "strict"


def validate_security_policy(content=None, doc_path="SECURITY.md"):
    """Validate that documented security advisory fallback policy matches AppSettings."""
    errors = []
    if content is None:
        p = Path(doc_path)
        if not p.exists():
            return [f"Missing security policy document: {doc_path}"]
        try:
            content = p.read_text(encoding="utf-8")
        except Exception as e:
            return [f"Could not read security policy document {doc_path}: {e}"]

    # Safely handle unexpected file formatting or missing target headings
    if (
        "## Telemetry and Network Access" not in content
        and "SECURITY_ADVISORY_FALLBACK_POLICY" not in content
    ):
        return errors

    if "SECURITY_ADVISORY_FALLBACK_POLICY" not in content:
        errors.append(
            f"Target setting 'SECURITY_ADVISORY_FALLBACK_POLICY' not found under '## Telemetry and Network Access' in {doc_path}"
        )
        return errors

    policy_lines = [
        line
        for line in content.splitlines()
        if "SECURITY_ADVISORY_FALLBACK_POLICY" in line
    ]
    policy_context = " ".join(policy_lines)

    # Match phrases documenting the default fallback policy
    default_match = re.search(
        r"(?:the\s+default,\s*[`']?(\w+)[`']?|default(?:s)?\s*(?:to|is|:)\s*[`']?(\w+)[`']?|[`']?(\w+)[`']?\s+(?:is|as)\s+the\s+default)",
        policy_context,
        re.IGNORECASE,
    )

    if not default_match:
        errors.append(
            f"Could not determine documented default fallback policy for 'SECURITY_ADVISORY_FALLBACK_POLICY' in {doc_path}"
        )
        return errors

    documented_default = (
        (default_match.group(1) or default_match.group(2) or default_match.group(3))
        .strip()
        .lower()
    )

    expected_policy = _get_expected_security_fallback_policy()
    if documented_default != expected_policy:
        errors.append(
            f"Documented fallback policy in {doc_path} ('{documented_default}') does not match AppSettings default ('{expected_policy}')"
        )

    return errors


_NON_NETWORK_URL_ATTR_PREFIX_RE = re.compile(
    r"\b(?:xmlns(?::[A-Za-z_][\w.-]*)?\s*=\s*['\"]$|placeholder\s*=\s*['\"][^'\"\r\n]*$)"
)


def _is_non_network_url_literal(file_content: str, match_start: int, url: str) -> bool:
    """Return True for XML namespace URIs and UI placeholder sample URLs."""
    from urllib.parse import urlparse

    prefix = file_content[max(0, match_start - 96) : match_start]
    if _NON_NETWORK_URL_ATTR_PREFIX_RE.search(prefix):
        return True
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except ValueError:
        hostname = ""
    return hostname == "proxy.example.com"


def update_security_md(sec_file="SECURITY.md", local_only: bool = True):
    """Scan for network dependencies and update SECURITY.md."""
    # Validate security policy prose in SECURITY.md against AppSettings
    policy_errors = validate_security_policy(doc_path=sec_file)
    if policy_errors:
        raise ValueError(
            f"Security policy validation failed: {'; '.join(policy_errors)}"
        )

    import concurrent.futures
    import re
    from urllib.parse import urlparse

    from scripts.validate_links import (
        DEFAULT_BYPASS_DOMAINS,
        URL_REGEX,
        is_template_or_redacted_url,
        validate_url,
    )

    network_deps = []
    urls_to_validate = set()

    config_path = "network_rules.json"
    config = {}
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

    # Scan only the shipped code named in network_rules.json, so test fixtures
    # (fake proxies and credentials) never reach SECURITY.md.
    scan_roots = [t for t in config.get("targets", []) if os.path.isdir(t)] or ["."]
    walk = (entry for target in scan_roots for entry in os.walk(target))

    for root, dirs, files in walk:
        # Exclude common external dependency dirs
        dirs[:] = [
            d
            for d in dirs
            if not d.startswith(".")
            and d
            not in (
                "venv",
                "env",
                "__pycache__",
                "node_modules",
                "site-packages",
                "build",
                "dist",
                "htmlcov",
            )
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
                if "[REDACTED]" in url:
                    continue
                if _is_non_network_url_literal(file_content, match.start(), url):
                    continue
                if is_template_or_redacted_url(url):
                    static_prefix = url.split("{", 1)[0]
                    try:
                        domain = urlparse(static_prefix).netloc
                    except Exception:
                        domain = ""
                    if domain:
                        network_deps.append(
                            f"- `{domain}` (via `{rel_path}`): Auto-discovered network URL"
                        )
                    continue

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
    network_deps = sorted(set(network_deps))

    has_critical_error = False
    print(f"Discovered {len(urls_to_validate)} unique network URLs to validate.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        # Request timeouts of no more than 5 seconds per domain handled by validate_url?
        # Wait, validate_url in validate_links.py uses TIMEOUT = 3.0 which is <= 5s.
        future_to_url = {
            executor.submit(
                validate_url, url, DEFAULT_BYPASS_DOMAINS, local_only=local_only
            ): url
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
                print(f"[FAIL] {url} generated an exception: {e}")
                has_critical_error = True

    if has_critical_error:
        raise Exception(
            "One or more network dependencies failed connectivity validation."
        )

    if not sec_file:
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
                "\nThis section is automatically generated by scanning the Python files in the directories listed under `targets` in `network_rules.json`. Tests are not scanned.\n\n"
            )
            for dep in network_deps:
                out_lines.append(dep + "\n")
            out_lines.append("\n")
            continue

        if in_network_section and line.startswith("## "):
            in_network_section = False

        if not in_network_section:
            out_lines.append(line)

    target_sec_file = (
        _resolve_output_path(sec_file)
        if _CHECK_STAGE_DIR is not None and sec_file == "SECURITY.md"
        else sec_file
    )
    with open(target_sec_file, "w", encoding="utf-8", newline="\n") as f:
        f.writelines(out_lines)


def get_handwritten_docs():
    """Discover all handwritten markdown documentation files."""
    generated_set = {
        "docs/api_reference.md",
        "docs/ui.md",
        "docs/admin_guide.md",
        "docs/keyboard_shortcuts.md",
        "SECURITY.md",
    }
    docs = []
    for root_file in ["README.md", "PRIVACY.md"]:
        if os.path.exists(root_file):
            docs.append(root_file)

    if os.path.exists("docs"):
        for root, dirs, files in os.walk("docs"):
            for f in sorted(files):
                if f.endswith(".md"):
                    full_path = Path(root, f).as_posix()
                    if full_path not in generated_set:
                        docs.append(full_path)
    return sorted(set(docs))


def audit_auto_merge_policy(content=None, doc_path="docs/auto_merge_policy.md"):
    """Audit auto-merge policy documentation consistency against classifier definitions."""
    from scripts.classify_pr import BOT_AUTHORS

    errors = []
    if content is None:
        p = Path(doc_path)
        if not p.exists():
            return [f"Missing auto-merge policy document: {doc_path}"]
        try:
            content = p.read_text(encoding="utf-8")
        except Exception as e:
            return [f"Could not read auto-merge policy document {doc_path}: {e}"]

    section = _extract_markdown_section(content, "1. Risk Classification & Eligibility")
    section_content = section if section is not None else content
    for bot in sorted(BOT_AUTHORS):
        if bot not in section_content:
            errors.append(
                f"Auto-merge policy ({doc_path}) is missing bot author: '{bot}'"
            )

    return errors


def audit_model_hosts(
    architecture_path="docs/architecture.md",
    security_path="SECURITY.md",
    architecture_content=None,
    security_content=None,
    allowed_hosts=None,
):
    """Audit documentation consistency for allowed model download hosts and network controls."""
    if allowed_hosts is None:
        try:
            from app.core.downloader import ALLOWED_MODEL_HOSTS

            allowed_hosts = ALLOWED_MODEL_HOSTS
        except Exception:
            allowed_hosts = frozenset({"huggingface.co", "hf.co"})

    errors = []

    # 1. Audit architecture documentation
    if architecture_content is None and architecture_path is not None:
        p = Path(architecture_path)
        if not p.exists():
            errors.append(f"Missing architecture document: {architecture_path}")
        else:
            try:
                architecture_content = p.read_text(encoding="utf-8")
            except Exception as e:
                errors.append(
                    f"Could not read architecture document {architecture_path}: {e}"
                )

    if architecture_content is not None:
        for host in sorted(allowed_hosts):
            if host not in architecture_content:
                errors.append(
                    f"Architecture doc ({architecture_path or 'provided content'}) is missing allowed model host: '{host}'"
                )
        if "https" not in architecture_content.lower():
            errors.append(
                f"Architecture doc ({architecture_path or 'provided content'}) does not document HTTPS scheme requirement for model downloads."
            )
        if (
            "ip" not in architecture_content.lower()
            or "literal" not in architecture_content.lower()
        ):
            errors.append(
                f"Architecture doc ({architecture_path or 'provided content'}) does not document IP literal restrictions for model downloads."
            )

    # 2. Audit security policy documentation
    if security_content is None and security_path is not None:
        p = Path(security_path)
        if not p.exists():
            errors.append(f"Missing security policy document: {security_path}")
        else:
            try:
                security_content = p.read_text(encoding="utf-8")
            except Exception as e:
                errors.append(
                    f"Could not read security policy document {security_path}: {e}"
                )

    if security_content is not None:
        for host in sorted(allowed_hosts):
            if host not in security_content:
                errors.append(
                    f"Security policy ({security_path or 'provided content'}) is missing allowed model host: '{host}'"
                )
        if (
            "ssrf" not in security_content.lower()
            and "ip literal" not in security_content.lower()
        ):
            errors.append(
                f"Security policy ({security_path or 'provided content'}) does not document SSRF prevention rules for model downloads."
            )

    return errors


audit_model_host_docs = audit_model_hosts
validate_model_hosts = audit_model_hosts


REQUIRED_PRIVACY_HOST_TOKENS = (
    "~/.autosorter/",
    "AUTOSORTER_APP_DIR",
    "~/.sortify/keys",
    r"%APPDATA%\Sortify\keys",
    "~/.smart-autosorter/",
    "autosorter_sessions_<user>",
)


def _extract_markdown_section(content: str, heading: str) -> str | None:
    """Return stripped body text under ``## <heading>``, or None if absent."""
    pattern = re.compile(
        rf"^##\s+{re.escape(heading)}\s*$([\s\S]*?)(?=^##\s+|\Z)",
        re.MULTILINE,
    )
    match = pattern.search(content)
    if not match:
        return None
    return match.group(1).strip()


def audit_privacy_policy(content=None, doc_path="PRIVACY.md"):
    """Audit PRIVACY.md encryption claims and host directory disclosures against runtime code."""
    errors = []
    if content is None:
        p = Path(doc_path)
        if not p.exists():
            return [f"Missing privacy policy document: {doc_path}"]
        try:
            content = p.read_text(encoding="utf-8")
        except Exception as e:
            return [f"Could not read privacy policy document {doc_path}: {e}"]

    try:
        from app.core import db_conn

        fail_closed = bool(getattr(db_conn, "_disable_pytest_win_fallback", True))
    except Exception:
        fail_closed = True

    affirmative_fallback_re = re.compile(
        r"(?<!instead of )(?<!rather than )(?<!without )(?<!never )(?<!not )"
        r"\b(?:falls?\s+back\s+to\s+(?:plain|standard|unencrypted)\s+sqlite|"
        r"falling\s+back\s+to\s+(?:plain|standard|unencrypted)\s+sqlite|"
        r"operating\s+in\s+standard\s+sqlite\s+fallback|"
        r"plain\s+sqlite\s+(?:fallback|pages))\b",
        re.IGNORECASE,
    )
    claims_fail_closed = bool(
        re.search(
            r"sqlcipher[\s\S]{0,240}?\b(?:fails?\s+closed|raises?\s+(?:a\s+|an\s+)?(?:\w+\s+)?error|refuses?\s+to\s+open)\b",
            content,
            re.IGNORECASE,
        )
    )

    if fail_closed:
        if affirmative_fallback_re.search(content):
            errors.append(
                f"Privacy policy ({doc_path}) claims a plain SQLite fallback, contradicting fail-closed SQLCipher enforcement in app/core/db_conn.py"
            )
        if not claims_fail_closed:
            errors.append(
                f"Privacy policy ({doc_path}) must state that missing or unverified SQLCipher fails closed and raises an error"
            )
    else:
        if claims_fail_closed and not affirmative_fallback_re.search(content):
            errors.append(
                f"Privacy policy ({doc_path}) claims fail-closed SQLCipher behavior, but app.core.db_conn._disable_pytest_win_fallback is False"
            )

    for token in REQUIRED_PRIVACY_HOST_TOKENS:
        if token not in content:
            errors.append(
                f"Privacy policy ({doc_path}) is missing host storage disclosure: '{token}'"
            )

    if "undo history" not in content.lower():
        errors.append(
            f"Privacy policy ({doc_path}) is missing undo history storage disclosure"
        )

    return errors


@functools.lru_cache(maxsize=16)
def _count_frontend_store_callers(root: Path) -> list[str]:
    """Return sorted relative paths of front-end modules importing store modules directly."""
    import ast

    root = Path(root).resolve()

    store_modules = {
        "app.core.db",
        "app.core.db_worker",
        "app.core.history",
        "app.core.ledger",
    }
    candidates = []
    for rel in ("app/main.py", "app/demo.py"):
        p = root / rel
        if p.is_file():
            candidates.append(p)
    for subdir in ("app/cli", "app/ui"):
        base = root / subdir
        if base.is_dir():
            candidates.extend(sorted(base.rglob("*.py")))

    callers = []
    for file_path in candidates:
        try:
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        imported = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name in store_modules for alias in node.names):
                    imported = True
                    break
            elif isinstance(node, ast.ImportFrom) and node.module in store_modules:
                imported = True
                break
        if imported:
            callers.append(file_path.relative_to(root).as_posix())
    return sorted(callers)


@functools.lru_cache(maxsize=16)
def _inspect_adr_runtime_state(repo_root: Path | None = None) -> dict:
    """Inspect local runtime source files offline to verify ADR implementation claims."""
    root = (Path(repo_root) if repo_root is not None else REPO_ROOT).resolve()

    ng_path = root / "app/core/network_guard.py"
    ng_src = ng_path.read_text(encoding="utf-8") if ng_path.is_file() else ""
    network_guard_patches_socket = "socket.socket.connect =" in ng_src

    mover_path = root / "app/core/mover.py"
    mover_src = mover_path.read_text(encoding="utf-8") if mover_path.is_file() else ""
    mover_has_coexisting_ledgers = (
        "TransactionLedger" in mover_src
        or "log_step" in mover_src
        or "transaction_step" in mover_src
    )

    history_path = root / "app/core/history.py"
    history_src = (
        history_path.read_text(encoding="utf-8") if history_path.is_file() else ""
    )
    history_has_snapshot_rollback = (
        "def _rollback_from_snapshot_internal" in history_src
    )

    val_arch_path = root / "scripts/validate_architecture.py"
    store_imports_blocked = False
    if val_arch_path.is_file():
        val_src = val_arch_path.read_text(encoding="utf-8")
        store_imports_blocked = all(
            token in val_src
            for token in (
                "app.core.db",
                "app.core.history",
                "app.core.ledger",
            )
        )
    frontend_store_callers = _count_frontend_store_callers(root)

    db_conn_path = root / "app/core/db_conn.py"
    db_conn_src = (
        db_conn_path.read_text(encoding="utf-8") if db_conn_path.is_file() else ""
    )
    foreign_keys_enabled = bool(
        re.search(r"PRAGMA\s+foreign_keys\s*=\s*(?:ON|1)\b", db_conn_src, re.IGNORECASE)
    )

    db_path = root / "app/core/db.py"
    db_src = db_path.read_text(encoding="utf-8") if db_path.is_file() else ""
    has_settings_audit = bool(
        re.search(
            r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+settings_audit\b",
            db_src,
            re.IGNORECASE,
        )
    )
    docs_ddl_match = re.search(
        r"CREATE\s+TABLE(?:\s+IF\s+NOT\s+EXISTS)?\s+documents\s*\(([\s\S]*?)\)",
        db_src,
        re.IGNORECASE,
    )
    has_documents_status = bool(
        docs_ddl_match
        and re.search(r"^\s*status\s+TEXT\b", docs_ddl_match.group(1), re.MULTILINE)
    )

    return {
        "network_guard_patches_socket": network_guard_patches_socket,
        "mover_has_coexisting_ledgers": mover_has_coexisting_ledgers,
        "history_has_snapshot_rollback": history_has_snapshot_rollback,
        "store_imports_blocked": store_imports_blocked,
        "frontend_store_callers": frontend_store_callers,
        "foreign_keys_enabled": foreign_keys_enabled,
        "has_settings_audit": has_settings_audit,
        "has_documents_status": has_documents_status,
    }


def audit_adr_status(
    content=None,
    doc_path=None,
    repo_root: Path | None = None,
    runtime_state: dict | None = None,
):
    """Audit accepted ADRs for accurate module citations and Implementation sections."""
    root = (Path(repo_root) if repo_root is not None else REPO_ROOT).resolve()
    if doc_path is None and content is None:
        adr_dir = root / "docs" / "adr"
        if not adr_dir.is_dir():
            return [f"Missing ADR directory: {adr_dir}"]
        shared_state = (
            runtime_state
            if runtime_state is not None
            else _inspect_adr_runtime_state(root)
        )
        all_errors = []
        for adr_file in sorted(adr_dir.glob("*.md")):
            all_errors.extend(
                audit_adr_status(
                    content=None,
                    doc_path=adr_file.as_posix(),
                    repo_root=root,
                    runtime_state=shared_state,
                )
            )
        return all_errors

    label = doc_path or "ADR"
    if content is None:
        assert doc_path is not None
        p = Path(doc_path)
        if not p.exists():
            return [f"Missing ADR document: {doc_path}"]
        try:
            content = p.read_text(encoding="utf-8")
        except Exception as e:
            return [f"Could not read ADR document {doc_path}: {e}"]

    frontmatter_match = re.match(r"^---\s*\n([\s\S]*?)\n---", content)
    if not frontmatter_match or not re.search(
        r"^status:\s*accepted\b",
        frontmatter_match.group(1),
        re.MULTILINE | re.IGNORECASE,
    ):
        return []

    errors = []
    filename = os.path.basename(str(label))
    state = (
        runtime_state if runtime_state is not None else _inspect_adr_runtime_state(root)
    )

    if filename.startswith("0001-"):
        if state["network_guard_patches_socket"]:
            if "app/core/network_guard.py" not in content:
                errors.append(
                    f"ADR 0001 ({label}) must cite 'app/core/network_guard.py' for worker socket sandboxing"
                )
            if "app/core/shared_registry.py" in content:
                errors.append(
                    f"ADR 0001 ({label}) cites re-export shim 'app/core/shared_registry.py' instead of 'app/core/network_guard.py'"
                )

    elif filename.startswith("0005-"):
        impl = _extract_markdown_section(content, "Implementation")
        if not impl:
            errors.append(
                f"ADR 0005 ({label}) is missing a non-empty '## Implementation' section"
            )

    elif filename.startswith("0006-"):
        has_open_gap = (
            state["mover_has_coexisting_ledgers"]
            or state["history_has_snapshot_rollback"]
        )
        impl = _extract_markdown_section(content, "Implementation")
        if has_open_gap:
            if not impl:
                errors.append(
                    f"ADR 0006 ({label}) is missing '## Implementation' while legacy ledgers or snapshot rollback remain in app/core/"
                )
            else:
                impl_lower = impl.lower()
                if "active" not in impl_lower or "open" not in impl_lower:
                    errors.append(
                        f"ADR 0006 ({label}) '## Implementation' must separate active and open items"
                    )
                if state["mover_has_coexisting_ledgers"] and (
                    "app/core/mover.py" not in impl
                    or ("TransactionLedger" not in impl and "log_step" not in impl)
                ):
                    errors.append(
                        f"ADR 0006 ({label}) '## Implementation' must document coexisting ledgers in app/core/mover.py"
                    )
                if state["history_has_snapshot_rollback"] and (
                    "app/core/history.py" not in impl
                    or "_rollback_from_snapshot_internal" not in impl
                ):
                    errors.append(
                        f"ADR 0006 ({label}) '## Implementation' must document snapshot rollback (_rollback_from_snapshot_internal) in app/core/history.py"
                    )
        else:
            if not impl:
                errors.append(
                    f"ADR 0006 ({label}) is missing a non-empty '## Implementation' section"
                )
            else:
                if (
                    "_rollback_from_snapshot_internal" in impl
                    or "still open" in impl.lower()
                ):
                    errors.append(
                        f"ADR 0006 ({label}) '## Implementation' still marks legacy rollback or ledgers open after removal"
                    )
                required_citations = (
                    "app/core/moves.py",
                    "app/core/mover.py",
                    "app/core/history.py",
                    "app/cli/ledger_cli.py",
                )
                missing_citations = [
                    mod for mod in required_citations if mod not in impl
                ]
                if missing_citations:
                    errors.append(
                        f"ADR 0006 ({label}) '## Implementation' must cite canonical ledger modules: {', '.join(missing_citations)}"
                    )

    elif filename.startswith("0007-"):
        callers = state["frontend_store_callers"]
        has_open_gap = (not state["store_imports_blocked"]) or bool(callers)
        impl = _extract_markdown_section(content, "Implementation")
        if has_open_gap:
            if not impl:
                errors.append(
                    f"ADR 0007 ({label}) is missing '## Implementation' while front-end store imports remain unblocked"
                )
            else:
                impl_lower = impl.lower()
                if "active" not in impl_lower or "open" not in impl_lower:
                    errors.append(
                        f"ADR 0007 ({label}) '## Implementation' must separate active and open items"
                    )
                if (
                    "scripts/validate_architecture.py" not in impl
                    or "unblocked" not in impl_lower
                ):
                    errors.append(
                        f"ADR 0007 ({label}) '## Implementation' must note unblocked front-end store callers in scripts/validate_architecture.py"
                    )
        elif impl and "unblocked" in impl.lower():
            errors.append(
                f"ADR 0007 ({label}) '## Implementation' still claims front-end store callers are unblocked after layer enforcement landed"
            )

    elif filename.startswith("0008-"):
        has_open_gap = (
            not state["foreign_keys_enabled"]
            or not state["has_settings_audit"]
            or not state["has_documents_status"]
        )
        impl = _extract_markdown_section(content, "Implementation")
        if has_open_gap:
            if not impl:
                errors.append(
                    f"ADR 0008 ({label}) is missing '## Implementation' while foreign_keys or baseline schema items remain unimplemented"
                )
            else:
                impl_lower = impl.lower()
                if "active" not in impl_lower or "open" not in impl_lower:
                    errors.append(
                        f"ADR 0008 ({label}) '## Implementation' must separate active and open items"
                    )
                if not state["foreign_keys_enabled"] and (
                    "app/core/db_conn.py" not in impl
                    or "foreign keys" not in impl_lower
                    and "foreign_keys" not in impl_lower
                ):
                    errors.append(
                        f"ADR 0008 ({label}) '## Implementation' must note disabled foreign keys in app/core/db_conn.py"
                    )
                if (
                    not state["has_settings_audit"] or not state["has_documents_status"]
                ) and (
                    "app/core/db.py" not in impl
                    or "settings_audit" not in impl
                    or "status" not in impl_lower
                ):
                    errors.append(
                        f"ADR 0008 ({label}) '## Implementation' must note unbuilt documents status column and settings_audit in app/core/db.py"
                    )
        elif impl and "leaves foreign keys disabled" in impl.lower():
            errors.append(
                f"ADR 0008 ({label}) '## Implementation' still claims foreign keys are disabled after PRAGMA foreign_keys=ON was enabled"
            )

    return errors


def audit_handwritten_docs(local_only: bool = True):
    """Audit external links and relative codebase path references in handwritten docs."""
    import re

    from scripts.validate_links import DEFAULT_BYPASS_DOMAINS, URL_REGEX, validate_url

    getattr(_inspect_adr_runtime_state, "cache_clear", lambda: None)()
    getattr(_count_frontend_store_callers, "cache_clear", lambda: None)()
    adr_runtime_state: dict | None = None

    handwritten_docs = get_handwritten_docs()
    errors = []

    path_pattern = re.compile(
        r"\b(?:app|scripts|docs|tests|notebooks|offline_bundle|sandbox)/(?:[a-zA-Z0-9_\-.]+/)*[a-zA-Z0-9_\-.]+(?:\.[a-zA-Z0-9]+)?"
    )

    for doc_path in handwritten_docs:
        if not os.path.exists(doc_path):
            continue
        try:
            with open(doc_path, "r", encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue

        # 1. External URL validation
        for match in URL_REGEX.finditer(content):
            url = match.group(0).strip().rstrip(".,;)'\"`<> \n\r\t\\")
            if not url:
                continue
            success, msg, is_critical = validate_url(
                url, DEFAULT_BYPASS_DOMAINS, local_only=local_only
            )
            if not success and is_critical:
                errors.append(f"Broken external link in {doc_path}: {url} ({msg})")

        # 2. Codebase path reference validation (excluding fenced code blocks)
        content_no_code_blocks = re.sub(r"```[\s\S]*?```", "", content)

        found_paths = set()

        # Markdown links: [label](path)
        for match in re.finditer(r"\[[^\]]*\]\(([^)]+)\)", content_no_code_blocks):
            target = match.group(1).strip()
            if not target.startswith(("http://", "https://", "mailto:", "#")):
                path_only = target.split("#")[0].strip()
                if path_only:
                    found_paths.add(path_only)

        # Path regex pattern
        for match in path_pattern.finditer(content_no_code_blocks):
            p = match.group(0).rstrip(".,;)'\"`:")
            if p:
                found_paths.add(p)

        # Verify existence
        for p in sorted(found_paths):
            doc_dir = os.path.dirname(doc_path)
            rel_to_doc = os.path.join(doc_dir, p) if doc_dir else p
            if not (os.path.exists(p) or os.path.exists(rel_to_doc)):
                errors.append(f"Invalid codebase path reference in {doc_path}: {p}")

        # 3. Auto-merge policy consistency check
        if doc_path.endswith("auto_merge_policy.md"):
            errors.extend(audit_auto_merge_policy(content=content, doc_path=doc_path))

        # 4. Architecture model download host consistency check
        if doc_path.endswith("architecture.md"):
            errors.extend(
                audit_model_hosts(
                    architecture_path=doc_path,
                    security_path=None,
                    architecture_content=content,
                )
            )

        # 5. Privacy policy encryption and host directory consistency check
        if os.path.basename(doc_path) == "PRIVACY.md":
            errors.extend(audit_privacy_policy(content=content, doc_path=doc_path))

        # 6. ADR implementation status and module citation consistency check
        norm_doc_path = doc_path.replace("\\", "/")
        if norm_doc_path.startswith("docs/adr/") or "/adr/" in norm_doc_path:
            if adr_runtime_state is None:
                adr_runtime_state = _inspect_adr_runtime_state(REPO_ROOT.resolve())
            errors.extend(
                audit_adr_status(
                    content=content,
                    doc_path=doc_path,
                    runtime_state=adr_runtime_state,
                )
            )

    return errors


def validate_mermaid_diagrams():
    """Validate Mermaid diagrams across all core documentation and tutorial files."""
    import re

    from scripts.diagram_toolchain import validate_mermaid_syntax

    doc_files = []
    if os.path.exists("docs"):
        for root, dirs, files in os.walk("docs"):
            for f in sorted(files):
                if f.endswith(".md"):
                    doc_files.append(Path(root, f).as_posix())

    errors = []

    for file_path in sorted(set(doc_files)):
        if not os.path.exists(file_path):
            continue

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                full_text = f.read()
        except Exception as e:
            errors.append(f"Could not read file {file_path}: {e}")
            continue

        blocks = re.findall(r"```mermaid\s*([\s\S]*?)```", full_text)

        for idx, block in enumerate(blocks, start=1):
            syntax_errors = validate_mermaid_syntax(block)
            for err in syntax_errors:
                errors.append(
                    f"Invalid Mermaid diagram block #{idx} in {file_path}: {err}"
                )

    return errors


def validate_mkdocs_config(config_path: str | Path = MKDOCS_CONFIG_PATH) -> list[str]:
    """Validate mkdocs.yml syntax and reject any non-allowlisted YAML tags safely."""
    import yaml
    from yaml.constructor import ConstructorError

    class _RestrictedMkDocsSafeLoader(yaml.SafeLoader):
        """SafeLoader subclass permitting only the approved superfences formatter tag."""

    def _construct_allowed_fence_tag(loader: yaml.SafeLoader, node: yaml.Node) -> str:
        if not isinstance(node, yaml.ScalarNode) or node.value.strip() != "":
            raise ConstructorError(
                None,
                None,
                f"expected an empty scalar for allowlisted tag '{node.tag}'",
                node.start_mark,
            )
        return node.tag

    for allowed_tag in ALLOWED_MKDOCS_YAML_TAGS:
        _RestrictedMkDocsSafeLoader.add_constructor(
            allowed_tag, _construct_allowed_fence_tag
        )

    safe_base_tags = frozenset(
        tag for tag in yaml.SafeLoader.yaml_constructors if isinstance(tag, str)
    )
    allowed_tags = safe_base_tags | ALLOWED_MKDOCS_YAML_TAGS

    cfg_path = Path(config_path)
    label = cfg_path.as_posix()
    if not os.path.exists(cfg_path):
        return [f"Missing MkDocs configuration file: {label}"]

    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw_yaml = f.read()
    except Exception as exc:
        return [f"Could not read MkDocs configuration {label}: {exc}"]

    errors: list[str] = []
    try:
        composed_docs = list(
            yaml.compose_all(raw_yaml, Loader=_RestrictedMkDocsSafeLoader)
        )
    except yaml.YAMLError as exc:
        return [f"Invalid YAML syntax in {label}: {exc}"]

    if len(composed_docs) != 1 or composed_docs[0] is None:
        return [
            f"Invalid MkDocs configuration {label}: expected a single non-empty YAML mapping document"
        ]

    visited_ids: set[int] = set()

    def _inspect_node(node: yaml.Node) -> None:
        node_id = id(node)
        if node_id in visited_ids:
            return
        visited_ids.add(node_id)

        if node.tag not in allowed_tags:
            errors.append(
                f"Disallowed YAML tag in {label}: '{node.tag}' "
                f"(only {sorted(ALLOWED_MKDOCS_YAML_TAGS)} is permitted)"
            )
        elif node.tag in ALLOWED_MKDOCS_YAML_TAGS:
            if not isinstance(node, yaml.ScalarNode) or node.value.strip() != "":
                errors.append(
                    f"Malformed allowlisted YAML tag in {label}: '{node.tag}' must have an empty scalar value"
                )

        if isinstance(node, yaml.SequenceNode):
            for child in node.value:
                _inspect_node(child)
        elif isinstance(node, yaml.MappingNode):
            for key_node, val_node in node.value:
                _inspect_node(key_node)
                _inspect_node(val_node)

    _inspect_node(composed_docs[0])
    if errors:
        return errors

    try:
        loaded_docs = list(yaml.load_all(raw_yaml, Loader=_RestrictedMkDocsSafeLoader))
    except yaml.YAMLError as exc:
        return [f"Rejected YAML construct in {label}: {exc}"]

    if len(loaded_docs) != 1 or not isinstance(loaded_docs[0], dict):
        return [
            f"Invalid MkDocs configuration {label}: top-level YAML document must be a mapping"
        ]

    return []


def validate_mkdocs_site(site_dir="site"):
    """Validate built MkDocs site artifacts (non-empty index.html and populated sitemap.xml)."""
    from defusedxml import ElementTree as ET

    site_path = Path(site_dir)
    errors = []

    index_path = site_path / "index.html"
    if not index_path.is_file():
        errors.append(f"Missing MkDocs root page: {index_path.as_posix()}")
    else:
        try:
            index_content = index_path.read_text(encoding="utf-8")
        except Exception as exc:
            errors.append(
                f"Could not read MkDocs root page {index_path.as_posix()}: {exc}"
            )
        else:
            if not index_content.strip():
                errors.append(f"Empty MkDocs root page: {index_path.as_posix()}")

    sitemap_path = site_path / "sitemap.xml"
    if not sitemap_path.is_file():
        errors.append(f"Missing MkDocs sitemap: {sitemap_path.as_posix()}")
    else:
        try:
            sitemap_content = sitemap_path.read_text(encoding="utf-8")
            root = ET.fromstring(sitemap_content)
        except Exception as exc:
            errors.append(
                f"Invalid XML in MkDocs sitemap {sitemap_path.as_posix()}: {exc}"
            )
        else:
            loc_entries = [
                (elem.text or "").strip()
                for elem in root.iter()
                if (elem.tag == "loc" or elem.tag.endswith("}loc"))
                and (elem.text or "").strip()
            ]
            if not loc_entries:
                errors.append(
                    f"MkDocs sitemap contains no <loc> entries: {sitemap_path.as_posix()}"
                )

    return errors


def main():
    """Execute the unified documentation generation and validation pipeline."""
    initial_cwd = Path.cwd()
    os.chdir(REPO_ROOT)
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
    parser.add_argument(
        "--build",
        dest="build",
        action="store_true",
        default=False,
        help="Run MkDocs HTML site generation build step.",
    )
    parser.add_argument(
        "--no-build",
        "--skip-build",
        dest="build",
        action="store_false",
        help="Skip MkDocs HTML site generation build step (default).",
    )
    parser.add_argument(
        "--local-only",
        dest="local_only",
        action="store_true",
        default=True,
        help="Validate external URL syntax locally without making live network requests (default).",
    )
    parser.add_argument(
        "--live-links",
        dest="local_only",
        action="store_false",
        help="Perform live network reachability checks on external URLs.",
    )
    parser.add_argument(
        "--mkdocs-config",
        default=None,
        help="Optional path to mkdocs.yml configuration file to validate (default: mkdocs.yml).",
    )

    args = parser.parse_args()

    mkdocs_cfg_target: str | Path
    if args.mkdocs_config:
        candidate_cfg = Path(args.mkdocs_config)
        mkdocs_cfg_target = (
            candidate_cfg
            if candidate_cfg.is_absolute()
            else (initial_cwd / candidate_cfg)
        )
    else:
        mkdocs_cfg_target = MKDOCS_CONFIG_PATH

    mkdocs_cfg_errors = validate_mkdocs_config(mkdocs_cfg_target)
    if mkdocs_cfg_errors:
        sys.stderr.write(
            "\nError: MkDocs configuration validation failed with the following issues:\n"
        )
        for err in mkdocs_cfg_errors:
            sys.stderr.write(f"  - {err}\n")
        sys.exit(1)
        return

    # 1. Identify files to check
    generated_files = [
        Path("docs/api_reference.md").as_posix(),
        Path("docs/ui.md").as_posix(),
        Path("docs/admin_guide.md").as_posix(),
        Path("docs/keyboard_shortcuts.md").as_posix(),
        "SECURITY.md",
        Path("app/config_schema.json").as_posix(),
    ]

    initial_contents = {}
    if args.check:
        for filepath in generated_files:
            if os.path.exists(filepath):
                try:
                    with open(filepath, "r", encoding="utf-8") as f:
                        initial_contents[filepath] = f.read().replace("\r\n", "\n")
                except Exception:
                    initial_contents[filepath] = None
            else:
                initial_contents[filepath] = None

    # 2. Run documentation generation
    tasks = [
        ("generate_tutorial_docs", generate_tutorial_docs),
        ("generate_api_docs", generate_api_docs),
        ("generate_ui_docs", generate_ui_docs),
        ("generate_admin_guide", generate_admin_guide),
        ("generate_keyboard_docs", generate_keyboard_docs),
        (
            "update_security_md",
            lambda: update_security_md(local_only=args.local_only),
        ),
        ("compile_diagram_assets", compile_diagram_assets),
        ("sync_config_schema", sync_config_schema),
    ]

    import tempfile
    from contextlib import nullcontext

    global _CHECK_STAGE_DIR
    changed_files = []
    stage_ctx = tempfile.TemporaryDirectory() if args.check else nullcontext(None)
    with stage_ctx as stage_dir_str:
        _CHECK_STAGE_DIR = Path(stage_dir_str) if stage_dir_str is not None else None
        try:
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
                return

            # Validate Mermaid diagram syntax across documentation files
            mermaid_validation_errors = validate_mermaid_diagrams()
            if mermaid_validation_errors:
                sys.stderr.write(
                    "\nError: Mermaid diagram syntax validation failed with the following issues:\n"
                )
                for err in mermaid_validation_errors:
                    sys.stderr.write(f"  - {err}\n")
                sys.exit(1)
                return

            # 3. Check for unsynced files and audit handwritten docs if requested
            if args.check:
                for filepath in generated_files:
                    new_content = None
                    staged_candidate = (
                        _CHECK_STAGE_DIR / filepath
                        if _CHECK_STAGE_DIR is not None
                        else None
                    )
                    read_target = (
                        str(staged_candidate)
                        if staged_candidate is not None and staged_candidate.exists()
                        else filepath
                    )
                    if os.path.exists(read_target):
                        try:
                            with open(read_target, "r", encoding="utf-8") as f:
                                new_content = f.read().replace("\r\n", "\n")
                        except Exception:
                            pass
                    if new_content != initial_contents.get(filepath):
                        changed_files.append(filepath)

                doc_audit_errors = audit_handwritten_docs(local_only=args.local_only)
                host_audit_errors = audit_model_hosts()
                all_audit_errors = list(
                    dict.fromkeys(doc_audit_errors + host_audit_errors)
                )
                if all_audit_errors:
                    sys.stderr.write(
                        "\nError: Documentation audit failed with the following issues:\n"
                    )
                    for err in all_audit_errors:
                        sys.stderr.write(f"  - {err}\n")
                    sys.exit(1)
                    return
        finally:
            _CHECK_STAGE_DIR = None

    # 4. Run MkDocs build when explicitly requested (and preserve strict build flags)
    if args.build:
        try:
            import mkdocs  # noqa: F401

            has_mkdocs = True
        except ModuleNotFoundError:
            has_mkdocs = False

        if not has_mkdocs:
            sys.stderr.write(
                "\nWarning: 'mkdocs' is not installed in the current environment.\n"
                "Skipping MkDocs HTML site generation.\n"
                "To build documentation with MkDocs, run with the docs dependency group:\n"
                "  uv run --group docs python scripts/generate_docs.py --build\n"
                "  or: uv run --group docs mkdocs build\n\n"
            )
        else:
            build_cmd = [sys.executable, "-m", "mkdocs", "build"]
            if args.mkdocs_config:
                build_cmd.extend(["-f", str(mkdocs_cfg_target)])
            if args.strict:
                build_cmd.append("--strict")

            print(f"Running MkDocs build: {' '.join(build_cmd)}")
            result = subprocess.run(build_cmd, capture_output=False)
            if result.returncode != 0:
                sys.stderr.write(
                    f"Error: MkDocs build failed with exit code {result.returncode}\n"
                )
                sys.exit(result.returncode)
                return

            site_validation_errors = validate_mkdocs_site()
            if site_validation_errors:
                sys.stderr.write(
                    "\nError: MkDocs site validation failed with the following issues:\n"
                )
                for err in site_validation_errors:
                    sys.stderr.write(f"  - {err}\n")
                sys.exit(1)
                return
    else:
        print("Skipping MkDocs build (pass --build to generate HTML site).")

    # 5. Report if files were out of sync
    if args.check and changed_files:
        sys.stderr.write(
            "\nError: The following auto-generated documentation files are out of sync or have modified contents:\n"
        )
        for f in changed_files:
            sys.stderr.write(f"  - {f}\n")
        sys.stderr.write("\nPlease commit the updated documentation files.\n")
        sys.exit(1)
        return

    print("Documentation generation and validation completed successfully.")


if __name__ == "__main__":
    main()
