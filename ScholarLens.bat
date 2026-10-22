@echo off
rem ScholarLens launcher — start Electron (which spawns the Python backend)
start "" "%~dp0node_modules\electron\dist\electron.exe" "%~dp0."
