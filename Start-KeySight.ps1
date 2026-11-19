# Start-KeySight.ps1 - one command: sets up Python, then starts KeySight.
#   .\Start-KeySight.ps1            asks how the phone should connect
#   .\Start-KeySight.ps1 -Tunnel    trusted HTTPS address (Cloudflare), no certificate warning
#   .\Start-KeySight.ps1 -Lan       self-signed HTTPS on your Wi-Fi, one warning on the phone
#   .\Start-KeySight.ps1 -NoBrowser do not open the HUD on this PC
# Double-click Start-KeySight.cmd instead if PowerShell blocks scripts.
param(
    [switch]$Tunnel,
    [switch]$Lan,
    [switch]$NoBrowser
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"

if ($Tunnel -and $Lan) { throw "Pick one: -Tunnel or -Lan." }

$py = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw "Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ and run this again."
    }
    Write-Host "First run: creating the Python environment..."
    python -m venv .venv
    & $py -m pip install -q -r requirements.txt
}

# One-time download of the hand-tracking files (skipped when already on disk).
& $py fetch_assets.py

# No mode given: offer the tunnel (the phone gets a real padlock) unless nobody is at the keyboard.
if (-not $Tunnel -and -not $Lan) {
    if ([Environment]::UserInteractive -and -not [Console]::IsInputRedirected) {
        Write-Host ""
        Write-Host "How should your phone connect?"
        Write-Host "  [T] Tunnel - trusted HTTPS address from Cloudflare, no certificate warning (recommended)"
        Write-Host "  [L] LAN    - your Wi-Fi only, the phone shows one certificate warning"
        $choice = Read-Host "Press Enter for Tunnel, or type L"
        if ($choice -match '^\s*[lL]') { $Lan = $true } else { $Tunnel = $true }
    } else {
        $Lan = $true
    }
}

$serverArgs = @()
if ($Tunnel) { $serverArgs += "--tunnel" } else { $serverArgs += "--lan" }
if ($NoBrowser) { $serverArgs += "--no-browser" }

& $py server.py @serverArgs
exit $LASTEXITCODE
