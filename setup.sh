#!/bin/bash
set -e

echo "Setting up development environment..."

# Check OS and install system dependencies
OS="$(uname -s)"
case "${OS}" in
    Linux*)
        echo "Linux detected. Checking for system dependencies..."
        if command -v dpkg-query >/dev/null 2>&1; then
            MISSING_PKGS=""
            if [ "$(dpkg-query -W -f='${Status}' python3-tk 2>/dev/null | grep -c "ok installed")" -eq 0 ]; then
                MISSING_PKGS="$MISSING_PKGS python3-tk"
            fi
            if [ "$(dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -c "ok installed")" -eq 0 ]; then
                MISSING_PKGS="$MISSING_PKGS python3-venv"
            fi
            
            if [ -n "$MISSING_PKGS" ]; then
                echo "Missing packages:$MISSING_PKGS. Installing..."
                if command -v apt-get >/dev/null 2>&1; then
                    sudo apt-get update && sudo apt-get install -y $MISSING_PKGS
                else
                    echo "apt-get not found. Please install manually:$MISSING_PKGS"
                fi
            else
                echo "System dependencies are already installed."
            fi
        elif command -v dnf >/dev/null 2>&1; then
            sudo dnf install -y python3-tkinter
        elif command -v pacman >/dev/null 2>&1; then
            sudo pacman -S --noconfirm tk
        fi
        ;;
    Darwin*)
        echo "macOS detected. No additional system dependencies needed for Tkinter usually."
        ;;
    CYGWIN*|MINGW32*|MSYS*|MINGW*)
        echo "Windows environment detected. No additional system dependencies needed."
        ;;
    *)
        echo "Unknown OS: ${OS}"
        ;;
esac

echo "Installing uv if not present..."
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.cargo/bin:$PATH"
fi

echo "Installing Python dependencies from pyproject.toml using uv..."
uv sync --all-extras

echo "Setup complete. Launching application..."
uv run smart-autosorter
