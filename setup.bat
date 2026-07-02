@echo off
setlocal

echo Setting up development environment...

:: Check if an offline bundle exists
set OFFLINE_MODE=0
if exist "offline_bundle.zip" (
    echo Detected offline_bundle.zip. Extracting...
    powershell -NoProfile -Command "Expand-Archive -Path 'offline_bundle.zip' -DestinationPath 'offline_bundle' -Force"
    if %ERRORLEVEL% NEQ 0 (
        echo Error: Failed to extract offline_bundle.zip using PowerShell.
        exit /b 1
    )
)

if exist "offline_bundle\" (
    echo Offline bundle found. Enabling air-gapped installation mode.
    set OFFLINE_MODE=1
)

:: Check if uv is in PATH or in the default cargo directory
where uv >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    if exist "%USERPROFILE%\.cargo\bin\uv.exe" (
        set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
    )
)

:: Detect missing package manager and install uv
where uv >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo uv package manager not found. Attempting to install...
    
    if "%OFFLINE_MODE%"=="1" (
        echo Error: uv is not installed, but offline mode is active.
        echo Please install uv manually before running this script offline.
        exit /b 1
    )

    :: Check internet connection gracefully
    powershell -NoProfile -Command "try { $response = Invoke-WebRequest -Uri 'https://astral.sh' -UseBasicParsing -TimeoutSec 5; exit 0 } catch { exit 1 }"
    if %ERRORLEVEL% NEQ 0 (
        echo Error: No internet connection available for the initial bootstrap.
        exit /b 1
    )
    
    powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
    if %ERRORLEVEL% NEQ 0 (
        echo Error: Failed to install uv.
        exit /b 1
    )
    
    set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
)

echo Synchronizing local environment...
if "%OFFLINE_MODE%"=="1" (
    echo Using offline wheels from bundle...
    if not exist ".venv\" (
        uv venv
    )
    uv pip install --offline --no-index --find-links offline_bundle/wheels -r offline_bundle/requirements.txt
    if %ERRORLEVEL% NEQ 0 (
        echo Error: Failed to install offline dependencies.
        exit /b 1
    )
) else (
    echo Synchronizing local environment with lockfile...
    uv sync --all-extras
    if %ERRORLEVEL% NEQ 0 (
        echo Error: Failed to synchronize environment.
        exit /b 1
    )
)

echo Installing pre-commit hooks...
if "%OFFLINE_MODE%"=="1" (
    echo Skipping pre-commit installation in offline mode to avoid network calls.
) else (
    uv run pre-commit install
    if %ERRORLEVEL% NEQ 0 (
        echo Error: Failed to install pre-commit hooks.
        exit /b 1
    )
)

echo Setup complete. Virtual environment provisioned.
echo You can manually run the application anytime using:
echo   uv run python app/main.py
echo Launching application now...
uv run python app/main.py
