"""启动服务。支持从 data/llm.env 读配置，避免每次手打环境变量。

用法：
    python run.py              # 有配置走 live，没配置走 mock
    python run.py --mock       # 强制离线
    python run.py --check      # 只验证模型连通性，不启动服务
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

ENV_FILE = ROOT / "data" / "llm.env"


def load_env_file() -> dict[str, str]:
    """读 data/llm.env。这是一个极简 KEY=VALUE 解析，不引入任何依赖。"""
    if not ENV_FILE.exists():
        return {}
    values: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip()
    return values


def apply_env(force_mock: bool) -> None:
    if force_mock:
        os.environ["LANGUAGE_CORE_LLM_API_KEY"] = ""
        return
    from_file = load_env_file()
    for k, v in from_file.items():
        os.environ.setdefault(k, v)


def check_model() -> int:
    """发一条最小请求，确认 key 与模型名可用。"""
    from language_core import config

    if config.MODE != "live":
        print("当前是 mock 模式，没有可检查的模型配置。")
        print(f"请创建 {ENV_FILE}，内容参考 .env.example")
        return 1

    request_body = {
        "model": config.LLM_MODEL,
        "messages": [{"role": "user", "content": "回复两个字：收到"}],
        "max_tokens": 64,
    }
    if urllib.parse.urlparse(config.LLM_BASE_URL).hostname == 'api.deepseek.com':
        request_body['thinking'] = {'type': 'disabled'}
    payload = json.dumps(request_body).encode('utf-8')

    req = urllib.request.Request(
        f"{config.LLM_BASE_URL}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config.LLM_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        text = data["choices"][0]["message"]["content"].strip()
        if not text:
            print("模型返回空内容，连通性未通过。")
            return 1
        usage = data.get("usage", {})
        print(f"连通正常。模型 {config.LLM_MODEL} 返回：{text}")
        print(f"本次用量：{usage.get('prompt_tokens')} + {usage.get('completion_tokens')} tokens")
        return 0
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        print(f"模型返回 HTTP {e.code}：{detail}")
        return 1
    except Exception as e:
        print(f"连接失败：{type(e).__name__}: {e}")
        return 1


def main() -> int:
    args = sys.argv[1:]
    apply_env(force_mock="--mock" in args)

    if "--check" in args:
        return check_model()

    from language_core import config
    from language_core.server import serve

    if config.MODE == "live":
        print(f"使用真实模型：{config.LLM_MODEL} @ {config.LLM_BASE_URL}")
    else:
        print("使用离线 mock 模型（未配置 data/llm.env）")
    serve()
    return 0


if __name__ == "__main__":
    sys.exit(main())
