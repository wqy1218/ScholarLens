@echo off
rem ScholarLens build: archive the current dist/ to dist_old/, then build a
rem fresh NSIS installer into dist/. Run from anywhere; it cds to its own folder
rem (cmd's `cd` alone does not switch drives).
setlocal
cd /d "%~dp0"

echo === ScholarLens build ===
echo Close ScholarLens first (tray icon - Exit), otherwise files cannot be replaced.
timeout /t 3 >nul

rem v1.0.4 (P2-3): README documents that a TMP under %LOCALAPPDATA%\Temp makes the
rem NSIS uninstaller step fail with exit code 2. Pin TMP/TEMP to a fresh directory
rem under the project so the documented build command works from this script.
if not exist "%~dp0_slbuild" md "%~dp0_slbuild"
set "TMP=%~dp0_slbuild"
set "TEMP=%~dp0_slbuild"

if exist "dist" (
  echo [1/3] existing dist found - archiving it
  rem Do not delete a previous archive: a second build would otherwise destroy
  rem the only copy of the older installer. Keep it under a random suffix.
  if exist "dist_old" (
    move "dist_old" "dist_old_%RANDOM%" >nul
  )
  move "dist" "dist_old" >nul
  if errorlevel 1 (
    echo Could not move dist to dist_old. Is the app still running?
    pause
    exit /b 1
  )
) else (
  echo [1/3] no existing dist - skipping archive
)

echo [2/3] clearing __pycache__ (must not enter the installer)
if exist "%~dp0server" for /d /r "%~dp0server" %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d"
if exist "%~dp0python-runtime" for /d /r "%~dp0python-runtime" %%d in (__pycache__) do @if exist "%%d" rd /s /q "%%d"

echo [3/3] building with electron-builder...
call npx electron-builder --win nsis --publish never
if errorlevel 1 (
  echo.
  echo BUILD FAILED - see the messages above.
  pause
  exit /b 1
)

echo.
echo Build complete: %CD%\dist
pause
