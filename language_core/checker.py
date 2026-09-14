"""校验器（checker）。

模型每次输出的遵循度是有方差的，稳定性不能只押在提示词上。
规则能判断的绝不交给模型：免费、瞬时、100% 准。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .persona import STYLE_ANCHOR, Character, Scene, Stage
from .segment import Segment, Turn

# ---------------------------------------------------------------- 词表

_AI_TONE = (
    "作为一个AI", "作为AI", "我是人工智能助手", "我是AI助手",
    "亲爱的用户", "您好，请问有什么可以帮您", "根据我的训练数据",
    "希望我的回答对您有帮助", "还有什么可以帮到您",
)

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\u2b00-\u2bff]"
)

_SENTENCE_END = re.compile(r"[。！？!?…~]+")

_COMMON_WORDS = (
    "你", "我", "的", "了", "是", "在", "不", "有", "就", "都", "也",
    "这", "那", "什么", "怎么", "一", "个", "好", "没", "会", "要",
)


@dataclass
class CheckItem:
    rule: str
    ok: bool
    hard: bool = False
    detail: str = ""


@dataclass
class CheckResult:
    items: list[CheckItem] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def hard_fail(self) -> bool:
        return any(i.hard and not i.ok for i in self.items)

    @property
    def passed(self) -> bool:
        return all(i.ok for i in self.items)

    def add(self, rule: str, ok: bool, hard: bool = False, detail: str = "") -> None:
        self.items.append(CheckItem(rule, ok, hard, detail))

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "hard_fail": self.hard_fail,
            "failed": [f"{i.rule}:{i.detail}" for i in self.items if not i.ok],
            "warnings": [f"{i.rule}:{i.detail}" for i in self.items if not i.ok and not i.hard],
            "metrics": self.metrics,
        }


# ---------------------------------------------------------------- 风格指纹


def style_fingerprint(char: Character) -> set[str]:
    """从角色卡提炼风格指纹，用于跨轮/跨场景的风格漂移度量。"""
    tokens: set[str] = set()
    raw = char.raw

    for phrase in raw.get("catchphrases", []):
        text = phrase.get("text", "")
        for ch in re.findall(r"[\u4e00-\u9fff]", text):
            tokens.add(ch)

    style = raw.get("speech_style", {})
    for ch in re.findall(r"[\u4e00-\u9fff]", style.get("register", "")):
        tokens.add(ch)

    tokens.update(_COMMON_WORDS)
    return tokens


def style_similarity(text: str, fingerprint: set[str]) -> float:
    """文本与风格指纹的重合度，0 到 1。

    这是一期可用的粗粒度指标，不依赖外部模型，适合做回归测试的漂移警报。
    它测的是「说话像不像这个人」的一个侧面，不能替代人工评审。
    """
    chars = re.findall(r"[\u4e00-\u9fff]", text or "")
    if not chars:
        return 1.0
    hits = sum(1 for c in chars if c in fingerprint)
    return round(hits / len(chars), 3)


# ---------------------------------------------------------------- 主校验


def check_turn(turn: Turn, *, scene: Scene, stage: Stage,
               char: Character | None = None,
               baseline_similarity: float | None = None,
               user_message: str = "",
               speak_policy: str = "must") -> CheckResult:
    res = CheckResult()

    spoken = [s for s in turn.segments if s.spoken and s.text]
    all_text = "".join(s.text or "" for s in turn.segments if s.text)
    dialogue = turn.dialogue
    res.add('nonempty_turn', any((s.text or '').strip() for s in turn.segments), hard=True,
            detail='' if turn.segments else '模型没有生成内容')

    # V1 至少一段可朗读。
    # 例外：编译器判定这一轮「不必多说」或「允许沉默」时，
    # 只有动作没有台词是正确行为，不是失败。
    # 只有 must（必须回应）和 close（收尾，至少要说一句）才硬性要求台词。
    if speak_policy in ("may_silent", "brief"):
        res.add("V1_has_spoken", True, hard=True,
                detail="本轮允许不说台词" if not spoken else "")
    else:
        res.add("V1_has_spoken", bool(spoken), hard=True,
                detail="" if spoken else "整轮没有可朗读的台词")

    metadata_ok = all(s.narration and s.narration.emotion and s.narration.pace and s.narration.expression for s in spoken)
    res.add('speech_metadata', bool(metadata_ok), hard=True,
            detail='' if metadata_ok else '台词缺少情绪、语速或表情')

    # V6 台词中不残留格式标记 —— 硬失败
    residue = any(mark in dialogue for mark in ("@seg", "[sep]"))
    res.add("V6_no_format_residue", not residue, hard=True,
            detail="" if not residue else "台词里残留了 @seg 或 [sep]")

    # V10 禁用词 —— 硬失败
    banned_hits = [w for w in STYLE_ANCHOR["banned_words"] if w in all_text]
    banned_hits += [w for w in _AI_TONE if w in all_text]
    res.add("V10_no_banned_words", not banned_hits, hard=False,
            detail="、".join(sorted(set(banned_hits))))

    # V7 段数：默认 1 段，上限按风格锚点
    n = len(turn.segments)
    min_seg = STYLE_ANCHOR["min_segments"]
    max_seg = STYLE_ANCHOR["max_segments"]
    res.add("V7_segment_count", min_seg <= n <= max_seg, detail=f"{n} 段")

    # V9 台词长度
    total_chars = len(dialogue)
    res.add("V9_total_chars", total_chars <= STYLE_ANCHOR["max_chars_total"],
            detail=f"{total_chars}/{STYLE_ANCHOR['max_chars_total']} 字")

    # 单段长度
    long_segs = [s.index for s in spoken if len(s.text or "") > STYLE_ANCHOR["max_chars_per_segment"]]
    res.add("V9_segment_chars", not long_segs,
            detail=f"第 {long_segs} 段超过 {STYLE_ANCHOR['max_chars_per_segment']} 字" if long_segs else "")

    # 不使用 emoji
    emojis = _EMOJI_RE.findall(all_text)
    res.add("no_emoji", not emojis, detail="".join(emojis))

    # V5 表情必须在场景白名单内
    allowed = set(scene.allowed_expressions()) | set(scene.exclusive_expressions())
    bad_expr: list[str] = []
    for s in turn.segments:
        if s.narration:
            for e in s.narration.expression:
                if e not in allowed:
                    bad_expr.append(e)
    res.add("V5_expression_in_scene", not bad_expr, detail="、".join(bad_expr))

    # V8 情绪强度不超过当前阶段上限
    over = []
    for s in turn.segments:
        if s.narration:
            for e in s.narration.emotion:
                if not 0 <= e.intensity <= 1:
                    over.append(f"{e.name}:{e.intensity}")
    res.add("V8_intensity_cap", not over, detail="、".join(over))

    # 台词不夹带括号描述
    paren = re.findall(r"[（(][^（()）]*[)）]", dialogue)
    res.add("no_paren_in_dialogue", not paren, detail="".join(paren))

    # ---- 反填充检查 ----
    # 这些是「没话找话」的可判定形式。判不了的不要写进来。
    anti = STYLE_ANCHOR.get("anti_filler", {})

    filler_hits = [p for p in anti.get("banned_patterns", []) if p in all_text]
    res.add("A1_no_filler_pattern", not filler_hits, detail="、".join(filler_hits))

    # 整轮都在反问。
    # 这不是「出现问号就违规」——点单确认、问候本来就要问。
    # 真正的问题是两种：用户提问她不答反问；用户只是陈述，她用提问来填话。
    if spoken:
        sentences_with_ends = re.findall(r'[^。！？!?]+[。！？!?]?', dialogue)
        all_questions = bool(sentences_with_ends) and all(re.search(r'[？?]$', x.strip()) for x in sentences_with_ends)
        user_asked = bool(re.search(r"[？?]\s*$", (user_message or "").strip()))
        bad = False
        detail = ""
        if all_questions and user_asked:
            bad = True
            detail = "用户提问，她却整轮反问，没有回答"
        elif all_questions and speak_policy in ("close", "may_silent"):
            bad = True
            detail = f"policy={speak_policy} 时仍整轮反问"
        res.add("A2_not_all_questions", not bad, detail=detail)

    # 复述：她的台词是否把用户上一句原样搬过来
    if user_message:
        core = re.sub(r"[\s。！？!?~～,，.]+", "", user_message)
        if len(core) >= 6:
            echoed = core in re.sub(r"[\s。！？!?~～,，.]+", "", dialogue)
            res.add("A3_no_echo", not echoed, detail="复述了用户刚说的话" if echoed else "")

    # ---- 指标 ----
    sentences = [x for x in _SENTENCE_END.split(dialogue) if x.strip()]
    avg_len = round(len(dialogue) / len(sentences), 1) if sentences else 0.0

    metrics: dict[str, Any] = {
        "segment_count": n,
        "spoken_count": len(spoken),
        "dialogue_chars": total_chars,
        "sentence_count": len(sentences),
        "avg_sentence_chars": avg_len,
        "expression_count": sum(len(s.narration.expression) for s in turn.segments if s.narration),
        "parse_status": turn.parse_status,
        "warnings": list(turn.warnings),
    }

    if char is not None and baseline_similarity is not None:
        fp = style_fingerprint(char)
        sim = style_similarity(dialogue, fp)
        metrics["style_similarity"] = sim
        if baseline_similarity is not None:
            drift = round(baseline_similarity - sim, 3)
            metrics["style_drift"] = drift
            res.add("style_drift", drift <= 0.15, detail=f"偏离基线 {drift}")

    res.metrics = metrics
    return res


# ---------------------------------------------------------------- 交叉校验


def check_cross_scene(turns_by_scene: dict[str, Turn], *, char: Character,
                      scenes: dict[str, Scene], stages: dict[str, Stage]) -> dict[str, Any]:
    """跨场景人格一致率。

    同一批输入在不同场景下跑，看风格指纹是否稳定。
    这是「她换了个地方还是不是同一个人」的自动化度量。
    """
    sims: dict[str, float] = {}
    for scene_id, turn in turns_by_scene.items():
        scene = scenes[scene_id]
        stage = stages[scene_id]
        r = check_turn(turn, scene=scene, stage=stage, char=char)
        sims[scene_id] = r.metrics.get("style_similarity", 0.0)

    if not sims:
        return {"consistency": 0.0, "per_scene": {}}

    values = list(sims.values())
    mean = sum(values) / len(values)
    spread = max(values) - min(values)

    # 一致率：均值高且离散度低
    consistency = round(max(0.0, mean - spread), 3)
    return {
        "consistency": consistency,
        "mean_similarity": round(mean, 3),
        "spread": round(spread, 3),
        "per_scene": sims,
    }
