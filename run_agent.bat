@echo off
if /I "%~1"=="cato" goto run
if /I "%~1"=="tacitus" goto run
if /I "%~1"=="callimachus" goto cal
if /I "%~1"=="vitruvius" goto vit
exit /b 1
:run
call "%~dp0config.bat"
set "PATH=C:\Program Files\nodejs;C:\ProgramData\miniconda3\envs\ai_agents\Scripts;%PATH%"
call C:\ProgramData\miniconda3\Scripts\activate.bat C:\ProgramData\miniconda3\envs\ai_agents
"C:\ProgramData\miniconda3\envs\ai_agents\python.exe" "%~dp0%~1\telegram.py" >> "%~dp0logs\%~1-launch.log" 2>&1

exit /b %errorlevel%
:cal
call "%~dp0callimachus\run.bat"
exit /b %errorlevel%

:vit
call "%~dp0vitruvius\run.bat"
exit /b %errorlevel%
