<#!
.SYNOPSIS
  Control the repo-monitor daemon.
.EXAMPLE
  .\monitor.ps1 -Action start
  .\monitor.ps1 -Action status
  .\monitor.ps1 -Action run-now
#>
[CmdletBinding()]
param(
  [ValidateSet('start', 'stop', 'status', 'run-now', 'report')]
  [string]$Action = 'status'
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Monitor = Join-Path $Root 'monitor.py'
$ConfigPath = Join-Path $Root 'monitor.config.json'
$PidPath = Join-Path $Root 'data\daemon.pid'

function Get-MonitorPython {
  if (Test-Path $ConfigPath) {
    $config = Get-Content -Raw -Encoding UTF8 $ConfigPath | ConvertFrom-Json
    $configured = [string]$config.python_exe
    if ($configured) { return $configured }
  }
  return (Get-Command python.exe -ErrorAction Stop).Source
}

function Get-Pythonw([string]$Python) {
  if ($Python -match '^(python|python\.exe|python3)$') { return 'pythonw.exe' }
  $candidate = Join-Path (Split-Path -Parent $Python) 'pythonw.exe'
  if (Test-Path $candidate) { return $candidate }
  return 'pythonw.exe'
}

function Read-DaemonPid {
  if (-not (Test-Path $PidPath)) { return $null }
  try { return [int](Get-Content -Raw -Encoding ASCII $PidPath).Trim() }
  catch { return $null }
}

function Invoke-Monitor([string[]]$Arguments) {
  $python = Get-MonitorPython
  & $python $Monitor @Arguments
  exit $LASTEXITCODE
}

New-Item -ItemType Directory -Force (Join-Path $Root 'data') | Out-Null

switch ($Action) {
  'start' {
    $oldPid = Read-DaemonPid
    if ($oldPid) {
      $existing = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
      if ($existing) {
        Write-Output "daemon already running PID=$oldPid"
        exit 0
      }
    }
    $pythonw = Get-Pythonw (Get-MonitorPython)
    $process = Start-Process -FilePath $pythonw `
      -ArgumentList @('"' + $Monitor + '"', '--daemon') `
      -WorkingDirectory $Root -WindowStyle Hidden -PassThru
    Set-Content -Path $PidPath -Value ([string]$process.Id) -Encoding ASCII
    Write-Output "daemon started PID=$($process.Id)"
  }
  'stop' {
    $daemonPid = Read-DaemonPid
    if ($daemonPid) {
      Stop-Process -Id $daemonPid -Force -ErrorAction SilentlyContinue
      Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
      Write-Output "daemon stopped PID=$daemonPid"
    } else {
      Write-Output 'daemon is not running'
    }
  }
  'status' { Invoke-Monitor @('status') }
  'run-now' { Invoke-Monitor @('--once') }
  'report' { Invoke-Monitor @('report') }
}
