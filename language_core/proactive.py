"""主动开口。

解决的问题：视频按分钟计费，冷场就是让用户白花钱。
对话走进死胡同时，由她这一端把话头挑起来。

与「查岗」的界线：
  她的话题来源永远是「她自己的状态 + 她此刻注意到的东西」，
  不是「你怎么不说话了」。
  措辞上必须避开一切等待框架和催促质问，那是另一种产品。

三道闸，缺一不可：
  ① 静默够久（且确实卡住了，不是用户正在打字）
  ② 过了冷却，且本轮死胡同还没开口过
  ③ 没有正在生成的回复
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from . import config

_TZ = timezone(timedelta(hours=8))

# 三种开口方式。她挑话题永远从这三类里出，不从「问用户」出。
KINDS = ("bored", "noticing", "inviting")

KIND_LABEL = {
    "bored": "自顾自（说她自己的状态）",
    "noticing": "注意到你（说此刻观察到的细节）",
    "inviting": "发起行动（把对话转成一起做点什么）",
}

# 绝对禁止出现的句式。命中即判定为查岗，不发。
FORBIDDEN_PATTERNS = (
    "你在干嘛", "你在干什么", "你怎么不说话", "你还在吗", "还在吗",
    "怎么不回", "怎么不回复", "我等你好久", "我一直在等", "你人呢",
    "好久没回", "怎么没动静", "你去哪了", "你在忙吗", "是不是不想聊",
)


@dataclass
class StallSignal:
    """死胡同信号。用来判断「是不是真的卡住了」。"""

    idle_seconds: float
    last_user_text: str = ""
    prev_user_text: str = ""
    last_reply_had_hook: bool = True

    @property
    def short_reply_streak(self) -> int:
        """用户连续用极短回应敷衍了几次。两条就是话题接不下去了。"""
        def brief(text: str) -> bool:
            t = "".join((text or "").split())
            return 0 < len(t) <= 4
        n = 0
        if brief(self.last_user_text):
            n += 1
            if brief(self.prev_user_text):
                n += 1
        return n


@dataclass
class ProactiveDecision:
    due: bool
    reason: str = ""
    kind: str | None = None
    idle_seconds: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "due": self.due,
            "reason": self.reason,
            "kind": self.kind,
            "idle_seconds": round(self.idle_seconds, 1),
            "threshold": config.PROACTIVE_IDLE_SECONDS,
            "cooldown": config.PROACTIVE_COOLDOWN_SECONDS,
        }


# 上一次主动开口用了哪种方式。从当时注入的旁白里反推——
# 不额外建字段，避免又多一处要同步的状态。
_KIND_FINGERPRINTS = (
    ("说你自己此刻的状态", "bored"),
    ("说你此刻注意到的具体细节", "noticing"),
    ("挑起一件此刻真能一起做的事", "inviting"),
)


def kind_from_history(context_content: str) -> str | None:
    text = context_content or ""
    for fingerprint, kind in _KIND_FINGERPRINTS:
        if fingerprint in text:
            return kind
    return None


def pick_kind(*, scene, char, stall: StallSignal, last_kind: str | None,
              seed: int = 0) -> str:
    """挑一种开口方式。

    优先级：场景里有可推进的事且用户在敷衍 → 发起行动；
    否则用户敷衍或她上句没留钩子 → 注意到你；再否则自顾自。
    连着两次不用同一种，重复会让主动消息显得机械。
    """
    activities = list(scene.activity_pool or [])
    styles = dict(char.raw.get("proactive_style", {}) or {})

    candidates: list[str] = []
    if activities and stall.short_reply_streak >= 1:
        candidates.append("inviting")
    if stall.short_reply_streak >= 1 or not stall.last_reply_had_hook:
        candidates.append("noticing")
    candidates.append("bored")
    if activities:
        candidates.append("inviting")

    # 只保留人设里写了的类型
    usable = [k for k in candidates if styles.get(k)]
    if not usable:
        usable = [k for k in KINDS if styles.get(k)] or ["bored"]

    if last_kind in usable and len(usable) > 1:
        usable = [k for k in usable if k != last_kind]
    return usable[seed % len(usable)]


def decide(*, last_activity_at: str | None, now: datetime | None = None,
           last_proactive_at: str | None = None,
           user_turns_since_proactive: int = 99,
           generating: bool = False) -> ProactiveDecision:
    """判断这一刻该不该主动开口。

    user_turns_since_proactive 是上一次她主动开口之后用户说了几句话。
    少于 PROACTIVE_MIN_USER_TURNS 就不追第二条——用户只回一个「嗯」
    不解锁下一次，否则等于陪着他一起敷衍，白烧他的钱。
    """
    now = now or datetime.now(_TZ)

    def parse(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=_TZ)

    last = parse(last_activity_at)
    if last is None:
        return ProactiveDecision(False, "没有活动记录")

    idle = (now - last).total_seconds()

    if generating:
        return ProactiveDecision(False, "正在生成回复", idle_seconds=idle)

    if idle < config.PROACTIVE_IDLE_SECONDS:
        return ProactiveDecision(
            False, f"静默 {idle:.1f}s，未到 {config.PROACTIVE_IDLE_SECONDS}s",
            idle_seconds=idle)

    if user_turns_since_proactive < config.PROACTIVE_MIN_USER_TURNS:
        return ProactiveDecision(
            False,
            f"距上次主动开口只收到 {user_turns_since_proactive} 句回应，"
            f"需 {config.PROACTIVE_MIN_USER_TURNS} 句才解锁下一次",
            idle_seconds=idle)

    prev = parse(last_proactive_at)
    if prev is not None:
        since = (now - prev).total_seconds()
        if since < config.PROACTIVE_COOLDOWN_SECONDS:
            return ProactiveDecision(
                False,
                f"距上条主动消息 {since:.0f}s，未过冷却 {config.PROACTIVE_COOLDOWN_SECONDS}s",
                idle_seconds=idle)

    return ProactiveDecision(True, f"静默 {idle:.1f}s，对话卡住了", idle_seconds=idle)
