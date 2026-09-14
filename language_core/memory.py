"""记忆系统。

四层结构（技术栈 D4：所有 SQL 收敛在本模块内，业务代码不写 SQL）：
  working   工作记忆 —— 最近 N 轮原文，全量进上下文
  summary   短期记忆 —— 每 10 条滚动压缩一次
  long_term 长期记忆 —— 永久事实与经历，按相关度召回
  open_loop 未闭合话题 —— 没结的事，按时间主动提起

分区维度只有一个：character_id。
场景是记忆的标签，不是隔离墙——回到旧场景不能失忆，也不能退回生疏。
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from . import config

# ---------------------------------------------------------------- 工具

_TZ = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def _days_since(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso)
    except Exception:
        return 999.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ)
    return max(0.0, (datetime.now(_TZ) - dt).total_seconds() / 86400.0)


# 关键词切分：中文按字，英文按词。够用且零依赖。
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def _overlap(query: str, target: str) -> float:
    """字符级重合度，0 到 1。用于第一期的关键词召回。"""
    q = set(_tokens(query))
    if not q:
        return 0.0
    t = set(_tokens(target))
    if not t:
        return 0.0
    return len(q & t) / len(q)


# ---------------------------------------------------------------- 数据类

MEMORY_TYPES = ("fact", "preference", "event", "relation")


@dataclass
class Memory:
    id: str
    character_id: str
    user_id: str
    type: str
    content: str
    confidence: float = 0.8
    source_scene: str | None = None
    source_turn: str | None = None
    created_at: str = ""
    expires_at: str | None = None
    visibility: str = "private"        # 第一期全填 private，为跨角色预留
    score: float = 0.0                 # 仅召回时填充

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "type": self.type,
            "content": self.content,
            "confidence": round(self.confidence, 2),
            "source_scene": self.source_scene,
            "created_at": self.created_at,
            "visibility": self.visibility,
        }
        if self.score:
            d["score"] = round(self.score, 3)
        return d


@dataclass
class OpenLoop:
    id: str
    character_id: str
    user_id: str
    topic: str
    expected_at: str | None = None
    status: str = "pending"            # pending | ask_now | asked | expired
    created_scene: str | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "topic": self.topic,
            "expected_at": self.expected_at,
            "status": self.status,
            "created_scene": self.created_scene,
            "created_at": self.created_at,
        }


@dataclass
class WorkingTurn:
    role: str
    content: str
    scene_id: str | None = None
    created_at: str = ""
    spoken: bool = True
    seg_count: int = 1
    context_content: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"role": self.role, "content": self.content,
                "scene_id": self.scene_id, "created_at": self.created_at,
                "spoken": self.spoken, "seg_count": self.seg_count}


@dataclass
class RecallResult:
    profile: dict[str, str] = field(default_factory=dict)
    memories: list[Memory] = field(default_factory=list)
    open_loops: list[OpenLoop] = field(default_factory=list)
    summary: str = ""
    recent: list[WorkingTurn] = field(default_factory=list)


# ---------------------------------------------------------------- 存储

_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    user_id      TEXT NOT NULL,
    character_id TEXT NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, character_id, key)
);

CREATE TABLE IF NOT EXISTS message (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    character_id TEXT NOT NULL,
    scene_id     TEXT,
    role         TEXT NOT NULL,
    content      TEXT NOT NULL,
    spoken       INTEGER NOT NULL DEFAULT 1,
    seg_count    INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_message_lookup ON message(user_id, character_id, id);

CREATE TABLE IF NOT EXISTS summary (
    user_id      TEXT NOT NULL,
    character_id TEXT NOT NULL,
    content      TEXT NOT NULL,
    covers_upto  TEXT,
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (user_id, character_id)
);

CREATE TABLE IF NOT EXISTS memory (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    character_id TEXT NOT NULL,
    type         TEXT NOT NULL,
    content      TEXT NOT NULL,
    confidence   REAL NOT NULL,
    source_scene TEXT,
    source_turn  TEXT,
    created_at   TEXT NOT NULL,
    expires_at   TEXT,
    visibility   TEXT NOT NULL DEFAULT 'private'
);
CREATE INDEX IF NOT EXISTS idx_memory_scope ON memory(user_id, character_id, visibility);

CREATE TABLE IF NOT EXISTS open_loop (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL,
    character_id  TEXT NOT NULL,
    topic         TEXT NOT NULL,
    expected_at   TEXT,
    status        TEXT NOT NULL,
    created_scene TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_loop_scope ON open_loop(user_id, character_id, status);
"""


