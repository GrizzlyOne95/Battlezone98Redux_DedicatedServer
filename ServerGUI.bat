@echo off
rem Opens the dedicated-server launcher window (same as: python server.py --gui).
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0server.py" --gui
) else (
  start "" pythonw "%~dp0server.py" --gui
)
