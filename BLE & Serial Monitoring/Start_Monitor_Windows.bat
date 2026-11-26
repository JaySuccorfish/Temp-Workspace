@echo off
REM Double-clickable zero-setup Windows launcher for Succorfish BLE & Serial Monitor.
REM Works even on a fresh Windows PC with no Python installed:
REM   1. Reuses .portable-env\win32-x86_64 or .build-venv if present.
REM   2. Otherwise runs portable_run.py with any installed Python 3.10+.
REM   3. If no Python is installed, downloads standalone portable Python 3.12 via PowerShell
REM      into .portable-env\win32-x86_64\python and launches portable_run.py automatically.

setlocal enabledelayedexpansion
cd /d "%~dp0"

set "PORTABLE_PY=%~dp0.portable-env\win32-x86_64\python\python.exe"
set "BUILD_VENV_PY=%~dp0.build-venv\Scripts\python.exe"

if exist "%PORTABLE_PY%" (
    "%PORTABLE_PY%" "%~dp0portable_run.py" %*
    goto :done
)

if exist "%BUILD_VENV_PY%" (
    "%BUILD_VENV_PY%" "%~dp0portable_run.py" %*
    goto :done
)

where py >nul 2>&1
if %errorlevel%==0 (
    py -3 "%~dp0portable_run.py" %*
    if %errorlevel%==0 goto :done
)

where python >nul 2>&1
if %errorlevel%==0 (
    python "%~dp0portable_run.py" %*
    if %errorlevel%==0 goto :done
)

echo [win] No Python found -- downloading standalone portable Python 3.12...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$dest = Join-Path '%~dp0' '.portable-env\win32-x86_64';" ^
  "New-Item -ItemType Directory -Force -Path $dest | Out-Null;" ^
  "$url = 'https://github.com/astral-sh/python-build-standalone/releases/download/20250317/cpython-3.12.9+20250317-x86_64-pc-windows-msvc-install_only.tar.gz';" ^
  "$tmp = Join-Path $env:TEMP 'sf_py_standalone.tar.gz';" ^
  "Invoke-WebRequest -Uri $url -OutFile $tmp;" ^
  "tar -xzf $tmp -C $dest;" ^
  "Remove-Item $tmp -Force -ErrorAction SilentlyContinue"

if exist "%PORTABLE_PY%" (
    "%PORTABLE_PY%" "%~dp0portable_run.py" %*
) else (
    echo [win] ERROR: Could not prepare portable Python.
    pause
    exit /b 1
)

:done
if not %errorlevel%==0 pause
exit /b %errorlevel%
