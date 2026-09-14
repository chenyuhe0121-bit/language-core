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

下游拿到 dialogue 合成语音，拿到 narration 驱动语气、表情、动作。两者严格分离。

## 为什么单独建这个项目

FLP 解决了传输、形象驱动和语音，companion-prototype 解决了界面，两者都没有记忆和角色系统。语言系统是唯一缺失且最影响留存的一环，必须独立于视频链路先成熟。

## 文档

| 文档 | 内容 |
|---|---|
| docs/00-vision.md | 目标、边界、设计原则、成功标准 |
| docs/01-tech-stack.md | 八项技术决策与理由 |
| docs/02-segment-protocol.md | 台词/旁白协议 segproto/1 |
| docs/03-architecture.md | 八个环节、五张卡、记忆四层、校验器 |

## 运行

```bash
python run.py
# http://127.0.0.1:8420
```

零第三方依赖，任何装了 Python 3.9 以上的机器直接能跑。

模型配置放在 `data/llm.env`（该目录不进版本库，key 不会跟着代码走）：

```
LANGUAGE_CORE_LLM_API_KEY=sk-xxx
LANGUAGE_CORE_LLM_BASE_URL=https://api.deepseek.com/v1
LANGUAGE_CORE_LLM_MODEL=deepseek-flash
```

三种启动方式：

```bash
python run.py                 # 有配置走真实模型，没配置走离线 mock
python run.py --check         # 只验证模型连通性，不启动服务
python run.py --mock          # 强制离线，用于跑测试和离线演示
```

`data/llm.env` 不存在时自动降级为 mock，不会报错。mock 的回复是按协议写死的，
只用于验证管道；真实对话必须接模型。

想让服务直接听环境变量也可以，不建文件即可：

```bash
set LANGUAGE_CORE_LLM_API_KEY=sk-xxx
python -m language_core.server
```

## 看看真实模型收到什么

```bash
python tools/capture_prompt.py
```

它起一个本地假模型服务接收请求，把完整的提示词打印到
`data/_captured_prompt.txt`。用来确认人设、场景、召回记忆是不是真的注入了。

## 自检

```bash
python -m language_core.persona    # 资产正交校验
python -m language_core.segment    # 协议解析自检
python -m language_core.memory     # 记忆系统自检
python tests/smoke.py              # 端到端冒烟测试
```

## 目录

```
language-core/
├─ docs/              规格文档
├─ language_core/
│   ├─ segment.py     协议解析与校验
│   ├─ compiler.py    上下文编译器
│   ├─ memory.py      记忆系统（唯一写 SQL 的地方）
│   ├─ llm.py         模型适配层
│   ├─ checker.py     校验器
│   ├─ persona.py     资产加载与正交校验
│   ├─ engine.py      一轮对话的编排
│   ├─ server.py      HTTP 服务（唯一可替换层）
│   ├─ assets/        五张卡的内容
│   └─ web/           调试界面
├─ tests/             端到端测试
└─ data/              运行期数据，不进版本库
```

## HTTP 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | /api/health | 健康检查与资产校验结果 |
| GET | /api/state | 当前场景、关系阶段、亲密度、已解锁场景 |
| GET | /api/scenes | 全部场景 |
| POST | /api/chat | 发一句话，返回台词加旁白与本轮决策 |
| POST | /api/scene | 切换场景，未解锁返回 403 |
| GET | /api/memory | 她记住的事与未闭合话题 |
| POST | /api/memory/delete | 删除单条记忆 |
| POST | /api/memory/update | 修改单条记忆 |
| GET | /api/export | 导出全部交互数据 |
| POST | /api/delete_all | 删除该角色下全部数据 |

## 文档写作约定

每份文档控制在两屏以内，只写结论和理由。不用强调语法，加粗只用于必须看见的一行。不写教程和背景铺垫，那些属于聊天记录不属于文档。
