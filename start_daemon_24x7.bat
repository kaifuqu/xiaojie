@echo off
REM start_daemon_24x7.bat - ASCII REM only for cmd.exe compatibility on Chinese Windows.
REM 7x24 daemon watchdog. No >> or > redirect to data\runtime logs - Python RotatingFileHandler needs exclusive rename.
setlocal EnableExtensions
cd /d "%~dp0"

chcp 65001 >nul 2>&1

if not exist "data\runtime" mkdir "data\runtime"
if not exist "data\runtime\state" mkdir "data\runtime\state"

REM [V29 go-live fix 2026-10-15] Interpreter priority: project venv -> py -> python.
REM Root cause: on this machine the "py" launcher resolves to
REM D:\xiaojie\_pyenv\py312\python.exe (the bare base interpreter that only exists
REM to create the venv; it has NO site-packages). Running verify_system.py with it
REM produced 37 FAILs / "No module named 'duckdb'", and the watchdog only saw a
REM non-zero exit code with no readable cause.
REM Fix: prefer the project venv interpreter, fall back to the original py/python
REM logic. Watchdog behaviour is otherwise unchanged.
set "PY_EXE=%~dp0_pyenv\venv\Scripts\python.exe"
if exist "%PY_EXE%" goto PY_EXE_READY
set "PY_EXE=py"
where py >nul 2>&1 || set "PY_EXE=python"
REM probe: broken py launcher (e.g. stale pyenv target) - fall back to python
"%PY_EXE%" -c "import sys" >nul 2>&1 || set "PY_EXE=python"
:PY_EXE_READY

"%PY_EXE%" -c "import sys" 2>nul
if errorlevel 1 goto :NO_PY

REM ---------------------------------------------------------------------------
REM V26.88+ 10-14: Strict mutual exclusion with UI (start_server.bat).
REM ---------------------------------------------------------------------------
REM DuckDB single-writer rule: this daemon will refuse to start if UI is
REM detected to be live (or recently crashed). Pass --force to bypass when
REM you are confident UI is dead.
set "ORCH_FORCE=0"
if /i "%1"=="--force" set "ORCH_FORCE=1"
if /i "%2"=="--force" set "ORCH_FORCE=1"

REM [P0 fix 2026-11-13] Unattended launches must never sit on 'pause'.
REM The external watchdog re-launches this bat every ~20 min while the daemon is down.
REM If verify_system.py keeps failing, an interactive 'pause' would leave one dead
REM window per retry, piling up forever. Pass --unattended to get log-and-exit instead.
set "UNATTENDED=0"
if /i "%1"=="--unattended" set "UNATTENDED=1"
if /i "%2"=="--unattended" set "UNATTENDED=1"
if /i "%3"=="--unattended" set "UNATTENDED=1"

REM [2026-11-15] User Ctrl+C stop flag: auto_sniper_daemon.py writes
REM data\runtime\state\user_manual_stop.json when the user presses Ctrl+C.
REM Machine-side starts (--unattended: external watchdog / logon autostart) MUST
REM respect it - the user asked: once Ctrl+C-ed, do NOT come back automatically.
REM A manual run (double-click, no --unattended) is explicit go-live intent: clear it.
set "USER_STOP_FLAG=%~dp0data\runtime\state\user_manual_stop.json"
if exist "%USER_STOP_FLAG%" (
    if "%UNATTENDED%"=="1" (
        echo [SKIP] User Ctrl+C stop flag found - not restarting. Start manually to resume.
        exit /b 0
    )
    echo [INFO] Clearing previous user Ctrl+C stop flag - manual start = go-live intent.
    del /q "%USER_STOP_FLAG%" >nul 2>&1
)

echo [CHECK] verify_system.py - once before watchdog loop ...
"%PY_EXE%" "%~dp0verify_system.py"
if errorlevel 1 goto :VERIFY_FAIL

echo [ORCH] Checking if UI (run_lab.py / ui/app.py) is currently running...
"%PY_EXE%" -c "from core.process_orchestrator import main_check, UI_LOCK_FILE; raise SystemExit(main_check(UI_LOCK_FILE, 'daemon', 'UI', force=bool(int('%ORCH_FORCE%'))))"
set "ORCH_EC=%errorlevel%"

