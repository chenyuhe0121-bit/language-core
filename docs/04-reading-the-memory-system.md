# 04 · 读懂记忆系统

给不想读全部代码的人。按「一句话从进到出」的顺序走，每一步给出文件和行号。

## 先记住四层

| 层 | 存什么 | 存多久 | 怎么用 | 表 |
|---|---|---|---|---|
| 工作记忆 | 当前对话原文 | 这通对话 | 全量进上下文 | `message` |
| 短期摘要 | 最近几天的浓缩印象 | 两周 | 常驻进上下文 | `summary` |
| 长期记忆 | 永久事实与经历 | 永久 | 按相关度召回 3 到 5 条 | `memory` |
| 未闭合话题 | 没结的事 | 有始有终 | 按时间主动提起 | `open_loop` |

另有一张 `profile` 表存用户画像键值对（亲密度、当前场景等），它不是记忆，是账号状态。

## 一句话的完整旅程

### 第 1 步：用户说话，先判断这句话是什么

`engine.py:38 classify_intent` — 规则分流。危机、安慰、提问、邀请各走不同策略。
危机信号会另外注入一段最高优先级指令，且不受预算裁剪。

### 第 2 步：抽取记忆，而且必须赶在召回之前

`engine.py:112 respond` 里这两行是顺序关键：

```python
extracted = self.writer.extract(message)
self._upsert_extracted(user_id, character_id, extracted, scene_id)
recall = self.store.recall_all(...)
```

- `memory.py:639 MemoryWriter.extract` — 同步抽取。只是本地正则，无 I/O。
- `engine.py:208 _upsert_extracted` — 同步落库，让它立刻可被召回。

**为什么必须同步**：用户说「我叫阿哲」，紧接着问「你还记得我叫什么吗」。
如果落库还在队列里，她就会答「你还没告诉过我」。用户判定为「她根本没记住我」。

`memory.py:566 extract_rule_based` 是抽取规则本体，能抽四类：
姓名、工作、偏好、事件，以及未闭合话题。

### 第 3 步：抽取里最容易被写错的一个判断

`memory.py:534 looks_like_name_query` — 区分「我叫阿哲」和「你还记得我叫什么吗」。

分不清的代价是双向的：把提问当成事实存下来，她会记错用户的名字；
把陈述当成提问，她永远记不住。这个函数同时被抽取和回复生成使用。

### 第 4 步：召回

`memory.py:449 recall_all` 是汇总入口，返回一个 `RecallResult`（`memory.py:134`）。
四样东西并行取回：

- `profile` — 画像，全量
- `memories` — `memory.py:345 recall` 打分召回
- `open_loops` — 未闭合话题，按状态取
- `summary` — 短期摘要，常驻
- `recent` — 最近 N 轮原文

### 第 5 步：打分怎么算

`memory.py:345 recall`，四项加权：

```
score = 关键词重合 × 0.55
      + 时间新鲜度 × 0.20      # 两周为一个衰减周期
      + 类型权重
      + 场景匹配加成 0.15
      + 置信度 × 0.10
```

低于 `config.RECALL_MIN_SCORE`（0.18）宁可不召回。不相关的记忆混进上下文比没召回更糟。

### 第 6 步：有一类记忆不参与打分

`memory.py:436 always_recall` — 高置信度的称呼类事实常驻注入。

**为什么**：用户问「你还记得我叫什么吗」，这句话和「用户希望被称呼为「阿哲」」
几乎没有字符重合，纯相似度召回必然失败。但这类事实漏掉就是信任崩塌，不能赌召回率。

### 第 7 步：注入上下文

`compiler.py:render_memory` 把召回结果渲染成提示词里的记忆段。
未闭合话题单独一节，因为它是唯一需要主动提起的记忆类型。

### 第 8 步：写入与压缩

- `engine.py:112` 回复返回后，`writer.submit` 把落库排进后台（此时已有抽取结果，不重复抽）
- `engine.py:223 _maybe_compress_summary` — 每 10 条消息压缩一次摘要

## 数据模型

```
profile(user_id, character_id, key, value)
message(id, user_id, character_id, scene_id, role, content, created_at)
summary(user_id, character_id, content, updated_at)
memory(id, user_id, character_id, type, content, confidence,
       source_scene, source_turn, created_at, expires_at, visibility)
open_loop(id, user_id, character_id, topic, expected_at, status,
          created_scene, created_at)
```

分区维度只有一个：`character_id`。场景是标签不是隔离墙。
`visibility` 第一期全填 `private`，为将来的跨角色认知预留。

## 用户数据权利

- `memory.py:471 export_user` — 导出全部交互数据
- `memory.py:483 delete_user` — 彻底删除
- `memory.py:333 delete_memory` — 删单条
- `memory.py:339 update_memory` — 改单条

删除必须真的生效，后续对话不能再出现该条。这是信任底线，测试里有专门用例。

## 已经踩过的坑（都写进了测试）

| 坑 | 现象 | 修法 |
|---|---|---|
| 异步写入竞态 | 刚说的名字，下一句问她就说不知道 | 抽取与关键落库改同步 |
| 疑问句被存成事实 | 她记错用户的名字 | `looks_like_name_query` |
| 关键事实召回失败 | 名字因零字符重合而召不回 | `always_recall` 常驻注入 |
| 首轮判断写错 | 每轮都重复同一句开场白 | 用 `recent` 是否为空判断 |
| 话题重复追问 | 每轮都问同一件未结的事 | 检查最近几轮是否已问过 |
| 未解锁信息泄露 | 相识阶段就聊家庭矛盾 | 只把公开信息写进固定前缀 |
| 点单句抽不到 | 用户点了美式，她说「你继续说」 | 加摄取类抽取规则，三种语序都覆盖 |
| 翻记忆提问不会查库 | 问「我上次说了什么」，她拿这句话本身去算相似度，必然召回失败 | `looks_like_recall_query` 触发主动检索 |

最后两条是同一类问题的两面：**用户提到一件事，和用户问起一件事，走的是两条不同的路径。**
前者靠抽取把信息存下来，后者靠识别意图去主动查。缺任何一条，用户都会觉得她没记住。

## 建议的读法

1. `memory.py:69-140` 先把四个数据类看完，这是全部概念
2. `engine.py:112-162` 看一句话怎么串起来，只有 50 行
3. `memory.py:345-375` 看召回打分，这是记忆系统的心脏
4. `memory.py:566-618` 看抽取规则
5. `tests/smoke.py` 看每个机制对应的断言，比读文档快
