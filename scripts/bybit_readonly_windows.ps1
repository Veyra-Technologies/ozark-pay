param(
    [Parameter(Mandatory=$true)][string]$VaultPath,
    [Parameter(Mandatory=$true)][string]$ContainerName
)
$ErrorActionPreference = 'Stop'
if ($ContainerName -notmatch '^ozark-[a-zA-Z0-9_.-]+$') { throw 'Expected an OzarkPay container' }
$resolvedVault = (Resolve-Path -LiteralPath $VaultPath).Path
$sealed = Get-Content -LiteralPath $resolvedVault -Raw
$payload = ConvertFrom-SecureString (ConvertTo-SecureString $sealed) -AsPlainText
try {
    $payload | docker exec -i $ContainerName python -B -m scripts.bybit_readonly
    if ($LASTEXITCODE -ne 0) { throw 'Read-only Bybit verification did not complete' }
} finally {
    Remove-Variable payload,sealed -ErrorAction SilentlyContinue
}
