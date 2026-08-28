param([string]$ExePath = '')

$ErrorActionPreference = 'Stop'
$exe = if ($ExePath) { $ExePath } else { Join-Path $PSScriptRoot 'dist\AnimaPromptStudio.exe' }
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Anima Prompt Studio.lnk'
$oldShortcut = Join-Path $desktop 'Anima Agent Prompt Studio.lnk'
$oldStopShortcut = Join-Path $desktop 'Anima Prompt Studio - Stop.lnk'

if (-not (Test-Path -LiteralPath $exe)) { throw "EXE not found: $exe. Build it first with .\Build-AnimaPromptStudioLauncher.ps1" }
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $exe
$shortcut.Arguments = ''
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = 'Launch Anima Agent Prompt Studio'
$shortcut.IconLocation = "$exe,0"
$shortcut.Save()
foreach ($legacy in @($oldShortcut, $oldStopShortcut)) { if ((Test-Path -LiteralPath $legacy) -and ($legacy -ne $shortcutPath)) { Remove-Item -LiteralPath $legacy -Force } }
Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\ie4uinit.exe') -ArgumentList '-show' -WindowStyle Hidden

Write-Output "ANIMA_SHORTCUT_INSTALL_OK=$shortcutPath"
