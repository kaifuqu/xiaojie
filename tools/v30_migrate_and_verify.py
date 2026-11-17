# -*- coding: utf-8 -*-
"""V30 生产库迁移与核验脚本（幂等，可重复运行）

用途：首次部署 / 事后核验 V30 混合基准是否真正落数。
做四件事，全部幂等：
  1. 建 index_daily 表 + 同步 4 个基准指数（260 天窗口）
  2. signal_pnl / strategy_effectiveness 幂等扩列（V30 新列 + V29 漏列）
  3. 跑一次 T+N 回填 + 战法有效性聚合 + 优化器报表（auto_apply=False）+ UI 快照
  4. 打印填充率与抽样，验证新旧两套基准口径并列可用

用法：
    set PYTHONPATH=D:\\xiaojie
    python tools/v30_migrate_and_verify.py

注意：
  · 只加列 / 只建新表，绝不 DROP、绝不改既有列口径
  · 需要 DuckDB 写锁：启动前请确认 auto_sniper_daemon 未运行
  · 全程 auto_apply=False，绝不改任何实盘评分权重
"""
from __future__ import annotations

import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 【铁律】必须在 import pandas 之前关掉 pyarrow（Win11 SAC 会让 pandas 崩）
from core import _pyarrow_compat as _pyarrow_compat  # noqa: E402

_pyarrow_compat.disable_pyarrow()

import pandas as pd  # noqa: E402,F401

V30_PNL_COLUMNS = [
    "bench_sh_pct", "bench_sz_pct", "bench_cyb_pct", "bench_blend_pct",
    "excess_sh_pct", "excess_sz_pct", "excess_cyb_pct", "excess_blend_pct",
    "beat_blend", "board_attributed", "board_excess_pct",
]
V30_EFF_COLUMNS = [
    "avg_excess_sh", "avg_excess_sz", "avg_excess_cyb",
    "avg_excess_blend", "beat_blend_rate",
]


def main() -> int:
    from data.db_core import get_conn
    from data.index_daily_store import (
        INDEX_NAMES, blend_return_pct, ensure_index_table, load_index_panel,
        single_index_return_pct, sync_index_quietly,
    )
    from data.signal_backtest import run_daily_backfill
    import data.signal_logger as signal_logger
    from core.strategy_effectiveness_snapshot import build_ui_snapshot
    from core.strategy_weight_optimizer import optimize_strategy_weights

    con = get_conn()
    if con is None or not hasattr(con, "execute"):
        print("[FAIL] 拿不到 DuckDB 写连接（可能被 daemon 占用）")
        return 2

    print("=" * 66)
    print("V30 生产库迁移与核验（指数 = 上证 / 深证 / 创业板 / 沪深300）")
    print("=" * 66)

    print("\n[1/5] 建 index_daily 表:", ensure_index_table(con))

    print("[2/5] 同步 4 个基准指数（260 天窗口，幂等）...")
    res = sync_index_quietly(db_conn=con, lookback_days=260)
    print("      ok=%s written=%s per_index=%s" % (
        res.get("ok"), res.get("written"), res.get("per_index")))
    if res.get("skipped"):
        print("      skipped=%s" % (res.get("skipped"),))

    print("[3/5] 幂等扩列...")
    signal_logger._ensure_v30_index_columns(con)
    pnl_cols = {str(c[1]).lower() for c in con.execute(
        "PRAGMA table_info('signal_pnl')").fetchall()}
    eff_cols = {str(c[1]).lower() for c in con.execute(
        "PRAGMA table_info('strategy_effectiveness')").fetchall()}
    print("      signal_pnl 缺列:", [c for c in V30_PNL_COLUMNS if c not in pnl_cols] or "无")
    print("      strategy_effectiveness 缺列:",
          [c for c in V30_EFF_COLUMNS if c not in eff_cols] or "无")

    print("[4/5] 跑回填 + 战法聚合 + 优化器报表(auto_apply=False) + UI 快照...")
    back = run_daily_backfill(con)
    print("      backfill:", back)
    opt = optimize_strategy_weights(con, auto_apply=False)
    print("      optimizer mode=%s 战法数=%s（绝不改实盘权重）" % (
        opt.get("mode"), len(opt.get("stats") or {})))
    snap_path = build_ui_snapshot(con)
    print("      快照:", snap_path)

    print("\n[5/5] 核验")
    row = con.execute(
        "SELECT COUNT(*), COUNT(benchmark_pct), COUNT(excess_pct), "
        "COUNT(bench_blend_pct), COUNT(excess_blend_pct), "
        "COUNT(board_attributed), COUNT(board_excess_pct) FROM signal_pnl"
    ).fetchone()
    print("      signal_pnl  总数=%d | 沪深300基准=%d 超额=%d | 混合基准=%d 超额=%d "
          "| 板块归因=%d 板块超额=%d" % tuple(row))
    row2 = con.execute(
        "SELECT COUNT(*), COUNT(avg_excess), COUNT(avg_excess_blend), "
        "COUNT(beat_blend_rate) FROM strategy_effectiveness"
    ).fetchone()
    print("      strategy_effectiveness 总数=%d | 沪深300超额=%d | 混合超额=%d "
          "| 跑赢率=%d" % tuple(row2))

    # 用 fetchall() 而非 .df()：pandas 3.x 下 iterrows 返回的 Series
    # 按列名索引，r[0] 这种位置索引会抛 KeyError。
    idx_rows = con.execute(
        "SELECT ts_code, COUNT(*), MIN(CAST(trade_date AS VARCHAR)), "
        "MAX(CAST(trade_date AS VARCHAR)) FROM index_daily "
        "GROUP BY ts_code ORDER BY ts_code").fetchall()
    print("\n      指数覆盖度：")
    for r in idx_rows:
        print("        %-10s %4d 行  %s ~ %s" % (
            INDEX_NAMES.get(str(r[0]), str(r[0])), int(r[1]),
            str(r[2])[:10], str(r[3])[:10]))

    panel = load_index_panel(con)
    if panel:
        any_code = sorted(panel)[0]
        tds = panel[any_code]["td"].tolist()
        print("\n      区间收益（%s ~ %s）：" % (tds[0], tds[-1]))
        for code in sorted(panel):
            print("        %-10s %+.3f%%" % (
                INDEX_NAMES.get(code, code),
                single_index_return_pct(panel, code, tds[0], tds[-1]) or 0.0))
        print("        %-10s %+.3f%%（等权混合，主口径）" % (
            "混合基准", blend_return_pct(panel, tds[0], tds[-1]) or 0.0))

    try:
        with io.open(snap_path, "r", encoding="utf-8") as f:
            snap = json.load(f)
        cov = snap.get("index_coverage") or {}
        print("\n      快照 index_coverage.primary_ready =", cov.get("primary_ready"))
        stats = snap.get("stats") or {}
        rows = stats if isinstance(stats, list) else list(stats.values())
        if rows:
            r0 = rows[0]
            print("      快照样本行:", {
                k: r0.get(k) for k in
                ("strategy_label", "win_rate", "avg_return", "avg_excess",
                 "avg_excess_blend", "beat_blend_rate")})
    except Exception as e:
        print("      快照读取失败（不影响迁移结果）: %s" % e)

    print("\nDONE：V30 迁移与核验完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
