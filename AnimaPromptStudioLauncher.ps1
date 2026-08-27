param(
    [int]$Port = 8191,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDirectory = Join-Path $Root 'data'
$LogPath = Join-Path $LogDirectory 'launcher.log'
$Url = "http://127.0.0.1:$Port"
$HealthUrl = "$Url/api/status"

function Write-LauncherLog([string]$Message) {
    try {
        New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
        Add-Content -LiteralPath $LogPath -Value ("{0} {1}" -f (Get-Date -Format 's'), $Message) -Encoding UTF8
    } catch {
        # Logging must never prevent the launcher from starting.
    }
}

function Test-Workbench([string]$Endpoint) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Endpoint -TimeoutSec 1
        return [int]$response.StatusCode -eq 200
    } catch {
        return $false
    }
}

try {
    Set-Location -LiteralPath $Root
    if (Test-Workbench $HealthUrl) {
        $existingWorker = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' OR Name = 'py.exe'" | Where-Object { $_.CommandLine -match 'backend\.worker' }
        if (-not $existingWorker) {
            $existingPython = Get-Command 'py.exe' -ErrorAction SilentlyContinue
            $workerPath = if ($existingPython) { $existingPython.Source } else { Join-Path $Root '.venv\Scripts\python.exe' }
            $workerArguments = if ($existingPython) { '-3.11 -m backend.worker' } else { '-m backend.worker' }
            Start-Process -FilePath $workerPath -ArgumentList $workerArguments -WorkingDirectory $Root -WindowStyle Hidden | Out-Null
        }
        Write-LauncherLog "Existing workbench detected at $Url"
        if (-not $NoBrowser) { Start-Process $Url }
        exit 0
    }

    $python = Get-Command 'py.exe' -ErrorAction SilentlyContinue
    if (-not $python) {
        $fallback = Join-Path $Root '.venv\Scripts\python.exe'
        if (Test-Path -LiteralPath $fallback) {
            $pythonPath = $fallback
            $arguments = "-m uvicorn backend.app:app --host 127.0.0.1 --port $Port"
        }
        else { throw 'Python launcher not found. Install Python 3.11 or create .venv.' }
    } else {
        $pythonPath = $python.Source
        $arguments = "-3.11 -m uvicorn backend.app:app --host 127.0.0.1 --port $Port"
    }

    $process = Start-Process -FilePath $pythonPath -ArgumentList $arguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    $workerArguments = if ($python) { '-3.11 -m backend.worker' } else { '-m backend.worker' }
    $worker = Start-Process -FilePath $pythonPath -ArgumentList $workerArguments -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    Write-LauncherLog "Started uvicorn pid=$($process.Id) port=$Port"

    $ready = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        Start-Sleep -Milliseconds 500
        if (Test-Workbench $HealthUrl) { $ready = $true; break }
    }
    if (-not $ready) { throw "Workbench did not become ready at $HealthUrl" }
    if (-not $NoBrowser) { Start-Process $Url }
    Write-LauncherLog "Workbench ready at $Url"
    exit 0
} catch {
    Write-LauncherLog ("Launcher failed: " + $_.Exception.Message)
    exit 1
}
