@echo off
rem Starts the dedicated server in this console (Ctrl+C or close to stop).
rem Extra arguments go straight to server.py, e.g.  StartServer.bat --log-level DEBUG
rem The first run offers to open the server's ports in Windows Firewall.
setlocal
cd /d "%~dp0"

set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Python 3 was not found. Install it from https://www.python.org/ and run this again.
  pause
  exit /b 1
)

%PY% -c "import websockets" >nul 2>&1 || (
  echo Installing server dependencies...
  %PY% -m pip install -r requirements.txt || (pause & exit /b 1)
)

netsh advfirewall firewall show rule name="BZR Dedicated Server TCP" >nul 2>&1
if errorlevel 1 (
  echo.
  echo Windows Firewall has no rule for the server yet, so other PCs cannot reach it.
  choice /m "Open TCP 1337 and UDP 1338-1369 now (asks for admin)"
  if not errorlevel 2 powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -Command New-NetFirewallRule -DisplayName ''BZR Dedicated Server TCP'' -Direction Inbound -Protocol TCP -LocalPort 1337 -Action Allow | Out-Null; New-NetFirewallRule -DisplayName ''BZR Dedicated Server UDP'' -Direction Inbound -Protocol UDP -LocalPort 1338-1369 -Action Allow | Out-Null'"
)

echo.
echo Players on this network: Multiplayer ^> Custom server ^> one of these addresses:
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do echo    %%a
echo Players over the internet need TCP 1337 and UDP 1338-1369 forwarded to this PC.
echo.

%PY% server.py --relay-pair-ports --relay-pair-port-start 1340 --relay-pair-port-end 1369 %*
echo.
echo Server stopped.
pause
