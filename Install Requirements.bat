@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "QUIET_MODE=0"
if /I "%~1"=="/quiet" set "QUIET_MODE=1"
set "ENV_ROOT=%LOCALAPPDATA%\MIDI Remixer\PythonEnv"
set "ENV_PYTHON=%ENV_ROOT%\Scripts\python.exe"

rem Always prefer an explicitly supported interpreter. A machine may have
rem Python 3.14 as its default while also having a compatible 3.11-3.13 install.
set "BASE_PYTHON="
where py >nul 2>nul
if not errorlevel 1 (
    call :probe_launcher 3.13
    call :probe_launcher 3.12
    call :probe_launcher 3.11
)
if not defined BASE_PYTHON (
    where python >nul 2>nul
    if not errorlevel 1 (
        python -c "import sys; raise SystemExit(0 if (3,11) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>nul
        if not errorlevel 1 set "BASE_PYTHON=python"
    )
)
if not defined BASE_PYTHON goto python_missing

if exist "%ENV_PYTHON%" (
    "%ENV_PYTHON%" -c "import sys, importlib.metadata as m; ok=(3,11)<=sys.version_info[:2]<=(3,13) and m.version('PySide6')=='6.8.3' and m.version('mido')=='1.3.3' and m.version('rapidfuzz')=='3.14.3' and m.version('openpyxl')=='3.1.5'; raise SystemExit(0 if ok else 1)" >nul 2>nul
    if not errorlevel 1 goto already_ready
)

if not exist "%ENV_PYTHON%" (
    echo Creating MIDI Library's reusable dependency environment...
    echo Python itself is not being reinstalled.
    if not exist "%ENV_ROOT%" mkdir "%ENV_ROOT%" >nul 2>nul
    %BASE_PYTHON% -m venv "%ENV_ROOT%"
    if errorlevel 1 goto failed
)

echo Installing or repairing MIDI Library libraries (not Python itself)...
"%ENV_PYTHON%" -m pip install --disable-pip-version-check -r "%~dp0requirements.lock"
if errorlevel 1 goto failed
"%ENV_PYTHON%" -c "import PySide6, mido, rapidfuzz, openpyxl; print('Verified shared MIDI Library environment')"
if errorlevel 1 goto failed

echo.
echo MIDI Library requirements are ready. Future Library/Remixer releases reuse this shared environment.
if "%QUIET_MODE%"=="0" pause
exit /b 0

:probe_launcher
if defined BASE_PYTHON exit /b 0
py -%~1 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == tuple(map(int, '%~1'.split('.'))) else 1)" >nul 2>nul
if not errorlevel 1 set "BASE_PYTHON=py -%~1"
exit /b 0

:already_ready
echo MIDI Library requirements are already installed. Nothing was reinstalled.
echo Use launch.bat to start the app.
if "%QUIET_MODE%"=="0" pause
exit /b 0

:python_missing
echo A compatible Python was not found. Install 64-bit Python 3.11, 3.12, or 3.13 first.
echo If Python 3.14 is your default, MIDI Remixer will still use an installed 3.11-3.13 through the Python launcher.
if "%QUIET_MODE%"=="0" pause
exit /b 1

:failed
echo.
echo Installation failed. The global Python environment was not modified.
if "%QUIET_MODE%"=="0" pause
exit /b 1
