# ==============================================================================
# AI Auto Job Applier — Setup & Bootstrapper (PowerShell)
# Usage:
#   ./setup                       (Interactive menu)
#   ./setup -All -Background      (Start All-in-One: Daemon + API + Langflow)
#   ./setup -StopAll              (Stop all background services)
#   ./setup -EnableAutoStart      (Enable auto-start on Windows PC boot/login)
#   ./setup -DisableAutoStart     (Disable auto-start on Windows PC boot/login)
#   ./setup -Daemon -Background   (Start background detached daemon)
#   ./setup -StopDaemon           (Stop running background daemon)
#   ./setup -Status               (Check all services, autostart & log status)
#   ./setup -Logs                 (Stream live daemon logs)
#   ./setup -Langflow             (Launch Langflow Web UI)
#   ./setup -Api                  (Start local REST API on port 8000)
#   ./setup -DryRun               (Execute single dry-run cycle)
#   ./setup -Test                 (Execute test suite)
#   ./setup -SyncCookies          (Sync cookies from system Firefox profile)
#   ./setup -Browser "firefox"    (Specify browser engine)
# ==============================================================================

param (
    [switch]$All,
    [switch]$StopAll,
    [switch]$EnableAutoStart,
    [switch]$DisableAutoStart,
    [switch]$Langflow,
    [switch]$Daemon,
    [switch]$Background,
    [switch]$StopDaemon,
    [switch]$Status,
    [switch]$Logs,
    [switch]$DryRun,
    [switch]$Api,
    [switch]$Test,
    [switch]$SyncCookies,
    [string]$Login = "",
    [string]$Browser = "firefox"
)

$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ProjectDir

Write-Host "`n=======================================================" -ForegroundColor Cyan
Write-Host "   AI Parallel Auto Job Applier - Setup & Launcher" -ForegroundColor Cyan
Write-Host "=======================================================`n" -ForegroundColor Cyan

$isQuickOp = $Status -or $Logs -or $StopAll -or $StopDaemon -or $DisableAutoStart

if (-not $isQuickOp) {
    # 1. Verify Python
    Write-Host "[1/4] Verifying Python runtime..." -ForegroundColor Yellow
    $pyVersion = python --version 2>&1
    Write-Host "Found: $pyVersion" -ForegroundColor Green

    # 2. Check / Install Dependencies
    Write-Host "`n[2/4] Ensuring Python dependencies..." -ForegroundColor Yellow
    python -m pip install -r requirements.txt --quiet
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Warning: pip install encountered issues. Continuing..." -ForegroundColor Yellow
    }

    # 3. Ensure Playwright Browser (Firefox & Chromium)
    Write-Host "`n[3/4] Ensuring Playwright Firefox and Chromium browsers..." -ForegroundColor Yellow
    python -m playwright install firefox chromium 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Note: Playwright browser install completed or already up to date." -ForegroundColor DarkGray
    }

    # 4. Initialize Profile & Database
    Write-Host "`n[4/4] Verifying profile.json and state.db..." -ForegroundColor Yellow
    if (-not (Test-Path "profile.json")) {
        Write-Host "Generating profile.json from resume-template source of truth..." -ForegroundColor Green
        python profile_gen.py
    } else {
        Write-Host "profile.json is present." -ForegroundColor Green
    }

    if (-not (Test-Path "state.db")) {
        Write-Host "Initializing state.db database..." -ForegroundColor Green
        python -c "from state import StateStore; s = StateStore('state.db'); s.close()"
    } else {
        Write-Host "state.db is initialized." -ForegroundColor Green
    }
}

# Daemon & Service Process Management Helpers
function Start-DaemonProcess ([switch]$AsBackground, [string]$BrowserEngine = "firefox") {
    $pidFile = Join-Path $ProjectDir ".daemon.pid"
    if (Test-Path $pidFile) {
        $existingPid = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
            Write-Host "Daemon is already running in background (PID: $existingPid)." -ForegroundColor Yellow
            Write-Host "View logs with: ./setup -Logs" -ForegroundColor DarkGray
            return
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }

    if ($AsBackground) {
        Write-Host "`n[Action] Launching Auto Applier Daemon in background ($BrowserEngine)..." -ForegroundColor Cyan
        $proc = Start-Process python -ArgumentList @("-u", "daemon.py", "--browser", $BrowserEngine) -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru
        $proc.Id | Out-File $pidFile -Encoding ascii
        Write-Host "Daemon running in background (PID: $($proc.Id))." -ForegroundColor Green
        Write-Host " - Live logs: Get-Content -Wait -Tail 30 daemon.log  (or ./setup -Logs)" -ForegroundColor DarkGray
        Write-Host " - Stop daemon: ./setup -StopDaemon" -ForegroundColor DarkGray
    } else {
        Write-Host "`n[Action] Starting continuous Auto Applier Daemon in foreground ($BrowserEngine)..." -ForegroundColor Cyan
        python daemon.py --browser $BrowserEngine
    }
}

