# Administrator Guide

This document is automatically generated. Do not edit manually.

## Configuration Parameters

The following parameters are extracted directly from the application's configuration schema (`app.config.Settings`).

### `CONTEXTUAL_RENAMING`
- **Default**: `False`
- **Required**: `False`

### `AI_ASSISTED_NAMING`
- **Default**: `False`
- **Required**: `False`

### `PRESERVE_HIERARCHY`
- **Default**: `False`
- **Required**: `False`

### `MAX_FOLDERS`
- **Default**: `12`
- **Required**: `False`

### `MAX_WORKERS`
- **Default**: `4`
- **Required**: `False`

### `MAX_DEPTH`
- **Default**: `5`
- **Required**: `False`

### `MAX_FEATURES`
- **Default**: `3`
- **Required**: `False`

### `CLEANUP_EMPTY_FOLDERS`
- **Default**: `True`
- **Required**: `False`

### `EXPLORER_INTEGRATION`
- **Default**: `False`
- **Required**: `False`

### `KEYWORD_RULES`
- **Default**: `PydanticUndefined`
- **Required**: `False`

### `LEARNED_RULES`
- **Default**: `PydanticUndefined`
- **Required**: `False`

### `POLICIES`
- **Default**: `PydanticUndefined`
- **Required**: `False`

### `VISUAL_TIMEOUT`
- **Default**: `30`
- **Required**: `False`

### `IMAGE_MAX_DIMENSION`
- **Default**: `1000`
- **Required**: `False`

### `IMAGE_SKIP_THRESHOLD`
- **Default**: `3000`
- **Required**: `False`

### `MODEL_THREADS`
- **Default**: `2`
- **Required**: `False`

### `PROTECTED_PATHS`
- **Default**: `PydanticUndefined`
- **Required**: `False`

### `PROXY`
- **Default**: ``
- **Required**: `False`

### `OCR_GPU_ENABLED`
- **Default**: `False`
- **Required**: `False`

### `AUDIO_GPU_ENABLED`
- **Default**: `False`
- **Required**: `False`

### `OCR_LANGUAGES`
- **Default**: `en`
- **Required**: `False`

### `CONFLICT_POLICY`
- **Default**: `rename`
- **Required**: `False`

### `SORTING_STRATEGY`
- **Default**: `default`
- **Required**: `False`

### `CLINICAL_SMART_RENAMING`
- **Default**: `False`
- **Required**: `False`

### `CLINICAL_GENERATE_AUDIT_REPORT`
- **Default**: `True`
- **Required**: `False`

### `COHERENCE_THRESHOLD`
- **Default**: `0.5`
- **Required**: `False`

### `DEBOUNCE_DELAY`
- **Default**: `0.6`
- **Required**: `False`

### `MAX_DEBOUNCE_DELAY`
- **Default**: `5.0`
- **Required**: `False`

### `TABULAR_MAX_SHEETS`
- **Default**: `10`
- **Required**: `False`

### `TABULAR_MAX_ROWS`
- **Default**: `10000`
- **Required**: `False`

### `TABULAR_MAX_CHARACTERS`
- **Default**: `50000`
- **Required**: `False`

### `IGNORED_EXTENSIONS`
- **Default**: `['.crdownload', '.tmp', '.download']`
- **Required**: `False`

### `AI_CONSENT_GRANTED`
- **Default**: `None`
- **Required**: `False`

### `LOG_FILE`
- **Default**: `~/.autosorter/autosorter.log`
- **Required**: `False`

### `STOP_WORDS`
- **Default**: `['about', 'all', 'also', 'and', 'are', 'because', 'been', 'but', 'can', 'com', 'could', 'csv', 'docx', 'don', 'for', 'from', 'get', 'has', 'have', 'how', 'inc', 'into', 'like', 'much', 'nan', 'not', 'only', 'other', 'out', 'over', 'page', 'pdf', 'should', 'site', 'some', 'team', 'than', 'that', 'the', 'their', 'there', 'these', 'this', 'through', 'txt', 'unnamed', 'was', 'well', 'what', 'when', 'where', 'which', 'who', 'will', 'with', 'would', 'xls', 'xlsx', 'your']`
- **Required**: `False`

