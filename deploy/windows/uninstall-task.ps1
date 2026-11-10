<#
.SYNOPSIS
  Stop and remove the ATW Windows scheduled task.  Data is preserved.
#>
[CmdletBinding()]
param(
    [string]$TaskName = "ATW",
    [string]$AtwHome = "",
    [switch]$Purge
)
$ErrorActionPreference = "Stop"
if (-not $AtwHome) { $AtwHome = Join-Path $env:LOCALAPPDATA "ATW" }

$service = New-Object -ComObject Schedule.Service
$service.Connect()
$folder = $service.GetFolder("\")
$removed = $false
try {
    $task = $folder.GetTask($TaskName)
    try { $task.Stop(0) } catch { }
    $folder.DeleteTask($TaskName, 0)
    $removed = $true
    Write-Host "removed scheduled task '$TaskName'"
} catch [System.Runtime.InteropServices.COMException] {
    Write-Host "scheduled task '$TaskName' not found"
}
if ($Purge) {
    Remove-Item -Recurse -Force $AtwHome -ErrorAction SilentlyContinue
    Write-Host "purged $AtwHome"
} else {
    Write-Host "ATW data preserved at $AtwHome"
}
