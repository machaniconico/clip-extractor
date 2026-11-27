@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
:: ==========================================
:: 管理者権限の自動取得
:: ==========================================
net session >nul 2>&1
if errorlevel 1 (
    echo [INFO] セットアップに必要な管理者権限を取得しています...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b 0
)
title Clip Extractor - はじめてのセットアップ
echo ==========================================================
echo   Clip Extractor - はじめてのセットアップ
echo ==========================================================
echo.
echo   このウィンドウでは、アプリの動作に必要なツールを自動導入します。
echo   通常は 1 回の実行で完了します（完了まで数分かかります）。
echo.

cd /d "%~dp0"

:: ==========================================
:: 1. Python
:: ==========================================
echo [1/6] Python を確認しています...
call :detect_python
if not defined PYTHON_CMD (
    echo [INFO] Python が見つかりません。winget で自動インストールします...
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo [ERROR] Python の自動インストールに失敗しました。
        echo         https://www.python.org/downloads/ から手動でインストールし、
        echo         「Add Python to PATH」にチェックを入れてから再度 setup.bat を実行してください。
        pause
        exit /b 1
    )
    call :refresh_path
    call :detect_python
    if not defined PYTHON_CMD (
        echo [INFO] Python の導入が完了しました。PATH を反映するため、
        echo        一度この画面を閉じて setup.bat をもう一度実行してください。
        pause
        exit /b 0
    )
)
echo [OK] Python を検出しました (%PYTHON_CMD%)
%PYTHON_CMD% --version

:: ==========================================
:: 2. FFmpeg
:: ==========================================
echo.
echo [2/6] FFmpeg（動画の切り抜きに必須）を確認しています...
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo [INFO] FFmpeg が見つかりません。winget で自動インストールします...
    winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements
    if errorlevel 1 (
        echo [WARN] FFmpeg の自動インストールに失敗しました。
        echo        https://ffmpeg.org/download.html から手動で導入し PATH に追加してください。
    ) else (
        call :refresh_path
        ffmpeg -version >nul 2>&1
        if errorlevel 1 (
            echo [OK] FFmpeg をインストールしました。PATH を反映するため、
            echo      一度この画面を閉じて setup.bat をもう一度実行してください。
            pause
            exit /b 0
        )
        echo [OK] FFmpeg をインストールしました
    )
) else (
    echo [OK] FFmpeg を検出しました
)

:: ==========================================
:: 3. Python 依存パッケージ & yt-dlp
:: ==========================================
echo.
echo [3/6] 必要な Python パッケージをインストールしています...
%PYTHON_CMD% -m pip install --upgrade pip >nul 2>&1
%PYTHON_CMD% -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Python パッケージのインストールに失敗しました。
    echo         インターネット接続を確認してから setup.bat を再実行してください。
    pause
    exit /b 1
)
echo [INFO] yt-dlp を最新版に更新しています...
%PYTHON_CMD% -m pip install --upgrade "yt-dlp[default]"
if errorlevel 1 (
    echo [WARN] yt-dlp の更新に失敗しました。YouTube で HTTP 403 が出る場合は後ほど setup.bat を再実行してください。
) else (
    echo [OK] yt-dlp を最新版にしました
)
echo [OK] Python パッケージの導入が完了しました

:: ==========================================
:: 4. CUDA ライブラリ (NVIDIA GPU 高速化)
:: ==========================================
echo.
echo [4/6] GPU 高速化ライブラリ (CUDA) を導入しています...
%PYTHON_CMD% -m pip install nvidia-cublas-cu12 nvidia-cudnn-cu12 2>nul
echo [OK] CUDA ライブラリの確認が完了しました（GPU がない PC でも CPU で動作します）

