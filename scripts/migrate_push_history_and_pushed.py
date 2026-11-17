# -*- coding: utf-8 -*-
"""
scripts/migrate_push_history_and_pushed.py  (一次性修复脚本，幂等)
========================================================================
两件事：

【1】清洗 data/push_history.json
    生产实测（2026-10-21）：116 只票里 32 只的条目数已达上限 20，
    且条目是【同日同池重复】（例：600577.SH 20 条全是 2026-10-18 / p1）。
    根因：core/push_dedup.record_push 旧实现无条件 append + arr[-20:] 截断，
          P1 底仓重建每次运行都写一遍，把真实推送历史挤没了。
    修复：同 (date, pool) 折叠成 1 条（保留最高分），条目数从 20 降到实际天数。

【2】回填 signal_log.pushed
    用 push_history.json 交叉验证 signal_log 的 pushed 标记。
    匹配口径：(交易日, 代码) —— 同票同日被推过即视为已推送
    （跨池冷却只会把记录留在最先推送的池上，所以池维度不可靠）。

【红线】
  · 只改 signal_log.pushed 一列 + push_history.json 的重复条目
  · 不改 score / strategy / trade_date / ts_code 等任何其它列
  · push_history.json 先备份到 data/push_history.json.bak_<ts>
  · 幂等：重复执行结果一致
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "quant_data.duckdb")
HIST = os.path.join(ROOT, "data", "push_history.json")
BJ = timezone(timedelta(hours=8))


def clean_history() -> dict:
    if not os.path.exists(HIST):
        print("  [SKIP] push_history.json 不存在")
        return {}
    with open(HIST, "r", encoding="utf-8") as f:
        hist = json.load(f)
    if not isinstance(hist, dict):
        print("  [SKIP] push_history.json 格式异常")
        return {}

    ts = datetime.now(BJ).strftime("%Y%m%d_%H%M%S")
    shutil.copy2(HIST, f"{HIST}.bak_{ts}")

    before = sum(len(v) for v in hist.values() if isinstance(v, list))
    out = {}
    for code, arr in hist.items():
        if not isinstance(arr, list):
            continue
        best: dict[tuple, dict] = {}
        order: list[tuple] = []
        for it in arr:
            if not isinstance(it, dict):
                continue
            d = str(it.get("date", "")).replace("-", "")[:8]
            p = str(it.get("pool", "")).strip().lower()
            try:
                sc = float(it.get("score", 0.0) or 0.0)
            except (TypeError, ValueError):
                sc = 0.0
            k = (d, p)
            if k not in best:
                best[k] = {"date": d, "pool": p, "score": sc}
                order.append(k)
            elif sc > best[k]["score"]:
                best[k]["score"] = sc
        if best:
            out[code] = [best[k] for k in order]

    after = sum(len(v) for v in out.values())
    with open(HIST, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  [OK] push_history 清洗：{before} 条 → {after} 条（票数 {len(out)}）")
    print(f"       备份：{os.path.basename(HIST)}.bak_{ts}")
    return out


def backfill_pushed() -> dict:
    if not os.path.exists(HIST):
        return {}
    with open(HIST, "r", encoding="utf-8") as f:
        hist = json.load(f)

    # (交易日, 裸码) -> 最高分
    idx: dict[tuple, float] = {}
    for code, arr in (hist or {}).items():
        if not isinstance(arr, list):
            continue
        bare = str(code).split(".")[0].strip()
        if not bare:
            continue
        for it in arr:
            if not isinstance(it, dict):
                continue
            d = str(it.get("date", "")).replace("-", "")[:8]
            if len(d) != 8 or not d.isdigit():
                continue
            try:
                sc = float(it.get("score", 0.0) or 0.0)
            except (TypeError, ValueError):
                sc = 0.0
            k = (d, bare)
            if sc > idx.get(k, -1e18):
                idx[k] = sc

    con = duckdb.connect(DB)
    try:
        rows = con.execute(
            "SELECT REPLACE(COALESCE(trade_date,''),'-',''), ts_code, pool FROM signal_log"
        ).fetchall()
        n = 0
        for d8, code, pool in rows:
            bare = str(code or "").split(".")[0].strip()
            if not d8 or not bare:
                continue
            if (d8, bare) not in idx:
                continue
            con.execute(
                "UPDATE signal_log SET pushed = TRUE "
                "WHERE REPLACE(COALESCE(trade_date,''),'-','') = ? "
                "AND (ts_code = ? OR ts_code = ? OR ts_code LIKE ?)",
                [d8, bare, f"{bare}.%", f"{bare}.%"],
            )
            n += 1

        t = con.execute(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN pushed THEN 1 ELSE 0 END) "
            "FROM signal_log"
        ).fetchone()
        print(f"  [OK] signal_log：候选 {t[0]} 条，其中标记 pushed=TRUE {t[1] or 0} 条")
    finally:
        con.close()
    return {"matched": n}


def main() -> int:
    print("=" * 64)
    print("push_history 清洗 + signal_log.pushed 回填")
    print("=" * 64)
    print("[1] 清洗 push_history.json")
    clean_history()
    print("\n[2] 回填 signal_log.pushed")
    backfill_pushed()
    print("\n完成 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
