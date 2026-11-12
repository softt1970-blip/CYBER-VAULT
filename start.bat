@echo off
title CYBER-VAULT // Portable Launcher
cd /d "%~dp0"

rem ============================================================
rem  PORTABLE MODE: everything is written INSIDE this folder.
rem  - dependencies go to "%~dp0libs"
rem  - pip temp/cache go to "%~dp0.tmp" and are deleted afterwards
rem  - the global Python installation is NEVER modified
rem ============================================================

set "ROOT=%~dp0"
set "LIBDIR=%ROOT%libs"
set "TMPDIR_LOCAL=%ROOT%.tmp"
set "PYEXE="

rem ---------- 1/5 stop previous instances ----------
taskkill /F /IM pythonw.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":17555" ^| findstr "LISTENING"') do (
    taskkill /F /PID %%a >nul 2>&1
)

rem ---------- 2/5 tidy Google Drive duplicates (never overwrite app.py) ----------
if not exist "backup" mkdir "backup" >nul 2>&1
if not exist "app.py" call :adopt_dup
for %%F in ("app (*).py") do move /y "%%F" "backup\%%~nxF" >nul 2>&1
for %%F in ("start (*).bat") do move /y "%%F" "backup\%%~nxF" >nul 2>&1

if not exist "app.py" (
    echo [FATAL] app.py not found in %ROOT%
    echo         Check "backup\" for a copy.
    pause
    exit /b 1
)

rem ---------- 3/5 locate Python ----------
for /f "delims=" %%p in ('where python 2^>nul') do (if not defined PYEXE set "PYEXE=%%p")
if not defined PYEXE (
    for /f "delims=" %%p in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (if not defined PYEXE set "PYEXE=%%p")
)
if not defined PYEXE (
    echo [FATAL] Python 3 not found.
    echo         Install Python 3 and make sure python.exe is on PATH.
    pause
    exit /b 1
)
set "PYW=%PYEXE:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PYEXE%"

rem ---------- 4/5 dependencies INSIDE the project ----------
if not exist "%LIBDIR%" mkdir "%LIBDIR%" >nul 2>&1
if not exist "%TMPDIR_LOCAL%" mkdir "%TMPDIR_LOCAL%" >nul 2>&1
"%PYEXE%" -S -c "import sys; sys.path.insert(0, r'%LIBDIR%'); import pystray, PIL, pyperclip" >nul 2>&1
if errorlevel 1 (
    echo [4/5] Installing dependencies into %LIBDIR% ...
    set "TMP=%TMPDIR_LOCAL%"
    set "TEMP=%TMPDIR_LOCAL%"
    set "PIP_NO_CACHE_DIR=1"
    "%PYEXE%" -m pip install --quiet --disable-pip-version-check --no-cache-dir --target "%LIBDIR%" pystray pillow pyperclip
    if errorlevel 1 (
        echo [FATAL] Failed to install dependencies into %LIBDIR%
        rmdir /s /q "%TMPDIR_LOCAL%" >nul 2>&1
        pause
        exit /b 1
    )
)
rmdir /s /q "%TMPDIR_LOCAL%" >nul 2>&1

rem ---------- 5/5 sanity check + launch ----------
"%PYEXE%" -c "import ast,io; ast.parse(io.open(r'%ROOT%app.py', encoding='utf-8').read())" >nul 2>&1
if errorlevel 1 (
    echo [FATAL] app.py has a syntax error. Run this to see it:
    echo         "%PYEXE%" -m py_compile "%ROOT%app.py"
    pause
    exit /b 1
)

echo [5/5] Starting CYBER-VAULT in the system tray ...
start "" "%PYW%" "%ROOT%app.py"
exit /b 0

:adopt_dup
for %%F in ("app (*).py") do move /y "%%F" "app.py" >nul 2>&1
goto :eof
