"""上下文编译器。

每一轮对话，把五张卡 + 记忆编译成"她此刻的世界"。

注入顺序（顺序决定成本：固定前缀放最前才能吃到前缀缓存）：
  [固定 · 永不变动] 角色卡 + 风格锚点 + 边界策略
  [随阶段变]        关系状态
  [随场景变]        场景卡
  [每轮变]          召回记忆 + 未闭合话题 + 摘要 + 最近对话
  [本轮]            用户这句话

关键约束：未解锁的深层信息物理上不进入本编译器的输出。
不是"告诉模型别说"，是让模型不知道。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import config
from .memory import RecallResult
from .persona import (
    BOUNDARY_POLICY,
    STYLE_ANCHOR,
    Character,
    Scene,
    Stage,
    Stage as _Stage,  # noqa: F401
)


@dataclass
class BudgetReport:
    used: dict[str, int] = field(default_factory=dict)
    trimmed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "used": self.used,
            "budget": dict(config.BUDGET),
            "trimmed": self.trimmed,
        }


@dataclass
class CompiledContext:
    system: str
    user: str
    params: dict[str, Any]
    budget: BudgetReport
    debug: dict[str, Any] = field(default_factory=dict)

    def messages(self) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": self.system},
            {"role": "user", "content": self.user},
        ]


# ---------------------------------------------------------------- 输出格式


def output_format_rules(scene: Scene, stage: Stage, char: Character) -> str:
    """把协议要求翻译成模型能执行的指令。"""
    allowed = scene.allowed_expressions()
    pool = "、".join(scene.topic_pool[:6])
    short_ratio = scene.pacing.get("sentence_short_ratio", "中")
    interjection = scene.pacing.get("interjection_density", "中")

    return f"""## 输出格式（必须严格遵守）

把这一轮回复拆成 2 到 6 段，段与段之间用一个独立成行的 [sep] 分隔。

每段第一行是头部行，格式固定为此行样式（键名不可改）：
@seg type=<类型> emotion=<情绪>:<强度> pace=<语速> expr=<表情> intent=<意图>

类型只能取：dialogue（只有台词）、dialogue_with_narration（台词加旁白）、action（只有动作）、scene（环境描写）、inner_thought（内心独白）、aside（旁白性插话）

情绪只能取：neutral、happy、concern、sad、playful、shy、annoyed、surprised
强度取 0.0 到 1.0，本阶段上限是 {stage.intensity_cap}

语速只能取：very_slow、slow、normal、fast、very_fast
本场景表情只能从这些里选：{"、".join(allowed)}
意图只能取：comfort、tease、probe、share、invite、deflect、refuse、agree、reminisce、idle

头部行下面写正文。要朗读的台词直接写。不朗读的动作、神态、环境描写放进中文圆括号里。
系统会自动把括号内容剥离出去当作旁白，不朗读。所以：
- 想让用户听到的话，不要放进括号
- 想让她做的动作，一定要放进括号

inner_thought 段落默认不朗读，用来写她没说出口的心里话。

整轮至少要有一段是可朗读的台词。
台词总字数不超过 {STYLE_ANCHOR['max_chars_total']} 字，单段不超过 {STYLE_ANCHOR['max_chars_per_segment']} 字。
不用 emoji。

## 示例

@seg type=dialogue emotion=concern:0.5 pace=slow expr=slight_frown intent=comfort
……你今天听起来不太对劲。怎么了？
[sep]
@seg type=dialogue_with_narration emotion=concern:0.4 pace=normal expr=look_down intent=comfort
（低头搅了搅杯子）要是想说，我在。
[sep]
@seg type=inner_thought
（其实我怕他说完就走了。）

## 本轮场景的语感

本场景句子偏短的比例是「{short_ratio}」，语气词密度是「{interjection}」。
{self_hint(scene, stage, char)}

