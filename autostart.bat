@echo off
REM tool-market autostart: uvicorn API + console + ngrok public tunnel.
REM Launched from a shortcut in the user's Startup folder.
REM
REM Load-bearing facts, each of which was wrong in an earlier draft:
REM   * autoforge is NOT pip-installed, so its source dir must be injected into
REM     sys.path or `import autoforge` fails inside toolmarket. That is what
REM     toolmarket_server.py does; this file only launches it.
REM   * The interpreter must be the one that actually has uvicorn. The hermes
REM     *runtime* python does not; the hermes *venv* python does. Picking the
REM     wrong one fails at `import uvicorn` with a traceback in the log and no
REM     listener on 8000 -- which reads like a port problem and is not one.
REM   * ngrok cannot compute its default config path on this host because
REM     %LocalAppData% is undefined in the service environment, so --config is
REM     passed explicitly. Without it ngrok exits with ERR_NGROK_4018.
REM   * `start` does not forward `>>` redirection to the child, and it mangles
REM     arguments containing non-ASCII path segments. So each child is launched
REM     as `cmd /c` with the redirection *inside* the quoted command, and the
REM     launcher lives at an ASCII path (C:\Users\china\toolmarket_server.py).
REM
REM 2026-09-16: a boot storm of console windows was traced to this file and its
REM sibling in the tool-market checkout both starting the same three children
REM at logon. Two fixes, below: every child is now gated on its port (or, for
REM ngrok, on its process) already being free, and nothing is started twice.
setlocal

set "PY=C:\Users\china\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe"
set "LAUNCHER=C:\Users\china\toolmarket_server.py"
set "CONSOLE=C:\Users\china\toolmarket_console.py"
set "LOGS=C:\Users\china\toolmarket_logs"
set "NGROK=C:\Users\china\miniconda3\Scripts\ngrok.exe"
set "NGROK_CFG=C:\Users\china\.ngrok.yml"

if not exist "%LOGS%" mkdir "%LOGS%"
echo [%DATE% %TIME%] autostart begin >> "%LOGS%\autostart.log"

REM -- the API: only if nothing already answers on 8000 -------------------
call :listening 8000
if "%LISTEN%"=="1" (
    echo [%DATE% %TIME%] api already up on 8000, skipping >> "%LOGS%\autostart.log"
) else (
    start "tool-market" /min cmd /c ""%PY%" "%LAUNCHER%" >> "%LOGS%\uvicorn.log" 2>&1"
    echo [%DATE% %TIME%] api launched >> "%LOGS%\autostart.log"
)

REM -- the console: a second listener on 8001 over the same durable store.
REM It cannot share 8000: the console moves the API under /api, while
REM market_push talks to the bare API at /resources on 8000 and ngrok points
REM there. One shelf, two ports.
call :listening 8001
if "%LISTEN%"=="1" (
    echo [%DATE% %TIME%] console already up on 8001, skipping >> "%LOGS%\autostart.log"
) else (
    start "tool-market console" /min cmd /c ""%PY%" "%CONSOLE%" >> "%LOGS%\console.log" 2>&1"
    echo [%DATE% %TIME%] console launched >> "%LOGS%\autostart.log"
)

REM Give uvicorn a moment to bind before the tunnel points at it. ngrok
REM retries on its own, so this is politeness rather than a requirement.
ping -n 6 127.0.0.1 >nul

REM -- the public tunnel: ngrok exits if another tunnel already holds a session
call :ngrok_running
if "%RUNNING%"=="1" (
    echo [%DATE% %TIME%] ngrok already running, skipping >> "%LOGS%\autostart.log"
) else (
    start "ngrok" /min cmd /c ""%NGROK%" http 8000 --config "%NGROK_CFG%" --log "%LOGS%\ngrok.log" --log-format json >> "%LOGS%\ngrok_stdout.log" 2>&1"
    echo [%DATE% %TIME%] ngrok launched >> "%LOGS%\autostart.log"
)

echo [%DATE% %TIME%] autostart end >> "%LOGS%\autostart.log"
endlocal
exit /b 0

:listening
set "LISTEN=0"
netstat -ano | findstr /C:"LISTENING" | findstr /C:":%~1 " >nul 2>&1 && set "LISTEN=1"
exit /b 0

:ngrok_running
set "RUNNING=0"
tasklist /FI "IMAGENAME eq ngrok.exe" 2>nul | find /I "ngrok.exe" >nul && set "RUNNING=1"
exit /b 0