function Stop-DaemonProcess {
    $pidFile = Join-Path $ProjectDir ".daemon.pid"
    $stopped = $false
    if (Test-Path $pidFile) {
        $targetPid = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($targetPid) {
            $p = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
            if ($p) {
                Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
                Write-Host "Daemon (PID: $targetPid) stopped successfully." -ForegroundColor Green
                $stopped = $true
            }
        }
        Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
    }

    if (-not $stopped) {
        $found = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' AND CommandLine LIKE '%daemon.py%'" -ErrorAction SilentlyContinue
        if ($found) {
            foreach ($item in $found) {
                Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
                Write-Host "Stopped daemon process (PID: $($item.ProcessId))." -ForegroundColor Green
                $stopped = $true
            }
        }
    }

    if (-not $stopped -and (Test-Path "state.db")) {
        $dbLockPid = python -c "from state import StateStore; s = StateStore('state.db'); r = s.conn.execute('SELECT holder_pid FROM daemon_lock WHERE id = 1').fetchone(); print(r[0] if r else '')" 2>$null
        if ($dbLockPid -and ($dbLockPid.Trim() -ne "")) {
            $lockProc = Get-Process -Id ([int]$dbLockPid.Trim()) -ErrorAction SilentlyContinue
            if ($lockProc) {
                Stop-Process -Id $lockProc.Id -Force -ErrorAction SilentlyContinue
                Write-Host "Daemon (PID: $($lockProc.Id)) stopped successfully." -ForegroundColor Green
                $stopped = $true
            }
        }
    }

    if (-not $stopped) {
        Write-Host "No active daemon process found." -ForegroundColor Yellow
    } else {
        python -c "from state import StateStore; s = StateStore('state.db'); s.conn.execute('DELETE FROM daemon_lock'); s.conn.commit()" 2>$null
    }
}

function Start-AllServices ([switch]$AsBackground, [string]$BrowserEngine = "firefox") {
    Write-Host "`n=======================================================" -ForegroundColor Cyan
    Write-Host "   Starting All Auto Applier Services (All-in-One)" -ForegroundColor Cyan
    Write-Host "=======================================================" -ForegroundColor Cyan

    # 1. Daemon
    Start-DaemonProcess -AsBackground:$AsBackground -BrowserEngine $BrowserEngine

    # 2. REST API on port 8000
    $apiPidFile = Join-Path $ProjectDir ".api.pid"
    $apiRunning = $false
    if (Test-Path $apiPidFile) {
        $aPid = Get-Content $apiPidFile -ErrorAction SilentlyContinue
        if ($aPid -and (Get-Process -Id $aPid -ErrorAction SilentlyContinue)) {
            Write-Host "REST API already running on http://127.0.0.1:8000 (PID: $aPid)." -ForegroundColor Yellow
            $apiRunning = $true
        }
    }
    if (-not $apiRunning) {
        $apiCim = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' AND CommandLine LIKE '%api.py%'" -ErrorAction SilentlyContinue
        if ($apiCim) {
            Write-Host "REST API already running on http://127.0.0.1:8000 (PID: $($apiCim[0].ProcessId))." -ForegroundColor Yellow
            $apiRunning = $true
        }
    }
    if (-not $apiRunning) {
        Write-Host "`n[Action] Launching Local REST API at http://127.0.0.1:8000..." -ForegroundColor Cyan
        $apiProc = Start-Process python -ArgumentList @("-u", "api.py", "--port", "8000") -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru
        $apiProc.Id | Out-File $apiPidFile -Encoding ascii
        Write-Host "REST API running in background on http://127.0.0.1:8000 (PID: $($apiProc.Id))." -ForegroundColor Green
    }

    # 3. Langflow UI on port 7860
    $lfPidFile = Join-Path $ProjectDir ".langflow.pid"
    $lfRunning = $false
    if (Test-Path $lfPidFile) {
        $lPid = Get-Content $lfPidFile -ErrorAction SilentlyContinue
        if ($lPid -and (Get-Process -Id $lPid -ErrorAction SilentlyContinue)) {
            Write-Host "Langflow Web UI already running on http://localhost:7860 (PID: $lPid)." -ForegroundColor Yellow
            $lfRunning = $true
        }
    }
    if (-not $lfRunning) {
        $lfCim = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' AND CommandLine LIKE '%run_langflow.py%'" -ErrorAction SilentlyContinue
        if ($lfCim) {
            Write-Host "Langflow Web UI already running on http://localhost:7860 (PID: $($lfCim[0].ProcessId))." -ForegroundColor Yellow
            $lfRunning = $true
        }
    }
    if (-not $lfRunning) {
        Write-Host "`n[Action] Launching Langflow Web UI at http://localhost:7860..." -ForegroundColor Cyan
        $lfProc = Start-Process python -ArgumentList @("-u", "run_langflow.py", "run", "--port", "7860") -WorkingDirectory $ProjectDir -WindowStyle Hidden -PassThru
        $lfProc.Id | Out-File $lfPidFile -Encoding ascii
        Write-Host "Langflow Web UI running in background on http://localhost:7860 (PID: $($lfProc.Id))." -ForegroundColor Green
    }

    Write-Host "`nAll 3 services are active in the background." -ForegroundColor Green
    Write-Host " - Stop all: ./setup -StopAll" -ForegroundColor DarkGray
    Write-Host " - Status:   ./setup -Status" -ForegroundColor DarkGray
}

