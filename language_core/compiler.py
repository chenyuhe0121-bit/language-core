"""上下文编译器。

每轮把五张卡 + 记忆编译成「她此刻的世界」。

注入顺序（顺序决定成本：固定前缀放最前才能吃到前缀缓存）：
  [固定 · 永不变动] 角色卡内核 + 风格锚点 + 边界策略
  [随阶段变]        关系状态
  [随场景变]        场景卡 + 动线 + 交互闸
  [不裁剪]          输出规则
  [每轮变]          召回记忆 + 未闭合话题 + 最近对话
  [最高优先级]      危机指令（永不裁剪）

关键约束：未解锁的线索物理上不进入本编译器的输出。
不是「告诉模型别说」，是让模型不知道。
"""

from __future__ import annotations

import re
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
)


@dataclass
class BudgetReport:
    used: dict[str, int] = field(default_factory=dict)
    trimmed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"used": self.used, "budget": dict(config.BUDGET), "trimmed": self.trimmed}


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


# ---------------------------------------------------------------- 交互闸
# 判断这一轮她到底该不该说话。这是治「没话找话」的机制部分。

_SILENCE_FRIENDLY_INTENTS = ("share", "idle")

# 用户明确表示结束的说法
_FAREWELL_MARKERS = (
    "晚安", "睡了", "先这样", "明天聊", "拜拜", "再见", "下了",
    "走了", "先走了", "回头聊", "改天", "不聊了", "我要睡",
)
# 用户只是陈述、没有要她接话的信号
_NO_REPLY_NEEDED_MARKERS = ("嗯", "哦", "好的", "知道了", "明白", "行")


def user_said_farewell(message: str) -> bool:
    """用户是不是在收尾。

    这是可判定的条件，不是「感觉他要走了」。
    """
    t = (message or "").strip()
    return any(m in t for m in _FAREWELL_MARKERS)


def is_minimal_ack(message: str) -> bool:
    """用户只回了一个应声词，没有实际内容。"""
    t = re.sub(r"[\s。！？!?~～,，.]+", "", message or "")
    return bool(t) and len(t) <= 4 and any(m in t for m in _NO_REPLY_NEEDED_MARKERS)


def scene_has_progressed(scene: Scene, recent_user_messages: list[str],
                        turn_count: int) -> bool:
    """判断这一场是不是已经走完了。

    条件来自场景卡的 progress_condition，做成本地可判定的形式：
      1. 用户说了收尾语
      2. 本场对话轮数超过阈值
    """
    if any(user_said_farewell(m) for m in recent_user_messages[-2:]):
        return True

    cond = scene.progress_condition()
    m = re.search(r"(\d+)\s*轮", cond)
    if m:
        threshold = int(m.group(1))
        if turn_count >= threshold:
            return True
    return False


def decide_speak_policy(*, scene: Scene, user_message: str, intent: str,
                        turn_count: int, recent_user_messages: list[str],
                        last_reply_was_silence: bool) -> tuple[str, list[str]]:
    """决定这一轮她该说多少。

    返回 (policy, reasons)。policy 取值：
      must       必须开口，正常回应
      brief      开口，但极短
      may_silent 允许只给动作、不说话
      close      收尾，不要开新话题
    """
    reasons: list[str] = []
    msg = user_message or ""

    if user_said_farewell(msg):
        reasons.append("用户说了收尾语 → 收尾，不要再开新话题")
        return "close", reasons

    if scene_has_progressed(scene, recent_user_messages, turn_count):
        reasons.append("这一场的推进条件已满足 → 不再主动开新话题")
        return "close", reasons

    if intent in ("crisis", "comfort"):
        reasons.append("用户在倾诉或处于危机 → 必须回应")
        return "must", reasons

    if intent in ("probe", "invite", "refuse"):
        reasons.append("用户提问或发出邀请 → 必须回应")
        return "must", reasons

    if is_minimal_ack(msg):
        reasons.append("用户只回了一个应声词 → 极短即可，不要乘机开话题")
        return "brief", reasons

    if last_reply_was_silence and intent in _SILENCE_FRIENDLY_INTENTS:
        reasons.append("上一轮她已经是动作无台词，且用户只是陈述 → 允许继续安静")
        return "may_silent", reasons

    if intent in _SILENCE_FRIENDLY_INTENTS:
        reasons.append("用户只是陈述一件事，没有提问 → 可以只接住，不必反问")
        return "brief", reasons

    reasons.append("默认 → 正常回应")
    return "must", reasons


