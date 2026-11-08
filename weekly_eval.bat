@echo off
rem Weekly regression eval and research signal (Task Scheduler: AgentsRegressionEval, Sunday 03:00). 2026-10-24.
cd /d D:\Agents
C:\ProgramData\miniconda3\envs\ai_agents\python.exe -X utf8 agent_control.py eval cato --k 3 --sample 8 >> logs\eval-cato.log 2>&1
C:\ProgramData\miniconda3\envs\ai_agents\python.exe -X utf8 agent_control.py eval cato --signal 3 >> logs\eval-cato.log 2>&1
