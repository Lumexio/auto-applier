@echo off
setlocal
cd /d "%~dp0"

echo =======================================================
echo    AI Parallel Auto Job Applier - Setup ^& Launcher
echo =======================================================

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1" %*
if %ERRORLEVEL% EQU 0 goto end

echo.
echo Notice: PowerShell launcher exited. Running direct fallback menu...
python profile_gen.py
python -c "from state import StateStore; s = StateStore('state.db'); s.close()"
python daemon.py --once --dry-run

:end
endlocal
