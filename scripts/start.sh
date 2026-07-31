#!/bin/bash
# AI Multi-Agent System 一键启动脚本 (Linux/Mac)

echo "========================================"
echo " AI Multi-Agent 项目管理系统"
echo "========================================"
echo ""

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "[错误] 未找到 Python3，请先安装"
    exit 1
fi

# 检查 pip
if ! command -v pip &> /dev/null; then
    echo "[错误] 未找到 pip，请先安装"
    exit 1
fi

# 安装后端依赖
echo "[1/3] 安装后端依赖..."
cd backend
pip install -r requirements.txt -q
if [ $? -ne 0 ]; then
    echo "[错误] 依赖安装失败"
    exit 1
fi
echo "[OK] 后端依赖安装完成"
echo ""

# 启动后端
echo "[2/3] 启动后端服务..."
python3 main.py &
BACKEND_PID=$!
echo "[OK] 后端服务启动中 (http://localhost:8000)"
echo ""

# 等待后端启动
sleep 3

# 启动前端
echo "[3/3] 启动前端服务..."
cd ../frontend
if [ -d "node_modules" ]; then
    npm run dev &
    FRONTEND_PID=$!
else
    echo "[提示] 需要先运行: cd frontend && npm install"
    npm install && npm run dev &
    FRONTEND_PID=$!
fi
echo "[OK] 前端服务启动中 (http://localhost:3000)"
echo ""

echo "========================================"
echo " 启动完成！"
echo " 后端: http://localhost:8000"
echo " 前端: http://localhost:3000"
echo " API文档: http://localhost:8000/docs"
echo "========================================"
echo ""
echo "按 Ctrl+C 停止所有服务"

# 等待中断信号
trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" SIGINT SIGTERM
wait