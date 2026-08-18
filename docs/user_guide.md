# User Guides

Welcome to the User Guides for Smart AutoSorter AI Pro.

## First-Run Steps & Setup Wizard

When you launch Smart AutoSorter AI Pro for the first time, you will be presented with the **Privacy & Data Setup Wizard**. The AI features require a small, one-time 80MB model download from Hugging Face.

1. **Accept & Download:** Click this to download the 80MB model and enable Smart AutoSorter's semantic sorting. This connects to Hugging Face only once.
2. **Decline (Offline Mode):** Skip the download and run the application entirely offline in flat non-semantic sorting mode.
3. **Help:** View this user guide to learn more about the implications of your choice.

## Privacy Configurations

Your privacy is our priority.
- **Local Processing:** If you download the model, all semantic analysis occurs strictly on your machine.
- **No External Communication:** We never send your files or personal data to any external server. 
- **Privacy Settings:** You can always verify if the model is downloaded and change your preferences in the Settings panel.

## Exclusion List Configuration

The Settings panel allows you to manage an **Exclusion List** (stop words). Words added to this list are common text terms (e.g., 'the', 'and', 'for') that are ignored during text normalization and AI semantic analysis.

It is important to distinguish stop word text filtering from file extension or filesystem rules:
- **Text Filtering:** Exclusion list terms remove frequent or generic words from document content before text analysis and clustering take place.
- **File Extensions:** Stop words do not filter file extensions (e.g., `.pdf`, `.docx`) or exclude specific file types from being processed by the application.

To manage your exclusion list:
- Type a word in the text box and press Enter to add it.
- Click the '×' button next to a word to remove it.

## Folder Cleanup Options

To keep your output directory organized, you can enable **Cleanup Empty Folders** in the Settings panel under *File Operations*. When enabled, the application will automatically remove any folders left empty after the sorting or clustering processes are completed.

## Background Folder Monitoring

Smart AutoSorter AI Pro provides continuous background directory monitoring to track target folders for real-time file additions and modifications.

### Continuous Watchdog Monitoring
When background folder monitoring is active, the system continuously tracks target directories for file events. When new files are added or existing files are modified, background monitoring automatically triggers sorting and reorganization without requiring manual intervention.

### Transient File Filtering
To ensure incomplete downloads or temporary working files do not disrupt background operations, the background monitor automatically filters out transient files. 
- **Filtered Extensions:** By default, files with transient extensions such as `.crdownload`, `.tmp`, and `.download` are ignored during background monitoring and file scanning.
- **Completion Detection:** Files are only processed once temporary download operations are fully complete and the file is finalized on disk.

### Debounce Timing & Event Handling
During rapid file creation or continuous file write operations (such as multi-file downloads or bulk file copies), processing every individual file event immediately would cause unnecessary background recalculations.
- **Standard Debounce Delay:** A default delay of 0.6 seconds pauses processing after a file event, waiting briefly to aggregate additional rapid changes.
- **Maximum Debounce Delay:** For continuous write streams, a maximum debounce delay limit of 5.0 seconds guarantees that background recalculations are scheduled once file activity stabilizes, preventing redundant processing while avoiding indefinite delays.

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