:: ==========================================
:: 5. Claude Code CLI（任意・通常はスキップで OK）
:: ==========================================
echo.
echo [5/6] Claude Code CLI（任意）を確認しています...
call claude --version >nul 2>&1
if not errorlevel 1 (
    echo [OK] Claude Code CLI はすでに導入済みです
) else (
    echo [INFO] 無料枠のある Gemini を使う場合、Claude CLI と Node.js は不要です。
    choice /c NY /n /d N /t 10 /m "Claude CLI も一緒に導入しますか？ [N=スキップ(推奨) / Y=導入] (10秒で自動スキップ): "
    if errorlevel 2 (
        echo [INFO] Node.js を確認しています...
        node --version >nul 2>&1
        if errorlevel 1 (
            echo [INFO] Node.js を winget で自動インストールします...
            winget install -e --id OpenJS.NodeJS.LTS --accept-package-agreements --accept-source-agreements
            call :refresh_path
        )
        call npm install -g @anthropic-ai/claude-code
        if errorlevel 1 (
            echo [WARN] Claude CLI の導入に失敗しました（Gemini / OpenAI モードは問題なく使えます）。
        ) else (
            echo [OK] Claude Code CLI を導入しました
        )
    ) else (
        echo [SKIP] Claude CLI をスキップしました（あとからでも導入できます）
    )
)

:: ==========================================
:: 6. デスクトップショートカット作成
:: ==========================================
echo.
echo [6/6] デスクトップショートカットを作成しています...
powershell -NoProfile -Command "try { $desktop = [Environment]::GetFolderPath('Desktop'); $ws = New-Object -ComObject WScript.Shell; $sc = $ws.CreateShortcut(\"$desktop\Clip Extractor.lnk\"); $sc.TargetPath = '%~dp0Clip Extractor.bat'; $sc.WorkingDirectory = '%~dp0'; $iconPath = '%~dp0assets\icon.ico'; if (Test-Path $iconPath) { $sc.IconLocation = $iconPath } else { Write-Host '[WARN] assets\icon.ico が見つかりません（既定アイコンを使用）' }; $sc.Description = 'Clip Extractor - 配信切り抜き自動生成'; $sc.Save(); Write-Host '[OK] デスクトップに Clip Extractor のショートカットを作成しました' } catch { Write-Host '[WARN] ショートカット作成に失敗しました:' $_.Exception.Message }"

echo.
echo ==========================================================
echo   セットアップが完了しました！
echo ==========================================================
echo.
if exist "%~dp0.gemini_key" (
    echo [OK] 保存済みの API キーを検出しました。すぐ使い始められます！
) else (
    echo [次のステップ（初回だけ・約2分・無料）]
    echo   1. Clip Extractor を起動し、「Settings / 設定」タブを開く
    echo   2. Google AI Studio (https://aistudio.google.com/apikey) で無料キーを取得
    echo   3. 「APIキー」欄に貼って「💾 このキーを保存」→「🔍 接続テスト」を押す
    echo   ※ 迷ったら同梱の「SETUP_GUIDE.html」（図解ガイド）をご覧ください。
)
echo.
choice /c YN /n /m "今すぐ Clip Extractor を起動しますか？ [Y=起動する / N=あとで起動]: "
if errorlevel 2 (
    echo.
    echo 起動するときはデスクトップの「Clip Extractor」または
    echo このフォルダの「Clip Extractor.bat」をダブルクリックしてください。
    pause
    exit /b 0
)

if not exist "%~dp0.gemini_key" (
    if exist "%~dp0SETUP_GUIDE.html" (
        echo [INFO] 初回用の図解ガイド（SETUP_GUIDE.html）をブラウザで開きます...
        start "" "%~dp0SETUP_GUIDE.html"
    )
)
echo [INFO] Clip Extractor を起動しています...
start "" "%~dp0Clip Extractor.bat"
exit /b 0

:: ==========================================
:: サブルーチン: PATH 再読み込み（再起動不要化）
:: ==========================================
:refresh_path
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "$m=[Environment]::GetEnvironmentVariable('Path','Machine'); $u=[Environment]::GetEnvironmentVariable('Path','User'); ($m,$u | Where-Object { $_ }) -join ';'"`) do (
    set "PATH=%%P"
)
exit /b 0

:: ==========================================
:: サブルーチン: 利用可能な Python コマンドを検出
:: ==========================================
:detect_python
set "PYTHON_CMD="
py -V:3.12 --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -V:3.12"
    exit /b 0
)
py --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py"
    exit /b 0
)
python --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    exit /b 0
)
exit /b 0
