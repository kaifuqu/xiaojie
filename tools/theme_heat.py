# -*- coding: utf-8 -*-
"""tools/theme_heat.py — 题材热度榜（只读分析 + UI 数据源）

背景：fact_limit_theme（KPL 涨停题材归类，3,650+ 行）长期「只写不读」。
本模块把它盘活成只读的「题材热度榜」：
  · 最近 N 个交易日每个题材的涨停家数排行
  · 当日代表个股（含连板状态）
  · 晋级率（上一交易日涨停股中今日继续涨停的比例）

红线：纯只读；不参与选股、不改任何策略阈值。

用法（CLI）：
    python tools/theme_heat.py                 # 最近 10 个交易日
    python tools/theme_heat.py --days 15 --top 25
"""
from __future__ import annotations

import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def theme_heat_snapshot(days: int = 10, top_n: int = 20,
                        retries: int = 2, delay: float = 1.0,
                        db_path: str = "") -> dict:
    """返回题材热度快照 dict（异常降级，绝不抛错）。"""
    out = {"ok": False, "error": "", "as_of": "", "days": days,
           "dates": [], "day_counts": {}, "promote_pct": None,
           "n_latest": 0, "n_prev": 0, "themes": []}
    try:
        from tools._ro_conn import open_ro

        con = open_ro(db_path, retries=retries, delay=delay)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    try:
        date_rows = con.execute(
            "SELECT DISTINCT trade_date FROM fact_limit_theme "
            "ORDER BY trade_date DESC LIMIT ?",
            [int(days)],
        ).fetchall()
        dates = sorted(str(r[0]) for r in date_rows)
        if not dates:
            out["error"] = "fact_limit_theme 无数据"
            return out
        out["dates"] = dates
        latest = dates[-1]
        prev = dates[-2] if len(dates) >= 2 else None
        out["as_of"] = latest

        # 当日/累计计数
        day_counts = dict(con.execute(
            "SELECT trade_date, COUNT(*) FROM fact_limit_theme "
            "WHERE trade_date BETWEEN ? AND ? GROUP BY 1",
            [dates[0], latest],
        ).fetchall())
        out["day_counts"] = {str(k): int(v) for k, v in day_counts.items()}
        out["n_latest"] = int(day_counts.get(latest, 0))

        # 晋级率：上一交易日涨停股（按 ts_code 集合）中今天仍涨停的占比
        try:
            if prev:
                prev_set = {r[0] for r in con.execute(
                    "SELECT DISTINCT ts_code FROM fact_limit_theme WHERE trade_date = ?",
                    [prev],
                ).fetchall()}
                cur_set = {r[0] for r in con.execute(
                    "SELECT DISTINCT ts_code FROM fact_limit_theme WHERE trade_date = ?",
                    [latest],
                ).fetchall()}
                out["n_prev"] = len(prev_set)
                if prev_set:
                    out["promote_pct"] = round(
                        len(prev_set & cur_set) / len(prev_set) * 100.0, 1)
        except Exception:
            pass

        # 题材排行（窗口累计 + 当日家数 + 活跃天数）
        rows = con.execute(
            """
            SELECT theme,
                   COUNT(*)                                        AS total_n,
                   COUNT(DISTINCT trade_date)                      AS active_days,
                   COUNT(*) FILTER (WHERE trade_date = ?)          AS today_n,
                   MAX(trade_date)                                 AS last_date
            FROM fact_limit_theme
            WHERE trade_date BETWEEN ? AND ? AND theme IS NOT NULL AND theme <> ''
            GROUP BY theme
            ORDER BY total_n DESC
            LIMIT ?
            """,
            [latest, dates[0], latest, int(top_n)],
        ).fetchall()

        stocks = con.execute(
            """
            SELECT theme, ts_code, status, lu_desc
            FROM fact_limit_theme
            WHERE trade_date = ? AND theme IS NOT NULL AND theme <> ''
            ORDER BY theme, ts_code
            """,
            [latest],
        ).fetchall()
        by_theme_stocks: dict = {}
        for th, code, status, desc in stocks:
            by_theme_stocks.setdefault(str(th), []).append(
                {"ts_code": str(code), "status": str(status or ""),
                 "lu_desc": str(desc or "")})

        for th, total_n, active_days, today_n, last_date in rows:
            th = str(th)
            out["themes"].append({
                "theme": th,
                "total_n": int(total_n),
                "active_days": int(active_days),
                "today_n": int(today_n),
                "last_date": str(last_date),
                "today_stocks": by_theme_stocks.get(th, [])[:12],
            })
        out["ok"] = True
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    finally:
        try:
            con.close()
        except Exception:
            pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--db", default="")
    args = ap.parse_args()

    snap = theme_heat_snapshot(args.days, args.top, db_path=args.db)
    if not snap["ok"]:
        print(f"[FAIL] {snap['error']}")
        return 1

    print("=" * 78)
    print(f"题材热度榜（最近 {len(snap['dates'])} 个交易日 | 截至 {snap['as_of']}）")
    print("=" * 78)
    _pr = "—" if snap["promote_pct"] is None else f"{snap['promote_pct']}%"
    print(f"当日涨停 {snap['n_latest']} 家；昨日涨停今日晋级率 {_pr}")
    print()
    print(f"{'#':>2} {'题材':<22} {'当日':>4} {'窗口':>4} {'活跃天':>4}  当日代表个股")
    for i, t in enumerate(snap["themes"], 1):
        rep = "、".join(
            f"{s['ts_code'].split('.')[0]}({s['status'] or '涨停'})"
            for s in t["today_stocks"][:5]) or "—"
        print(f"{i:>2} {t['theme'][:22]:<22} {t['today_n']:>4} {t['total_n']:>4} "
              f"{t['active_days']:>4}  {rep}")
    print("\n说明：题材归类来自 KPL 涨停原因（fact_limit_theme）；只读展示，不参与选股。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
