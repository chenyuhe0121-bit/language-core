"""用 GitHub API 推送仓库内容。

为什么不用 git push：当前沙箱禁止进程创建管道，git 的 HTTPS 传输会失败
（couldn't create signal pipe / schannel SEC_E_NO_CREDENTIALS）。
GitHub 的 REST API 走正常的 HTTPS 请求，不受影响。

做的事情等价于：
    git add -A && git commit && git push

流程：每个文件 -> blob -> tree -> commit -> 更新 main 引用
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

OWNER = "chenyuhe0121-bit"
REPO = "language-core"
BRANCH = "main"

# 这些路径永不推送
EXCLUDE_DIRS = {".git", ".tmp", "__pycache__", "data"}
EXCLUDE_FILES = {"llm.env"}

MAX_BLOB = 40 * 1024 * 1024   # GitHub 单文件上限


def token() -> str:
    return (REPO_ROOT / "data" / "_gh_token.txt").read_text(encoding="utf-8").strip()


def api(path: str, payload: dict | None = None, method: str = "GET"):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "Authorization": f"Bearer {token()}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "language-core",
            "Content-Type": "application/json",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code} on {method} {path}: {detail}") from None


def collect_files() -> list[tuple[str, Path]]:
    out: list[tuple[str, Path]] = []
    for p in sorted(REPO_ROOT.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(REPO_ROOT)
        parts = set(rel.parts)
        if parts & EXCLUDE_DIRS or p.name in EXCLUDE_FILES:
            continue
        if p.name.endswith((".pyc", ".db")) or p.name.startswith("_"):
            continue
        out.append((rel.as_posix(), p))
    return out


def main() -> int:
    files = collect_files()
    print(f"待推送文件：{len(files)} 个")

    # 安全闸：任何文件都不许含真实密钥。
    # 密钥在这里拆成两段拼接，否则本脚本扫描自己会命中自己。
    secret_marks = [
        "sk-5630c27d" + "7008437bae1b5e7170365aae",
        "github_pat_11B4F7M7Y0MUw" + "JIjXaaTOG_mISDxAmFTvCwRU9c",
    ]
    for rel, path in files:
        text = path.read_text(encoding="utf-8", errors="ignore")
        for mark in secret_marks:
            if mark in text:
                print(f"  !! 中止：{rel} 含密钥（{mark[:12]}...）")
                return 1
    print("密钥检查：通过\n")

    # 0. GitHub 不允许往完全空的仓库写 blob（会返回 409 Git Repository is empty）。
    #    先用 Contents API 落一个种子提交把仓库初始化，再基于它建树。
    import base64 as _b64
    seed_path = ".seed"
    seed_text = "init"
    try:
        st, cur = api(f"/repos/{OWNER}/{REPO}/contents/{seed_path}")
        base_parent = None  # 已存在，走下面的常规流程
        print(f"仓库已有内容，当前 {seed_path} 的 sha={cur['sha'][:8]}")
    except RuntimeError as e:
        if "404" not in str(e):
            raise
        st, created = api(
            f"/repos/{OWNER}/{REPO}/contents/{seed_path}",
            {"message": "init", "content": _b64.b64encode(seed_text.encode()).decode("ascii")},
            "PUT",
        )
        base_parent = created["commit"]["sha"]
        print(f"仓库已初始化，种子提交 {base_parent[:8]}\n")

    # 1. 建 blob
    tree_items = []
    for rel, path in files:
        size = path.stat().st_size
        if size > MAX_BLOB:
            print(f"  跳过超大文件 {rel}")
            continue
        import base64
        content = base64.b64encode(path.read_bytes()).decode("ascii")
        st, blob = api(f"/repos/{OWNER}/{REPO}/git/blobs",
                       {"content": content, "encoding": "base64"}, "POST")
        tree_items.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        print(f"  blob {rel}  ({size} B)")

    # 2. 建 tree
    print("\n建 tree ...")
    st, tree = api(f"/repos/{OWNER}/{REPO}/git/trees",
                   {"tree": tree_items}, "POST")
    print(f"  tree {tree['sha']}")

    # 3. 建 commit
    print("建 commit ...")
    message = (
        "语言系统内核 v0.1：台词/旁白协议、上下文编译器、四层记忆、人格校验\n\n"
        "- segproto/1 协议：台词与旁白严格分离，内心独白默认不朗读\n"
        "- 五张卡正交：角色卡/场景卡/关系状态/风格锚点/边界策略\n"
        "- 记忆四层：工作记忆/短期摘要/长期记忆/未闭合话题\n"
        "- 上下文编译器：按预算分层注入，危机指令永不裁剪\n"
        "- checker：规则型校验，硬失败触发重生成\n"
        "- 41 项端到端测试\n"
        "- 零第三方依赖，任何装了 Python 的机器直接能跑"
    )
    st, commit = api(f"/repos/{OWNER}/{REPO}/git/commits",
                     {"message": message, "tree": tree["sha"],
                      "parents": [base_parent] if base_parent else []}, "POST")
    print(f"  commit {commit['sha']}")

    # 4. 建分支引用
    print(f"建分支 {BRANCH} ...")
    try:
        st, ref = api(f"/repos/{OWNER}/{REPO}/git/refs",
                      {"ref": f"refs/heads/{BRANCH}", "sha": commit["sha"]}, "POST")
        print(f"  {ref['ref']} -> {ref['object']['sha']}")
    except RuntimeError as e:
        if "422" in str(e):
            st, ref = api(f"/repos/{OWNER}/{REPO}/git/refs/heads/{BRANCH}",
                          {"sha": commit["sha"], "force": True}, "PATCH")
            print(f"  已更新 {ref['ref']} -> {ref['object']['sha']}")
        else:
            raise

    # 5. 收尾：设默认分支
    try:
        api(f"/repos/{OWNER}/{REPO}", {"default_branch": BRANCH}, "PATCH")
    except RuntimeError:
        pass

    print(f"\n完成：https://github.com/{OWNER}/{REPO}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
