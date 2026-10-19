# Troubleshooting Guide

Welcome to the Smart AutoSorter AI Pro troubleshooting guide. If you are experiencing issues during setup, particularly with downloading the AI model, please consult the sections below.

![Setup Wizard Network Flow](assets/diagrams/setup_wizard_flow.svg)

## Common Network Failure Messages

### 1. "Download failed: Cannot connect to host" or "Connection timeout"
**Cause:** Your firewall, antivirus, or network proxy is blocking the background network request to Hugging Face (`huggingface.co`), or you are completely disconnected from the internet.
**Solution:**
- Check your internet connection.
- Temporarily disable your VPN or firewall to see if it allows the download to proceed.
- If you are on an enterprise network, you may need to ask your administrator for an **offline deployment bundle** to sideload the model.

### 2. "Download failed: Insufficient disk space"
**Cause:** The 80MB AI model download requires free disk space on your local drive.
**Solution:**
- Free up at least 200MB of space on your main system drive.
- Clear unneeded temporary files on your drive, then retry the download.

## Manual Retries

If the initial download in the Setup Wizard fails or if you previously declined AI consent, you can manually trigger consent setup or retry the AI model download at any time:

### Using CLI Commands
- Grant consent and trigger model setup directly from the command line:
  ```bash
  smart-autosorter --accept-ai-consent
  ```
- Alternatively, update your stored configuration setting directly:
  ```bash
  smart-autosorter config --set AI_CONSENT_GRANTED true
  ```
- In non-interactive or headless execution environments without a TTY, include non-interactive flags to bypass interactive dialog prompts:
  ```bash
  smart-autosorter --accept-ai-consent --skip-wizard --non-interactive
  ```

### Using Text User Interface (TUI)
- Launch the interactive terminal interface:
  ```bash
  smart-autosorter --tui
  ```
- Press `[Ctrl+W]` to reopen the Model Onboarding Wizard modal.
- Use `[Tab]` to navigate to the **AI Consent Granted** toggle switch, press `[Space]` or `[Enter]` to toggle consent on, and press `[Enter]` on **Finish & Save** to save your preferences and retry initialization.

If the problem persists and you cannot resolve your network issues, you can keep using the application offline (`smart-autosorter --decline-ai-consent`). Files are still clustered by content using TF-IDF keyword vectors instead of the embedding model; see [Offline Mode](user_guide.md#offline-mode).

## Settings configuration

The Settings dialog (`[Ctrl+O]`) checks values when you press **Save**. If one is out of range, it shows a "Settings error" and keeps the dialog open so you can correct it:

| Setting | Allowed values |
|---|---|
| Worker concurrency | A whole number greater than 0 |
| Max folders | 1 to 50. It limits the folders created at each level of the tree, not the total |
| PDF text density threshold | 0 or more. PDF pages with fewer characters than this are treated as scanned and sent to OCR when the `ml` extra is installed |

Protected paths and ignored extensions are comma-separated lists. A leading dot is added to an extension if you leave it out, so `tmp` becomes `.tmp`. See the [Administrator Guide](admin_guide.md) for every setting and its default.

## Session recovery

If Sortify was closed while it was moving files, the next start shows **Interrupted Session Detected** with the session id, target folder and status. Pick one:

- **Rollback** undoes the moves that session already made, using its ledger, then rescans the folder. If the rollback fails, for example because of a permission error, the session files are kept so you can try again.
- **Clean** deletes the session's working files and leaves your files where they are now. Clean also deletes the session's rollback journal, so the dialog can no longer undo the moves it had already made. Choose Rollback if you're not sure.

From the command line, `smart-autosorter undo --list` shows recent sessions and `smart-autosorter undo --session-id ID` rolls one back.

## DropZone errors

The DropZone (`[Ctrl+D]`) sorts files you paste or drop into its queue. **Sort Items** reports an error when nothing in the queue can be sorted:

- **"No file or folder paths provided."** The queue is empty. Paste one or more paths into the input and press Enter.
- **"Protected path blocked: NAME"** The item is inside one of your protected paths (Settings, General), so Sortify will not move it. Remove the path from the protected list if you do want it sorted.
- **"No ready files to sort in queue."** Every queued item is protected, missing (**Not Found**) or **Unsupported**. The DropZone accepts `.txt`, `.docx`, `.csv`, `.xlsx`, `.xls` and `.pdf`; **Filter Unsupported** removes the rest from the queue.

## CRO forensic ingestion

The CRO Multi-Study Forensic Ingestion dialog (`[Ctrl+C]`) scans a source folder of clinical study files and files them into a TMF-style structure under the target folder.

- **"Source directory does not exist."** Check the source path. It must be an existing folder on this machine.
- **"Forensic scan error: …"** The pipeline stopped part-way. The log in the dialog shows the last step it reached; fix the cause it names (often a permission error on the target folder) and run it again.
