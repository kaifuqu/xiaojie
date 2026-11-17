# -*- coding: utf-8 -*-
"""tools/backup_snapshot.py — 源码快照备份（git 的安全替代，Tier 0 · 0.4）

为什么不用 git：
    2026-11-01 在 D:\\xiaojie 执行 git init 后，.git 目录属主变成沙箱账号，
    导致 Codex shell 全量 helper_unknown_error，需管理员 takeown + icacls 才恢复。
    在"家用 Windows 单机、无远端"的场景里，git 的收益（分支/合并）用不上，
    但风险（属主/ACL 混乱）是实打实的。因此改为「zip 快照 + 清单」方案：
      · 备份源码（.py/.md/.yaml/.bat/.json/.txt/.sql/.ps1/.toml）
      · 排除 data/ logs/ _pyenv/ __pycache__/ *.duckdb 等运行态与大文件
      · 【安全】排除含明文凭据的文件名（config.yaml / .env*）——
        否则每次快照都会在磁盘上多留一份明文密钥副本（实测曾一次留下 8 份）
      · 生成 manifest（每个文件的 size + sha256），可做差异比对
      · 不做任何 git 操作、不上传任何文件

用法：
    python tools/backup_snapshot.py                 # 备份到 _snapshots/
    python tools/backup_snapshot.py --list          # 列出已有快照
    python tools/backup_snapshot.py --diff <name>   # 与某快照比对（只报新增/修改/删除）
    python tools/backup_snapshot.py --keep 10       # 只保留最近 10 份
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SNAP_DIR = os.path.join(ROOT, "_snapshots")

EXCLUDE_DIRS = {
    "data", "logs", "_pyenv", "__pycache__", ".git", "_snapshots", ".venv", "venv",
    "_bk_v30", "_qa_logs", "parent-meal-signup", "xuanguchi", "node_modules", ".idea",
}
EXCLUDE_EXT = {".duckdb", ".wal", ".log", ".pyc", ".tmp", ".zip", ".7z", ".parquet", ".pkl"}
INCLUDE_EXT = {".py", ".md", ".yaml", ".yml", ".bat", ".ps1", ".sh", ".json", ".txt", ".sql", ".toml", ".cfg", ".ini"}

# 【安全 · 2026-11-03】含明文凭据的文件名，一律不进快照。
# 背景：config.yaml 里存着 Tushare token / HiThink key / DeepSeek key / 2 个企微 webhook，
#       本工具不读 .gitignore，自带一套 _iter_files() 规则 → 历史上每次打快照
#       都会把整份 config.yaml（含明文密钥）复制进 zip，实测磁盘上曾同时存在 8 份副本。
# 现在按「文件名」精确排除；.env 及 .env.* 一并排除。
EXCLUDE_NAMES = {
    "config.yaml",
    ".env",
    "master_control.json",
}
EXCLUDE_NAME_PREFIXES = (".env.",)   # 覆盖 .env.production / .env.local 等变体


def _is_excluded_name(fn: str) -> bool:
    """判断文件名是否属于「绝不进快照」的敏感名单。"""
    name = (fn or "").strip().lower()
    if not name:
        return True
    if name in EXCLUDE_NAMES:
        return True
    return name.startswith(EXCLUDE_NAME_PREFIXES)



def _iter_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".git")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in EXCLUDE_EXT or ext not in INCLUDE_EXT:
                continue
            # 【安全 · 2026-11-03】跳过含明文凭据的文件名（config.yaml / .env*）
            if _is_excluded_name(fn):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace("\\", "/")
            yield full, rel


def _sha256(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def make_snapshot(tag: str = "") -> str:
    os.makedirs(SNAP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"src_{stamp}" + (f"_{tag}" if tag else "")
    zpath = os.path.join(SNAP_DIR, name + ".zip")
    manifest = {}
    n = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for full, rel in _iter_files():
            try:
                st = os.stat(full)
                manifest[rel] = {"size": st.st_size, "sha256": _sha256(full)}
                zf.write(full, rel)
                n += 1
            except Exception as e:
                print(f"  [skip] {rel}: {e}")
    mpath = os.path.join(SNAP_DIR, name + ".manifest.json")
    with open(mpath, "w", encoding="utf-8") as f:
        json.dump({"name": name, "created": datetime.now().isoformat(timespec="seconds"),
                   "root": ROOT, "files": manifest}, f, ensure_ascii=False, indent=1)
    size_mb = os.path.getsize(zpath) / 1024 / 1024
    print(f"[OK] 快照完成：{zpath}")
    print(f"     文件 {n} 个，压缩后 {size_mb:.2f} MB")
    print(f"     清单：{mpath}")
    print(f"     [安全] 已排除含明文凭据的文件：{', '.join(sorted(EXCLUDE_NAMES))} 及 .env* 变体")
    return name


def list_snapshots():
    if not os.path.isdir(SNAP_DIR):
        print("暂无快照目录")
        return
    items = sorted(x for x in os.listdir(SNAP_DIR) if x.endswith(".zip"))
    if not items:
        print("暂无快照")
        return
    for x in items:
        p = os.path.join(SNAP_DIR, x)
        print(f"  {x:42s} {os.path.getsize(p)/1024/1024:8.2f} MB  "
              f"{datetime.fromtimestamp(os.path.getmtime(p)):%Y-%m-%d %H:%M}")


def diff_snapshot(name: str) -> int:
    mpath = os.path.join(SNAP_DIR, name if name.endswith(".json") else name + ".manifest.json")
    if not os.path.exists(mpath):
        print(f"[FAIL] 找不到清单 {mpath}")
        return 1
    old = json.load(open(mpath, encoding="utf-8"))["files"]
    cur = {}
    for full, rel in _iter_files():
        try:
            cur[rel] = _sha256(full)
        except Exception:
            pass
    added = sorted(set(cur) - set(old))
    removed = sorted(set(old) - set(cur))
    changed = sorted(k for k in set(cur) & set(old) if cur[k] != old[k]["sha256"])
    print(f"与快照 {name} 对比：")
    print(f"  新增 {len(added)} · 修改 {len(changed)} · 删除 {len(removed)}")
    for label, lst in (("新增", added), ("修改", changed), ("删除", removed)):
        for x in lst[:200]:
            print(f"    [{label}] {x}")
    return 0


def prune(keep: int):
    if not os.path.isdir(SNAP_DIR):
        return
    zips = sorted(x for x in os.listdir(SNAP_DIR) if x.endswith(".zip"))
    for x in zips[:-keep] if keep > 0 else []:
        for p in (os.path.join(SNAP_DIR, x), os.path.join(SNAP_DIR, x.replace(".zip", ".manifest.json"))):
            try:
                os.remove(p)
                print(f"  [prune] {os.path.basename(p)}")
            except Exception:
                pass


def main() -> int:
    ap = argparse.ArgumentParser(description="源码快照备份（git 的安全替代）")
    ap.add_argument("--tag", default="", help="快照标签")
    ap.add_argument("--list", action="store_true", help="列出已有快照")
    ap.add_argument("--diff", default="", help="与指定快照比对")
    ap.add_argument("--keep", type=int, default=0, help="只保留最近 N 份")
    a = ap.parse_args()
    if a.list:
        list_snapshots(); return 0
    if a.diff:
        return diff_snapshot(a.diff)
    make_snapshot(a.tag)
    if a.keep:
        prune(a.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
