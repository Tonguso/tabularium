# One-time setup (run once as yourself, no admin needed): register the supervisor to start at logon and remove the
# old watchdog task. Everything else (restarts, crash recovery, the dashboard) is handled by supervisor.py from then on.
$action   = New-ScheduledTaskAction -Execute 'C:\ProgramData\miniconda3\envs\ai_agents\pythonw.exe' -Argument '"D:\Agents\supervisor.py"' -WorkingDirectory 'D:\Agents'
$trigger  = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'AgentsSupervisor' -Action $action -Trigger $trigger -Settings $settings -Force `
    -Description 'Owns the four Telegram agents and the dashboard; restarts any that exit. Restart an agent with: python D:\Agents\agent_control.py restart <agent>' | Out-Null
Unregister-ScheduledTask -TaskName 'CatoAgentsWatchdog' -Confirm:$false -ErrorAction SilentlyContinue
if (-not (Get-ScheduledTask -TaskName 'AgentsSupervisor' | Where-Object State -eq 'Running')) { Start-ScheduledTask -TaskName 'AgentsSupervisor' }
Get-ScheduledTask -TaskName 'AgentsSupervisor' | Select-Object TaskName, State
