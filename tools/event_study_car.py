# -*- coding: utf-8 -*-
"""tools/event_study_car.py — 事件研究：信号前后超额收益曲线（只读分析）

回答一个问题：**这条战法的信号是不是在 T+1 就已经透支了？**

做法（经典事件研究，gs-quant `_event_study` 的简化本地化版）：
  · 事件 = signal_strategy_ledger 里每条信号（按 log_date 定位）
  · 超额收益 AR(t) = 个股日收益 − 基准日收益（默认沪深300 000300.SH）
  · 窗口默认 T-3 ~ T+10（交易日），CAR(t) = 窗口内 AR 的累计均值
  · 分别按池（p2~p5）与按战法（样本 n >= --min-n）输出曲线表

数据来源（全部只读）：
  daily_data.close / pre_close  → 个股日收益
  index_daily.close / pre_close → 基准日收益

用法：
    python tools/event_study_car.py
    python tools/event_study_car.py --pool p5 --start -5 --end 10
    python tools/event_study_car.py --db _snapshots/xxx.duckdb --json car.json

红线：纯只读；不改策略、不写库。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _norm_code(c: str) -> str:
    c = (c or "").strip()
    if "." in c:
        return c
    if c.startswith("6"):
        return c + ".SH"
    if c.startswith(("0", "3")):
        return c + ".SZ"
    if c.startswith(("4", "8", "9")):
        return c + ".BJ"
    return c


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="")
    ap.add_argument("--bench", default="000300.SH")
    ap.add_argument("--start", type=int, default=-3)
    ap.add_argument("--end", type=int, default=10)
    ap.add_argument("--pool", default="", help="只看某池（p2/p3/p4/p5），默认全部")
    ap.add_argument("--min-n", type=int, default=8, help="战法曲线最少样本（默认 8）")
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    from tools._ro_conn import open_ro

    con = open_ro(args.db)
    try:
        where = "WHERE l.log_date IS NOT NULL"
        params = []
        _pool_arg = ""
        if args.pool:
            _pool_arg = args.pool.strip().lower()
            if not _pool_arg.startswith("p"):
                _pool_arg = "p" + _pool_arg
            where += " AND lower(l.pool) = ?"
            params.append(_pool_arg)
        sig_rows = con.execute(
            f"""
            SELECT DISTINCT l.signal_uid, l.log_date, l.ts_code, lower(COALESCE(l.pool,'')) AS pool
            FROM signal_strategy_ledger l
            {where}
            """,
            params,
        ).fetchall()
        strat_rows = con.execute(
            f"""
            SELECT DISTINCT l.strategy_id, l.signal_uid
            FROM signal_strategy_ledger l
            {where}
            """,
            params,
        ).fetchall()
        if not sig_rows:
            print("[SKIP] 账本中没有可用信号。")
            return 0
        pool_of = {str(uid): str(pool) for uid, _d, _c, pool in sig_rows}
        # 池过滤用 lower(pool) 可能是 'p5' 或 '5'
        _pool_filter = args.pool.strip().lower()
        if _pool_filter:
            def _match(pool: str) -> bool:
                p = (pool or "").lower()
                return p == _pool_filter or p == f"p{_pool_filter}" or p.lstrip("p") == _pool_filter.lstrip("p")
            sig_rows = [r for r in sig_rows if _match(str(r[3]))]
            strat_rows = [r for r in strat_rows if _match(pool_of.get(str(r[1]), ""))]
            if not sig_rows:
                print(f"[SKIP] 池 {args.pool} 没有信号。")
                return 0

        uid_strats: dict = {}
        for sid, uid in strat_rows:
            uid_strats.setdefault(str(uid), []).append(str(sid))

        # 交易日轴（用库里已有的交易日，避免依赖日历表完整性）
        td_rows = con.execute(
            "SELECT DISTINCT trade_date FROM daily_data ORDER BY trade_date"
        ).fetchall()
        axis = [str(r[0]) for r in td_rows]
        idx_of = {d: i for i, d in enumerate(axis)}
        if not axis:
            print("[SKIP] daily_data 为空。")
            return 0

        codes = sorted({_norm_code(str(c)) for _u, _d, c, _p in sig_rows})
        dmin = axis[0]
        dmax = axis[-1]
        # 个股日收益
        ret = {}
        if codes:
            q_marks = ",".join(["?"] * len(codes))
            rows = con.execute(
                f"""
                SELECT ts_code, trade_date, close, pre_close
                FROM daily_data
                WHERE ts_code IN ({q_marks}) AND trade_date BETWEEN ? AND ?
                """,
                codes + [dmin, dmax],
            ).fetchall()
            for code, d, c, pc in rows:
                try:
                    c = float(c); pc = float(pc)
                    if pc > 0 and c > 0:
                        ret[(str(code), str(d))] = c / pc - 1.0
                except Exception:
                    continue
        # 基准日收益
        bench = {}
        rows = con.execute(
            """
            SELECT trade_date, close, pre_close FROM index_daily
            WHERE ts_code = ? AND trade_date BETWEEN ? AND ?
            """,
            [args.bench, dmin, dmax],
        ).fetchall()
        for d, c, pc in rows:
            try:
                c = float(c); pc = float(pc)
                if pc > 0 and c > 0:
                    bench[str(d)] = c / pc - 1.0
            except Exception:
                continue
        if not bench:
            print(f"[WARN] 基准 {args.bench} 无数据，退化为裸收益（不做超额）。")

        offsets = list(range(args.start, args.end + 1))

        def car_curve(sig_subset):
            """返回 {offset: (mean_AR, mean_CAR, n)}。"""
            acc = {o: [] for o in offsets}
            for uid, log_date, ts_code, _pool in sig_subset:
                code = _norm_code(str(ts_code))
                i0 = idx_of.get(str(log_date))
                if i0 is None:
                    continue
                ars = {}
                for o in offsets:
                    j = i0 + o
                    if j < 0 or j >= len(axis):
                        continue
                    d = axis[j]
                    rs = ret.get((code, d))
                    rb = bench.get(d)
                    if rs is None:
                        continue
                    ars[o] = rs - (rb if rb is not None else 0.0)
                if not ars:
                    continue
                for o, v in ars.items():
                    acc[o].append(v)
            out = {}
            run = 0.0
            for o in offsets:
                vals = acc[o]
                if vals:
                    m = sum(vals) / len(vals)
                    run += m
                    out[o] = (m * 100.0, run * 100.0, len(vals))
                else:
                    out[o] = (None, None, 0)
            return out

        def _print_curve(title, curve, n_events):
            print(f"\n—— {title}（事件数={n_events}）——")
            print("  偏移 |  平均AR%  |    CAR%   |  样本n")
            for o in offsets:
                ar, car, n = curve[o]
                if ar is None:
                    print(f"  {o:+3d}  |     —     |     —     |    0")
                else:
                    print(f"  {o:+3d}  |  {ar:+7.2f}  |  {car:+7.2f}  |  {n:>4}")

        print("=" * 78)
        print(f"事件研究 CAR 曲线（基准={args.bench}，窗口 {args.start:+d}~{args.end:+d}）")
        print("=" * 78)
        result = {"bench": args.bench, "start": args.start, "end": args.end,
                  "by_pool": {}, "by_strategy": {}}

        # 按池
        pools = sorted({str(p) or "(空)" for _u, _d, _c, p in sig_rows})
        for p in pools:
            subset = [r for r in sig_rows if (str(r[3]) or "(空)") == p]
            curve = car_curve(subset)
            _print_curve(f"池 {p}", curve, len(subset))
            result["by_pool"][p] = {str(o): (None if curve[o][0] is None else
                                             {"ar": round(curve[o][0], 3),
                                              "car": round(curve[o][1], 3),
                                              "n": curve[o][2]})
                                    for o in offsets}

        # 按战法（n >= min_n）
        strats: dict = {}
        for r in sig_rows:
            for sid in uid_strats.get(str(r[0]), []):
                strats.setdefault(sid, []).append(r)
        shown = 0
        for sid in sorted(strats, key=lambda s: -len(strats[s])):
            subset = strats[sid]
            if len(subset) < args.min_n:
                continue
            curve = car_curve(subset)
            _print_curve(f"战法 {sid}", curve, len(subset))
            result["by_strategy"][sid] = {str(o): (None if curve[o][0] is None else
                                                   {"ar": round(curve[o][0], 3),
                                                    "car": round(curve[o][1], 3),
                                                    "n": curve[o][2]})
                                          for o in offsets}
            shown += 1
        if shown == 0:
            print(f"\n[INFO] 没有战法达到 --min-n={args.min_n} 的事件数。")

        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(result, f, ensure_ascii=False, indent=2)
            print(f"\n[JSON] 已写入 {args.json}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