## Precedence Rules

The application evaluates configuration parameters using a strict precedence hierarchy to determine how settings interact. The priority is applied as follows, from highest to lowest:

1. **Local Settings File (`~/.autosorter/settings.json`):** This local configuration file takes absolute priority. Any parameters defined here will override environment variables and default properties.
2. **Environment Variables (or `.env` file):** Variables configured in the environment take precedence over default parameters.
3. **Default Parameters:** Base defaults are used as fallbacks if a setting is not explicitly defined in the local file or environment.

## Dynamic Configuration Saves

System settings modified during runtime are dynamically saved to the local JSON configuration file (`~/.autosorter/settings.json`) located in the user's home directory. To ensure stability and prevent excessive disk writes, these dynamic changes are saved with a short debounced delay of 0.5 seconds.

## Compliance Policies & Routing Rules

### Rule Syntax & Types

Compliance policies categorize and sort documents based on three rule types:

- **Keyword Rules**: Search for files containing a specific word or phrase anywhere in their text contents (for example, 'invoice' or 'billing').
- **Pattern Rules**: Match files using structured formatting or text sequences (such as a standard format of letters followed by numbers) to match specific document types.
- **Override Rules**: Check for exact text matches, taking precedent to bypass standard classification rules.

### Sequential Execution & Priority

Rules are evaluated sequentially, starting from the highest priority value down to the lowest. Because rules are checked in priority order, if a higher-priority rule matches, it will be executed first. This can sometimes result in 'shadowing', where a lower-priority rule never runs because a higher-priority rule has already matched the same conditions. To resolve overlaps, adjust rule priority numbers or make matching conditions more specific.

### Halting Parameters

Each policy includes a 'Halt on mismatch' setting. When active, if a document fails to meet this rule's criteria, the system will immediately stop evaluating any remaining lower-priority rules. This halting behavior is crucial for enforcing strict sequential checks and ensuring that files do not proceed to general classification or AI-based sorting if they fail compliance conditions.

### Path Validation Rules

To ensure system security, stability, and compatibility across operating systems, all target paths must comply with the following strict validation rules:

- **No Absolute Paths**: All target paths must be relative paths and cannot start with leading slashes (such as `/` or `\`).
- **No Directory Traversal**: Paths are blocked from using directory traversal segments (such as `..`) to prevent files from being moved outside of the designated folders.
- **No Illegal Characters**: Target paths must not contain any prohibited characters, including `<`, `>`, `:`, `"`, `|`, `?`, or `*`.

### Configuration Recovery & Troubleshooting

If the system detects invalid fields or syntax errors in the configuration file (`settings.json`), automatic saves are locked. This safeguard prevents overwriting and potentially corrupting your existing settings. While saves are suspended, the application will use temporary default values to prevent crashes.

To resolve a blocked-save state, follow these recovery options:

1. **Check Warning Banners**: Review the detailed list of validation errors displayed under the warning banner in the application settings dialog.
2. **Manually Edit Settings**: Open the local configuration file (`~/.autosorter/settings.json`) and correct the invalid values or formats.
3. **Reset Configuration**: Delete the invalid `settings.json` file or click the reset button to restore default, valid configuration values, which will immediately re-enable automatic saving.

## Maintenance Scripts and CLI Commands

### `sandbox_cli.py`
CLI tool for testing ML extraction and analysis in an isolated sandbox environment.

#### Usage
```text
usage: sandbox_cli.py [-h] {reset,extract,analyze} ...

Sandbox CLI Tool for ML Accuracy Verification

positional arguments:
  {reset,extract,analyze}
                        Available commands
    reset               Reset the sandbox dataset to its golden state
    extract             Extract text from a specific sandbox file
    analyze             Run the analysis pipeline on all sandbox files

options:
  -h, --help            show this help message and exit
```

### `scripts/prepare_offline.py`
Utility script to prepare an offline deployment bundle.

### `scripts/install_offline.py`
Offline installation and verification script.

