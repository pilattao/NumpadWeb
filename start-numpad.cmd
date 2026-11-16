@echo off
title Web Numpad
cd /d "%~dp0"
rem py (Python 3.14) is already allowed in Windows Firewall on this PC; plain "python" may be blocked.
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 numpad_server.py %*
) else (
  python numpad_server.py %*
)
echo.
pause
