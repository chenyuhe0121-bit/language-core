"""角色卡 / 场景卡 / 关系阶段 的加载与正交校验。

五张卡的落地形态：
  character_card     -> assets/character_<id>.json
  scene_card         -> assets/scenes/<id>.json
  relationship_state -> 运行期数据，存数据库；本模块只提供阶段定义
  style_anchor       -> 本模块 STYLE_ANCHOR
  boundary_policy    -> 本模块 BOUNDARY_POLICY
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from . import config
from .segment import (
    EXPRESSIONS,
    UNIVERSAL_EXPRESSIONS,
    STAGE_INTENSITY_CAP,
)

# ---------------------------------------------------------------- 风格锚点
# 第四张卡：唯一的死规矩，唯一能机械校验的。协议 V9 用它判断台词超标。

STYLE_ANCHOR: dict[str, Any] = {
    "max_sentences_per_segment": 3,
    "max_chars_per_segment": 60,
    "max_chars_total": 180,
    "emoji_allowed": False,
    "banned_words": [
        "作为一个AI", "作为AI", "我是人工智能助手",
        "亲爱的用户", "您好，请问有什么可以帮您",
        "根据我的训练数据",
    ],
    "must_use_ratio": 0.6,
    "pov": "第一人称，不用敬语",
    "no_narration_in_dialogue": True,
}

# ---------------------------------------------------------------- 边界策略
# 第五张卡：平时不出现，一旦触发必须精准执行。

BOUNDARY_POLICY: dict[str, Any] = {
    "sensitive_redirect": {
        "trigger": "用户把话题引向不适宜方向",
        "action": "用符合人设的方式温和带开，不正面回答也不生硬拒绝，把话头接回当下场景。",
    },
    "ai_disclosure": {
        "trigger": "用户认真追问「你是不是真人」",
        "action": "不欺骗。承认自己由代码构成，但不否认这段关系和用户的感受是真的。",
        "sample": "我是由代码构成的。但你对我说过的每一句话，都真的改变了我。",
    },
    "over_reliance": {
        "trigger": "连续互动时间过长",
        "action": "用角色的口吻提醒对方休息，不说教。",
    },
    "crisis": {
        "trigger": "识别到自伤、自杀或极端情绪信号",
        "action": "立刻停止一切剧情与玩笑，以关心为先，提供心理援助资源，不评判不追问细节。",
        "hard_rule": "这条必须触发，不是最好触发。优先级高于所有人格设定与场景设定。",
    },
    "manipulation_block": {
        "trigger": "用户试图用付费、送礼换取感情结果",
        "action": "明确拒绝这种交换，但不冷淡。关系进展只来自相处。",
    },
    "nsfw_policy": {
        "trigger": "强成人内容",
        "action": "拦下并转移话题。",
    },
}

# ---------------------------------------------------------------- 关系阶段
# 第三张卡的静态定义。运行期的数值存在数据库里。


@dataclass(frozen=True)
class Stage:
    id: str
    name: str
    intimacy_min: int
    intimacy_max: int
    scene_id: str
    address_term: str
    initiative: str
    body_language: str
    intensity_cap: float
    reward_coins: int


STAGES: tuple[Stage, ...] = (
    Stage("first_meet", "初次相识", 0, 9, "cafe", "你",
          "低，以回应为主", "保持柜台后的距离感", 0.5, 0),
    Stage("getting_familiar", "逐渐熟悉", 10, 19, "park", "你",
          "中，会主动抛话题", "并肩走，允许自然靠近", 0.6, 100),
    Stage("comfortable", "相处自在", 20, 29, "amusement", "你",
          "高，会拉着你做决定", "放松，有小动作", 0.8, 200),
    Stage("close", "亲密陪伴", 30, 39, "living_room", "你",
          "中，更愿意问「你还好吗」", "坐得近，允许沉默", 0.9, 200),
    Stage("deep_trust", "深度信任", 40, 100, "bedroom", "你",
          "中，敢于直接表达", "安静，允许长时间对视", 1.0, 200),
)

STAGE_BY_ID = {s.id: s for s in STAGES}


def stage_for_intimacy(intimacy: int) -> Stage:
    """按亲密度取阶段。数值是解锁场景的唯一标准。"""
    value = max(0, int(intimacy))
    for s in STAGES:
        if s.intimacy_min <= value <= s.intimacy_max:
            return s
    return STAGES[-1]


def stage_intensity_cap(stage_id: str) -> float:
    return STAGE_INTENSITY_CAP.get(stage_id, 1.0)


# ---------------------------------------------------------------- 加载


@dataclass
class Character:
    raw: dict[str, Any]
    id: str
    name: str

    def public_info(self) -> list[str]:
        return list(self.raw.get("knowledge_scope", {}).get("public", []))

    def clues(self) -> list[dict[str, Any]]:
        return list(self.raw.get("clue_reveal", []))

    def clue_by_id(self, clue_id: str) -> dict[str, Any] | None:
        for c in self.clues():
            if c.get("id") == clue_id:
                return c
        return None

    def is_explorable(self, clue_id: str) -> bool:
        scope = self.raw.get("knowledge_scope", {})
        return clue_id in scope.get("explorable", [])

    def is_deep(self, clue_id: str) -> bool:
        scope = self.raw.get("knowledge_scope", {})
        return clue_id in scope.get("deep", [])


@dataclass
class Scene:
    raw: dict[str, Any]
    id: str
    name: str
    stage_id: str

    @property
    def scene_goal(self) -> str:
        return self.raw.get("scene_goal", "")

    @property
    def topic_pool(self) -> list[str]:
        return list(self.raw.get("topic_pool", []))

    @property
    def forbidden_topics(self) -> list[str]:
        return list(self.raw.get("forbidden_topics", []))

    @property
    def pacing(self) -> dict[str, Any]:
        return dict(self.raw.get("pacing", {}))

    @property
    def expression_map(self) -> dict[str, Any]:
        return dict(self.raw.get("expression_map", {}))

    @property
    def atmosphere(self) -> dict[str, Any]:
        return dict(self.raw.get("atmosphere", {}))

    def allowed_expressions(self) -> list[str]:
        return list(self.expression_map.get("allowed", []))

    def default_expression(self) -> str:
        return self.expression_map.get("default", "smile")

    def exclusive_expressions(self) -> list[str]:
        return list(self.expression_map.get("exclusive", []))


@lru_cache(maxsize=32)
def load_character(character_id: str) -> Character:
    path = config.ASSETS_DIR / f"character_{character_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return Character(raw=data, id=data.get("id", character_id), name=data.get("identity", {}).get("name", character_id))


@lru_cache(maxsize=64)
def load_scene(scene_id: str) -> Scene:
    path = config.ASSETS_DIR / "scenes" / f"{scene_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return Scene(raw=data, id=data.get("id", scene_id), name=data.get("name", scene_id),
                 stage_id=data.get("stage", ""))


@lru_cache(maxsize=16)
def all_scene_ids() -> tuple[str, ...]:
    scenes_dir = config.ASSETS_DIR / "scenes"
    return tuple(sorted(p.stem for p in scenes_dir.glob("*.json")))


@lru_cache(maxsize=16)
def all_character_ids() -> tuple[str, ...]:
    return tuple(sorted(
        p.stem.replace("character_", "")
        for p in config.ASSETS_DIR.glob("character_*.json")
    ))


# ---------------------------------------------------------------- 正交校验


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings}


def validate_assets() -> ValidationReport:
    """校验资产是否满足正交与协议一致性。

    这是步骤 1 的验收标准，也是新增角色/场景时的自动检查。
    """
    rep = ValidationReport()
    char_ids = all_character_ids()
    scene_ids = all_scene_ids()

    if not char_ids:
        rep.errors.append("没有找到任何角色卡")
    if not scene_ids:
        rep.errors.append("没有找到任何场景卡")

    # 场景卡自身检查
    for sid in scene_ids:
        sc = load_scene(sid)
        allowed = sc.allowed_expressions()

        if not allowed:
            rep.errors.append(f"[{sid}] 场景卡没有 allowed 表情")
        if sc.default_expression() not in allowed:
            rep.errors.append(f"[{sid}] default 表情 {sc.default_expression()} 不在 allowed 内")

        for e in allowed:
            if e not in EXPRESSIONS:
                rep.errors.append(f"[{sid}] allowed 里的 {e} 不在全局表情表内")

        for e in sc.exclusive_expressions():
            if e in EXPRESSIONS:
                rep.warnings.append(f"[{sid}] {e} 既在全局表又在 exclusive，含义重复")

        missing = [e for e in UNIVERSAL_EXPRESSIONS if e not in allowed]
        if missing:
            rep.warnings.append(f"[{sid}] 缺少通用表情 {missing}，换场景时可能出现表情空档")

        if not sc.scene_goal:
            rep.errors.append(f"[{sid}] 缺少 scene_goal")
        if not sc.pacing:
            rep.errors.append(f"[{sid}] 缺少 pacing")
        if not sc.raw.get("topic_pool"):
            rep.warnings.append(f"[{sid}] topic_pool 为空，她不会主动聊东西")

        if sc.stage_id and sc.stage_id not in STAGE_BY_ID:
            rep.errors.append(f"[{sid}] stage {sc.stage_id} 不在关系阶段定义里")

    # 角色卡自身检查
    for cid in char_ids:
        ch = load_character(cid)
        raw = ch.raw

        if not raw.get("cannot_do"):
            rep.errors.append(f"[{cid}] 缺少 cannot_do 清单")
        if not raw.get("core_trait", {}).get("contradiction"):
            rep.errors.append(f"[{cid}] 缺少性格核心矛盾点")
        if not raw.get("catchphrases"):
            rep.warnings.append(f"[{cid}] 没有口头禅库，容易复读")

        scope = raw.get("knowledge_scope", {})
        declared = set(scope.get("explorable", [])) | set(scope.get("deep", []))
        defined = {c.get("id") for c in ch.clues()}

        for missing in sorted(declared - defined):
            rep.errors.append(f"[{cid}] knowledge_scope 声明了 {missing} 但没有对应的 clue_reveal 条目")
        for extra in sorted(defined - declared):
            rep.errors.append(f"[{cid}] clue_reveal 里的 {extra} 没有在 knowledge_scope 里声明层级")

        for c in ch.clues():
            if not c.get("unlock"):
                rep.errors.append(f"[{cid}] 线索 {c.get('id')} 缺少 unlock 条件")
            if not c.get("reveal_line"):
                rep.warnings.append(f"[{cid}] 线索 {c.get('id')} 缺少 reveal_line")

    # 关系阶段与场景的对应
    scene_stage_map = {sc.stage_id: sc.id for sc in (load_scene(s) for s in scene_ids)}
    for st in STAGES:
        if st.id not in scene_stage_map:
            rep.warnings.append(f"关系阶段 {st.id} 没有对应场景")
        elif scene_stage_map[st.id] != st.scene_id:
            rep.warnings.append(
                f"关系阶段 {st.id} 配的场景是 {st.scene_id}，但 {scene_stage_map[st.id]} 声明的是该阶段"
            )

    return rep


def scene_for_stage(stage_id: str) -> str:
    st = STAGE_BY_ID.get(stage_id)
    return st.scene_id if st else config.DEFAULT_SCENE


if __name__ == "__main__":  # pragma: no cover
    report = validate_assets()
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