function Stop-AllServices {
    Write-Host "`nStopping All Auto Applier Services..." -ForegroundColor Cyan

    # 1. Stop Daemon
    Stop-DaemonProcess

    # 2. Stop REST API
    $apiPidFile = Join-Path $ProjectDir ".api.pid"
    if (Test-Path $apiPidFile) {
        $targetPid = Get-Content $apiPidFile -ErrorAction SilentlyContinue
        if ($targetPid) {
            $p = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
            if ($p) {
                Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
                Write-Host "REST API (PID: $targetPid) stopped." -ForegroundColor Green
            }
        }
        Remove-Item $apiPidFile -Force -ErrorAction SilentlyContinue
    }
    $apiProcs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' AND CommandLine LIKE '%api.py%'" -ErrorAction SilentlyContinue
    if ($apiProcs) {
        foreach ($item in $apiProcs) {
            Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped API process (PID: $($item.ProcessId))." -ForegroundColor Green
        }
    }

    # 3. Stop Langflow
    $lfPidFile = Join-Path $ProjectDir ".langflow.pid"
    if (Test-Path $lfPidFile) {
        $targetPid = Get-Content $lfPidFile -ErrorAction SilentlyContinue
        if ($targetPid) {
            $p = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
            if ($p) {
                Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
                Write-Host "Langflow (PID: $targetPid) stopped." -ForegroundColor Green
            }
        }
        Remove-Item $lfPidFile -Force -ErrorAction SilentlyContinue
    }
    $lfProcs = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' AND CommandLine LIKE '%run_langflow.py%'" -ErrorAction SilentlyContinue
    if ($lfProcs) {
        foreach ($item in $lfProcs) {
            Stop-Process -Id $item.ProcessId -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped Langflow process (PID: $($item.ProcessId))." -ForegroundColor Green
        }
    }
}

function Set-WindowsAutoStart ([bool]$Enable) {
    $startupFolder = [System.IO.Path]::Combine($env:APPDATA, "Microsoft\Windows\Start Menu\Programs\Startup")
    $startupVbs = Join-Path $startupFolder "AutoJobApplierAll.vbs"

    if ($Enable) {
        $setupScript = Join-Path $ProjectDir "setup.ps1"
        # VBScript silently launches PowerShell with hidden window on Windows user login (zero console flash)
        $vbsLines = @(
            'Set objShell = CreateObject("Wscript.Shell")',
            "objShell.Run ""powershell.exe -ExecutionPolicy Bypass -WindowStyle Hidden -File """"$setupScript"""" -All -Background"", 0, False"
        )
        $vbsContent = $vbsLines -join "`r`n"
        [System.IO.File]::WriteAllText($startupVbs, $vbsContent, [System.Text.Encoding]::ASCII)
        Write-Host "`n[Windows Auto-Start] ENABLED successfully!" -ForegroundColor Green
        Write-Host "When you turn on your PC and log into Windows, All-in-One (Daemon + REST API + Langflow) will automatically launch in the background." -ForegroundColor White
        Write-Host "Startup file: $startupVbs" -ForegroundColor DarkGray
    } else {
        if (Test-Path $startupVbs) {
            Remove-Item $startupVbs -Force -ErrorAction SilentlyContinue
            Write-Host "`n[Windows Auto-Start] DISABLED successfully (removed from Windows Startup folder)." -ForegroundColor Yellow
        } else {
            Write-Host "`n[Windows Auto-Start] Already disabled (no startup entry found)." -ForegroundColor Yellow
        }
    }
}

