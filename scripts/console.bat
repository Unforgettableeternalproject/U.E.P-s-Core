@echo off
rem U.E.P Console launcher (ASCII only: cmd misparses multibyte text in .bat)
rem   scripts\console.bat            -> --mode real --play
rem   scripts\console.bat <args...>  -> pass args through as-is
setlocal
rem Run from repo root so .env and outputs\e2e (host epoch file) resolve consistently
cd /d "%~dp0.."
set "PY=%USERPROFILE%\.uep\venv-inference-312-t211\Scripts\python.exe"
if not exist "%PY%" (
  echo Inference venv not found: %PY%
  echo Run scripts\setup_infer_env.ps1 first.
  pause
  exit /b 1
)
set "UEP_E2E_ALLOW_GPU=1"
set "PYTHONUTF8=1"
rem Cold VOX load can exceed the 240s default when disk cache is cold; an already-set env var wins
if not defined UEP_INFER_SPEAK_PREPARE_TIMEOUT_S set "UEP_INFER_SPEAK_PREPARE_TIMEOUT_S=600"
if "%~1"=="" (
  "%PY%" -m uep.console --mode real --play
) else (
  "%PY%" -m uep.console %*
)
echo.
echo Console exited with code %ERRORLEVEL%.
pause
endlocal
