@echo off
REM Echo Thought Core — 使用主專案 (U.E.P-s-Core) 的 Python 環境啟動
REM Usage: run.bat [--model MODEL] [--window SIZE] [--debug]

set UEP_PYTHON=C:\Users\Bernie\source\repos\Unforgettableeternalproject\U.E.P-s-Core\env\Scripts\python.exe
set PYTHONIOENCODING=utf-8

if not exist "%UEP_PYTHON%" (
    echo [ERROR] U.E.P Core Python not found at: %UEP_PYTHON%
    echo Please check the path or install dependencies in the local .venv
    pause
    exit /b 1
)

"%UEP_PYTHON%" -m echo_thought_core.cli %*
