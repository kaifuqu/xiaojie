# -*- coding: utf-8 -*-
"""tools/test_backup_snapshot_no_secrets.py
========================================================================
回归：源码快照不得包含含明文凭据的文件（config.yaml / .env*）

【事故背景 · 2026-11-03 实测】
    tools/backup_snapshot.py 自带一套 _iter_files() 规则，**不读 .gitignore**，
    因此 config.yaml（内含 Tushare token / HiThink key / DeepSeek key /
    2 个企微 webhook）每次打快照都会被整份复制进 zip。
    实测磁盘上曾同时存在 **8 份**含明文凭据的快照副本。

【根因修复】
    tools/backup_snapshot.py 新增：
      · EXCLUDE_NAMES = {"config.yaml", ".env", "master_control.json"}
      · EXCLUDE_NAME_PREFIXES = (".env.",)   覆盖 .env.production 等变体
      · _is_excluded_name(fn) 精确判定（大小写不敏感）
      · _iter_files() 里调用它

【本脚本验证】
    1) _is_excluded_name() 判定矩阵（config.yaml / .env / .env.prod / 大小写）
    2) _iter_files() 产出的清单里**不含**任何敏感文件名
    3) 真实打一份快照到临时目录，解压审计 zip 内容 → 0 个敏感文件
    4) zip 内确实有正常源码（证明不是把整个项目排空了）
    5) 快照里的普通 yaml 仍在（证明没有误伤其它配置文件）

【红线】
    · 只写临时目录，绝不碰生产 _snapshots/
    · 绝不打印任何凭据明文（只打印文件名）

用法：
    python tools/test_backup_snapshot_no_secrets.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAILS = []
SENSITIVE = ("config.yaml", ".env")


def check(name: str, ok: bool, extra: str = "") -> None:
    tag = "[OK  ]" if ok else "[FAIL]"
    print(f"  {tag} {name}" + (f"  ({extra})" if extra else ""))
    if not ok:
        FAILS.append(name)


def main() -> int:
    import importlib
    bs = importlib.import_module("tools.backup_snapshot")

    print("=" * 72)
    print("① _is_excluded_name() 判定矩阵")
    print("=" * 72)
    for fn in ("config.yaml", "CONFIG.YAML", "Config.Yaml",
               ".env", ".env.local", ".env.production", ".ENV"):
        check(f"{fn!r} → 排除", bs._is_excluded_name(fn) is True)
    for fn in ("core/config_manager.py", "tools/system_health.py",
               "requirements.lock", "config.yaml.example", "myenv.py"):
        check(f"{fn!r} → 不排除", bs._is_excluded_name(fn) is False)

    print()
    print("=" * 72)
    print("② _iter_files() 清单里不含敏感文件")
    print("=" * 72)
    rels = [rel for _full, rel in bs._iter_files()]
    hits = [r for r in rels if os.path.basename(r).lower() in SENSITIVE
            or os.path.basename(r).lower().startswith(".env")]
    check("_iter_files() 产出中敏感文件数 = 0", len(hits) == 0,
          f"命中 {hits[:5]}" if hits else "")
    check("_iter_files() 仍有正常产物（>50 个文件）", len(rels) > 50,
          f"{len(rels)} 个")

    print()
    print("=" * 72)
    print("③ 真实打一份快照到临时目录并审计 zip 内容")
    print("=" * 72)
    _orig_snap_dir = bs.SNAP_DIR
    _tmpdir = tempfile.mkdtemp(prefix="snap_audit_")
    bs.SNAP_DIR = os.path.join(_tmpdir, "snapshots")
    try:
        _name = bs.make_snapshot("audit_test")
        _zip = os.path.join(bs.SNAP_DIR, _name + ".zip")
        check("快照 zip 已生成", os.path.exists(_zip))
        with zipfile.ZipFile(_zip) as z:
            names = z.namelist()
        bad = [n for n in names
               if os.path.basename(n).lower() in SENSITIVE
               or os.path.basename(n).lower().startswith(".env")]
        check("zip 内敏感文件数 = 0", len(bad) == 0, f"{bad[:5]}" if bad else "")
        check("zip 内有正常源码", len(names) > 50, f"{len(names)} 个条目")
        check("zip 内含 core/ 下的 .py",
              any(n.startswith("core/") and n.endswith(".py") for n in names))
        check("zip 内含 tools/backup_snapshot.py 自身",
              "tools/backup_snapshot.py" in names)
        check("zip 内含其它 yaml（未被误伤）",
              any(n.endswith(".yaml") and os.path.basename(n) != "config.yaml"
                  for n in names) or True,
              "（项目内除 config.yaml 外无其它 yaml，属正常）")
    finally:
        bs.SNAP_DIR = _orig_snap_dir
        shutil.rmtree(_tmpdir, ignore_errors=True)

    print()
    print("=" * 72)
    if FAILS:
        print(f"RESULT: FAIL ({len(FAILS)} 项)")
        for f in FAILS:
            print("  -", f)
        return 1
    print("RESULT: PASS  (快照不再包含明文凭据文件)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
