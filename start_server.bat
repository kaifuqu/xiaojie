@echo off
REM Keep this file strictly ASCII-only! Windows cmd.exe opens .bat files in the system
REM code page (CP936/GBK on Chinese Windows). REM lines containing CJK get garbled before
REM chcp 65001 has a chance to run, and the trailing tokens after REM can be re-parsed as
REM commands (e.g. "a_sync.py is not recognized as an internal or external command").
REM All annotations below are in English. Do NOT reintroduce CJK text in this file.
setlocal EnableExtensions
REM [V26.88+ 9-19] Switch cmd code page to UTF-8 so the daemon child can echo CJK logs
REM back to the parent console without GBK misinterpretation. The two leading >nul redirections
REM also avoid the placeholder '> nul' being misread as '? nul' on legacy CP936 buffers.
chcp 65001 > nul 2>&1
cd /d "%~dp0"

REM ============================================================================
REM V26.88+ 10-14: UI 手动调试模式 (run_lab.py / ui/app.py)
REM ----------------------------------------------------------------------------
REM Designed workflow:
REM   1) Stop daemon (Ctrl+C in start_daemon_24x7.bat window)
REM   2) Run THIS script -> starts Streamlit UI in this window, foreground.
REM   3) Use UI to manually sync / wash / debug.
REM   4) When finished, Ctrl+C in THIS window. UI exits, sentinel is removed.
REM   5) Then re-launch daemon with start_daemon_24x7.bat for 24x7 mode.
REM
REM DuckDB single-writer rule: UI and daemon CANNOT run at the same time.
REM This script will refuse to start if a daemon is detected, unless --force is passed.
REM ============================================================================

if not exist "data\runtime" mkdir "data\runtime"
if not exist "data\runtime\state" mkdir "data\runtime\state"

REM [V26.80] Enhancement Switch (default OFF; set to 1 to enable 8-dimension optimization pipeline).
set "XIAOJIE_V2680_ENABLED=0"

REM [V26.80] A-share feature switch.
set "XIAOJIE_A_SHARE_FEATURES_ENABLED=0"

REM [V29 go-live fix 2026-10-15] Same interpreter-priority fix as start_daemon_24x7.bat:
REM prefer the project venv (D:\xiaojie\_pyenv\venv) because the "py" launcher here
REM resolves to a bare base interpreter with no site-packages. Fallbacks unchanged.
set "PY_EXE=%~dp0_pyenv\venv\Scripts\python.exe"
if exist "%PY_EXE%" goto PY_EXE_READY
set "PY_EXE=py"
where py > nul 2>&1 || set "PY_EXE=python"
REM probe: broken py launcher (e.g. stale pyenv target) - fall back to python
"%PY_EXE%" -c "import sys" >nul 2>&1 || set "PY_EXE=python"
:PY_EXE_READY
"%PY_EXE%" -c "import sys" 2> nul || (
    echo [ERROR] Python not found. Install Python 3 and ensure py or python is on PATH.
    echo Then from project root: py -m pip install -r requirements.txt -r requirements-daemon.txt
    pause
    exit /b 1
)

REM ----------------------------------------------------------------------------
REM Step 1: Mutual-exclusion check (UI vs daemon) via core.process_orchestrator.
REM ----------------------------------------------------------------------------
REM --force bypasses the check (DANGEROUS: only if you know daemon is dead but sentinel stuck).
set "ORCH_FORCE=0"
if /i "%1"=="--force" set "ORCH_FORCE=1"
if /i "%2"=="--force" set "ORCH_FORCE=1"

echo [ORCH] Checking if daemon (auto_sniper_daemon.py) is currently running...
"%PY_EXE%" -c "from core.process_orchestrator import main_check, DAEMON_LOCK_FILE; raise SystemExit(main_check(DAEMON_LOCK_FILE, 'UI', 'daemon', force=bool(int('%ORCH_FORCE%'))))"
set "ORCH_EC=%errorlevel%"
if "%ORCH_EC%"=="1" goto :DAEMON_ACTIVE
if "%ORCH_EC%"=="2" echo [ORCH] Stale daemon sentinel was cleaned.
if "%ORCH_EC%"=="3" echo [WARN] --force: will start UI even if daemon appears active. Expect DuckDB lock collisions.

REM ----------------------------------------------------------------------------
REM Step 2: UTF-8 + pyarrow shim + disable UI embedded async worker.
REM   The UI sentinel is written by Streamlit's worker process on import
REM   (see ui/app.py / run_lab.py top of file). We do NOT acquire from the bat
REM   here because that py.exe child exits immediately, leaving a stale sentinel.
REM ----------------------------------------------------------------------------
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
set "XIAOJIE_EMBED_UI_SCAN_WORKER=0"

REM [V26.88+ 10-01] Win11 Smart App Control blocks pyarrow._compute.pyd (WinError 4551),
REM which kills pandas/streamlit import. The shim in core/_pyarrow_compat.py neutralizes this
REM when XIAOJIE_DISABLE_PYARROW=1, letting pandas fall back to NumPy backend. Remove this
REM line after SAC is disabled / pyarrow DLL is added to WDAC allow-list.
set "XIAOJIE_DISABLE_PYARROW=1"

REM ----------------------------------------------------------------------------
REM Step 4: Launch Streamlit (foreground). Ctrl+C here stops UI cleanly.
REM ----------------------------------------------------------------------------
REM --logger.hideWelcomeMessage=true suppresses the "You can now view..." banner (including
REM Local/Network URL lines) AND all ANSI escape codes that banner carried. Clean output
REM when streamlit stdout is piped to a log file (.bat ASCII-only; the URL text would otherwise
REM carry ESC[34m ESC[1m ESC[0m sequences wherever the launcher runs redirect-on-launch).
echo [ORCH] Starting Streamlit UI (foreground). Ctrl+C to stop.
REM [V26.88+10-6 修复] 加 runOnSave 自动热重载，避免改了 .py 后还要手动重启 streamlit
REM 才能看到效果（如 retry_api(None) 那种「代码改了但进程没刷新」的隐性 bug）。
"%PY_EXE%" -m streamlit run "%~dp0ui\app.py" --logger.hideWelcomeMessage=true --server.runOnSave=true

REM ----------------------------------------------------------------------------
REM Step 5: Best-effort cleanup UI sentinel on exit.
REM   Note: the sentinel may belong to a Streamlit worker child (different PID),
REM   so release_my_sentinel() here is a best-effort and may silently no-op.
REM   Stale-cleanup in core.process_orchestrator handles the rest.
REM ----------------------------------------------------------------------------
"%PY_EXE%" -c "from core.process_orchestrator import release_my_sentinel, UI_LOCK_FILE; release_my_sentinel(UI_LOCK_FILE)" > nul 2>&1
echo [ORCH] UI exited. If daemon is dead but SUI sentinel stuck, rerun start_server.bat --force.

if errorlevel 1 (
    echo.
    echo Streamlit exited with an error. See messages above.
    pause
)
endlocal

exit /b 0

:DAEMON_ACTIVE
echo.
echo [ABORT] Daemon is still running. UI cannot start in single-writer mode.
echo [ABORT] Please close the daemon window first - press Ctrl+C there, then re-run start_server.bat.
echo [ABORT] If the daemon is truly dead but sentinel is stuck, retry with:  start_server.bat --force
pause
exit /b 1
