@echo off
chcp 65001 >nul
title Vibe Coding - 启动中...

echo.
echo ==========================================
echo   Vibe Coding - 手机远程控制 Claude Code
echo ==========================================
echo.

:: 检查 Node.js
where node >nul 2>nul
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Node.js，请先安装：https://nodejs.org/
    pause
    exit /b 1
)

:: 检查 Claude CLI
where claude >nul 2>nul
if %errorlevel% neq 0 (
    echo [错误] 未检测到 Claude CLI，请先安装：npm install -g @anthropic-ai/claude-cli
    pause
    exit /b 1
)

:: 安装依赖
if not exist "node_modules" (
    echo [1/3] 正在安装依赖...
    npm install --production
)

:: 获取本机 IP
echo [2/3] 获取本机 IP...
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /i "IPv4" ^| findstr /v "127.0.0.1"') do (
    set IP=%%a
)
set IP=%IP: =%

:: 启动服务
echo [3/3] 启动服务...
echo.
echo ==========================================
echo   服务已启动！
echo   电脑访问: http://localhost:3000
echo   手机访问: http://%IP%:3000
echo   密码: vibe123
echo ==========================================
echo.

:: 自动打开浏览器
start http://localhost:3000

:: 运行服务器
node server.js
