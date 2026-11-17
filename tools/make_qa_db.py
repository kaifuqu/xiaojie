# -*- coding: utf-8 -*-
"""tools/make_qa_db.py — 生成 DuckDB 只读副本，供"要写库"的回归测试使用。

背景：生产库 data/quant_data.duckdb 在开发/自检时不应被写。
本脚本把它整库复制到 data/_qa/quant_data.duckdb（含 WAL），
之后 tools/test_signal_log_wiring.py 等测试全部在副本上跑。

溯源（2026-11 新增）：
    每次复制都会写一份 sidecar 清单 data/_qa/quant_data.duckdb.manifest.json，
    记录「这份副本是从哪一版生产库复制来的」——
      · source_db / source_mtime / source_size / source_wal_size
      · 关键表行数（daily_data / signal_log / signal_pnl / ledger / strategy_effectiveness）
    这样同一份回归在不同时间跑出的差异，可以追溯到"生产库数据版本"这一变量。
    生产库被 daemon 独占时（另一个进程持有写锁）只要文件能复制就仍然可用。

用法：
    python tools/make_qa_db.py             # 复制/覆盖（并写 manifest）
    python tools/make_qa_db.py --from _snapshots/xxx.duckdb   # 从指定库复制（生产库被
                                           # daemon 独占时的兜底：manifest 会如实记录来源）
    python tools/make_qa_db.py --freeze    # 副本已存在且 manifest 存在时拒绝覆盖（防基线漂移）
    python tools/make_qa_db.py --freeze --force   # 强制覆盖并刷新 manifest
    python tools/make_qa_db.py --drop      # 删除副本与 manifest
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "data", "quant_data.duckdb")
QA_DIR = os.path.join(ROOT, "data", "_qa")
DST = os.path.join(QA_DIR, "quant_data.duckdb")
MANIFEST = DST + ".manifest.json"

_ROW_TABLES = (
    "daily_data", "signal_log", "signal_pnl",
    "signal_strategy_ledger", "strategy_effectiveness", "paper_order",
)


def _write_manifest(src_path: str) -> dict:
    out = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_db": src_path,
        "source_mtime": None,
        "source_size": 0,
        "source_wal_size": 0,
        "rows": {},
    }
    try:
        out["source_mtime"] = datetime.fromtimestamp(
            os.path.getmtime(src_path)).isoformat(timespec="seconds")
        out["source_size"] = os.path.getsize(src_path)
        wal = src_path + ".wal"
        out["source_wal_size"] = os.path.getsize(wal) if os.path.exists(wal) else 0
    except Exception as e:
        out["source_error"] = f"{type(e).__name__}: {e}"
    # 行数从「副本」读（副本此刻无其他进程持有，读是安全的）
    try:
        import duckdb

        con = duckdb.connect(DST, read_only=True)
        try:
            for t in _ROW_TABLES:
                try:
                    out["rows"][t] = int(
                        con.execute(f"select count(*) from {t}").fetchone()[0])
                except Exception:
                    pass
        finally:
            con.close()
    except Exception as e:
        out["rows_error"] = f"{type(e).__name__}: {e}"
    with open(MANIFEST, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return out


def main() -> int:
    if "--drop" in sys.argv:
        for p in (DST, DST + ".wal", MANIFEST):
            if os.path.exists(p):
                os.remove(p)
                print(f"[rm] {p}")
        return 0
    src = SRC
    if "--from" in sys.argv:
        try:
            src = os.path.abspath(sys.argv[sys.argv.index("--from") + 1])
        except IndexError:
            print("[FAIL] --from 需要一个 .duckdb 文件路径")
            return 1
        print(f"[INFO] 使用指定来源库：{src}")
    if not os.path.exists(src):
        print(f"[FAIL] 找不到来源库 {src}")
        return 1
    if "--freeze" in sys.argv and "--force" not in sys.argv:
        if os.path.exists(DST) and os.path.exists(MANIFEST):
            print(f"[FROZEN] QA 副本已存在（{DST} 与 manifest 均存在）。")
            print("         如需重建：python tools/make_qa_db.py --freeze --force")
            return 0
    os.makedirs(QA_DIR, exist_ok=True)
    for p in (DST, DST + ".wal"):
        if os.path.exists(p):
            os.remove(p)
    shutil.copyfile(src, DST)
    if os.path.exists(src + ".wal"):
        shutil.copyfile(src + ".wal", DST + ".wal")
    m = _write_manifest(src)
    print(f"[OK] QA 副本已生成：{DST}（{os.path.getsize(DST)/1024/1024:.1f} MB）")
    print(f"     溯源：source={src}")
    print(f"           source_mtime={m.get('source_mtime')} | rows={m.get('rows')}")
    print(f"     manifest：{MANIFEST}")
    print("     测试写库请一律走 tools/_qa_env.use_qa_db()，不要直连生产库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
