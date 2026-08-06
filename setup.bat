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
echo [2/2] Creating desktop shortcut...
python create_shortcut.py
IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo WARNING: Could not create shortcut automatically.
    echo You can still run the app by double-clicking launcher.py
)

echo.
echo ================================================
echo  Setup complete!
echo  Double-click 'ELD Checker' on your Desktop to start.
echo ================================================
echo.
pause
