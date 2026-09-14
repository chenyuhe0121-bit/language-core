"""HTTP 服务。

技术栈 D3：本层是唯一可替换的层。内核（segment / compiler / memory / llm）
全部纯标准库，换 FastAPI 时只需重写本文件的路由。
"""

from __future__ import annotations

import json
import mimetypes
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import config, persona
from .engine import Engine, classify_intent
from .persona import validate_assets

WEB_DIR = config.ROOT / "language_core" / "web"

_engine_lock = threading.Lock()
_engine: Engine | None = None


def engine() -> Engine:
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = Engine()
        return _engine


class Handler(BaseHTTPRequestHandler):
    server_version = "LanguageCore/0.1"

    # ---- 基础 ----

    def log_message(self, fmt: str, *args: Any) -> None:
        if config.MODE == "live" or True:
            print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200, ctype: str = "text/plain; charset=utf-8") -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _static(self, rel: str) -> None:
        path = (WEB_DIR / rel).resolve()
        if not str(path).startswith(str(WEB_DIR.resolve())) or not path.is_file():
            self._send_text("not found", 404)
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        self._send_text(path.read_text(encoding="utf-8"), 200, ctype)

    # ---- 路由 ----

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(parsed.query)

        if route == "/":
            self._static("index.html")
        elif route == "/app.js":
            self._static("app.js")
        elif route == "/app.css":
            self._static("app.css")
        elif route == "/api/health":
            self._send_json({
                "ok": True,
                "mode": config.MODE,
                "model": config.LLM_MODEL if config.MODE == "live" else "mock",
                "assets": validate_assets().to_dict(),
            })
        elif route == "/api/scenes":
            self._send_json({"scenes": [
                {
                    "id": s,
                    "name": persona.load_scene(s).name,
                    "stage": persona.load_scene(s).stage_id,
                    "goal": persona.load_scene(s).scene_goal,
                }
                for s in persona.all_scene_ids()
            ]})
        elif route == "/api/state":
            user_id = (query.get("user_id") or ["local"])[0]
            character_id = (query.get("character_id") or [config.DEFAULT_CHARACTER])[0]
            self._send_json(self._state(user_id, character_id))
        elif route == "/api/memory":
            user_id = (query.get("user_id") or ["local"])[0]
            character_id = (query.get("character_id") or [config.DEFAULT_CHARACTER])[0]
            self._send_json(self._memory_view(user_id, character_id))
        elif route == "/api/export":
            user_id = (query.get("user_id") or ["local"])[0]
            character_id = (query.get("character_id") or [config.DEFAULT_CHARACTER])[0]
            self._send_json(engine().store.export_user(user_id, character_id))
        else:
            self._send_json({"error": "not found", "route": route}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path.rstrip("/")
        body = self._read_json()

        if route == "/api/chat":
            self._chat(body)
        elif route == "/api/scene":
            self._switch_scene(body)
        elif route == "/api/memory/delete":
            self._delete_memory(body)
        elif route == "/api/memory/update":
            self._update_memory(body)
        elif route == "/api/delete_all":
            self._delete_all(body)
        else:
            self._send_json({"error": "not found", "route": route}, 404)

    # ---- 业务 ----

    def _ident(self, body: dict[str, Any]) -> tuple[str, str]:
        return (
            str(body.get("user_id") or "local"),
            str(body.get("character_id") or config.DEFAULT_CHARACTER),
        )

    def _state(self, user_id: str, character_id: str) -> dict[str, Any]:
        eng = engine()
        rel = eng.relationship(user_id, character_id)
        scene_id = eng.store.get_profile(user_id, character_id).get("current_scene", config.DEFAULT_SCENE)
        scene = persona.load_scene(scene_id)
        return {
            "user_id": user_id,
            "character": {"id": character_id, "name": persona.load_character(character_id).name},
            "scene": {"id": scene.id, "name": scene.name, "stage": scene.stage_id, "goal": scene.scene_goal},
            "stage": {
                "id": rel["stage"].id,
                "name": rel["stage"].name,
                "intimacy_cap": rel["stage"].intensity_cap,
                "body_language": rel["stage"].body_language,
            },
            "intimacy": rel["intimacy"],
            "unlocked_scenes": rel["unlocked_scenes"],
            "recent": [t.to_dict() for t in eng.store.recent_turns(user_id, character_id, 20)],
            "mode": config.MODE,
            "model": config.LLM_MODEL if config.MODE == "live" else "mock",
        }

    def _memory_view(self, user_id: str, character_id: str) -> dict[str, Any]:
        eng = engine()
        return {
            "profile": eng.store.get_profile(user_id, character_id),
            "memories": [m.to_dict() for m in eng.store.all_memories(user_id, character_id)],
            "open_loops": [l.to_dict() for l in eng.store.open_loops(user_id, character_id, statuses=("pending", "ask_now", "asked"))],
            "summary": eng.store.get_summary(user_id, character_id),
        }

    def _chat(self, body: dict[str, Any]) -> None:
        user_id, character_id = self._ident(body)
        message = str(body.get("message") or "").strip()
        if not message:
            self._send_json({"error": "message 为空"}, 400)
            return

        eng = engine()
        profile = eng.store.get_profile(user_id, character_id)
        scene_id = str(body.get("scene_id") or profile.get("current_scene") or config.DEFAULT_SCENE)

        # 场景必须是已解锁的
        rel = eng.relationship(user_id, character_id)
        if scene_id not in rel["unlocked_scenes"]:
            self._send_json({
                "error": "场景未解锁",
                "scene_id": scene_id,
                "unlocked": rel["unlocked_scenes"],
            }, 403)
            return

        result = eng.respond(
            user_id=user_id, character_id=character_id,
            scene_id=scene_id, message=message,
        )
        payload = result.to_dict()
        payload["intent"] = classify_intent(message)
        payload["state"] = {
            "scene_id": scene_id,
            "intimacy": rel["intimacy"],
            "stage": rel["stage"].name,
        }
        self._send_json(payload)

    def _switch_scene(self, body: dict[str, Any]) -> None:
        user_id, character_id = self._ident(body)
        scene_id = str(body.get("scene_id") or "")
        eng = engine()
        rel = eng.relationship(user_id, character_id)

        if scene_id not in persona.all_scene_ids():
            self._send_json({"error": f"未知场景 {scene_id}"}, 404)
            return
        if scene_id not in rel["unlocked_scenes"]:
            self._send_json({"error": "场景未解锁", "unlocked": rel["unlocked_scenes"]}, 403)
            return

        eng.store.set_profile(user_id, character_id, "current_scene", scene_id)
        self._send_json({"ok": True, "state": self._state(user_id, character_id)})

    def _delete_memory(self, body: dict[str, Any]) -> None:
        memory_id = str(body.get("memory_id") or "")
        ok = engine().store.delete_memory(memory_id)
        self._send_json({"ok": ok, "memory_id": memory_id})

    def _update_memory(self, body: dict[str, Any]) -> None:
        memory_id = str(body.get("memory_id") or "")
        content = str(body.get("content") or "").strip()
        if not content:
            self._send_json({"error": "content 为空"}, 400)
            return
        ok = engine().store.update_memory(memory_id, content)
        self._send_json({"ok": ok, "memory_id": memory_id})

    def _delete_all(self, body: dict[str, Any]) -> None:
        user_id, character_id = self._ident(body)
        counts = engine().store.delete_user(user_id, character_id)
        self._send_json({"ok": True, "deleted": counts})


def serve() -> None:
    print("=" * 56)
    print("  Language Core · 陪伴产品语言系统内核")
    print(f"  模式: {config.MODE}   模型: {config.LLM_MODEL if config.MODE == 'live' else 'mock(离线)'}")
    print(f"  地址: http://{config.HOST}:{config.PORT}")
    print("=" * 56)

    report = validate_assets()
    if not report.ok:
        print("资产校验未通过:")
        for e in report.errors:
            print("  [错误]", e)
    for w in report.warnings:
        print("  [提示]", w)

    httpd = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n停止服务")
    finally:
        httpd.server_close()
        if _engine is not None:
            _engine.shutdown()


if __name__ == "__main__":
    serve()
