@echo off
setlocal

echo ============================================
echo World Radio - Windows build
echo ============================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on PATH. Install Python 3 and try again.
    exit /b 1
)

if not exist venv (
    echo Creating virtual environment...
    python -m venv venv
)

call venv\Scripts\activate.bat

echo.
echo Installing dependencies...
python -m pip install --upgrade pip >nul
pip install -r requirements.txt
pip install pyinstaller

echo.
echo Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo.
echo Building with PyInstaller...
pyinstaller world_radio.spec --noconfirm

if errorlevel 1 (
    echo.
    echo Build failed - see the errors above.
    pause
    exit /b 1
)

echo.
echo ============================================
echo Build complete.
echo Output: dist\World Radio\World Radio.exe
echo.
echo Reminder: VLC media player must be installed
echo on any machine that RUNS this build, including
echo this one if you want to test it now.
echo ============================================
pause
