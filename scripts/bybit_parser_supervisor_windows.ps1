param(
    [Parameter(Mandatory=$true)][string]$VaultPath,
    [Parameter(Mandatory=$true)][string]$EnvironmentPath,
    [Parameter(Mandatory=$true)][string]$Image
)
$ErrorActionPreference = 'Stop'
# Run as the DPAPI owner in an interactive logon. No password or API pair is
# supplied to Task Scheduler, process arguments, Docker env or persistent files.
$mutex = [Threading.Mutex]::new($false, 'Local\OzarkPay.Bybit.ReadOnly.Supervisor')
if (-not $mutex.WaitOne(0)) { $mutex.Dispose(); exit 0 }
$wrapper = Join-Path $PSScriptRoot 'bybit_quote_worker_windows.ps1'
$child = $null
try {
    foreach ($source in @($VaultPath,$EnvironmentPath,$wrapper)) {
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw 'Parser source missing' }
    }
    while ($true) {
        # The task only recreates its stopped worker. Provider outages mark
        # health unavailable and are retried by the worker, never restart storms.
        $raw = & docker inspect ozark-bybit-parser --format '{{json .}}' 2>$null
        if ($LASTEXITCODE -eq 0) {
            $container = $raw | ConvertFrom-Json
            if ($container.Config.Labels.'com.ozarkpay.service' -ne 'bybit-readonly') {
                throw 'Container ownership label mismatch'
            }
            if ($container.State.Running) { Start-Sleep -Seconds 15; continue }
            if ($child -and -not $child.HasExited) { Start-Sleep -Seconds 5; continue }
            & docker rm ozark-bybit-parser 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { Start-Sleep -Seconds 15; continue }
        } else {
            & docker info --format '{{.ServerVersion}}' 2>$null | Out-Null
            if ($LASTEXITCODE -ne 0) { Start-Sleep -Seconds 15; continue }
        }
        if ($child -and -not $child.HasExited) { Start-Sleep -Seconds 5; continue }
        # Arguments contain paths and an image ID only, never decrypted secrets.
        $arguments = @('-NoProfile','-NonInteractive','-File',('"'+$wrapper+'"'),
            '-VaultPath',('"'+$VaultPath+'"'),'-EnvironmentPath',('"'+$EnvironmentPath+'"'),
            '-Image',('"'+$Image+'"'))
        $child = Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList $arguments -WindowStyle Hidden -PassThru
        Start-Sleep -Seconds 30
    }
} finally {
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}
