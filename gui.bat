@echo off
rem The web interface for what needs you (questions the forms asked, fit decisions, sites on probation, jobs to finish by hand).
rem   gui.bat              open it in your browser (starts it first if it is not running)
rem   gui.bat --demo       look around on made-up data
rem   gui.bat --port 9000  use another port
setlocal
cd /d "%~dp0"
python -m gui --open %*
exit /b %errorlevel%
