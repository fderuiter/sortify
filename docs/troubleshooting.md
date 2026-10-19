# Troubleshooting Guide

Welcome to the Smart AutoSorter AI Pro troubleshooting guide. This page covers model downloads, settings errors, interrupted sessions, the DropZone and CRO ingestion. The **Help** buttons in the terminal UI open the matching section.

![Setup Wizard Network Flow](assets/diagrams/setup_wizard_flow.svg)

## Model download failures

The Model Manager (`[Ctrl+K]`) downloads the optional embedding model (`all-MiniLM-L6-v2` in ONNX format) from Hugging Face. When a download fails it shows **Model download failed:** followed by the reason.

### Network errors
The message names a network, connection or timeout error. A firewall, antivirus, VPN or proxy is blocking `huggingface.co`, or the machine is offline.

- Check your internet connection, and try again with the VPN off.
- Behind a corporate proxy, enter it in the Model Manager's proxy field before downloading.
- If the network can't be opened, ask your administrator for the offline deployment bundle, which includes the model.

### "Insufficient disk space"
The message reads `Insufficient disk space. Required: N bytes, Free: M bytes.` or `Insufficient disk space on the target drive.` Sortify checks that the target drive has room for the whole file before it downloads. Free that much space plus a margin, then retry.

## Granting consent and retrying the download

Consent and the download are separate steps. Granting consent allows Sortify to use the model; the download itself starts from the Model Manager.

### In the terminal UI
1. Start the app with `smart-autosorter --tui`.
2. Press `[Ctrl+W]` to open the Model Onboarding Wizard, switch **AI Consent Granted** on with `[Space]`, and choose **Finish & Save**.
3. Press `[Ctrl+K]` to open the Model Manager and choose **Start Download**, or **Retry Download** after a failure.

### From the command line
`smart-autosorter --accept-ai-consent` opens the terminal UI with consent granted for that run. It does not start the download; use the Model Manager for that.

`smart-autosorter config --set AI_CONSENT_GRANTED true` is meant to save consent permanently, but it currently prints the new value without reliably saving it ([#806](https://github.com/fderuiter/sortify/issues/806)). Use the wizard until that is fixed.

You can keep using Sortify without the model (`smart-autosorter --decline-ai-consent`). Files are still clustered by content using TF-IDF keyword vectors; see [Offline Mode](user_guide.md#offline-mode).

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
