# 02 · 台词 / 旁白协议

版本 segproto/1。本文是语言内核与下游（TTS、形象驱动）之间的唯一契约，冻结后变更需升版本号并通知下游。

## 设计目标

分离"说什么"与"怎么说"；旁白绝不朗读；可机械解析；每个字段取值范围封闭可校验；不绑定任何 TTS 或形象驱动厂商；同一个情绪标签既喂语音也喂表情。

## 类比

这就是舞台剧本。台词是演员念出来的，舞台指示是给演员和导演看的。区别只是下游不是人类演员，所以舞台指示被拆成了机器可用的字段。

## 数据结构

顶层：

```json
{
  "protocol": "segproto/1",
  "turn_id": "t_xxx",
  "character_id": "elise",
  "scene_id": "cafe",
  "stage": "first_meet",
  "segments": [],
  "dialogue": "纯台词拼接",
  "narration": {},
  "subtitle": [],
  "meta": {"created_at": "", "model": "", "parse_status": "ok", "warnings": []}
}
```

dialogue 是纯台词拼接，下游 TTS 只读这个字段，不需要理解 segments。narration 是整轮旁白汇总。parse_status 取 ok / repaired / degraded，让下游知道这轮质量。

单个 segment：

| 字段 | 说明 |
|---|---|
| index | 顺序，从 0 开始 |
| type | 见下节 |
| text | 文本内容 |
| spoken | 是否朗读。这是下游唯一需要判断的布尔值 |
| narration | 旁白，无则为 null |
| raw | 未解析原文，仅解析失败时有值，供调试 |

## segment 类型

| type | 含义 | spoken |
|---|---|---|
| dialogue | 只有台词 | true |
| dialogue_with_narration | 台词加旁白 | true |
| action | 动作表情描写，无可念文本 | false |
| scene | 环境描写 | false |
| condition | 剧情推进或氛围 | false |
| inner_thought | 内心独白 | false |
| aside | 旁白性插话 | false |

内心独白默认不朗读。它是给用户看的，不是给她说的，真人不会把心里话念出来。需要朗读时由上游显式设 spoken 为 true。

## 旁白字段

所有枚举封闭。模型输出枚举外的值由解析器映射到最近的合法值并记 warning，不报错，因为报错会打断对话。

| 字段 | 取值 |
|---|---|
| emotion | 数组，最多两项，每项为 name 加 intensity |
| emotion.name | 封闭 8 个情绪，见下 |
| emotion.intensity | 0.0 到 1.0，步长 0.1 |
| pace | very_slow / slow / normal / fast / very_fast |
| pauses | none / short / sentence_end / paragraph / hesitation |
| emphasis | 字符串数组，每项必须是 text 的子串 |
| expression | 表情数组，受场景卡白名单约束 |
| intent | comfort / tease / probe / share / invite / deflect / refuse / agree / reminisce / idle |

emphasis 必须是 text 子串，否则下游无法定位重读哪个字。这条可自动校验。

## 情绪表（封闭，第一期只允许这 8 个）

neutral 平静、happy 开心、concern 关切、sad 难过、playful 俏皮、shy 害羞心虚、annoyed 不悦、surprised 惊讶。

强度语义：0.1 到 0.3 隐约，几乎不影响语调；0.4 到 0.6 明显，语调可辨；0.7 到 0.9 强烈；1.0 极点，仅允许在深度信任阶段。

关系阶段越浅，允许的强度上限越低。相识阶段出现 1.0 是穿帮。

## 表情表

合法取值不是全局固定，而是全局表情表与当前场景卡白名单的交集。理由是咖啡店能做的动作和游乐园不一样，素材是按场景拍的。

解析时模型输出了白名单外的表情，替换为该场景的 default 并记 warning。

全局标签：smile 系、frown 系、视线系（look_down / look_away / glance_up）、姿态系（tilt_head / lean_forward / lean_back）、眼睛系（blink_slow / eyes_wide）、头部动作（nod / shake_head）、手部（hand_to_face / fidget）。

## 模型输出格式

不让模型直接输出 JSON：转义易错、更慢更贵、且双轨内容需要可读性，内容团队要能直接读改。

让模型输出极简行协议，由我们做确定性解析：

```
@seg type=dialogue emotion=concern:0.5 pace=slow expr=slight_frown,lean_forward intent=comfort
……啊，你今天听起来不太对劲。怎么了？
[sep]
@seg type=dialogue emotion=concern:0.4 pace=normal expr=look_down
（低头搅了搅杯子）……要是想说，我在。
[sep]
@seg type=inner_thought
（其实我有点怕他说完就走了。）
```

规则：头部行以 @seg 开头，key=value 用空格分隔；缺 type 时默认 dialogue_with_narration；不以 @seg 开头的段落也按默认类型处理；[sep] 独占一行作段落分隔；括号内文本自动识别为 action 或 scene 并从台词中剥离。

保留括号这个约定，是因为它是内容团队的书写直觉，剧本写法就是括号包动作。但括号内容的归属由解析器机械决定，不靠模型自律。

模型经常忘记写头部。此时按默认类型处理，expression 为空，只记 warning。能降级就不要失败。

## 校验规则

V1 至少有一个 spoken 段落。V2 emphasis 必须是 text 子串。V3 emotion.name 在情绪表内。V4 intensity 在 0 到 1。V5 expression 在场景白名单内。V6 dialogue 中不残留括号或 @seg。V7 段数在 1 到 6 之间。V8 intensity 不超过当前阶段上限。V9 台词长度不超上限。V10 台词不含禁用词。

长度上限取自 `persona.STYLE_ANCHOR`（当前：单段 100 字、整轮 400 字）。它是唯一的数值来源，改那里即可，不要在两处各写一份。

V1、V6、V10 是硬失败需要重生成，其余软降级。

防「没话找话」的补充规则，同样机械判定：

- A1 不得出现填话句式（「你还在吗」「还需要别的吗」这类）
- A2 用户提问时不得整轮反问；收尾与允许沉默的策略下不得整轮反问
- A3 不得逐字复述用户刚说过的话

**一条下游必须知道的例外**：编译器判定本轮不必多说或允许沉默时（`speak_policy` 为 `brief` 或 `may_silent`），只有动作、没有台词是**正确行为**，不算失败。收到 `spoken` 全为 false 的一轮是合法的，TTS 应当跳过该轮而非报错。

## 下游适配器边界

本协议不含任何声学或渲染参数。转换由下游做：

```
segproto/1 → TTS 适配器 → 豆包 SSML / Azure SSML / MiniMax 参数
           → 形象适配器 → AVTR 表情标签 / BlendShape 权重
```

比如 concern 强度 0.6 翻译成 TTS 的音高降 5% 语速 0.9 倍，形象侧翻译成 slight_frown 权重 0.6。inner_thought 则跳过不合成。

映射表由下游维护，但枚举值由本协议定义，加枚举必须双边确认。

## 版本化

加枚举值或加可选字段是 minor 变更，下游需补映射。改字段语义或删字段是 major，必须双边同时上线。

## 待确认

下游 TTS 是整段合成还是逐段流式合成，决定段落要不要带时间戳。形象驱动需不需要逐段对齐，若需要则段落必须带时间轴。inner_thought 是否以字幕形式呈现给用户。是否需要镜头维度。
