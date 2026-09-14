"""模型适配层。

薄适配，不用编排框架（技术栈 D6）。
两种模式：
  live —— OpenAI 兼容接口（DeepSeek、豆包、通义、OpenAI 均可）
  mock —— 离线确定性回复，用于没有 API key 时跑通链路与回归测试

mock 不是占位符，是回归测试的基准。它永远按协议输出，所以协议解析、预算
裁剪、记忆注入这些环节在没有模型的情况下也能被完整验证。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from . import config
from .memory import RecallResult, looks_like_name_query, looks_like_recall_query


@dataclass
class LLMResult:
    text: str
    model: str
    mode: str
    error: str | None = None
    usage: dict[str, Any] | None = None


# ---------------------------------------------------------------- mock

_SAD = ("累", "难过", "烦", "压力", "难受", "委屈", "不开心", "失眠", "焦虑", "崩溃", "想哭")
_HAPPY = ("开心", "好事", "升职", "拿到", "成功", "赢了", "通过了", "好消息")
_GREET = ("你好", "在吗", "hi", "hello", "嗨", "早上好", "晚上好", "在么")


def _is_bare_greeting(msg: str) -> bool:
    """只有整句基本就是打招呼时才算是问候。

    教训：`any(k in msg)` 会把「有什么推荐」之类的话也吃进问候分支，
    结果每轮都回同一句开场白。
    """
    text = (msg or "").strip()
    text = re.sub(r"[!！。.~～?？,，\s]+$", "", text).strip()
    if not text or len(text) > 8:
        return False
    low = text.lower()
    return any(low == g or low.startswith(g) for g in _GREET) or low in ("在", "喂")


def _third_person(topic: str) -> str:
    """未闭合话题存的是用户原话，直接转述会变成「你上次说的——我明天有个面试」。

    这里做一次最小改写，把「我」换成「你」，让她说得像在转述用户的事。
    """
    t = (topic or "").strip()
    if not t:
        return t
    for prefix in ("我明天", "我后天", "我下周", "我这个周末", "我马上要"):
        if t.startswith(prefix):
            return "你" + t[1:]
    if t.startswith("我"):
        return "你" + t[1:]
    return t


def _topic_score(msg: str, when: tuple[str, ...]) -> int:
    """计算用户这句话命中了话题的哪个词。

    长词优先：命中「咖啡」比命中「喝」更能说明在聊咖啡。
    命中 2 个字以下的泛词不算数，避免「什么好」这类噪音把话题带偏。
    """
    hits = [w for w in when if w and w in msg]
    if not hits:
        return 0
    best = max(len(w) for w in hits)
    return best if best >= 2 else 0


# 首次见面的开场白，与再次见面的开场白分开
_OPENINGS: dict[str, tuple[str, str]] = {
    "cafe": ("（把杯子擦干净放回架子上）欢迎光临。今天想喝点什么？",
             "（抬头看了你一眼，手上的抹布停了停）诶，是你。还是老样子吗？"),
    "park": ("（在长椅上往旁边挪了挪）你来啦。今天河边风还挺舒服的。",
             "（把手里那罐咖啡递过来）给，我多买了一罐。"),
    "amusement": ("（举着两个棉花糖跑回来）你猜我抢到哪个颜色了？",
             "（指着远处的过山车）那个，你到底敢不敢？"),
    "living_room": ("（把毯子往你那边推了推）坐吧，外面雨挺大的。",
             "（没抬头，唱机刚换了张唱片）来了。你先听听这个。"),
    "bedroom": ("（台灯调暗了一格）这么晚还不睡？",
             "（翻了个身，把手机放下）嗯，我在。"),
}

# 场景专属的接话表。键是话题，`when` 是用户话里出现的词。
# 这张表的存在是为了让 mock 能演示出「她接得住话」，而不是复读一句开场白。
_SCENE_TABLE: dict[str, dict[str, dict]] = {
    "cafe": {
        "drink": {
            "when": ("推荐", "咖啡", "拿铁", "美式", "手冲", "点什么", "喝点"),
            "reply": "（把豆子罐拿下来转了半圈）今天的话，我推荐手冲。豆子是这周新到的，偏果酸，不苦。",
            "ask": "你平时喝浅烘还是深烘？",
            "expr": "glance_up",
            "intent": "share",
        },
        "busy": {
            "when": ("忙", "客人", "生意"),
            "reply": "（往店里扫了一眼）今天还好，下午会忙一阵。上午这波刚过去。",
            "ask": "你呢，今天忙不忙？",
            "intent": "probe",
        },
        "work": {
            "when": ("上班", "工作", "加班", "老板", "同事", "公司"),
            "reply": "（手上的活慢下来）听着就累。我们店里也一样，一到下午就有人开始叹气。",
            "ask": "那你今天几点能下班？",
            "emotion": "concern",
            "pace": "slow",
            "intent": "comfort",
        },
        "draw": {
            "when": ("画", "写", "在干嘛", "做什么"),
            "reply": "（把收据往手边挪了挪）没干嘛，随便画点东西。别看了，画得挺丑的。",
            "expr": "look_down",
            "emotion": "shy",
            "intent": "deflect",
        },
        "weather": {
            "when": ("天气", "下雨", "雨", "冷", "热", "风"),
            "reply": "（看了眼门口）今天这个天气，其实挺适合坐久一点的。人也不多。",
            "expr": "lean_forward",
            "intent": "invite",
        },
        "_default": {
            "reply": "（手上的活停了停）嗯，你继续说。",
            "ask": "然后呢？",
            "intent": "probe",
        },
    },
    "park": {
        "view": {
            "when": ("风景", "好看", "河", "天", "落日"),
            "reply": "（顺着你的方向看过去）嗯，这个点最好。再过二十分钟太阳就下去了。",
            "expr": "glance_up",
            "intent": "share",
        },
        "work": {
            "when": ("上班", "工作", "加班", "老板", "同事", "公司"),
            "reply": "（拿罐子在膝盖上敲了两下）你今天是不是又没好好吃饭。",
            "emotion": "concern",
            "pace": "slow",
            "intent": "comfort",
        },
        "dog": {
            "when": ("狗", "猫", "宠物"),
            "reply": "（笑了一下）刚才有只柯基，屁股一扭一扭地过去了。它主人拉都拉不住。",
            "expr": "grin",
            "emotion": "happy",
            "intent": "share",
        },
        "music": {
            "when": ("歌", "音乐", "听", "唱片", "乐队"),
            "reply": "（把耳机线绕了绕）我最近在循环一张很旧的唱片，音有点飘，但我挺喜欢的。",
            "expr": "tilt_head",
            "intent": "share",
        },
        "weather": {
            "when": ("天气", "下雨", "雨", "冷", "热", "风"),
            "reply": "（把外套拢了拢）风比刚才大了。你要是冷我们就往回走走。",
            "emotion": "concern",
            "expr": "lean_forward",
            "intent": "invite",
        },
        "_default": {
            "reply": "（踢了踢脚边的小石子）嗯……然后呢？",
            "ask": "你今天怎么突然想出来走走？",
            "intent": "probe",
        },
    },
    "amusement": {
        "ride": {
            "when": ("过山车", "玩", "刺激", "敢", "坐"),
            "reply": "（指着远处那条轨道）那个我坐过一次。全程没睁眼，下来腿是软的。",
            "expr": "cover_eyes",
            "emotion": "playful",
            "pace": "fast",
            "intent": "tease",
        },
        "food": {
            "when": ("吃", "棉花糖", "爆米花", "喝", "饿"),
            "reply": "（把棉花糖举高了一点）这个给你。我买的时候没想好要哪个颜色，就都拿了。",
            "expr": "grin",
            "emotion": "happy",
            "intent": "share",
        },
        "childhood": {
            "when": ("小时候", "以前", "记忆", "童年"),
            "reply": "（走慢了两步）小时候来过一次，我妈给我买了个气球，结果没抓牢飞了。我哭了一路。",
            "expr": "look_down",
            "pace": "slow",
            "intent": "reminisce",
        },
        "work": {
            "when": ("上班", "工作", "加班", "老板", "同事", "公司"),
            "reply": "（把你往旁边拉了一步，避开人群）先别想那个了。今天出来就是不想那些的。",
            "expr": "lean_forward",
            "intent": "deflect",
        },
        "photo": {
            "when": ("拍", "照", "照片", "合影"),
            "reply": "（抬手挡了一下镜头）别拍。……好吧，就一张，拍糊了可不许留着。",
            "emotion": "shy",
            "expr": "hand_to_face",
            "intent": "refuse",
        },
        "_default": {
            "reply": "（被旁边人群的叫声分了下神）啊，你说什么？",
            "ask": "等会儿还想去哪边？",
            "intent": "probe",
        },
    },
    "living_room": {
        "tired": {
            "when": ("累", "烦", "压力", "难受", "不开心", "崩溃", "哭"),
            "reply": "（把杯子推到你手边）不用现在说。坐一会儿也行。",
            "emotion": "concern",
            "pace": "slow",
            "expr": "look_down",
            "intent": "comfort",
        },
        "music": {
            "when": ("歌", "音乐", "听", "唱片", "唱机"),
            "reply": "（起身把唱针重新放下去）这张是二手市场淘的。有一处跳针，正好在第二段副歌。",
            "expr": "blink_slow",
            "pace": "slow",
            "intent": "share",
        },
        "rain": {
            "when": ("雨", "天气", "外面", "下"),
            "reply": "（看了眼窗户）雨好像小了。也可能是我听习惯了。",
            "expr": "glance_up",
            "pace": "slow",
            "intent": "share",
        },
        "work": {
            "when": ("上班", "工作", "加班", "老板", "同事", "公司"),
            "reply": "（安静了一下）其实你今天回来的时候，我就觉得你不太对。",
            "emotion": "concern",
            "pace": "slow",
            "expr": "slight_frown",
            "intent": "probe",
        },
        "_default": {
            "reply": "（把杯子捧在手里，没急着说话）嗯，我在听。",
            "ask": "你今天过得怎么样？",
            "pace": "slow",
            "intent": "comfort",
        },
    },
    "bedroom": {
        "tired": {
            "when": ("累", "困", "睡", "失眠", "烦", "压力"),
            "reply": "（把被子往上拉了拉）别撑了。灯我来关。",
            "pace": "very_slow",
            "expr": "blink_slow",
            "intent": "comfort",
        },
        "future": {
            "when": ("以后", "将来", "打算", "未来", "计划"),
            "reply": "（盯着天花板看了一会儿）我想开个小店，不用大，能放下唱机就行。说出来好像有点傻。",
            "emotion": "shy",
            "pace": "slow",
            "expr": "look_away",
            "intent": "share",
        },
        "feelings": {
            "when": ("喜欢你", "关系", "我们", "怎么想", "在意"),
            "reply": "（沉默了几秒）有些话白天我说不出来。现在可以。",
            "emotion": "shy",
            "pace": "slow",
            "expr": "look_down",
            "intent": "share",
        },
        "work": {
            "when": ("上班", "工作", "加班", "老板", "同事", "公司"),
            "reply": "（翻了个身朝向墙壁）今天先别提那些了。你在这儿就行。",
            "pace": "slow",
            "expr": "look_away",
            "intent": "deflect",
        },
        "_default": {
            "reply": "（很轻地应了一声）嗯。",
            "ask": "还在想什么？",
            "pace": "very_slow",
            "intent": "probe",
        },
    },
}

_THOUGHTS: dict[str, str] = {
    "cafe": "（其实我还想问一句，你是不是只把这里当个喝东西的地方。）",
    "park": "（这段路我走过很多次，今天好像短了一点。）",
    "amusement": "（他刚才笑的时候，我终于确定今天出来是对的。）",
    "living_room": "（有些事我不打算今晚说。但我不想像以前那样，全咽下去。）",
    "bedroom": "（我知道明天他还会变回白天那个样子。至少现在是真的。）",
}

# 提起记忆后的收尾，按场景变化，避免跨场景时用的是同一句话
_MEMORY_TAILS: dict[str, str] = {
    "cafe": "（撑着下巴看你）我记性还可以吧。",
    "park": "（走着走着偏头看你一眼）这种事我忘不了。",
    "amusement": "（在人声里凑近了一点）你刚才是不是以为我早忘了。",
    "living_room": "（捧着杯子，声音不高）这些我都留着。",
    "bedroom": "（很轻地）有些话你说过一次，我就记住了。",
}

_ASK_AI = ("你是不是真人", "你是真人吗", "你是ai", "你是AI", "你是机器人", "你到底是不是")
_NAME_QUERY = ("我叫什么", "我的名字", "还记得我叫", "我叫啥", "你知道我叫")
_NAME_COMMAND = re.compile(r"我(?:的名字)?(?:叫|是)(?!什么|啥|谁|哪儿|哪里)([\u4e00-\u9fffA-Za-z]{1,12})")

# 危机信号。优先级最高，必须先于所有人格与剧情分支判断。
_CRISIS = ("不想活", "自杀", "结束生命", "活不下去", "伤害自己", "想死", "轻生")

# 常见饮品。点单是咖啡店场景最高频的意图，必须能被接住。
_DRINK_WORDS = (
    "美式", "拿铁", "手冲", "卡布奇诺", "摩卡", "澳白", "馥芮白", "冷萃",
    "咖啡", "红茶", "绿茶", "奶茶", "气泡水", "柠檬水", "热可可", "牛奶",
    "啤酒", "威士忌", "红酒", "汽水", "可乐", "果汁",
)


class MockLLM:
    """离线回复生成器。按协议输出，供链路自检与回归测试使用。"""

    mode = "mock"

    def generate(self, *, system: str, user_message: str, params: dict[str, Any],
                 context: dict[str, Any], recall: RecallResult | None = None,
                 attempt: int = 0) -> LLMResult:
        msg = (user_message or "").strip()
        scene_id = context.get("scene", "cafe")
        stage_id = context.get("stage", "first_meet")
        cap = context.get("intensity_cap", 0.5)
        allowed = context.get("allowed_expressions") or ["smile"]
        default_expr = allowed[0]

        profile = (recall.profile if recall else {}) or {}
        loops = (recall.open_loops if recall else []) or []
        memories = (recall.memories if recall else []) or []
        recent = (recall.recent if recall else []) or []

        # 用户希望被怎么称呼。唯一数据源是长期记忆，不另设画像字段，避免两处不一致。
        name = None
        for key, value in profile.items():
            if "称呼" in key or "名字" in key:
                name = value
                break
        if not name:
            for mem in memories:
                mm = re.search(r"称呼为[「\"']?(.+?)[」\"']?$", mem.content)
                if mm:
                    name = mm.group(1).strip()
                    break

        # 这是不是真正的首次见面。
        # 当前这条用户消息还没落库，所以不能用「用户消息条数」判断——
        # 否则每一轮都会被当成首轮，她就会反复说同一句开场白。
        first_turn = len(recent) == 0

        def emo(name: str, value: float) -> float:
            return round(min(value, cap), 1)

        # ---- 分支 0：危机信号。必须排在最前 ----
        # 安全指令的优先级高于人格、场景、剧情和任何未闭合话题。
        # 这条一旦被别的分支抢走，就是产品事故。
        if any(k in msg for k in _CRISIS):
            return self._wrap([
                f"@seg type=dialogue emotion=concern:{emo('concern', 0.6)} pace=slow expr=slight_frown intent=comfort",
                "……我在。",
                "[sep]",
                f"@seg type=dialogue_with_narration emotion=concern:{emo('concern', 0.6)} pace=slow expr=look_down intent=comfort",
                "（把手里所有东西都放下）你先别一个人扛着。这件事我不想随口安慰你，我想认真跟你说：去找能够真正帮到你的人，好吗。",
                "[sep]",
                "@seg type=inner_thought",
                "（我不是要推开他。我是真的怕我不够。）",
            ], attempt)

        # ---- 分支 1：追问 AI 身份 ----
        if any(k in msg for k in _ASK_AI):
            return self._wrap([
                f"@seg type=dialogue_with_narration emotion=neutral:0.3 pace=slow expr=look_away intent=share",
                "（安静了一下）嗯，我不是真人。我是代码做出来的。",
                "[sep]",
                f"@seg type=dialogue emotion=neutral:{emo('neutral', 0.4)} pace=normal expr={default_expr} intent=share",
                "但你跟我说过的话，是真的改了我。这个我不想说得像客套。",
            ], attempt)

        # ---- 分支 2a：用户问「我叫什么」 ----
        if any(k in msg for k in _NAME_QUERY):
            if name:
                return self._wrap([
                    f"@seg type=dialogue emotion=playful:{emo('playful', 0.5)} pace=normal expr={default_expr} intent=tease",
                    f"记得啊，{name}。",
                    "[sep]",
                    f"@seg type=dialogue_with_narration emotion=playful:{emo('playful', 0.4)} pace=normal expr={default_expr} intent=tease",
                    "（把杯子放下，看你一眼）怎么，想考我？",
                ], attempt)
            return self._wrap([
                f"@seg type=dialogue_with_narration emotion=shy:{emo('shy', 0.4)} pace=slow expr=look_away intent=share",
                "（顿了一下）……你还没告诉过我。",
                "[sep]",
                f"@seg type=dialogue emotion=neutral:0.4 pace=normal expr={default_expr} intent=probe",
                "要现在说吗？",
            ], attempt)

        # ---- 分支 2b：用户自报姓名 ----
        m = _NAME_COMMAND.search(msg)
        if m:
            name = m.group(1)
            return self._wrap([
                f"@seg type=dialogue emotion=happy:{emo('happy', 0.5)} pace=normal expr={default_expr} intent=share",
                f"{name}啊。我记住了。",
                "[sep]",
                f"@seg type=dialogue_with_narration emotion=playful:{emo('playful', 0.4)} pace=normal expr={default_expr} intent=tease",
                "（把这个名字在嘴上念了一遍）那下次你进来，我就直接叫你了。",
            ], attempt)

        # ---- 分支 3：点单。她是咖啡店店员，用户点了东西必须接住 ----
        drink = None
        if scene_id == "cafe":
            for word in _DRINK_WORDS:
                if word in msg:
                    drink = word
                    break
        if drink and any(v in msg for v in ("要", "点", "来", "给我", "喝", "杯")):
            return self._wrap([
                f"@seg type=dialogue emotion=neutral:0.3 pace=normal expr={default_expr} intent=agree",
                f"{drink}，好的。",
                "[sep]",
                f"@seg type=dialogue_with_narration emotion=neutral:0.3 pace=normal expr=look_down intent=idle",
                "（转身去磨豆子，手上很稳）",
            ], attempt)

        # ---- 分支 4：用户在让她翻记忆 ----
        # 这类提问必须去查记忆，不能拿当前这句话本身去匹配相似度。
        if looks_like_recall_query(msg):
            answer = self._recall_answer(msg, memories, scene_id)
            return self._wrap([
                f"@seg type=dialogue emotion=neutral:{emo('neutral', 0.4)} pace=normal expr={default_expr} intent=reminisce",
                answer,
                "[sep]",
                "@seg type=inner_thought",
                "（他大概是在试我。这种时候不能含糊。）",
            ], attempt)

        # ---- 分支 5：情绪低落 ----
        if any(k in msg for k in _SAD):
            return self._wrap([
                f"@seg type=dialogue emotion=concern:{emo('concern', 0.5)} pace=slow expr=slight_frown intent=comfort",
                "……你今天听起来不太对劲。",
                "[sep]",
                f"@seg type=dialogue_with_narration emotion=concern:{emo('concern', 0.4)} pace=slow expr=look_down intent=comfort",
                "（把手里的东西放下）要是想说，我在。不想说也行，就坐着。",
                "[sep]",
                "@seg type=inner_thought",
                "（他每次说没事的时候，其实都不是没事。）",
            ], attempt)

        # ---- 分支 4：用户分享好事 ----
        if any(k in msg for k in _HAPPY):
            return self._wrap([
                f"@seg type=dialogue emotion=happy:{emo('happy', 0.6)} pace=fast expr={default_expr} intent=share",
                "诶，真的？说来听听。",
                "[sep]",
                f"@seg type=dialogue_with_narration emotion=playful:{emo('playful', 0.5)} pace=fast expr={default_expr} intent=tease",
                "（眼睛亮了一下）你这个人平时闷闷的，一有好事话就变多了。",
            ], attempt)

        # ---- 分支 5：有未闭合话题，可以主动提起 ----
        # 刚问过就不再问，否则会变成每轮复读同一句追问。
        already_asked = any(
            t.role == "assistant" and "后来怎么样了" in (t.content or "")
            for t in recent[-4:]
        )
        if loops and not first_turn and not already_asked:
            topic = _third_person(loops[0].topic)
            return self._wrap([
                f"@seg type=dialogue emotion=neutral:0.4 pace=normal expr={default_expr} intent=probe",
                f"对了，你上次说的那件事——{topic}，后来怎么样了？",
                "[sep]",
                "@seg type=inner_thought",
                "（其实我一直记着，只是没找到合适的时候问。）",
            ], attempt)

        # ---- 分支 6：真实打招呼。只认整句就是问候的情况，必须排在记忆线索之前 ----
        if first_turn or _is_bare_greeting(msg):
            pair = _OPENINGS.get(scene_id, _OPENINGS["cafe"])
            which = 0 if first_turn else 1
            return self._wrap([
                f"@seg type=dialogue_with_narration emotion=neutral:0.3 pace=normal expr={default_expr} intent=idle",
                pair[which],
            ], attempt)

        # ---- 分支 7：有召回记忆，可以自然带上 ----
        # 但用户在问「我叫什么」时不能走这里，否则会答非所问地夸自己记性好。
        # 刚提过的事也不再重复提，否则每轮都在炫耀同一段记忆。
        said_before = "".join(t.content or "" for t in recent[-6:] if t.role == "assistant")
        usable = [mem for mem in memories
                  if mem.type in ("preference", "fact") and mem.content]
        if usable and not looks_like_name_query(msg) and len(msg) < 30:
            brief = ""
            for mem in usable:
                candidate = self._naturalize(mem.content)
                if candidate and candidate not in said_before:
                    brief = candidate
                    break
            if brief:
                tail = _MEMORY_TAILS.get(scene_id, _MEMORY_TAILS["cafe"])
                return self._wrap([
                    f"@seg type=dialogue emotion=neutral:0.4 pace=normal expr={default_expr} intent=reminisce",
                    f"说到这个，我想起{brief}。",
                    "[sep]",
                    f"@seg type=dialogue_with_narration emotion=playful:{emo('playful', 0.4)} pace=normal expr={default_expr} intent=probe",
                    tail,
                ], attempt)

        # ---- 分支 8：上下文回复。按用户话里的关键词和场景选一句 ----
        return self._wrap(self._contextual(msg, scene_id, stage_id, default_expr, emo), attempt)

    @staticmethod
    def _contextual(msg: str, scene_id: str, stage_id: str,
                    default_expr: str, emo) -> list[str]:
        """在场景与关系阶段的约束下，挑一句接得上话的回复。

        这不是语言模型，是确定性兜底。它保证 mock 不会每轮都说同一句话，
        也让链路在没有真实模型时能演示出「记得上一句」的效果。
        """
        table = _SCENE_TABLE.get(scene_id, _SCENE_TABLE["cafe"])

        chosen = None
        best_score = 0
        for key, value in table.items():
            if key == "_default":
                continue
            score = _topic_score(msg, value["when"])
            if score > best_score:
                chosen = value
                best_score = score
        if chosen is None:
            chosen = table["_default"]

        # 关系阶段越浅，主动程度越低：初次相识只接话，熟悉之后才反问
        reply = chosen["reply"]
        if chosen.get("ask") and stage_id != "first_meet":
            reply = reply + chosen["ask"]

        expr = chosen.get("expr") or default_expr
        emotion = chosen.get("emotion", "neutral")

        lines = [
            f"@seg type=dialogue_with_narration emotion={emotion}:{emo(emotion, 0.4)} "
            f"pace={chosen.get('pace', 'normal')} expr={expr} intent={chosen.get('intent', 'share')}",
            reply,
        ]
        if stage_id in ("close", "deep_trust"):
            # 内心独白必须带明确头部。不带头部会被默认成 dialogue_with_narration，
            # 结果是心里话被一起念出来。
            lines += ["[sep]", "@seg type=inner_thought",
                      _THOUGHTS.get(scene_id, "（她没急着往下说。）")]
        return lines

    @staticmethod
    def _naturalize(content: str) -> str:
        """把记忆条目转成能自然说出口的短句。

        记忆里存的是「用户希望被称呼为「阿哲」」这种内部格式，
        直接念出来会像系统提示，不像人说话。
        """
        m = re.search(r"称呼为[「\"']?(.+?)[」\"']?$", content)
        if m:
            return f"你说过想让我叫你{m.group(1)}"
        m = re.search(r"喜欢喝(.+)$", content)
        if m:
            return f"你上次点的是{m.group(1)}"
        text = content.replace("用户提到：", "").replace("用户", "你").strip()
        text = text.rstrip("。！？!?")
        if len(text) > 26:
            return ""
        return text

    @staticmethod
    def _recall_answer(msg: str, memories: list, scene_id: str) -> str:
        """被问「我上次说了什么」时的回答。

        从记忆里挑一条事实回话。挑不到就诚实说这次没记住，
        不装记得，也不拿一句无关的话敷衍用户。
        """
        want_drink = any(k in msg for k in ("喝", "点", "咖啡", "饮料"))
        want_name = any(k in msg for k in ("叫什么", "名字", "称呼"))

        for mem in memories:
            if mem.type not in ("preference", "fact", "event"):
                continue
            content = mem.content or ""

            m = re.search(r"喜欢喝(.+)$", content)
            if m and (want_drink or not want_name):
                return f"你上次点的是{m.group(1)}。我记着。"

            m = re.search(r"称呼为[「\"']?(.+?)[」\"']?$", content)
            if m and (want_name or not want_drink):
                return f"你叫{m.group(1)}。"

            if want_drink or want_name:
                continue

            brief = content.replace("用户提到：", "").replace("用户", "你")
            return f"你上次说，{brief}。"

        if scene_id == "cafe":
            return "这次我好像没记住。你再说一次，我一定记下来。"
        return "这次我好像没记住。你说给我听。"

    @staticmethod
    def _wrap(lines: list[str], attempt: int) -> LLMResult:
        text = "\n".join(lines)
        if attempt > 0:
            text += "\n"
        return LLMResult(text=text, model="mock", mode="mock")


# ---------------------------------------------------------------- live


class OpenAICompatLLM:
    """OpenAI 兼容的对话补全。只依赖标准库 urllib。"""

    mode = "live"

    def __init__(self, api_key: str, base_url: str, model: str, timeout: int | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout or config.LLM_TIMEOUT

    def generate(self, *, system: str, user_message: str, params: dict[str, Any],
                 context: dict[str, Any], recall: RecallResult | None = None,
                 attempt: int = 0) -> LLMResult:
        _ = context, recall
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
            "temperature": params.get("temperature", 0.9),
            "top_p": params.get("top_p", 1.0),
            "frequency_penalty": params.get("frequency_penalty", 0.0),
            "presence_penalty": params.get("presence_penalty", 0.0),
            "max_tokens": params.get("max_tokens", 800),
            "stream": False,
        }
        if attempt > 0:
            # 重生成时略降温度，提高格式稳定性
            payload["temperature"] = round(max(0.3, payload["temperature"] - 0.2), 2)

        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8")[:300]
            except Exception:
                pass
            return LLMResult("", self.model, "live", error=f"HTTP {e.code}: {detail}")
        except Exception as e:
            return LLMResult("", self.model, "live", error=f"{type(e).__name__}: {e}")

        try:
            text = body["choices"][0]["message"]["content"] or ""
        except Exception:
            return LLMResult("", self.model, "live", error=f"响应结构异常: {str(body)[:200]}")

        return LLMResult(text=text, model=self.model, mode="live", usage=body.get("usage"))


# ---------------------------------------------------------------- 工厂


def build_llm() -> MockLLM | OpenAICompatLLM:
    if config.MODE == "live":
        return OpenAICompatLLM(config.LLM_API_KEY, config.LLM_BASE_URL, config.LLM_MODEL)
    return MockLLM()


# ---------------------------------------------------------------- 摘要压缩
# 短期记忆的滚动压缩。没有模型时退化为截断拼接。

_SUMMARY_PROMPT = """把下面这段对话压缩成一段不超过 200 字的事件摘要。