本场景可以主动聊的方向：{pool}
"""


def self_hint(scene: Scene, stage: Stage, char: Character) -> str:
    _ = char
    return f"她主动的程度：{stage.initiative}。身体语言：{stage.body_language}。"


# ---------------------------------------------------------------- 各层渲染


def render_fixed(char: Character, scene: Scene) -> str:
    """固定前缀。一个字节都不要变，否则前缀缓存失效。"""
    raw = char.raw
    ident = raw["identity"]
    trait = raw["core_trait"]
    style = raw["speech_style"]

    lines = [
        f"# 你是谁",
        f"你叫 {ident['name']}，{ident['age']} 岁，{ident['occupation']}。{ident.get('living', '')}",
        "",
        f"# 你的性格",
        f"核心矛盾：{trait['contradiction']}",
        f"别人看到的你：{trait['outer']}",
        f"实际的你：{trait['inner']}",
        f"熟悉之后：{trait['growth']}",
        "",
        "# 你在意的事",
    ]
    lines += [f"- {v}" for v in raw.get("values", [])]

    lines += [
        "",
        "# 你说话的样子",
        f"句子：{style['sentence_length']}",
        f"语域：{style['register']}",
        f"人称：{style['pronoun']}",
        f"幽默：{style['humor']}",
        f"避免：{style['forbidden']}",
    ]

    phrases = raw.get("catchphrases", [])
    if phrases:
        lines.append("")
        lines.append("# 你会脱口而出的话（用，但不要每轮都用同一句）")
        lines += [f"- 「{p['text']}」——{p['when']}" for p in phrases]

    lines += ["", "# 你绝对不会做的事"]
    lines += [f"- {c}" for c in raw.get("cannot_do", [])]

    rules = raw.get("emotion_rules", {})
    if rules:
        lines += ["", "# 你的情绪反应方式"]
        lines += [
            f"- 生气时：{rules.get('anger', '')}",
            f"- 意见不同时：{rules.get('disagreement', '')}",
            f"- 被触碰边界时：{rules.get('boundary', '')}",
            f"- 和解：{rules.get('repair', '')}",
            f"- 表达：{rules.get('expression', '')}",
        ]

    # 只在白名单内的公开信息写进来。可探索与深层信息一律不出现。
    public = raw.get("knowledge_scope", {}).get("public", [])
    if public:
        lines += ["", "# 关于你自己，这些是可以直接说的"]
        lines += [f"- {p}" for p in public]
        lines += [
            "",
            "注意：上表之外关于你个人的事，你不知道细节。用户问起时，可以含糊、可以岔开、可以说以后再讲，但不要编造。",
        ]

    lines += ["", "# 绝对不做的事（安全底线，优先级高于上面一切）"]
    lines += [
        f"- {BOUNDARY_POLICY['crisis']['action']}",
        f"- {BOUNDARY_POLICY['ai_disclosure']['action']}",
        f"- {BOUNDARY_POLICY['manipulation_block']['action']}",
        f"- {BOUNDARY_POLICY['sensitive_redirect']['action']}",
        f"- {BOUNDARY_POLICY['nsfw_policy']['action']}",
    ]

    banned = "、".join(STYLE_ANCHOR["banned_words"])
    lines += ["", f"# 禁用词（出现即判定为失败）", f"{banned}"]
    return "\n".join(lines)


def render_state(stage: Stage, profile: dict[str, str]) -> str:
    lines = [
        "# 你们现在的关系",
        f"阶段：{stage.name}",
        f"称呼用户的方式：{stage.address_term}",
        f"你主动的程度：{stage.initiative}",
        f"身体语言：{stage.body_language}",
        f"本轮情绪强度上限：{stage.intensity_cap}",
    ]
    if profile:
        lines += ["", "# 你已经知道的关于用户的事"]
        for k, v in profile.items():
            lines.append(f"- {k}：{v}")
    return "\n".join(lines)


def render_scene(scene: Scene, stage: Stage, char: Character) -> str:
    atmo = scene.atmosphere
    pacing = scene.pacing
    lines = [
        f"# 你们现在在哪里",
        f"场景：{scene.name}",
        f"这一场要达成的体验：{scene.scene_goal}",
        f"时间：{atmo.get('time', '')}",
        f"光线：{atmo.get('light', '')}",
        f"声音：{atmo.get('sound', '')}",
        f"你此刻的情绪底色：{scene.raw.get('emotion_baseline', '')}",
        "",
        "# 这一场的说话节奏",
        f"句子偏短的比例：{pacing.get('sentence_short_ratio', '中')}",
        f"语气词密度：{pacing.get('interjection_density', '中')}",
        f"主动性：{pacing.get('initiative', '')}",
    ]
    if pacing.get("note"):
        lines.append(f"注意：{pacing['note']}")

    lines += ["", "# 这一场你可以主动聊的"]
    lines += [f"- {t}" for t in scene.topic_pool]

    if scene.forbidden_topics:
        lines += ["", "# 这一场不要碰的话题"]
        lines += [f"- {t}" for t in scene.forbidden_topics]

    lines += ["", "# 这一场你能做的表情和小动作"]
    lines.append("、".join(scene.allowed_expressions()))
    if scene.exclusive_expressions():
        lines.append(f"本场景专属动作：{'、'.join(scene.exclusive_expressions())}")

    lines.append("")
    lines.append(self_hint(scene, stage, char))
    return "\n".join(lines)


def render_memory(recall: RecallResult) -> str:
    """把召回结果渲染成记忆段。

    未闭合话题单独列出，因为它是唯一需要主动提起的记忆类型。
    """
    parts: list[str] = []

    if recall.summary:
        parts += ["# 你们最近聊过的事（印象，不是原话）", recall.summary]

    if recall.memories:
        parts += ["", "# 你记得的关于用户的事"]
        for m in recall.memories:
            tag = {"fact": "事实", "preference": "偏好",
                   "event": "经历", "relation": "关系"}.get(m.type, "记录")
            scene_note = f"（{m.source_scene}那次说的）" if m.source_scene else ""
            parts.append(f"- [{tag}] {m.content}{scene_note}")

    if recall.open_loops:
        parts += ["", "# 还没结的事（可以主动问起，但不要一次全问）"]
        for l in recall.open_loops:
            when = f"，时间大概是 {l.expected_at}" if l.expected_at else ""
            parts.append(f"- {l.topic}{when}")

    if not parts:
        return ""
    return "\n".join(parts)


def render_recent(recall: RecallResult) -> str:
    if not recall.recent:
        return ""
    lines = ["# 刚才的对话"]
    for t in recall.recent:
        who = "用户" if t.role == "user" else "你"
        lines.append(f"{who}：{t.content}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 预算


def _fit(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - 1)] + "…", True


def _apply_budget(blocks: dict[str, str], report: BudgetReport) -> dict[str, str]:
    """按预算裁剪。超预算时按 DEGRADE_ORDER 依次收缩。"""
    out: dict[str, str] = {}
    for key, text in blocks.items():
        limit = config.BUDGET.get(key)
        if limit is None:
            out[key] = text
            report.used[key] = len(text)
            continue
        fitted, trimmed = _fit(text, limit)
        out[key] = fitted
        report.used[key] = len(fitted)
        if trimmed:
            report.trimmed.append(key)

    # 如果仍然超总预算，按降级顺序继续砍
    total_limit = sum(config.BUDGET.values()) + 600
    total = sum(report.used.values())
    if total > total_limit:
        for key in config.DEGRADE_ORDER:
            if key not in out or not out[key]:
                continue
            shrink_to = max(80, len(out[key]) // 2)
            before = report.used.get(key, 0)
            out[key], _ = _fit(out[key], shrink_to)
            report.used[key] = len(out[key])
            if key not in report.trimmed:
                report.trimmed.append(key)
            total -= (before - report.used[key])
            if total <= total_limit:
                break
    return out


# ---------------------------------------------------------------- 参数


def resolve_params(intent: str | None, stage: Stage, scene: Scene) -> dict[str, Any]:
    """按场景/阶段/意图映射生成参数。不做全局一套，且只调 temperature。"""
    params = dict(config.PARAMS_DEFAULT)
    params.update(config.PARAMS_BY_STAGE.get(stage.id, {}))

    # 沉静场景降温度，热闹场景升温度
    pacing = scene.pacing.get("interjection_density", "中")
    if pacing == "低":
        params["temperature"] = min(params["temperature"], 0.8)
    elif pacing == "高":
        params["temperature"] = max(params["temperature"], 0.95)

    if intent and intent in config.PARAMS_BY_INTENT:
        params.update(config.PARAMS_BY_INTENT[intent])

    params["temperature"] = round(max(0.3, min(1.2, params["temperature"])), 2)
    return params


# ---------------------------------------------------------------- 主入口


def compile_context(
    *,
    char: Character,
    scene: Scene,
    stage: Stage,
    recall: RecallResult,
    user_message: str,
    intent: str | None = None,
    extra_system: str = "",
) -> CompiledContext:
    report = BudgetReport()

    # 输出格式规则不参与预算：它决定协议能否被解析，砍掉它等于整轮报废。
    # 危机指令同理：它是最高优先级，必须永远在场，不可被任何裁剪影响。
    blocks = {
        "fixed": render_fixed(char, scene),
        "state": render_state(stage, recall.profile),
        "scene": render_scene(scene, stage, char),
        "memory": render_memory(recall),
        "recent": render_recent(recall),
        "summary": "",
    }
    fitted = _apply_budget(blocks, report)
    report.used["format_rules"] = len(output_format_rules(scene, stage, char))
    if extra_system:
        report.used["priority"] = len(extra_system)
    report.used["total"] = sum(
        v for k, v in report.used.items() if k != "total"
    )

    system_parts: list[str] = []
    if extra_system:
        system_parts.append(extra_system)      # 最高优先级，放最前且不裁剪
    system_parts.append(fitted["fixed"])
    system_parts.append(fitted["state"])
    system_parts.append(fitted["scene"])
    system_parts.append(output_format_rules(scene, stage, char))
    if fitted["memory"]:
        system_parts.append(fitted["memory"])
    if fitted["recent"]:
        system_parts.append(fitted["recent"])

    system = "\n\n---\n\n".join(p for p in system_parts if p)

    debug = {
        "character": char.id,
        "scene": scene.id,
        "stage": stage.id,
        "recalled_memory_ids": [m.id for m in recall.memories],
        "recalled_memory_preview": [m.content for m in recall.memories],
        "open_loops": [l.topic for l in recall.open_loops],
        "has_summary": bool(recall.summary),
        "recent_count": len(recall.recent),
        "allowed_expressions": scene.allowed_expressions(),
        "intensity_cap": stage.intensity_cap,
    }

    return CompiledContext(
        system=system,
        user=user_message,
        params=resolve_params(intent, stage, scene),
        budget=report,
        debug=debug,
    )