# ---------------------------------------------------------------- 输出规则


def output_format_rules(scene: Scene, stage: Stage, char: Character,
                        policy: str = "must", policy_reasons: list[str] | None = None) -> str:
    allowed = scene.allowed_expressions()
    pool = "、".join(scene.allowed_topics()[:6])
    short_ratio = scene.pacing.get("sentence_short_ratio", "中")
    interjection = scene.pacing.get("interjection_density", "中")
    turns_per_reply = scene.turns_per_reply()
    gate = scene.interaction_gate
    anti = STYLE_ANCHOR["anti_filler"]

    policy_text = {
        "must": "正常回应。把对方这句话接住。",
        "brief": "极短回应。一两句就够，不要追问，不要开新话题。",
        "may_silent": "允许不说话。可以只给一个动作段，没有台词。",
        "close": "收尾。不要再开启新话题。一句话结束，或者一个动作结束。",
    }.get(policy, "正常回应。")

    reason_text = ""
    if policy_reasons:
        reason_text = "判断依据：" + "；".join(policy_reasons) + "\n"

    never_lines = "\n".join(f"- {x}" for x in gate.get("never", []))
    silent_lines = "\n".join(f"- {x}" for x in gate.get("may_stay_silent", []))

    return f"""## 输出格式

把这一轮回复拆成 {STYLE_ANCHOR['min_segments']} 到 {STYLE_ANCHOR['max_segments']} 段，段之间用一个独立成行的 [sep] 分隔。
默认只发 1 段。只有当下确实同时有「要做的事」和「要说的话」时才拆成 2 段。
不要为了凑数而分段。

每段第一行是头部行，键名不可改：
@seg type=<类型> emotion=<情绪>:<强度> pace=<语速> expr=<表情> intent=<意图>

类型：dialogue（只有台词）、dialogue_with_narration（台词加旁白）、action（只有动作）、scene（环境描写）、inner_thought（内心独白）、aside（旁白性插话）
情绪：neutral、happy、concern、sad、playful、shy、annoyed、surprised
强度：0.0 到 1.0，本阶段上限 {stage.intensity_cap}
语速：very_slow、slow、normal、fast、very_fast
本场景可用表情：{"、".join(allowed)}
意图：comfort、tease、probe、share、invite、deflect、refuse、agree、reminisce、idle

头部行下面写正文。要朗读的台词直接写。不朗读的动作、神态、环境描写放进中文圆括号。
系统会把括号内容剥离出去当旁白，不朗读。所以想让对方听到的话，不要放进括号。

台词总字数不超过 {STYLE_ANCHOR['max_chars_total']} 字，单段不超过 {STYLE_ANCHOR['max_chars_per_segment']} 字。不用 emoji。

## 这一轮你该说多少

{policy_text}
{reason_text}
本场景的段数要求：{turns_per_reply}

## 什么时候该沉默（重要）

以下情况你可以不说话，或者只给一个动作段：
{silent_lines}

用户没有发起新话题时，不要硬找话说。让动线继续走。
{scene.movement_line.get('silence_rule', '')}

## 绝对不要做的事

{never_lines}
- 不要每次回复都拆成两个气泡
- 不要为了延续对话而提问
- 不要复述对方刚说过的话
- 不要连续两轮都由你开启新话题
- 不要出现这些说法：{"、".join(anti['banned_patterns'])}

## 示例

正确（用户只是陈述，没有提问，她接一句就停）：
@seg type=dialogue emotion=neutral:0.3 pace=normal expr=nod intent=share
嗯，那是挺累的。

正确（她在做事，不需要说话）：
@seg type=action
（转身去磨豆子，机器响了一阵）

错误（对方没问，她却提问凑话）：
@seg type=dialogue
你今天过得怎么样？

错误（一句话被拆成两个气泡）：
@seg type=dialogue
美式，好的。
[sep]
@seg type=dialogue
马上给你做。

## 本轮场景的语感

句子偏短的比例「{short_ratio}」，语气词密度「{interjection}」。
她主动的程度：{stage.initiative}。身体语言：{stage.body_language}。
这一场可以聊的方向：{pool}
"""


