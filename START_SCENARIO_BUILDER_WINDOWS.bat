@echo off
setlocal
cd /d "%~dp0"
title WDS Training Scenario Builder
if not exist "scenario_backend.py" (
  echo ERROR: scenario_backend.py not found in this folder.
  echo This launcher belongs in the ORIGINAL LOCAL FACILITATOR SIMULATOR folder.
  pause
  exit /b 1
)
if not exist "scenario_builder.html" (
  echo ERROR: scenario_builder.html not found in this folder.
  pause
  exit /b 1
)
where py >nul 2>nul
if not errorlevel 1 (
  set "PYCMD=py -3"
) else (
  where python >nul 2>nul
  if errorlevel 1 (
    echo ERROR: Python 3 not found. Install or use your existing Windows facilitator launcher.
    pause
    exit /b 1
  )
  set "PYCMD=python"
)
rem Reuse an already-running localhost service if the WDS catalog responds.
%PYCMD% -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/scenario/catalog',timeout=2).read(100)" >nul 2>nul
if errorlevel 1 (
  echo Starting the LOCAL-ONLY WDS scenario preparation service...
  start "WDS Scenario Preparation (Local Only)" cmd /k "%PYCMD% scenario_backend.py --port 8765"
  timeout /t 3 /nobreak >nul
)
start "" "http://127.0.0.1:8765/scenario_builder.html"
echo Local builder opened. Leave the Python backend window open during downloads and conversion.
endlocal
