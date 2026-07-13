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
