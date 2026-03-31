<#
.SYNOPSIS
    Register a Windows Task Scheduler task that starts the Ollama localtunnel
    watchdog at user login — no terminal window, auto-restart on failure.

.DESCRIPTION
    Creates (or updates) a scheduled task named "GAOS-OllamaTunnel" that:
      - Triggers at user login
      - Runs start_ollama_tunnel.py via the repo venv Python, hidden (no console)
      - Is set to restart up to 3 times on failure, 1 minute apart
      - Logs stdout+stderr to logs\ollama-tunnel.log in the repo root

    Must be run once from an elevated PowerShell (Run as Administrator) OR
    from a normal user session — both work; task is registered for current user.

.PARAMETER Port
    Local Ollama port. Default: 11434

.PARAMETER Project
    GCP project id. Default: morphic-gaos-prod

.PARAMETER RetryDelaySec
    Seconds the Python watchdog waits between tunnel restarts. Default: 10

.EXAMPLE
    # Register with defaults
    powershell -ExecutionPolicy Bypass -File scripts\register_ollama_tunnel_task.ps1

    # Register with custom port
    powershell -ExecutionPolicy Bypass -File scripts\register_ollama_tunnel_task.ps1 -Port 8000

    # Unregister
    Unregister-ScheduledTask -TaskName "GAOS-OllamaTunnel" -Confirm:$false
#>

param(
    [int]    $Port           = 11434,
    [string] $Project        = "morphic-gaos-prod",
    [int]    $RetryDelaySec  = 10,
    [string] $Subdomain      = "gaos-ollama"   # fixed URL: https://gaos-ollama.loca.lt
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# ── Paths ──────────────────────────────────────────────────────────────────
$RepoRoot   = Split-Path -Parent $PSScriptRoot
$PythonExe  = Join-Path $RepoRoot ".venv\Scripts\pythonw.exe"   # pythonw = no console window
$ScriptPath = Join-Path $RepoRoot "scripts\start_ollama_tunnel.py"
$LogDir     = Join-Path $RepoRoot "logs"
$LogFile    = Join-Path $LogDir   "ollama-tunnel.log"

if (-not (Test-Path $PythonExe)) {
    Write-Error "pythonw.exe not found at $PythonExe — has the venv been created?"
}

if (-not (Test-Path $LogDir)) {
    New-Item -ItemType Directory -Path $LogDir | Out-Null
    Write-Host "[setup] Created logs\ directory"
}

# ── Task definition ────────────────────────────────────────────────────────
$TaskName   = "GAOS-OllamaTunnel"
$TaskDesc   = "Morphic-GAOS: keeps the localtunnel to Ollama alive and syncs OLLAMA_HOST to Secret Manager."

# pythonw.exe drops inherited handles, so pass --log-file directly; no shell redirect needed
$CmdArgs = "`"$ScriptPath`" " +
           "--port $Port --project $Project --retry-delay $RetryDelaySec " +
           "--subdomain $Subdomain " +
           "--log-file `"$LogFile`""

$Action  = New-ScheduledTaskAction `
    -Execute  $PythonExe `
    -Argument $CmdArgs `
    -WorkingDirectory $RepoRoot

# Two triggers: one at startup (no user needed), one at logon (catches re-logins).
# Both are needed because AtLogOn never fires if the machine doesn't reboot after task
# registration — and AtStartup alone would miss cases where only the user re-logs.
$TriggerLogon  = New-ScheduledTaskTrigger -AtLogOn
$TriggerBoot   = New-ScheduledTaskTrigger -AtStartup

$Settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 0) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -MultipleInstances IgnoreNew

# ── Register (replace if exists) ──────────────────────────────────────────
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "[task] Removed existing task '$TaskName'"
}

Register-ScheduledTask `
    -TaskName    $TaskName `
    -Description $TaskDesc `
    -Action      $Action `
    -Trigger     @($TriggerLogon, $TriggerBoot) `
    -Settings    $Settings `
    -RunLevel    Limited | Out-Null   # runs as current user, no elevation needed

Write-Host ""
Write-Host "[task] Registered: $TaskName"
Write-Host "       Python:     $PythonExe"
Write-Host "       Script:     $ScriptPath"
Write-Host "       Log file:   $LogFile"
Write-Host "       Port:       $Port"
Write-Host "       Project:    $Project"
Write-Host ""
Write-Host "The tunnel will start automatically at your next login OR next reboot."
Write-Host "Starting task now..."
try {
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    Write-Host "[task] Started: $TaskName"
} catch {
    Write-Host "[task] Could not start immediately (may already be running): $_"
}
Write-Host "To restart manually:"
Write-Host "    Stop-ScheduledTask -TaskName '$TaskName' -ErrorAction SilentlyContinue; Start-ScheduledTask -TaskName '$TaskName'"
Write-Host ""
Write-Host "To tail the log:"
Write-Host "    Get-Content '$LogFile' -Wait"
Write-Host ""
Write-Host "To unregister:"
Write-Host "    Unregister-ScheduledTask -TaskName '$TaskName' -Confirm:`$false"
