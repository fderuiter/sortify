# User Guides

Welcome to the User Guides for Smart AutoSorter AI Pro.

## First-Run Steps & Setup Wizard

When you launch Smart AutoSorter AI Pro for the first time, the **Setup Wizard** automatically runs dynamic system checks (<500ms) to scan for local PyTorch dependencies and pre-installed model weight bundles.

### Setup Scenarios

1. **Air-Gapped Enterprise Deployment (Pre-Installed Bundles):**
   - If pre-packaged model weights are detected in system paths (e.g. `offline_bundle/model/`, `~/.smart-autosorter/offline_bundle/model/`, or `~/.autosorter/model/`), the wizard confirms that **Air-Gapped Local AI** is available.
   - Smart AutoSorter automatically configures local semantic AI categorization.
   - All AI categorization and text analysis run 100% locally on your machine with **zero network calls** generated.

2. **Offline Setup Without Local Bundles:**
   - If no pre-installed offline model bundle is detected, the setup wizard defaults to non-semantic **extension-based sorting**.
   - No internet connection or model download is required.
   - If internet connectivity is available, you may optionally click **Accept & Download** to fetch model weights from Hugging Face.

## Air-Gapped Local AI & Offline Bundle Directory Locations

Smart AutoSorter AI Pro supports complete offline, air-gapped semantic AI sorting when model weights are placed in any of the following local directory paths:

- **Local Workspace Bundle Path:** `offline_bundle/model/` (relative to application execution root)
- **User Home Bundle Path:** `~/.smart-autosorter/offline_bundle/model/`
- **Application Configuration Path:** `~/.autosorter/model/`
- **Custom Environment Variable Path:** Path specified by `MODEL_PATH` environment variable.

When model weights are placed in any of these locations, the application detects them on launch and enables local AI features without requiring internet access.

## Privacy Configurations

Your privacy is our priority.
- **Air-Gapped Local Processing:** All semantic vector calculations, TF-IDF analysis, and OCR occur strictly on your local machine.
- **No External Communication:** When offline model bundles are active or network sandboxing is enforced, zero external network calls are made. 
- **Privacy & AI Settings:** You can verify local AI status and model directory paths anytime in the Settings panel.

## Exclusion List Configuration

The Settings panel allows you to manage an **Exclusion List** (stop words). Words added to this list will be ignored by the AI sorting engine (e.g., 'the', 'and', file extensions). 

- Type a word in the text box and press Enter to add it.
- Click the '×' button next to a word to remove it.

## Folder Cleanup Options

To keep your output directory organized, you can enable **Cleanup Empty Folders** in the Settings panel under *File Operations*. When enabled, the application will automatically remove any folders left empty after the sorting or clustering processes are completed.

## Offline Non-Semantic Mode

This application includes a dedicated offline non-semantic sorting fallback mode. This mode is activated automatically if you decline the model download, if you are completely offline during setup, or if the model is otherwise missing.

### How Offline Mode Works
In offline non-semantic mode, the AI clustering features are disabled. Instead, the application processes folders by grouping files based purely on file extensions or basic alphabetical sorting rules, without analyzing the internal text or semantic meaning. 
- Files are grouped into generic category folders (e.g., all `.txt` files into a Text Documents folder).
- No background network connections are attempted.
- Performance is extremely fast as no heavy machine learning computations occur.

## System Limits

The following rules and constraints govern how the AI sorts your files:

### Supported File Formats
The system currently supports the following file formats for sorting:
- `.txt`
- `.docx`
- `.csv`
- `.xlsx`
- `.xls`
- `.pdf`

### AI Clustering Constraints
To ensure optimal performance and categorization:
- A minimum of **3 supported files** is required to enable AI clustering.
- The system will generate a maximum of **12 folders** (subdirectories).

### Miscellaneous Folder
The **Miscellaneous** folder acts as a fallback for files that the AI cannot confidently categorize. Files are placed here if they have:
- Insufficient text content.
- Low semantic scores.
- Unreadable data.
