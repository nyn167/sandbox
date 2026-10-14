# One-time: run the sandbox server + bots at every log on (and now). PAPER MONEY ONLY.
#   powershell -ExecutionPolicy Bypass -File C:\sandbox\windows\install-tasks.ps1
# Remove: Unregister-ScheduledTask -TaskName "NYN Sandbox Server","NYN Sandbox Bots" -Confirm:$false
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
foreach ($t in @(@("NYN Sandbox Server", "start-server.bat"), @("NYN Sandbox Bots", "start-bots.bat"))) {
    $action = New-ScheduledTaskAction -Execute (Join-Path $root "windows\$($t[1])") -WorkingDirectory $root
    Register-ScheduledTask -TaskName $t[0] -Action $action -Trigger $trigger -Settings $settings `
        -Description "NYN paper-trading sandbox (paper money only, never real orders)" -Force | Out-Null
    Start-ScheduledTask -TaskName $t[0]
    Write-Host "Installed and started $($t[0])"
}
