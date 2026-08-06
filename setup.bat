@echo off
echo ================================================
echo  ELD Checker - Setup
echo ================================================
echo.

:: Check Python is installed
python --version >nul 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo ERROR: Python is not installed or not on PATH.
    echo.
    echo Please install Python from https://www.python.org/downloads/
    echo Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

echo Python found:
python --version
echo.

echo [1/2] Installing dependencies...
python -m pip install -r requirements.txt
IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo ERROR: Failed to install dependencies.
    pause
    exit /b 1
)

echo.
echo [2/3] Creating desktop shortcut...
python create_shortcut.py
IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo WARNING: Could not create shortcut automatically.
    echo You can still run the app by double-clicking launcher.py
)

echo.
echo [3/3] Checking local settings files...
set MISSING=0
IF NOT EXIST "service_account.json" (
    echo   MISSING: service_account.json  ^(Google key - copy from the other PC^)
    set MISSING=1
)
IF NOT EXIST "config.json" (
    echo   MISSING: config.json           ^(copy config.example.json and set sheet_id^)
    set MISSING=1
)
IF NOT EXIST "roster.json" (
    echo   MISSING: roster.json           ^(copy roster.example.json and list the drivers^)
    set MISSING=1
)
IF %MISSING%==0 echo   All three present.

echo.
echo ================================================
IF %MISSING%==1 (
    echo  Setup incomplete - add the files listed above.
    echo  They are deliberately not in the repository:
    echo  the key is secret and the roster has real names.
) ELSE (
    echo  Setup complete!
    echo  Double-click 'ELD Checker' on your Desktop to start.
)
echo ================================================
echo.
pause