class MemoryStore:
    """记忆存储。单写入线程串行化，避免 SQLite 并发写冲突。"""

    def __init__(self, db_path: str | None = None):
        self.db_path = str(db_path or config.DB_PATH)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """给已存在的旧库补上新增的列。

        上线过的库不能重建，用户数据不能丢。
        每加一列就在这里登记，启动时自动补齐。
        """
        wanted = {
            "message": {
                "spoken": "INTEGER NOT NULL DEFAULT 1",
                "seg_count": "INTEGER NOT NULL DEFAULT 1",
                "context_content": "TEXT NOT NULL DEFAULT ''",
            },
        }
        for table, columns in wanted.items():
            try:
                existing = {
                    r["name"] for r in
                    self._conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
            except sqlite3.Error:
                continue
            for name, decl in columns.items():
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    # ---- 用户画像 ----

    def set_profile(self, user_id: str, character_id: str, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO profile(user_id, character_id, key, value, updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(user_id, character_id, key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (user_id, character_id, key, value, _now()),
            )
            self._conn.commit()

    def get_profile(self, user_id: str, character_id: str) -> dict[str, str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM profile WHERE user_id=? AND character_id=?",
                (user_id, character_id),
            ).fetchall()
        return {r["key"]: r["value"] for r in rows}

    # ---- 工作记忆 ----

    def add_message(self, user_id: str, character_id: str, role: str, content: str,
                    scene_id: str | None = None, spoken: bool = True,
                    seg_count: int = 1, context_content: str = "") -> str:
        mid = f"m_{uuid.uuid4().hex[:12]}"
        with self._lock:
            self._conn.execute(
                "INSERT INTO message(id, user_id, character_id, scene_id, role, content, spoken, seg_count, created_at, context_content) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (mid, user_id, character_id, scene_id, role, content,
                 1 if spoken else 0, int(seg_count), _now(), context_content),
            )
            self._conn.commit()
        return mid

    def last_assistant_was_silent(self, user_id: str, character_id: str) -> bool:
        """上一轮她是不是只给了动作、没有台词。

        这是判断「能不能继续安静」的依据，不是靠模型自觉。
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT spoken FROM message WHERE user_id=? AND character_id=? AND role='assistant' "
                "ORDER BY rowid DESC LIMIT 1",
                (user_id, character_id),
            ).fetchone()
        if row is None:
            return False
        return not bool(row["spoken"])

    def last_assistant_opening(self, user_id: str, character_id: str,
                               limit: int = 3) -> list[str]:
        """最近几轮她说过的话，用于检测开场白重复。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT content FROM message WHERE user_id=? AND character_id=? AND role='assistant' "
                "ORDER BY rowid DESC LIMIT ?",
                (user_id, character_id, limit),
            ).fetchall()
        return [r["content"] for r in rows if r["content"]]

    def recent_turns(self, user_id: str, character_id: str, limit: int) -> list[WorkingTurn]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT role, content, scene_id, spoken, seg_count, created_at, context_content FROM message "
                "WHERE user_id=? AND character_id=? ORDER BY rowid DESC LIMIT ?",
                (user_id, character_id, limit),
            ).fetchall()
        return [WorkingTurn(r["role"], r["content"], r["scene_id"], r["created_at"],
                            bool(r["spoken"]), int(r["seg_count"] or 1), r["context_content"])
                for r in reversed(rows)]

    def message_count(self, user_id: str, character_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM message WHERE user_id=? AND character_id=?",
                (user_id, character_id),
            ).fetchone()
        return int(row["n"]) if row else 0

    # ---- 短期摘要 ----

    def get_summary(self, user_id: str, character_id: str) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT content FROM summary WHERE user_id=? AND character_id=?",
                (user_id, character_id),
            ).fetchone()
        return row["content"] if row else ""

    def set_summary(self, user_id: str, character_id: str, content: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO summary(user_id, character_id, content, updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(user_id, character_id) DO UPDATE SET content=excluded.content, updated_at=excluded.updated_at",
                (user_id, character_id, content, _now()),
            )
            self._conn.commit()

    # ---- 长期记忆 ----

    def add_memory(self, user_id: str, character_id: str, type_: str, content: str,
                   confidence: float = 0.8, source_scene: str | None = None,
                   source_turn: str | None = None, visibility: str = "private") -> Memory | None:
        """写入长期记忆。低置信度直接拒收，防止错误记忆沉淀。"""
        if confidence < config.MEMORY_MIN_CONFIDENCE:
            return None
        content = (content or "").strip()
        if not content:
            return None

        # 去重：同角色下已有高度相似内容则不再写入
        for existing in self.all_memories(user_id, character_id):
            if _overlap(content, existing.content) > 0.85:
                return None

        mem = Memory(
            id=f"mem_{uuid.uuid4().hex[:12]}",
            character_id=character_id,
            user_id=user_id,
            type=type_ if type_ in MEMORY_TYPES else "fact",
            content=content,
            confidence=confidence,
            source_scene=source_scene,
            source_turn=source_turn,
            created_at=_now(),
            visibility=visibility,
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO memory(id, user_id, character_id, type, content, confidence, "
                "source_scene, source_turn, created_at, expires_at, visibility) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (mem.id, mem.user_id, mem.character_id, mem.type, mem.content, mem.confidence,
                 mem.source_scene, mem.source_turn, mem.created_at, mem.expires_at, mem.visibility),
            )
            self._conn.commit()
        return mem

    def all_memories(self, user_id: str, character_id: str) -> list[Memory]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM memory WHERE user_id=? AND character_id=? AND visibility='private' "
                "ORDER BY rowid DESC",
                (user_id, character_id),
            ).fetchall()
        return [self._row_to_memory(r) for r in rows]

    def delete_memory(self, memory_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM memory WHERE id=?", (memory_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def update_memory(self, memory_id: str, content: str) -> bool:
        with self._lock:
            cur = self._conn.execute("UPDATE memory SET content=? WHERE id=?", (content, memory_id))
            self._conn.commit()
        return cur.rowcount > 0

    def recall(self, user_id: str, character_id: str, query: str,
               current_scene: str | None = None, limit: int | None = None) -> list[Memory]:
        """按相关度召回。

        评分 = 关键词重合 + 类型权重 + 时间新鲜度 + 场景匹配
        低于阈值宁可不召回——不相关的记忆混进上下文比没召回更糟。
        """
        limit = limit or config.RECALL_LIMIT
        scored: list[Memory] = []

        for mem in self.all_memories(user_id, character_id):
            kw = _overlap(query, mem.content)
            age = _days_since(mem.created_at)
            freshness = 1.0 / (1.0 + age / 14.0)          # 两周为一个衰减周期
            scene_bonus = 0.15 if (current_scene and mem.source_scene == current_scene) else 0.0
            type_weight = {"relation": 0.12, "preference": 0.08,
                           "event": 0.06, "fact": 0.10}.get(mem.type, 0.05)

            mem.score = (
                kw * 0.55
                + freshness * 0.20
                + type_weight
                + scene_bonus
                + mem.confidence * 0.10
            )
            if kw > 0 or scene_bonus > 0:
                scored.append(mem)

        scored.sort(key=lambda m: m.score, reverse=True)
        return [m for m in scored if m.score >= config.RECALL_MIN_SCORE][:limit]

    # ---- 未闭合话题 ----

    def add_open_loop(self, user_id: str, character_id: str, topic: str,
                      expected_at: str | None = None, scene: str | None = None) -> OpenLoop | None:
        topic = (topic or "").strip()
        if not topic:
            return None
        for loop in self.open_loops(user_id, character_id, statuses=("pending", "ask_now")):
            if _overlap(topic, loop.topic) > 0.6:
                return None

        loop = OpenLoop(
            id=f"loop_{uuid.uuid4().hex[:12]}",
            character_id=character_id,
            user_id=user_id,
            topic=topic,
            expected_at=expected_at,
            status="pending",
            created_scene=scene,
            created_at=_now(),
        )
        with self._lock:
            self._conn.execute(
                "INSERT INTO open_loop(id, user_id, character_id, topic, expected_at, status, created_scene, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (loop.id, loop.user_id, loop.character_id, loop.topic, loop.expected_at,
                 loop.status, loop.created_scene, loop.created_at),
            )
            self._conn.commit()
        return loop

    def open_loops(self, user_id: str, character_id: str,
                   statuses: tuple[str, ...] | None = None) -> list[OpenLoop]:
        statuses = statuses or ("pending", "ask_now")
        marks = ",".join("?" for _ in statuses)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM open_loop WHERE user_id=? AND character_id=? AND status IN ({marks}) "
                f"ORDER BY rowid DESC",
                (user_id, character_id, *statuses),
            ).fetchall()
        return [OpenLoop(r["id"], r["character_id"], r["user_id"], r["topic"],
                         r["expected_at"], r["status"], r["created_scene"], r["created_at"])
                for r in rows]

    def set_open_loop_status(self, loop_id: str, status: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE open_loop SET status=? WHERE id=?", (status, loop_id))
            self._conn.commit()

    def mark_open_loops_asked(self, user_id: str, character_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE open_loop SET status='asked' WHERE user_id=? AND character_id=? AND status='ask_now'",
                (user_id, character_id),
            )
            self._conn.commit()

    # ---- 汇总召回 ----

    def always_recall(self, user_id: str, character_id: str, limit: int = 3) -> list[Memory]:
        """必须常驻的关键事实，不参与相似度竞争。

        教训：用户问「你还记得我叫什么吗」时，问题文本和「用户希望被称呼为「阿哲」」
        几乎没有任何字符重合，纯相似度召回必然失败。
        但这类事实一旦漏掉，用户会直接认定「她根本没记住我」——是信任崩塌级别的事故。

        所以名字、称呼这类关键事实永远注入，不赌召回率。
        """
        pool = [m for m in self.all_memories(user_id, character_id)
                if m.confidence >= 0.9 and "称呼" in m.content]
        return pool[:limit]

    def recall_all(self, user_id: str, character_id: str, query: str,
                   current_scene: str | None, recent_turns: int | None = None) -> RecallResult:
        recent_turns = recent_turns or config.RECENT_TURNS

        # 关键事实先占位，避免常规召回把它们挤掉
        pinned = self.always_recall(user_id, character_id)
        pinned_ids = {m.id for m in pinned}

        recalled = self.recall(user_id, character_id, query, current_scene,
                               limit=config.RECALL_LIMIT)
        merged = pinned + [m for m in recalled if m.id not in pinned_ids]

        return RecallResult(
            profile=self.get_profile(user_id, character_id),
            memories=merged,
            open_loops=self.open_loops(user_id, character_id, statuses=("pending", "ask_now")),
            summary=self.get_summary(user_id, character_id),
            recent=self.recent_turns(user_id, character_id, recent_turns),
        )

    # ---- 用户数据权利 ----

    def export_user(self, user_id: str, character_id: str) -> dict[str, Any]:
        """导出全部交互数据。用户有权复制自己的数据。"""
        return {
            "user_id": user_id,
            "character_id": character_id,
            "profile": self.get_profile(user_id, character_id),
            "summary": self.get_summary(user_id, character_id),
            "memories": [m.to_dict() for m in self.all_memories(user_id, character_id)],
            "open_loops": [l.to_dict() for l in self.open_loops(user_id, character_id, statuses=("pending", "ask_now", "asked"))],
            "messages": [t.to_dict() for t in self.recent_turns(user_id, character_id, 500)],
        }

    def delete_user(self, user_id: str, character_id: str) -> dict[str, int]:
        """彻底删除该用户在该角色下的全部数据。"""
        counts: dict[str, int] = {}
        with self._lock:
            for table in ("profile", "message", "summary", "memory", "open_loop"):
                cur = self._conn.execute(
                    f"DELETE FROM {table} WHERE user_id=? AND character_id=?", (user_id, character_id)
                )
                counts[table] = cur.rowcount
            self._conn.commit()
        return counts

    # ---- 生命周期 ----

    def close(self) -> None:
        """关闭数据库连接。不关会在 Windows 上留下文件锁。"""
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ---- 内部 ----

    @staticmethod
    def _row_to_memory(r: sqlite3.Row) -> Memory:
        return Memory(
            id=r["id"], character_id=r["character_id"], user_id=r["user_id"],
            type=r["type"], content=r["content"], confidence=r["confidence"],
            source_scene=r["source_scene"], source_turn=r["source_turn"],
            created_at=r["created_at"], expires_at=r["expires_at"], visibility=r["visibility"],
        )


# ---------------------------------------------------------------- 记忆抽取
# 规则通道：离线可跑，且是 LLM 抽取的兜底。
# LLM 通道由 memory_extractor 参数注入（见 server.py 的异步写入）。

# 中文不能用 \b，改用否定前瞻排除疑问词，并强制命令式语气。
# 教训：`我是`/`叫我` 这类宽泛模式会把「你还记得我叫什么吗」误判为自报姓名。
_NAME_COMMAND_PATTERNS = [
    re.compile(r"我(?:的名字)?(?:叫|是)(?!什么|啥|谁|哪儿|哪里|不是)([\u4e00-\u9fffA-Za-z]{1,12})"),
    re.compile(r"(?:你可以|你)?叫我(?!什么|啥)([\u4e00-\u9fffA-Za-z]{1,12})"),
]

_NAME_EN_PATTERNS = [
    re.compile(r"my name is\s+([A-Za-z]{1,20})", re.IGNORECASE),
    re.compile(r"call me\s+([A-Za-z]{1,20})", re.IGNORECASE),
]

# 用户被怎么称呼，只有两种情况算数：自报姓名，或明确要她怎么叫。
# 其他问句一律不碰，宁可漏也不能记错。
_NAME_QUERY_PATTERNS = [
    re.compile(r"(?:我|我的名字).{0,4}(?:叫什么|叫啥|是什么)"),
    re.compile(r"还记得我"),
]

# 感叹式的自报姓名（「我叫阿哲啊」「我是小林呢」）。这些是陈述，不是提问。
_NAME_EXCLAIM = ("啊", "呀", "啦", "哈", "吧", "哦", "噢", "嘛", "来着")


def looks_like_name_query(text: str) -> bool:
    """这句话是在问「我叫什么」，还是在自报姓名。

    这是共享判断，记忆抽取与回复生成都用它。
    分不清的代价很大：把提问当成事实存下来，她会记错用户的名字；
    把陈述当成提问，她就永远记不住。
    """
    t = text or ""
    if not any(p.search(t) for p in _NAME_QUERY_PATTERNS):
        return False
    # 出现了感叹式语气词，说明是在陈述而不是在问
    return not any(t.rstrip("。！!~～ ").endswith(x) for x in _NAME_EXCLAIM)

_JOB_PATTERNS = [
    re.compile(r"我是(?:一个|一名)?([\u4e00-\u9fff]{2,10}(?:师|员|生|工|总监|经理|医生|老师|律师))"),
    re.compile(r"我(?:在|做)([\u4e00-\u9fff]{2,12})(?:工作|上班)"),
    re.compile(r"I(?:'m| am) a[n]?\s+([a-zA-Z ]{3,24})", re.IGNORECASE),
]

# 常见饮品。命中直接提升为偏好记忆，因为「点了什么」是陪伴场景里被问得最多的一类事实。
_DRINK_WORDS = (
    "美式", "拿铁", "手冲", "卡布奇诺", "摩卡", "澳白", "馥芮白", "冷萃",
    "espresso", "latte", "americano", "flat white",
    "咖啡", "红茶", "绿茶", "奶茶", "气泡水", "柠檬水", "热可可", "牛奶",
    "啤酒", "威士忌", "红酒", "汽水", "可乐", "果汁",
)

# 点单与摄取。放在事件规则之前，否则「今天喝了杯冰美式」会被当成泛泛的事件。
# 三种语序都要覆盖：动词在前、动词在量词后、量词起头。
_CONSUME_VERBS = r"(?:喝|吃|要点|要了|来了|点了|来|要)"
_CONSUME_PATTERNS = [
    re.compile(_CONSUME_VERBS + r"(?:了|过|杯|瓶|份|碗|个|点)?\s*"
               r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z ]{0,12})"),
    re.compile(r"(?:给|帮|替)我(?:来|要|点|拿)?\s*(?:一|两|半)?\s*(?:杯|瓶|份|碗|个)?\s*"
               r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z ]{0,12})"),
    re.compile(r"^(?:一|两|半)?\s*(?:杯|瓶|份|碗|个)\s*"
               r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z ]{0,12})"),
]

