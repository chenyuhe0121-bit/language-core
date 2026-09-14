"""打包转交：清理本地私有内容，生成一个可以直接发给同事的副本。

它会：
  1. 建一个全新的目录（默认 dist/language-core）
  2. 只复制该给别人看的东西：代码、文档、素材、测试
  3. 排除密钥、对话数据库、调试产物、一次性运维脚本、字节码
  4. 跑一遍检查，确认副本里没有密钥和个人数据
  5. 打成一个 zip

原目录一个字都不动。

用法：
    python tools/make_package.py              # 生成 dist/language-core/ 和 zip
    python tools/make_package.py --out E:\\tmp # 换个输出位置
    python tools/make_package.py --no-zip     # 只要目录
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 完全不复制的东西
EXCLUDE_DIRS = {
    ".git", ".tmp", "__pycache__", ".venv", "venv",
    "data", "dist", ".vscode", ".idea",
}
# 不复制具体文件（含本地凭据依赖或私有内容）
EXCLUDE_FILES = {
    "llm.env", "push_to_github.py", "push_history.py",
    ".DS_Store", "Thumbs.db",
}
# 密钥特征。命中任何一个就中止打包。
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
]
# 需要检查密钥的文件类型
TEXT_SUFFIXES = {".py", ".js", ".css", ".html", ".md", ".json", ".txt", ".env", ".example"}


def should_skip(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    if path.name in EXCLUDE_FILES:
        return True
    if path.suffix == ".pyc" or path.name.startswith("_"):
        return True
    return False


def copy_tree(dest: Path) -> list[Path]:
    copied: list[Path] = []
    for src in sorted(ROOT.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(ROOT)
        if should_skip(src):
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        copied.append(rel)
    return copied


def scan_secrets(dest: Path) -> list[str]:
    hits: list[str] = []
    for f in dest.rglob("*"):
        if not f.is_file() or f.suffix not in TEXT_SUFFIXES:
            continue
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pat in SECRET_PATTERNS:
            for m in pat.finditer(text):
                hits.append(f"{f.relative_to(dest)}  命中 {m.group(0)[:16]}…")
    return hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(ROOT / "dist"))
    ap.add_argument("--no-zip", action="store_true")
    args = ap.parse_args()

    out_root = Path(args.out)
    dest = out_root / "language-core"

    if dest.exists():
        print(f"清掉已有副本 {dest}")
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    print(f"复制到 {dest}")
    copied = copy_tree(dest)
    print(f"  共 {len(copied)} 个文件")

    print("\n检查副本里有没有密钥…")
    hits = scan_secrets(dest)
    if hits:
        print("  发现问题，已中止：")
        for h in hits:
            print("   ", h)
        shutil.rmtree(dest)
        return 1
    print("  通过，没有密钥")

    print("\n检查有没有误带数据文件…")
    bad = [p for p in dest.rglob("*")
           if p.suffix in (".db", ".db-journal", ".db-wal") or p.name == "llm.env"]
    if bad:
        print("  发现问题：", [str(p.relative_to(dest)) for p in bad])
        shutil.rmtree(dest)
        return 1
    print("  通过，没有数据库与密钥文件")

    # 副本里放一份说明
    (dest / "怎么运行.txt").write_text(
        "怎么运行\n"
        "========\n\n"
        "1. 需要 Python 3.9 或以上。项目零第三方依赖，不需要 pip install。\n\n"
        "2. 在项目根目录执行：\n"
        "       python run.py\n"
        "   然后浏览器打开 http://127.0.0.1:8420\n\n"
        "3. 想接真实模型，在 data 目录下新建 llm.env，内容：\n"
        "       LANGUAGE_CORE_LLM_API_KEY=你的密钥\n"
        "       LANGUAGE_CORE_LLM_BASE_URL=https://api.deepseek.com/v1\n"
        "       LANGUAGE_CORE_LLM_MODEL=deepseek-flash\n"
        "   没有这个文件也能启动，但会进入离线演示模式，\n"
        "   回复是按关键词写死的脚本，不能用来评判角色质量。\n\n"
        "4. 跑测试：\n"
        "       python -m unittest tests.test_conversation -v\n\n"
        "5. 看文档：docs/ 目录，建议按编号顺序读。\n"
        "   只看两份的话：02（台词旁白协议）和 03（总体架构）。\n",
        encoding="utf-8",
    )

    if not args.no_zip:
        zip_path = out_root / "language-core.zip"
        if zip_path.exists():
            zip_path.unlink()
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(dest.rglob("*")):
                if f.is_file():
                    z.write(f, f.relative_to(out_root))
        size = zip_path.stat().st_size
        print(f"\n打包完成：{zip_path}  （{size/1024:.0f} KB）")

    print(f"\n目录副本：{dest}")
    print(f"文件数：{len(list(p for p in dest.rglob('*') if p.is_file()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
