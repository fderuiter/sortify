@echo off
echo Setting up local environment with uv...
uv sync --all-extras
if %errorlevel% neq 0 (
    echo Setup failed!
    exit /b %errorlevel%
)
uv run pre-commit install
echo Setup complete. Virtual environment provisioned.