# 修饰语与标点，归一化时去掉，只留核心词
_DRINK_NOISE = ("一杯", "两杯", "半杯", "杯", "瓶", "份", "碗", "个", "一点", "冰", "热", "的", "了", "过", "要", "来", "点")

_COMMON_DRINK_TAIL = re.compile(r"([\u4e00-\u9fff]{2,4}(?:美式|拿铁|手冲|摩卡|冷萃|奶茶|咖啡|茶|水|酒))")


def _normalize_drink(raw: str) -> str:
    """把「一杯冰美式」这类说法归一到饮品名。返回空串表示不像饮品。"""
    text = (raw or "").strip()
    for noise in _DRINK_NOISE:
        text = text.replace(noise, "")
    text = text.strip("，。！？、,.!? ")

    low = text.lower()
    for word in _DRINK_WORDS:
        if word in low:
            return word
    m = _COMMON_DRINK_TAIL.search(text)
    if m:
        return m.group(1)
    return ""


_PREFERENCE_PATTERNS = [
    re.compile(r"我(?:最喜欢|喜欢|爱吃|讨厌|不喜欢|怕|受不了)([\u4e00-\u9fffA-Za-z0-9]{2,20})"),
]

_EVENT_PATTERNS = [
    re.compile(r"(?:今天|昨天|明天|下周|后天|这个周末)([\u4e00-\u9fff，,、]{4,40})"),
]

