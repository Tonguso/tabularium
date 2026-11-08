@echo off
rem Start the supervisor now (it normally starts at logon through the Task Scheduler task AgentsSupervisor).
rem Launch this through Explorer so the process is not tied to the launching app: Start-Process explorer.exe '"D:\Agents\start_supervisor.bat"'
start "" /B "C:\ProgramData\miniconda3\envs\ai_agents\pythonw.exe" "%~dp0supervisor.py"
