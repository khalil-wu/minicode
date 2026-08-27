@echo off
REM MiniCode 启动脚本

echo 启动 MiniCode 桌面端...
echo.

REM Electron 的 sidecar 会从项目根启动 python -m backend；这里不再
REM 另起一个 uvicorn 进程，避免 backend 包导入路径和端口所有权分裂。
echo [1/2] 启动前端开发服务器 (Port 5173)...
cd /d "%~dp0frontend"
start "MiniCode Frontend" cmd /k "npm run dev"
timeout /t 5 /nobreak > nul

REM 使用 desktop 中实际存在的开发入口；该入口会连接前端并管理后端 sidecar。
echo [2/2] 启动 Electron 桌面端...
cd /d "%~dp0desktop"
start "MiniCode Desktop" cmd /k "npm run start:client:devfrontend"

echo.
echo 已发起 MiniCode 启动流程。
echo 前端: http://localhost:5173
echo 后端由 Electron sidecar 管理。
echo.
pause
