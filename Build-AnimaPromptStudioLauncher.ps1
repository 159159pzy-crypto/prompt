param([switch]$InstallShortcut)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Project = Join-Path $Root 'launcher\AnimaPromptStudioLauncher.csproj'
$Publish = Join-Path $Root 'launcher\bin\Release\net8.0-windows\win-x64\publish'
$Dist = Join-Path $Root 'dist'
$dotnet = Get-Command dotnet.exe -ErrorAction SilentlyContinue
$localDotnet = Join-Path $env:LOCALAPPDATA 'Codex\dotnet-sdk-8\dotnet.exe'
if (-not $dotnet -and (Test-Path -LiteralPath $localDotnet)) { $dotnet = Get-Command $localDotnet }
if (-not $dotnet) { throw 'dotnet SDK 8 is required.' }
$sdk = & $dotnet.Source --list-sdks 2>$null
if (-not $sdk -and (Test-Path -LiteralPath $localDotnet)) { $dotnet = Get-Command $localDotnet; $sdk = & $dotnet.Source --list-sdks 2>$null }
if (-not $sdk) { throw 'dotnet SDK 8 is required; runtime alone is insufficient.' }
New-Item -ItemType Directory -Force -Path $Dist | Out-Null
& $dotnet.Source publish $Project -c Release -r win-x64 --self-contained true /p:PublishSingleFile=true /p:IncludeNativeLibrariesForSelfExtract=true /p:EnableCompressionInSingleFile=true
$source = Join-Path $Publish 'AnimaPromptStudio.exe'
if (-not (Test-Path -LiteralPath $source)) { throw "Publish output not found: $source" }
Copy-Item -LiteralPath $source -Destination (Join-Path $Dist 'AnimaPromptStudio.exe') -Force
Write-Output "ANIMA_EXE_BUILD_OK=$(Join-Path $Dist 'AnimaPromptStudio.exe')"
if ($InstallShortcut) { & (Join-Path $Root 'install-launcher-shortcut.ps1') -ExePath (Join-Path $Dist 'AnimaPromptStudio.exe') }
