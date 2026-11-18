@echo off
rem Starts the dedicated server in this console (Ctrl+C or close to stop).
rem Extra arguments go straight to server.py, e.g.  StartServer.bat --log-level DEBUG
rem The first run offers to open the server's ports in Windows Firewall.
setlocal
cd /d "%~dp0"

set "PY="
rem Quoted inside the value: the folder may contain spaces.
if exist "%~dp0.venv\Scripts\python.exe" set PY="%~dp0.venv\Scripts\python.exe"
if not defined PY where py >nul 2>&1 && set "PY=py -3"
if not defined PY where python >nul 2>&1 && set "PY=python"
if not defined PY (
  echo Python 3 was not found. Install it from https://www.python.org/ and run this again.
  pause
  exit /b 1
)

%PY% -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1 || (
  echo This server needs Python 3.9 or newer ^(3.12 recommended^). Install it from https://www.python.org/
  pause
  exit /b 1
)

%PY% -c "import websockets" >nul 2>&1 || (
  echo Installing server dependencies...
  %PY% -m pip install -r requirements.txt || (pause & exit /b 1)
)

rem Same rule names as the launcher's Setup Guide, so following both never
rem creates duplicates. Older versions of this script used the second names.
set "HAVE_RULE="
netsh advfirewall firewall show rule name="BZR server TCP" >nul 2>&1 && set "HAVE_RULE=1"
netsh advfirewall firewall show rule name="BZR Dedicated Server TCP" >nul 2>&1 && set "HAVE_RULE=1"
if not defined HAVE_RULE if not exist "%~dp0data\.firewall-declined" (
  echo.
  echo Windows Firewall has no rule for the server yet, so other PCs cannot reach it.
  echo The rules apply to Private networks: set this network to Private in Windows Settings.
  choice /m "Open TCP 1337 and UDP 1338-1369 now (asks for admin)"
  if errorlevel 2 (
    if not exist "%~dp0data" mkdir "%~dp0data"
    echo declined> "%~dp0data\.firewall-declined"
    echo Not asking again. Delete data\.firewall-declined to be asked next time.
  ) else (
    powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -Wait -ArgumentList '-NoProfile -Command New-NetFirewallRule -DisplayName ''BZR server TCP'' -Direction Inbound -Protocol TCP -LocalPort 1337 -Action Allow -Profile Private | Out-Null; New-NetFirewallRule -DisplayName ''BZR server UDP'' -Direction Inbound -Protocol UDP -LocalPort 1338-1369 -Action Allow -Profile Private | Out-Null'"
  )
)

echo.
echo This PC's network addresses (use the one on the same network as the players):
powershell -NoProfile -Command "Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.NetAdapter.Status -eq 'Up' } | ForEach-Object { '   ' + $_.IPv4Address.IPAddress + '   (' + $_.InterfaceAlias + ')' }" 2>nul
echo.
echo How players connect (replace ^<address^> with one of the addresses above):
echo    OpenShim:        Multiplayer setup screen ^> Custom ^> ^<address^>
echo    Stock Steam/GOG: launch option  /bzrserver=ws://^<address^>:1337/
echo Players over the internet need TCP 1337 and UDP 1338-1369 forwarded to this PC.
echo.

%PY% server.py --relay-pair-ports --relay-pair-port-start 1340 --relay-pair-port-end 1369 %*
echo.
echo Server stopped.
pause
