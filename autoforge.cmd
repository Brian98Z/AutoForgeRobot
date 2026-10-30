@echo off
setlocal
cd /d "%~dp0"
set "AUTOFORGE_PROJECT=%~dp0"
if not exist "target\release\autoforge.exe" (
  echo Build missing. Run: cargo build --release --locked
  exit /b 1
)
"target\release\autoforge.exe" %*
exit /b %errorlevel%
