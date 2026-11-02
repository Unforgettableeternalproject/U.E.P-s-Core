@echo off
REM Echo TTS System - Run tests with U.E.P-s-Core Python environment
REM Usage: test.bat [pytest args...]
REM   test.bat                       - Run all tests
REM   test.bat -x -v                 - Stop on first failure + verbose
REM   test.bat tests/test_poise.py   - Run specific test file

set "UEP_PYTHON=C:\Users\Bernie\source\repos\Unforgettableeternalproject\U.E.P-s-Core\env\Scripts\python.exe"
set PYTHONIOENCODING=utf-8

if not exist "%UEP_PYTHON%" (
    echo [ERROR] U.E.P Core Python not found at: %UEP_PYTHON%
    pause
    exit /b 1
)

"%UEP_PYTHON%" -m pytest tests/ -o "addopts=" %*
