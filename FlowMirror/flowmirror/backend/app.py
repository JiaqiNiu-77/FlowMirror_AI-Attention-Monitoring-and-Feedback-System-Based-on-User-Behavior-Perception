"""
FlowMirror 后端 - 纯 Python 标准库实现（无需 Flask）
使用 http.server 替代 Flask，提供相同的 API 接口
"""
import random
import re
import datetime
import platform
import subprocess
import threading
import time
import sqlite3
from datetime import timedelta
from datetime import datetime as dt
import json
import os
import logging
import urllib.request
import urllib.error
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import io

# ─── 日志配置 ───
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ─── 路径与环境变量 ───
base_dir = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(base_dir, '.env')

# 数据库路径
DB_PATH = os.path.join(base_dir, 'flowmirror.db')

# 手动加载 .env
OPENAI_API_KEY = None
OPENAI_BASE_URL = 'https://openrouter.ai/api/v1'
OPENAI_MODEL = 'openai/gpt-3.5-turbo'
OPENAI_FALLBACK_MODELS = []
if os.path.exists(env_path):
    with open(env_path, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, val = line.split('=', 1)
                key = key.strip()
                val = val.strip()
                if key == 'OPENAI_API_KEY':
                    OPENAI_API_KEY = val
                elif key == 'OPENAI_BASE_URL':
                    OPENAI_BASE_URL = val
                elif key == 'OPENAI_MODEL':
                    OPENAI_MODEL = val
                elif key == 'OPENAI_FALLBACK_MODELS':
                    OPENAI_FALLBACK_MODELS = [item.strip() for item in val.split(',') if item.strip()]

DATA_DIR = os.path.join(base_dir, 'data')
if not os.path.exists(DATA_DIR):
    os.makedirs(DATA_DIR)

# ─── 数据库初始化 ───
def init_database():
    """初始化SQLite数据库，创建必要的表"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    # 启用 WAL 模式，避免并发写入时 disk I/O error
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=5000')
    cursor = conn.cursor()
    
    # 创建检测记录表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS behavior_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            app TEXT NOT NULL,
            start_time TEXT,
            end_time TEXT,
            duration_minutes REAL,
            category TEXT,
            activity TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 创建分析结果表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS analysis_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            total_duration REAL,
            switch_count INTEGER,
            avg_duration REAL,
            max_continuous REAL,
            slacking_score INTEGER,
            status TEXT,
            summary TEXT,
            time_distribution TEXT,
            app_usage TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # 创建AI对话记录表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            user_message TEXT NOT NULL,
            ai_response TEXT NOT NULL,
            source TEXT,
            model TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    conn.commit()
    conn.close()
    logger.info(f"数据库初始化完成: {DB_PATH}")

# 初始化数据库
init_database()

if OPENAI_API_KEY:
    logger.info("已从 .env 加载 OPENAI_API_KEY")
else:
    logger.warning("未检测到 OPENAI_API_KEY，AI 聊天功能将使用本地回复系统")

# ─── 应用分类 ───
APP_CATEGORIES = {
    "work": ["Word", "Excel", "PowerPoint", "VS Code", "Notion", "Figma", "PDF"],
    "browser": ["Chrome", "Edge", "Safari", "Firefox", "Microsoft Edge"],
    "communication": ["WeChat", "Slack", "Email", "飞书", "QQ", "微信"],
    "entertainment": ["Spotify", "YouTube", "Netflix"]
}

APPS = ["Chrome", "Word", "Excel", "PowerPoint", "WeChat", "VS Code", "Spotify", "Notion", "Figma", "Email", "PDF"]

# ─── 工具函数 ───

def get_app_category(app):
    for category, apps in APP_CATEGORIES.items():
        if app in apps:
            return category
    return "other"

def get_duration(start_time, end_time):
    start = dt.strptime(start_time, "%H:%M")
    end = dt.strptime(end_time, "%H:%M")
    duration = (end - start).total_seconds() / 60
    # 处理跨天的情况（start时间晚于end时间）
    if duration < 0:
        duration += 24 * 60
    return duration


def get_item_duration(item):
    """兼容 start/end 形式和 duration 形式的数据"""
    if "duration_minutes" in item:
        try:
            return max(0.0, float(item["duration_minutes"]))
        except (TypeError, ValueError):
            return 0.0

    if "duration" in item:
        try:
            raw_duration = float(item["duration"])
            unit = item.get("duration_unit", "seconds")
            if unit == "minutes":
                return max(0.0, raw_duration)
            if unit == "hours":
                return max(0.0, raw_duration * 60)
            return max(0.0, raw_duration / 60)
        except (TypeError, ValueError):
            return 0.0

    start_time = item.get("start")
    end_time = item.get("end")
    if start_time and end_time:
        return get_duration(start_time, end_time)
    return 0.0

def _resolve_date_key(target_date=None):
    return target_date or dt.now().strftime('%Y-%m-%d')


def _get_data_file_path(target_date=None):
    return os.path.join(DATA_DIR, f"{_resolve_date_key(target_date)}.json")


def _safe_json_loads(raw_value, default):
    try:
        parsed = json.loads(raw_value) if raw_value else None
        return parsed if isinstance(parsed, type(default)) else default
    except Exception:
        return default


def _merge_numeric_maps(base_map, delta_map):
    merged = dict(base_map or {})
    for key, value in (delta_map or {}).items():
        merged[key] = round(float(merged.get(key, 0) or 0) + float(value or 0), 2)
    return merged


def save_data(data, target_date=None):
    filename = _get_data_file_path(target_date)
    
    # 读取现有数据
    existing_data = []
    if os.path.exists(filename):
        try:
            with open(filename, 'r') as f:
                existing_data = json.load(f)
            if not isinstance(existing_data, list):
                existing_data = []
        except Exception as e:
            logger.error(f"读取数据文件失败: {e}")
            existing_data = []
    
    # 合并数据
    merged_data = []
    
    # 处理现有数据
    for item in existing_data:
        if isinstance(item, dict) and 'start' in item and 'end' in item:
            merged_data.append(item)
    
    # 处理新数据
    for item in data:
        if isinstance(item, dict):
            # 只持久化原始行为时间片，避免监测汇总数据污染当天文件
            if 'start' in item and 'end' in item:
                merged_data.append(item)
    
    # 保存合并后的数据
    with open(filename, 'w') as f:
        json.dump(merged_data, f, indent=2)
    logger.info(f"数据已保存到 {filename}，共 {len(merged_data)} 条记录")

def save_app_usage_data(data, switch_count=None, app_count=None, target_date=None, persist_mode='delta'):
    """保存 app_usage 格式的数据到当天文件。

    persist_mode:
    - delta: 传入的是本次新增数据，累加到当天文件
    - absolute: 传入的是当天最终汇总，直接覆盖当天文件
    """
    filename = _get_data_file_path(target_date)

    # 读取现有数据
    existing_data = []
    if persist_mode == 'delta' and os.path.exists(filename):
        try:
            with open(filename, 'r') as f:
                existing_data = json.load(f)
            if not isinstance(existing_data, list):
                existing_data = []
        except Exception as e:
            logger.error(f"读取数据文件失败: {e}")
            existing_data = []

    # 构建现有 app 用时映射（用于累加）
    existing_app_map = {}
    prev_switch_count = 0
    prev_app_count = 0
    for item in existing_data:
        if isinstance(item, dict):
            if item.get('_meta') == 'summary':
                prev_switch_count = item.get('cumulative_switch_count', 0)
                prev_app_count = item.get('cumulative_app_count', 0)
                continue
            app = item.get('app', 'Unknown')
            dur = item.get('duration_minutes', 0)
            existing_app_map[app] = existing_app_map.get(app, 0) + dur

    # 累加新数据
    for item in data:
        if isinstance(item, dict):
            app = item.get('app', 'Unknown')
            dur = item.get('duration_minutes', 0)
            existing_app_map[app] = existing_app_map.get(app, 0) + dur

    # 切换次数：直接累加（每次停止监测时的真实切换次数 +1）
    if persist_mode == 'absolute':
        cumulative_switch = max(0, int(switch_count or 0))
    else:
        cumulative_switch = prev_switch_count + (switch_count or 0)

    # 应用数量：取历史最大值
    current_app_count = len([app for app, dur in existing_app_map.items() if dur > 0])
    if persist_mode == 'absolute':
        cumulative_apps = max(0, int(app_count if app_count is not None else current_app_count))
    elif app_count is not None:
        cumulative_apps = max(prev_app_count, app_count)
    else:
        cumulative_apps = max(prev_app_count, current_app_count)

    # 转换回列表格式保存
    merged_data = []
    for app, dur in existing_app_map.items():
        if dur > 0:
            merged_data.append({
                "app": app,
                "duration_minutes": round(dur, 2),
                "duration_unit": "minutes",
                "category": "other",
                "activity": "active"
            })

    # 追加累计统计元数据
    merged_data.append({
        "_meta": "summary",
        "cumulative_switch_count": cumulative_switch,
        "cumulative_app_count": cumulative_apps
    })

    with open(filename, 'w') as f:
        json.dump(merged_data, f, indent=2)
    logger.info(f"app_usage 数据已保存（模式: {persist_mode}），切换 {cumulative_switch} 次，应用 {cumulative_apps} 个")


def load_today_timeline_data(target_date=None):
    """读取当天行为数据（支持时间片格式和 app_usage 格式）"""
    filename = _get_data_file_path(target_date)
    if not os.path.exists(filename):
        return []

    try:
        with open(filename, 'r') as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [
            item for item in data
            if isinstance(item, dict) and ('start' in item or 'duration_minutes' in item)
        ]
    except Exception as e:
        logger.error(f"加载当天行为数据失败: {e}")
        return []

def save_behavior_to_db(data, target_date=None):
    """保存检测记录到数据库"""
    if not data:
        return
    
    today = _resolve_date_key(target_date)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    
    # 先删除今天的记录（避免重复）
    cursor.execute('DELETE FROM behavior_records WHERE date = ?', (today,))
    
    # 插入新记录
    for item in data:
        if isinstance(item, dict):
            app = item.get('app', 'Unknown')
            start_time = item.get('start')
            end_time = item.get('end')
            duration = get_item_duration(item)
            category = item.get('category', 'other')
            activity = item.get('activity', 'active')
            
            cursor.execute('''
                INSERT INTO behavior_records 
                (date, app, start_time, end_time, duration_minutes, category, activity)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (today, app, start_time, end_time, duration, category, activity))
    
    conn.commit()
    conn.close()
    logger.info(f"检测记录已保存到数据库，今日共 {len(data)} 条记录")

def save_analysis_to_db(result, target_date=None, persist_mode='delta'):
    """保存分析结果到数据库。

    persist_mode:
    - delta: result 是本次新增数据，累加到当天记录
    - absolute: result 是当天最终累计，直接覆盖当天记录
    """
    if not result:
        return

    today = _resolve_date_key(target_date)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    # 读取当天已有记录
    cursor.execute('''
        SELECT total_duration, switch_count, app_usage, time_distribution
        FROM analysis_results WHERE date = ?
    ''', (today,))
    existing = cursor.fetchone()

    new_duration = result.get('total_duration', 0) or 0
    new_switch = result.get('switch_count', 0) or 0
    new_app_usage = result.get('app_usage', {}) or {}
    new_time_distribution = result.get('time_distribution', {}) or {}

    if persist_mode == 'absolute':
        total_duration = round(float(new_duration or 0), 2)
        total_switch = max(0, int(new_switch or 0))
        merged_app_usage = {
            app: round(float(duration or 0), 2)
            for app, duration in new_app_usage.items()
            if float(duration or 0) > 0
        }
        merged_time_distribution = {
            key: round(float(duration or 0), 2)
            for key, duration in new_time_distribution.items()
        }
    elif existing:
        existing_app_usage = _safe_json_loads(existing[2], {})
        existing_time_distribution = _safe_json_loads(existing[3], {})
        total_duration = (existing[0] or 0) + new_duration
        total_switch = (existing[1] or 0) + new_switch
        merged_app_usage = _merge_numeric_maps(existing_app_usage, new_app_usage)
        merged_time_distribution = _merge_numeric_maps(existing_time_distribution, new_time_distribution)
    else:
        total_duration = new_duration
        total_switch = new_switch
        merged_app_usage = {
            app: round(float(duration or 0), 2)
            for app, duration in new_app_usage.items()
        }
        merged_time_distribution = {
            key: round(float(duration or 0), 2)
            for key, duration in new_time_distribution.items()
        }

    merged_analysis = analyze_behavior([
        {
            "app": app,
            "duration_minutes": duration,
            "duration_unit": "minutes",
            "category": "idle" if app == "Idle" else "other",
            "activity": "idle" if app == "Idle" else "active"
        }
        for app, duration in merged_app_usage.items()
        if float(duration or 0) > 0
    ], switch_count_override=total_switch)

    cursor.execute('''
        INSERT OR REPLACE INTO analysis_results
        (date, total_duration, switch_count, avg_duration, max_continuous,
         slacking_score, status, summary, time_distribution, app_usage)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        today,
        total_duration,
        total_switch,
        merged_analysis.get('avg_duration'),
        merged_analysis.get('max_continuous'),
        merged_analysis.get('slacking_score'),
        merged_analysis.get('status'),
        merged_analysis.get('summary'),
        json.dumps(merged_time_distribution),
        json.dumps(merged_app_usage)
    ))

    conn.commit()
    conn.close()
    logger.info(f"分析结果已保存到数据库: {today}（模式: {persist_mode}）")

def save_chat_to_db(user_message, ai_response, source='local', model=''):
    """保存AI对话记录到数据库"""
    today = dt.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    
    cursor.execute('''
        INSERT INTO chat_records 
        (date, user_message, ai_response, source, model)
        VALUES (?, ?, ?, ?, ?)
    ''', (today, user_message, ai_response, source, model))
    
    conn.commit()
    conn.close()
    logger.info(f"对话记录已保存到数据库")

def get_history_data(days=7):
    """获取历史数据"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()
    
    # 获取最近的分析结果
    cursor.execute('''
        SELECT date, total_duration, switch_count, slacking_score, status, summary
        FROM analysis_results
        ORDER BY date DESC
        LIMIT ?
    ''', (days,))
    analysis_results = cursor.fetchall()
    
    # 获取最近的检测记录
    cursor.execute('''
        SELECT date, app, start_time, end_time, duration_minutes, category
        FROM behavior_records
        ORDER BY date DESC, start_time DESC
    ''')
    behavior_records = cursor.fetchall()
    
    # 获取最近的对话记录
    cursor.execute('''
        SELECT date, user_message, ai_response, source, created_at
        FROM chat_records
        ORDER BY created_at DESC
        LIMIT 50
    ''')
    chat_records = cursor.fetchall()
    
    conn.close()
    
    return {
        'analysis_results': [
            {
                'date': r[0],
                'total_duration': r[1],
                'switch_count': r[2],
                'slacking_score': r[3],
                'status': r[4],
                'summary': r[5]
            } for r in analysis_results
        ],
        'behavior_records': [
            {
                'date': r[0],
                'app': r[1],
                'start_time': r[2],
                'end_time': r[3],
                'duration_minutes': r[4],
                'category': r[5]
            } for r in behavior_records
        ],
        'chat_records': [
            {
                'date': r[0],
                'user_message': r[1],
                'ai_response': r[2],
                'source': r[3],
                'created_at': r[4]
            } for r in chat_records
        ]
    }

def get_personality_summary():
    """获取人格总结 - 累加所有天的数据，永不清零"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    cursor = conn.cursor()

    # 获取所有天的分析结果
    cursor.execute('''
        SELECT date, total_duration, switch_count, slacking_score, status, summary,
               time_distribution, app_usage
        FROM analysis_results
        ORDER BY date ASC
    ''')
    all_results = cursor.fetchall()
    conn.close()

    if not all_results:
        return {
            "total_days": 0,
            "total_duration": 0,
            "avg_daily_duration": 0,
            "total_switch_count": 0,
            "avg_slacking_score": 0,
            "best_day": None,
            "worst_day": None,
            "personality_type": "尚未形成",
            "personality_description": "开始监测后，系统将根据你的工作习惯生成专属人格总结。",
            "personality_emoji": "🔮",
            "daily_scores": [],
            "top_apps": {},
            "time_distribution_total": {},
            "status_distribution": {}
        }

    # 累加统计
    total_duration = 0
    total_switch = 0
    total_slacking = 0
    best_day = None
    worst_day = None
    best_score = 101
    worst_score = -1
    daily_scores = []
    all_app_usage = {}
    time_dist_total = {"productive": 0, "fragmented": 0, "idle": 0, "communication": 0}
    status_counts = {}

    for r in all_results:
        date, duration, switches, score, status, summary, time_dist_json, app_usage_json = r
        total_duration += (duration or 0)
        total_switch += (switches or 0)
        total_slacking += (score or 0)

        if score is not None:
            daily_scores.append({"date": date, "score": score})
            if score < best_score:
                best_score = score
                best_day = {"date": date, "score": score, "status": status}
            if score > worst_score:
                worst_score = score
                worst_day = {"date": date, "score": score, "status": status}

        # 累加应用使用时长
        if app_usage_json:
            try:
                app_usage = json.loads(app_usage_json)
                for app, mins in app_usage.items():
                    all_app_usage[app] = all_app_usage.get(app, 0) + mins
            except:
                pass

        # 累加时间分布
        if time_dist_json:
            try:
                td = json.loads(time_dist_json)
                for key in time_dist_total:
                    time_dist_total[key] += td.get(key, 0)
            except:
                pass

        # 统计状态分布
        if status:
            status_counts[status] = status_counts.get(status, 0) + 1

    total_days = len(all_results)
    avg_daily_duration = round(total_duration / total_days, 1) if total_days > 0 else 0
    avg_slacking = round(total_slacking / total_days, 1) if total_days > 0 else 0

    # 排序 top 应用
    top_apps = dict(sorted(all_app_usage.items(), key=lambda x: x[1], reverse=True)[:10])

    # 根据数据判断人格类型
    personality_type, personality_desc, personality_emoji = _derive_personality(
        avg_slacking, avg_daily_duration, total_switch, total_days,
        time_dist_total, status_counts, top_apps
    )

    return {
        "total_days": total_days,
        "total_duration": round(total_duration, 1),
        "avg_daily_duration": avg_daily_duration,
        "total_switch_count": total_switch,
        "total_switch": total_switch,
        "total_apps": len(top_apps),
        "avg_slacking_score": avg_slacking,
        "best_day": best_day,
        "worst_day": worst_day,
        "personality_type": personality_type,
        "personality_description": personality_desc,
        "personality_emoji": personality_emoji,
        "daily_scores": daily_scores,
        "top_apps": top_apps,
        "time_distribution_total": {k: round(v, 1) for k, v in time_dist_total.items()},
        "status_distribution": status_counts
    }

def _derive_personality(avg_score, avg_duration, total_switch, total_days,
                         time_dist, status_counts, top_apps):
    """根据累计数据推导人格类型（16种摆烂人格）"""
    if total_days < 1:
        return "尚未形成", "开始监测后，系统将根据你的工作习惯生成专属人格总结。", "🔮"

    productive_ratio = time_dist.get("productive", 0) / max(time_dist.get("productive", 0) + time_dist.get("fragmented", 0) + time_dist.get("idle", 0) + time_dist.get("communication", 0), 1)
    idle_ratio = time_dist.get("idle", 0) / max(sum(time_dist.values()), 1)
    comm_ratio = time_dist.get("communication", 0) / max(sum(time_dist.values()), 1)

    # 判断是否有社交类应用（微信、QQ、飞书、钉钉等）
    social_apps = {"WeChat", "QQ", "飞书", "钉钉", "Telegram", "Slack", "微信"}
    social_ratio = sum(top_apps.get(a, 0) for a in social_apps) / max(sum(top_apps.values()), 1)

    # 判断是否有娱乐类应用
    entertainment_apps = {"Steam", "游戏", "Game", "YouTube", "Bilibili", "抖音", "TikTok", "Netflix"}
    entertainment_ratio = sum(top_apps.get(a, 0) for a in entertainment_apps) / max(sum(top_apps.values()), 1)

    # 判断是否有音乐类应用
    music_apps = {"Spotify", "Apple Music", "网易云音乐", "QQ音乐", "QQ音乐"}
    music_ratio = sum(top_apps.get(a, 0) for a in music_apps) / max(sum(top_apps.values()), 1)

    # 切换频率（每分钟切换次数）
    switch_rate = total_switch / max(avg_duration, 1)

    # 16种人格判定
    if avg_score <= 20 and avg_duration > 120:
        return "深度专注大师", f"专注力MAX，一旦进入心流状态谁也拦不住！平均每天专注 {avg_duration:.0f} 分钟，摆烂指数仅 {avg_score:.0f}。", "🧠"
    elif avg_score <= 20:
        return "闪电产出王", f"效率爆表，别人还在发呆你已经交稿了！平均摆烂指数 {avg_score:.0f}，短时高效是你的超能力。", "⚡"
    elif avg_score <= 35 and productive_ratio > 0.5:
        return "精准狙击手", f"目标明确，直奔主题，绝不浪费时间。摆烂指数 {avg_score:.0f}，高效产出占比 {productive_ratio*100:.0f}%。", "🎯"
    elif avg_score <= 35:
        return "学霸附体", f"学习工作两不误，知识就是你的武器。摆烂指数 {avg_score:.0f}，保持这种好习惯！", "📚"
    elif avg_score <= 50 and social_ratio > 0.3:
        return "社交蝴蝶", f"微信QQ飞书钉钉来回切换，社交永不掉线！摆烂指数 {avg_score:.0f}，社交占比 {social_ratio*100:.0f}%。", "📱"
    elif avg_score <= 50 and music_ratio > 0.15:
        return "BGM打工仔", f"没有音乐我活不了，边听歌边干活才是人生！摆烂指数 {avg_score:.0f}，BGM是你最好的工友。", "🎵"
    elif avg_score <= 50 and switch_rate > 0.15:
        return "切换狂魔", f"一秒切5个APP，多任务处理大师（自封）！摆烂指数 {avg_score:.0f}，每分钟切换 {switch_rate:.2f} 次。", "🔄"
    elif avg_score <= 50:
        return "佛系打工", f"随缘工作，随缘摆烂，一切看心情。摆烂指数 {avg_score:.0f}，这种节奏其实挺健康的。", "☯️"
    elif avg_score <= 65 and social_ratio > 0.25:
        return "社交蝴蝶", f"工作群里水群，朋友圈里冲浪，社交永不掉线。摆烂指数 {avg_score:.0f}，社交占比 {social_ratio*100:.0f}%。", "📱"
    elif avg_score <= 65 and switch_rate > 0.2:
        return "切换狂魔", f"APP切换如翻书，注意力分散是你的常态。摆烂指数 {avg_score:.0f}，试着减少切换频率。", "🔄"
    elif avg_score <= 65:
        return "摸鱼达人", f"工作五分钟，摸鱼两小时，深谙职场生存之道。摆烂指数 {avg_score:.0f}，该收心啦！", "☕"
    elif avg_score <= 80 and entertainment_ratio > 0.2:
        return "游戏人间", f"工作？什么工作？我只知道Steam打折了！摆烂指数 {avg_score:.0f}，娱乐占比 {entertainment_ratio*100:.0f}%。", "🎮"
    elif avg_score <= 80 and idle_ratio > 0.4:
        return "树懒模式", f"慢…慢…慢…急什么，明天再说。摆烂指数 {avg_score:.0f}，空闲时间占比 {idle_ratio*100:.0f}%。", "🛋️"
    elif avg_score <= 80:
        return "摆烂王者", f"摆烂界的天花板，躺平也是一种艺术。摆烂指数 {avg_score:.0f}，从每天专注10分钟开始改变吧！", "🦥"
    elif avg_score <= 90:
        return "游戏人间", f"摆烂指数 {avg_score:.0f}！你已经进入了摆烂的至高境界。不过认识到问题就是改变的第一步！", "🎮"
    else:
        return "摆烂王者", f"摆烂指数高达 {avg_score:.0f}！你是摆烂界的传奇人物。建议从每天专注5分钟开始，慢慢找回状态！", "🦥"

# ─── 模拟数据生成 ───

def generate_mock_data():
    data = []
    current_time = dt.strptime("09:00", "%H:%M")
    end_time = dt.strptime("18:00", "%H:%M")
    patterns = [
        {"apps": ["Word", "PDF", "Word", "PDF"], "min_duration": 20, "max_duration": 40},
        {"apps": ["Chrome", "WeChat", "Word", "Chrome", "WeChat"], "min_duration": 2, "max_duration": 8},
        {"apps": ["Word", "PowerPoint", "Excel", "Chrome", "Word"], "min_duration": 5, "max_duration": 15},
        {"apps": ["Word", "Word", "Word"], "min_duration": 40, "max_duration": 60},
        {"apps": ["VS Code", "Chrome", "VS Code"], "min_duration": 30, "max_duration": 50}
    ]
    main_pattern = random.choice(patterns)
    while current_time < end_time:
        if random.random() < 0.8 and main_pattern["apps"]:
            app = random.choice(main_pattern["apps"])
            duration = random.randint(main_pattern["min_duration"], main_pattern["max_duration"])
        else:
            app = random.choice(APPS)
            duration = random.randint(5, 60)
        end = current_time + timedelta(minutes=duration)
        if end > end_time:
            end = end_time
        data.append({
            "app": app,
            "start": current_time.strftime("%H:%M"),
            "end": end.strftime("%H:%M"),
            "category": get_app_category(app),
            "activity": random.choice(["active", "idle"]) if duration > 10 else "active"
        })
        current_time = end
    return data

# ─── 行为分析 ───

def analyze_behavior(data, switch_count_override=None):
    if not data:
        return {
            "total_duration": 0, "switch_count": 0, "avg_duration": 0,
            "max_continuous": 0, "slacking_score": 0, "status": "稳定推进型",
            "tags": ["稳定推进型"],
            "summary": "暂无数据，请先使用电脑工作",
            "time_distribution": {"productive": 0, "fragmented": 0, "idle": 0, "communication": 0},
            "interference_sources": [], "focus_periods": [], "app_usage": {}
        }

    # 检查数据格式，如果是app_usage格式（每个应用一条记录），转换为时间片段格式
    if data and all('duration_minutes' in item or 'duration' in item for item in data):
        # 构建app_usage
        app_usage = {}
        for item in data:
            app = item.get('app', 'Unknown')
            duration = get_item_duration(item)
            app_usage[app] = app_usage.get(app, 0) + duration
        
        # 计算总时长
        total_duration = sum(app_usage.values())
        
        # 计算切换次数（简化计算：应用数量减1）
        if switch_count_override is not None:
            switch_count = max(0, int(switch_count_override))
        else:
            switch_count = len(app_usage) - 1
        
        # 计算平均时长
        avg_duration = total_duration / len(app_usage) if app_usage else 0
        
        # 计算最大连续时长（简化计算：最长的应用使用时长）
        max_continuous = max(app_usage.values()) if app_usage else 0
        
        # 计算时间分布
        time_distribution = {"productive": 0, "fragmented": 0, "idle": 0, "communication": 0}
        for app, duration in app_usage.items():
            if app == "Idle":
                time_distribution["idle"] += duration
            elif app in ["Messages", "Mail", "微信", "QQ", "Slack", "Discord"]:
                time_distribution["communication"] += duration
            elif duration < 10:
                time_distribution["fragmented"] += duration
            else:
                time_distribution["productive"] += duration
        
        # 计算干扰源
        interference_sources = []
        communication_time = time_distribution["communication"]
        if communication_time > total_duration * 0.3:
            interference_sources.append("频繁的沟通消息")
        if switch_count > 10:
            interference_sources.append("高频软件切换")
        
        # 计算专注时段
        focus_periods = []
        for app, duration in app_usage.items():
            if duration > 30 and app != "Idle" and app not in ["Messages", "Mail", "微信", "QQ", "Slack", "Discord"]:
                focus_periods.append({
                    "start": "--:--", "end": "--:--",
                    "app": app, "duration": round(duration, 2)
                })
        
        # 计算摆烂指数
        slacking_score = 0
        fragmented_ratio = time_distribution["fragmented"] / total_duration if total_duration > 0 else 0
        slacking_score += fragmented_ratio * 30
        idle_ratio = time_distribution["idle"] / total_duration if total_duration > 0 else 0
        slacking_score += idle_ratio * 25
        communication_ratio = time_distribution["communication"] / total_duration if total_duration > 0 else 0
        slacking_score += communication_ratio * 20
        if total_duration > 0:
            switch_per_hour = switch_count / (total_duration / 60)
            if switch_per_hour > 20:
                slacking_score += 15
            elif switch_per_hour > 10:
                slacking_score += 5
        if max_continuous > 30:
            slacking_score -= 20
        elif max_continuous > 20:
            slacking_score -= 10
        slacking_score = max(0, min(100, slacking_score))
    else:
        # 原始逻辑：处理时间片段数据
        total_duration = sum(get_item_duration(item) for item in data)
        if switch_count_override is not None:
            switch_count = max(0, int(switch_count_override))
        else:
            switch_count = len(data) - 1 if len(data) > 0 else 0
        avg_duration = total_duration / len(data) if len(data) > 0 else 0

        max_continuous = 0
        current_continuous = 0
        continuous_blocks = []
        for i, item in enumerate(data):
            duration = get_item_duration(item)
            if i == 0:
                current_continuous = duration
            else:
                prev_app = data[i-1]["app"]
                current_app = item["app"]
                prev_category = data[i-1].get("category", "other")
                current_category = item.get("category", "other")
                if (current_app == prev_app or
                    (prev_category == "work" and current_category == "work") or
                    (prev_category == "browser" and current_category == "work") or
                    (prev_category == "work" and current_category == "browser")):
                    current_continuous += duration
                else:
                    if current_continuous > 10:
                        continuous_blocks.append(current_continuous)
                    current_continuous = duration
            max_continuous = max(max_continuous, current_continuous)
        if current_continuous > 10:
            continuous_blocks.append(current_continuous)

        time_distribution = {"productive": 0, "fragmented": 0, "idle": 0, "communication": 0}
        for item in data:
            duration = get_item_duration(item)
            category = item.get("category", "other")
            activity = item.get("activity", "active")
            if activity == "idle":
                time_distribution["idle"] += duration
            elif category == "communication":
                time_distribution["communication"] += duration
            elif duration < 10:
                time_distribution["fragmented"] += duration
            else:
                time_distribution["productive"] += duration

        interference_sources = []
        communication_count = sum(1 for item in data if item.get("category") == "communication")
        if communication_count > len(data) * 0.3:
            interference_sources.append("频繁的沟通消息")
        if switch_count > len(data) * 0.8:
            interference_sources.append("高频软件切换")

        focus_periods = []
        for i, item in enumerate(data):
            duration = get_item_duration(item)
            if duration > 30 and item.get("activity") == "active" and item.get("category") in ["work", "browser"]:
                focus_periods.append({
                    "start": item.get("start", "--:--"), "end": item.get("end", "--:--"),
                    "app": item["app"], "duration": round(duration, 2)
                })

        app_usage = {}
        for item in data:
            app = item["app"]
            duration = get_item_duration(item)
            app_usage[app] = app_usage.get(app, 0) + duration

        slacking_score = 0
        fragmented_ratio = time_distribution["fragmented"] / total_duration if total_duration > 0 else 0
        slacking_score += fragmented_ratio * 30
        idle_ratio = time_distribution["idle"] / total_duration if total_duration > 0 else 0
        slacking_score += idle_ratio * 25
        communication_ratio = time_distribution["communication"] / total_duration if total_duration > 0 else 0
        slacking_score += communication_ratio * 20
        if len(data) > 0 and total_duration > 0:
            switch_per_hour = switch_count / (total_duration / 60)
            if switch_per_hour > 20:
                slacking_score += 15
            elif switch_per_hour > 10:
                slacking_score += 5
        if continuous_blocks:
            avg_continuous = sum(continuous_blocks) / len(continuous_blocks)
            if avg_continuous > 30:
                slacking_score -= 20
            elif avg_continuous > 20:
                slacking_score -= 10
        slacking_score = max(0, min(100, slacking_score))

    # 生成状态和标签
    status = "稳定推进型"
    tags = ["稳定推进型"]
    if slacking_score < 20 and max_continuous > 120:
        status = "深度专注型"; tags = ["深度专注型", "高效工作"]
    elif slacking_score < 30 and time_distribution["productive"] > total_duration * 0.7:
        status = "高效产出型"; tags = ["高效产出型", "成果显著"]
    elif slacking_score < 30 and len(focus_periods) > 3:
        status = "专注时段型"; tags = ["专注时段型", "节奏良好"]
    elif slacking_score < 40 and switch_count < 15:
        status = "持续稳定型"; tags = ["持续稳定型", "专注度高"]
    elif switch_count > 25:
        status = "切换焦虑型"; tags = ["切换焦虑型"]
    elif avg_duration < 8:
        status = "伪努力型"; tags = ["伪努力型"]
    elif max_continuous > 90 and switch_count < 8:
        status = "精神内耗型"; tags = ["精神内耗型"]
    elif time_distribution["idle"] > total_duration * 0.4:
        status = "隐性摆烂型"; tags = ["隐性摆烂型"]

    summary = generate_ai_summary(slacking_score, switch_count, avg_duration, max_continuous, total_duration, time_distribution, interference_sources, focus_periods)

    return {
        "total_duration": round(total_duration, 2),
        "switch_count": switch_count,
        "avg_duration": round(avg_duration, 2),
        "max_continuous": round(max_continuous, 2),
        "slacking_score": int(slacking_score),
        "status": status,
        "tags": tags,
        "summary": summary,
        "time_distribution": {k: round(v, 2) for k, v in time_distribution.items()},
        "interference_sources": interference_sources,
        "focus_periods": focus_periods,
        "app_usage": {app: round(d, 2) for app, d in app_usage.items()}
    }

# ─── AI 总结生成 ───

def generate_ai_summary(slacking_score, switch_count, avg_duration, max_continuous, total_duration, time_distribution, interference_sources, focus_periods):
    if slacking_score > 70:
        summary = "你今天处于明显的低效状态，"
        if switch_count > 25:
            summary += f"一共切换了{switch_count}次窗口，注意力被严重碎片化。"
        if time_distribution["idle"] > 0:
            summary += f"同时存在较长时间的无效停留，"
        summary += "建议减少不必要的软件切换，专注于单个任务的持续推进。"
    elif slacking_score > 40:
        summary = "你今天有一定的工作投入，"
        if time_distribution["fragmented"] > time_distribution["productive"]:
            summary += "但时间被较多碎片化，"
        if interference_sources:
            summary += f"主要干扰来自{interference_sources[0]}，"
        summary += "建议优化工作流程，增加连续工作时间。"
    elif slacking_score < 20 and max_continuous > 120:
        summary = "你今天表现出了令人惊叹的深度专注能力！"
        summary += f"最长连续工作时间达到{int(max_continuous)}分钟，"
        if focus_periods:
            summary += f"在{focus_periods[0]['start']}到{focus_periods[0]['end']}期间进入了深度心流状态，"
        summary += "这种专注程度非常难得，继续保持这种卓越的工作状态！"
    elif slacking_score < 30 and time_distribution["productive"] > total_duration * 0.7:
        summary = "你今天的工作效率令人印象深刻！"
        summary += f"有效工作时间占比达到{int(time_distribution['productive']/total_duration*100)}%，"
        if max_continuous > 60:
            summary += f"最长连续工作时间达到{int(max_continuous)}分钟，"
        summary += "你的产出效率非常高，继续保持这种优秀的工作状态！"
    elif slacking_score < 30 and len(focus_periods) > 3:
        summary = "你今天的工作节奏非常出色！"
        summary += f"一天内达到了{len(focus_periods)}个黄金专注时段，"
        if focus_periods:
            summary += f"在{focus_periods[0]['start']}到{focus_periods[0]['end']}期间表现尤为突出，"
        summary += "这种良好的工作节奏有助于持续产出高质量的成果，继续保持！"
    elif slacking_score < 40 and switch_count < 15:
        summary = "你今天展现了出色的专注力和稳定性！"
        summary += f"仅切换了{switch_count}次窗口，平均停留时间达到{int(avg_duration)}分钟，"
        if max_continuous > 60:
            summary += f"最长连续工作时间达到{int(max_continuous)}分钟，"
        summary += "这种持续稳定的工作状态非常有利于深入思考和解决复杂问题，继续保持！"
    else:
        summary = "你今天工作状态良好，"
        if max_continuous > 60:
            summary += f"最长连续工作时间达到{int(max_continuous)}分钟，"
        if focus_periods:
            summary += f"在{focus_periods[0]['start']}到{focus_periods[0]['end']}期间进入了深度专注状态，"
        summary += "保持这种高效的工作模式，继续加油！"
    return summary


def parse_llm_error(exc):
    """提取更友好的模型调用失败原因"""
    if isinstance(exc, urllib.error.HTTPError):
        body = ""
        try:
            body = exc.read().decode('utf-8', errors='replace')
        except Exception:
            body = ""

        lower_body = body.lower()
        if exc.code == 403 and "not available in your region" in lower_body:
            return "当前配置的模型在你所在地区不可用"
        if exc.code == 402 and "insufficient credits" in lower_body:
            return "OpenRouter 账号余额不足，当前账号尚未购买 credits"
        if exc.code == 429:
            return "模型服务当前限流，请稍后重试"
        if body:
            return f"HTTP {exc.code}: {body}"
        return f"HTTP {exc.code}"

    if isinstance(exc, urllib.error.URLError):
        return f"网络请求失败：{exc.reason}"

    return str(exc)


def get_llm_model_candidates():
    """获取候选模型列表，支持主模型 + 备用模型"""
    candidates = []
    for model in [OPENAI_MODEL, *OPENAI_FALLBACK_MODELS]:
        if model and model not in candidates:
            candidates.append(model)
    return candidates


def build_chat_messages(message, page_context=None):
    """构建聊天消息，注入页面上下文但保持自然对话"""
    context_lines = []
    if isinstance(page_context, dict):
        page_name = page_context.get("page")
        if page_name:
            context_lines.append(f"- 当前页面: {page_name}")

        for key, label in [
            ("slacking_score", "摆烂指数"),
            ("productive_time_hours", "有效工作时间(小时)"),
            ("switch_count", "软件切换次数"),
            ("max_continuous_minutes", "最长连续时间(分钟)"),
            ("analysis_status", "分析状态"),
            ("analysis_summary", "AI分析总结"),
            ("analysis_app_usage", "分析页软件使用占比"),
            ("analysis_timeline", "分析页行为时间轴"),
            ("current_app", "当前应用"),
            ("monitoring_status", "监测状态"),
            ("monitoring_total_minutes", "总监测时间(分钟)"),
            ("monitoring_switch_count", "监测切换次数"),
            ("monitoring_app_count", "监测应用数量"),
            ("monitoring_app_usage", "监测页应用使用时间"),
            ("monitoring_logs", "监测页日志"),
            ("history_summary", "历史页本周总结"),
            ("history_daily_tags", "历史页每日标签"),
            ("pet_name", "宠物名称"),
            ("pet_level", "宠物等级"),
            ("pet_happiness", "宠物心情"),
            ("pet_exp", "宠物经验"),
            ("pet_total_focus_time", "宠物总专注时间"),
            ("pet_focus_streak", "宠物连续专注天数"),
            ("pet_rewards_earned", "宠物获得奖励数"),
            ("pet_mood", "宠物状态"),
            ("pet_rewards", "宠物奖励列表"),
            ("pet_logs", "宠物状态日志"),
        ]:
            value = page_context.get(key)
            if value not in (None, "", "--"):
                context_lines.append(f"- {label}: {value}")

    context_text = "\n".join(context_lines) if context_lines else "- 当前没有可用页面数据"

    system_prompt = (
        "你是 FlowMirror 的 AI 助手「小镜」，一个陪伴用户提升工作效率的智能伙伴。\n\n"

        "## 你的身份\n"
        "- 名字：小镜\n"
        "- 性格：温暖、专业、偶尔幽默，像一个懂效率的好朋友\n"
        "- 语气：自然口语化，像微信聊天一样轻松，不要像客服机器人\n\n"

        "## 你能做什么\n"
        "1. **解读工作数据**：结合页面上下文中的摆烂指数、软件切换次数、有效工作时间、连续专注时间等指标，给出有针对性的分析和建议\n"
        "2. **效率提升建议**：根据用户的工作模式，提供实用的时间管理、专注力提升、减少干扰等方法论\n"
        "3. **软件使用分析**：分析用户使用各类软件的时间和频率，指出潜在问题（如碎片化严重、娱乐占比过高等）\n"
        "4. **行为模式解读**：解释各种行为标签的含义（深度专注型、切换焦虑型、伪努力型、精神内耗型、隐性摆烂型等），帮助用户理解自己的工作模式\n"
        "5. **宠物系统互动**：回答关于宠物养成、等级、经验值、奖励、心情等问题，鼓励用户通过专注工作来培养宠物\n"
        "6. **产品使用指导**：指导用户如何使用 FlowMirror 的各个功能页面（首页、实时监测、结果分析、历史记录、宠物中心）\n"
        "7. **工作心理学**：分享关于专注力、拖延症、工作节奏、番茄工作法、深度工作等话题的知识\n"
        "8. **闲聊陪伴**：用户累了想聊几句也可以，适当关心用户的感受\n\n"

        "## 回答规则\n"
        "- 结合页面上下文中的真实数据来回答，**严禁编造**页面中没有的数据、结论或趋势\n"
        "- 如果用户问的数据在当前页面上下文中没有，诚实说明「当前页面暂无该数据」，不要瞎编\n"
        "- 回答长度根据问题灵活调整：简单问题2-3句话，复杂分析可以详细展开\n"
        "- 可以使用 **加粗**、短列表等 Markdown 格式让回答更清晰\n"
        "- 偶尔可以用 emoji 增加亲切感，但不要每句都堆砌\n"
        "- 给建议时要具体可执行，不要说空话套话（如「合理安排时间」改为「试试把大任务拆成25分钟的番茄钟」）\n\n"

        f"## 当前页面上下文\n{context_text}"
    )

    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message}
    ]


def call_llm_api(message, page_context=None):
    """调用外部大语言模型，按候选模型顺序尝试"""
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json"
    }

    last_error = "未配置可用模型"
    for model in get_llm_model_candidates():
        payload = json.dumps({
            "model": model,
            "messages": build_chat_messages(message, page_context),
            "temperature": 0.3,
            "max_tokens": 220
        }).encode('utf-8')

        req = urllib.request.Request(
            OPENAI_BASE_URL + "/chat/completions",
            data=payload,
            headers=headers,
            method='POST'
        )

        import ssl
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

        try:
            logger.info(f"正在调用 AI API，模型: {model}")
            with urllib.request.urlopen(req, timeout=10, context=context) as resp:
                resp_data = json.loads(resp.read().decode('utf-8'))
                ai_response = resp_data['choices'][0]['message']['content'].strip()
                logger.info(f"AI 回复成功，模型: {model}")
                return {
                    "ok": True,
                    "model": model,
                    "response": ai_response
                }
        except Exception as e:
            last_error = parse_llm_error(e)
            logger.warning(f"AI API调用失败，模型 {model}: {last_error}")

    return {
        "ok": False,
        "error": last_error
    }

# ─── 本地智能回复系统 ───

def use_local_response_system(message):
    """本地智能回复系统（LLM 不可用时的兜底方案）"""
    # 按主题分类的回复库，每个主题有多条回复随机选择
    responses = {
        # ── 问候与闲聊 ──
        '你好': ['你好呀！我是小镜，你的效率小伙伴 😊 有什么想聊的？', '嗨～今天工作状态怎么样？需要我帮你分析一下吗？'],
        '你好吗': ['我挺好的！随时准备帮你看看今天的工作表现～你呢，今天累不累？', '精神满满！等着帮你分析数据呢 😄'],
        '谢谢': ['不客气呀！有问题随时找我～', '能帮到你就好！继续加油哦 💪'],
        '再见': ['拜拜～记得休息，别太累了哦！', '下次见！祝你今天剩下的时间效率满满 ✨'],
        '累了': ['辛苦了！建议站起来活动一下，喝杯水，休息5-10分钟再继续，效率会更高的 💧', '累了就歇会儿吧～研究表明每工作50分钟休息10分钟是最优节奏，你今天休息够了吗？'],

        # ── 摆烂指数 ──
        '摆烂指数': [
            '摆烂指数是 FlowMirror 的核心指标，范围 0-100。**越低越好**——0分说明你今天简直是效率之神，100分嘛…你懂的 😅\n\n它是根据这些因素综合计算的：\n- 碎片化程度（频繁切换软件）\n- 空闲/发呆时间占比\n- 沟通软件打扰频率\n- 连续工作时长',
            '简单来说，摆烂指数就是你的「效率体检分数」。它会分析你一天中软件切换频率、有效工作时长、连续专注时间等数据，综合打出一个分数。\n\n**低于30**：表现优秀 👏\n**30-50**：中规中矩，有提升空间\n**高于50**：需要注意了，可能需要调整工作方式',
        ],
        '如何减少摆烂指数': [
            '降低摆烂指数的核心就是三个字：**少切换**。具体试试这几招：\n\n1. **番茄工作法**：25分钟只做一件事，期间关闭所有通知\n2. **批量处理消息**：别每来一条微信就看，集中到固定时间段回复\n3. **关闭不必要的标签页**：Chrome开了20个标签？先关掉15个\n4. **设置专注时段**：上午9-11点设为「勿扰时间」，这段时间只做最重要的任务',
        ],

        # ── 专注度与工作效率 ──
        '专注度': [
            '提升专注力其实没那么难，关键是**减少决策疲劳**：\n\n- 每天开始工作前，先确定今天最重要的3件事\n- 把大任务拆成30分钟以内能完成的小步骤\n- 手机放到看不见的地方（真的有用！）\n- 用 FlowMirror 的实时监测功能，随时看看自己有没有在偷偷切到微信 😏',
        ],
        '如何提高工作效率': [
            '分享几个我觉得特别好用的方法：\n\n1. **两分钟原则**：如果一件事2分钟能做完，立刻做，不要放到待办清单\n2. **吃青蛙**：每天早上第一件事做最难的任务（那只「青蛙」），之后一天都会轻松很多\n3. **时间块**：把一天分成几个时间块，每个块只做一类事\n4. **学会说不**：不是所有会议都需要参加，不是所有消息都需要秒回',
        ],
        '如何保持工作动力': [
            '动力这东西不能只靠意志力硬撑，试试这些「系统化」的方法：\n\n- **小胜利积累**：完成一个小任务就划掉它，那种满足感会上瘾的\n- **可视化进度**：看看 FlowMirror 的历史记录，看到自己一天天在进步就是最好的动力\n- **奖励机制**：专注工作1小时奖励自己刷10分钟手机（别超标就行 😂）\n- **找到意义**：想想这个任务完成后能带来什么，而不只是「我必须做完」',
        ],
        '番茄工作法': [
            '番茄工作法超简单的：\n\n🍅 **25分钟专注** → **5分钟休息** → 重复4次 → **15-30分钟长休息**\n\n关键规则：\n- 专注期间**绝对不**看手机、回消息、开新标签页\n- 如果被打断了，这个番茄作废，重新开始\n- 休息时**离开屏幕**，走动、喝水、看远处都行\n\nFlowMirror 的实时监测可以帮你看看自己是不是真的在专注哦～',
        ],

        # ── 软件使用 ──
        '软件使用': ['你可以在「结果分析」页面查看详细的软件使用占比图，看看时间都花在哪些软件上了。如果发现娱乐软件占比太高…嗯，你懂的 😏'],
        '软件切换': ['频繁切换软件是效率的隐形杀手！每次切换，你的大脑需要约23分钟才能重新进入深度专注状态。建议试试把同类任务集中处理，减少不必要的上下文切换。'],
        '行为时间轴': ['行为时间轴在「结果分析」页面，它会把你一天中使用各个软件的时间按顺序展示出来，像一部你今天的工作「纪录片」🎬。通过它你可以一眼看出哪些时段在专注、哪些时段在频繁切换。'],

        # ── 行为标签 ──
        '行为标签': [
            'FlowMirror 会根据你的工作数据自动打上行为标签，一共有这些类型：\n\n🟢 **正面标签**：\n- **深度专注型**：长时间沉浸在一件事中，效率爆表\n- **高效产出型**：有效工作时间占比超过70%\n- **专注时段型**：一天中有多个黄金专注时段\n- **持续稳定型**：切换少、节奏稳\n\n🔴 **需要注意的标签**：\n- **切换焦虑型**：窗口切换超过25次，注意力严重碎片化\n- **伪努力型**：看起来在忙，但每件事都只做几分钟\n- **精神内耗型**：长时间对着一个东西但产出很低\n- **隐性摆烂型**：40%以上的时间在发呆',
        ],

        # ── 产品使用 ──
        'FlowMirror是什么': ['FlowMirror 是一个**工作行为分析工具**，它能：\n\n- 📊 实时监测你使用哪些软件、用了多久\n- 📈 分析你的工作模式，计算摆烂指数\n- 🏷️ 自动给你打上行为标签（深度专注型、切换焦虑型等）\n- 🐱 通过宠物养成系统激励你保持专注\n- 📅 记录历史数据，追踪你的效率变化趋势\n\n简单说就是——让你看清自己一天的时间到底去哪了 😄'],
        '如何使用FlowMirror': ['使用超简单的，三步走：\n\n1. **生成数据**：点击首页的「生成今日行为数据」按钮\n2. **查看分析**：点击「分析我的一天」，看看你的摆烂指数和行为标签\n3. **持续追踪**：每天使用，在「历史记录」页面查看效率趋势\n\n如果想实时监测，还可以打开「实时监测」页面，它会自动追踪你当前正在使用的软件。'],
        'FlowMirror有什么功能': ['FlowMirror 的功能模块：\n\n- 🏠 **首页**：今日数据概览和快速操作\n- 📊 **实时监测**：自动追踪当前使用的软件\n- 📈 **结果分析**：详细的摆烂指数、行为标签、软件使用占比、行为时间轴\n- 📅 **历史记录**：查看最近7天的效率趋势\n- 🐱 **宠物中心**：通过专注工作培养你的专属宠物\n- 🤖 **AI 助手**：就是我啦！随时回答你的问题'],

        # ── 宠物系统 ──
        '宠物': ['你的小宠物正在等你回来呢！保持专注工作就能获得经验值，升级后宠物会变得更可爱哦 🐱 你可以在「宠物中心」页面查看宠物的详细状态。'],
        '等级': ['宠物等级越高越厉害！每次升级都会解锁新的外观和特效。保持30分钟以上的连续专注就能获得大量经验值，冲冲冲！'],
        '经验值': ['经验值满格就能升级！获得经验值的方式：完成30分钟以上的专注任务。专注时间越长，获得的经验值越多 ✨'],
        '奖励': ['专注工作就能解锁各种奖励！连续专注7天还有特殊宠物皮肤 🎁 具体可以在「宠物中心」的奖励列表查看。'],

        # ── 工作心理学 ──
        '拖延症': ['拖延的本质不是懒，而是**情绪管理问题**——你在逃避任务带来的负面情绪（焦虑、无聊、害怕做不好）。几个应对方法：\n\n1. **5分钟起步法**：告诉自己「只做5分钟」，通常开始之后就会继续下去\n2. **降低启动门槛**：把任务的第一步变得超级简单\n3. **原谅自己**：研究表明，越自责越拖延，接受自己偶尔的拖延反而能减少它\n4. **环境设计**：把手机放远一点，把工作相关的页面打开，减少「开始」的阻力'],
        '深度工作': ['深度工作（Deep Work）是 Cal Newport 提出的概念，指**在无干扰的状态下进行的高认知难度工作**。这种状态下你的产出质量和数量都会远超平时。\n\n要进入深度工作状态：\n- 至少预留 **90分钟** 不被打断的时间块\n- 提前告诉同事/家人这个时段不要打扰你\n- 关闭所有通知（手机静音、电脑开启勿扰模式）\n- 准备好水和一切需要的资料，避免中途起身'],
        '什么是有效工作时间': ['有效工作时间是指你**真正投入工作**的时间，不包括：\n\n- 发呆/走神的时间\n- 频繁切换软件造成的注意力恢复时间\n- 聊天、刷手机等非工作活动\n\nFlowMirror 会通过分析你的软件使用模式来估算有效工作时间。一般来说，knowledge worker 的有效工作时间占上班时间的 **60-70%** 就算不错了。'],
    }

    message_lower = message.lower()

    # 精确匹配
    for key, resp in responses.items():
        if key.lower() == message_lower:
            return random.choice(resp) if isinstance(resp, list) else resp

    # 包含匹配（优先匹配更长的关键词）
    matched_keys = [key for key in responses if key.lower() in message_lower]
    if matched_keys:
        matched_keys.sort(key=len, reverse=True)
        resp = responses[matched_keys[0]]
        return random.choice(resp) if isinstance(resp, list) else resp

    # 分词匹配
    message_words = set(message_lower.split())
    best_match = None
    best_common = 0
    for key, resp in responses.items():
        key_words = set(key.lower().split())
        common_words = message_words.intersection(key_words)
        if len(common_words) >= 2 and len(common_words) > best_common:
            best_common = len(common_words)
            best_match = resp
    if best_match:
        return random.choice(best_match) if isinstance(best_match, list) else best_match

    # 正则模式匹配
    if re.search(r'如何|怎样|怎么', message_lower):
        if '专注' in message_lower:
            return random.choice(responses['专注度'])
        elif '效率' in message_lower:
            return random.choice(responses['如何提高工作效率'])
        elif '摆烂' in message_lower:
            return random.choice(responses['如何减少摆烂指数'])
        elif '动力' in message_lower:
            return random.choice(responses['如何保持工作动力'])
        elif '拖延' in message_lower:
            return random.choice(responses['拖延症'])
        elif '番茄' in message_lower:
            return random.choice(responses['番茄工作法'])

    if re.search(r'什么是|什么叫|定义', message_lower):
        if '摆烂' in message_lower:
            return random.choice(responses['摆烂指数'])
        elif '专注' in message_lower or '深度' in message_lower:
            return random.choice(responses['深度工作'])
        elif '行为' in message_lower and '标签' in message_lower:
            return random.choice(responses['行为标签'])
        elif 'flowmirror' in message_lower:
            return random.choice(responses['FlowMirror是什么'])
        elif '有效' in message_lower and '时间' in message_lower:
            return random.choice(responses['什么是有效工作时间'])

    # 情感关键词匹配
    if any(w in message_lower for w in ['累', '烦', '不想', '好难', '崩溃', '焦虑', '压力']):
        return random.choice(responses['累了'])

    # 默认回复（更自然、更有引导性）
    default_responses = [
        '这个问题挺有意思的！不过我目前是本地模式，回答范围有限。你可以试试问我关于摆烂指数、专注度提升、软件使用分析、宠物养成等方面的问题～',
        '嗯…让我想想 🤔 你可以问我这些话题：\n- 摆烂指数是怎么算的\n- 如何提高专注力\n- 各种行为标签是什么意思\n- 宠物系统怎么玩\n- 番茄工作法、深度工作等效率方法',
        '我是 FlowMirror 的 AI 助手小镜，最擅长聊工作效率相关的话题！比如「我的摆烂指数怎么降」「什么是深度专注」「怎么克服拖延症」等等～',
    ]
    return random.choice(default_responses)


# ─── HTTP 请求处理器 ───

FRONTEND_DIR = os.path.join(os.path.dirname(base_dir), 'frontend')

class FlowMirrorHandler(BaseHTTPRequestHandler):
    """处理所有 API 请求和前端静态文件"""

    macos_warning_flags = {
        "applescript_permission_logged": False,
        "quartz_missing_logged": False,
        "appkit_missing_logged": False,
    }
    last_detection_error = None

    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode('utf-8'))

    def _send_html(self, filepath):
        """发送前端静态文件"""
        try:
            with open(filepath, 'rb') as f:
                content = f.read()
            self.send_response(200)
            if filepath.endswith('.html'):
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Expires', '0')
                # 注入时间戳到 HTML，确保浏览器不使用缓存
                ts = f'<!-- cache-bust: {int(time.time())} -->'
                content = content.replace(b'</head>', ts.encode() + b'</head>', 1)
            elif filepath.endswith('.css'):
                self.send_header('Content-Type', 'text/css; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            elif filepath.endswith('.js'):
                self.send_header('Content-Type', 'application/javascript; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            elif filepath.endswith('.png') or filepath.endswith('.jpg') or filepath.endswith('.jpeg'):
                self.send_header('Content-Type', 'image/png')
            else:
                self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', len(content))
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, "File not found")

    def _send_error(self, message, status=400):
        self._send_json({"error": message}, status)

    def _read_body(self):
        try:
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length > 0:
                body = self.rfile.read(content_length)
                return json.loads(body.decode('utf-8'))
        except (ValueError, json.JSONDecodeError) as e:
            logger.warning(f"请求体解析失败: {e}")
        return None

    def do_OPTIONS(self):
        """处理 CORS 预检请求"""
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        if self.path == '/api/generate-data':
            self._handle_generate_data()
        elif self.path == '/api/monitoring-data':
            self._handle_monitoring_data()
        elif self.path == '/api/today-analysis':
            self._handle_today_analysis()
        elif self.path == '/api/today-analysis-detail':
            self._handle_today_analysis_detail()
        elif self.path == '/api/history':
            self._handle_history()
        elif self.path == '/api/personality-summary':
            self._handle_personality_summary()
        elif self.path.startswith('/api/'):
            self._send_error("未知的接口", 404)
        else:
            # 服务前端静态文件
            self._serve_static()

    def _serve_static(self):
        """服务前端静态文件"""
        frontend_dir = os.path.join(os.path.dirname(base_dir), 'frontend')
        if self.path == '/':
            self.path = '/index.html'
        file_path = os.path.join(frontend_dir, self.path.lstrip('/'))
        # 安全检查：防止路径遍历
        if not os.path.abspath(file_path).startswith(os.path.abspath(frontend_dir)):
            self.send_error(403, "Forbidden")
            return
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            # 根据文件扩展名设置 Content-Type
            if file_path.endswith('.html'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
                self.send_header('Pragma', 'no-cache')
                self.send_header('Expires', '0')
                # 注入时间戳，确保浏览器不使用缓存
                ts = f'<!-- cache-bust: {int(time.time())} -->'.encode()
                content = content.replace(b'</head>', ts + b'</head>', 1)
            elif file_path.endswith('.css'):
                self.send_response(200)
                self.send_header('Content-Type', 'text/css; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            elif file_path.endswith('.js'):
                self.send_response(200)
                self.send_header('Content-Type', 'application/javascript; charset=utf-8')
                self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            elif file_path.endswith('.png'):
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
            elif file_path.endswith('.jpg') or file_path.endswith('.jpeg'):
                self.send_response(200)
                self.send_header('Content-Type', 'image/jpeg')
            elif file_path.endswith('.svg'):
                self.send_response(200)
                self.send_header('Content-Type', 'image/svg+xml')
            elif file_path.endswith('.ico'):
                self.send_response(200)
                self.send_header('Content-Type', 'image/x-icon')
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Content-Length', len(content))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404, "File not found")

    def do_POST(self):
        if self.path == '/api/analyze':
            self._handle_analyze()
        elif self.path == '/api/save-session':
            self._handle_save_session()
        elif self.path == '/api/chat':
            self._handle_chat()
        elif self.path == '/api/start-monitoring':
            self._handle_start_monitoring()
        elif self.path == '/api/stop-monitoring':
            self._handle_stop_monitoring()
        else:
            self._send_error("未知的接口", 404)

    def _handle_generate_data(self):
        """获取今日行为数据"""
        real_data = load_today_timeline_data()
        if real_data:
            logger.info(f"返回真实时间片数据，共 {len(real_data)} 条")
            self._send_json(real_data)
        else:
            logger.info("无真实数据，返回空数组")
            self._send_json([])

    def _handle_save_session(self):
        """已废弃：数据保存已迁移到 /api/stop-monitoring 中自动完成。
        保留此接口仅为兼容旧版前端，不再执行任何写入操作。"""
        self._send_json({"ok": True, "saved": False, "reason": "deprecated, use stop-monitoring"})
        return
        request_data = self._read_body()
        if not isinstance(request_data, dict):
            self._send_error("请求数据格式错误")
            return

        duration = float(request_data.get('duration_minutes', 0) or 0)
        switch_count = int(request_data.get('switch_count', 0) or 0)
        app_count = int(request_data.get('app_count', 0) or 0)
        app_usage_raw = request_data.get('app_usage', {})

        logger.info(f"[save-session] 收到数据: duration={duration}, switch={switch_count}, apps={app_count}, app_usage={app_usage_raw}")

        # 防御性检查：单次 session 不应超过 24 小时
        if duration > 1440:
            logger.warning(f"[save-session] 异常数据: duration={duration}分钟，已忽略")
            duration = 0
        if switch_count > 1000:
            logger.warning(f"[save-session] 异常数据: switch_count={switch_count}，已忽略")
            switch_count = 0

        if duration <= 0 and switch_count <= 0:
            self._send_json({"ok": True, "saved": False, "reason": "no data"})
            return

        today = dt.now().strftime('%Y-%m-%d')
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()

        # 读取当天已有记录
        cursor.execute('SELECT total_duration, switch_count, app_usage FROM analysis_results WHERE date = ?', (today,))
        existing = cursor.fetchone()

        if existing:
            new_duration = (existing[0] or 0) + duration
            new_switch = (existing[1] or 0) + switch_count
            logger.info(f"[save-session] 数据库已有: {existing[0]}分钟, {existing[1]}切换, 本次: {duration}分钟, {switch_count}切换")
            existing_usage = {}
            try:
                existing_usage = json.loads(existing[2]) if existing[2] else {}
            except:
                pass
        else:
            new_duration = duration
            new_switch = switch_count
            existing_usage = {}

        # 合并 app_usage（用 JSON 在 SQL 层面合并，避免竞争条件）
        if isinstance(app_usage_raw, dict):
            for app, dur in app_usage_raw.items():
                existing_usage[app] = (existing_usage.get(app, 0) or 0) + float(dur or 0)

        merged_usage_json = json.dumps(existing_usage, ensure_ascii=False)

        # 重新计算分析指标
        merged_analysis = analyze_behavior([
            {"app": app, "duration_minutes": dur, "duration_unit": "minutes",
             "category": "idle" if app == "Idle" else "other",
             "activity": "idle" if app == "Idle" else "active"}
            for app, dur in existing_usage.items()
            if float(dur or 0) > 0
        ], switch_count_override=new_switch)

        # 使用 UPSERT（INSERT ... ON CONFLICT UPDATE）避免竞争条件
        cursor.execute('''
            INSERT INTO analysis_results
            (date, total_duration, switch_count, avg_duration, max_continuous,
             slacking_score, status, summary, time_distribution, app_usage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                total_duration = total_duration + excluded.total_duration,
                switch_count = switch_count + excluded.switch_count,
                avg_duration = excluded.avg_duration,
                max_continuous = excluded.max_continuous,
                slacking_score = excluded.slacking_score,
                status = excluded.status,
                summary = excluded.summary,
                time_distribution = excluded.time_distribution,
                app_usage = excluded.app_usage
        ''', (
            today,
            duration,
            switch_count,
            merged_analysis.get('avg_duration'),
            merged_analysis.get('max_continuous'),
            merged_analysis.get('slacking_score'),
            merged_analysis.get('status'),
            merged_analysis.get('summary'),
            json.dumps(merged_analysis.get('time_distribution', {})),
            json.dumps(existing_usage)
        ))

        conn.commit()
        conn.close()
        # 计算 app_count（去重后的应用数量，排除 Idle）
        new_app_count = len([k for k, v in existing_usage.items() if v and v > 0 and k != 'Idle'])
        logger.info(f"[save-session] 已累加: +{duration}分钟, +{switch_count}切换 → 当天累计: {new_duration}分钟")
        self._send_json({"ok": True, "saved": True, "total_duration": new_duration, "switch_count": new_switch, "app_count": new_app_count})

    def _handle_today_analysis(self):
        """返回当天的累计分析数据"""
        today = dt.now().strftime('%Y-%m-%d')
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT total_duration, switch_count, app_usage FROM analysis_results WHERE date = ?', (today,))
        row = cursor.fetchone()
        conn.close()

        if row:
            app_usage = {}
            try:
                app_usage = json.loads(row[2]) if row[2] else {}
            except:
                pass
            self._send_json({
                "total_duration": row[0] or 0,
                "switch_count": row[1] or 0,
                "app_count": len([k for k, v in app_usage.items() if v and v > 0])
            })
        else:
            self._send_json({"total_duration": 0, "switch_count": 0, "app_count": 0})

    def _handle_today_analysis_detail(self):
        """返回当天的完整分析数据（包括 app_usage、slacking_score 等）"""
        today = dt.now().strftime('%Y-%m-%d')
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT total_duration, switch_count, slacking_score, status, summary, time_distribution, app_usage FROM analysis_results WHERE date = ?', (today,))
        row = cursor.fetchone()
        conn.close()

        if row:
            app_usage = {}
            time_distribution = {}
            try:
                app_usage = json.loads(row[6]) if row[6] else {}
            except:
                pass
            try:
                time_distribution = json.loads(row[5]) if row[5] else {}
            except:
                pass
            self._send_json({
                "total_duration": row[0] or 0,
                "switch_count": row[1] or 0,
                "slacking_score": row[2] or 0,
                "status": row[3] or '',
                "summary": row[4] or '',
                "time_distribution": time_distribution,
                "app_usage": app_usage
            })
        else:
            self._send_json({"total_duration": 0, "switch_count": 0, "slacking_score": 0, "status": "", "summary": "", "time_distribution": {}, "app_usage": {}})

    def _handle_history(self):
        """获取历史记录"""
        days = 7
        result = get_history_data(days)
        self._send_json(result)

    def _handle_personality_summary(self):
        """获取人格总结 - 累加所有天数据"""
        result = get_personality_summary()
        self._send_json(result)

    def _handle_analyze(self):
        """分析行为数据（只分析，永远不保存到数据库。保存请用 /api/save-session）"""
        # 读取请求中的数据
        request_data = self._read_body()
        all_data = []
        explicit_switch_count = None

        # 新格式：{ app_usage_data: [...], switch_count: N, app_count: N }
        if isinstance(request_data, dict) and 'app_usage_data' in request_data:
            app_usage_payload = request_data['app_usage_data']
            explicit_switch_count = request_data.get('switch_count')
            if isinstance(app_usage_payload, list):
                for item in app_usage_payload:
                    if isinstance(item, dict):
                        item.setdefault('app', 'Unknown')
                        item.setdefault('category', 'other')
                        item.setdefault('activity', 'active')
                        all_data.append(item)

        # 旧格式：直接是列表
        elif isinstance(request_data, list) and request_data:
            for item in request_data:
                if isinstance(item, dict):
                    item.setdefault('app', 'Unknown')
                    item.setdefault('category', 'other')
                    item.setdefault('activity', 'active')
                    if 'start' not in item and 'end' not in item and 'duration' not in item and 'duration_minutes' not in item:
                        item.setdefault('start', '00:00')
                        item.setdefault('end', '00:00')
                    all_data.append(item)

        # 分析数据（不保存）
        result = analyze_behavior(all_data, switch_count_override=explicit_switch_count)
        result['behavior_data'] = all_data
        self._send_json(result)

    def _handle_chat(self):
        """AI 聊天接口"""
        data = self._read_body()
        if not data or not isinstance(data, dict):
            self._send_error("请求数据格式错误")
            return
        message = data.get('message', '').strip()
        page_context = data.get('context')
        if not message:
            self._send_error("消息不能为空")
            return

        logger.info(f"收到聊天请求: {message[:30]}...")

        # 尝试使用大语言模型
        if OPENAI_API_KEY:
            llm_result = call_llm_api(message, page_context)
            if llm_result["ok"]:
                try:
                    response = {
                        'response': llm_result['response'],
                        'source': 'llm',
                        'model': llm_result['model']
                    }
                    save_chat_to_db(message, llm_result['response'], 'llm', llm_result.get('model', ''))
                    self._send_json(response)
                except BrokenPipeError:
                    logger.warning("客户端已断开连接，无法发送 AI 回复")
                return

            fallback_response = use_local_response_system(message)
            user_message = (
                f"外部 AI 暂时不可用：{llm_result['error']}。\n"
                f"当前已切换为本地兜底回复：{fallback_response}"
            )
            try:
                response = {
                    'response': user_message,
                    'source': 'local_fallback',
                    'llm_error': llm_result['error']
                }
                save_chat_to_db(message, user_message, 'local_fallback', '')
                self._send_json(response)
            except BrokenPipeError:
                logger.warning("客户端已断开连接，无法发送本地回复")
        else:
            logger.info("未配置 API Key，使用本地回复系统")
            ai_response = use_local_response_system(message)
            save_chat_to_db(message, ai_response, 'local_only', '')
            self._send_json({
                'response': ai_response,
                'source': 'local_only',
                'llm_error': '未配置 OPENAI_API_KEY'
            })

    # ─── 实时监控状态 ───
    monitoring_active = False
    monitoring_thread = None
    monitoring_data = {
        "current_app": "未检测",
        "switch_count": 0,
        "app_count": 0,
        "most_used": "无",
        "total_time": 0,
        "logs": [],
        "app_usage": {},
        "status": "stopped",
        "error": None,
        "is_not_working": False
    }

    def _handle_monitoring_data(self):
        """获取实时监测数据"""
        # 如果监控未运行，尝试检测一次当前窗口
        if not FlowMirrorHandler.monitoring_active:
            app_name = self._get_active_window()
            if app_name and app_name != "Unknown":
                FlowMirrorHandler.monitoring_data["current_app"] = app_name
                FlowMirrorHandler.monitoring_data["error"] = None
            else:
                FlowMirrorHandler.monitoring_data["error"] = self._get_detection_error()

        response_data = dict(FlowMirrorHandler.monitoring_data)
        response_data["monitoring_active"] = FlowMirrorHandler.monitoring_active
        self._send_json(response_data)

    def _get_active_window(self):
        """获取当前活跃窗口名称"""
        system = platform.system()
        FlowMirrorHandler.last_detection_error = None
        try:
            if system == "Windows":
                try:
                    import win32gui
                    hwnd = win32gui.GetForegroundWindow()
                    return win32gui.GetWindowText(hwnd) or "Unknown"
                except ImportError:
                    FlowMirrorHandler.last_detection_error = "需要安装 pywin32：pip install pywin32"
                    return None  # pywin32 未安装
            elif system == "Darwin":  # macOS
                # 方法1：osascript（零 Python 依赖，但可能需要系统权限）
                try:
                    result = subprocess.run(
                        ['osascript', '-e',
                         'tell application "System Events" to get name of first application process whose frontmost is true'],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        return result.stdout.strip()

                    stderr = (result.stderr or "").strip()
                    if stderr:
                        if not FlowMirrorHandler.macos_warning_flags["applescript_permission_logged"]:
                            logger.warning(f"osascript 失败: {stderr}")
                            FlowMirrorHandler.macos_warning_flags["applescript_permission_logged"] = True
                        if "not authorized" in stderr.lower() or "(-1743)" in stderr:
                            FlowMirrorHandler.last_detection_error = (
                                "macOS 阻止了窗口检测。请在“系统设置 > 隐私与安全性 > 自动化”中允许当前 Python/终端控制“系统事件”，"
                                "必要时也检查“辅助功能”权限。"
                            )
                        else:
                            FlowMirrorHandler.last_detection_error = f"AppleScript 检测失败：{stderr}"
                except FileNotFoundError:
                    FlowMirrorHandler.last_detection_error = "系统缺少 osascript，无法在 macOS 上检测前台窗口"
                except subprocess.TimeoutExpired:
                    FlowMirrorHandler.last_detection_error = "AppleScript 检测超时，请稍后重试"

                # 方法2：CGWindowListCopyWindowInfo（无需辅助功能权限，但依赖 pyobjc）
                try:
                    from Quartz import CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly, kCGNullWindowID
                    from Quartz import kCGWindowOwnerName
                    window_list = CGWindowListCopyWindowInfo(
                        kCGWindowListOptionOnScreenOnly, kCGNullWindowID
                    )
                    if window_list:
                        # 从后往前遍历，跳过系统窗口
                        for window in reversed(window_list):
                            owner = window.get(kCGWindowOwnerName, '')
                            if owner and owner not in ('Window Server', 'Dock', 'SystemUIServer', 'loginwindow', 'Notification Center', 'ControlCenter'):
                                return owner
                except ImportError:
                    if not FlowMirrorHandler.macos_warning_flags["quartz_missing_logged"]:
                        logger.warning("Quartz 不可用，可选安装：pip install pyobjc-framework-Quartz")
                        FlowMirrorHandler.macos_warning_flags["quartz_missing_logged"] = True
                except Exception as e:
                    logger.warning(f"CGWindowListCopyWindowInfo 失败: {e}")

                # 方法3：pyobjc NSWorkspace
                try:
                    from AppKit import NSWorkspace
                    workspace = NSWorkspace.sharedWorkspace()
                    active_app = workspace.activeApplication()
                    name = active_app.get('NSApplicationName', '')
                    if name:
                        return name
                except ImportError:
                    if not FlowMirrorHandler.macos_warning_flags["appkit_missing_logged"]:
                        logger.warning("AppKit 不可用，可选安装：pip install pyobjc-framework-Cocoa")
                        FlowMirrorHandler.macos_warning_flags["appkit_missing_logged"] = True
                except Exception as e:
                    logger.warning(f"pyobjc NSWorkspace 失败: {e}")

                return None  # 所有方法都不可用
            elif system == "Linux":
                # 尝试 xdotool
                try:
                    result = subprocess.run(
                        ['xdotool', 'getwindowfocus', 'getwindowname'],
                        capture_output=True, text=True, timeout=3
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        return result.stdout.strip()
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
                # 尝试 wmctrl
                try:
                    result = subprocess.run(
                        ['wmctrl', '-l'],
                        capture_output=True, text=True, timeout=3
                    )
                    if result.returncode == 0 and result.stdout.strip():
                        lines = result.stdout.strip().split('\n')
                        if lines:
                            # 取最后一行（活跃窗口通常在最后）
                            parts = lines[-1].split(None, 3)
                            if len(parts) >= 4:
                                return parts[3]
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
                # 尝试 xprop
                try:
                    result = subprocess.run(
                        ['xprop', '-root', '_NET_ACTIVE_WINDOW'],
                        capture_output=True, text=True, timeout=3
                    )
                    if result.returncode == 0:
                        return "Active Window (xprop)"
                except (FileNotFoundError, subprocess.TimeoutExpired):
                    pass
                FlowMirrorHandler.last_detection_error = "需要安装 xdotool：sudo apt install xdotool"
                return None  # 所有工具都不可用
        except Exception as e:
            logger.error(f"获取活跃窗口失败: {e}")
            FlowMirrorHandler.last_detection_error = f"窗口检测异常：{e}"
            return None

    def _get_detection_error(self):
        """获取检测失败的原因"""
        if FlowMirrorHandler.last_detection_error:
            return FlowMirrorHandler.last_detection_error

        system = platform.system()
        if system == "Windows":
            return "需要安装 pywin32：pip install pywin32"
        elif system == "Darwin":
            return (
                "未能检测到 macOS 前台窗口。优先检查“系统设置 > 隐私与安全性 > 自动化 / 辅助功能”权限；"
                "如仍失败，可安装 pyobjc-framework-Quartz 或 pyobjc-framework-Cocoa 作为备用检测方式。"
            )
        elif system == "Linux":
            return "需要安装 xdotool：sudo apt install xdotool"
        return "不支持当前操作系统"

    def _get_user_activity(self):
        """检测用户活动（鼠标和键盘）"""
        system = platform.system()
        try:
            if system == "Windows":
                try:
                    import win32api, win32con
                    # 获取最后输入时间
                    last_input = win32api.GetLastInputInfo()
                    current = win32api.GetTickCount()
                    idle_time = (current - last_input) / 1000  # 转换为秒
                    return idle_time
                except ImportError:
                    return -1  # 无法检测
            elif system == "Darwin":  # macOS
                try:
                    # 使用 ioreg 命令获取空闲时间
                    result = subprocess.run(
                        ['ioreg', '-c', 'IOHIDSystem'],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        for line in result.stdout.split('\n'):
                            if 'HIDIdleTime' in line:
                                idle_ns = int(line.split('=')[1].strip())
                                idle_time = idle_ns / 1e9  # 转换为秒
                                return idle_time
                except Exception:
                    pass
                return -1  # 无法检测
            elif system == "Linux":
                try:
                    # 读取 X 服务器的空闲时间
                    result = subprocess.run(
                        ['xprintidle'],
                        capture_output=True, text=True, timeout=5
                    )
                    if result.returncode == 0:
                        idle_ms = int(result.stdout.strip())
                        idle_time = idle_ms / 1000  # 转换为秒
                        return idle_time
                except (FileNotFoundError, ValueError):
                    pass
                return -1  # 无法检测
        except Exception as e:
            logger.error(f"检测用户活动失败: {e}")
        return -1  # 无法检测

    def _handle_start_monitoring(self):
        """开始实时监控"""
        # 测试能否检测窗口（最多重试3次）
        test_app = None
        for attempt in range(3):
            test_app = self._get_active_window()
            if test_app and test_app != "Unknown":
                logger.info(f"窗口检测成功（第{attempt+1}次尝试）: {test_app}")
                break
            logger.warning(f"窗口检测失败（第{attempt+1}次尝试）")
            time.sleep(1)

        if test_app is None or test_app == "Unknown":
            error_msg = self._get_detection_error()
            logger.error(f"无法检测活跃窗口: {error_msg}")
            self._send_json({"status": "error", "message": f"无法检测活跃窗口：{error_msg}"})
            return

        # 无论监测是否已经在运行，都重置监控数据
        FlowMirrorHandler.monitoring_active = True
        FlowMirrorHandler.monitoring_data = {
            "current_app": test_app,
            "switch_count": 0,
            "app_count": 1,
            "most_used": test_app,
            "total_time": 0,
            "logs": [],
            "app_usage": {test_app: 0},
            "status": "running",
            "error": None,
            "is_not_working": False
        }

        # 如果监控线程未运行，则启动
        if not FlowMirrorHandler.monitoring_thread or not FlowMirrorHandler.monitoring_thread.is_alive():
            # 启动后台监控线程
            FlowMirrorHandler.monitoring_thread = threading.Thread(
                target=self._monitoring_loop, daemon=True
            )
            FlowMirrorHandler.monitoring_thread.start()
            logger.info(f"实时监控已启动，当前窗口: {test_app}")
            self._send_json({"status": "started", "message": "监控已启动", "current_app": test_app})
        else:
            logger.info(f"实时监控已在运行，重置数据，当前窗口: {test_app}")
            self._send_json({"status": "started", "message": "监控已重置", "current_app": test_app})

    def _save_session_to_db(self, duration, switch_count, app_usage_raw):
        """将 session 数据累加到当天数据库记录（内部方法）"""
        today = dt.now().strftime('%Y-%m-%d')
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()

        cursor.execute('SELECT total_duration, switch_count, app_usage FROM analysis_results WHERE date = ?', (today,))
        existing = cursor.fetchone()

        existing_dur = 0
        existing_sw = 0
        if existing:
            existing_dur = existing[0] or 0
            new_duration = existing_dur + duration
            new_switch = (existing[1] or 0) + switch_count
            existing_usage = {}
            try:
                existing_usage = json.loads(existing[2]) if existing[2] else {}
            except:
                pass
            logger.info(f"[save-to-db] 数据库已有: {existing_dur}分钟, {existing[1]}切换")
        else:
            new_duration = duration
            new_switch = switch_count
            existing_usage = {}
            logger.info(f"[save-to-db] 数据库无记录，新建")

        if isinstance(app_usage_raw, dict):
            for app, dur in app_usage_raw.items():
                existing_usage[app] = (existing_usage.get(app, 0) or 0) + float(dur or 0)

        merged_analysis = analyze_behavior([
            {"app": app, "duration_minutes": dur, "duration_unit": "minutes",
             "category": "idle" if app == "Idle" else "other",
             "activity": "idle" if app == "Idle" else "active"}
            for app, dur in existing_usage.items()
            if float(dur or 0) > 0
        ], switch_count_override=new_switch)

        new_app_count = len([k for k, v in existing_usage.items() if v and v > 0 and k != 'Idle'])

        # 使用 UPSERT，total_duration 和 switch_count 在 SQL 层面原子累加
        cursor.execute('''
            INSERT INTO analysis_results
            (date, total_duration, switch_count, avg_duration, max_continuous,
             slacking_score, status, summary, time_distribution, app_usage)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                total_duration = total_duration + excluded.total_duration,
                switch_count = switch_count + excluded.switch_count,
                avg_duration = excluded.avg_duration,
                max_continuous = excluded.max_continuous,
                slacking_score = excluded.slacking_score,
                status = excluded.status,
                summary = excluded.summary,
                time_distribution = excluded.time_distribution,
                app_usage = excluded.app_usage
        ''', (
            today, duration, switch_count,
            merged_analysis.get('avg_duration'), merged_analysis.get('max_continuous'),
            merged_analysis.get('slacking_score'), merged_analysis.get('status'),
            merged_analysis.get('summary'),
            json.dumps(merged_analysis.get('time_distribution', {})),
            json.dumps(existing_usage)
        ))
        conn.commit()
        conn.close()
        logger.info(f"[save-to-db] 累加: +{duration}分钟, +{switch_count}切换 → 当天累计: {new_duration}分钟")
        return new_duration, new_switch, new_app_count

    def _handle_stop_monitoring(self):
        """停止实时监控，并自动保存本次 session 数据到数据库"""
        if not FlowMirrorHandler.monitoring_active:
            self._send_json({"status": "not_running", "message": "监控未在运行"})
            return

        FlowMirrorHandler.monitoring_active = False

        # 在清零之前，保存本次 session 数据到数据库
        stopped_snapshot = dict(FlowMirrorHandler.monitoring_data)
        app_usage = stopped_snapshot.get("app_usage", {})
        switch_count = stopped_snapshot.get("switch_count", 0)
        total_minutes = sum(float(v or 0) for v in app_usage.values()) if app_usage else 0

        if total_minutes > 0 or switch_count > 0:
            try:
                result = self._save_session_to_db(total_minutes, switch_count, app_usage)
                logger.info(f"[stop-monitoring] 自动保存: {total_minutes}分钟, {switch_count}切换 → 累计: {result[0]}分钟")
            except Exception as e:
                logger.error(f"[stop-monitoring] 自动保存失败: {e}")
                result = None

        # 读取保存后的数据库累计值，返回给前端
        today = dt.now().strftime('%Y-%m-%d')
        conn = sqlite3.connect(DB_PATH, timeout=10)
        cursor = conn.cursor()
        cursor.execute('SELECT total_duration, switch_count, app_usage FROM analysis_results WHERE date = ?', (today,))
        db_row = cursor.fetchone()
        conn.close()

        db_total = db_row[0] if db_row else 0
        db_switch = db_row[1] if db_row else 0
        db_apps = {}
        try:
            db_apps = json.loads(db_row[2]) if db_row and db_row[2] else {}
        except:
            pass
        db_app_count = len([k for k, v in db_apps.items() if v and v > 0 and k != 'Idle'])

        FlowMirrorHandler.monitoring_data["status"] = "stopped"
        logger.info("实时监控已停止")
        self._send_json({
            "status": "stopped",
            "message": "监控已停止",
            "data": stopped_snapshot,
            "cumulative": {
                "total_duration": db_total,
                "switch_count": db_switch,
                "app_count": db_app_count
            }
        })
        FlowMirrorHandler.monitoring_data.update({
            "switch_count": 0,
            "app_count": 0,
            "most_used": "无",
            "total_time": 0,
            "logs": [],
            "app_usage": {},
            "status": "stopped",
            "is_not_working": False
        })

    def _monitoring_loop(self):
        """后台监控循环"""
        prev_app = FlowMirrorHandler.monitoring_data["current_app"]
        last_active_app = prev_app if prev_app not in ("Idle", "Unknown") else None
        start_time = time.time()
        last_save_time = time.time()
        idle_threshold = 60  # 空闲阈值（秒）
        not_working_threshold = 300  # 长时间空闲阈值（秒）

        while FlowMirrorHandler.monitoring_active:
            try:
                # 检测用户活动
                idle_time = self._get_user_activity()
                is_idle = idle_time >= idle_threshold

                # 检测活跃窗口
                detected = self._get_active_window()
                if detected and detected != "Unknown":
                    current_app = detected
                else:
                    # 检测失败时保留上一次的应用名
                    current_app = prev_app

                # 如果用户长时间未操作，标记为不活跃
                if is_idle:
                    current_app = "Idle"

                now = time.time()
                elapsed = now - start_time

                # 应用切换检测：Idle 只是“无人操作”的状态，不算真正的软件切换。
                # 只有从一个非 Idle 应用切到另一个非 Idle 应用时才计入切换次数。
                if current_app not in ("Idle", "Unknown"):
                    if last_active_app and current_app != last_active_app:
                        FlowMirrorHandler.monitoring_data["switch_count"] += 1
                        log_entry = {
                            "time": dt.now().strftime("%H:%M:%S"),
                            "from": last_active_app,
                            "to": current_app
                        }
                        FlowMirrorHandler.monitoring_data["logs"].append(log_entry)
                        if len(FlowMirrorHandler.monitoring_data["logs"]) > 50:
                            FlowMirrorHandler.monitoring_data["logs"] = \
                                FlowMirrorHandler.monitoring_data["logs"][-50:]
                        logger.info(f"应用切换: {last_active_app} → {current_app}")
                    last_active_app = current_app

                # 更新统计
                FlowMirrorHandler.monitoring_data["current_app"] = current_app
                FlowMirrorHandler.monitoring_data["total_time"] = round(elapsed, 1)
                FlowMirrorHandler.monitoring_data["is_idle"] = is_idle
                FlowMirrorHandler.monitoring_data["is_not_working"] = idle_time >= not_working_threshold if idle_time >= 0 else False
                if is_idle:
                    FlowMirrorHandler.monitoring_data["idle_time"] = round(idle_time, 1)

                # 更新应用使用统计（每2秒检测一次，加2秒 = 2/60分钟）
                if current_app != "Unknown":
                    usage = FlowMirrorHandler.monitoring_data["app_usage"]
                    usage[current_app] = usage.get(current_app, 0) + (2/60)
                    FlowMirrorHandler.monitoring_data["app_count"] = len(usage)
                    if usage:
                        FlowMirrorHandler.monitoring_data["most_used"] = \
                            max(usage, key=usage.get)

                # 定期保存数据（每隔30秒）
                if now - last_save_time >= 30:
                    self._save_monitoring_session()
                    last_save_time = now

                prev_app = current_app
                time.sleep(2)

            except Exception as e:
                logger.error(f"监控循环错误: {e}")
                time.sleep(5)

    def log_message(self, format, *args):
        logger.info(f"{self.client_address[0]} - {format % args}")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """多线程 HTTP 服务器"""
    daemon_threads = True
    allow_reuse_address = True  # 允许端口复用，避免 "Address already in use" 错误


if __name__ == '__main__':
    HOST = '0.0.0.0'
    PORT = 5001
    server = ThreadedHTTPServer((HOST, PORT), FlowMirrorHandler)
    logger.info(f"FlowMirror 后端已启动: http://{HOST}:{PORT}")
    logger.info("API 接口:")
    logger.info("  GET  /api/generate-data  - 生成行为数据")
    logger.info("  POST /api/analyze         - 分析行为数据")
    logger.info("  POST /api/chat            - AI 聊天")
    logger.info("  GET  /api/monitoring-data - 实时监测数据")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("服务器已停止")
        server.server_close()
