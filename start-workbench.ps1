param(
    [int]$Port = 8191,
    [switch]$NoBrowser,
    [switch]$SkipInstall
)

$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot

$url = "http://127.0.0.1:$Port"
$outputDir = Join-Path $PSScriptRoot 'output'
$stdoutLogPath = Join-Path $outputDir 'workbench-server.out.log'
$stderrLogPath = Join-Path $outputDir 'workbench-server.err.log'

function Test-WorkbenchHealth {
    param([string]$Address)
    try {
        $response = Invoke-WebRequest -Uri "$Address/api/status" -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -eq 200
    } catch {
        return $false
    }
}

if (Test-WorkbenchHealth -Address $url) {
    $existingWorker = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'py.exe'" | Where-Object { $_.CommandLine -match 'backend\.worker' }
    if (-not $existingWorker) {
        if (Get-Command py -ErrorAction SilentlyContinue) { Start-Process -FilePath (Get-Command py).Source -ArgumentList '-3.11 -m backend.worker' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden | Out-Null }
        elseif (Get-Command python -ErrorAction SilentlyContinue) { Start-Process -FilePath (Get-Command python).Source -ArgumentList '-m backend.worker' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden | Out-Null }
    }
    Write-Host "Anima Prompt Studio is already running: $url"
    if (-not $NoBrowser) {
        Start-Process -FilePath 'explorer.exe' -ArgumentList $url | Out-Null
    }
    exit 0
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    throw "Port $Port is already in use by another process."
}

$python = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $python = (Get-Command py).Source
    $pythonArgs = @('-3.11')
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $python = (Get-Command python).Source
    $pythonArgs = @()
} else {
    throw 'Python was not found. Install Python 3.11 and ensure py or python is on PATH.'
}

if (-not $SkipInstall) {
    & $python @pythonArgs -c 'import fastapi, uvicorn, httpx' 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Installing runtime dependencies...'
        & $python @pythonArgs -m pip install -r (Join-Path $PSScriptRoot 'requirements.txt')
        if ($LASTEXITCODE -ne 0) {
            throw 'Dependency installation failed. See the pip error above.'
        }
    }
}

New-Item -ItemType Directory -Path $outputDir -Force | Out-Null
$uvicornArgs = @('-m', 'uvicorn', 'backend.app:app', '--host', '127.0.0.1', '--port', "$Port")
$process = Start-Process -FilePath $python -ArgumentList (@($pythonArgs) + $uvicornArgs) -WorkingDirectory $PSScriptRoot -RedirectStandardOutput $stdoutLogPath -RedirectStandardError $stderrLogPath -PassThru
$workerOut = Join-Path $outputDir 'workbench-worker.out.log'
$workerErr = Join-Path $outputDir 'workbench-worker.err.log'
$workerArgs = @($pythonArgs) + @('-m', 'backend.worker')
$worker = Start-Process -FilePath $python -ArgumentList $workerArgs -WorkingDirectory $PSScriptRoot -RedirectStandardOutput $workerOut -RedirectStandardError $workerErr -WindowStyle Hidden -PassThru

$ready = $false
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    Start-Sleep -Milliseconds 500
    if ($process.HasExited) {
        $details = @()
        if (Test-Path -LiteralPath $stderrLogPath) { $details += Get-Content -LiteralPath $stderrLogPath -Tail 20 -ErrorAction SilentlyContinue }
        if (Test-Path -LiteralPath $stdoutLogPath) { $details += Get-Content -LiteralPath $stdoutLogPath -Tail 20 -ErrorAction SilentlyContinue }
        throw "Service failed to start. $($details -join ' ')"
    }
    if (Test-WorkbenchHealth -Address $url) {
        $ready = $true
        break
    }
}

if (-not $ready) {
    if ($worker -and -not $worker.HasExited) { Stop-Process -Id $worker.Id -Force -ErrorAction SilentlyContinue }
    throw "Service did not become ready within 15 seconds. Logs: $stdoutLogPath and $stderrLogPath"
}

Write-Host "Anima Prompt Studio started: $url"
if (-not $NoBrowser) {
    Start-Process -FilePath 'explorer.exe' -ArgumentList $url | Out-Null
}
