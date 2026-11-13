@echo off
REM Lethe Cross-Platform Wrapper Script (Windows CMD / PowerShell)
REM Automatically resolves the active Python interpreter (.venv, venv, or PATH)
REM and launches Lethe without requiring tools to guess the Python path.

setlocal
set "SCRIPT_DIR=%~dp0"
if "%SCRIPT_DIR:~-1%"=="\" set "SCRIPT_DIR=%SCRIPT_DIR:~0,-1%"
set "PYTHONUNBUFFERED=1"
set "PYTHONIOENCODING=utf-8"

if defined LETHE_PYTHON (
    if exist "%LETHE_PYTHON%" (
        set "PYTHON_BIN=%LETHE_PYTHON%"
        goto :run
    )
)

if exist "%SCRIPT_DIR%\.venv\Scripts\python.exe" (
    set "PYTHON_BIN=%SCRIPT_DIR%\.venv\Scripts\python.exe"
    goto :run
)

if exist "%SCRIPT_DIR%\venv\Scripts\python.exe" (
    set "PYTHON_BIN=%SCRIPT_DIR%\venv\Scripts\python.exe"
    goto :run
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    for /f "delims=" %%P in ('py -3 -c "import sys; print(sys.executable)" 2^>nul') do (
        if exist "%%P" (
            set "PYTHON_BIN=%%P"
            goto :run
        )
    )
)

for /f "delims=" %%P in ('where python 2^>nul') do (
    "%%P" -c "import sys" >nul 2>nul
    if not errorlevel 1 (
        set "PYTHON_BIN=%%P"
        goto :run
    )
)

echo Error: Could not locate a Python 3.10+ interpreter for Lethe. 1>&2
exit /b 1

:run
if defined PYTHONPATH (
    set "PYTHONPATH=%SCRIPT_DIR%;%PYTHONPATH%"
) else (
    set "PYTHONPATH=%SCRIPT_DIR%"
)

"%PYTHON_BIN%" -m lethe.cli %*
exit /b %ERRORLEVEL%