# 未闭合话题：含未来时间指向 + 未完成事件的句子
_FUTURE_MARKERS = ("明天", "后天", "下周", "下个月", "这个周末", "过几天", "下周", "待会", "马上要", "即将")
_EVENT_NOUNS = ("面试", "考试", "答辩", "手术", "体检", "出差", "旅行", "约", "见面", "报告", "汇报", "比赛", "生日", "搬家", "入职")

# 「翻记忆」型提问。用户问这类问题时她必须去查记忆，不能拿当前这句话去匹配。
# 注意第二条判断要求句中有疑问词，所以「我上次点了美式」这类陈述不会被误判。
_RECALL_QUERY_MARKERS = (
    "上次", "之前", "以前", "那次", "刚才",
    "我说过", "我说了", "我提过", "我跟你说过",
    "我点了", "我要了", "我吃了", "我喝了",
    "你还记得", "记不记得", "还记得吗", "你记得",
    "再说一次", "再说一遍", "重复一遍",
)


def looks_like_recall_query(text: str) -> bool:
    """用户是不是在让她翻记忆。

    分不清的代价：用户问「我上次点了什么」，系统却拿这句话本身去算相似度，
    自然什么都召不回来，她只能答「你继续说」，用户认为她完全没记住。
    """
    t = text or ""
    if not any(k in t for k in _RECALL_QUERY_MARKERS):
        return False
    # 且句中要有疑问或指示语气，避免把「我上次点了美式」这种陈述也当成提问
    return any(k in t for k in ("什么", "啥", "吗", "呢", "?", "？", "哪个", "哪些"))


