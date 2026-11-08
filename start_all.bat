@echo off
rem The one file to start or restart everything (2026-10-10). Double-click it.
rem   Nothing running:  starts the supervisor (Task Scheduler task AgentsSupervisor), which starts the four agents and the dashboard.
rem   Already running:  asks every agent to finish its current work and restart on the current code. Nothing is killed.
rem Optional: start_all.bat --no-tests   (skip the offline test suites before a restart)
pushd "%~dp0"
"C:\ProgramData\miniconda3\envs\ai_agents\python.exe" agent_control.py start_all %*
popd
pause
