param([int]$Port = 8191)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
Start-Process -FilePath 'py' -ArgumentList '-3.11 -m backend.worker' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden | Out-Null
py -3.11 -m uvicorn backend.app:app --host 127.0.0.1 --port $Port