function Show-DaemonStatus {
    $pidFile = Join-Path $ProjectDir ".daemon.pid"
    $isRunning = $false
    Write-Host "`n=======================================================" -ForegroundColor DarkCyan
    Write-Host "             System & Services Status" -ForegroundColor White
    Write-Host "=======================================================" -ForegroundColor DarkCyan

    # 1. Daemon Status
    if (Test-Path $pidFile) {
        $activeDaemonPid = Get-Content $pidFile -ErrorAction SilentlyContinue
        if ($activeDaemonPid -and (Get-Process -Id $activeDaemonPid -ErrorAction SilentlyContinue)) {
            Write-Host "  Daemon:        RUNNING in background (PID: $activeDaemonPid)" -ForegroundColor Green
            $isRunning = $true
        }
    }
    if (-not $isRunning) {
        $found = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' AND CommandLine LIKE '%daemon.py%'" -ErrorAction SilentlyContinue
        if ($found) {
            foreach ($item in $found) {
                Write-Host "  Daemon:        RUNNING (PID: $($item.ProcessId))" -ForegroundColor Green
                $isRunning = $true
            }
        }
    }
    if (-not $isRunning -and (Test-Path "state.db")) {
        $dbLockPid = python -c "from state import StateStore; s = StateStore('state.db'); r = s.conn.execute('SELECT holder_pid FROM daemon_lock WHERE id = 1').fetchone(); print(r[0] if r else '')" 2>$null
        if ($dbLockPid -and ($dbLockPid.Trim() -ne "")) {
            $lockProc = Get-Process -Id ([int]$dbLockPid.Trim()) -ErrorAction SilentlyContinue
            if ($lockProc) {
                Write-Host "  Daemon:        RUNNING (PID: $dbLockPid, Lock Active)" -ForegroundColor Green
                $isRunning = $true
            }
        }
    }
    if (-not $isRunning) {
        Write-Host "  Daemon:        STOPPED" -ForegroundColor Yellow
    }

    # 2. REST API Status
    $apiPidFile = Join-Path $ProjectDir ".api.pid"
    $apiPid = if (Test-Path $apiPidFile) { Get-Content $apiPidFile -ErrorAction SilentlyContinue } else { $null }
    $apiProc = if ($apiPid) { Get-Process -Id $apiPid -ErrorAction SilentlyContinue } else { $null }
    if (-not $apiProc) {
        $apiCim = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' AND CommandLine LIKE '%api.py%'" -ErrorAction SilentlyContinue
        if ($apiCim) { $apiProc = $apiCim[0] }
    }
    if ($apiProc) {
        $pIdVal = if ($apiProc.Id) { $apiProc.Id } else { $apiProc.ProcessId }
        Write-Host "  REST API:      RUNNING (PID: $pIdVal) -> http://127.0.0.1:8000" -ForegroundColor Green
    } else {
        Write-Host "  REST API:      STOPPED" -ForegroundColor Yellow
    }

    # 3. Langflow UI Status
    $lfPidFile = Join-Path $ProjectDir ".langflow.pid"
    $lfPid = if (Test-Path $lfPidFile) { Get-Content $lfPidFile -ErrorAction SilentlyContinue } else { $null }
    $lfProc = if ($lfPid) { Get-Process -Id $lfPid -ErrorAction SilentlyContinue } else { $null }
    if (-not $lfProc) {
        $lfCim = Get-CimInstance Win32_Process -Filter "Name = 'python.exe' AND CommandLine LIKE '%run_langflow.py%'" -ErrorAction SilentlyContinue
        if ($lfCim) { $lfProc = $lfCim[0] }
    }
    if ($lfProc) {
        $pIdVal = if ($lfProc.Id) { $lfProc.Id } else { $lfProc.ProcessId }
        Write-Host "  Langflow UI:   RUNNING (PID: $pIdVal) -> http://localhost:7860" -ForegroundColor Green
    } else {
        Write-Host "  Langflow UI:   STOPPED" -ForegroundColor Yellow
    }

    # 4. Windows Auto-Start Status
    $startupVbs = Join-Path ([System.IO.Path]::Combine($env:APPDATA, "Microsoft\Windows\Start Menu\Programs\Startup")) "AutoJobApplierAll.vbs"
    if (Test-Path $startupVbs) {
        Write-Host "  PC Auto-Start: ENABLED (All-in-One starts on login)" -ForegroundColor Green
    } else {
        Write-Host "  PC Auto-Start: DISABLED" -ForegroundColor DarkGray
    }

    $logFile = Join-Path $ProjectDir "daemon.log"
    if (Test-Path $logFile) {
        Write-Host "`nRecent daemon activity (daemon.log):" -ForegroundColor DarkCyan
        Get-Content $logFile -Tail 10
    }
}

