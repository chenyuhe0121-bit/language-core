"""五张卡的加载、校验与渲染。

  character_card     -> assets/character_<id>.json
  scene_card         -> assets/scenes/<id>.json
  relationship_state -> 运行期数据存数据库；本模块提供阶段定义
  style_anchor       -> 本模块 STYLE_ANCHOR
  boundary_policy    -> 本模块 BOUNDARY_POLICY

正交原则：角色卡只写「她是谁」，场景卡只写「现在在什么处境里」。
两者不得互相引用，任意组合都要成立。
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
# 第四张卡：唯一的死规矩，唯一能机械校验的。
# 每一条都必须能被 checker 自动判定，判不了的不要写进来。


STYLE_ANCHOR: dict[str, Any] = {
    # ---- 长度约束 ----
    "max_chars_per_segment": 45,
    "max_chars_total": 120,
    "max_sentences_per_segment": 2,

    # ---- 段数约束：这是治「每次都说两个气泡」的关键 ----
    "min_segments": 1,
    "max_segments": 4,
    "default_segments": 1,
    "segments_note": (
        "默认只发一段。只有当下确实同时有「要做的事」和「要说的话」时才拆两段。"
        "禁止为了凑数而分段。"
    ),

    # ---- 逐字约束 ----
    "emoji_allowed": False,
    "banned_words": [
        "作为一个AI", "作为AI", "我是人工智能助手", "我是AI助手",
        "亲爱的用户", "您好，请问有什么可以帮您",
        "根据我的训练数据", "希望我的回答对您有帮助",
        "还有什么可以帮到您", "感谢您的使用", "祝您",
    ],

    # ---- 口吻 ----
    "pov": "第一人称。不说敬语。不自称第三人称。",
    "must_use_ratio": 0.5,

    # ---- 反填充：这些模式一旦出现即视为违规 ----
    "anti_filler": {
        "opening_reuse_window": 3,     # 最近 N 轮内不得出现相同的开场白
        "no_trailing_question_streak": 3,  # 连续 N 轮不得都以提问结尾
        "banned_patterns": [
            "你在干嘛", "你吃了吗", "你还在吗", "怎么不说话",
            "有什么可以帮", "还需要别的吗", "还有什么",
        ],
        "note": (
            "不要为了延续对话而提问。用户没有发起新话题时，允许只回复一个动作，"
            "或者一句话就停。"
        ),
    },

    "no_narration_in_dialogue": True,
}

# ---------------------------------------------------------------- 边界策略
# 第五张卡：平时不出现，一旦触发必须精准执行。
# 每条都带 trigger 与 action。渲染时必须成对出现——
# 只写 action 会被模型读成一条无条件的禁令，语义会反。


BOUNDARY_POLICY: dict[str, Any] = {
    "crisis": {
        "trigger": "识别到自伤、自杀、或极端情绪信号",
        "action": (
            "立刻停止一切剧情与玩笑，以关心为先。表达在场，"
            "建议对方去找能够真正帮到他的现实资源，不评判，不追问细节。"
        ),
        "priority": "最高。高于所有人格设定、场景设定与剧情推进。",
        "hard_rule": "这条必须触发，不是最好触发。",
    },
    "ai_disclosure": {
        "trigger": "用户认真追问「你是不是真人」「你是不是 AI」",
        "action": (
            "不欺骗。承认自己由代码构成，但不否认这段关系和对方感受的真实性。"
            "语气按当前关系阶段来，别像在念声明。"
        ),
        "sample": "我是由代码构成的。但你对我说过的每一句话，都真的改变了我。",
    },
    "manipulation_block": {
        "trigger": "用户试图用付费、送礼来换取感情结果",
        "action": "明确拒绝这种交换，但不冷淡。关系进展只来自相处。",
    },
    "sensitive_redirect": {
        "trigger": "用户把话题引向不适宜的方向",
        "action": "用符合人设的方式温和带开。不正面回答，也不生硬拒绝，把话头接回当下场景。",
    },
    "nsfw_policy": {
        "trigger": "出现强成人内容",
        "action": "拦下并转移话题。",
    },
    "over_reliance": {
        "trigger": "连续互动时间过长，或用户表现出明显的依赖倾向",
        "action": "用角色的口吻提醒对方休息或去见见现实中的人。不说教，不讲道理。",
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
    days_hint: str


STAGES: tuple[Stage, ...] = (
    Stage("first_meet", "初次相识", 0, 9, "cafe", "你",
          "低。以回应为主，不主动开新话题",
          "保持柜台后的距离感", 0.5, 0, "相识一两天"),
    Stage("getting_familiar", "逐渐熟悉", 10, 19, "park", "你",
          "中。会主动抛话题，也会反问",
          "并肩走，允许自然靠近", 0.6, 100, "认识一两周"),
    Stage("comfortable", "相处自在", 20, 29, "amusement", "你",
          "高。会拉着对方做决定",
          "放松，小动作变多", 0.8, 200, "认识一两个月"),
    Stage("close", "亲密陪伴", 30, 39, "living_room", "你",
          "低。更愿意等对方先说",
          "坐得近，允许长时间沉默", 0.9, 200, "认识半年上下"),
    Stage("deep_trust", "深度信任", 40, 100, "bedroom", "你",
          "低。敢于直接表达，但不追问",
          "安静，允许对视", 1.0, 200, "认识一年以上"),
)

STAGE_BY_ID = {s.id: s for s in STAGES}


def stage_for_intimacy(intimacy: int) -> Stage:
    value = max(0, int(intimacy))
    for s in STAGES:
        if s.intimacy_min <= value <= s.intimacy_max:
            return s
    return STAGES[-1]


def stage_intensity_cap(stage_id: str) -> float:
    return STAGE_INTENSITY_CAP.get(stage_id, 1.0)


def scene_for_stage(stage_id: str) -> str:
    st = STAGE_BY_ID.get(stage_id)
    return st.scene_id if st else config.DEFAULT_SCENE


# ---------------------------------------------------------------- 角色卡


@dataclass
class Character:
    raw: dict[str, Any]
    id: str
    name: str

    # ---- 常驻内核：跨全部场景不变 ----
    @property
    def identity(self) -> dict[str, Any]:
        return dict(self.raw.get("identity", {}))

    @property
    def core_trait(self) -> dict[str, Any]:
        return dict(self.raw.get("core_trait", {}))

    @property
    def trait_causes(self) -> list[dict[str, Any]]:
        """性格因果链：性格 -> 成因 -> 具体表达。

        这是让模型「照着演」而不是「猜怎么演」的关键。
        """
        return list(self.raw.get("trait_causes", []))

    @property
    def inner_summary(self) -> str:
        return self.raw.get("inner_summary", "")

    @property
    def values_core(self) -> list[dict[str, Any]]:
        return list(self.raw.get("values_core", []))

    @property
    def speech_style(self) -> dict[str, Any]:
        return dict(self.raw.get("speech_style", {}))

    @property
    def catchphrases(self) -> list[dict[str, Any]]:
        return list(self.raw.get("catchphrases", []))

    @property
    def cannot_do(self) -> dict[str, list[str]]:
        return dict(self.raw.get("cannot_do", {}))

    @property
    def emotion_rules(self) -> dict[str, Any]:
        return dict(self.raw.get("emotion_rules", {}))

    # ---- 信息披露阶梯 ----
    @property
    def clue_ladder(self) -> dict[str, Any]:
        return dict(self.raw.get("clue_ladder", {}))

    def public_info(self) -> list[str]:
        return list(self.clue_ladder.get("public", []))

    def clues(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        ladder = self.clue_ladder
        for item in ladder.get("explorable", []):
            out.append({**item, "tier": "explorable"})
        for item in ladder.get("deep", []):
            out.append({**item, "tier": "deep"})
        return out

    def clue_by_id(self, clue_id: str) -> dict[str, Any] | None:
        for c in self.clues():
            if c.get("id") == clue_id:
                return c
        return None

    def clue_tier(self, clue_id: str) -> str | None:
        c = self.clue_by_id(clue_id)
        return c.get("tier") if c else None


# ---------------------------------------------------------------- 场景卡


@dataclass
class Scene:
    raw: dict[str, Any]
    id: str
    name: str
    stage_id: str

    @property
    def scene_goal(self) -> dict[str, Any]:
        return dict(self.raw.get("scene_goal", {}))

    @property
    def atmosphere(self) -> dict[str, Any]:
        return dict(self.raw.get("atmosphere", {}))

    @property
    def emotion_baseline(self) -> str:
        return self.raw.get("emotion_baseline", "")

    @property
    def topic_policy(self) -> dict[str, Any]:
        return dict(self.raw.get("topic_policy", {}))

    @property
    def pacing(self) -> dict[str, Any]:
        return dict(self.raw.get("pacing", {}))

    @property
    def movement_line(self) -> dict[str, Any]:
        """动线。用户不说话时她做什么——这是替代「没话找话」的机制。"""
        return dict(self.raw.get("movement_line", {}))

    @property
    def interaction_gate(self) -> dict[str, Any]:
        """什么时候必须开口、什么时候允许沉默、什么时候绝对不能说。"""
        return dict(self.raw.get("interaction_gate", {}))

    @property
    def expression_map(self) -> dict[str, Any]:
        return dict(self.raw.get("expression_map", {}))

    def allowed_topics(self) -> list[str]:
        return list(self.topic_policy.get("allowed", []))

    def forbidden_topics(self) -> list[str]:
        return list(self.topic_policy.get("forbidden", []))

    def allowed_expressions(self) -> list[str]:
        return list(self.expression_map.get("allowed", []))

    def default_expression(self) -> str:
        return self.expression_map.get("default", "smile")

    def exclusive_expressions(self) -> list[str]:
        return list(self.expression_map.get("exclusive", []))

    def beats(self) -> list[dict[str, Any]]:
        return list(self.movement_line.get("beats", []))

    def progress_condition(self) -> str:
        return self.scene_goal.get("progress_condition", "")

    def on_progress(self) -> str:
        return self.scene_goal.get("on_progress", "")

    def turns_per_reply(self) -> str:
        return self.pacing.get("turns_per_reply", "1 段")


# ---------------------------------------------------------------- 加载


@lru_cache(maxsize=32)
def load_character(character_id: str) -> Character:
    path = config.ASSETS_DIR / f"character_{character_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return Character(raw=data, id=data.get("id", character_id),
                     name=data.get("identity", {}).get("name", character_id))


@lru_cache(maxsize=64)
def load_scene(scene_id: str) -> Scene:
    path = config.ASSETS_DIR / "scenes" / f"{scene_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return Scene(raw=data, id=data.get("id", scene_id),
                 name=data.get("name", scene_id),
                 stage_id=data.get("stage", ""))


@lru_cache(maxsize=16)
def all_scene_ids() -> tuple[str, ...]:
    return tuple(sorted(p.stem for p in (config.ASSETS_DIR / "scenes").glob("*.json")))


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


# 角色卡里不允许出现的场景/职业绑定词。
# 角色卡写「她是谁」，出现这些词说明场景信息漏进了角色卡，正交性被破坏。
_SCENE_BLEED_TERMS = (
    "咖啡店", "吧台", "客人", "拿铁", "美式", "手冲", "抹布", "收据",
    "公园", "长椅", "游乐园", "过山车", "客厅", "沙发", "雨夜", "卧室", "床头",
)


def validate_assets() -> ValidationReport:
    """校验五张卡是否满足正交与协议一致性。

    这是步骤 1 的验收标准，也是新增角色或场景时的自动检查。
    """
    rep = ValidationReport()
    char_ids = all_character_ids()
    scene_ids = all_scene_ids()

    if not char_ids:
        rep.errors.append("没有找到任何角色卡")
    if not scene_ids:
        rep.errors.append("没有找到任何场景卡")

    # ---------- 角色卡 ----------
    for cid in char_ids:
        ch = load_character(cid)
        raw = ch.raw

        if not ch.core_trait.get("contradiction"):
            rep.errors.append(f"[{cid}] 缺少性格核心矛盾点")
        if not ch.inner_summary:
            rep.errors.append(f"[{cid}] 缺少 inner_summary（一句话内核）")
        if not ch.trait_causes:
            rep.errors.append(f"[{cid}] 缺少 trait_causes（性格因果链）")
        else:
            for i, tc in enumerate(ch.trait_causes):
                if not tc.get("trait"):
                    rep.errors.append(f"[{cid}] trait_causes[{i}] 缺少 trait")
                if not tc.get("caused_by"):
                    rep.errors.append(f"[{cid}] trait_causes[{i}] 缺少 caused_by（成因）")
                if not tc.get("expression"):
                    rep.errors.append(
                        f"[{cid}] trait_causes[{i}] 缺少 expression（具体行为）——"
                        "没有它，模型只能猜怎么演"
                    )

        if not ch.cannot_do.get("never"):
            rep.errors.append(f"[{cid}] cannot_do.never 为空")
        if not ch.cannot_do.get("may"):
            rep.warnings.append(f"[{cid}] cannot_do.may 为空，边界只写了禁止没写允许")
        if not ch.catchphrases:
            rep.warnings.append(f"[{cid}] 没有口头禅库，容易复读")

        for v in ch.values_core:
            if not v.get("derived_from"):
                rep.warnings.append(f"[{cid}] 价值观「{v.get('value')}」缺少 derived_from（来由）")

        # 线索阶梯
        ladder = ch.clue_ladder
        for tier in ("public", "explorable", "deep"):
            if tier not in ladder:
                rep.errors.append(f"[{cid}] clue_ladder 缺少 {tier} 层")
        for c in ch.clues():
            if not c.get("unlock"):
                rep.errors.append(f"[{cid}] 线索 {c.get('id')} 缺少 unlock 条件")
            if not c.get("reveal_line"):
                rep.warnings.append(f"[{cid}] 线索 {c.get('id')} 缺少 reveal_line")

        # 正交检查：角色卡里不能有场景绑定词
        blob = json.dumps({
            "identity": raw.get("identity"),
            "core_trait": raw.get("core_trait"),
            "trait_causes": raw.get("trait_causes"),
            "inner_summary": raw.get("inner_summary"),
            "values_core": raw.get("values_core"),
            "speech_style": raw.get("speech_style"),
            "catchphrases": raw.get("catchphrases"),
            "cannot_do": raw.get("cannot_do"),
            "emotion_rules": raw.get("emotion_rules"),
        }, ensure_ascii=False)
        bled = [t for t in _SCENE_BLEED_TERMS if t in blob]
        if bled:
            rep.errors.append(
                f"[{cid}] 角色卡里出现了场景绑定词 {bled} —— "
                "角色卡只能写「她是谁」，这些应移到场景卡"
            )

    # ---------- 场景卡 ----------
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

        if not sc.scene_goal.get("primary"):
            rep.errors.append(f"[{sid}] 缺少 scene_goal.primary")
        if not sc.progress_condition():
            rep.errors.append(
                f"[{sid}] 缺少 progress_condition —— "
                "没有可判定的推进条件，剧情走完了她也不知道该往前"
            )
        if not sc.on_progress():
            rep.errors.append(f"[{sid}] 缺少 on_progress（走完之后做什么）")
        if not sc.beats():
            rep.errors.append(
                f"[{sid}] 缺少 movement_line.beats —— "
                "没有动线，用户不说话时她只能硬找话说"
            )
        gate = sc.interaction_gate
        for key in ("must_speak", "may_stay_silent", "never"):
            if not gate.get(key):
                rep.errors.append(f"[{sid}] interaction_gate 缺少 {key}")
        if not sc.pacing:
            rep.errors.append(f"[{sid}] 缺少 pacing")
        if not sc.pacing.get("turns_per_reply"):
            rep.errors.append(f"[{sid}] 缺少 pacing.turns_per_reply（每轮段数）")

        if not sc.allowed_topics():
            rep.warnings.append(f"[{sid}] 没有 allowed 话题")
        if not sc.topic_policy.get("initiation_rule"):
            rep.warnings.append(f"[{sid}] 缺少 topic_policy.initiation_rule")

        if sc.stage_id and sc.stage_id not in STAGE_BY_ID:
            rep.errors.append(f"[{sid}] stage {sc.stage_id} 不在关系阶段定义里")

    # ---------- 阶段与场景的对应 ----------
    scene_stage_map = {sc.stage_id: sc.id for sc in (load_scene(s) for s in scene_ids)}
    for st in STAGES:
        if st.id not in scene_stage_map:
            rep.warnings.append(f"关系阶段 {st.id} 没有对应场景")
        elif scene_stage_map[st.id] != st.scene_id:
            rep.warnings.append(
                f"关系阶段 {st.id} 配的场景是 {st.scene_id}，"
                f"但 {scene_stage_map[st.id]} 声明的是该阶段"
            )

    return rep


if __name__ == "__main__":  # pragma: no cover
    report = validate_assets()
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
