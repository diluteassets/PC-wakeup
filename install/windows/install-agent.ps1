<#
.SYNOPSIS
    Installs the pcwake agent as a scheduled task that starts at logon.

.DESCRIPTION
    A per-user task in the interactive session, not a SYSTEM service. That is
    a deliberate trade-off: LockWorkStation only works from an interactive
    session, so a SYSTEM service could not implement /lock. The cost is that
    the agent is not running between boot and logon, so the PC reads as
    offline during that window. Waking is unaffected -- that is the NIC's
    job, not the agent's.

    Run from an elevated PowerShell prompt in the repository root.

.EXAMPLE
    .\install\windows\install-agent.ps1 -ConfigPath C:\pcwake\config.toml
#>
[CmdletBinding()]
param(
    [string]$ConfigPath = "$env:ProgramData\pcwake\config.toml",
    [string]$PythonPath = "",
    [string]$TaskName = "pcwake-agent"
)

$ErrorActionPreference = "Stop"

if (-not (([Security.Principal.WindowsPrincipal] `
        [Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator))) {
    throw "Run this from an elevated PowerShell prompt."
}

if (-not $PythonPath) {
    $found = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $found) { $found = Get-Command python.exe -ErrorAction SilentlyContinue }
    if (-not $found) { throw "No Python found on PATH. Pass -PythonPath explicitly." }
    $PythonPath = $found.Source
}

if (-not (Test-Path $ConfigPath)) {
    throw "No config at $ConfigPath. Copy config.example.toml there and edit it first."
}

Write-Host "Python: $PythonPath"
Write-Host "Config: $ConfigPath"

# pythonw keeps the agent from flashing a console window at every logon.
$arguments = "-m pcwake.agent --config `"$ConfigPath`""
$action = New-ScheduledTaskAction -Execute $PythonPath -Argument $arguments
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Highest privileges so shutdown.exe does not prompt. Interactive so
# LockWorkStation has a session to lock.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Highest

# The defaults would stop the agent after three days and refuse to start it
# on battery -- both wrong for something meant to run continuously.
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -DontStopOnIdleEnd `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Removing the existing task first."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $action `
    -Trigger $trigger `
    -Principal $principal `
    -Settings $settings `
    -Description "pcwake agent: reports presence and performs power commands." | Out-Null

Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Installed and started '$TaskName'."
Write-Host "Check it:   Get-ScheduledTask -TaskName $TaskName"
Write-Host "Remove it:  Unregister-ScheduledTask -TaskName $TaskName"
Write-Host ""
Write-Host "Now run the setup checks:"
Write-Host "  python -m pcwake.agent --config `"$ConfigPath`" doctor"