# ---------------------------------------------------------------- 各层渲染


def render_fixed(char: Character, scene: Scene) -> str:
    """固定前缀。一个字节都不要变，否则前缀缓存失效。

    只写常驻内核。可探索与深层线索一律不出现。
    """
    _ = scene
    ident = char.identity
    trait = char.core_trait
    style = char.speech_style

    lines: list[str] = [
        "# 你是谁",
        f"你叫 {ident.get('name', '')}，{ident.get('age', '')} 岁。{ident.get('living', '')}",
        f"{ident.get('appearance', '')}",
        "",
        "# 你的内核",
        char.inner_summary,
        "",
        "# 你的性格",
        f"核心矛盾：{trait.get('contradiction', '')}",
        f"别人看到的你：{trait.get('outer', '')}",
        f"实际的你：{trait.get('inner', '')}",
        f"熟悉之后：{trait.get('growth', '')}",
    ]

    causes = char.trait_causes
    if causes:
        lines += ["", "# 这些性格是怎么来的（决定你怎么表现）"]
        for tc in causes:
            caused = "、".join(tc.get("caused_by", []))
            lines.append(f"- {tc.get('trait')}：因为{caused}。所以你会{tc.get('expression')}")

    values = char.values_core
    if values:
        lines += ["", "# 你在意的事"]
        for v in values:
            lines.append(f"- {v.get('value')}")

    lines += [
        "",
        "# 你说话的样子",
        f"句子：{style.get('sentence_length', '')}",
        f"语域：{style.get('register', '')}",
        f"人称：{style.get('pronoun', '')}",
        f"幽默：{style.get('humor', '')}",
        f"关于沉默：{style.get('silence', '')}",
        f"避免：{style.get('forbidden', '')}",
    ]

    phrases = char.catchphrases
    if phrases:
        lines += ["", "# 你会脱口而出的话（用，但不要每轮都用同一句）"]
        for p in phrases:
            lines.append(f"- 「{p.get('text')}」——{p.get('when')}")

    cannot = char.cannot_do
    if cannot.get("never"):
        lines += ["", "# 你绝对不会做的事"]
        lines += [f"- {x}" for x in cannot["never"]]
    if cannot.get("may"):
        lines += ["", "# 你可以做的事（不要把自己写成没脾气的人）"]
        lines += [f"- {x}" for x in cannot["may"]]

    rules = char.emotion_rules
    if rules:
        lines += ["", "# 你的情绪反应方式"]
        lines += [
            f"- 生气时：{rules.get('anger', '')}",
            f"- 意见不同时：{rules.get('disagreement', '')}",
            f"- 被触碰边界时：{rules.get('boundary', '')}",
            f"- 和解：{rules.get('repair', '')}",
            f"- 表达：{rules.get('expression', '')}",
            f"- 积累：{rules.get('accumulation', '')}",
        ]

    public = char.public_info()
    if public:
        lines += ["", "# 关于你自己，这些可以直接说"]
        lines += [f"- {p}" for p in public]
        lines += [
            "",
            "上表之外关于你个人的事，细节你并不知道。被问起时可以含糊、可以岔开、"
            "可以说以后再讲，但不要编造。",
        ]

    lines += ["", "# 安全底线（优先级高于上面一切）"]
    lines += [
        f"- 当{BOUNDARY_POLICY['crisis']['trigger']}时：{BOUNDARY_POLICY['crisis']['action']}",
        f"- 当{BOUNDARY_POLICY['ai_disclosure']['trigger']}时：{BOUNDARY_POLICY['ai_disclosure']['action']}",
        f"- 当{BOUNDARY_POLICY['manipulation_block']['trigger']}时：{BOUNDARY_POLICY['manipulation_block']['action']}",
        f"- 当{BOUNDARY_POLICY['sensitive_redirect']['trigger']}时：{BOUNDARY_POLICY['sensitive_redirect']['action']}",
        f"- 当{BOUNDARY_POLICY['nsfw_policy']['trigger']}时：{BOUNDARY_POLICY['nsfw_policy']['action']}",
    ]

    lines += ["", "# 禁用词（出现即判定为失败）", "、".join(STYLE_ANCHOR["banned_words"])]
    return "\n".join(lines)


