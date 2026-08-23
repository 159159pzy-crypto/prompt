param([int]$Port = 8191)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
py -3.11 -m uvicorn backend.app:app --host 127.0.0.1 --port $Port