# Handle Command Line Arguments
if ($All) {
    Start-AllServices -AsBackground:$Background -BrowserEngine $Browser
    exit 0
}

if ($StopAll) {
    Stop-AllServices
    exit 0
}

if ($EnableAutoStart) {
    Set-WindowsAutoStart -Enable $true
    exit 0
}

if ($DisableAutoStart) {
    Set-WindowsAutoStart -Enable $false
    exit 0
}

if ($Langflow) {
    Write-Host "`n[Action] Starting Langflow Web UI at http://localhost:7860..." -ForegroundColor Cyan
    python run_langflow.py run --port 7860
    exit 0
}

if ($Api) {
    Write-Host "`n[Action] Starting Local REST API at http://127.0.0.1:8000..." -ForegroundColor Cyan
    python api.py --port 8000
    exit 0
}

if ($Login -ne "") {
    Write-Host "`n[Action] Opening headed $Browser browser for pre-authentication at: $Login" -ForegroundColor Cyan
    python daemon.py --login $Login --browser $Browser
    exit 0
}

if ($Daemon) {
    Start-DaemonProcess -AsBackground:$Background -BrowserEngine $Browser
    exit 0
}

if ($StopDaemon) {
    Stop-DaemonProcess
    exit 0
}

if ($Status) {
    Show-DaemonStatus
    exit 0
}

if ($Logs) {
    $logFile = Join-Path $ProjectDir "daemon.log"
    if (Test-Path $logFile) {
        Write-Host "Streaming live daemon logs (Ctrl+C to exit)..." -ForegroundColor Cyan
        Get-Content -Wait -Tail 30 $logFile
    } else {
        Write-Host "No daemon.log found. Start the daemon first." -ForegroundColor Yellow
    }
    exit 0
}

if ($DryRun) {
    Write-Host "`n[Action] Running single cycle in Dry-Run mode using $Browser..." -ForegroundColor Cyan
    python daemon.py --once --dry-run --browser $Browser
    exit 0
}

if ($Test) {
    Write-Host "`n[Action] Running full test suite..." -ForegroundColor Cyan
    python run_tests.py
    exit 0
}

if ($SyncCookies) {
    Write-Host "`n[Action] Syncing cookies from system Firefox profile..." -ForegroundColor Cyan
    python daemon.py --sync-cookies --browser $Browser
    exit 0
}

