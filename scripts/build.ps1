# Build a one-folder PyInstaller bundle of Hub Cowork.
#
# Usage:
#   .\scripts\build.ps1               # incremental
#   .\scripts\build.ps1 -Clean        # wipe build/ and dist/ first
#
# Output:
#   dist\HubCowork\HubCowork.exe      <- ship this folder
#
# After a successful build, smoke-test by running the EXE directly:
#   .\dist\HubCowork\HubCowork.exe
# It should behave identically to `python -m hub_cowork`.

param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$projectDir = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Push-Location $projectDir

try {
    $venvPython = Join-Path $projectDir ".venv\Scripts\python.exe"
    if (-not (Test-Path $venvPython)) {
        Write-Host "Virtualenv not found at $venvPython" -ForegroundColor Red
        Write-Host "Create one with:  python -m venv .venv ; .\.venv\Scripts\Activate.ps1 ; pip install -e ." -ForegroundColor Yellow
        exit 1
    }

    # Make sure PyInstaller is installed in the venv.
    & $venvPython -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Installing PyInstaller into the virtualenv..." -ForegroundColor Cyan
        & $venvPython -m pip install --upgrade pyinstaller
    }

    if ($Clean) {
        Write-Host "Cleaning build\ and dist\ ..." -ForegroundColor Cyan
        Remove-Item -Recurse -Force "build"  -ErrorAction SilentlyContinue
        Remove-Item -Recurse -Force "dist"   -ErrorAction SilentlyContinue
    }

    # Stop any running instance — PyInstaller cannot overwrite a locked EXE.
    $existing = Get-CimInstance Win32_Process -Filter "Name = 'HubCowork.exe'" -ErrorAction SilentlyContinue
    if ($existing) {
        Write-Host "Stopping running HubCowork.exe (PID $($existing.ProcessId -join ', '))" -ForegroundColor Yellow
        $existing | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 500
    }

    Write-Host "Building HubCowork (one-folder)..." -ForegroundColor Cyan
    & $venvPython -m PyInstaller hub_cowork.spec --noconfirm

    if ($LASTEXITCODE -ne 0) {
        Write-Host "PyInstaller build failed." -ForegroundColor Red
        exit $LASTEXITCODE
    }

    $exe = Join-Path $projectDir "dist\HubCowork\HubCowork.exe"
    if (Test-Path $exe) {
        $size = "{0:N1} MB" -f ((Get-Item $exe).Length / 1MB)
        Write-Host ""
        Write-Host "Build OK." -ForegroundColor Green
        Write-Host "  EXE:    $exe  ($size)" -ForegroundColor Green
        Write-Host "  Folder: $(Split-Path -Parent $exe)" -ForegroundColor Green
        Write-Host ""
        Write-Host "Smoke-test:  & '$exe'" -ForegroundColor Gray
    } else {
        Write-Host "Build reported success but EXE not found at $exe" -ForegroundColor Red
        exit 1
    }
}
finally {
    Pop-Location
}
