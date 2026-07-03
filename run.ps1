$ErrorActionPreference = 'Stop'

# --- ffmpeg check ---
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Host "==> ffmpeg not found." -ForegroundColor Yellow
    Write-Host "    Install ffmpeg from: https://ffmpeg.org/download.html"
    Write-Host "    Or via winget: winget install ffmpeg"
    exit 1
} else {
    $ver = & ffmpeg -version | Select-Object -First 1
    Write-Host "==> ffmpeg found: $ver"
}

# --- venv ---
if (-not (Test-Path -Path "venv")) {
    Write-Host "==> Creating virtual environment..."
    python -m venv venv
}

# --- deps ---
Write-Host "==> Installing Python dependencies..."
& .\venv\Scripts\pip install -q -r requirements.txt

# --- .env check ---
if (-not (Test-Path -Path ".env") -and (-not $env:OPENROUTER_API_KEY)) {
    Write-Host "==> No .env found and OPENROUTER_API_KEY not set." -ForegroundColor Yellow
    Write-Host "    Create .env from .env.example or enter the key in the web UI."
}

# --- run ---
Write-Host ""
Write-Host "==> Starting server at http://127.0.0.1:5000"
& .\venv\Scripts\python app.py