# Interactive Menu
while ($true) {
    Write-Host "`n-------------------------------------------------------" -ForegroundColor DarkCyan
    Write-Host "Select an action to launch:" -ForegroundColor White
    Write-Host "  [0] Start All-in-One (Daemon + REST API + Langflow Web UI)" -ForegroundColor Green
    Write-Host "  [1] Start Langflow Web UI (http://localhost:7860)" -ForegroundColor Yellow
    Write-Host "  [2] Pre-Login to Target Job Site (Preserves Sessions)" -ForegroundColor Yellow
    Write-Host "  [3] Start Auto Applier Daemon (Foreground or Background)" -ForegroundColor Yellow
    Write-Host "  [4] Run Single Cycle (Dry-Run / Test)" -ForegroundColor Yellow
    Write-Host "  [5] Start Local REST API Service (http://localhost:8000)" -ForegroundColor Yellow
    Write-Host "  [6] Run Test Suite" -ForegroundColor Yellow
    Write-Host "  [7] Sync Cookies from System Firefox Profile" -ForegroundColor Yellow
    Write-Host "  [A] Enable/Disable Auto-Start on Windows PC Boot" -ForegroundColor Yellow
    Write-Host "  [S] Services & Daemon Status" -ForegroundColor Yellow
    Write-Host "  [L] View Live Daemon Logs" -ForegroundColor Yellow
    Write-Host "  [K] Stop All Services (Daemon + API + Langflow)" -ForegroundColor Red
    Write-Host "  [Q] Exit" -ForegroundColor Red
    Write-Host "-------------------------------------------------------" -ForegroundColor DarkCyan
    $choice = Read-Host "Enter option [0-7, A, S, L, K, Q]"

    switch ($choice.ToUpper()) {
        "0" {
            Start-AllServices -AsBackground -BrowserEngine $Browser
        }
        "1" {
            Write-Host "`nLaunching Langflow UI on http://localhost:7860..." -ForegroundColor Cyan
            Write-Host "Tip: You can import 'auto_applier_flow.json' inside Langflow!" -ForegroundColor Green
            python run_langflow.py run --port 7860
            break
        }
        "2" {
            $url = Read-Host "`nEnter target site login URL (or press Enter for default)"
            if ([string]::IsNullOrWhiteSpace($url)) { $url = "https://google.com" }
            python daemon.py --login $url
        }
        "3" {
            $mode = Read-Host "`nLaunch mode: [B]ackground (detached) or [F]oreground (live console) [B/F, Default: B]"
            if ($mode -and $mode.ToUpper() -eq "F") {
                Start-DaemonProcess -BrowserEngine $Browser
            } else {
                Start-DaemonProcess -AsBackground -BrowserEngine $Browser
            }
        }
        "4" {
            Write-Host "`nExecuting single Dry-Run cycle..." -ForegroundColor Cyan
            python daemon.py --once --dry-run
        }
        "5" {
            Write-Host "`nStarting Local REST API on http://127.0.0.1:8000..." -ForegroundColor Cyan
            python api.py --port 8000
        }
        "6" {
            Write-Host "`nRunning Test Suite..." -ForegroundColor Cyan
            python run_tests.py
        }
        "7" {
            Write-Host "`nSyncing cookies from system Firefox profile..." -ForegroundColor Cyan
            python daemon.py --sync-cookies
        }
        "A" {
            $startupVbs = Join-Path ([System.IO.Path]::Combine($env:APPDATA, "Microsoft\Windows\Start Menu\Programs\Startup")) "AutoJobApplierAll.vbs"
            $isAuto = Test-Path $startupVbs
            if ($isAuto) {
                $ans = Read-Host "`nWindows Auto-Start is currently ENABLED. Disable it? [y/N]"
                if ($ans.ToUpper() -eq "Y") {
                    Set-WindowsAutoStart -Enable $false
                }
            } else {
                $ans = Read-Host "`nWindows Auto-Start is currently DISABLED. Enable All-in-One auto-start on PC boot? [Y/n]"
                if ($ans.ToUpper() -ne "N") {
                    Set-WindowsAutoStart -Enable $true
                }
            }
        }
        "S" {
            Show-DaemonStatus
            $pidFile = Join-Path $ProjectDir ".daemon.pid"
            $runningPid = if (Test-Path $pidFile) { Get-Content $pidFile -ErrorAction SilentlyContinue } else { $null }
            if ($runningPid -and (Get-Process -Id $runningPid -ErrorAction SilentlyContinue)) {
                $stopChoice = Read-Host "`nDo you want to stop the running background daemon? [y/N]"
                if ($stopChoice.ToUpper() -eq "Y") {
                    Stop-DaemonProcess
                }
            }
        }
        "L" {
            $logFile = Join-Path $ProjectDir "daemon.log"
            if (Test-Path $logFile) {
                Write-Host "`nStreaming live daemon logs (Ctrl+C to return to menu)..." -ForegroundColor Cyan
                Get-Content -Wait -Tail 30 $logFile
            } else {
                Write-Host "No daemon.log found yet. Start the daemon first." -ForegroundColor Yellow
            }
        }
        "K" {
            Stop-AllServices
        }
        "Q" {
            Write-Host "Goodbye!" -ForegroundColor Cyan
            exit 0
        }
        Default {
            Write-Host "Invalid option. Please try again." -ForegroundColor Yellow
        }
    }
}
