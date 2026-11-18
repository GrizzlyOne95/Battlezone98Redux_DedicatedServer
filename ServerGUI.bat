@echo off
rem Opens the dedicated-server launcher window (same as: python server.py --gui).
setlocal
set "PYW="
rem Quoted inside the value: the folder may contain spaces.
if exist "%~dp0.venv\Scripts\pythonw.exe" set PYW="%~dp0.venv\Scripts\pythonw.exe"
if not defined PYW where pythonw >nul 2>&1 && set "PYW=pythonw"
rem The python.org installer does not add Python to PATH by default, but it
rem installs the py launcher, whose windowed twin is pyw.
if not defined PYW where pyw >nul 2>&1 && set "PYW=pyw -3"
if not defined PYW (
  echo Python 3 was not found. Install it from https://www.python.org/ and run this again.
  pause
  exit /b 1
)
start "" %PYW% "%~dp0server.py" --gui
