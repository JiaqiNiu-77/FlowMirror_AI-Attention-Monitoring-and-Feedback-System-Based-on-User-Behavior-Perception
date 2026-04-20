#!/bin/bash
# FlowMirror 启动脚本 (macOS)
# 双击此文件即可启动服务

# 获取脚本所在目录（无论从哪里双击都能正确定位）
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/flowmirror/backend"

echo "========================================"
echo "  FlowMirror - 摆烂指数分析器"
echo "========================================"
echo ""

# 检查 Python3 是否安装
if ! command -v python3 &> /dev/null; then
    echo "❌ 未检测到 Python3，请先安装："
    echo "   brew install python3"
    echo ""
    echo "按任意键退出..."
    read -n 1
    exit 1
fi

echo "✅ Python3: $(python3 --version)"

# 检查 .env 文件
if [ ! -f ".env" ]; then
    echo "⚠️  未找到 .env 文件，AI 聊天功能将使用本地回复系统"
else
    echo "✅ 配置文件: .env"
fi

# 检查端口是否被占用
PORT=5001
if lsof -i :$PORT &> /dev/null; then
    echo "⚠️  端口 $PORT 已被占用，正在尝试释放..."
    lsof -t -i :$PORT | xargs kill 2>/dev/null
    sleep 1
fi

echo ""
echo "🚀 正在启动服务..."
echo "   访问地址: http://localhost:$PORT"
echo ""
echo "⚠️  首次使用软件检测功能时，macOS 可能会弹出权限请求"
echo "   请在「系统设置 > 隐私与安全性 > 自动化」中允许终端控制「系统事件」"
echo ""
echo "按 Ctrl+C 停止服务"
echo "========================================"
echo ""

# 启动服务
python3 app.py
