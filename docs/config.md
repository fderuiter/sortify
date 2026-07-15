# Configuration Reference Manual

## Overview
This document provides a comprehensive reference for configuring the application. It details operational limits, path parameters, processing settings, and precedence rules to help technical users customize their sorting environment safely.

## Operational Limits
The following settings control the core operational capacity and features of the system:

- **MAX_FOLDERS:** 12 (Maximum number of destination folders allowed)
- **MAX_WORKERS:** 15 (Number of active worker threads for parallel processing)
- **MAX_DEPTH:** 5 (Maximum directory sorting depth limit)
- **MAX_FEATURES:** 3 (Limit on the number of concurrent features)
- **CLEANUP_EMPTY_FOLDERS:** True (Flag to remove folders that become empty after sorting)
- **CONTEXTUAL_RENAMING:** False (Enable or disable contextual renaming of items)
- **PRESERVE_HIERARCHY:** False (Whether to maintain the existing directory structure)
- **KEYWORD_RULES:** Empty (Custom keyword mapping rules)
- **AI_CONSENT_GRANTED:** Null (Tracks whether user consent has been granted for AI features)

## Path Parameters and Processing Settings

### Log File Location
System operational logs are stored in the user's home directory:
**Path:** `~/.autosorter/autosorter.log`

### System Stop Words
The system excludes the following active stop words during processing to prevent noise in sorting operations:
`the`, `and`, `for`, `this`, `that`, `with`, `from`, `inc`, `com`, `pdf`, `docx`, `txt`, `csv`, `xlsx`, `xls`, `site`, `team`, `page`, `nan`, `unnamed`, `your`, `have`, `will`, `are`, `not`, `can`, `all`, `was`, `has`, `but`, `what`, `there`, `out`, `about`, `get`, `would`, `like`, `which`, `their`, `when`, `who`, `some`, `how`, `these`, `into`, `other`, `could`, `than`, `only`, `also`, `over`, `well`, `because`, `through`, `don`, `should`, `been`, `much`, `where`

## Precedence Rules
The application evaluates configuration parameters using a strict precedence hierarchy to determine how settings interact. The priority is applied as follows, from highest to lowest:

1. **Local Settings File (`~/.autosorter/settings.json`):** This local configuration file takes absolute priority. Any parameters defined here will override environment variables and default properties.
2. **Environment Variables (or `.env` file):** Variables configured in the environment take precedence over default parameters.
3. **Default Parameters:** Base defaults are used as fallbacks if a setting is not explicitly defined in the local file or environment.

## Dynamic Configuration Saves
System settings modified during runtime are dynamically saved to the local JSON configuration file (`~/.autosorter/settings.json`) located in the user's home directory. To ensure stability and prevent excessive disk writes, these dynamic changes are saved with a short debounced delay of 0.5 seconds.