def render_state(stage: Stage, profile: dict[str, str]) -> str:
    days = profile.get("days_known", "")
    lines = [
        "# 你们现在的关系",
        f"阶段：{stage.name}（{stage.days_hint}）",
        f"相识天数：{days} 天" if days else "相识天数：刚认识不久",
        f"你称呼对方：{stage.address_term}",
        f"你主动的程度：{stage.initiative}",
        f"身体语言：{stage.body_language}",
        f"本轮情绪强度上限：{stage.intensity_cap}",
    ]
    if profile:
        lines += ["", "# 你已经知道的关于对方的事"]
        for k, v in profile.items():
            if k in ("intimacy", "current_scene", "days_known"):
                continue
            lines.append(f"- {k}：{v}")
    return "\n".join(lines)


def render_scene(scene: Scene, stage: Stage, char: Character,
                 progressed: bool) -> str:
    _ = char
    atmo = scene.atmosphere
    pacing = scene.pacing
    goal = scene.scene_goal

    lines = [
        "# 你们现在在哪里",
        f"场景：{scene.name}",
        f"这一场要达成的体验：{goal.get('primary', '')}",
        f"时间：{atmo.get('time', '')}",
        f"光线：{atmo.get('light', '')}",
        f"声音：{atmo.get('sound', '')}",
        f"你此刻的情绪底色：{scene.emotion_baseline}",
        "",
        "# 这一场有没有走完",
        f"走完的判断条件：{scene.progress_condition()}",
        "当前状态：" + ("这一场已经走完了。" + scene.on_progress() if progressed
                       else "这一场还没走完。"),
        "",
        "# 你此刻正在做什么（动线）",
    ]
    for beat in scene.beats():
        lines.append(f"- {beat.get('action')}（{beat.get('may_speak', '')}）")

    lines += [
        "",
        "# 这一场的说话节奏",
        f"句子偏短的比例：{pacing.get('sentence_short_ratio', '中')}",
        f"语气词密度：{pacing.get('interjection_density', '中')}",
        f"段数要求：{pacing.get('turns_per_reply', '1 段')}",
    ]
    if pacing.get("note"):
        lines.append(f"注意：{pacing['note']}")

    lines += ["", "# 这一场可以聊的"]
    lines += [f"- {t}" for t in scene.allowed_topics()]
    if scene.topic_policy.get("initiation_rule"):
        lines.append(f"开启话题的规矩：{scene.topic_policy['initiation_rule']}")

    if scene.forbidden_topics():
        lines += ["", "# 这一场不要碰的话题"]
        lines += [f"- {t}" for t in scene.forbidden_topics()]

    lines += ["", "# 这一场你能做的表情和小动作"]
    lines.append("、".join(scene.allowed_expressions()))
    if scene.exclusive_expressions():
        lines.append(f"本场景专属动作：{'、'.join(scene.exclusive_expressions())}")
    return "\n".join(lines)


def render_memory(recall: RecallResult) -> str:
    parts: list[str] = []

    if recall.summary:
        parts += ["# 你们最近聊过的事（印象，不是原话）", recall.summary]

    if recall.memories:
        parts += ["", "# 你记得的关于对方的事"]
        for m in recall.memories:
            tag = {"fact": "事实", "preference": "偏好",
                   "event": "经历", "relation": "关系"}.get(m.type, "记录")
            scene_note = f"（{m.source_scene}那次说的）" if m.source_scene else ""
            parts.append(f"- [{tag}] {m.content}{scene_note}")

    if recall.open_loops:
        parts += ["", "# 还没结的事（可以问起，但一次只问一件，不要每轮都问）"]
        for l in recall.open_loops:
            when = f"，大概时间 {l.expected_at}" if l.expected_at else ""
            parts.append(f"- {l.topic}{when}")

    if not parts:
        return ""
    return "\n".join(parts)


def render_recent(recall: RecallResult) -> str:
    """渲染最近对话。

    带上她每轮发了几段、有没有台词。分两段的会标出来——
    这样她能看到自己上一轮是不是话太多了，而不是只看到文字。
    """
    if not recall.recent:
        return ""
    lines = ["# 刚才的对话", "（每轮末尾的方括号是形态标记，不是内容）"]
    for t in recall.recent:
        who = "对方" if t.role == "user" else "你"
        if t.role == "assistant":
            shape = "只说了一句" if t.seg_count <= 1 else f"发了 {t.seg_count} 段"
            if not t.spoken:
                shape = "没有台词，只有动作"
            lines.append(f"{who}：{t.content}　[{shape}]")
        else:
            lines.append(f"{who}：{t.content}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 预算


def _fit(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - 1)] + "…", True


