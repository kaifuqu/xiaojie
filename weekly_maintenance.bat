@echo off
setlocal EnableExtensions
cd /d "%~dp0"
REM chcp must run BEFORE any CJK text line (UTF-8 file on CP936 console).
chcp 65001 >nul 2>&1

REM Optional: run the same weekly orchestration as daemon 03:30 (scheme 2: stop holders then VACUUM).
REM Use when you prefer Task Scheduler instead of relying on auto_sniper_daemon to spawn the child.

set "PY_EXE=py"
where py >nul 2>&1 || set "PY_EXE=python"
REM probe: broken py launcher (e.g. stale pyenv target) - fall back to python
"%PY_EXE%" -c "import sys" >nul 2>&1 || set "PY_EXE=python"
"%PY_EXE%" -c "import sys" >nul 2>&1 || (
    echo [ERROR] Python not found.
    exit /b 1
)

"%PY_EXE%" "%~dp0tools\weekly_db_maintenance_orchestrated.py" --no-pause
set "RC=%ERRORLEVEL%"

REM P4 尾盘标定（V33）：把本周积累的 P4 信号按尾盘阶段分桶，输出
REM data\p4_tail_calibration.json（只建议、不改配置）。失败不影响主维护结果。
"%PY_EXE%" "%~dp0tools\p4_tail_calibration.py" --days 250
if errorlevel 1 echo [WARN] P4 tail calibration skipped (non-fatal).

exit /b %RC%
