param([int]$Port = 8191)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$LogDirectory = Join-Path $Root 'data'
$LogPath = Join-Path $LogDirectory 'launcher.log'

try {
    $connections = @(Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
    $processIds = @($connections | Select-Object -ExpandProperty OwningProcess -Unique)
    if (-not $processIds) {
        Write-Output "ANIMA_STOP_NOT_RUNNING=127.0.0.1:$Port"
        exit 0
    }

    foreach ($processId in $processIds) {
        $process = Get-Process -Id $processId -ErrorAction SilentlyContinue
        if ($process) {
            Stop-Process -Id $processId -Force
            Write-Output "ANIMA_STOPPED_PID=$processId"
        }
    }
    try {
        New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
        Add-Content -LiteralPath $LogPath -Value ("{0} stopped port={1}" -f (Get-Date -Format 's'), $Port) -Encoding UTF8
    } catch {
        # Logging must never prevent shutdown.
    }
    exit 0
} catch {
    Write-Error ("Unable to stop Anima Prompt Studio: " + $_.Exception.Message)
    exit 1
}
