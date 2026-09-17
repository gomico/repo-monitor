<#!
.SYNOPSIS
  Install repo-monitor at the current user's Windows logon.
.DESCRIPTION
  Creates a Startup shortcut. If shortcut creation fails, the script throws
  and does not install a registry entry. A daemon is started immediately.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$MonitorControl = Join-Path $Root 'monitor.ps1'
$Startup = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $Startup 'repo-monitor.lnk'
$PowerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$Arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + $MonitorControl + '" -Action start'
try {
  $shell = New-Object -ComObject WScript.Shell
  $shortcut = $shell.CreateShortcut($ShortcutPath)
  $shortcut.TargetPath = $PowerShell
  $shortcut.Arguments = $Arguments
  $shortcut.WorkingDirectory = $Root
  $shortcut.WindowStyle = 7
  $shortcut.Save()
} catch {
  throw "Unable to create Startup shortcut '$ShortcutPath': $($_.Exception.Message)"
}
Write-Output "Startup shortcut: $ShortcutPath"

& $PowerShell -NoProfile -ExecutionPolicy Bypass -File $MonitorControl -Action start
if ($LASTEXITCODE -ne 0) { throw "Unable to start monitor daemon (exit $LASTEXITCODE)" }
