# Troubleshooting Guide

Welcome to the Smart AutoSorter AI Pro troubleshooting guide. This page covers model downloads, settings errors, interrupted sessions, the DropZone and CRO ingestion. The **Help** buttons in the terminal UI open the matching section.

![Setup Wizard Network Flow](assets/diagrams/setup_wizard_flow.svg)

## Model download failures

The Onboarding Wizard (`[Ctrl+W]`) and Model Manager (`[Ctrl+G]` or Command Palette `[Ctrl+K]`) download the optional local embedding model (`all-MiniLM-L6-v2` in ONNX format) from Hugging Face and verify its SHA-256 digests before activation. While weights are missing, if a download fails, or if the optional `onnxruntime` package (`smart-autosorter[ml]`) is not installed, the status bar shows `[AI: Fallback]` and Sortify continues organizing folders locally with keyword rules and TF-IDF clustering.

### Network errors
The message names a network, connection or timeout error. A firewall, antivirus, VPN or proxy is blocking `huggingface.co`, or the machine is offline.

- Check your internet connection, and try again with the VPN off.
- Behind a corporate proxy, enter the proxy URL directly in the Onboarding Wizard (`#input-wizard-proxy`), in the Model Manager (`[Ctrl+G]`), or on the CLI via `smart-autosorter model download --proxy http://proxy.example.com:8080`.
- On metered or offline networks, enable **Defer model download** in the Onboarding Wizard (or pass `--defer-model-download` on the CLI) to skip background transfers and sort immediately in `[AI: Fallback]` mode.
- If verified weights are present on disk but the badge still shows `[AI: Fallback]`, run `smart-autosorter model status` to check `ONNX Runtime:`; install `smart-autosorter[ml]` (`uv sync --extra ml` or `pip install 'smart-autosorter[ml]'`) so `onnxruntime` can execute local semantic inference.
- If the network can't be opened, ask your administrator for the offline deployment bundle, which includes the pre-verified model files.

### "Insufficient disk space"
The message reads `Insufficient disk space. Required: N bytes, Free: M bytes.` or `Insufficient disk space on the target drive.` Sortify checks that the target drive has room for the whole file before it downloads. Free that much space plus a margin, then retry.

## Granting consent and retrying the download

Completing onboarding with **AI Consent Granted** switched on automatically starts a background model download when verified weights are absent (unless deferred), while scanning your workspace right away. Once SHA-256 verification finishes (and `onnxruntime` is installed), the status bar switches from `[AI: Downloading …]` to `[AI: Active]` and automatically re-scans an unedited mock sorting plan with semantic embeddings.

### In the terminal UI
1. Start the app with `smart-autosorter` (or `smart-autosorter --tui`).
2. On first launch (or by pressing `[Ctrl+W]`), switch **AI Consent Granted** on with `[Space]`, optionally configure a proxy URL or opt into starter rule template packs, and choose **Finish & Save** to save consent and start background provisioning automatically.
3. To inspect progress, retry an interrupted download, verify hashes, or delete cached weights later, press `[Ctrl+G]` (or open the Command Palette with `[Ctrl+K]` and select **Open Model Manager**).

### From the command line
- Save AI consent across restarts, verify or download model weights with terminal progress output, and run directly from top-level setup or `scan`, `sort`, `drop`, and `daemon`:
  ```bash
  smart-autosorter --accept-ai-consent
  smart-autosorter scan ~/Downloads --accept-ai-consent
  smart-autosorter sort ~/Downloads --accept-ai-consent
  ```
- Save AI consent in headless scripts or on metered/offline links without downloading weights immediately:
  ```bash
  smart-autosorter --accept-ai-consent --non-interactive
  smart-autosorter scan ~/Downloads --accept-ai-consent --defer-model-download
  ```
- Check model and ONNX Runtime status, retry downloads (including `--proxy`), verify SHA-256 hashes, or remove local model files directly:
  ```bash
  smart-autosorter model status
  smart-autosorter model download --proxy http://proxy.example.com:8080
  smart-autosorter model verify
  smart-autosorter model remove
  ```

You can also decline AI consent permanently (`smart-autosorter --decline-ai-consent`). Files are still clustered locally by content using TF-IDF keyword vectors; see [Offline Mode](user_guide.md#offline-mode).

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

From the command line, `smart-autosorter undo --list` shows recent sessions and `smart-autosorter undo --session-id ID` rolls one back. For automated headless batch recovery, transaction state transitions, or CLI reconciliation without interactive prompts, see the dedicated [Ledger Reconciliation Runbook](runbooks/ledger_reconciliation.md).

### Multi-session recovery

When multiple interrupted sessions are detected on startup, Sortify displays the **Multi-Session Recovery** dialog showing all abandoned sessions with their session IDs, target directories, step counts, and statuses.

You can manage all sessions at once or take action on individual sessions:
- **Rollback All** (`#btn-rollback-all`): Reverts uncommitted operations across all detected sessions and updates the file tree.
- **Clean All** (`#btn-clean-all`): Cleans up working files and journal ledgers for all abandoned sessions.
- **Resume** (`#btn-resume`): Continues sorting for the selected session while retaining remaining sessions for subsequent recovery.
- **Rollback** (`#btn-rollback`): Rolls back only the selected session.
- **Clean** (`#btn-clean`): Cleans working files for the selected session only, allowing selective recovery of remaining sessions.

For authoritative headless batch procedures, CLI commands (`smart-autosorter ledger status`, `reconcile`, `purge`, `export`), and resolving stale daemon locks (`.autosorter.pid` / `.autosorter.sock`), refer to the [Operational Runbook for Ledger Reconciliation](runbooks/ledger_reconciliation.md).

## DropZone errors

The DropZone (`[Ctrl+D]` or `smart-autosorter drop --tui`) sorts files and folders you paste or drop into its queue, either in place or into a chosen destination folder (**Destination folder** input or **Browse** button). **Sort Items** reports an error when nothing in the queue can be sorted or the destination is invalid:

- **"No file or folder paths provided."** The queue is empty. Paste or drop one or more file or folder paths (including multi-file terminal drops or `file://` URIs) into the input and press Enter.
- **"Protected path blocked: NAME"** A queued source item or the selected destination folder is inside one of your protected paths (Settings, General), so Sortify will not move files from or into it. Remove the path from the protected list or pick another destination folder if you want to proceed.
- **"No ready files to sort in queue."** Every queued item is protected, missing (**Not Found**) or **Unsupported**. The DropZone accepts all formats supported by the sorting pipeline—documents (`.txt`, `.docx`, `.csv`, `.xlsx`, `.xls`, `.pdf`), images (`.png`, `.jpg`, `.jpeg`), audio (`.mp3`, `.wav`, `.m4a`), video/media, archives, installers/binaries, design/CAD files, Jev-classified code and presentation formats (`.pptx`, `.ppt`, `.key`, `.py`, `.json`, `.yaml`, etc.), and any file matching your configured rules or extension categories—while keeping extensions in **Ignored extensions** as **Unsupported**. **Filter Unsupported** removes unsupported items from the queue.

## CRO forensic ingestion

The CRO Multi-Study Forensic Ingestion dialog (accessible via Command Palette `[Ctrl+K]`) scans a source folder of clinical study files and files them into a TMF-style structure under the target folder.

- **"Source directory does not exist."** Check the source path. It must be an existing folder on this machine.
- **"Forensic scan error: …"** The pipeline stopped part-way. The log in the dialog shows the last step it reached; fix the cause it names (often a permission error on the target folder) and run it again.
