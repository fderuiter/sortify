# Troubleshooting Guide

Welcome to the Smart AutoSorter AI Pro troubleshooting guide. If you are experiencing issues during setup, particularly with downloading the AI model, please consult the sections below.

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
- Use the Settings panel to clear up any unneeded files, then retry the download.

## Manual Retries

If the initial download in the Setup Wizard fails or if you accidentally clicked "Decline (Offline Mode)", you can manually trigger the download at any time:

1. Open the **Settings** panel from the main application window.
2. Scroll down to the **AI Features & Privacy** section.
3. Click the **Download AI Model** button. 
4. The setup wizard will reappear, allowing you to try the 80MB model download again.

If the problem persists and you cannot resolve your network issues, you can continue using the application in **Offline Non-Semantic Mode**, which will still process your files automatically, albeit without advanced AI context.

## Offline Model Directory Overrides & Model Resolution Recovery

In air-gapped or network-isolated enterprise environments, issues may arise with custom model directory overrides or missing model weights. Follow the steps below to diagnose and recover from model resolution errors.

### 1. `ModelWeightsNotFoundError` (Model files missing in all search paths)
**Cause:** The application evaluated all 4 search precedence levels (`MODEL_PATH` / `FLORENCE_2_PATH` / `EASYOCR_PATH` environment overrides -> PyInstaller `_MEIPASS` bundle -> local `offline_bundle/` directory -> user home directory fallback) but could not locate the model bundle or required manifest files.
**Recovery Steps:**
- **Step 1:** Verify whether custom path environment variables are set correctly (`MODEL_PATH`, `FLORENCE_2_PATH`, `EASYOCR_PATH`). Ensure paths are absolute or valid relative paths without typos.
- **Step 2:** Ensure the target model directory contains all required manifest files:
  - For `model`: `config.json` and model weights (`model.onnx` or equivalent weight file).
  - For `florence-2`: `config.json`, `processor_config.json`, and model weights (`model.safetensors`).
  - For `easyocr`: `craft_mlt_25k.pth` and language weights (`english_g2.pth`).
- **Step 3:** If deploying in an air-gapped network, re-run `python scripts/prepare_offline.py` or `python scripts/install_offline.py` to recreate and unpack the full offline bundle into the root `offline_bundle/` folder.

### 2. `OfflineModelLoadError` / Network Blocked During Sandboxed Loading
**Cause:** The application's network sandbox detected an external network request during offline model initialization because a required model component or tokenizer asset was missing locally.
**Recovery Steps:**
- **Step 1:** Do not attempt online downloads in air-gapped environments.
- **Step 2:** Verify that all tokenizer, preprocessor, and configuration files listed in the **Administrator Guide** are present in the offline model folder.
- **Step 3:** Confirm read permissions on the model folder and all subdirectories for the user account running the application process.

### 3. Configuration Mismatches & Save State Lock
**Cause:** `settings.json` or `.env` contains invalid configuration types or malformed values for model path variables.
**Recovery Steps:**
- **Step 1:** Check the warning banner in the application Settings panel or review `autosorter.log` for specific validation error messages.
- **Step 2:** Edit `.env` or `~/.autosorter/settings.json` to fix syntax errors or invalid paths.
- **Step 3:** To reset configuration to clean defaults, delete `~/.autosorter/settings.json` or copy a clean `.env.example` to `.env`.

