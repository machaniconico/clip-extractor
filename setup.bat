@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
:: ==========================================
:: Auto-elevate to administrator
:: ==========================================
net session >nul 2>&1
if errorlevel 1 (
    echo [INFO] Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 0
)
title Clip Extractor - Setup
echo ==========================================
echo   Clip Extractor - Setup
echo ==========================================
echo.

cd /d "%~dp0"

:: ==========================================
:: 1. Python
:: ==========================================
echo [1/7] Checking Python...
py --version >nul 2>&1
if errorlevel 1 (
    python --version >nul 2>&1
    if errorlevel 1 (
        echo [INFO] Python not found. Installing via winget...
        winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
        if errorlevel 1 (
            echo [ERROR] Python install failed.
            echo Please install manually from https://www.python.org/downloads/
            echo Check "Add Python to PATH" during installation.
            pause
            exit /b 1
        )
        echo [INFO] Python installed. Please restart this script.
        pause
        exit /b 0
    )
)
echo [OK] Python found
py --version 2>nul || python --version

:: ==========================================
:: 2. FFmpeg
:: ==========================================
echo.
echo [2/7] Checking FFmpeg...
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [INFO] FFmpeg not found. Installing via winget...
    winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo [WARN] FFmpeg auto-install failed.
        echo Please install manually from https://ffmpeg.org/download.html and add to PATH.
    ) else (
        echo [OK] FFmpeg installed. Please restart this script to refresh PATH.
        pause
        exit /b 0
    )
) else (
    echo [OK] FFmpeg found
)

:: ==========================================
:: 3. Node.js + Claude Code CLI
:: ==========================================
echo.
echo [3/7] Checking Node.js...
node --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] Node.js not found. Installing via winget...
    winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo [WARN] Node.js auto-install failed. OpenAI/Gemini mode is still available.
        echo Manual install: https://nodejs.org/
    ) else (
        echo [OK] Node.js installed. Please restart this script to refresh PATH.
        pause
        exit /b 0
    )
) else (
    echo [OK] Node.js found
)

echo [INFO] Checking Claude Code CLI...
claude --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] Claude Code CLI not found. Installing...
    call npm install -g @anthropic-ai/claude-code
    if errorlevel 1 (
        echo [WARN] Claude CLI install failed. OpenAI/Gemini mode is still available.
    ) else (
        echo [OK] Claude Code CLI installed
    )
) else (
    echo [OK] Claude Code CLI found
)

:: ==========================================
:: 4. Gemini API key setup
:: ==========================================
echo.
echo [4/7] Checking Gemini API key...
if exist "%~dp0.gemini_key" (
    echo [OK] Gemini API key file found
) else (
    echo.
    echo ============================================
    echo   Gemini API key setup [free tier available]
    echo ============================================
    echo   1. Visit https://aistudio.google.com/apikey
    echo      and create an API key
    echo   2. Paste the key below and press Enter
    echo      [You can also set it later in Settings tab]
    echo ============================================
    set /p GEMINI_KEY="Gemini API Key (skip=Enter): "
    if defined GEMINI_KEY (
        >"%~dp0.gemini_key" echo !GEMINI_KEY!
        echo [OK] API key saved to .gemini_key
    ) else (
        echo [SKIP] You can enter the key later in Settings tab.
    )
)

:: ==========================================
:: 5. Python dependencies
:: ==========================================
echo.
echo [5/7] Installing Python dependencies...
py -m pip install --upgrade pip >nul 2>&1
py -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Python dependencies install failed.
    pause
    exit /b 1
)
echo [OK] Python dependencies installed

:: ==========================================
:: 6. CUDA libraries (GPU acceleration for faster-whisper)
:: ==========================================
echo.
echo [6/7] Installing CUDA libraries...
py -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 2>nul
echo [OK] CUDA libraries installed (if GPU available)

:: ==========================================
:: 7. Desktop shortcut
:: ==========================================
echo.
echo [7/7] Creating desktop shortcut...
powershell -NoProfile -Command "try { $desktop = [Environment]::GetFolderPath('Desktop'); $ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut(\"$desktop\Clip Extractor.lnk\"); $sc.TargetPath = '%~dp0Clip Extractor.bat'; $sc.WorkingDirectory = '%~dp0'; $sc.Save(); Write-Host '[OK] Desktop shortcut created' } catch { Write-Host '[WARN] Could not create shortcut:' $_.Exception.Message }"

echo.
echo ==========================================
echo   Setup complete!
echo   Launch via "Clip Extractor.bat" or
echo   the desktop shortcut.
echo ==========================================
pause
