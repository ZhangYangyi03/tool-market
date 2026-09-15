@echo off
REM tool-market autostart: uvicorn API + ngrok public tunnel.
REM Registered as a Windows Scheduled Task (ONSTART). See register_startup_task.
REM
REM Two things here are load-bearing and were both wrong in the first draft:
REM   * autoforge is NOT pip-installed, so its source dir must be injected into
REM     sys.path or `import autoforge` fails inside toolmarket.
REM   * ngrok cannot compute its default config path on this host because
REM     %LocalAppData% is undefined in the service environment, so --config is
REM     passed explicitly. Without it ngrok exits with ERR_NGROK_4018.
setlocal

set "PY=C:\Users\china\AppData\Local\hermes\hermes-agent\.hermes-runtime\python\generation-1785702796-22504-dfc5499f\cpython-3.11-windows-x86_64-none\python.exe"
set "AF=D:\Users\china\Desktop\项目_开发\autoforge"
set "TM=D:\Users\china\Desktop\项目_开发\tool-market"
set "LOGS=%TM%\logs"
set "NGROK=C:\Users\china\miniconda3\Scripts\ngrok.exe"
set "NGROK_CFG=C:\Users\china\.ngrok.yml"

if not exist "%LOGS%" mkdir "%LOGS%"

REM -- the API ------------------------------------------------------------
REM Started via a small launcher rather than `-m uvicorn`, because the two
REM sys.path entries have to be in place before uvicorn imports the app.
start "tool-market" /min "%PY%" "%TM%\autostart_server.py" >> "%LOGS%\uvicorn.log" 2>&1

REM -- the console --------------------------------------------------------
REM A second process, on its own port, reading the same durable store. It cannot
REM share 8000: the console moves the API under /api, while market_push talks to
REM the bare API at /resources on 8000. Two ports, one shelf.
REM
REM This is why the console is launched from here at all -- it is a window onto
REM the shelf, and a window that only exists while somebody remembers to open it
REM is the same as no window. Started detached (/min) so a console crash cannot
REM take the API's tunnel down with it.
start "tool-market console" /min "%PY%" "%USERPROFILE%\toolmarket_console.py" >> "%LOGS%\console.log" 2>&1

REM Give uvicorn a moment to bind before the tunnel points at it. ngrok
REM retries, so this is politeness rather than a requirement.
timeout /t 5 /nobreak >nul

REM -- the public tunnel --------------------------------------------------
start "ngrok" /min "%NGROK%" http 8000 --config "%NGROK_CFG%" --log "%LOGS%\ngrok.log" --log-format json

endlocal
exit /b 0
