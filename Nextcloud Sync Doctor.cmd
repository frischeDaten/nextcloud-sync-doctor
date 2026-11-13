@echo off
setlocal
title Nextcloud Sync Doctor
cd /d "%~dp0"

rem --- locate a Python 3.10+ interpreter -------------------------------------
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY where python3 >nul 2>&1 && set "PY=python3"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
  if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
)
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do (
  if exist "%%~fD\python.exe" set "PY=%%~fD\python.exe"
)

if not defined PY (
  echo.
  echo   Python 3.10 or newer was not found.
  echo   Install it from https://www.python.org/downloads/windows/
  echo   ^(tick "Add python.exe to PATH" during setup^) and run this file again.
  echo.
  pause
  exit /b 1
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
  echo.
  echo   Found %PY%, but version 3.10 or newer is required.
  echo.
  pause
  exit /b 1
)

rem --- default to the web UI, but pass a subcommand through ("... --demo scan") ---
set "CMD=serve"
for %%A in (%*) do (
  if /i "%%~A"=="scan" set "CMD="
  if /i "%%~A"=="serve" set "CMD="
  if /i "%%~A"=="check" set "CMD="
  if /i "%%~A"=="report" set "CMD="
)
if defined CMD (
  %PY% -m ncsd serve %*
) else (
  %PY% -m ncsd %*
)
if errorlevel 1 pause
endlocal
