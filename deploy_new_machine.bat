@echo off
REM =============================================================================
REM deploy_new_machine.bat - one-shot bootstrap for a brand-new Windows machine
REM that will run this system 24x7 as a live trading machine.
REM
REM STYLE RULES (learned the hard way on a CP936 / non-ASCII code page):
REM   1) REM and echo lines must stay pure ASCII.
REM   2) NO parenthesised "if ( ... ) else ( ... )" blocks anywhere. An unbalanced
REM      bracket inside echoed text silently terminates the block early and the
REM      else branch runs too. Use flat "if errorlevel" + goto instead.
REM   3) Probe order must start with the project's own interpreters, because the
REM      reference machine has NO system Python: the "py" launcher there answers
REM      "No installed Pythons found!".
REM
REM Steps: 1) find Python 3.12  2) create venv  3) pip install requirements.lock
REM         4) runtime dirs     5) verify_system.py  -> must end with 0 FAIL
REM
REM config.yaml carries your API keys and travels with this folder. Never paste
REM keys into chat, logs or any remote repo.
REM =============================================================================
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul 2>&1

if not exist "data\runtime" mkdir "data\runtime"
if not exist "data\runtime\state" mkdir "data\runtime\state"
if not exist "data\runtime\logs" mkdir "data\runtime\logs"
if not exist "data\runtime\pool_cache" mkdir "data\runtime\pool_cache"
if not exist "data\runtime\scan_async" mkdir "data\runtime\scan_async"
if not exist "data\runtime\observability" mkdir "data\runtime\observability"
if not exist "data\runtime\health" mkdir "data\runtime\health"
if not exist "data\runtime\p1_gene" mkdir "data\runtime\p1_gene"

echo =============================================================================
echo  Step 1 of 5 : locate a Python 3.12.x interpreter
echo =============================================================================
set "PY_EXE="

if not exist "_pyenv\venv\Scripts\python.exe" goto :TRY_PORTABLE
"_pyenv\venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2]==(3, 12) else 1)" >nul 2>&1
if errorlevel 1 goto :TRY_PORTABLE
set "PY_EXE=%~dp0_pyenv\venv\Scripts\python.exe"
goto :PY_READY

:TRY_PORTABLE
if not exist "_pyenv\py312\python.exe" goto :TRY_LAUNCHER
"_pyenv\py312\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2]==(3, 12) else 1)" >nul 2>&1
if errorlevel 1 goto :TRY_LAUNCHER
set "PY_EXE=%~dp0_pyenv\py312\python.exe"
goto :PY_READY

:TRY_LAUNCHER
where py >nul 2>&1
if errorlevel 1 goto :TRY_PLAIN
py -3.12 -c "import sys" >nul 2>&1
if errorlevel 1 goto :TRY_PLAIN
set "PY_EXE=py -3.12"
goto :PY_READY

:TRY_PLAIN
where python >nul 2>&1
if errorlevel 1 goto :NO_PY
python -c "import sys; raise SystemExit(0 if sys.version_info[:2]==(3, 12) else 1)" >nul 2>&1
if errorlevel 1 goto :NO_PY
set "PY_EXE=python"
goto :PY_READY

:PY_READY
echo [OK] Using: %PY_EXE%
%PY_EXE% -c "import sys; print('     version:', sys.version)"
goto :STEP2

:NO_PY
echo [ERROR] No Python 3.12.x interpreter found.
echo.
echo Tried, in order:
echo   1. _pyenv\venv\Scripts\python.exe   - project venv, not present yet
echo   2. _pyenv\py312\python.exe         - bundled portable build, not present
echo   3. py -3.12                        - Windows launcher, no 3.12 registered
echo   4. python on PATH                  - missing or not 3.12
echo.
echo Fix, pick ONE:
echo   A  Copy the whole project folder INCLUDING the _pyenv directory to the
echo      same absolute path D:\xiaojie. The bundled portable Python then just
echo      works and there is nothing to install.  <- fastest, recommended
echo   B  Install Python 3.12.7 from https://www.python.org/downloads/windows/
echo      and tick "Add python.exe to PATH", then re-run this file.
echo   C  Extract the official embeddable 3.12.7 zip into _pyenv\py312 and fix
echo      the ._pth bootstrap file.  See DEPLOY_NEW_MACHINE.md for details.
pause
exit /b 1

:STEP2
echo.
echo =============================================================================
echo  Step 2 of 5 : isolated venv at _pyenv\venv
echo =============================================================================
if exist "_pyenv\venv\Scripts\python.exe" goto :VENV_EXISTS
%PY_EXE% -m venv "_pyenv\venv"
if errorlevel 1 goto :VENV_CREATE_FAIL
echo [OK] venv created.
goto :VENV_OK

:VENV_EXISTS
echo [SKIP] _pyenv\venv already exists - reusing it.
echo        If dependencies look stale, delete the _pyenv folder and re-run.

:VENV_OK
set "VENV_PY=%~dp0_pyenv\venv\Scripts\python.exe"
"%VENV_PY%" -c "import sys" 2>nul
if errorlevel 1 goto :VENV_BROKEN
"%VENV_PY%" -c "import sys; print('     venv python:', sys.version)"
goto :STEP3

:VENV_CREATE_FAIL
echo [ERROR] venv creation failed.
pause
exit /b 1

:VENV_BROKEN
echo [ERROR] venv python is broken. Delete the _pyenv folder and re-run.
pause
exit /b 1

:STEP3
echo.
echo =============================================================================
echo  Step 3 of 5 : install pinned dependencies from requirements.lock
echo =============================================================================
"%VENV_PY%" -m pip install -U pip
"%VENV_PY%" -m pip install -r requirements.lock
if errorlevel 1 goto :PIP_FAIL
echo [OK] dependencies installed.
goto :STEP4

:PIP_FAIL
echo.
echo [ERROR] pip install failed. Read the error above.
echo Common causes: no internet, or a proxy blocking pypi.org.
pause
exit /b 1

:STEP4
echo.
echo =============================================================================
echo  Step 4 of 5 : runtime directory skeleton
echo =============================================================================
"%VENV_PY%" -c "from core.runtime_data_paths import ensure_runtime_data_layout; ensure_runtime_data_layout(); print('     runtime layout OK')"
if errorlevel 1 echo [WARN] layout helper failed - the mkdir calls above already ran.
goto :STEP5

:STEP5
echo.
echo =============================================================================
echo  Step 5 of 5 : system health check - verify_system.py
echo =============================================================================
"%VENV_PY%" verify_system.py
set "VERIFY_EC=%errorlevel%"
echo.
if not "%VERIFY_EC%"=="0" goto :BLOCKED
echo [DONE] Health check passed. Next step:
echo        start_daemon_24x7.bat
echo.
pause
exit /b 0

:BLOCKED
echo [BLOCKED] Health check reported problems. Fix the [FAIL] lines above.
echo          Most common first-run cause: daily_data is still empty. Start the
echo          daemon once with internet access and let the 19:45 sync do its
echo          first full pull, then re-run verify_system.py.
echo.
pause
exit /b %VERIFY_EC%
