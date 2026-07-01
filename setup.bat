@echo off
setlocal

echo Setting up development environment...

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

echo Synchronizing local environment with lockfile...
uv sync --all-extras
if %ERRORLEVEL% NEQ 0 (
    echo Error: Failed to synchronize environment.
    exit /b 1
)

echo Installing pre-commit hooks...
uv run pre-commit install
if %ERRORLEVEL% NEQ 0 (
    echo Error: Failed to install pre-commit hooks.
    exit /b 1
)

echo Setup complete. Virtual environment provisioned.
echo You can manually run the application anytime using:
echo   .venv\Scripts\smart-autosorter.exe
echo Launching application now...
uv run smart-autosorter
