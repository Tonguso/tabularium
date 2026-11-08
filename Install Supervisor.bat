@echo off
rem Double-click this once. It registers the supervisor to start when you log on and starts it now.
rem It asks Windows for nothing beyond your own account: no admin prompt.
echo Registering the agents supervisor...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_supervisor.ps1"
if errorlevel 1 (
    echo.
    echo Something went wrong. Take a screenshot of this window and send it to Claude.
) else (
    echo.
    echo Done. The agents now start on their own at every logon and recover on their own.
)
echo.
pause
