@echo off
echo === MiniCode 诊断工具 ===
echo.

echo 1. 检查 Python 版本...
python --version
echo.

echo 2. 检查后端模块导入...
python -c "import backend; print('✓ backend 模块正常')" 2>&1
echo.

echo 3. 检查端口占用...
echo 端口 8000:
netstat -ano | findstr ":8000.*LISTENING"
echo 端口 5173:
netstat -ano | findstr ":5173.*LISTENING"
echo.

echo 4. 检查前端目录...
if exist "frontend\dist" (
    echo ✗ frontend\dist 存在 (会导致使用旧版本)
    echo   建议删除: rmdir /s /q frontend\dist
) else (
    echo ✓ frontend\dist 不存在 (正常)
)
echo.

echo 5. 测试后端启动...
echo 尝试启动后端 (5秒)...
start /b python -m backend
timeout /t 5 /nobreak >nul
netstat -ano | findstr ":8000.*LISTENING" >nul
if %errorlevel%==0 (
    echo ✓ 后端启动成功
) else (
    echo ✗ 后端启动失败
    echo   请手动运行查看错误: python -m backend
)
echo.

echo === 诊断完成 ===
pause
