$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path $PSScriptRoot 'start-workbench.ps1'
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop 'Anima Agent Prompt Studio.lnk'
$powershell = Join-Path $env:WINDIR 'System32\WindowsPowerShell\v1.0\powershell.exe'

$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $powershell
$shortcut.Arguments = "-NoLogo -NoProfile -ExecutionPolicy Bypass -File `"$scriptPath`""
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.Description = 'Launch Anima Agent Prompt Studio'
$shortcut.IconLocation = "$powershell,0"
$shortcut.Save()

Write-Host "Created desktop shortcut: $shortcutPath"
