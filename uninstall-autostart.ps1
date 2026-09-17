<#!
.SYNOPSIS
  Remove repo-monitor from the current user's Windows logon startup.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$Startup = [Environment]::GetFolderPath('Startup')
$ShortcutPath = Join-Path $Startup 'repo-monitor.lnk'
if (Test-Path $ShortcutPath) {
  Remove-Item $ShortcutPath -Force
  Write-Output "Removed Startup shortcut: $ShortcutPath"
}
