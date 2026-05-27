@echo off
echo === MiniCode 依赖重新安装 ===
echo.

echo 1. 检查 Python 版本...
python --version
echo.

echo 2. 检查当前依赖...
pip list | findstr "fastapi uvicorn pydantic httpx"
echo.

echo 3. 重新安装依赖...
echo 这可能需要几分钟...
pip install -e .
echo.

echo 4. 验证安装...
python -c "import fastapi; import uvicorn; import pydantic; print('✓ 核心依赖安装成功')"
echo.

echo === 安装完成 ===
echo.
echo 现在可以运行: start.bat
pause
