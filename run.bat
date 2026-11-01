@echo off
REM Echo Memory System — 使用 U.E.P 共用 Python 環境啟動
REM Usage: run.bat [chat|dashboard]

set UEP_PYTHON=%USERPROFILE%\.uep\venv-inference-312\Scripts\python.exe
set PYTHONIOENCODING=utf-8

if not exist "%UEP_PYTHON%" (
    echo [ERROR] U.E.P Core Python not found at: %UEP_PYTHON%
    echo Please check the shared U.E.P Core environment path.
    pause
    exit /b 1
)

"%UEP_PYTHON%" Entry.py %*
