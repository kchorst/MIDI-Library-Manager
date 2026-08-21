@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "PYTHON_EXE="
set "GUI_PYTHON="
set "SYSTEM_PYW="
set "SYSTEM_PYW_ARGS="
set "SHARED_ENV=%LOCALAPPDATA%\MIDI Remixer\PythonEnv"
set "APP_ENTRY=MIDI Library Manager.pyw"
if /I "%~1"=="manager" set "APP_ENTRY=MIDI Library Manager.pyw"

call :try_env "%SHARED_ENV%"
call :try_env "%~dp0.venv"

rem Reuse a compatible private environment from a nearby earlier release. This
rem avoids downloading PySide6 again every time a new code ZIP is extracted.
if not defined PYTHON_EXE (
    for /d %%D in ("%~dp0..\MIDI-Remixer-v*") do call :try_env "%%~fD\.venv"
)

rem Preserve the pre-v0.19 launcher behavior when the user's existing Python
rem already has the exact supported dependencies. No private environment is
rem created and nothing is reinstalled in this case.
if not defined PYTHON_EXE call :try_system_python

if not defined PYTHON_EXE if not defined SYSTEM_PYW goto setup

goto start_app

:setup
echo Preparing MIDI Library's reusable dependency environment one time...
echo Python itself is not being reinstalled.
call "%~dp0Install Requirements.bat" /quiet
if errorlevel 1 exit /b 1
call :try_env "%SHARED_ENV%"
if not defined PYTHON_EXE goto failed

:start_app
if defined SYSTEM_PYW (
    start "" %SYSTEM_PYW% %SYSTEM_PYW_ARGS% "%~dp0%APP_ENTRY%"
    exit /b 0
)
start "" "%GUI_PYTHON%" "%~dp0%APP_ENTRY%"
exit /b 0

:try_env
if defined PYTHON_EXE exit /b 0
if not exist "%~1\Scripts\python.exe" exit /b 0
"%~1\Scripts\python.exe" -c "import sys, importlib.metadata as m; ok=(3,11)<=sys.version_info[:2]<=(3,13) and m.version('PySide6')=='6.8.3' and m.version('mido')=='1.3.3' and m.version('rapidfuzz')=='3.14.3' and m.version('openpyxl')=='3.1.5'; raise SystemExit(0 if ok else 1)" >nul 2>nul
if errorlevel 1 exit /b 0
set "PYTHON_EXE=%~1\Scripts\python.exe"
set "GUI_PYTHON=%~1\Scripts\pythonw.exe"
if not exist "%GUI_PYTHON%" set "GUI_PYTHON=%PYTHON_EXE%"
exit /b 0

:try_system_python
where pyw >nul 2>nul
if not errorlevel 1 (
    call :try_pyw_version 3.13
    call :try_pyw_version 3.12
    call :try_pyw_version 3.11
    if defined SYSTEM_PYW exit /b 0
)
where pythonw >nul 2>nul
if not errorlevel 1 (
    python -c "import sys, importlib.metadata as m; ok=(3,11)<=sys.version_info[:2]<=(3,13) and m.version('PySide6')=='6.8.3' and m.version('mido')=='1.3.3' and m.version('rapidfuzz')=='3.14.3' and m.version('openpyxl')=='3.1.5'; raise SystemExit(0 if ok else 1)" >nul 2>nul
    if not errorlevel 1 (
        set "SYSTEM_PYW=pythonw"
        set "SYSTEM_PYW_ARGS="
    )
)
exit /b 0

:try_pyw_version
if defined SYSTEM_PYW exit /b 0
py -%~1 -c "import sys, importlib.metadata as m; ok=sys.version_info[:2] == tuple(map(int, '%~1'.split('.'))) and m.version('PySide6')=='6.8.3' and m.version('mido')=='1.3.3' and m.version('rapidfuzz')=='3.14.3' and m.version('openpyxl')=='3.1.5'; raise SystemExit(0 if ok else 1)" >nul 2>nul
if not errorlevel 1 (
    set "SYSTEM_PYW=pyw"
    set "SYSTEM_PYW_ARGS=-%~1"
)
exit /b 0

:failed
echo MIDI Library could not prepare a compatible private environment.
pause
exit /b 1
