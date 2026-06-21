@echo off
setlocal
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

set SCRIPT_DIR=%~dp0
set REPO_ROOT=%SCRIPT_DIR%..

echo [MiniCode Desktop] Repo root: %REPO_ROOT%

if not exist "%REPO_ROOT%\frontend\node_modules" (
  echo [MiniCode Desktop] Installing frontend dependencies...
  call npm --prefix "%REPO_ROOT%\frontend" install
  if errorlevel 1 exit /b 1
)

if not exist "%REPO_ROOT%\desktop\node_modules\electron\package.json" (
  echo [MiniCode Desktop] Installing desktop dependencies...
  call npm --prefix "%REPO_ROOT%\desktop" install
  if errorlevel 1 exit /b 1
)

echo [MiniCode Desktop] Building frontend...
set MINICODE_VITE_RELATIVE_BASE=1
call npm --prefix "%REPO_ROOT%\frontend" run build
if errorlevel 1 exit /b 1
set MINICODE_VITE_RELATIVE_BASE=

echo [MiniCode Desktop] Launching desktop client...
call npm --prefix "%REPO_ROOT%\desktop" run start
exit /b %errorlevel%
