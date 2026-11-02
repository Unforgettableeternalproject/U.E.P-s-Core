@echo off
REM Echo STT System — 使用主專案 (U.E.P-s-Core) 的 Python 環境跑測試
REM Usage: test.bat [pytest args...]
REM   test.bat                    — 跑全部測試
REM   test.bat -x -v              — 遇到失敗停止 + verbose
REM   test.bat tests/test_vad.py  — 只跑特定測試

set UEP_PYTHON=C:\Users\Bernie\source\repos\Unforgettableeternalproject\U.E.P-s-Core\env\Scripts\python.exe
set PYTHONIOENCODING=utf-8

if not exist "%UEP_PYTHON%" (
    echo [ERROR] U.E.P Core Python not found at: %UEP_PYTHON%
    pause
    exit /b 1
)

"%UEP_PYTHON%" -m pytest tests/ -o "addopts=" %*
