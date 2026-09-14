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
PARAMS_DEFAULT = {"temperature": 0.9, "top_p": 1.0, "frequency_penalty": 0.15, "presence_penalty": 0.0, "max_tokens": 1200}
MAX_CONTEXT_CHARS = 48000
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
    "recent": 24000,    # 完整旧消息移除，角色卡不裁剪
}
# 超预算时的降级顺序：先砍靠后的
DEGRADE_ORDER = ["recent", "memory", "summary", "scene", "state"]

# ---- 记忆 ----
RECENT_TURNS = 60         # 消息数，约 30 轮
SUMMARY_EVERY = 10        # 每多少条消息触发一次摘要压缩
RECALL_LIMIT = 5          # 单次召回条数上限
RECALL_MIN_SCORE = 0.18   # 召回最低分，宁可不召回不污染上下文
MEMORY_MAX_CONFIDENCE = 1.0
MEMORY_MIN_CONFIDENCE = 0.5

# ---- 服务器 ----
HOST = os.environ.get("LANGUAGE_CORE_HOST", "127.0.0.1")
PORT = int(os.environ.get("LANGUAGE_CORE_PORT", "8420"))

# ---- 主动开口 ----
# 视频按分钟计费，冷场就是让用户白花钱。所以对话卡住时由她挑起话头。
# 但连续追是白烧用户的钱，所以有冷却，以及「一个死胡同只开口一次」两道闸。
PROACTIVE_ENABLED = os.environ.get("LANGUAGE_CORE_PROACTIVE", "1") not in ("0", "false", "")
PROACTIVE_IDLE_SECONDS = float(os.environ.get("LANGUAGE_CORE_PROACTIVE_IDLE", "10"))
PROACTIVE_COOLDOWN_SECONDS = float(os.environ.get("LANGUAGE_CORE_PROACTIVE_COOLDOWN", "60"))
PROACTIVE_POLL_SECONDS = float(os.environ.get("LANGUAGE_CORE_PROACTIVE_POLL", "3"))
# 用户至少回几句才解锁下一次主动开口。
# 只回一个「嗯」不算——那样她会陪着用户一起敷衍，白烧他的钱。
PROACTIVE_MIN_USER_TURNS = int(os.environ.get("LANGUAGE_CORE_PROACTIVE_MIN_TURNS", "2"))

MODE = "live" if LLM_API_KEY else "mock"
