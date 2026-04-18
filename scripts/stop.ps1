# Stop Hub Cowork
# Kills only the pythonw.exe processes whose command line targets
# meeting_agent.py in this repo. This way the upstream hub-se-agent (or
# any other pythonw app) keeps running.

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$repoRoot  = Resolve-Path (Join-Path $scriptDir "..") | Select-Object -ExpandProperty Path
# Match both `-m hub_cowork` and the legacy `meeting_agent.py` script path.
$legacyTarget = Join-Path $repoRoot "src\hub_cowork\host\meeting_agent.py"

$stopped = $false
Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -and (
            $_.CommandLine -like "*-m hub_cowork*" -or
            $_.CommandLine -like "*$legacyTarget*"
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
