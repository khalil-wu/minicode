@echo off
setlocal EnableExtensions

echo ==========================================
echo MiniCode dev launcher
echo ==========================================
echo.

if not exist "pyproject.toml" (
  echo Error: run this script from the MiniCode project root.
  exit /b 1
)

for /f %%P in ('powershell -NoProfile -Command "for($p=8000;$p -lt 8100;$p++){try{$l=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Parse('127.0.0.1'),$p);$l.Start();$l.Stop();Write-Output $p;break}catch{}}"') do set "BACKEND_PORT=%%P"
for /f %%P in ('powershell -NoProfile -Command "for($p=5173;$p -lt 5273;$p++){try{$l=[Net.Sockets.TcpListener]::new([Net.IPAddress]::Parse('127.0.0.1'),$p);$l.Start();$l.Stop();Write-Output $p;break}catch{}}"') do set "FRONTEND_PORT=%%P"

if "%BACKEND_PORT%"=="" (
  echo Error: no free backend port found in 8000-8099.
  exit /b 1
)

if "%FRONTEND_PORT%"=="" (
  echo Error: no free frontend port found in 5173-5272.
  exit /b 1
)

set "MINICODE_BACKEND_HOST=127.0.0.1"
set "MINICODE_BACKEND_PORT=%BACKEND_PORT%"
set "MINICODE_API_BASE_URL=http://127.0.0.1:%BACKEND_PORT%"
set "MINICODE_WS_BASE_URL=ws://127.0.0.1:%BACKEND_PORT%"
set "MINICODE_FRONTEND_URL=http://127.0.0.1:%FRONTEND_PORT%"
set "VITE_DEV_BACKEND_ORIGIN=http://127.0.0.1:%BACKEND_PORT%"
set "VITE_API_BASE_URL=http://127.0.0.1:%BACKEND_PORT%"
set "VITE_WS_BASE_URL=ws://127.0.0.1:%BACKEND_PORT%"

echo 1. Starting backend on %MINICODE_API_BASE_URL%
start "MiniCode backend" /B python -m backend > backend.log 2>&1
echo    Backend log: backend.log

echo.
echo 2. Starting frontend on %MINICODE_FRONTEND_URL%
pushd frontend
start "MiniCode frontend" /B npm run dev -- --host 127.0.0.1 --port %FRONTEND_PORT% > ..\frontend.log 2>&1
popd
echo    Frontend log: frontend.log

echo.
echo ==========================================
echo MiniCode started
echo ==========================================
echo Backend:  %MINICODE_API_BASE_URL%
echo Frontend: %MINICODE_FRONTEND_URL%
echo WebSocket: %MINICODE_WS_BASE_URL%
echo.
echo Logs:
echo   type backend.log
echo   type frontend.log
echo.
pause