def extract_rule_based(user_message: str) -> tuple[list[tuple[str, str, float]], list[str]]:
    """规则抽取。返回 (记忆列表, 未闭合话题列表)。

    记忆列表元素为 (type, content, confidence)。
    """
    text = user_message or ""
    memories: list[tuple[str, str, float]] = []
    loops: list[str] = []

    # 用户在问「我叫什么」时绝不抽取姓名，否则会把疑问句存成事实
    asking_about_name = looks_like_name_query(text)

    if not asking_about_name:
        for pat in _NAME_COMMAND_PATTERNS + _NAME_EN_PATTERNS:
            m = pat.search(text)
            if m:
                name = m.group(1).strip()
                if name and name not in ("你", "我", "谁", "什么", "啥") and len(name) <= 12:
                    memories.append(("fact", f"用户希望被称呼为「{name}」", 0.95))
                    break

    for pat in _JOB_PATTERNS:
        m = pat.search(text)
        if m:
            memories.append(("fact", f"用户的工作与「{m.group(1).strip()}」有关", 0.8))
            break

    # 点单与摄取优先于泛事件。先抽到饮品就不再把整句记成事件，避免同一件事存两条。
    drink = ""
    for pat in _CONSUME_PATTERNS:
        m = pat.search(text)
        if m:
            drink = _normalize_drink(m.group(1))
            if drink:
                break
    if drink:
        memories.append(("preference", f"用户喜欢喝{drink}", 0.9))
    else:
        for pat in _PREFERENCE_PATTERNS:
            m = pat.search(text)
            if m:
                memories.append(("preference", f"用户提到：{m.group(0).strip()}", 0.75))
                break

    if not drink:
        for pat in _EVENT_PATTERNS:
            m = pat.search(text)
            if m:
                memories.append(("event", f"用户提到：{m.group(0).strip()}", 0.7))
                break

    # 未闭合话题：有未来时间指向 + 事件名词
    if any(marker in text for marker in _FUTURE_MARKERS):
        for noun in _EVENT_NOUNS:
            if noun in text:
                # 摘出包含该名词的短句作为话题
                for piece in re.split(r"[。！？!?\n，,]", text):
                    if noun in piece and piece.strip():
                        loops.append(piece.strip()[:40])
                        break
                break

    return memories, loops


