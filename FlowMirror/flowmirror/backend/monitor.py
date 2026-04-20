import time
import json
import platform
import requests
from datetime import datetime
import os
import logging

# ─── 日志配置 ───
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─── 路径配置（使用绝对路径，与 app.py 保持一致） ───
base_dir = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(base_dir, 'data')
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# ─── 应用分类（与 app.py 保持一致） ───
APP_CATEGORIES = {
    "work": ["Word", "Excel", "PowerPoint", "VS Code", "Notion", "Figma", "PDF",
             "Microsoft Word", "Microsoft Excel", "Microsoft PowerPoint"],
    "browser": ["Chrome", "Edge", "Safari", "Firefox", "Microsoft Edge"],
    "communication": ["WeChat", "Slack", "Email", "飞书", "QQ", "微信",
                      "Microsoft Outlook", "Mail"],
    "entertainment": ["Spotify", "YouTube", "Netflix"]
}

def get_app_category(app_name):
    """获取应用分类（与 app.py 逻辑一致）"""
    for category, apps in APP_CATEGORIES.items():
        if app_name in apps:
            return category
    # 模糊匹配：检查应用名是否包含关键词
    app_lower = app_name.lower()
    if any(kw in app_lower for kw in ["word", "excel", "powerpoint", "vscode", "code", "notion", "figma", "pdf"]):
        return "work"
    if any(kw in app_lower for kw in ["chrome", "edge", "safari", "firefox"]):
        return "browser"
    if any(kw in app_lower for kw in ["wechat", "微信", "slack", "qq", "飞书", "outlook", "mail"]):
        return "communication"
    if any(kw in app_lower for kw in ["spotify", "youtube", "netflix"]):
        return "entertainment"
    return "other"

def get_active_app():
    """获取当前活跃应用"""
    if platform.system() == "Windows":
        try:
            import win32api
            import win32gui
            hwnd = win32gui.GetForegroundWindow()
            return win32gui.GetWindowText(hwnd)
        except ImportError:
            logger.warning("Windows API 不可用，请安装 pywin32")
            return "Unknown"
    elif platform.system() == "Darwin":  # macOS
        try:
            from AppKit import NSWorkspace
            workspace = NSWorkspace.sharedWorkspace()
            active_app = workspace.activeApplication()
            return active_app['NSApplicationName']
        except ImportError:
            logger.warning("AppKit 不可用")
            return "Unknown"
    elif platform.system() == "Linux":
        try:
            import subprocess
            result = subprocess.run(['xdotool', 'getwindowfocus', 'getwindowname'],
                                  capture_output=True, text=True)
            return result.stdout.strip() or "Unknown"
        except FileNotFoundError:
            logger.warning("xdotool 未安装，请运行: sudo apt install xdotool")
            return "Unknown"
    return "Unknown"

def save_data(data):
    """保存数据到本地"""
    filename = os.path.join(DATA_DIR, f"{datetime.now().strftime('%Y-%m-%d')}.json")
    with open(filename, 'w') as f:
        json.dump(data, f, indent=2)
    logger.info(f"数据已保存到 {filename}")

def upload_data(data):
    """上传数据到后端"""
    try:
        response = requests.post('http://localhost:5001/api/analyze',
                                json=data,
                                headers={'Content-Type': 'application/json'},
                                timeout=10)
        logger.info(f"数据上传成功: {response.status_code}")
        return response.json()
    except Exception as e:
        logger.error(f"数据上传失败: {e}")
        return None

def monitor_app_usage():
    """监控应用使用情况"""
    logger.info("开始监控应用使用情况...")
    logger.info("按 Ctrl+C 停止监控")

    data = []
    current_app = None
    start_time = None

    try:
        while True:
            app = get_active_app()
            now = datetime.now()

            if app != current_app:
                if current_app:
                    # 记录上一个应用的使用时间
                    data.append({
                        "app": current_app,
                        "start": start_time.strftime("%H:%M"),
                        "end": now.strftime("%H:%M"),
                        "category": get_app_category(current_app),
                        "activity": "active"
                    })
                    # 每10条记录上传一次数据
                    if len(data) >= 10:
                        save_data(data)
                        result = upload_data(data)
                        if result:
                            logger.info(f"分析结果: 摆烂指数 = {result['slacking_score']}, 状态 = {result['status']}")
                        data = []

                current_app = app
                start_time = now
                logger.info(f"当前应用: {app}")

            time.sleep(1)  # 每秒检查一次
    except KeyboardInterrupt:
        logger.info("监控已停止")
        # 保存最后一次数据
        if current_app:
            now = datetime.now()
            data.append({
                "app": current_app,
                "start": start_time.strftime("%H:%M"),
                "end": now.strftime("%H:%M"),
                "category": get_app_category(current_app),
                "activity": "active"
            })
            save_data(data)
            result = upload_data(data)
            if result:
                logger.info(f"最终分析结果: 摆烂指数 = {result['slacking_score']}, 状态 = {result['status']}")

if __name__ == "__main__":
    monitor_app_usage()