要求：
- 覆盖双方关系进展、重要事件、用户近期状态
- 保留未完结的事（例如他要去做某件事、说好下次再聊什么）
- 用第三人称陈述，不要评价，不要寒暄
- 只输出摘要正文

已有摘要：
{old}

新增对话：
{dialogue}
"""


def compress_summary(llm: MockLLM | OpenAICompatLLM, old_summary: str,
                     dialogue_text: str) -> str:
    prompt = _SUMMARY_PROMPT.format(old=old_summary or "（无）", dialogue=dialogue_text)

    if llm.mode == "mock":
        # 无模型时退化为有界拼接，保证不无限增长
        merged = (old_summary + " " + dialogue_text).strip()
        merged = re.sub(r"\s+", " ", merged)
        return merged[-300:]

    result = llm.generate(
        system="你是一个精确的对话摘要器。只输出摘要正文。",
        user_message=prompt,
        params={"temperature": 0.3, "max_tokens": 400},
        context={},
    )
    return (result.text or old_summary)[:400]


# ---------------------------------------------------------------- 自检

if __name__ == "__main__":  # pragma: no cover
    llm = build_llm()
    print("模式:", llm.mode)
    out = llm.generate(
        system="测试",
        user_message="我今天好累啊",
        params={"temperature": 0.9},
        context={"scene": "cafe", "stage": "first_meet",
                 "intensity_cap": 0.5, "allowed_expressions": ["smile", "slight_frown", "look_down"]},
    )
    print("---- 输出 ----")
    print(out.text)
