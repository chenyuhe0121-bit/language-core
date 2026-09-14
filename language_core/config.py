"""全局配置。零第三方依赖。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = ROOT / "language_core" / "assets"
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "language_core.db"

# 默认角色与场景
DEFAULT_CHARACTER = "elise"
DEFAULT_SCENE = "cafe"

# ---- 模型 ----
LLM_API_KEY = os.environ.get("LANGUAGE_CORE_LLM_API_KEY", "").strip()
LLM_BASE_URL = os.environ.get("LANGUAGE_CORE_LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
LLM_MODEL = os.environ.get("LANGUAGE_CORE_LLM_MODEL", "deepseek-chat")
LLM_TIMEOUT = int(os.environ.get("LANGUAGE_CORE_LLM_TIMEOUT", "60"))

# 按场景/阶段的参数映射（技术栈 D6：参数不全局一套）
PARAMS_DEFAULT = {"temperature": 0.9, "top_p": 1.0, "frequency_penalty": 0.3, "presence_penalty": 0.3, "max_tokens": 800}
PARAMS_BY_INTENT = {
    "comfort":   {"temperature": 0.7},
    "reminisce": {"temperature": 0.8},
    "tease":     {"temperature": 1.0},
    "playful":   {"temperature": 1.0},
}
PARAMS_BY_STAGE = {
    "first_meet":        {"temperature": 0.8},
    "getting_familiar":  {"temperature": 0.9},
    "comfortable":       {"temperature": 0.9},
    "close":             {"temperature": 1.0},
    "deep_trust":        {"temperature": 1.0},
}

# ---- 上下文预算（单位：字符。中文约等于 1 token/字）----
BUDGET = {
    "fixed": 1400,      # 角色卡 + 风格锚点 + 边界策略
    "state": 400,       # 关系状态
    "scene": 500,       # 场景卡
    "memory": 600,      # 召回记忆
    "summary": 300,     # 滚动摘要
    "recent": 1200,     # 最近对话
}
# 超预算时的降级顺序：先砍靠后的
DEGRADE_ORDER = ["recent", "memory", "summary", "scene", "state"]

# ---- 记忆 ----
RECENT_TURNS = 8          # 工作记忆保留的对话轮数
SUMMARY_EVERY = 10        # 每多少条消息触发一次摘要压缩
RECALL_LIMIT = 5          # 单次召回条数上限
RECALL_MIN_SCORE = 0.18   # 召回最低分，宁可不召回不污染上下文
MEMORY_MAX_CONFIDENCE = 1.0
MEMORY_MIN_CONFIDENCE = 0.5

# ---- 服务器 ----
HOST = os.environ.get("LANGUAGE_CORE_HOST", "127.0.0.1")
PORT = int(os.environ.get("LANGUAGE_CORE_PORT", "8420"))

MODE = "live" if LLM_API_KEY else "mock"
