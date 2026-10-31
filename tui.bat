@echo off
rem The terminal dashboard. Textual lives in its own small environment (.venv-tui) so it cannot disturb Langflow's packages.
rem   tui.bat              the live dashboard for this folder
rem   tui.bat --demo       the dashboard on made-up data
rem   tui.bat --once       print one text snapshot and exit
setlocal
cd /d "%~dp0"
if not exist ".venv-tui\Scripts\python.exe" (
  echo Creating the dashboard environment ^(one time, a few MB^)...
  python -m venv .venv-tui || goto :fail
  ".venv-tui\Scripts\python.exe" -m pip install --quiet --disable-pip-version-check textual pyyaml || goto :fail
)
".venv-tui\Scripts\python.exe" -m tui %*
exit /b %errorlevel%
:fail
echo Could not set up the dashboard environment. Is Python on PATH and online?
exit /b 1
