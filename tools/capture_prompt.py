"""抓取真实发给大模型的提示词。

用一个本地假模型服务接收请求，把提示词原样打印出来。
不联网、不花钱，用来回答「接入真模型后它到底看到什么」。

跑法：
    python tools/capture_prompt.py
"""

from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CAPTURED: list[dict] = []


class Catcher(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        CAPTURED.append(body)

        reply = json.dumps({
            "id": "fake",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": (
                        "@seg type=dialogue_with_narration emotion=neutral:0.3 "
                        "pace=normal expr=smile intent=agree\n"
                        "美式，好的。\n[sep]\n"
                        "@seg type=action\n（转身去磨豆子，手上很稳）"
                    ),
                },
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1234, "completion_tokens": 56},
        }, ensure_ascii=False).encode("utf-8")

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)


def main() -> int:
    import os

    server = ThreadingHTTPServer(("127.0.0.1", 0), Catcher)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    os.environ["LANGUAGE_CORE_LLM_API_KEY"] = "fake-key"
    os.environ["LANGUAGE_CORE_LLM_BASE_URL"] = f"http://127.0.0.1:{port}/v1"
    os.environ["LANGUAGE_CORE_LLM_MODEL"] = "fake-model"

    # 必须在设置环境变量之后再导入，config 在导入时读取环境变量
    from language_core import config
    from language_core.engine import Engine
    from language_core.memory import MemoryStore

    db = config.DATA_DIR / "_capture.db"
    if db.exists():
        db.unlink()

    eng = Engine(store=MemoryStore(str(db)))
    U = "capture"

    print("=" * 70)
    print("先制造一些记忆，让提示词里有东西可看")
    print("=" * 70)
    for m in ["我叫阿哲", "我要一杯美式", "我明天有个面试"]:
        eng.respond(user_id=U, character_id="elise", scene_id="cafe", message=m)
        print(f"  用户: {m}")

    CAPTURED.clear()

    print()
    print("=" * 70)
    print("现在用户说一句，抓取真正发给大模型的完整内容")
    print("=" * 70)
    eng.respond(user_id=U, character_id="elise", scene_id="cafe",
                message="我上次点了什么？")
    eng.shutdown()
    server.shutdown()

    if not CAPTURED:
        print("没有抓到请求")
        return 1

    payload = CAPTURED[-1]
    messages = payload["messages"]

    out = []
    out.append(f"模型: {payload.get('model')}")
    out.append(f"参数: temperature={payload.get('temperature')} top_p={payload.get('top_p')} "
               f"frequency_penalty={payload.get('frequency_penalty')} "
               f"presence_penalty={payload.get('presence_penalty')} "
               f"max_tokens={payload.get('max_tokens')}")
    out.append(f"system 提示词长度: {len(messages[0]['content'])} 字符")
    out.append("")
    out.append("=" * 70)
    out.append("【system 提示词全文】")
    out.append("=" * 70)
    out.append(messages[0]["content"])
    out.append("")
    out.append("=" * 70)
    out.append("【用户这一句】")
    out.append("=" * 70)
    out.append(messages[1]["content"])

    text = "\n".join(out)
    dest = config.DATA_DIR / "_captured_prompt.txt"
    dest.write_text(text, encoding="utf-8")
    print(text)

    try:
        db.unlink()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
