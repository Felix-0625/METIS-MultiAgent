@echo off
REM AI Multi-Agent System 一键启动脚本 (Windows)

echo ========================================
echo  AI Multi-Agent 项目管理系统
echo ========================================
echo.

REM 检查 Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 Python，请先安装 Python 3.10+
    pause
    exit /b 1
)

REM 检查 pip
pip --version >nul 2>&1
if errorlevel 1 (
    echo [错误] 未找到 pip，请先安装
    pause
    exit /b 1
)

REM 清理占用 8000 端口的旧进程（避免端口冲突）
echo [0/3] 检查端口占用...
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    echo [提示] 发现旧进程 PID=%%a 占用 8000 端口，正在终止...
    taskkill /PID %%a /F >nul 2>&1
)
REM 清理占用 3000 端口的旧前端进程
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":3000 " ^| findstr "LISTENING"') do (
    echo [提示] 发现旧进程 PID=%%a 占用 3000 端口，正在终止...
    taskkill /PID %%a /F >nul 2>&1
)
echo [OK] 端口检查完成
echo.

REM 安装后端依赖
echo [1/3] 安装后端依赖...
cd /d "%~dp0backend"
pip install -r requirements.txt -q
if errorlevel 1 (
    echo [错误] 依赖安装失败
    pause
    exit /b 1
)
echo [OK] 后端依赖安装完成
echo.

REM 启动后端
echo [2/3] 启动后端服务...
start "AI-Agent Backend" cmd /k "cd /d "%~dp0backend" && python main.py"
echo [OK] 后端服务启动中 (http://localhost:8000)
echo.

REM 等待后端启动
timeout /t 3 /nobreak >nul

REM 启动前端
echo [3/3] 启动前端服务...
cd /d "%~dp0frontend"
if exist "node_modules" (
    start "AI-Agent Frontend" cmd /k "npm run dev"
) else (
    echo [提示] 首次运行，正在安装前端依赖（需要几分钟）...
    start "AI-Agent Frontend" cmd /k "cd /d "%~dp0frontend" && npm install && npm run dev"
)
echo [OK] 前端服务启动中 (http://localhost:3000)
echo.

echo ========================================
echo  启动完成！
echo  后端: http://localhost:8000
echo  前端: http://localhost:3000
echo  API文档: http://localhost:8000/docs
echo ========================================
echo.
echo 按任意键退出...
pause >nul
