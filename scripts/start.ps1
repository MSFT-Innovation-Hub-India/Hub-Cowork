# Start Hub Cowork (invisible, detached from this terminal).
# Use -Force to kill any existing instances and start fresh.
param(
    [switch]$Force
)

$projectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$pythonw = Join-Path $projectDir ".venv\Scripts\pythonw.exe"

if (-not (Test-Path $pythonw)) {
    Write-Host "pythonw.exe not found at $pythonw" -ForegroundColor Red
    exit 1
}

# Find any already-running instances of this app.
$existing = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*-m hub_cowork*" }

if ($existing) {
    if ($Force) {
        Write-Host "Stopping existing instance(s): PID $($existing.ProcessId -join ', ')" -ForegroundColor Yellow
        $existing | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 800
    } else {
        # Probe the WebSocket port to see if it's actually responsive.
        $alive = $false
        try {
            $tcp = New-Object System.Net.Sockets.TcpClient
            $iar = $tcp.BeginConnect("127.0.0.1", 18080, $null, $null)
            if ($iar.AsyncWaitHandle.WaitOne(500)) { $tcp.EndConnect($iar); $alive = $true }
            $tcp.Close()
        } catch { $alive = $false }

        if ($alive) {
            Write-Host "Hub Cowork is already running (PID $($existing.ProcessId -join ', '))." -ForegroundColor Yellow
            Write-Host "Look for the tray icon near the clock, or run with -Force to restart." -ForegroundColor Yellow
            exit 0
        } else {
            Write-Host "Found stale process(es) (PID $($existing.ProcessId -join ', ')) not responding on port 18080. Cleaning up..." -ForegroundColor Yellow
            $existing | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
            Start-Sleep -Milliseconds 800
        }
    }
}

# Launch the package (installed editable with `pip install -e .`, or with
# PYTHONPATH pointing at src/). Uses `-m hub_cowork` which dispatches to
# hub_cowork/__main__.py.
$env:PYTHONPATH = Join-Path $projectDir "src"
Start-Process -FilePath $pythonw -ArgumentList "-m","hub_cowork" -WorkingDirectory $projectDir -WindowStyle Hidden
Write-Host "Hub Cowork started. Look for the tray icon near the clock." -ForegroundColor Green
