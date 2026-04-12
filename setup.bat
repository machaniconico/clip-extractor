@echo off
chcp 65001 >nul
echo ==========================================
echo   Clip Extractor - Setup
echo ==========================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python が見つかりません。
    echo https://www.python.org/downloads/ からインストールしてください。
    echo インストール時に「Add Python to PATH」にチェックを入れてください。
    pause
    exit /b 1
)

echo [OK] Python found
python --version

:: Check FFmpeg
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [WARNING] FFmpeg が見つかりません。
    echo https://ffmpeg.org/download.html からインストールしてください。
)
echo [OK] FFmpeg found

:: Check yt-dlp
yt-dlp --version >nul 2>&1
if errorlevel 1 (
    echo [INFO] yt-dlp をインストールします...
    pip install yt-dlp
)
echo [OK] yt-dlp found

:: Install dependencies
echo.
echo [INFO] 依存パッケージをインストールしています...
pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] パッケージのインストールに失敗しました。
    pause
    exit /b 1
)

:: Install CUDA-optimized faster-whisper
echo.
echo [INFO] CUDA対応版をセットアップしています...
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 2>nul
echo [OK] CUDA libraries installed (if available)

echo.
echo ==========================================
echo   セットアップ完了！
echo   「Clip Extractor.bat」をダブルクリックで起動できます。
echo ==========================================
pause
