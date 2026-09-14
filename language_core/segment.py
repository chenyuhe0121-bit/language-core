"""
segproto/1 —— 台词 / 旁白分段协议

纯标准库实现，不引入任何第三方依赖。
详见 docs/02-segment-protocol.md

设计要点：
  1. 台词（dialogue）与旁白（narration）严格分离，旁白永不朗读
  2. 模型输出「行协议」而非 JSON —— 更短、更快、可读、免转义
  3. 解析失败一律降级，不抛异常 —— 陪伴产品不能因为格式问题中断对话
  4. 所有枚举封闭，越界值映射到合法值并记 warning
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from typing import Any

PROTOCOL_VERSION = "segproto/1"

# 段落分隔符
SEG_SEPARATOR = "[sep]"

# ---------------------------------------------------------------- 类型枚举

SEGMENT_TYPES: dict[str, bool] = {
    # type: 默认是否朗读
    "dialogue": True,
    "dialogue_with_narration": True,
    "action": False,
    "scene": False,
    "condition": False,
    "inner_thought": False,   # 关键决策：内心独白默认不朗读
    "aside": False,
}

DEFAULT_SEGMENT_TYPE = "dialogue_with_narration"

# 情绪封闭集
EMOTIONS: dict[str, str] = {
    "neutral": "平静",
    "happy": "开心",
    "concern": "关切",
    "sad": "难过",
    "playful": "俏皮",
    "shy": "害羞/心虚",
    "annoyed": "不悦",
    "surprised": "惊讶",
}

PACES = ("very_slow", "slow", "normal", "fast", "very_fast")
PAUSES = ("none", "short", "sentence_end", "paragraph", "hesitation")

INTENTS = (
    "comfort", "tease", "probe", "share", "invite",
    "deflect", "refuse", "agree", "reminisce", "idle",
)

# 全局表情表（实际可用值 = 本表 ∩ 场景卡白名单）
EXPRESSIONS = (
    "smile", "grin", "wry_smile", "surprised",
    "slight_frown", "frown",
    "look_down", "look_away", "glance_up",
    "tilt_head", "lean_forward", "lean_back",
    "blink_slow", "eyes_wide",
    "nod", "shake_head",
    "hand_to_face", "fidget",
)

# 任何场景都可用的最小通用集。场景卡白名单必须包含这几个，
# 否则关系阶段切换时会出现「哪个场景都没有可用表情」的空档。
UNIVERSAL_EXPRESSIONS = (
    "smile", "slight_frown", "look_down", "look_away",
    "glance_up", "tilt_head", "blink_slow", "nod", "hand_to_face",
)

# 关系阶段允许的情绪强度上限（防穿帮：相识阶段不该有 1.0 的情绪爆发）
STAGE_INTENSITY_CAP: dict[str, float] = {
    "first_meet": 0.5,
    "getting_familiar": 0.6,
    "comfortable": 0.8,
    "close": 0.9,
    "deep_trust": 1.0,
}
DEFAULT_INTENSITY_CAP = 1.0

# ---------------------------------------------------------------- 正则

# @seg type=dialogue emotion=concern:0.5 pace=slow expr=a,b intent=comfort
_HEADER_RE = re.compile(r"^@seg\b(.*)$", re.IGNORECASE)
_KV_RE = re.compile(r"(\w+)\s*=\s*(\S+)")

# 中英文括号内容：（...）或 (...)
_PAREN_RE = re.compile(r"[（(]([^（()）]*)[)）]")

# 情绪：name 或 name:intensity
_EMOTION_RE = re.compile(r"^([a-zA-Z_]+)(?::([0-9]*\.?[0-9]+))?$")


def _now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


# ---------------------------------------------------------------- 数据结构

@dataclass
class Emotion:
    name: str
    intensity: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "intensity": round(self.intensity, 2)}


@dataclass
class Narration:
    """旁白：控制语气、情绪、表情。全部字段可空。"""

    emotion: list[Emotion] = field(default_factory=list)
    pace: str | None = None
    pauses: list[str] = field(default_factory=list)
    emphasis: list[str] = field(default_factory=list)
    expression: list[str] = field(default_factory=list)
    intent: str | None = None

    def is_empty(self) -> bool:
        return not (
            self.emotion or self.pace or self.pauses
            or self.emphasis or self.expression or self.intent
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "emotion": [e.to_dict() for e in self.emotion],
            "pace": self.pace,
            "pauses": self.pauses,
            "emphasis": self.emphasis,
            "expression": self.expression,
            "intent": self.intent,
        }


@dataclass
class Segment:
    index: int
    type: str
    text: str | None
    spoken: bool
    narration: Narration | None = None
    raw: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "type": self.type,
            "text": self.text,
            "spoken": self.spoken,
            "narration": self.narration.to_dict() if self.narration else None,
            "raw": self.raw,
        }


@dataclass
class Turn:
    """一轮完整回复 —— 交给下游的唯一契约。"""

    segments: list[Segment]
    character_id: str = "elise"
    scene_id: str = "cafe"
    stage: str = "first_meet"
    turn_id: str = ""
    model: str = ""
    parse_status: str = "ok"            # ok | repaired | degraded
    warnings: list[str] = field(default_factory=list)

    # ---- 下游便捷视图 ----

    @property
    def dialogue(self) -> str:
        """纯台词拼接。TTS 只读这个字段。"""
        parts = [s.text for s in self.segments if s.spoken and s.text]
        return "".join(parts)

    @property
    def narration(self) -> dict[str, Any]:
        """整轮旁白汇总：取第一个非空旁白作为主基调。"""
        for s in self.segments:
            if s.narration and not s.narration.is_empty():
                return s.narration.to_dict()
        return Narration().to_dict()

    @property
    def subtitle(self) -> list[dict[str, Any]]:
        """给前端渲染用：所有段落（含不朗读的）。"""
        return [
            {"type": s.type, "text": s.text, "spoken": s.spoken}
            for s in self.segments if s.text
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL_VERSION,
            "turn_id": self.turn_id,
            "character_id": self.character_id,
            "scene_id": self.scene_id,
            "stage": self.stage,
            "segments": [s.to_dict() for s in self.segments],
            "dialogue": self.dialogue,
            "narration": self.narration,
            "subtitle": self.subtitle,
            "meta": {
                "created_at": _now_iso(),
                "model": self.model,
                "parse_status": self.parse_status,
                "warnings": self.warnings,
            },
        }


# ---------------------------------------------------------------- 解析

def parse(
    raw_text: str,
    *,
    character_id: str = "elise",
    scene_id: str = "cafe",
    stage: str = "first_meet",
    model: str = "",
    allowed_expressions: list[str] | None = None,
    default_expression: str | None = None,
    local_expressions: list[str] | None = None,
    max_chars: int | None = None,
    banned_words: list[str] | None = None,
) -> Turn:
    """把模型输出解析为 Turn。

    local_expressions 是场景专属动作（如咖啡店的 wipe_cup）。
    它们不在全局表情表里，但对本场景是合法的，必须单独传进来。
    本函数**不抛异常**。任何异常格式都会降级并记录 warning。
    """
    warnings: list[str] = []
    status = "ok"
    allowed = set(allowed_expressions) if allowed_expressions else None
    localset = set(local_expressions) if local_expressions else set()
    banned = banned_words or []

    blocks = _split_blocks(raw_text, warnings)
    segments: list[Segment] = []

    for i, block in enumerate(blocks):
        seg = _parse_block(
            block, i,
            warnings=warnings,
            allowed_expressions=allowed,
            default_expression=default_expression,
            local_expressions=localset,
        )
        if seg is not None:
            segments.append(seg)

    # ---- 校验 V1：至少一段可朗读 ----
    if not any(s.spoken and s.text for s in segments):
        status = "degraded"
        warnings.append("V1:no_spoken_segment")

    # ---- 校验 V7：段数 [2,6] ----
    if len(segments) > 6:
        segments = segments[:6]
        warnings.append("V7:truncated_to_6")

    # ---- 校验 V2 / V8 / V9 ----
    cap = 1.0  # 情绪强度不由亲密度决定。
    for seg in segments:
        if seg.narration is None:
            continue
        _check_emphasis(seg, warnings)
        _cap_intensity(seg, cap, warnings)

    _check_banned(segments, banned, warnings)
    if any(w.endswith(":banned_word") for w in warnings):
        status = "degraded"

    if warnings and status == "ok":
        status = "repaired"

    # 重新编号（截断后可能不连续）
    for i, seg in enumerate(segments):
        seg.index = i

    return Turn(
        segments=segments,
        character_id=character_id,
        scene_id=scene_id,
        stage=stage,
        turn_id=f"t_{uuid.uuid4().hex[:12]}",
        model=model,
        parse_status=status,
        warnings=warnings,
    )


def _split_blocks(raw: str, warnings: list[str]) -> list[str]:
    text = (raw or "").strip()
    if not text:
        warnings.append("empty_output")
        return []

    # 去掉模型可能包的 markdown 代码块
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    if SEG_SEPARATOR in text:
        blocks = [b.strip() for b in text.split(SEG_SEPARATOR)]
    else:
        # 没有分隔符：按空行切；仍只有一段则整段作为单块
        parts = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
        blocks = parts if parts else [text]
        if len(blocks) > 1:
            warnings.append("missing_separator")

    return [b for b in blocks if b]


def _parse_block(
    block: str,
    index: int,
    *,
    warnings: list[str],
    allowed_expressions: set[str] | None,
    default_expression: str | None,
    local_expressions: set[str] | None = None,
) -> Segment | None:
    lines = block.split("\n")
    narration = Narration()
    seg_type: str | None = None
    header_found = False

    if lines and _HEADER_RE.match(lines[0].strip()):
        header_found = True
        header = _HEADER_RE.match(lines[0].strip()).group(1)  # type: ignore[union-attr]
        body_lines = lines[1:]
        kv = dict(_KV_RE.findall(header))

        if "type" in kv:
            t = kv["type"].lower()
            if t in SEGMENT_TYPES:
                seg_type = t
            else:
                warnings.append(f"unknown_type:{t}")
        else:
            warnings.append("header_missing_type")

        _apply_kv(kv, narration, warnings, allowed_expressions,
                  default_expression, local_expressions or set())
    else:
        body_lines = lines
        warnings.append("missing_header")

    body = "\n".join(body_lines).strip()

    # 剥离括号内容 → action / scene
    actions = _PAREN_RE.findall(body)
    clean_text = _PAREN_RE.sub("", body).strip()
    clean_text = re.sub(r"\s{2,}", " ", clean_text).strip()

    if seg_type is None:
        seg_type = DEFAULT_SEGMENT_TYPE

    # 只有括号没有台词 → 段落实际是动作/场景
    if not clean_text and actions:
        seg_type = "action" if seg_type in ("dialogue", "dialogue_with_narration") else seg_type

    spoken = SEGMENT_TYPES.get(seg_type, True)
    text = clean_text if clean_text else (actions[0] if actions and not spoken else None)

    if not header_found and not actions and not narration.is_empty():
        pass  # 无头部但有旁白（少见），照常处理

    return Segment(
        index=index,
        type=seg_type,
        text=text or None,
        spoken=spoken and bool(text),
        narration=narration if not narration.is_empty() else None,
    )


def _apply_kv(
    kv: dict[str, str],
    narration: Narration,
    warnings: list[str],
    allowed_expressions: set[str] | None,
    default_expression: str | None,
    local_expressions: set[str] | None = None,
) -> None:
    local_expressions = local_expressions or set()
    # emotion=concern:0.5,joy:0.3
    if "emotion" in kv:
        for item in kv["emotion"].split(","):
            item = item.strip()
            if not item:
                continue
            m = _EMOTION_RE.match(item)
            if not m:
                warnings.append(f"bad_emotion:{item}")
                continue
            name, intens = m.group(1).lower(), m.group(2)
            if name not in EMOTIONS:
                warnings.append(f"V3:unknown_emotion:{name}")
                name = "neutral"
            val = float(intens) if intens else 0.5
            narration.emotion.append(Emotion(name=name, intensity=max(0.0, min(1.0, val))))
        narration.emotion = narration.emotion[:2]   # 最多主+次两个

    if "pace" in kv:
        p = kv["pace"].lower()
        if p in PACES:
            narration.pace = p
        else:
            warnings.append(f"unknown_pace:{p}")

    if "pause" in kv or "pauses" in kv:
        raw_p = kv.get("pauses") or kv.get("pause", "")
        for p in raw_p.split(","):
            p = p.strip().lower()
            if p in PAUSES:
                if p not in narration.pauses:
                    narration.pauses.append(p)
            elif p:
                warnings.append(f"unknown_pause:{p}")

    if "emphasis" in kv:
        for e in kv["emphasis"].split(","):
            e = e.strip()
            if e and e not in narration.emphasis:
                narration.emphasis.append(e)

    if "expr" in kv or "expression" in kv:
        raw_e = kv.get("expr") or kv.get("expression", "")
        for e in raw_e.split(","):
            e = e.strip().lower()
            if not e:
                continue
            # 场景专属动作也是合法值。校验的是「在所有已知表情里认不认识它」，
            # 至于它属不属于当前场景，由白名单那一层判断。
            if e not in EXPRESSIONS and e not in local_expressions:
                warnings.append(f"unknown_expression:{e}")
                continue
            # 场景专属动作（exclusive）也算当前场景合法，例如咖啡店的 wipe_cup。
            # 它不是全局表情，但确实是这一场能做的动作。
            if allowed_expressions is not None and e not in allowed_expressions and e not in local_expressions:
                warnings.append(f"V5:expression_not_in_scene:{e}")
                if default_expression:
                    if default_expression not in narration.expression:
                        narration.expression.append(default_expression)
                continue
            if e not in narration.expression:
                narration.expression.append(e)

    if "intent" in kv:
        it = kv["intent"].lower()
        if it in INTENTS:
            narration.intent = it
        else:
            warnings.append(f"unknown_intent:{it}")


def _check_emphasis(seg: Segment, warnings: list[str]) -> None:
    assert seg.narration is not None
    if not seg.narration.emphasis:
        return
    haystack = seg.text or ""
    kept = []
    for e in seg.narration.emphasis:
        if e and e in haystack:
            kept.append(e)
        else:
            warnings.append(f"V2:emphasis_not_in_text:{e}")
    seg.narration.emphasis = kept


def _cap_intensity(seg: Segment, cap: float, warnings: list[str]) -> None:
    assert seg.narration is not None
    for emo in seg.narration.emotion:
        if emo.intensity > cap:
            warnings.append(f"V8:intensity_capped:{emo.name}:{emo.intensity}->{cap}")
            emo.intensity = cap


def _check_banned(segments: list[Segment], banned: list[str], warnings: list[str]) -> None:
    if not banned:
        return
    joined = "".join(s.text or "" for s in segments if s.spoken)
    for w in banned:
        if w and w in joined:
            warnings.append(f"V10:banned_word:{w}")


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":  # pragma: no cover
    sample = """@seg type=dialogue emotion=concern:0.5 pace=slow expr=slight_frown,lean_forward intent=comfort emphasis=不太对劲
……啊，你今天听起来不太对劲。怎么了？
[sep]
@seg type=dialogue emotion=concern:0.4 pace=normal expr=look_down
（低头搅了搅杯子）……要是想说，我在。
[sep]
@seg type=inner_thought
（其实我有点怕他说完就走了。）"""

    turn = parse(
        sample,
        allowed_expressions=["smile", "slight_frown", "look_down", "lean_forward"],
        default_expression="smile",
    )
    import json
    print(json.dumps(turn.to_dict(), ensure_ascii=False, indent=2))
