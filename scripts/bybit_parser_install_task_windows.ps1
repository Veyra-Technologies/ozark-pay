param(
    [Parameter(Mandatory=$true)][string]$VaultPath,
    [Parameter(Mandatory=$true)][string]$EnvironmentPath,
    [Parameter(Mandatory=$true)][string]$Image
)
$ErrorActionPreference='Stop'
$supervisor=Join-Path $PSScriptRoot 'bybit_parser_supervisor_windows.ps1'
foreach($path in @($supervisor,$VaultPath,$EnvironmentPath)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf) -or $path.Contains('"')) { throw 'Invalid parser path' }
}
if($Image -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Pin the validated image digest' }
$owner=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$exe=(Get-Process -Id $PID).Path
$arguments='-NoProfile -NonInteractive -WindowStyle Hidden -File "'+$supervisor+'" -VaultPath "'+$VaultPath+'" -EnvironmentPath "'+$EnvironmentPath+'" -Image '+$Image
$action=New-ScheduledTaskAction -Execute $exe -Argument $arguments
$trigger=New-ScheduledTaskTrigger -AtLogOn -User $owner
$principal=New-ScheduledTaskPrincipal -UserId $owner -LogonType Interactive -RunLevel Limited
$settings=New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName 'OzarkPay-Bybit-ReadOnly' -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description 'Owner DPAPI read-only quote supervisor; no payment admission or trade permission' -Force | Out-Null
Start-ScheduledTask -TaskName 'OzarkPay-Bybit-ReadOnly'
Write-Output 'OzarkPay owner-logon read-only parser task registered and started'
