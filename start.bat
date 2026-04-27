@echo off
REM ============================================================
REM  Smart Daily Planner — Normal Mode Launcher (Windows)
REM
REM  Prerequisites: complete PRE_TESTING_GUIDE.md first.
REM  Requires .env file with real API keys.
REM
REM  For demo mode (no API keys): run run_demo.py instead.
REM ============================================================

title Smart Daily Planner — Normal Mode
cd /d "%~dp0"

echo.
echo  ============================================
echo   Smart Daily Planner — Normal Mode
echo   Working dir: %CD%
echo  ============================================
echo.

REM ── Kill any existing process on port 8080 ───────────────────
echo  [+] Checking for stale processes on port 8080...
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":8080 " ^| findstr "LISTENING"') do (
    echo  [+] Releasing port 8080 (PID %%a)...
    taskkill /F /PID %%a 2>nul
)
timeout /t 1 /nobreak > nul
echo  [+] Port 8080 is free.
echo.

REM ── Check .env exists ────────────────────────────────────────
if not exist ".env" (
    echo  [ERROR] .env file not found.
    echo  Copy .env.example to .env and fill in your API keys.
    echo  See PRE_TESTING_GUIDE.md for instructions.
    echo.
    pause
    exit /b 1
)

REM ── Activate venv if present ─────────────────────────────────
if exist ".venv\Scripts\activate.bat" (
    call .venv\Scripts\activate.bat
    echo  [+] Virtual environment activated.
)

REM ── Create __init__.py markers ───────────────────────────────
if not exist "config\__init__.py"      type nul > "config\__init__.py"
if not exist "api\__init__.py"         type nul > "api\__init__.py"
if not exist "agents\__init__.py"      type nul > "agents\__init__.py"
if not exist "tools\__init__.py"       type nul > "tools\__init__.py"
if not exist "mcp_servers\__init__.py" type nul > "mcp_servers\__init__.py"

REM ── Start server ─────────────────────────────────────────────
echo  [+] Starting at http://localhost:8080
echo  [+] API docs: http://localhost:8080/docs
echo  Press Ctrl+C to stop.
echo.

start /min "" cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:8080"

python -m uvicorn api.main:app --host 0.0.0.0 --port 8080 --reload

echo.
pause
