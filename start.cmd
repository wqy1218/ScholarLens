@echo off
rem ScholarLens launcher.
rem In cmd.exe, `cd D:\ScholarLens` does NOT switch the current drive, so
rem `npm start` keeps running in C:\Users\... and fails with ENOENT package.json.
rem `cd /d` switches both drive and directory; this script always runs from its
rem own folder, so double-clicking it is enough.
setlocal
cd /d "%~dp0"
echo Starting ScholarLens from %CD%
call npm start
if errorlevel 1 (
  echo.
  echo ScholarLens exited with an error. See the messages above.
  pause
)
