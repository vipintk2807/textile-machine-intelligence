@echo off
REM Usage: scripts\restore_backup.bat data\backups\<backup-file>.db
REM Close the application first.
cd /d "%~dp0\.."
if "%~1"=="" (
  echo Usage: scripts\restore_backup.bat path\to\backup.db
  exit /b 1
)
call .venv\Scripts\activate.bat
python scripts\db_tools.py restore "%~1"
