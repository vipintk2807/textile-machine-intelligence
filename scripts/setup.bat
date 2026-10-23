@echo off
REM Creates a virtual environment and installs dependencies. Run from the project folder.
cd /d "%~dp0\.."
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3.11 or newer from python.org and tick "Add python.exe to PATH".
  exit /b 1
)
if not exist .venv (
  python -m venv .venv || exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
python -m pip install -r requirements.txt || exit /b 1
echo.
echo Setup complete. Start the app with scripts\run.bat
