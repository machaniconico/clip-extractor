@echo off
chcp 65001 >nul
title Clip Extractor

:: Get script directory
cd /d "%~dp0"

:: Find Python that has gradio installed
set PYTHON_CMD=
py -V:3.12 -c "import gradio" >nul 2>&1
if not errorlevel 1 (
    set PYTHON_CMD=py -V:3.12
    goto :found_python
)
python -c "import gradio" >nul 2>&1
if not errorlevel 1 (
    set PYTHON_CMD=python
    goto :found_python
)
echo.
echo [ERROR] Gradio が見つかりません。setup.bat を実行してください。
pause
exit /b 1

:found_python
echo Clip Extractor を起動しています...
echo ブラウザが自動で開きます。閉じるにはこのウィンドウを閉じてください。
echo.

:: Start server and open browser
start http://localhost:8080
%PYTHON_CMD% web_app.py

pause
