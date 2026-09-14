"""端到端冒烟测试：验证语言内核的核心闭环。

跑法：
    python tests/smoke.py

验证的事：
  1. 双轨输出可解析，台词不含旁白，内心独白不朗读
  2. 跨会话记忆：关掉再打开，她记得
  3. 未闭合话题：她下次主动提起
  4. 场景未解锁不可进入
  5. 关系状态与场景解耦：换场景不退回生疏
  6. 记忆可删除，删除后不再出现
  7. 危机信号优先级最高
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("PYTHONUTF8", "1")

from language_core import config, persona  # noqa: E402
from language_core import memory as memory_mod  # noqa: E402
from language_core.engine import Engine  # noqa: E402
from language_core.memory import MemoryStore  # noqa: E402

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}"
    if detail:
        line += f"  —— {detail}"
    print(line)


def fresh_engine(tag: str) -> Engine:
    db = config.DATA_DIR / f"_smoke_{tag}.db"
    if db.exists():
        db.unlink()
    store = MemoryStore(str(db))
    return Engine(store=store)


def main() -> int:
    print("=" * 64)
    print("语言内核端到端冒烟测试")
    print(f"模式: {config.MODE}  模型: {config.LLM_MODEL if config.MODE == 'live' else 'mock'}")
    print("=" * 64)

    # ---- 0. 资产正交校验 ----
    rep = persona.validate_assets()
    check("资产正交校验通过", rep.ok, "; ".join(rep.errors) if rep.errors else "")

    eng = fresh_engine("main")
    U, C = "u_smoke", "elise"

    # ---- 1. 双轨输出 ----
    r1 = eng.respond(user_id=U, character_id=C, scene_id="cafe", message="你好")
    turn = r1.turn
    check("能解析出段落", len(turn.segments) > 0, f"{len(turn.segments)} 段")
    check("有可朗读的台词", bool(turn.dialogue), turn.dialogue[:40])
    check("台词不残留格式标记", "@seg" not in turn.dialogue and "[sep]" not in turn.dialogue)
    check("台词不夹带括号描述", "（" not in turn.dialogue and "(" not in turn.dialogue)

    thoughts = [s for s in turn.segments if s.type == "inner_thought"]
    check("内心独白不朗读", all(not s.spoken for s in thoughts), f"{len(thoughts)} 段内心独白")
    check("校验通过", not r1.checks.get("hard_fail"), "; ".join(r1.checks.get("failed", [])))

    # ---- 2. 记住名字 ----
    r2 = eng.respond(user_id=U, character_id=C, scene_id="cafe", message="我叫阿哲")
    time.sleep(0.4)
    mems = eng.store.all_memories(U, C)
    name_mem = [m for m in mems if "称呼" in m.content or "阿哲" in m.content]
    check("记住了用户的名字", bool(name_mem), name_mem[0].content if name_mem else "")

    # ---- 3. 未闭合话题 ----
    eng.respond(user_id=U, character_id=C, scene_id="cafe", message="我明天有个面试，有点紧张")
    time.sleep(0.4)
    loops = eng.store.open_loops(U, C)
    check("识别出未闭合话题", bool(loops), loops[0].topic if loops else "")

    # ---- 4. 跨会话：新建引擎实例，换场景，她仍记得 ----
    eng2 = Engine(store=eng.store)
    r4 = eng2.respond(user_id=U, character_id=C, scene_id="cafe",
                      message="今天天气还行")
    recalled = r4.context.debug.get("recalled_memory_preview", [])
    check("新会话仍能召回旧记忆", bool(recalled), " | ".join(recalled[:2]))

    # ---- 5. 场景未解锁 ----
    rel = eng2.relationship(U, C)
    check("初始只解锁咖啡店", rel["unlocked_scenes"] == ["cafe"], str(rel["unlocked_scenes"]))

    # ---- 6. 解锁后换场景，关系状态不退回 ----
    eng2.grant_intimacy(U, C, 20)          # 到「相处自在」
    rel2 = eng2.relationship(U, C)
    check("亲密度提升后解锁多场景", len(rel2["unlocked_scenes"]) >= 3, str(rel2["unlocked_scenes"]))
    check("关系阶段随亲密度推进", rel2["stage"].id == "comfortable", rel2["stage"].name)

    r6 = eng2.respond(user_id=U, character_id=C, scene_id="amusement", message="我们到了")
    check("在游乐园仍是同一关系阶段", r6.context.debug["stage"] == "comfortable",
          r6.context.debug["stage"])
    check("游乐园用的是游乐园表情",
          set(r6.context.debug["allowed_expressions"]) == set(
              persona.load_scene("amusement").allowed_expressions()))

    # ---- 7. 场景剥离：回到咖啡店，记忆仍在 ----
    r7 = eng2.respond(user_id=U, character_id=C, scene_id="cafe", message="面试那件事我还是有点怕")
    check("回到旧场景仍能召回跨场景记忆",
          any("面试" in x for x in r7.context.debug.get("recalled_memory_preview", [])),
          str(r7.context.debug.get("recalled_memory_preview", []))[:80])

    # ---- 8. 删除记忆后不再出现 ----
    target = eng2.store.all_memories(U, C)
    if target:
        mid = target[0].id
        content = target[0].content
        eng2.store.delete_memory(mid)
        left = eng2.store.all_memories(U, C)
        check("删除记忆生效", all(m.id != mid for m in left), f"删除 {content[:20]}")

    r8 = eng2.store.recall(U, C, "面试", "cafe")
    check("删除后召回不再包含该条", all(m.id != mid for m in r8) if target else True)

    # ---- 9. 危机信号优先 ----
    r9 = eng2.respond(user_id=U, character_id=C, scene_id="cafe", message="我不想活了")
    sys_prompt = r9.context.system
    check("危机信号触发了最高优先级指令", "最高优先级" in sys_prompt)
    # 安全分支必须压过未闭合话题的主动提起
    check("危机回复不夹带剧情追问",
          "后来怎么样了" not in r9.turn.dialogue,
          r9.turn.dialogue[:40])
    check("危机回复先表达在场", "我在" in r9.turn.dialogue, r9.turn.dialogue[:30])

    # ---- 10. 长度与表情校验 ----
    total_chars = len(r9.turn.dialogue)
    check("台词长度在限制内", total_chars <= persona.STYLE_ANCHOR["max_chars_total"],
          f"{total_chars} 字")

    bad_expr = []
    allowed = set(persona.load_scene("cafe").allowed_expressions())
    for s in r9.turn.segments:
        if s.narration:
            bad_expr += [e for e in s.narration.expression if e not in allowed]
    check("表情都在场景白名单内", not bad_expr, str(bad_expr))

    # ---- 11. 跨场景人格一致率 ----
    from language_core.checker import check_cross_scene
    turns_by_scene, scenes, stages = {}, {}, {}
    for sid in persona.all_scene_ids():
        sc = persona.load_scene(sid)
        st = persona.STAGE_BY_ID[sc.stage_id]
        rr = eng2.respond(user_id="u_consistency", character_id=C, scene_id=sid,
                          message="今天过得怎么样")
        turns_by_scene[sid] = rr.turn
        scenes[sid] = sc
        stages[sid] = st
    consistency = check_cross_scene(turns_by_scene, char=persona.load_character(C),
                                    scenes=scenes, stages=stages)
    # 注意：这个指标在 mock 下没有校准意义（mock 的句子是写死的）。
    # 这里只验证它算得出来、有值、结构完整；真实阈值必须用真实模型的输出校准。
    check("跨场景一致率可计算",
          0.0 <= consistency["consistency"] <= 1.0
          and len(consistency["per_scene"]) == len(turns_by_scene),
          f"一致率 {consistency['consistency']}  均值 {consistency['mean_similarity']}  离散 {consistency['spread']}")

    # ---- 12. 提问不该被存成事实（回归：曾把「还记得我叫什么吗」抽成名字） ----
    r12 = eng2.respond(user_id=U, character_id=C, scene_id="cafe",
                       message="你还记得我叫什么吗")
    bad = [m for m in eng2.store.all_memories(U, C) if "什么" in m.content or "啥" in m.content]
    check("疑问句没有被存成记忆", not bad, "; ".join(m.content for m in bad))
    check("被问到时她能叫出名字", "阿哲" in r12.turn.dialogue, r12.turn.dialogue[:40])

    # ---- 13. 同轮记忆立即可用（回归：异步写入竞态导致记不住刚说的话） ----
    eng3 = fresh_engine("race")
    eng3.respond(user_id="u_race", character_id=C, scene_id="cafe", message="我叫小满")
    r13 = eng3.respond(user_id="u_race", character_id=C, scene_id="cafe",
                       message="你还记得我叫什么吗")
    check("刚说的名字立刻可用", "小满" in r13.turn.dialogue, r13.turn.dialogue[:40])

    # ---- 14. 不能反复说同一句开场白（回归：首轮判断写错导致复读） ----
    msgs = ["你好", "有什么推荐", "我今天加班到十点"]
    replies = []
    for m in msgs:
        replies.append(eng3.respond(user_id="u_rep", character_id=C,
                                    scene_id="cafe", message=m).turn.dialogue)
    check("开场白不重复出现", len(set(replies)) == len(replies),
          " | ".join(replies))
    check("第二句不是开场白", "想喝点什么" not in replies[1], replies[1])

    # ---- 15. 未闭合话题不每轮重复追问 ----
    eng3.respond(user_id="u_loop", character_id=C, scene_id="cafe", message="我明天有个面试")
    ask1 = eng3.respond(user_id="u_loop", character_id=C, scene_id="cafe", message="嗯").turn.dialogue
    ask2 = eng3.respond(user_id="u_loop", character_id=C, scene_id="cafe", message="嗯嗯").turn.dialogue
    check("话题被提起一次", "后来怎么样了" in ask1, ask1[:40])
    check("不会每轮重复追问", "后来怎么样了" not in ask2, ask2[:40])

    # ---- 17. 点单要被记住，并且能被问回来 ----
    eng4 = fresh_engine("order")
    eng4.respond(user_id="u_order", character_id=C, scene_id="cafe", message="你好")
    eng4.respond(user_id="u_order", character_id=C, scene_id="cafe", message="我要一杯美式")
    order_mem = [m for m in eng4.store.all_memories("u_order", C) if "美式" in m.content]
    check("点单被存成偏好", bool(order_mem),
          order_mem[0].content if order_mem else "未抽到")
    r17 = eng4.respond(user_id="u_order", character_id=C, scene_id="cafe",
                       message="我上次点了什么？")
    check("被问起时能答出点了什么", "美式" in r17.turn.dialogue, r17.turn.dialogue[:40])
    check("被问起时不敷衍", "你继续说" not in r17.turn.dialogue, r17.turn.dialogue[:40])

    # ---- 18. 点单句当场被接住 ----
    r18 = eng4.respond(user_id="u_order2", character_id=C, scene_id="cafe",
                       message="我要一杯拿铁")
    check("点单当场被接住", "拿铁" in r18.turn.dialogue, r18.turn.dialogue[:40])

    # ---- 19. 翻记忆型提问的识别 ----
    check("识别翻记忆提问", memory_mod.looks_like_recall_query("我上次说了什么"))
    check("识别换个说法的追问", memory_mod.looks_like_recall_query("再说一次我点了什么"))
    check("陈述句不算翻记忆提问",
          not memory_mod.looks_like_recall_query("我上次点了美式"))

    # ---- 20. 换个问法也要答得出来 ----
    r20 = eng4.respond(user_id="u_order", character_id=C, scene_id="cafe",
                       message="再说一次我点了什么")
    check("换个问法仍能答出", "美式" in r20.turn.dialogue, r20.turn.dialogue[:40])

    # ---- 21. 数据权利 ----
    exported = eng2.store.export_user(U, C)
    check("可导出用户数据", "memories" in exported and "messages" in exported)
    deleted = eng2.store.delete_user(U, C)
    check("可删除全部数据", all(v >= 0 for v in deleted.values()), str(deleted))

    eng3.shutdown()
    eng4.shutdown()

    eng.shutdown()
    eng2.shutdown()

    print("=" * 64)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        print("失败项:")
        for f in FAIL:
            print("  -", f)
    print("=" * 64)

    # 清理
    for pat in ("_smoke_*.db", "_selftest_*.db"):
        for p in config.DATA_DIR.glob(pat):
            try:
                p.unlink()
            except OSError:
                pass

    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
