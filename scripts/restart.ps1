# Restart Hub Cowork (stop + start). Convenience wrapper.
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
& (Join-Path $scriptDir "stop.ps1")
Start-Sleep -Milliseconds 500
& (Join-Path $scriptDir "start.ps1") -Force