class MemoryWriter:
    """异步记忆写入。

    分工（这是为了避免一类很伤信任的事故）：
      抽取 —— 同步做。它只是本地正则，没有 I/O，开销可忽略。
              用户刚说完「我叫阿哲」紧接着问「你还记得我叫什么吗」，
              如果抽取还在排队，她就会答「你还没告诉过我」。
      落库 —— 异步做。这是真正的写入，放后台，不阻塞回复。
    """

    def __init__(self, store: MemoryStore,
                 extractor: Callable[[str], tuple[list[tuple[str, str, float]], list[str]]] | None = None):
        self.store = store
        self.extractor = extractor or extract_rule_based
        self._queue: list[tuple[str, str, list[tuple[str, str, float]], list[str], str, str]] = []
        self._cv = threading.Condition()
        self._stop = False
        self._thread = threading.Thread(target=self._run, name="memory-writer", daemon=True)
        self._thread.start()

    def extract(self, message: str) -> tuple[list[tuple[str, str, float]], list[str]]:
        """同步抽取。在回复路径上调用，保证同轮产生的记忆立即可用。"""
        try:
            return self.extractor(message)
        except Exception:
            return [], []

    def submit(self, user_id: str, character_id: str, message: str,
               scene_id: str, turn_id: str,
               extracted: tuple[list[tuple[str, str, float]], list[str]] | None = None) -> None:
        """把落库排进后台。extracted 为空时退回异步抽取。"""
        with self._cv:
            self._queue.append((user_id, character_id, extracted, scene_id, turn_id, message))
            self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._queue and not self._stop:
                    self._cv.wait(timeout=1.0)
                if self._stop and not self._queue:
                    return
                item = self._queue.pop(0) if self._queue else None
            if item is None:
                continue
            try:
                self._process(*item)
            except Exception:
                # 记忆写入失败不能影响主流程
                pass

    def _process(self, user_id: str, character_id: str,
                 extracted: tuple[list[tuple[str, str, float]], list[str]] | None,
                 scene_id: str, turn_id: str, message: str) -> None:
        memories, loops = extracted if extracted is not None else self.extractor(message)
        for type_, content, conf in memories:
            self.store.add_memory(user_id, character_id, type_, content,
                                  confidence=conf, source_scene=scene_id, source_turn=turn_id)
        for topic in loops:
            self.store.add_open_loop(user_id, character_id, topic, scene=scene_id)

    def shutdown(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":  # pragma: no cover
    import os
    import time

    db = str(config.DATA_DIR / "_selftest_memory.db")
    if os.path.exists(db):
        os.remove(db)
    store = MemoryStore(db)

    store.set_profile("u1", "elise", "address", "阿哲")
    store.add_message("u1", "elise", "user", "我明天有个面试", "cafe")
    store.add_message("u1", "elise", "assistant", "加油，回来告诉我结果", "cafe")

    writer = MemoryWriter(store)
    writer.submit("u1", "elise", "我叫阿哲，我明天有个面试，我最喜欢喝美式", "cafe", "t1")
    time.sleep(0.5)

    print("画像:", store.get_profile("u1", "elise"))
    print("记忆:", [m.to_dict() for m in store.all_memories("u1", "elise")])
    print("未闭合:", [l.to_dict() for l in store.open_loops("u1", "elise")])
    print("召回(面试):", [m.content for m in store.recall("u1", "elise", "我那个面试怎么样了", "park")])

    writer.shutdown()
    store._conn.close()
    os.remove(db)

class ConversationStore(MemoryStore):
    """工作台会话与质量反馈。SQLite 仍只在此模块使用。"""
    def __init__(self, db_path=None):
        super().__init__(db_path)
        self._conn.executescript('''
            CREATE TABLE IF NOT EXISTS conversation (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, character_id TEXT NOT NULL,
                scene_id TEXT NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL,
                intimacy INTEGER NOT NULL DEFAULT 0, use_memory INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS reply_record (
                turn_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                user_text TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS feedback (
                turn_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL,
                rating INTEGER NOT NULL, note TEXT NOT NULL, updated_at TEXT NOT NULL);
        ''')
        self._conn.commit()

    def create_conversation(self, owner, character_id, scene_id, intimacy=0):
        cid = 'chat_' + uuid.uuid4().hex
        with self._lock:
            self._conn.execute('INSERT INTO conversation VALUES(?,?,?,?,?,?,?,?)',
                (cid, owner, character_id, scene_id, '新对话', _now(), intimacy, 0))
            self._conn.commit()
        self.set_profile(cid, character_id, 'intimacy', str(intimacy))
        self.set_profile(cid, character_id, 'current_scene', scene_id)
        return self.conversation(owner, cid)

    def conversation(self, owner, cid):
        with self._lock:
            row = self._conn.execute('SELECT * FROM conversation WHERE id=? AND owner=?', (cid, owner)).fetchone()
        if row is None: raise ValueError('会话不存在')
        return dict(row)

    def conversations(self, owner):
        with self._lock:
            return [dict(r) for r in self._conn.execute('SELECT * FROM conversation WHERE owner=? ORDER BY rowid DESC', (owner,)).fetchall()]

    def configure_conversation(self, owner, cid, scene_id, intimacy, use_memory):
        convo = self.conversation(owner, cid)
        with self._lock:
            self._conn.execute('UPDATE conversation SET scene_id=?,intimacy=?,use_memory=? WHERE id=? AND owner=?',
                               (scene_id, intimacy, int(use_memory), cid, owner))
            self._conn.commit()
        self.set_profile(cid, convo['character_id'], 'current_scene', scene_id)
        self.set_profile(cid, convo['character_id'], 'intimacy', str(intimacy))
        return self.conversation(owner, cid)

    def save_reply(self, cid, user_text, payload):
        with self._lock:
            self._conn.execute('INSERT INTO reply_record VALUES(?,?,?,?,?)',
                (payload['reply']['turn_id'], cid, user_text, json.dumps(payload, ensure_ascii=False), _now()))
            self._conn.execute("UPDATE conversation SET title=? WHERE id=? AND title='新对话'", (user_text[:24], cid))
            self._conn.commit()

    def replies(self, owner, cid):
        self.conversation(owner, cid)
        with self._lock:
            rows = self._conn.execute('SELECT r.*,f.rating,f.note FROM reply_record r LEFT JOIN feedback f ON r.turn_id=f.turn_id WHERE r.conversation_id=? ORDER BY r.rowid', (cid,)).fetchall()
        return [{'user_text': r['user_text'], 'payload': json.loads(r['payload']),
                 'feedback': {'rating': r['rating'], 'note': r['note']}} for r in rows]

    def save_feedback(self, owner, cid, turn_id, rating, note):
        self.conversation(owner, cid)
        if rating not in (-1, 1): raise ValueError('评分必须为喜欢或不喜欢')
        with self._lock:
            if not self._conn.execute('SELECT 1 FROM reply_record WHERE turn_id=? AND conversation_id=?', (turn_id, cid)).fetchone():
                raise ValueError('回复不存在')
            self._conn.execute('INSERT INTO feedback VALUES(?,?,?,?,?) ON CONFLICT(turn_id) DO UPDATE SET rating=excluded.rating,note=excluded.note,updated_at=excluded.updated_at',
                               (turn_id, cid, rating, note[:2000], _now()))
            self._conn.commit()
