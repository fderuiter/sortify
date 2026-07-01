@echo off
echo Setting up development environment...

echo Windows detected. No additional system dependencies needed.

echo Installing uv if not present...
where uv >nul 2>nul
if %errorlevel% neq 0 (
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    set PATH="%USERPROFILE%\.cargo\bin;%PATH%"
)

echo Installing Python dependencies from pyproject.toml using uv...
uv sync --all-extras

echo Setup complete. Launching application...
uv run smart-autosorter
