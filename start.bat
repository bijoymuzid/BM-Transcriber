@echo off
cd /d "%~dp0"

echo ========================================
echo   Bengali Audio Transcriber — Quick Start
echo ========================================
echo.

REM --- Check Python ---
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python 3.10+ from https://python.org
    pause
    exit /b 1
)

REM --- Create venv if missing ---
if not exist "venv" (
    echo [1/3] Creating virtual environment...
    python -m venv venv
) else (
    echo [1/3] Virtual environment found.
)

REM --- Install dependencies ---
echo [2/3] Installing Python dependencies...
call venv\Scripts\pip install -q -r requirements.txt

REM --- Check .env ---
if not exist ".env" (
    echo [WARNING] No .env file found. You can enter your API key in the web UI.
) else if not exist ".env.example" (
    echo [WARNING] .env.example missing, but .env exists.
)

REM --- Run ---
echo [3/3] Starting server...
echo.
echo     Open in your browser: http://127.0.0.1:5000
echo     Press Ctrl+C to stop the server.
echo.
call venv\Scripts\python app.py

pause
