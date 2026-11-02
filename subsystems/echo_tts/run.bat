@echo off
REM Echo TTS System - Launch with U.E.P-s-Core Python environment
REM Usage: run.bat              (Interactive Rich CLI shell)
REM        run.bat shell        (Same as above)
REM        run.bat generate     (One-shot generation)
REM        run.bat profile      (Profiling)
REM        run.bat smoke        (Scaffold verification)

set "UEP_PYTHON=C:\Users\Bernie\source\repos\Unforgettableeternalproject\U.E.P-s-Core\env\Scripts\python.exe"
set PYTHONIOENCODING=utf-8

if not exist "%UEP_PYTHON%" (
    echo [ERROR] U.E.P Core Python not found at: %UEP_PYTHON%
    echo Please check the shared U.E.P Core environment path.
    pause
    exit /b 1
)

"%UEP_PYTHON%" Entry.py %*
