# Stop Hub Cowork
# Kills only the pythonw.exe processes that launched this repo's
# `hub_cowork` package. This way the upstream hub-se-agent (or any other
# pythonw app) keeps running.

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot  = Resolve-Path (Join-Path $scriptDir "..") | Select-Object -ExpandProperty Path
# Match `-m hub_cowork`, plus the legacy direct-script paths from earlier
# project layouts so an old running instance can still be cleaned up.
$legacyMeeting = Join-Path $repoRoot "src\hub_cowork\host\meeting_agent.py"
$legacyDesktop = Join-Path $repoRoot "src\hub_cowork\host\desktop_host.py"

$stopped = $false
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and (
            $_.CommandLine -like "*-m hub_cowork*" -or
            $_.CommandLine -like "*$legacyMeeting*" -or
            $_.CommandLine -like "*$legacyDesktop*"
        )
    } |
    ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
        $stopped = $true
    }

if ($stopped) {
    Write-Host "Hub Cowork stopped." -ForegroundColor Yellow
} else {
    Write-Host "Hub Cowork is not running." -ForegroundColor Gray
}
