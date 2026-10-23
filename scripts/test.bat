@echo off
REM Runs the automated tests and lint checks.
cd /d "%~dp0\.."
call .venv\Scripts\activate.bat
python -m pytest || exit /b 1
python -m ruff check . || exit /b 1
echo All checks finished.
