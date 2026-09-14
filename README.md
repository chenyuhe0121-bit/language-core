# Language Core · 陪伴产品语言系统内核

Muse 产品的语言系统独立内核。负责角色怎么想、记得什么、怎么说，不负责画面和声音。

输入用户一句话，输出一段带表演指令的台词，并且记得住他。

```json
{
  "dialogue": "你今天听起来不太对劲，怎么了？",
  "narration": {
    "emotion": [{"name": "concern", "intensity": 0.6}],
    "pace": "slow",
    "expression": ["slight_frown", "lean_forward"]
  }
}
```

下游拿到 `dialogue` 合成语音，拿到 `narration` 驱动语气、表情、动作。两者严格分离。

## 为什么单独建这个项目

FLP 解决了传输、形象驱动和语音，companion-prototype 解决了界面，两者都没有记忆和角色系统。语言系统是唯一缺失且最影响留存的一环，必须独立于视频链路先成熟。

## 文档

| 文档 | 内容 |
|---|---|
| docs/00-vision.md | 目标、边界、设计原则、成功标准 |
| docs/01-tech-stack.md | 八项技术决策与理由 |
| docs/02-segment-protocol.md | 台词/旁白协议 segproto/1，**与下游的契约** |
| docs/03-architecture.md | 链路、提示词结构、卡格式、记忆、校验、缺口 |
| docs/04-reading-the-memory-system.md | 记忆系统代码导读 |
| docs/05-legacy-five-part-architecture.md | 历史设计存档（已不再生效） |
| docs/06-git-sync.md | 同步到 GitHub |

## 运行

```bash
python run.py
# http://127.0.0.1:8420
```

零第三方依赖，任何装了 Python 3.9 以上的机器直接能跑，不需要 pip 装任何东西。

模型配置放在 `data/llm.env`（该目录不进版本库，密钥不会跟着代码走）：

```
LANGUAGE_CORE_LLM_API_KEY=sk-xxx
LANGUAGE_CORE_LLM_BASE_URL=https://api.deepseek.com/v1
LANGUAGE_CORE_LLM_MODEL=deepseek-flash
```

三种启动方式：

```bash
python run.py           # 有配置走真实模型，没配置走离线管道演示
python run.py --check   # 只验证模型连通性，不启动服务
python run.py --mock    # 强制离线，用于跑测试
```

`data/llm.env` 不存在时会降级到离线模式，不会报错。**但离线模式的回复是按关键词写死的脚本，只能用来验证管道通不通，不能用来评判角色质量。** 页面右上角会显示当前是真实模型还是离线演示。

## 界面怎么用

打开 http://127.0.0.1:8420 后：

- **左侧**：角色列表（可新增）、会话列表（可新建）、记忆与未闭合话题
- **中间**：对话。每段台词下面有一排彩色标签，显示这一段的**情绪 / 语速 / 意图 / 表情**，情绪标签下的小横条长度就是强度
- **右上角**：切换场景、调整关系阶段、开关记忆
- **每条回复下方**：喜欢 / 不合适 / 详情 / 朗读
- **"＋ 增加角色"**：弹出表单，可手工填，也可直接导入 SillyTavern 的 JSON 角色卡

## HTTP 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 健康检查与资产校验结果 |
| GET | `/api/characters` | 角色列表 |
| POST | `/api/characters` | 新增角色（导入或手填，后端自动展开与校验） |
| GET | `/api/scenes` | 场景列表 |
| GET | `/api/sessions` | 会话列表 |
| POST | `/api/sessions` | 新建会话 |
| GET | `/api/state` | 当前状态 + 历史记录 + 记忆 |
| POST | `/api/chat` | 发一句话，整轮返回 |
| POST | `/api/chat/stream` | 发一句话，SSE 按段推送 |
| POST | `/api/chat/cancel` | 中断正在生成的回复 |
| POST | `/api/feedback` | 给某轮回复好评/差评与备注 |
| POST | `/api/session/settings` | 改场景、关系值、记忆开关 |
| POST | `/api/scene` | 同上，保留的旧别名 |
| GET | `/api/memory` | 记忆与未闭合话题 |
| POST | `/api/memory/delete` `/api/memory/update` | 删除 / 修改单条记忆 |
| GET | `/api/export` | 导出某个会话的全部数据 |

SSE 事件：`start`（开始）→ `segment`（逐段推送，含旁白）→ `done`（整轮 payload）或 `error`。

## 自检

```bash
python -m unittest tests.test_conversation -v   # 24 项结构性测试
python -m language_core.persona                 # 资产校验
python -m language_core.segment                 # 协议解析自检
python tools/capture_prompt.py                  # 抓取真实发给模型的完整提示词
python tools/quality_probe.py                   # 真模型质量探针（3 角色 × 6 轮追问）
```

## 目录

```
language-core/
├─ docs/                    规格文档
├─ language_core/
│   ├─ config.py            全部参数集中在这里
│   ├─ segment.py           台词/旁白协议：解析、校验、封闭枚举
│   ├─ memory.py            记忆与存储，唯一写 SQL 的地方
│   ├─ compiler.py          上下文编译器，拼提示词
│   ├─ llm.py               模型适配：真实接口 + 离线脚本
│   ├─ checker.py           输出校验与反填充
│   ├─ persona.py           角色卡/场景卡加载与关系阶段
│   ├─ cards.py             角色卡导入与展开
│   ├─ engine.py            一轮对话的编排
│   ├─ server.py            HTTP 与 SSE（唯一可替换的一层）
│   ├─ assets/              内置角色卡与场景卡
│   └─ web/                 前端界面
├─ tests/                   结构性测试
├─ tools/                   运维与调试工具
└─ data/                    运行期数据（密钥、数据库），不进版本库
```

## 下游接入要看什么

如果要把这个内核接到语音和形象驱动上，读两份就够：

1. **`docs/02-segment-protocol.md`** —— 契约本身。字段、枚举、校验规则、适配器边界
2. **`docs/03-architecture.md`** —— 一轮对话的链路，以及哪些能力还没接线

有一条要特别注意：**收到 `spoken` 全为 false 的一轮是合法的**（那一轮她只有动作没有台词），TTS 应当跳过而不是报错。

## 文档写作约定

每份文档控制在两屏以内，只写结论和理由。不用强调语法，加粗只用于必须看见的一行。不写教程和背景铺垫，那些属于聊天记录不属于文档。
