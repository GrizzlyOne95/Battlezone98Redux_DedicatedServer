@echo off
rem Opens the dedicated-server launcher window.
if exist "%~dp0.venv\Scripts\pythonw.exe" (
  start "" "%~dp0.venv\Scripts\pythonw.exe" "%~dp0server_gui.py"
) else (
  start "" pythonw "%~dp0server_gui.py"
)
