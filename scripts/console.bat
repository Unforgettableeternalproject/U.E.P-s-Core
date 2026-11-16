@echo off
rem U.E.P Console launcher (ASCII only: cmd misparses multibyte text in .bat)
rem   scripts\console.bat            -> --mode real --play
rem   scripts\console.bat <args...>  -> pass args through as-is
rem
rem Cycle-2 test baseline: the flags below are applied on EVERY (re)start.
rem Precedence (verified against pydantic-settings, see uep\config.py UepSettings):
rem   process environment variable  >  .env file  >  code default
rem So a plain "set" here beats whatever .env says, and the baseline really takes effect.
rem (Do NOT switch to "if not defined": a stale shell variable would then silently win.)
rem Laya settings (UEP_DECISION_*, UEP_LAYA_*, UEP_TRANSLATE_*, UEP_LLM_*, UEP_LORE_*) live in .env: not touched here.
rem
rem In-console restart: type  /restart UEP_BEHAVIOR_DRIVER=on UEP_SCHEDULE_REST_WINDOW=plan
rem   -> console writes outputs\e2e\console_overrides.env, exits with code 75, this loop
rem      rebuilds baseline + overrides and starts again with the same args.
rem   /restart KEY=   drops that override (back to baseline);  /restart  = same flags;  /flags = show effective values
rem   The override file is deleted at every fresh start of this bat.
setlocal EnableExtensions
rem Run from repo root so .env and outputs\e2e (host epoch file) resolve consistently
cd /d "%~dp0.."
set "PY=%USERPROFILE%\.uep\venv-inference-312-t211\Scripts\python.exe"
if not exist "%PY%" (
  echo Inference venv not found: %PY%
  echo Run scripts\setup_infer_env.ps1 first.
  pause
  exit /b 1
)
set "OVR=%CD%\outputs\e2e\console_overrides.env"
if not exist "%CD%\outputs\e2e" mkdir "%CD%\outputs\e2e"
if exist "%OVR%" del "%OVR%"
set "CONSOLE_ARGS=%*"
if not defined CONSOLE_ARGS set "CONSOLE_ARGS=--mode real --play"

:loop
call :run
set "RC=%ERRORLEVEL%"
if "%RC%"=="75" goto restarting
echo.
echo Console exited with code %RC%.
pause
endlocal
exit /b %RC%

:restarting
echo.
echo Console requested restart, applying overrides from %OVR% ...
goto loop

:run
rem Subroutine scope: setlocal here means every iteration starts from the original environment,
rem so removing a key from the override file really restores the baseline (or unsets it).
setlocal
set "UEP_E2E_ALLOW_GPU=1"
set "PYTHONUTF8=1"
rem Cold VOX load can exceed the 240s default when disk cache is cold; an already-set env var wins
if not defined UEP_INFER_SPEAK_PREPARE_TIMEOUT_S set "UEP_INFER_SPEAK_PREPARE_TIMEOUT_S=600"
set "UEP_CONSOLE_OVERRIDE_FILE=%OVR%"
set "UEP_CONSOLE_RELAUNCH=1"

rem ---- Baseline (on from the first start) ----
set "UEP_TOOL_PATH=agent"
set "UEP_AGENT_LOOP_ENTRY=1"
set "UEP_CONSOLE_INGRESS_ENDPOINT=on"
rem Behavior: watch the shadow proposals vs actual state first. When they look right, switch with
rem   /restart UEP_BEHAVIOR_DRIVER=on UEP_SCHEDULE_REST_WINDOW=plan
rem (UEP_SCHEDULE_PLANNER stays shadow; UEP_PRESENCE_ARBITER=on is a later step.)
set "UEP_BEHAVIOR_DRIVER=shadow"
set "UEP_SCHEDULE_PLANNER=shadow"
set "UEP_SCHEDULE_REST_WINDOW=fixed"
set "UEP_PRESENCE_ARBITER=off"
rem C9 dream window
set "UEP_DREAM_WINDOW_START=09:00"
set "UEP_DREAM_WINDOW_END=23:59"

rem ---- Kept OFF by default (flip with /restart when the checklist item calls for it) ----
rem UEP_INFER_VOX_EMOTION_ENABLED=1     C11 emotional speech
rem UEP_GATE_REQUIRE_INTERACTION=1      only when shutdown summary not_interaction == 0
rem UEP_TOOL_TESTKIT=1                  guide 3.1.1 test toolkit (needs UEP_TOOL_PATH=agent)
rem UEP_MODULE_COORDINATOR=1            module coordinator (producer)
rem UEP_TOOL_MODULE_EVENTS=1            module.* events (subscriber; needs UEP_MODULE_COORDINATOR=1)
rem UEP_MODULE_UNLOAD_RELEASE_RATIO=0.5 before the first non-pinned GPU module goes live

rem ---- /restart overrides accumulated in this bat run ----
if exist "%OVR%" for /f "usebackq tokens=1* delims==" %%A in ("%OVR%") do set "%%A=%%B"

"%PY%" -m uep.console %CONSOLE_ARGS%
set "RC_RUN=%ERRORLEVEL%"
endlocal & exit /b %RC_RUN%
