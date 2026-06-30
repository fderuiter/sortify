@echo off
echo Setting up development environment...

echo Windows detected. No additional system dependencies needed.

echo Setting up Python virtual environment...
if not exist venv (
    python -m venv venv
)

call venv\Scripts\activate

echo Installing Python dependencies from pyproject.toml...
python -m pip install --upgrade pip
python -m pip install .

echo Setup complete. Launching application...
python main.py
