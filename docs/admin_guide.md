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

