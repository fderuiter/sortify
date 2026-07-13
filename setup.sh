#!/usr/bin/env bash
set -e

echo "Setting up development environment..."

# Check if an offline bundle exists
OFFLINE_MODE=0
if [ -f "offline_bundle.zip" ]; then
    echo "Detected offline_bundle.zip. Extracting..."
    # Attempt to extract using python if unzip is not available, or just use unzip
    if command -v unzip >/dev/null 2>&1; then
        unzip -q -o offline_bundle.zip -d offline_bundle
    elif command -v python >/dev/null 2>&1 || command -v python3 >/dev/null 2>&1; then
        ${PYTHON:-python} -m zipfile -e offline_bundle.zip offline_bundle
    else
        echo "Error: unzip or Python required to extract offline_bundle.zip."
        exit 1
    fi
fi

if [ -d "offline_bundle" ]; then
    echo "Offline bundle found. Enabling air-gapped installation mode."
    OFFLINE_MODE=1
fi

# Check if uv is in PATH, or if it is installed in the default cargo directory
if ! command -v uv >/dev/null 2>&1; then
    if [ -f "$HOME/.cargo/bin/uv" ]; then
        export PATH="$HOME/.cargo/bin:$PATH"
    fi
fi

# Detect missing package manager and install uv
if ! command -v uv >/dev/null 2>&1; then
    echo "uv package manager not found."
    echo "Error: uv is not installed."
    echo "Please install uv manually before running this setup script."
    echo ""
    echo "Installation instructions:"
    echo "Run the following command in your terminal:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo ""
    echo "Or refer to the official documentation: https://docs.astral.sh/uv/getting-started/installation/"
    exit 1
fi

echo "Synchronizing local environment..."
if [ "$OFFLINE_MODE" -eq 1 ]; then
    echo "Using offline wheels from bundle..."
    # We must create the venv first if it doesn't exist
    if [ ! -d ".venv" ]; then
        uv venv
    fi
    # Use uv pip install with find-links to install entirely offline
    uv pip install --offline --no-index --find-links offline_bundle/wheels -r offline_bundle/requirements.txt
else
    echo "Synchronizing local environment with lockfile..."
    uv sync --all-extras
fi

echo "Installing pre-commit hooks..."
if [ "$OFFLINE_MODE" -eq 1 ]; then
    echo "Skipping pre-commit installation in offline mode to avoid network calls."
else
    uv run pre-commit install
fi

echo "Setup complete. Virtual environment provisioned."
echo "You can manually run the application anytime using:"
echo "  uv run python app/main.py"
echo "Launching application now..."
uv run python app/main.py
