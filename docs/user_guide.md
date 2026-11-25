# User Guide

Smart AutoSorter AI Pro organizes a folder of documents into a generated folder tree. You can drive it from the full-screen terminal interface (TUI) or from the command line, and every run can be undone.

The examples below use `smart-autosorter`. When running from a source checkout, prefix each command with `uv run`.

## First Run and AI Consent
<a id="first-run-steps--setup-wizard"></a>

On first launch the **Privacy & Data Setup Wizard** asks whether you allow the optional AI model download: a one-time, roughly 80 MB embedding model from Hugging Face.

```mermaid
flowchart TD
    A["Launch Application"]
    B{First-Run Setup Wizard}
    C["Download 80MB Model from Hugging Face"]
    D["Enable Semantic AI Sorting"]
    E["Fallback to Offline Non-Semantic Mode"]
    F["Open User Guide"]
    G["Enter Workspace Directory Path"]
    H["Select Quick Preset (Demo / Downloads / Documents)"]
    I{Validate Directory Path}
    J["Display Inline Error Banner"]
    K["Begin Directory Scan"]
    A --> B
    B -->|Accept & Download| C
    C -->|Download Successful| D
    C -->|Network Error or Offline| E
    B -->|Decline| E
    B -->|Help| F
    B -->|Enter Path| G
    B -->|Select Preset| H
    H -->|Populate Path| G
    G -->|Finish & Save| I
    I -->|Invalid Path| J
    J -->|Correct Path| G
    I -->|Valid Path| K
    click A "#first-run-steps--setup-wizard" "Launch Application Step"
    click B "#first-run-steps--setup-wizard" "First-Run Setup Wizard"
    click C "https://huggingface.co" "Hugging Face Model Download" _blank
    click D "#privacy-configurations" "Privacy & Semantic AI Settings"
    click E "#offline-non-semantic-mode" "Offline Mode Information"
    click F "#first-run-steps--setup-wizard" "User Guide Reference"
    click G "#first-run-steps--setup-wizard" "Workspace Directory Input Field (#input-dir)"
    click H "#first-run-steps--setup-wizard" "Quick Preset Buttons"
    click I "#first-run-steps--setup-wizard" "Directory Path Validation"
    click J "#first-run-steps--setup-wizard" "Inline Error Banner (#lbl-error)"
    click K "#first-run-steps--setup-wizard" "Scan and Organize Workspace"
```
Declining is fine. Sorting still works offline using TF-IDF keyword clustering (see [Offline Mode](#offline-mode)).

### From the command line
- Grant consent, persist your preference across restarts, verify or download the local ONNX model with terminal progress, and select a workspace in interactive terminals (or combine directly with `scan`, `sort`, `drop`, or `daemon`):
  ```bash
  smart-autosorter --accept-ai-consent
  smart-autosorter scan ~/Downloads --accept-ai-consent
  smart-autosorter sort ~/Downloads --accept-ai-consent
  ```
- Save AI consent without downloading model weights right away (for metered or offline networks), falling back cleanly to local keyword/TF-IDF clustering:
  ```bash
  smart-autosorter scan ~/Downloads --accept-ai-consent --defer-model-download
  ```
- Decline AI consent, persist that preference across restarts, and stay 100% offline:
  ```bash
  smart-autosorter --decline-ai-consent
  ```
- In scripts or headless CI environments, add `--non-interactive` so no workspace prompt or TUI dialog waits for input:
  ```bash
  smart-autosorter --accept-ai-consent --non-interactive
  ```
- Manage local model weights directly at any time with the `model` subcommand:
  ```bash
  smart-autosorter model status
  smart-autosorter model download --proxy http://proxy.example.com:8080
  smart-autosorter model verify
  smart-autosorter model remove
  ```

### In the terminal interface
The wizard opens automatically on first launch of `smart-autosorter`:
1. **AI Consent & Live Model Status**: Use `[Tab]` / `[Shift+Tab]` to focus the AI Consent switch (`#switch-consent`), then press `[Space]` or `[Enter]` to toggle consent. The wizard inspects your local model directory and ONNX Runtime availability on launch and shows whether semantic AI is **Verified** (`[AI: Active]`), **Downloading**, **Missing**, or in `[AI: Fallback]` when `smart-autosorter[ml]` (`onnxruntime`) is not installed (`#lbl-wizard-model-status`).
2. **Proxy & Deferred Download Options**: Optionally enter a corporate HTTP/HTTPS proxy URL (`#input-wizard-proxy`) or check **Defer model download (metered or offline network)** (`#chk-defer-download`) to skip background transfers and sort immediately using offline keyword/TF-IDF rules.
3. **Workspace Directory & Quick Presets**: Enter the target directory path (`#input-dir`), or choose a quick preset button:
   - **Demo Workspace**: creates `sandbox/demo_workspace` and automatically enables **Generate Sample Documents** (`#chk-sample-corpus`) so finishing setup renders a non-empty sorting plan immediately.
   - **Downloads**: your user `~/Downloads` directory.
   - **Documents**: your user `~/Documents` directory.
4. **Opt-In Starter Rule Templates & Live Rule Counter**: All six built-in starter rule template packs start unchecked on clean profiles so no rules are added unless you select them. Checking packs updates the pinned rule counter (`#lbl-wizard-rule-summary`) above **Finish & Save** in real time. Select **Try Interactive Sample Playground** (`#btn-open-playground`) to preview sample documents and test how starter packs change live classification predictions before saving.
5. **Validation and Error Handling**: If an invalid or non-existent path is entered, selecting **Finish & Save** (or pressing `[Enter]` inside `#input-dir`) displays an inline error banner (`#lbl-error`), announces the error message via screen reader, and restores focus directly to the input field so you can correct it.
6. **Finish & Save**: Highlight **Finish & Save** (or press `[Enter]` inside `#input-dir`). If you selected starter packs, a confirmation toast reports the saved keyword rule and policy counts. When AI consent is enabled and verified weights are absent (unless deferred), Sortify automatically starts a non-blocking background model download and scans the chosen workspace immediately using local keyword/TF-IDF clustering. Once SHA-256 verification completes (with `onnxruntime` installed), the status bar switches to `[AI: Active]` and automatically re-scans the workspace if the mock plan has no manual edits, locks, or running moves.

Reopen the wizard any time with `[Ctrl+W]`, or open the **Model Manager** directly with `[Ctrl+G]` (also available in the Command Palette via `[Ctrl+K]`).

### AI Status Bar Badges
The TUI status bar and `smart-autosorter model status` distinguish four runtime AI states:
- `[AI: Active]`: AI consent is enabled, cryptographically verified ONNX model weights exist on disk, and `onnxruntime` (`smart-autosorter[ml]`) is installed and healthy.
- `[AI: Downloading XX%]`: AI consent is enabled and local model weights are downloading in the background while keyword/TF-IDF clustering remains available.
- `[AI: Fallback]`: AI consent is enabled, but verified model weights are missing/deferred/degraded or `onnxruntime` (`smart-autosorter[ml]`) is not installed (sorting continues locally with keyword rules and TF-IDF vectors).
- `[AI: Disabled]`: AI consent is off and in-memory AI models are unloaded.

## Sorting a Folder

### In the terminal interface
1. Start the app with `smart-autosorter` (or `smart-autosorter --tui`).
2. Press `[Ctrl+B]` to choose a folder, then `[Ctrl+S]` to scan it. The proposed folder tree appears; nothing has moved yet.
3. Review the plan. `[Ctrl+F]` searches the tree (press `[Enter]`, `[Down]`, or `[Esc]` inside the search box to return focus to the tree while keeping the filter active, and press `[Esc]` again on the tree to clear the filter), `[Space]` checks items, `[Ctrl+L]` locks a file in place, `[F2]` renames a file or folder category node, `[Ctrl+N]` creates a folder, and `[Ctrl+R]` opens the active rule manager modal. Press `[R]` to reassign checked files or the active item to a target folder (if a text input field such as the search box is focused, defocus it first before pressing `[F2]`, `[Ctrl+R]`, or `[R]`; see [Keyboard Shortcuts](keyboard_shortcuts.md)). You can also press `[Ctrl+K]` to open the [Command Palette](#command-palette) for command discovery and to trigger all system actions without memorizing keybindings.
4. Press `[Ctrl+E]` to execute. You can review duplicates first, and an audit report is shown when the moves finish.

Press `[Ctrl+K]` for the command palette, `[Ctrl+G]` for the Model Manager, `[?]` or `[F1]` for the full list of shortcuts, and `[Ctrl+Q]` to quit. Every shortcut is also listed in [Keyboard Shortcuts](keyboard_shortcuts.md).

### From the command line
```bash
smart-autosorter --demo                      # seed sandbox/demo_workspace and run an undoable demo sort
smart-autosorter scan ~/Downloads            # print the proposed folder tree, move nothing
smart-autosorter scan ~/Downloads --json     # the same plan as JSON, for scripts
smart-autosorter sort ~/Downloads --dry-run  # verify the plan without moving
smart-autosorter sort ~/Downloads            # sort in place
smart-autosorter sort ~/Downloads --dest-dir ~/Sorted
```

Global flags (`--json`, `-q` / `--quiet`, `--no-color`, `--accept-ai-consent`, `--decline-ai-consent`, `--defer-model-download`, `--skip-wizard`, `--non-interactive`, `--tui` / `--interactive`, and `--settings-file` / `--profile`) work identically whether placed before or after any top-level or nested subcommand (`sort`, `scan`, `config`, `daemon`, `undo`, `drop`, `ledger`, `quarantine`, `model`, and `rules`).

Transient sort-override options for `sort`, `scan`, `drop`, and `daemon` (omitted and rejected by `config` and `undo` so `config` only displays persisted settings):
- `--max-folders N`: maximum folders per level (default 12, up to 50).
- `--strategy`: `default` (keyword clustering), `generative` (local LLM folder names, needs the `ml` extra), or the clinical `clinical_tmf` / `clinical_isf` strategies.
- `--conflict-policy skip|rename|overwrite`: what to do when a destination file already exists (`overwrite` moves the existing file to `.sortify_overwritten/<session_id>/` so `undo` restores it).
- `--duplicates off|review|skip|stage|trash` and `--detect-original-by`: byte-identical duplicate handling and survivor selection.
- `--contextual-renaming` / `--no-contextual-renaming`: toggle AI contextual file renaming for the run.
- `--export-report FILE --report-format html|json`: save an audit report of the run.
- `--preset demo|downloads|documents` (on `sort` and `scan`): use a standard folder instead of naming one (`demo` seeds `sandbox/demo_workspace` with sample files whenever that folder is missing or empty, without overwriting existing user files).

## Command Palette
<a id="command-palette"></a>

The Command Palette is a global searchable overlay providing quick access to every action and workflow in the terminal interface. Press `[Ctrl+K]` from anywhere in the application to summon the palette.

### Navigation and Search Filtering

When the palette opens, focus is placed in the command search input field (`#palette-input`):
- **Real-Time Search Filtering**: As you type, the palette filters commands in real time across command titles, categories, descriptions, and keyboard shortcuts.
- **Arrow Navigation**: Press the `[Up]` and `[Down]` arrow keys to navigate through the filtered list of commands (`#palette-list`) while retaining focus in the input field.
- **Selection**: Press `[Enter]` while in the input field or click a highlighted command in the list to execute the action immediately.
- **Cancellation**: Press `[Esc]` to dismiss the command palette overlay without executing an action.

### Workflows Without Dedicated Shortcuts

While primary actions (such as scanning, executing plans, or opening the **Model Manager** via `Open Model Manager` / `[Ctrl+G]`) have direct keybindings, five workflows do not have dedicated shortcuts and are executed directly through the command palette:

- **CRO Forensic Ingestion** (`Open CRO Forensic Ingestion`): Multi-study clinical compliance ingestion workflow to scan clinical study folders and organize documents into Trial Master File (TMF/ISF) folder structures.
- **Export Active Rules** (`Export Active Rules`): Open the rule export dialog to serialize active keyword rules and compliance policies to disk in Sortify JSON (`*.json`), Hazel (`*.hazelrules`), or DropIt (`*.ini`) formats.
- **Import External Rules** (`Import External Rules`): Open the rule migration dialog to import external rule sets and taxonomy definitions from Hazel, DropIt, or TagSpaces configurations.
- **Export Simulation Report** (`Export Simulation Report`): Export dry-run simulation audit reports to HTML or JSON formats for verification before executing file moves.
- **Starter Rule Templates** (`Browse Starter Rule Templates`): Open the starter rule template gallery to browse and apply curated rule packs (such as Developer Workspace or Media & Downloads).

## How Files Are Placed

Each file goes through these steps in order, and the first one that matches decides where it goes:
1. Locked paths you pinned in the TUI.
2. Compliance policies and the sensitivity classifier. Highly sensitive files go to a quarantine folder for review.
3. Keyword rules, imported rules, and policies (see [Rule Migration and Export](#rule-migration-and-export)), alongside rules learned from your past corrections.
4. Extension categories for files whose text isn't extracted (for example `Images`, `Archives`, `Executables`).
5. The folder a file with the same content went to before.
6. Content clustering for everything else.

<a id="ai-clustering-constraints"></a>
Clustering groups documents with similar wording and names each folder after its two strongest keywords (for example `Finance-Budget`). It recurses into large groups to make sub-folders, up to five levels deep. When fewer than three documents are left to cluster at the top level they go to `Miscellaneous`.

## Supported File Formats

Text is extracted from `.txt`, `.docx`, `.csv`, `.xlsx`, `.xls` and `.pdf`. With the optional `ml` extra installed, images (`.png`, `.jpg`, `.jpeg`) are read with OCR and audio (`.mp3`, `.wav`, `.m4a`) is transcribed with Whisper (which needs `ffmpeg` and `whisper` on your `PATH`). Every other file type is still sorted, into an extension category folder.

## Undoing a Run

Every run is recorded as a session that can be rolled back:
```bash
smart-autosorter undo --list           # show recent sessions
smart-autosorter undo --latest         # roll back the most recent run
smart-autosorter undo --session-id ID  # roll back a specific run
```
In the TUI, `[Ctrl+Y]` opens the history view.

## Settings

View settings with `smart-autosorter config --show`, and change one with `config --set KEY VALUE`. Settings are stored in `~/.autosorter/settings.json`; command-line flags override them for a single run. In the TUI, `[Ctrl+O]` opens the settings dialog.

### Exclusion list (stop words)
Stop words are common terms (e.g. 'the', 'and', 'for') ignored when analyzing document text. They don't filter file types. List and set settings accept either a comma-separated string or a JSON array:
```bash
smart-autosorter config --set STOP_WORDS "word1,word2,word3"
smart-autosorter config --set STOP_WORDS '["word1", "word2", "word3"]'
```

### Cleaning up empty folders
When enabled, folders left empty after sorting are removed.
```bash
smart-autosorter config --set CLEANUP_EMPTY_FOLDERS true
```

### AI consent
```bash
smart-autosorter config --set AI_CONSENT_GRANTED true
smart-autosorter config --set AI_CONSENT_GRANTED null
```

## Rule Migration and Export
<a id="rule-migration-and-export"></a>

Sortify allows you to import existing rules from competitor automation tools—such as macOS Hazel, DropIt for Windows, and TagSpaces—as well as export active rules for backup or cross-tool compatibility.

### Accessing the Template Gallery and Rule Migration Tools

In the terminal interface (TUI):
1. Press `[Ctrl+O]` to open the **Application Settings** dialog.
2. Select the **AI & Privacy Settings** tab.
3. Choose **Starter Rule Template Gallery** (`#btn-open-templates`) to view built-in template packs (such as Developer Workspace and Media & Downloads).
4. Use the bottom action buttons to manage external rules:
   - **Import External Rules** (`#btn-import-rules`): opens the rule import dialog to migrate rules from Hazel, DropIt, or TagSpaces.
   - **Export Rules** (`#btn-export-rules`): opens the rule export dialog to save active keyword rules and policies to disk.
   - **Apply Selected Templates** (`#btn-apply-templates`): merges checked starter template packs into your configuration.
   - **Close** (`#btn-close-gallery`) or `[Esc]`: returns to the settings dialog.

### Active Rule Manager
<a id="active-rule-manager"></a>

Sortify provides a dedicated **Active Rule Manager** modal to inspect, add, edit, toggle, and delete active keyword rules and compliance policies.

#### Launching the Rule Manager
You can open the active rule manager using either of the following methods:
- **Direct Shortcut**: Press `[Ctrl+R]` from the main plan tree review screen.
- **Settings Dialog**: Press `[Ctrl+O]` to open Settings, switch to the **AI & Privacy Settings** tab, and select **Active Rule Manager** (`#btn-open-rules`).
- **Command Palette**: Press `[Ctrl+K]` and select **Open Active Rule Manager**.

*Note on focus*: When focus is inside a text input field (such as the tree search box or modal input fields), `[Ctrl+R]` and `[F2]` do not trigger modal actions. Defocus the text input or press `[Esc]` before using these shortcuts.

#### Managing Rules and Controls
The active rule manager displays all configured rules in the interactive **Rules Table** (`#rules-table`) with columns for `Status` (Active, Disabled, or `⚠ Masked by '<pattern>'` when a higher-priority policy shadows a lower-priority policy), `Type` (e.g. `keyword`, `compliance`, `pattern`, `size`, `age`, including compound badges such as `[all]`, `[any]`, `[none]`, `[not]`, `[regex]`, and `[case]`), `Pattern`, `Target Folder`, and `Priority`.

Use the following shortcuts and button controls:
- **Toggle Rule Status** (`[Space]` or `#btn-toggle-rule`): Toggles the selected rule between *Active* and *Disabled*. Toggling a keyword rule moves it between `KEYWORD_RULES` and `DISABLED_KEYWORD_RULES`, and toggling a learned rule moves it between `LEARNED_RULES` and `DISABLED_LEARNED_RULES` while preserving separator-normalized stem matching (`_` and `-`) and row cursor focus on the table.
- **Add Rule** (`#btn-add-rule`): Opens the rule editor modal (`RuleEditModal`) to define either a keyword rule or a prioritized policy (`type`, `expression`, `target_path`, `rename_pattern`, `priority`, `halting`, and optional `action`), including compound boolean conditions. Choose **Match Mode** (`Simple Keyword / Pattern`, `All conditions (AND)`, `Any condition (OR)`, or `None of conditions (NOR)`), configure optional **Negate**, **Regex** (bounded to 256 characters and validated against ReDoS nested quantifiers), and **Case-sensitive** flags, and add or remove multi-clause conditions (`name`, `extension`, `content`, `path`, or `any_text` with `contains`, `equals`, `starts_with`, `ends_with`, `glob`, or `regex` operators). Pick a workspace file in `#select-sample-file` to live-test your draft rule locally without outbound network calls or moving files, and view total matching file counts across the workspace. Target paths, rename templates, and policy actions are validated before saving.
- **Edit Rule** (`[Enter]` or `#btn-edit-rule`): Opens the rule editor modal for the highlighted keyword rule or compound policy, allowing you to update its rule category, policy attributes, match mode, boolean condition clauses, flags, summary pattern expression, rename template, or target folder while live-previewing sample matches.
- **Delete Rule** (`[Delete]` or `#btn-delete-rule`): Prompts a confirmation dialog (`DeleteRuleConfirmModal`) before permanently deleting the selected rule or policy from settings.
- **Import Rules** (`[Ctrl+I]` / `[I]` or `#btn-import-rules`): Opens the external rule import dialog (`RuleImportModal`) to import rules from Hazel, DropIt, TagSpaces, or Sortify JSON. Completing the import workflow updates the rules table immediately without closing the active rule manager.
- **Export Rules** (`[Ctrl+E]` / `[E]` or `#btn-export-rules`): Opens the export dialog (`ExportRulesModal`) displaying active rule counts and exports active rules (including compound boolean conditions, exclusions, metadata policies, and rename templates) to Sortify JSON, Hazel, or DropIt formats.
- **Browse Templates** (`[Ctrl+B]` / `[B]` or `#btn-browse-templates`): Opens the template gallery modal (`TemplateGalleryModal`) to browse and apply pre-configured starter packs. When templates are applied, the rules table refreshes immediately.
- **Close Dialog** (`[Esc]` or `#btn-close-rules`): Closes the active rule manager and returns to the previous view. When any rule was added, edited, toggled, imported, or deleted during the session, Sortify automatically rescans the active workspace directory.

#### Inspecting Plan Routing and Document Previews
- **Plan Tree Routing Badges & Inspector**: Every file leaf in `#plan-tree` displays a compact routing badge (`[POLICY]`, `[KEYWORD]`, `[LEARNED]`, `[JEV]`, `[HIST]`, `[SIM]`, `[FOLDER]`, `[EXT]`, `[CLUSTER]`, or `[FALLBACK]`). Selecting a file updates the inspector panel with the winning rule expression, policy priority, or top TF-IDF cluster keywords.
- **Document Text & Density Preview** (`[Ctrl+X]`): Opens `ExtractionPreviewModal` for any supported document format (both PDF and non-PDF files) to inspect sanitized extracted text (with secret tokens and home paths scrubbed) alongside per-page PDF character density metrics.

### Importing External Rules

The **Import External Rules** modal lets you import rules from third-party tools into Sortify:

#### Supported Importers
- **Hazel (`.hazelrules`, `.plist`)**: Parses Apple property list rule packages (`HazelRuleImporter`), preserving `all` (`AND`), `any` (`OR`), and `none` (`NOR`) condition modes, nested condition groups (up to 2 nesting levels and 32 clauses), negated operators (`does not contain`, `is not`, `does not match`), bounded regular expressions, case-sensitivity flags, single-condition metadata policies (`Size`, `Age`, `DateModified`, `DateCreated`, `EXIFDate`), `RenamePattern` templates, move actions, and archive extraction actions (`NSRuleActionUnarchive` / `Unarchive`) on `PolicyRule` entries.
- **DropIt (`.ini`, `.xml`, `.json`, `.dropit`)**: Parses DropIt INI, XML, and JSON association packages (`DropItRuleImporter`), translating move and archive extraction (`Extract` / `Unarchive`) rules, `/` exclusion masks (such as `*.jpg/sea*`), `#` / `!` / `-` exclusion tokens, XML/JSON `<Exclude>` filters, regular expression associations, and case-sensitivity flags into keyword rules and compound `PolicyRule` entries.
- **TagSpaces (`taggroup.json`, `taggroups.json`)**: Parses TagSpaces tag taxonomy and smart rule exports (`TagSpacesRuleImporter`), converting tag groupings into keyword rules and multi-clause `AND` smart rules (including negated operators) into single compound `PolicyRule` entries.
- **Sortify JSON (`*.json`)**: Imports native Sortify JSON rule backups (`SortifyRuleImporter`, v1 and v2) with lossless fidelity across `KEYWORD_RULES`, `DISABLED_KEYWORD_RULES`, `LEARNED_RULES`, `DISABLED_LEARNED_RULES`, `EXTENSION_CATEGORIES`, `POLICIES` (active and disabled, including `match_mode`, `conditions`, `negate`, `regex`, `case_sensitive`, metadata policy types, and `rename_pattern`), and per-folder `FOLDER_RULE_SCOPES`.

#### Import Workflow
1. Select **Browse...** (`#btn-browse-rule-file`) to choose a rule file via your OS file dialog, or type the path in `#input-rule-file-path` (supports `.hazelrules`, `.plist`, `.ini`, `.xml`, `.json`, and `.dropit`).
2. Select **Parse File** (`#btn-parse-rule-file`) or press `[Enter]` (selecting a file via **Browse...** parses it automatically).
3. Keep **Import new rules disabled for review** (`#chk-import-disabled`) checked (the default for external Hazel, DropIt, and TagSpaces packages) to stage newly imported rules as disabled until you inspect and enable them. Native **Sortify JSON** backups automatically preserve each rule's original active and disabled states unless you explicitly check `#chk-import-disabled` or pass `--disabled` / `--enable`.
4. Inspect the parsed preview:
   - **Imported Keywords**: Displays extracted keyword mappings and normalized relative destination folders.
   - **Imported Policies**: Displays rule types (including `[unarchive]`, `[all]`, `[any]`, `[none]`, `[not]`, `[regex]`, and `[case]` badges), readable condition summaries or wildcard/regex/metadata expressions, target paths, and rule priorities.
   - **Warnings Banner**: Displays converted destination paths (`'original' -> 'relative'`), skipped unsafe paths, and fail-closed rules containing unsupported filters.
5. Resolve any detected conflicts (see [Conflict Resolution](#conflict-resolution) below).
6. Select **Merge Rules into Settings** (`#btn-merge-imported-rules`) to commit the imported rules.

#### Path Normalization, Unarchive Mapping, and Fail-Closed Filter Guards
- **Destination Path Normalization**: Exported absolute destination paths (POSIX `/Users/...`, Windows `C:\...`, UNC shares `\\server\share\...`, `~/...`, `%ENV%\...`, and `file://` URIs) are converted into safe relative folder paths and listed with a preview warning. Rules whose destination cannot become a safe relative folder (such as `/`, `C:\`, traversal segments `..`, illegal characters, or OS-reserved names) are skipped before merge with a warning.
- **Archive Extraction Mapping**: External extraction rules (`Unarchive`, `NSRuleActionUnarchive`, `Extract`, `Unpack`, `Decompress`) map to Sortify's built-in `unarchive` policy action when destination folders are valid.
- **Compound Exclusions and Negations**: DropIt `/` exclusion separators (`*.jpg/sea*`), `<Exclude>` tags, `#` / `!` / `-` exclusion masks, Hazel `none` groups and negated operators (`does not contain`, `is not`), and TagSpaces negated smart-rule clauses convert directly into compound negated `PolicyCondition` clauses instead of splitting rules into `OR` catch-alls.
- **Fail-Closed Handling of Unsupported Filters**: Rules that rely on unsupported DropIt/TagSpaces numeric file-size or date filters, multi-condition/nested Hazel metadata combinations, OS extended attributes, or unrecognized condition operators fail closed: the importer skips the entire rule and appends a descriptive warning to `ImportResult` rather than stripping the narrowing filter and emitting a broad catch-all rule. Script executions and `Copy`-only actions are likewise skipped with warnings.
- **Syntax Errors and Malformed Files**: If a file cannot be parsed or contains syntax errors (including unsafe or >256-character regular expressions), the importer surfaces non-blocking warnings without corrupting existing settings.
- **TagSpaces Asymmetry**: TagSpaces taxonomy and smart rule imports generate keyword rules and compound policies, while exporting back to native TagSpaces taxonomy files is not supported (see [Exporting Active Rules](#exporting-active-rules)).

#### Headless CLI Rule Management, Folder Scopes, and Profiles
You can inspect, add, remove, enable, disable, import, and export rules from scripts or CI—globally, scoped to a folder (`--folder`), or inside a named profile (`--profile` / `--settings-file` across all CLI commands, sharing `store.db` undo history and ONNX models in `~/.autosorter/`):
```bash
# List, add, disable, re-enable, or remove rules (supports --folder, --profile, --settings-file, and --json)
smart-autosorter rules list --folder ~/Downloads --json
smart-autosorter rules add invoice Finances/Invoices --folder ~/Downloads
smart-autosorter rules disable "q1 tax report" --kind learned --folder ~/Downloads
smart-autosorter rules enable "q1 tax report" --kind learned --folder ~/Downloads
smart-autosorter rules remove invoice --folder ~/Downloads

# Run sorting or scanning with a named profile (~/.autosorter/profiles/work.json)
smart-autosorter sort ~/Downloads --profile work --dry-run

# Preview converted paths, skipped clauses, and conflicts without modifying settings
smart-autosorter rules import ~/Exports/profile.ini --dry-run

# Import external rules disabled for review (default) with conflict resolution overrides
smart-autosorter rules import ~/Exports/rules.hazelrules --conflict-strategy keep_existing --resolve invoice=overwrite

# Import new rules enabled immediately and emit structured JSON output
smart-autosorter rules import ~/Exports/rules.hazelrules --enable --json

# Restore a lossless Sortify JSON v2 backup (preserves active, disabled, learned, extension, compound, and folder-scoped rules)
smart-autosorter rules import ./backup_rules.json

# Export rules to Sortify JSON v2, Hazel (.hazelrules), or DropIt (.ini)
smart-autosorter rules export ./backup_rules.json
smart-autosorter rules export ./dropit_rules.ini --format dropit_ini --folder ~/Downloads
```

### Conflict Resolution
<a id="conflict-resolution"></a>

When an imported rule targets a keyword or pattern that already exists in your settings with a different target path, Sortify flags it under **Detected Rule Conflicts**:

- **Bulk Resolution Strategy**: Use the dropdown selector to apply a uniform policy across all conflicts:
  - **Keep Existing (Skip Conflicts)** (`keep_existing`): Retains your existing settings rule and ignores the incoming conflicting rule.
  - **Overwrite Existing Rules** (`overwrite`): Replaces your existing destination path with the imported rule's destination path.
  - **Skip Conflicting Rules** (`skip_conflicting`): Omits conflicting rules while merging non-conflicting rules.
- **Interactive Per-Rule Override**: In the **Rule Conflicts** table (`#dt-rule-conflicts`), click or press `[Space]` or `[Enter]` on any row to cycle that rule's resolution individually between *Keep Existing*, *Overwrite*, and *Skip Conflicting*.

### Exporting Active Rules
<a id="exporting-active-rules"></a>

The **Export Active Rules** modal serializes your active keyword rules and compliance policies to disk for backups or migration into other workflow tools.

#### Supported Export Formats
- **Sortify JSON (`*.json`)**: Native Sortify JSON format (`format: "sortify"`, version 2) preserving full rule structure across `KEYWORD_RULES`, `DISABLED_KEYWORD_RULES`, `LEARNED_RULES`, `DISABLED_LEARNED_RULES`, `EXTENSION_CATEGORIES`, `POLICIES` (active and disabled, including compound boolean conditions, metadata expressions, and rename templates), and per-folder `FOLDER_RULE_SCOPES`.
- **Hazel Rules (`*.hazelrules`)**: Apple property list format mapping active keyword rules and policies to Hazel-compatible move (`NSRuleActionMove`), unarchive (`NSRuleActionUnarchive`), and trash (`NSRuleActionTrash`) rules.
- **DropIt Configuration (`*.ini`)**: Standard DropIt configuration file with `[DropIt]` header and `[Association_N]` sections (mapping trash/duplicate disposal policies to `Delete` and omitting disabled policies).

*Note: While TagSpaces tag groups can be imported into keyword rules, exporting back to native TagSpaces taxonomy files is not supported.*

#### Export Workflow
1. Select your target format from the **Export Format** dropdown (`sortify_json`, `hazel`, or `dropit_ini`). The destination filename automatically updates its extension (`.json`, `.hazelrules`, or `.ini`).
2. Enter the target file path in the **Destination File Path** field (`#input-export-path`).
3. Select **Export Rules** (`#btn-do-export`) or press `[Enter]`.

#### Path Validation and Safety Guards
Destination paths are validated before writing:
- Directory traversal segments (`..`) and null characters (`\0`) are rejected.
- Destination paths pointing to existing directory paths rather than file paths are blocked.
- Parent directories are automatically created if they do not yet exist, provided write permissions are available.

## Background Folder Monitoring

The daemon watches a folder and sorts new files as they arrive:
```bash
smart-autosorter daemon start ~/Downloads
smart-autosorter daemon status
smart-autosorter daemon stop
```
`pause`, `resume`, `health` and `metrics` are also available.

```mermaid
stateDiagram-v2
    [*] --> Idle: Watchdog Active
    Idle --> EventDetected: Directory Change Event
    EventDetected --> CheckTransient: Inspect File Extension
    CheckTransient --> TransientIgnored: Extension in (.crdownload, .tmp, .download)
    TransientIgnored --> Idle: Drop Transient Event
    CheckTransient --> StartDebounce: Valid File Extension
    StartDebounce --> AggregatingEvents: Standard Debounce Timer (0.6s)
    AggregatingEvents --> AggregatingEvents: New Event Received (Reset 0.6s Timer)
    AggregatingEvents --> TriggerSorting: Debounce Timer Expires (0.6s)
    AggregatingEvents --> TriggerSorting: Max Debounce Limit Reached (5.0s)
    TriggerSorting --> Idle: Execute Sorting Pipeline
```
- **Incomplete downloads are ignored.** Files ending in `.crdownload`, `.tmp` or `.download` are skipped until they are finished.
- **Bursts are batched.** After a file event the daemon waits 0.6 seconds (`DEBOUNCE_DELAY`) for more changes, and never more than 5 seconds (`MAX_DEBOUNCE_DELAY`) during a continuous stream of writes.

## Offline Mode
<a id="offline-non-semantic-mode"></a>

If you decline the model download, are offline during setup, or the model is missing, the app runs without the embedding model:
- Documents are still clustered by content, using TF-IDF keyword vectors instead of sentence embeddings. Groupings can be a little less precise for documents that use different words for the same topic.
- No network connections are made for the model.

## Privacy
<a id="privacy-configurations"></a>

All document processing happens on your machine, and local databases are encrypted. The app's only network traffic is the optional model download and a security-advisory check for that model. See the Privacy Policy (`PRIVACY.md` in the repository) for details.
