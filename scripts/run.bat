@echo off
REM Starts the application on http://127.0.0.1:8501
cd /d "%~dp0\.."
if not exist .venv\Scripts\activate.bat (
  echo Virtual environment not found. Run scripts\setup.bat first.
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m streamlit run app.py --server.address 127.0.0.1
