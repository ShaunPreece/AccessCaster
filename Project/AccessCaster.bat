@echo off
rem AccessCaster - starts the local server HIDDEN (no console window stays open) and lets it
rem open the accessible web UI itself. The server now behaves like an app: it stops by itself
rem when you close that web page, and launching it again replaces any previous instance.
cd /d "%~dp0"

rem Prefer pythonw.exe so NO console window appears while the server runs.
where pythonw >nul 2>nul
if not errorlevel 1 (
  start "" pythonw "%~dp0server.py"
  exit /b 0
)

rem pythonw not found: fall back to python. Confirm Python is installed first.
where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found on PATH. Install Python, then run this again.
  pause
  exit /b 1
)
echo pythonw.exe was not found, so a small console window will stay open while AccessCaster runs.
echo Close it to stop the server.
python "%~dp0server.py"