if "%ORCH_EC%"=="1" goto :UI_ACTIVE
if "%ORCH_EC%"=="2" echo [ORCH] Stale UI sentinel was cleaned.
if "%ORCH_EC%"=="3" echo [WARN] --force: will start daemon even if UI appears active. Expect DuckDB lock collisions.

set "XIAOJIE_EMBED_UI_SCAN_WORKER=0"
set "PYTHONUNBUFFERED=1"

REM [V26.88+ 10-01] Win11 Smart App Control blocks pyarrow._compute.pyd (WinError 4551);
REM the shim in core/_pyarrow_compat.py is what lets the daemon boot. See that file for the
REM root-cause and the long-term fix (disable SAC or add pyarrow DLL to WDAC allow-list).
set "XIAOJIE_DISABLE_PYARROW=1"

echo.
echo [WATCHDOG] Rotating log: data\runtime\sniper.log - see core\log_config.py
echo [WARN] One daemon only. Do not run start_server.bat or second copy on same DB.
echo [STOP] Press Ctrl+C to stop this window.
echo.

:LOOP
echo [START] auto_sniper_daemon.py

REM Sentinel is written by auto_sniper_daemon.py itself (PID of the real daemon child,
REM not the watchdog's short-lived py.exe /c child). Watchdog only refreshes the check
REM each iteration; do NOT acquire from the bat here.

"%PY_EXE%" -u "%~dp0auto_sniper_daemon.py"
set "DAEMON_EC=%errorlevel%"

REM [P0 fix 2026-11-13] exit code 2 = another daemon instance already holds the lock.
REM Previously main() just returned 0 in that case, so this watchdog loop treated it as
REM "daemon finished, restart in 30s" and kept spawning a python that dies instantly.
REM With autostart + a manually started daemon coexisting that is an endless spin.
REM Now: someone else owns the lock -> this window has nothing to do -> exit quietly.
if "%DAEMON_EC%"=="2" goto :ALREADY_RUNNING

REM [2026-11-15] User pressed Ctrl+C -> daemon wrote the user-stop flag -> the
REM watchdog loop must NOT restart. Exit instead (user asked: no auto-restart).
if exist "%USER_STOP_FLAG%" (
    echo.
    echo [STOP] User Ctrl+C stop flag detected - watchdog loop exits, no restart.
    echo [STOP] To go live again: double-click start_daemon_24x7.bat.
    exit /b 0
)

REM After daemon exits, daemon sentinel should be gone too (release_my_sentinel is called
REM from auto_sniper_daemon.py on normal shutdown). If it lingers (daemon killed forcefully),
REM cleanup stale is deferred to next check_peer_active() on next bat launch.

echo.
echo 守护进程已退出（exit=%DAEMON_EC%）。为避免数据库维护/压缩期间被立即重拉起，看门狗将等待 30 秒后再自动重启...
timeout /t 30 /nobreak
goto LOOP

:ALREADY_RUNNING
echo.
echo [SKIP] 已有 auto_sniper_daemon 实例在运行（退出码 2），本看门狗窗口退出，不再重启。
echo [SKIP] 这是正常情况：例如登录自启任务与手动启动的窗口同时存在。
exit /b 0

:UI_ACTIVE
echo.
echo [ABORT] UI session is still active. Daemon cannot start in single-writer mode.
echo [ABORT] Please close the Streamlit window first - press Ctrl+C there, then re-run start_daemon_24x7.bat.
echo [ABORT] If UI is truly dead but sentinel stuck, retry with:  start_daemon_24x7.bat --force
if not "%UNATTENDED%"=="1" pause
exit /b 1

:NO_PY
echo [ERROR] Python not found. Install Python 3 and add py or python to PATH.
echo Then: py -m pip install -r requirements.txt -r requirements-daemon.txt
if not "%UNATTENDED%"=="1" pause
exit /b 1

:VERIFY_FAIL
echo [ERROR] verify_system.py failed. Fix errors above, then run this bat again.
if not "%UNATTENDED%"=="1" pause
exit /b 1
