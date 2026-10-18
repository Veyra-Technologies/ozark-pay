param(
    [Parameter(Mandatory=$true)][string]$VaultPath,
    [Parameter(Mandatory=$true)][string]$EnvironmentPath,
    [Parameter(Mandatory=$true)][string]$Image,
    [string]$ContainerName = 'ozark-bybit-parser',
    [switch]$Activate
)
$ErrorActionPreference = 'Stop'
if ($ContainerName -notmatch '^ozark-[a-zA-Z0-9_.-]+$') { throw 'Expected an OzarkPay container' }
$resolvedVault = (Resolve-Path -LiteralPath $VaultPath).Path
$resolvedEnvironment = (Resolve-Path -LiteralPath $EnvironmentPath).Path
$payload = ConvertFrom-SecureString (ConvertTo-SecureString (Get-Content -LiteralPath $resolvedVault -Raw)) -AsPlainText
$workerArgs = @('python','-B','-m','scripts.bybit_quote_worker')
if ($Activate) { $workerArgs += '--activate' }
try {
    $payload | docker run -i --name $ContainerName --network ozark-tj-qa-20261017_default --env-file $resolvedEnvironment --memory 384m --cpus 1 --log-driver local --log-opt max-size=1m --log-opt max-file=1 --log-opt compress=false $Image @workerArgs
    if ($LASTEXITCODE -ne 0) { throw 'Read-only quote worker stopped' }
} finally {
    Remove-Variable payload -ErrorAction SilentlyContinue
}
