"""一轮对话的完整编排。

① 分流   判断这句话是什么，决定走哪条策略
② 召回   找她该想起什么
③④ 取场景卡与关系阶段
⑤ 编译   拼成她眼前的世界
⑥ 生成
⑦ 校验   不合格就重生成
⑧ 写入   异步，不阻塞回复
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import config, persona
from .checker import check_turn
from .compiler import CompiledContext, compile_context
from .llm import build_llm, compress_summary
from .memory import MemoryStore, MemoryWriter, RecallResult
from .persona import load_character, load_scene, stage_for_intimacy
from .segment import Turn, parse

# ---------------------------------------------------------------- ① 分流

INTENT_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("crisis",   ("不想活", "自杀", "结束生命", "活不下去", "伤害自己")),
    ("refuse",   ("滚", "别烦我", "不想聊这个")),
    ("comfort",  ("累", "难过", "烦", "压力", "难受", "委屈", "不开心", "失眠", "焦虑", "崩溃")),
    ("tease",    ("哈哈", "笑死", "你真", "你是不是傻")),
    ("probe",    ("吗？", "么？", "为什么", "怎么样", "你说呢")),
    ("invite",   ("我们去", "要不要一起", "陪我去")),
    ("reminisce", ("上次", "之前", "那次", "以前")),
]


def classify_intent(message: str) -> str:
    text = (message or "").strip()
    if not text:
        return "idle"
    for name, keys in INTENT_RULES:
        if any(k in text for k in keys):
            return name
    if text.endswith(("?", "？")):
        return "probe"
    return "share"


# ---------------------------------------------------------------- 结果


@dataclass
class TurnResult:
    turn: Turn
    context: CompiledContext
    checks: dict[str, Any]
    regenerated: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "reply": self.turn.to_dict(),
            "debug": {
                "params": self.context.params,
                "budget": self.context.budget.to_dict(),
                "context": self.context.debug,
                "checks": self.checks,
                "regenerated": self.regenerated,
                "mode": config.MODE,
                "error": self.error,
            },
        }


# ---------------------------------------------------------------- 引擎


# ---------------------------------------------------------------- 工具


def _serialize_context(turn) -> str:
    """把一轮回复还原成带旁白的行协议，供下一轮回放。

    历史里如果只存纯台词，她会丢掉自己上一轮的情绪和语速；
    下次开口时状态就接不上。
    """
    parts = []
    for s in turn.segments:
        head = '@seg type=' + s.type
        if s.narration:
            n = s.narration
            if n.emotion:
                head += ' emotion=' + n.emotion[0].name + ':' + str(n.emotion[0].intensity)
            head += ' pace=' + (n.pace or 'normal')
            if n.expression:
                head += ' expr=' + ','.join(n.expression)
        parts.append(head + '\n' + (s.text or ''))
    return '\n[sep]\n'.join(parts)


PROACTIVE_MAX_CHARS = 40
PROACTIVE_MAX_CONTAINMENT = 0.65


def _bigrams(text: str) -> set[str]:
    t = "".join(ch for ch in (text or "") if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    return {t[i:i + 2] for i in range(len(t) - 1)} or ({t} if t else set())


def _containment(a: str, b: str) -> float:
    """a 有多少比例的内容也出现在 b 里。

    用包含度而不是 Jaccard 相似度：同一个话题里换句话说是正常的
    （她刚说完画画，接着还提画画不算重复），但如果新说的这句
    几乎整句都能在上一条里找到，那就是把上一条又说了一遍。
    """
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga)


import re as _re

_SENTENCE_SPLIT = _re.compile(r'(?<=[。！!？?；;])')


def _first_sentence(text: str) -> str:
    """取第一句，去掉标点和空白，用于比对开头是否重复。"""
    t = (text or "").strip()
    if not t:
        return ""
    parts = [p for p in _SENTENCE_SPLIT.split(t) if p.strip()]
    head = parts[0] if parts else t
    return _re.sub(r'[\s。！!？?；;，,、…]+', '', head)


def _same_opening(a: str, b: str, min_len: int = 4) -> bool:
    """她是不是把上一条的开头原样搬过来了。

    「这算哪门子厉害。你到底是饿了…」→「这算哪门子厉害。行，我猜中了没有…」
    整体重合度只有 0.42，按整句判是判不出来的，但用户一眼就看到重复了。
    真正刺眼的是开头那一句被原样搬过来。

    判定用精确相等，不用包含。用包含会误伤——
    「厉害什么」是「厉害什么饿不饿还能猜错」的子串，但它其实是个正常的新起手。
    """
    ha, hb = _first_sentence(a), _first_sentence(b)
    if len(ha) < min_len or len(hb) < min_len:
        return False
    return ha == hb


def _proactive_violations(turn, previous: str) -> tuple[bool, list[str]]:
    """主动开口的硬约束。返回 (是否不合格, 原因)。

    一条、不超过 40 字、不以问号结尾——这三条是产品要求：
    用户在按分钟付费，起个头不该长篇大论，也不该变成提问。
    与上一条重复那两条是实测踩出来的：模型会把上一轮说过的话
    稍微改改再说一遍，用户看到的是「她怎么又说了一遍」。
    """
    problems: list[str] = []
    text = (turn.dialogue or "").strip()

    if not text:
        problems.append('没有台词')
    if len(turn.segments) > 1:
        problems.append(f'{len(turn.segments)} 段，只该发一段')
    if len(text) > PROACTIVE_MAX_CHARS:
        problems.append(f'{len(text)} 字，超过 {PROACTIVE_MAX_CHARS}')
    if text.endswith(('？', '?')):
        problems.append('以问号结尾，这一轮不该提问')
    if previous and _containment(text, previous) > PROACTIVE_MAX_CONTAINMENT:
        problems.append('新说的几乎整句都在上一条里，等于重复')
    elif previous and _same_opening(text, previous):
        problems.append('开头和上一条一样')
    return bool(problems), problems


def _trim_proactive(turn, previous: str):
    """重试仍不合格时的兜底：压到一段、去掉重复的开头、去掉问句。

    宁可短，宁可少说一句，也不能让她在用户付费的时间里重复自己。

    开头重复的处理是直接删掉那一句——实测下来这是最干净的做法：
    让她重说一遍，模型还是会把同一个开头搬过来。
    """
    parts = [s for s in turn.segments if s.spoken and (s.text or '').strip()]
    keep = parts[0] if parts else None
    if keep is None:
        return turn

    text = (keep.text or '').strip()

    # 开头和上一条一样 → 把这句删掉，只留后面的
    if previous and _same_opening(text, previous):
        sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
        if len(sentences) > 1:
            text = ''.join(sentences[1:]).strip()
        else:
            # 整句都是重复的，没有什么可留
            raise RuntimeError('主动开口整句重复，本轮放弃')

    # 再只留第一句。硬截断会留下半句话，读起来更像出错。
    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if sentences:
        text = sentences[0].strip()

    text = text.rstrip()
    while text.endswith(('？', '?')):
        text = text[:-1].rstrip('，,、 ')

    if len(text) > PROACTIVE_MAX_CHARS:
        cut = text[:PROACTIVE_MAX_CHARS]
        # 优先切在句号上，退而求其次切在逗号，都没有就切在最后一个整字
        for marks in (('。', '！', '!'), ('，', '；', ';', '、')):
            pos = max((cut.rfind(m) for m in marks), default=-1)
            if pos >= PROACTIVE_MAX_CHARS // 2:
                cut = cut[:pos + 1]
                break
        else:
            cut = cut.rstrip('，,、；; ')
        text = cut.rstrip()
        if not text.endswith(('。', '！', '!', '，', '、', '；')):
            text += '。'

    if previous and _same_opening(text, previous):
        raise RuntimeError('主动开口与上一条重复，本轮放弃')

    if text and not text.endswith(('。', '！', '!', '…')):
        text += '。'

    turn.segments = [s for s in turn.segments if s is keep]
    keep.text = text
    for i, s in enumerate(turn.segments):
        s.index = i
    return turn


class Engine:
    def __init__(self, store: MemoryStore | None = None, llm: Any | None = None):
        self.store = store or MemoryStore()
        self.llm = llm or build_llm()
        self.writer = MemoryWriter(self.store)
        self._turn_counter = 0

    # ---- 关系状态 ----

    def relationship(self, user_id: str, character_id: str) -> dict[str, Any]:
        profile = self.store.get_profile(user_id, character_id)
        try:
            intimacy = int(profile.get("intimacy", "0"))
        except ValueError:
            intimacy = 0
        stage = stage_for_intimacy(intimacy)
        unlocked = [s.scene_id for s in persona.STAGES if s.intimacy_min <= intimacy]
        return {
            "intimacy": intimacy,
            "stage": stage,
            "unlocked_scenes": unlocked or ["cafe"],
            "days_known": int(profile.get("days_known", "0") or 0),
        }

    def grant_intimacy(self, user_id: str, character_id: str, delta: int) -> int:
        """亲密度是解锁场景的唯一标准。按完整分钟累计，反复接通不能刷。"""
        current = self.relationship(user_id, character_id)["intimacy"]
        new_value = max(0, min(100, current + delta))
        self.store.set_profile(user_id, character_id, "intimacy", str(new_value))
        return new_value

    # ---- 主流程 ----

    def respond(self, *, user_id: str, character_id: str, scene_id: str,
                message: str, allow_regenerate: bool = True, use_memory: bool = False,
                on_segment: Any = None, cancelled: Any = None) -> TurnResult:
        char = load_character(character_id)
        scene = load_scene(scene_id)
        rel = self.relationship(user_id, character_id)
        stage = rel["stage"]

        intent = classify_intent(message)

        # 记忆必须赶在召回之前可用。
        # 用户说「我叫阿哲」，紧接着问「你还记得我叫什么吗」——这两句之间
        # 如果记忆还在队列里没落库，她就会答「你还没告诉过我」。
        # 这类事故用户直接判定为「她根本没记住我」，不能接受。
        # 所以本轮抽取到的记忆采用同步 upsert（本地写，开销极小，且自带去重），
        # 后台线程只负责兜底整理。
        extracted = self.writer.extract(message) if use_memory else ([], [])
        self._upsert_extracted(user_id, character_id, extracted, scene_id)

        recall = self.store.recall_all(user_id, character_id, message, scene_id) if use_memory else RecallResult(
            profile=self.store.get_profile(user_id, character_id),
            recent=self.store.recent_turns(user_id, character_id, config.RECENT_TURNS))

        # 本场已经聊了多少轮（用户消息数），用于判断场景是否走完
        turn_count = self.store.message_count(user_id, character_id) // 2
        # 上一轮她是不是只给了动作、没有台词
        last_reply_was_silence = self.store.last_assistant_was_silent(user_id, character_id)

        # 危机信号优先级最高，盖过所有人格与场景设定
        extra = ""
        if intent == "crisis":
            from .persona import BOUNDARY_POLICY
            extra = (
                "# 【最高优先级】用户可能出现危机信号\n"
                f"触发条件：{BOUNDARY_POLICY['crisis']['trigger']}\n"
                f"你要做的：{BOUNDARY_POLICY['crisis']['action']}\n"
                "这一轮不要推进剧情、不要开玩笑、不要追问细节。"
            )

        ctx = compile_context(
            char=char, scene=scene, stage=stage, recall=recall,
            user_message=message, intent=intent, extra_system=extra,
            turn_count=turn_count, last_reply_was_silence=last_reply_was_silence,
        )

        turn, checks, regen, error = self._generate_and_check(
            ctx=ctx, scene=scene, stage=stage, char=char, recall=recall,
            allow_regenerate=allow_regenerate, on_segment=on_segment, cancelled=cancelled,
        )

        # ---- ⑧ 落库与异步写入 ----
        if cancelled and cancelled():
            raise RuntimeError('本轮已停止')
        self.store.add_message(user_id, character_id, "user", message, scene_id)
        self.store.add_message(
            user_id, character_id, "assistant", turn.dialogue, scene_id,
            spoken=bool(turn.dialogue.strip()),
            seg_count=len(turn.segments),
            context_content=_serialize_context(turn),
        )
        self.writer.submit(user_id, character_id, message, scene_id, turn.turn_id,
                           extracted=extracted)
        if use_memory:
            self._maybe_compress_summary(user_id, character_id)
        self.store.mark_open_loops_asked(user_id, character_id)

        return TurnResult(turn=turn, context=ctx, checks=checks,
                          regenerated=regen, error=error)

    def proactive(self, *, user_id: str, character_id: str, scene_id: str,
                  kind: str, seed: int = 0, use_memory: bool = False,
                  on_segment: Any = None, cancelled: Any = None) -> TurnResult:
        """她主动开口的一轮。没有用户输入。

        与 respond 的区别：
          - 不做分流（没有用户消息可分类）
          - 不写用户消息
          - 提示词里多一段主动开口指令，明确禁止催问句式
          - 落库时打上 proactive 标记，供「一个死胡同只开口一次」的判断使用
          - 生成后过三道硬约束：一段、不超过 40 字、不能重复上一条

        三道约束里前两道是产品要求（用户在按分钟付费，起个头不该长篇大论），
        第三道是实测踩出来的：模型会把上一轮说过的话稍微改改再说一遍，
        用户看到的是"她怎么又说了一遍"。
        """
        char = load_character(character_id)
        scene = load_scene(scene_id)
        rel = self.relationship(user_id, character_id)
        stage = rel["stage"]
        previous = self.store.last_assistant_text(user_id, character_id)
        turn_count = self.store.message_count(user_id, character_id) // 2

        def build(strict: bool) -> CompiledContext:
            recall = self.store.recall_all(user_id, character_id, "", scene_id) if use_memory else RecallResult(
                profile=self.store.get_profile(user_id, character_id),
                recent=self.store.recent_turns(user_id, character_id, config.RECENT_TURNS))
            return compile_context(
                char=char, scene=scene, stage=stage, recall=recall,
                user_message="", intent="proactive", turn_count=turn_count,
                proactive=kind, proactive_seed=seed, proactive_strict=strict,
                proactive_previous=previous,
            )

        ctx = build(False)
        turn, checks, regen, error = self._generate_and_check(
            ctx=ctx, scene=scene, stage=stage, char=char, recall=RecallResult(),
            allow_regenerate=True, on_segment=on_segment, cancelled=cancelled,
        )

        # 不合格就带上更严的约束重来一次
        if _proactive_violations(turn, previous)[0]:
            if on_segment is not None:
                raise RuntimeError('主动开口不合格，已重试') from None
            strict_ctx = build(True)
            retry, checks2, regen2, error2 = self._generate_and_check(
                ctx=strict_ctx, scene=scene, stage=stage, char=char, recall=RecallResult(),
                allow_regenerate=True, on_segment=None, cancelled=cancelled,
            )
            if not _proactive_violations(retry, previous)[0]:
                ctx, turn, checks, regen, error = strict_ctx, retry, checks2, regen2 + 1, error2
            else:
                turn = _trim_proactive(retry, previous)
                ctx = strict_ctx

        if cancelled and cancelled():
            raise RuntimeError('本轮已停止')

        if not turn.dialogue.strip():
            raise RuntimeError('主动开口没有产生台词')

        self.store.add_message(
            user_id, character_id, "assistant", turn.dialogue, scene_id,
            spoken=True, seg_count=len(turn.segments),
            context_content=_serialize_context(turn), proactive=True,
        )

        return TurnResult(turn=turn, context=ctx, checks=checks,
                          regenerated=regen, error=error)

    def _generate_and_check(self, *, ctx: CompiledContext, scene, stage, char,
                            recall: RecallResult, allow_regenerate: bool, on_segment=None, cancelled=None):
        error: str | None = None
        regen = 0
        checks: dict[str, Any] = {}

        emitted = 0
        for attempt in range(2 if allow_regenerate else 1):
            buffer = ''
            invalid_stream = False
            def delta(chunk):
                nonlocal buffer, emitted, invalid_stream
                if cancelled and cancelled():
                    raise RuntimeError('本轮已停止')
                buffer += chunk
                while '[sep]' in buffer:
                    block, buffer = buffer.split('[sep]', 1)
                    partial = parse(block, character_id=char.id, scene_id=scene.id,
                                    allowed_expressions=scene.allowed_expressions(),
                                    default_expression=scene.default_expression())
                    validation = check_turn(partial, scene=scene, stage=stage, char=char,
                                            user_message=ctx.user, speak_policy='brief')
                    if validation.hard_fail or invalid_stream:
                        invalid_stream = True
                        continue
                    if emitted + len(partial.segments) > 6:
                        raise RuntimeError('输出片段过多，已停止生成')
                    for segment in partial.segments:
                        segment.index = emitted
                        on_segment(segment.to_dict())
                        emitted += 1
            result = self.llm.generate(
                system=ctx.system, user_message=ctx.user, params=ctx.params,
                context=ctx.debug, recall=recall, attempt=attempt,
                messages=ctx.messages() if attempt == 0 else [*ctx.messages(), {'role': 'system', 'content': '上一版格式不合格。请重新生成回复：每段必须有 @seg type=dialogue emotion=名称:强度 pace=normal expr=表情 的完整头部，下一行才是台词。不要输出任何其他内容。'}], on_delta=delta if on_segment else None)
            if result.error:
                # Never present scripted mock dialogue as a live-model reply.
                raise RuntimeError('模型暂时无法完成回复，请重试。' + result.error[:180])

            if result.text.count('[sep]') >= 6:
                raise RuntimeError('模型输出超过六个片段，请重试。')
            turn = parse(
                result.text,
                character_id=char.id,
                scene_id=scene.id,
                stage=stage.id,
                model=result.model,
                allowed_expressions=scene.allowed_expressions(),
                default_expression=scene.default_expression(),
                local_expressions=scene.exclusive_expressions(),
                max_chars=None,
                # 禁用词由 checker 统一判定，这里不重复传，避免两处词表漂移
                banned_words=None,
            )
            checks = check_turn(turn, scene=scene, stage=stage,
                                char=char, user_message=ctx.user,
                                speak_policy=ctx.debug.get("speak_policy", "must")).to_dict()

            if not checks.get("hard_fail"):
                if on_segment:
                    for seg in turn.segments[emitted:]:
                        on_segment(seg.to_dict())
                return turn, checks, regen, error
            if emitted:
                raise RuntimeError('本轮输出未通过校验，已停止生成；请重试。')
            regen += 1
        raise RuntimeError('模型连续输出不符合协议，请重试。')

    def _upsert_extracted(self, user_id: str, character_id: str,
                          extracted: tuple[list[tuple[str, str, float]], list[str]],
                          scene_id: str) -> None:
        """把本轮抽取到的记忆同步落库，让它立刻可被召回。

        规则抽取带置信度，add_memory 内部会做去重与低置信拒收，
        所以重复调用是安全的。
        """
        memories, loops = extracted
        for type_, content, conf in memories:
            self.store.add_memory(user_id, character_id, type_, content,
                                  confidence=conf, source_scene=scene_id)
        for topic in loops:
            self.store.add_open_loop(user_id, character_id, topic, scene=scene_id)

    def _maybe_compress_summary(self, user_id: str, character_id: str) -> None:
        """短期记忆滚动压缩。每 SUMMARY_EVERY 条消息触发一次。"""
        n = self.store.message_count(user_id, character_id)
        if n == 0 or n % config.SUMMARY_EVERY != 0:
            return
        recent = self.store.recent_turns(user_id, character_id, config.SUMMARY_EVERY)
        dialogue = "\n".join(
            f"{'用户' if t.role == 'user' else '她'}：{t.content}" for t in recent
        )
        old = self.store.get_summary(user_id, character_id)
        new_summary = compress_summary(self.llm, old, dialogue)
        if new_summary:
            self.store.set_summary(user_id, character_id, new_summary)

    def shutdown(self) -> None:
        """停止后台写入线程并关闭数据库连接。

        只停线程不关连接会在 Windows 上留下文件锁，测试结束删库会失败。
        """
        self.writer.shutdown()
        self.store.close()
