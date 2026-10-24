@echo off
setlocal
cd /d "%~dp0"
title ATLAS Local Case Analysis
where python >nul 2>nul
if errorlevel 1 (
  echo ERROR: Python is required. Install Python 3.11 or newer and enable Add to PATH.
  pause
  exit /b 1
)
where node >nul 2>nul
if errorlevel 1 (
  echo ERROR: Node.js is required. Install Node.js 18 or newer and reopen this terminal.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe (
  echo Creating Python virtual environment...
  python -m venv .venv || goto :failed
)
if not exist .venv\.requirements-installed (
  echo Installing backend dependencies for first use...
  .venv\Scripts\python.exe -m pip install -r requirements.txt || goto :failed
  echo installed>.venv\.requirements-installed
)
echo Initializing local SQLite database...
.venv\Scripts\python.exe -c "from backend.local_store import initialize; initialize(); print('Local database ready.')" || goto :failed
if not exist frontend\node_modules (
  echo Installing frontend dependencies...
  pushd frontend
  call npm ci
  if errorlevel 1 (popd & goto :failed)
  popd
)
netstat -ano | findstr /R /C:":8000 .*LISTENING" >nul
if errorlevel 1 (
  echo Starting backend at http://127.0.0.1:8000 ...
  start "ATLAS Backend" /D "%~dp0" "%~dp0.venv\Scripts\python.exe" -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
) else echo Backend already listening at http://127.0.0.1:8000
netstat -ano | findstr /R /C:":3000 .*LISTENING" >nul
if errorlevel 1 (
  echo Starting frontend at http://localhost:3000 ...
  start "ATLAS Frontend" /D "%~dp0frontend" cmd /k npm run dev -- --host 127.0.0.1 --port 3000
) else echo Frontend already listening at http://localhost:3000
echo.
echo ATLAS starts locally with SQLite. Open http://localhost:3000
exit /b 0
:failed
echo.
echo Startup failed. Review the error above and verify internet access for first-time dependency installation.
pause
exit /b 1

