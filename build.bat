@echo off
chcp 65001 >nul
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
    echo [INFO] Python not found. Installing via winget...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo [ERROR] Python install failed.
        echo https://www.python.org/downloads/ から手動インストールしてください。
        pause
        exit /b 1
    )
    echo [INFO] Please restart this script after Python installation.
    pause
    exit /b 0
)
echo [OK] Python found
py --version

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
        echo https://ffmpeg.org/download.html から手動インストールしてPATHに追加してください。
    ) else (
        echo [OK] FFmpeg installed. PATH反映のためスクリプトを再起動してください。
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
        echo [WARN] Node.js auto-install failed.
        echo https://nodejs.org/ から手動インストールしてください。
    ) else (
        echo [OK] Node.js installed. PATH反映のためスクリプトを再起動してください。
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
    echo   Gemini API key setup (free)
    echo ============================================
    echo   1. https://aistudio.google.com/apikey
    echo      にアクセスしてAPIキーを作成
    echo   2. 下にAPIキーを貼り付けてEnter
    echo   (後でアプリのSettings画面でも入力可能)
    echo ============================================
    set /p GEMINI_KEY="Gemini API Key (skip=Enter): "
    if defined GEMINI_KEY (
        echo %GEMINI_KEY%>"%~dp0.gemini_key"
        echo [OK] API key saved
    ) else (
        echo [SKIP] Settings画面で後から入力できます
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
:: 6. CUDA libraries
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
echo   デスクトップの「Clip Extractor」から起動できます
echo ==========================================
pause
