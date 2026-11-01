# ==============================================================================
# The desk: what is waiting for you, plus terminals that show the project running.
#
#   ./desk                 open it now: one Windows Terminal window with three panes
#                            left   the live dashboard            (tui.bat)
#                            top    what is waiting for you       (pending.py, refreshed every minute)
#                            bottom the daemon: starts it, then follows its log
#   ./setup -EnableDesk    open it at every Windows login      (./setup -DisableDesk to stop)
#
# The bottom pane starts the services itself, and only once Ollama answers: the daemon checks the models a single time, at
# start, so one that starts before Ollama is ready runs the whole session without its extra models.
# Add -DaemonOnly to leave the REST API and Langflow out.
# ==============================================================================

param (
    [ValidateSet("Launch", "Daemon", "Pending")][string]$Pane = "Launch",
    [switch]$DaemonOnly,
    [int]$WaitMinutes = 10
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ProjectDir
$Self = Join-Path $ProjectDir "desk.ps1"
$Setup = Join-Path $ProjectDir "setup.ps1"
$LogFile = Join-Path $ProjectDir "daemon.log"

function Test-Ollama {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null
        return $true
    } catch {
        return $false
    }
}

function Get-DaemonProcess {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe' AND CommandLine LIKE '%daemon.py%'" -ErrorAction SilentlyContinue | Select-Object -First 1
}

# --------------------------------------------------------------------------- pane: what is waiting for you
if ($Pane -eq "Pending") {
    $Host.UI.RawUI.WindowTitle = "Auto-Applier - Pending"
    python pending.py --watch 60
    exit 0
}

# --------------------------------------------------------------------------- pane: start the daemon, follow its log
if ($Pane -eq "Daemon") {
    $Host.UI.RawUI.WindowTitle = "Auto-Applier - Daemon"
    Write-Host "Auto-Applier daemon" -ForegroundColor Cyan

    $deadline = (Get-Date).AddMinutes($WaitMinutes)
    $up = Test-Ollama
    if (-not $up) {
        Write-Host "Waiting for Ollama (the daemon must not start before it)..." -ForegroundColor Yellow -NoNewline
        while (-not $up -and (Get-Date) -lt $deadline) {
            Start-Sleep -Seconds 3
            $up = Test-Ollama
            Write-Host "." -NoNewline
        }
        Write-Host ""
    }
    if ($up) {
        Write-Host "Ollama is up." -ForegroundColor Green
    } else {
        Write-Host "Ollama did not answer within $WaitMinutes minutes. Starting anyway: the extra models will not be used until you restart the daemon (setup.bat -StopAll, then setup.bat -All -Background)." -ForegroundColor Red
    }

    $flags = @{ Background = $true; SkipChecks = $true }
    if ($DaemonOnly) { $flags.Daemon = $true } else { $flags.All = $true }
    for ($attempt = 1; $attempt -le 6; $attempt++) {
        & $Setup @flags
        Start-Sleep -Seconds 20
        if (Get-DaemonProcess) { break }
        # After a quick restart the previous daemon's lock is still "fresh" for two minutes, and a new daemon refuses to start under it.
        Write-Host "The daemon is not running yet (a lock from before the restart can last two minutes). Trying again in 40 s ($attempt of 6)..." -ForegroundColor Yellow
        Start-Sleep -Seconds 40
    }
    if (-not (Get-DaemonProcess)) {
        Write-Host "The daemon did not start. Read the lines below, or run: setup.bat -Status" -ForegroundColor Red
    }

    for ($i = 0; $i -lt 30 -and -not (Test-Path $LogFile); $i++) { Start-Sleep -Seconds 1 }
    Write-Host "`nFollowing daemon.log (Ctrl+C stops following, not the daemon)`n" -ForegroundColor DarkGray
    if (Test-Path $LogFile) { Get-Content -Wait -Tail 40 $LogFile } else { Write-Host "No daemon.log yet." -ForegroundColor Yellow }
    exit 0
}

# --------------------------------------------------------------------------- launch the window
$daemonArgs = "-Pane Daemon" + $(if ($DaemonOnly) { " -DaemonOnly" } else { "" })
$ps = "powershell.exe -NoLogo -NoProfile -NoExit -ExecutionPolicy Bypass -File `"$Self`""

if (Get-Command wt.exe -ErrorAction SilentlyContinue) {
    # ' ; ' separates Windows Terminal commands: a new tab, then two splits (the right half, then the right half's lower part)
    $wtArgs = @(
        "-w new --maximized new-tab --title `"Dashboard`" --suppressApplicationTitle -d `"$ProjectDir`" cmd.exe /c .\tui.bat",
        "split-pane -V --size 0.42 --title `"Pending`" --suppressApplicationTitle -d `"$ProjectDir`" $ps -Pane Pending",
        "split-pane -H --size 0.4 --title `"Daemon`" --suppressApplicationTitle -d `"$ProjectDir`" $ps $daemonArgs"
    ) -join " ; "
    Start-Process -FilePath "wt.exe" -ArgumentList $wtArgs
    Write-Host "Desk opened in Windows Terminal." -ForegroundColor Green
} else {
    # No Windows Terminal: three ordinary windows
    Start-Process -FilePath "cmd.exe" -ArgumentList @("/c", ".\tui.bat") -WorkingDirectory $ProjectDir
    Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoLogo", "-NoProfile", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", $Self, "-Pane", "Pending") -WorkingDirectory $ProjectDir
    $daemonList = @("-NoLogo", "-NoProfile", "-NoExit", "-ExecutionPolicy", "Bypass", "-File", $Self, "-Pane", "Daemon")
    if ($DaemonOnly) { $daemonList += "-DaemonOnly" }
    Start-Process -FilePath "powershell.exe" -ArgumentList $daemonList -WorkingDirectory $ProjectDir
    Write-Host "Desk opened in three windows (install Windows Terminal for a single tiled window)." -ForegroundColor Green
}
