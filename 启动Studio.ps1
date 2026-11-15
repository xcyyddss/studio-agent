param(
    [ValidateRange(1024, 65535)][int]$Port = 8766,
    [string]$DataRoot = (Join-Path $PSScriptRoot 'studio-data'),
    [ValidateRange(1, 32)][int]$Workers = 6
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$env:PYTHONIOENCODING = 'utf-8'
python -B -m studio_runtime --data-root $DataRoot --workers $Workers serve --port $Port
