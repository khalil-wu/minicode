@echo off
REM MiniCode 启动脚本

echo 启动 MiniCode 桌面端...
echo.

REM 1. 启动后端
echo [1/3] 启动后端服务 (Port 8000)...
cd /d C:\Desktop\MiniCode\backend
start "MiniCode Backend" cmd /k "set PYTHONPATH=. && python -m uvicorn main:app --reload --host 0.0.0.0 --port 8000"
timeout /t 3 /nobreak > /dev/null

REM 2. 启动前端
echo [2/3] 启动前端开发服务器 (Port 5173)...
cd /d C:\Desktop\MiniCode\frontend
start "MiniCode Frontend" cmd /k "npm run dev"
timeout /t 5 /nobreak > /dev/null

REM 3. 启动 Electron
echo [3/3] 启动 Electron 桌面端...
cd /d C:\Desktop\MiniCode\frontend
start "MiniCode Desktop" cmd /k "npm run electron:dev"

echo.
echo 所有服务已启动！
echo 后端: http://localhost:8000
echo 前端: http://localhost:5173
echo.
pause
