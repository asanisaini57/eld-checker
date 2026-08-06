@echo off
setlocal EnableDelayedExpansion
echo ================================================
echo  ELD Checker - Setup
echo ================================================
echo.

:: ── Find a Python that actually works ────────────────────────────────
:: The "py" launcher is checked first because on Windows 11 the bare
:: "python" command is often an install-manager stub with no runtime behind
:: it — running it prints a usage screen and waits at a [y/N] prompt.
:: Redirecting stdin from nul stops that prompt hanging this script.
set PYCMD=
py -3 --version >nul 2>&1 <nul
if %ERRORLEVEL% EQU 0 set PYCMD=py -3

if not defined PYCMD (
    python --version >nul 2>&1 <nul
    if !ERRORLEVEL! EQU 0 set PYCMD=python
)

if not defined PYCMD (
    echo ERROR: No working Python runtime found.
    echo.
    where py >nul 2>&1
    if !ERRORLEVEL! EQU 0 (
        echo The Python launcher is installed but has no runtime behind it.
        echo That is why "python" only shows a usage screen.
        echo.
        echo Fix it by installing a runtime:
        echo.
        echo     py install 3.14
        echo.
        echo Then run this setup again.
    ) else (
        echo Install Python from https://www.python.org/downloads/
        echo IMPORTANT: tick "Add Python to PATH" during installation.
    )
    echo.
    pause
    exit /b 1
)

echo Python found:
%PYCMD% --version
echo.

echo [1/3] Installing dependencies...
%PYCMD% -m pip install -r requirements.txt
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo [2/3] Creating desktop shortcut...
%PYCMD% create_shortcut.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo WARNING: Could not create shortcut automatically.
    echo You can still run the app by double-clicking launcher.py
)

echo.
echo [3/3] Checking local settings files...
set MISSING=0
if not exist "service_account.json" (
    echo   MISSING: service_account.json  ^(Google key - copy from the other PC^)
    set MISSING=1
)
if not exist "config.json" (
    echo   MISSING: config.json           ^(copy config.example.json and set sheet_id^)
    set MISSING=1
)
if not exist "roster.json" (
    echo   MISSING: roster.json           ^(copy roster.example.json and list the drivers^)
    set MISSING=1
)
if "!MISSING!"=="0" echo   All three present.

echo.
echo ================================================
if "!MISSING!"=="1" (
    echo  Setup incomplete - add the files listed above.
    echo  They are deliberately not in the repository:
    echo  the key is secret and the roster has real names.
) else (
    echo  Setup complete.
    echo  Double-click 'ELD Checker' on your Desktop to start.
)
echo ================================================
echo.
pause
