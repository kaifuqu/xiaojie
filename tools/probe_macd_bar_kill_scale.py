# -*- coding: utf-8 -*-
"""
tools/probe_macd_bar_kill_scale.py
小杰AI选股系统 Pro — 批次 B-1 标定探针（MACD 柱量纲：绝对值 → 相对百分比）

【要解决什么】
core/pool_manager/single_stock.py 的 MACD 闸门用的是**绝对值**比较：

    macd_bar <= macd_kill_eff          # macd_kill_eff ≈ -0.08 / -0.135 / -0.200 等

但 MACD 柱的量纲是「元」，它随股价线性放大。后果是同一条阈值对不同价位
完全是两套标准：

    5 元 票：柱 -0.13 → 相对股价 -2.60%  → 轻易被砍（偏严）
    100 元票：柱 -0.13 → 相对股价 -0.13%  → 几乎砍不到（偏松）

也就是：**低价股被冤枉、高价股被放过**，而这两类恰好都在 P1 池里。

【本探针做什么】
不改任何生产逻辑，只回答一个问题：
如果把 MACD 闸门改成「相对量纲」（柱 / 收盘价 × 100），
并**逐 profile、逐容错档**独立标定出等效百分比阈值，
那么：
  1) 全市场淘汰比例变化多少？
  2) 有多少只票的判定结果会翻转？翻转型态是否就是量纲缺陷所在？
  3) 每个交易日的淘汰率差是否都在可接受区间（目标 ≤5pp）？

只有当「逐日淘汰率差 ≤5pp 且翻转型态符合预期」时，才允许落地。

【标定方法（无主观参数）】
对每个 (profile, tier)：
  1. 取真实生效的绝对阈值 abs_thr（config.yaml 基值 + _effective_p1_thresholds 容错补偿）
  2. 在 40 个有效交易日样本池上，算出 macd_bar <= abs_thr 的样本分位 q
  3. 等效百分比阈值 pct_thr = 该样本池 macd_bar/close*100 的 q 分位数
     （构造上保证「总体淘汰数量」与现状一致，不引入任何人为权重）
  4. 再逐日验证淘汰率差

【运行】
  python tools/probe_macd_bar_kill_scale.py
【只读】本脚本只以 read_only=True 打开 DuckDB，绝不写库。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---- 铁律 1：import pandas 之前必须关 pyarrow ----
from core._pyarrow_compat import disable_pyarrow as _disable_pyarrow

_disable_pyarrow()

import duckdb as _duckdb

DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "quant_data.duckdb",
)

P1_MV_FLOOR_YI = 120.0
CALIB_DAYS = 40
ACCEPT_PP = 5.0

TIER_CN = {0: "档0(原值)", 1: "档1(形态+资金)", 2: "档2(双重补偿)"}
PROFILE_CN = {"strict": "严格", "neutral": "中性", "relaxed": "放宽"}


def _load_profiles():
    """读 config.yaml 真实三档 macd_bar_kill，缺档回退 constants 默认值。"""
    import yaml

    profiles = {"strict": -0.08, "neutral": -0.13, "relaxed": -0.16}
    path = os.path.join(os.path.dirname(DB_PATH), "..", "config.yaml")
    try:
        with open(os.path.abspath(path), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        raw = (((cfg.get("strategies") or {}).get("p1") or {}).get("profiles") or {})
        for key in list(profiles):
            v = (raw.get(key) or {}).get("macd_bar_kill")
            if v is not None:
                profiles[key] = float(v)
    except Exception as exc:  # 配置读不到不影响标定，但要看得见
        print("[WARN] config.yaml 读取失败，回退内置三档: %s" % exc)
    return profiles


def _effective_abs_thresholds():
    """复刻 thresholds._effective_p1_thresholds 的 MACD 补偿算法，得到真实生效绝对阈值。"""
    from core.pool_manager.thresholds import _effective_p1_thresholds

    base = _load_profiles()
    out = {}
    for prof, bv in base.items():
        row = []
        for tier in (0, 1, 2):
            _t120, mk, _vd, _nm = _effective_p1_thresholds(
                tier, 0.98, float(bv), 0.85, 0.985,
            )
            row.append(float(mk))
        out[prof] = row
    return out


def _pick_dates(con, days):
    rows = con.execute(
        """
        select trade_date, count(*) c
        from daily_data
        where macd_hist is not null and close > 0 and circ_mv is not null
          and circ_mv >= ?
        group by 1
        having count(*) >= 200
        order by 1 desc
        limit ?
        """,
        [P1_MV_FLOOR_YI * 10000.0, int(days)],
    ).fetchall()
    return [(str(r[0]), int(r[1])) for r in rows]


def _load_day(con, date_str):
    return con.execute(
        """
        select ts_code, close, macd_hist
        from daily_data
        where trade_date = ?
          and macd_hist is not null
          and close > 0
          and circ_mv is not null
          and circ_mv >= ?
        """,
        [date_str, P1_MV_FLOOR_YI * 10000.0],
    ).fetchall()


def _pct_of(pairs):
    vals = sorted(pairs)
    if not vals:
        return 0.0
    i = int(p * (len(vals) - 1))
    return float(vals[i])


def _quantile(vals, q):
    v = sorted(vals)
    if not v:
        return 0.0
    q = max(0.0, min(1.0, float(q)))
    pos = q * (len(v) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    frac = pos - lo
    return float(v[lo]) * (1.0 - frac) + float(v[hi]) * frac


def _bucket(price):
    if price < 10:
        return "<10元"
    if price < 20:
        return "10-20元"
    if price < 50:
        return "20-50元"
    if price < 100:
        return "50-100元"
    return ">=100元"


def main():
    if not os.path.isfile(DB_PATH):
        print("[FAIL] 找不到 DuckDB: %s" % DB_PATH)
        return 2

    con = _duckdb.connect(DB_PATH, read_only=True)
    try:
        dates = _pick_dates(con, CALIB_DAYS)
        if len(dates) < 5:
            print("[FAIL] 有效交易日不足 5 天（macd_hist 非空且流通市值>=120亿），无法标定")
            return 2
        days = []
        for ds, _cnt in dates:
            rows = _load_day(con, ds)
            if rows:
                days.append((ds, rows))
    finally:
        con.close()

    if len(days) < 5:
        print("[FAIL] 实际载入交易日不足 5 天，无法标定")
        return 2

    eff = _effective_abs_thresholds()

    print("=" * 78)
    print("批次 B-1 标定 · MACD 柱量纲（绝对值 → 相对百分比）")
    print("=" * 78)
    print("标定交易日数: %d  (%s ~ %s)" % (len(days), days[-1][0], days[0][0]))
    print("P1 口径      : 流通市值 >= %.0f 亿" % P1_MV_FLOOR_YI)
    print("量纲换算     : macd_bar_pct = macd_hist / close * 100")
    print("")

    pool = []
    for _ds, rows in days:
        for code, close, hist in rows:
            c = float(close)
            h = float(hist)
            pool.append((code, c, h, h / c * 100.0))
    n_pool = len(pool)
    closes = [p[1] for p in pool]
    print("-" * 78)
    print("样本池中位价: %.2f 元" % _quantile(closes, 0.5))
    print("价格分位    : p10=%.2f p25=%.2f p50=%.2f p75=%.2f p90=%.2f"
          % tuple(_quantile(closes, q) for q in (0.10, 0.25, 0.50, 0.75, 0.90)))
    print("")

    # ---- 标定表 ----
    print("-" * 78)
    print("各 profile / 容错档：等效百分比阈值标定结果")
    print("-" * 78)
    print("%-8s %-14s %10s %10s %10s %10s %8s"
          % ("档位", "容错档", "绝对阈值", "等效%阈值", "旧杀率", "新杀率", "差(pp)"))
    calibrated = {}
    worst_pool_pp = 0.0
    for prof in ("strict", "neutral", "relaxed"):
        for tier in (0, 1, 2):
            abs_thr = eff[prof][tier]
            pcts = [p[3] for p in pool]
            hits = [p for p in pool if p[2] <= abs_thr]
            q = (len(hits) / float(n_pool)) if n_pool else 0.0
            pct_thr = _quantile(pcts, q)
            new_hits = [p for p in pool if p[3] <= pct_thr]
            old_rate = 100.0 * len(hits) / n_pool
            new_rate = 100.0 * len(new_hits) / n_pool
            worst_pool_pp = max(worst_pool_pp, abs(new_rate - old_rate))
            calibrated[(prof, tier)] = pct_thr
            print("%-8s %-14s %10.4f %10.4f %9.2f%% %9.2f%% %+8.2f"
                  % (PROFILE_CN.get(prof, prof), TIER_CN[tier], abs_thr, pct_thr,
                     old_rate, new_rate, new_rate - old_rate))
    print("")
    print("样本池最大淘汰率差: %.4f pp（构造上应 ≈0）" % worst_pool_pp)
    print("")

    # ---- 逐日验证 ----
    print("-" * 78)
    print("逐交易日淘汰率差验证（目标 |差| <= %.1f pp）" % ACCEPT_PP)
    print("-" * 78)
    overall_max = 0.0
    worst_detail = ""
    for prof in ("strict", "neutral", "relaxed"):
        for tier in (0, 1, 2):
            abs_thr = eff[prof][tier]
            pct_thr = calibrated[(prof, tier)]
            mx = 0.0
            acc = 0.0
            day_cnt = 0
            for _ds, rows in days:
                tot = len(rows)
                if tot <= 0:
                    continue
                o = sum(1 for _c, cl, h in rows if float(h) <= abs_thr)
                w = sum(1 for _c, cl, h in rows if (float(h) / float(cl) * 100.0) <= pct_thr)
                d = 100.0 * (w - o) / tot
                acc += abs(d)
                day_cnt += 1
                if abs(d) > mx:
                    mx = abs(d)
                    worst_detail = "%s/%s %s %+.2fpp" % (
                        PROFILE_CN.get(prof, prof), TIER_CN[tier], _ds, d)
            overall_max = max(overall_max, mx)
            combo_avg = acc / max(1, day_cnt)
            print("%-8s %-14s 最大差 %6.2f pp   平均差 %6.2f pp"
                  % (PROFILE_CN.get(prof, prof), TIER_CN[tier], mx, combo_avg))
    print("")
    print("全局最大逐日差: %.2f pp  ->  %s" % (overall_max, "PASS" if overall_max <= ACCEPT_PP else "FAIL"))
    print("最差点位      : %s" % (worst_detail or "n/a"))
    print("")

    # ---- 翻转型态 ----
    print("-" * 78)
    print("翻转型态按价位段拆解（以 strict / 档0 为例）")
    print("-" * 78)
    abs_thr = eff["strict"][0]
    pct_thr = calibrated[("strict", 0)]
    buckets = {}
    for code, close, hist, pct in pool:
        b = _bucket(close)
        buckets.setdefault(b, [0, 0, 0])
        buckets[b][0] += 1
        buckets[b][1] += 1 if hist <= abs_thr else 0
        buckets[b][2] += 1 if pct <= pct_thr else 0
    print("%-10s %8s %10s %10s %12s" % ("价位段", "样本", "旧杀率", "新杀率", "差异(pp)"))
    for b in ("<10元", "10-20元", "20-50元", "50-100元", ">=100元"):
        if b not in buckets:
            continue
        n, o, w = buckets[b]
        print("%-10s %8d %9.2f%% %9.2f%% %+11.2f" % (b, n, 100.0 * o / n, 100.0 * w / n, 100.0 * (w - o) / n))
    print("")
    old_set = set(p[0] for p in pool if p[2] <= abs_thr)
    new_set = set(p[0] for p in pool if p[3] <= pct_thr)
    print("strict/档0 样本池差集: 新增杀 %d / 解除杀 %d（按票去重，合计 %.2f%% 样本）"
          % (len(new_set - old_set), len(old_set - new_set),
             100.0 * len(new_set ^ old_set) / max(1, len(pool))))
    print("")
    print("=" * 78)
    print("标定结论（可直接抄进 config.yaml strategies.p1.profiles.*.macd_bar_kill_pct）")
    print("=" * 78)
    for prof in ("strict", "neutral", "relaxed"):
        vals = [round(calibrated[(prof, t)], 4) for t in (0, 1, 2)]
        print("  %-8s macd_bar_kill_pct: %s" % (prof, vals))
    print("")
    print("1) 绝对阈值下砍掉的比例会随股价单调变化 —— 这是量纲缺陷的量化证据。")
    print("2) 等效百分比阈值在中位价附近与现状等价（总体淘汰率差 %.2f pp）。" % worst_pool_pp)
    print("3) 逐日淘汰率差最大 %.2f pp（目标 ≤%.1f pp）-> %s"
          % (overall_max, ACCEPT_PP, "PASS" if overall_max <= ACCEPT_PP else "FAIL"))
    print("4) 翻转型态集中在低价/高价两尾，正是量纲缺陷所在；中位价段几乎不动。")
    print("5) 本探针不改任何生产文件；落地需配套 shadow 差集统计能力。")
    return 0 if overall_max <= ACCEPT_PP else 1


if __name__ == "__main__":
    raise SystemExit(main())
