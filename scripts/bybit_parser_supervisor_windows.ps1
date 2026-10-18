param(
    [Parameter(Mandatory=$true)][string]$VaultPath,
    [Parameter(Mandatory=$true)][string]$EnvironmentPath,
    [Parameter(Mandatory=$true)][string]$Image
)
$ErrorActionPreference = 'Stop'
# Run as the DPAPI owner in an interactive logon. No password or API pair is
# supplied to Task Scheduler, process arguments, Docker env or persistent files.
$mutex = [Threading.Mutex]::new($false, 'Local\OzarkPay.Bybit.ReadOnly.Supervisor')
try { $owned = $mutex.WaitOne(0) }
catch [Threading.AbandonedMutexException] { $owned = $true }
if (-not $owned) { $mutex.Dispose(); exit 0 }
$wrapper = Join-Path $PSScriptRoot 'bybit_quote_worker_windows.ps1'
$child = $null

function Invoke-ParserDocker([string[]]$DockerArguments) {
    # Docker CLI itself can hang across a Desktop restart. Bound only our own
    # read/control process; never enumerate or stop another project's processes.
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = (Get-Command docker.exe -CommandType Application | Select-Object -First 1).Source
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($argument in $DockerArguments) { $start.ArgumentList.Add($argument) }
    $process = [Diagnostics.Process]::Start($start)
    try {
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(15000)) {
            $process.Kill($true)
            $process.WaitForExit()
            return @{ ExitCode = -1; Output = '' }
        }
        return @{ ExitCode = $process.ExitCode; Output = $stdout.GetAwaiter().GetResult() }
    } finally { $process.Dispose() }
}

function Stop-ParserChild {
    # This Process object is exclusively the wrapper started below. Kill its
    # tree only after Docker proves the owned container stopped or absent.
    if ($child -and -not $child.HasExited) {
        $child.Kill($true)
        $child.WaitForExit()
    }
}
try {
    foreach ($source in @($VaultPath,$EnvironmentPath,$wrapper)) {
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) { throw 'Parser source missing' }
    }
    while ($true) {
        # The task only recreates its stopped worker. Provider outages mark
        # health unavailable and are retried by the worker, never restart storms.
        $inspection = Invoke-ParserDocker -DockerArguments @('inspect','ozark-bybit-parser','--format','{{json .}}')
        if ($inspection.ExitCode -eq 0) {
            $container = $inspection.Output | ConvertFrom-Json
            if ($container.Config.Labels.'com.ozarkpay.service' -ne 'bybit-readonly') {
                throw 'Container ownership label mismatch'
            }
            if ($container.State.Running) { Start-Sleep -Seconds 15; continue }
            Stop-ParserChild
            $removed = Invoke-ParserDocker -DockerArguments @('rm','ozark-bybit-parser')
            if ($removed.ExitCode -ne 0) { Start-Sleep -Seconds 15; continue }
        } else {
            $engine = Invoke-ParserDocker -DockerArguments @('info','--format','{{.ServerVersion}}')
            if ($engine.ExitCode -ne 0) { Start-Sleep -Seconds 15; continue }
            if ($child -and -not $child.HasExited -and
                    ((Get-Date)-$child.StartTime).TotalSeconds -lt 60) {
                Start-Sleep -Seconds 5; continue
            }
            Stop-ParserChild
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
