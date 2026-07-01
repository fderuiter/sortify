#!/usr/bin/env bash
set -e

echo "Setting up development environment..."

# Check if uv is in PATH, or if it is installed in the default cargo directory
if ! command -v uv >/dev/null 2>&1; then
    if [ -f "$HOME/.cargo/bin/uv" ]; then
        export PATH="$HOME/.cargo/bin:$PATH"
    fi
fi

# Detect missing package manager and install uv
if ! command -v uv >/dev/null 2>&1; then
    echo "uv package manager not found. Attempting to install..."
    
    # Check internet connection gracefully
    if ! curl -Is https://astral.sh >/dev/null; then
        echo "Error: No internet connection available for the initial bootstrap."
        exit 1
    fi
    
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.cargo/bin:$PATH"
fi

echo "Synchronizing local environment with lockfile..."
uv sync --all-extras

echo "Installing pre-commit hooks..."
uv run pre-commit install

echo "Setup complete. Virtual environment provisioned."
echo "You can manually run the application anytime using:"
echo "  .venv/bin/smart-autosorter"
echo "Launching application now..."
uv run smart-autosorter