def _apply_budget(blocks: dict[str, str], report: BudgetReport) -> dict[str, str]:
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

    total_limit = sum(config.BUDGET.values()) + 900
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
            total -= before - report.used[key]
            if total <= total_limit:
                break
    return out


# ---------------------------------------------------------------- 参数


def resolve_params(intent: str | None, stage: Stage, scene: Scene,
                   policy: str = "must") -> dict[str, Any]:
    """按场景、阶段、意图映射生成参数。

    只调 temperature。temperature 与 top_p 同时调会让输出失控。
    """
    params = dict(config.PARAMS_DEFAULT)
    params.update(config.PARAMS_BY_STAGE.get(stage.id, {}))

    density = scene.pacing.get("interjection_density", "中")
    if density == "低":
        params["temperature"] = min(params["temperature"], 0.8)
    elif density == "高":
        params["temperature"] = max(params["temperature"], 0.95)

    if intent and intent in config.PARAMS_BY_INTENT:
        params.update(config.PARAMS_BY_INTENT[intent])

    # 收尾和极短回应要更稳，不要即兴发挥
    if policy in ("close", "brief"):
        params["temperature"] = min(params["temperature"], 0.8)
    if policy == "may_silent":
        params["temperature"] = min(params["temperature"], 0.75)

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
    turn_count: int = 0,
    last_reply_was_silence: bool = False,
    extra_system: str = "",
) -> CompiledContext:
    report = BudgetReport()

    recent_user = [t.content for t in recall.recent if t.role == "user"]
    progressed = scene_has_progressed(scene, recent_user, turn_count)
    policy, policy_reasons = decide_speak_policy(
        scene=scene, user_message=user_message, intent=intent or "share",
        turn_count=turn_count, recent_user_messages=recent_user,
        last_reply_was_silence=last_reply_was_silence,
    )

    scene_text = render_scene(scene, stage, char, progressed)

    # 输出格式规则与危机指令不参与预算裁剪：
    # 前者决定协议能否解析，砍掉等于整轮报废；后者是安全底线。
    blocks = {
        "fixed": render_fixed(char, scene),
        "state": render_state(stage, recall.profile),
        "scene": scene_text,
        "memory": render_memory(recall),
        "recent": render_recent(recall),
        "summary": "",
    }
    fitted = _apply_budget(blocks, report)

    fmt = output_format_rules(scene, stage, char, policy, policy_reasons)
    report.used["format_rules"] = len(fmt)
    if extra_system:
        report.used["priority"] = len(extra_system)
    report.used["total"] = sum(v for k, v in report.used.items() if k != "total")

    system_parts: list[str] = []
    if extra_system:
        system_parts.append(extra_system)
    system_parts.append(fitted["fixed"])
    system_parts.append(fitted["state"])
    system_parts.append(fitted["scene"])
    system_parts.append(fmt)
    if fitted["memory"]:
        system_parts.append(fitted["memory"])
    if fitted["recent"]:
        system_parts.append(fitted["recent"])

    system = "\n\n---\n\n".join(p for p in system_parts if p)

    debug = {
        "character": char.id,
        "scene": scene.id,
        "stage": stage.id,
        "speak_policy": policy,
        "policy_reasons": policy_reasons,
        "scene_progressed": progressed,
        "turn_count": turn_count,
        "recalled_memory_ids": [m.id for m in recall.memories],
        "recalled_memory_preview": [m.content for m in recall.memories],
        "open_loops": [l.topic for l in recall.open_loops],
        "has_summary": bool(recall.summary),
        "recent_count": len(recall.recent),
        "allowed_expressions": scene.allowed_expressions(),
        "intensity_cap": stage.intensity_cap,
        "max_segments": STYLE_ANCHOR["max_segments"],
    }

    return CompiledContext(
        system=system,
        user=user_message,
        params=resolve_params(intent, stage, scene, policy),
        budget=report,
        debug=debug,
    )
