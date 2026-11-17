@echo off
setlocal EnableExtensions
cd /d "%~dp0"

REM ============================================================
REM  Full health check for stop window
REM  [1/2] verify_system.py       - pre-launch full verification
REM  [2/2] tools\system_health.py - ops patrol (ledger / daemon freshness)
REM
REM  Run with daemon STOPPED for a complete DB check (expect 80/0/0).
REM  While daemon is running, the DuckDB single-writer lock makes DB
REM  checks fail with IO errors - environmental, not a fault. Warn first.
REM
REM  NOTE: keep this file pure ASCII (cmd codepage safety).
REM ============================================================

set "PY_EXE=py"
where py >nul 2>&1 || set "PY_EXE=python"
REM probe: broken py launcher (e.g. stale pyenv target) - fall back to python
"%PY_EXE%" -c "import sys" >nul 2>&1 || set "PY_EXE=python"
"%PY_EXE%" -c "import sys" >nul 2>&1 || (
    echo [ERROR] Python not found.
    exit /b 1
)

echo ==============================================================
echo  Full health check for stop window
echo ==============================================================

REM Probe: can we open the DB read-only? If not, daemon holds the lock and
REM verify_system DB checks will fail with IO errors (environmental).
"%PY_EXE%" -c "import sys, os, duckdb; p = os.path.join(r'%~dp0', 'data', 'quant_data.duckdb'); con = duckdb.connect(p, read_only=True); con.close()" >nul 2>&1
if not errorlevel 1 (
    echo [OK] database readable - full DB-check mode.
    echo.
) else (
    echo [WARN] database is LOCKED ^(daemon running^): verify_system DB checks
    echo        may fail with IO errors ^(environmental, not a fault^).
    echo        Close the daemon first for a full 80/0/0 result.
    echo.
)

echo [1/2] verify_system.py
"%PY_EXE%" verify_system.py
set "RC1=%ERRORLEVEL%"
echo.
echo [2/2] tools\system_health.py
"%PY_EXE%" tools\system_health.py
set "RC2=%ERRORLEVEL%"
echo.

if "%RC1%"=="0" if "%RC2%"=="0" (
    echo [OK] both health checks passed.
) else (
    echo [WARN] non-zero exit: verify_system=%RC1% system_health=%RC2%
    echo        If caused by daemon DB lock, re-run in the stop window.
)

exit /b %RC1%
