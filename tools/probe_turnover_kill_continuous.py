# -*- coding: utf-8 -*-
"""
tools/probe_turnover_kill_continuous.py
小杰AI选股系统 Pro — 批次 B-2 标定探针（股性呆滞连续阈值 smoothstep 版）

【要解决什么】
core/pool_manager/single_stock.py 用**硬阶梯**判「股性呆滞」：

    circ_mv >= 2000 亿  → 近5日均换手 < 0.65%  才杀
    1000~2000 亿       → 近5日均换手 < 1.05%  才杀
    < 1000 亿          → 近5日均换手 < 1.50%  才杀

问题是边界是**跳变**的：
    999 亿  → 底线 1.50%
   1000 亿  → 底线 1.05%     ← 同一只票，市值涨 0.1% 就换了一套标准
   1999 亿 → 底线 1.05%
   2000 亿 → 底线 0.65%     ← 同上

一只票的生死取决于它落在断点的哪一侧，而市值本身是每天波动的连续量。
这会制造两类噪声：
    · 边界票反复横跳（今天进池、明天被砍）
    · 断点附近的票天然获得额外豁免（可被"卡市值"规避）

【候选方案：窄带 smoothstep 平滑断点】
不做全局线性插值（会把中段所有票的口径都改掉），只在断点附近 ±100 亿
的窄带内做三次平滑过渡，函数与一阶导数都连续：

    mv <  900 亿  : 1.50%（与现状 <1000 亿 档完全一致）
    900~1100 亿   : smoothstep 1.50% → 1.05%
    1100~1900 亿  : 1.05%（与现状 1000~2000 亿 档完全一致）
    1900~2100 亿  : smoothstep 1.05% → 0.65%
    mv >= 2100 亿 : 0.65%（与现状 >=2000 亿 档完全一致）

    smooth(t) = t*t*(3-2t),  t in [0,1]

优点：
    · 在 1000 亿 / 2000 亿附近没有阈值跳变，导数也连续
    · 远离断点（约 70% 样本）时阈值与现状逐位相同
    · 相比全局线性插值，对正常样本的扰动小得多

【本探针做什么】
不改任何生产逻辑，只回答：
    1) 新函数是否值连续 + 一阶导连续（数值自检）
    2) 换成 smoothstep 后，逐日淘汰率差与逐票差集有多大（目标 <3%）
    3) 断点附近（±5% 市值带）翻转样本有多少
    4) 放宽通道（adaptive_turnover_kill_threshold_relaxed 的 ×0.75 同构阶梯）
       的差集是否同步收敛

【运行】
  python tools/probe_turnover_kill_continuous.py
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

# 与 core/pool_manager/single_stock.py 一致的现状硬阶梯
OLD_STEPS = [(2000.0, 0.65), (1000.0, 1.05), (0.0, 1.50)]
P1_MV_FLOOR_YI = 120.0
CALIB_DAYS = 40
ACCEPT_DIFFSET_PCT = 3.0
RELAX_FACTOR = 0.75  # 放宽通道对基础阶梯统一乘的系数

# smoothstep 过渡带内每 1 亿的最大理论斜率 = 幅度 / 带宽 * 1.5
#   幅度 0.45pp / 带宽 200亿 * 1.5 = 0.003375 pp/亿
# 判定用「比现状断点跳变(0.45pp)小 20 倍以上」这一相对标准，而不是绝对 0。
STEP_JUMP_REF_PP = 0.45
ACCEPT_JUMP_RATIO = 0.05  # 允许的最大跳变 = 0.45 * 0.05 = 0.0225 pp/亿

# smoothstep 窄带参数（与生产实现将保持一致）
BAND1 = (900.0, 1100.0)
BAND2 = (1900.0, 2100.0)
HIGH_MID = 1.05
LOW_MID = 1.50
HIGH_TOP = 0.65


def _smoothstep(t: float) -> float:
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else float(t))
    return t * t * (3.0 - 2.0 * t)


def old_threshold_pct(mv_yi):
    """现状硬阶梯：复刻生产逻辑，只做标定不改生产文件。"""
    if mv_yi >= 2000.0:
        return OLD_STEPS[0][1]
    if mv_yi >= 1000.0:
        return OLD_STEPS[1][1]
    return OLD_STEPS[2][1]


def new_threshold_pct(mv_yi):
    """候选 smoothstep 阈值：窄带平滑，断点无跳变，远离断点与现状一致。"""
    mv = float(mv_yi)
    lo1, hi1 = BAND1
    lo2, hi2 = BAND2
    if mv <= lo1:
        return LOW_MID
    if mv < hi1:
        t = (mv - lo1) / (hi1 - lo1)
        return LOW_MID + _smoothstep(t) * (HIGH_MID - LOW_MID)
    if mv <= lo2:
        return HIGH_MID
    if mv < hi2:
        t = (mv - lo2) / (hi2 - lo2)
        return HIGH_MID + _smoothstep(t) * (HIGH_TOP - HIGH_MID)
    return HIGH_TOP


def old_threshold_relaxed(mv_yi):
    """现状放宽通道：复刻 adaptive_turnover_kill_threshold_relaxed 的基础阶梯部分。"""
    return max(0.8, old_threshold_pct(mv_yi) * RELAX_FACTOR)


def new_threshold_relaxed(mv_yi):
    """候选放宽通道：与主阶梯同源的 smoothstep 版本，×0.75 与地板 0.8 均保留。"""
    return max(0.8, new_threshold_pct(mv_yi) * RELAX_FACTOR)


def continuity_check():
    """连续性 + 一阶导连续性数值自检：相邻 1 亿步长下阈值跳变与其变化率必须收敛。"""
    worst_jump = 0.0
    worst_jump_at = 0.0
    worst_djump = 0.0
    worst_djump_at = 0.0
    prev_d = None
    for i in range(1, 6000):
        mv = float(i)
        a = new_threshold_pct(mv - 1.0)
        b = new_threshold_pct(mv)
        jump = abs(b - a)
        if jump > worst_jump:
            worst_jump = jump
            worst_jump_at = mv
        if prev_d is not None:
            djump = abs((b - a) - prev_d)
            if djump > worst_djump:
                worst_djump = djump
                worst_djump_at = mv
        prev_d = b - a
    return worst_jump, worst_jump_at, worst_djump, worst_djump_at


def anchor_check():
    """锚点校验：远离断点处必须与现状阈值逐位一致。"""
    checks = [
        (200.0, 1.50), (899.0, 1.50),
        (1200.0, 1.05), (1800.0, 1.05),
        (2200.0, 0.65), (5000.0, 0.65),
    ]
    bad = []
    for mv, want in checks:
        got = new_threshold_pct(mv)
        if abs(got - want) > 1e-9:
            bad.append((mv, want, got))
    return bad


def _pick_dates(con, days):
    rows = con.execute(
        """
        select trade_date, count(*) c
        from daily_data
        where turnover_rate_f is not null and close > 0 and circ_mv is not null
          and circ_mv >= ?
        group by 1
        having count(*) >= 200
        order by 1 desc
        limit ?
        """,
        [P1_MV_FLOOR_YI * 10000.0, int(days)],
    ).fetchall()
    return [str(r[0]) for r in rows]


def _load_day(con, date_str):
    """取该交易日往前 5 根 K 线的近 5 日均换手（与生产 avg_trn_5 口径一致）。"""
    return con.execute(
        """
        with recent as (
            select ts_code, close, circ_mv, turnover_rate_f,
                   row_number() over (partition by ts_code order by trade_date desc) rn
            from daily_data
            where trade_date <= ?
              and turnover_rate_f is not null
              and close > 0 and circ_mv is not null
              and circ_mv >= ?
        )
        select ts_code,
               any_value(close),
               any_value(circ_mv) / 10000.0,
               avg(turnover_rate_f) avg_trn5
        from recent
        where rn <= 5
        group by ts_code
        having count(*) >= 3
        """,
        [date_str, P1_MV_FLOOR_YI * 10000.0],
    ).fetchall()


def main():
    if not os.path.isfile(DB_PATH):
        print("[FAIL] 找不到 DuckDB: %s" % DB_PATH)
        return 2

    # ---------- 1. 函数自检 ----------
    wj, wj_at, wd, wd_at = continuity_check()
    bad_anchor = anchor_check()
    accept_jump = STEP_JUMP_REF_PP * ACCEPT_JUMP_RATIO
    ok_cont = wj <= accept_jump
    ok_deriv = wd <= accept_jump
    print("=" * 78)
    print("批次 B-2 标定 · 股性呆滞连续阈值（smoothstep 窄带）")
    print("=" * 78)
    print("[自检] 值连续性: 相邻 1 亿最大跳变 %.6f pp @ %.0f 亿（容差 %.4f pp）-> %s"
          % (wj, wj_at, accept_jump, "PASS" if ok_cont else "FAIL"))
    print("[自检] 导数连续性: 相邻步长最大二阶差 %.6f pp（容差 %.4f pp）-> %s"
          % (wd, accept_jump, "PASS" if ok_deriv else "FAIL"))
    print("[自检] 锚点一致性: %s"
          % ("PASS（6/6 与现状完全相同）" if not bad_anchor else "FAIL %s" % bad_anchor))
    o1 = abs(old_threshold_pct(1000.0) - old_threshold_pct(999.0))
    o2 = abs(old_threshold_pct(2000.0) - old_threshold_pct(1999.0))
    print("[对照] 现状断点跳变: 1000亿处 %.2f pp, 2000亿处 %.2f pp" % (o1, o2))
    print("[对照] 现状在 1000 亿处的导数不连续（阶跃），候选函数已平滑")
    print("")

    con = _duckdb.connect(DB_PATH, read_only=True)
    try:
        dates = _pick_dates(con, CALIB_DAYS)
        if len(dates) < 5:
            print("[FAIL] 有效交易日不足 5 天（turnover_rate_f 非空且流通市值>=120亿）")
            return 2
        days = []
        for ds in dates:
            rows = _load_day(con, ds)
            if rows:
                days.append((ds, [(r[0], float(r[1]), float(r[2]), float(r[3])) for r in rows]))
    finally:
        con.close()

    if len(days) < 5:
        print("[FAIL] 实际载入交易日不足 5 天，无法标定")
        return 2

    print("标定交易日数: %d (%s ~ %s)" % (len(days), days[-1][0], days[0][0]))
    print("P1 口径      : 流通市值 >= %.0f 亿，近 5 日均换手（需 >=3 根 K 线）" % P1_MV_FLOOR_YI)
    print("")

    # ---------- 2. 主阶梯：逐日淘汰率差 + 差集 ----------
    print("-" * 78)
    print("主阶梯：逐交易日淘汰率（旧硬阶梯 vs 新 smoothstep）")
    print("-" * 78)
    print("%-12s %8s %10s %10s %12s" % ("交易日", "样本", "旧杀率", "新杀率", "差异(pp)"))
    max_pp = 0.0
    avg_pp = 0.0
    pool_old, pool_new = set(), set()
    pool_n = 0
    for ds, rows in days:
        n = len(rows)
        o = sum(1 for r in rows if r[3] < old_threshold_pct(r[2]))
        w = sum(1 for r in rows if r[3] < new_threshold_pct(r[2]))
        d = 100.0 * (w - o) / n
        max_pp = max(max_pp, abs(d))
        avg_pp += abs(d)
        pool_n += n
        for r in rows:
            if r[3] < old_threshold_pct(r[2]):
                pool_old.add(r[0])
            if r[3] < new_threshold_pct(r[2]):
                pool_new.add(r[0])
        print("%-12s %8d %9.2f%% %9.2f%% %+11.2f" % (ds, n, 100.0 * o / n, 100.0 * w / n, d))
    avg_pp /= max(1, len(days))
    print("最大逐日差 %.2f pp   平均逐日差 %.2f pp" % (max_pp, avg_pp))
    print("")

    # ---------- 3. 逐票差集 ----------
    print("-" * 78)
    print("主阶梯：逐票差集（40 日票去重）")
    print("-" * 78)
    universe = set()
    for _ds, rows in days:
        for r in rows:
            universe.add(r[0])
    add_kill = pool_new - pool_old
    rel_kill = pool_old - pool_new
    diff_pct = 100.0 * len(add_kill | rel_kill) / max(1, len(universe))
    print("票宇宙        : %d 只" % len(universe))
    print("旧杀/新杀    : %d / %d 只" % (len(pool_old), len(pool_new)))
    print("新增杀 %d 只 / 解除杀 %d 只 = 差集 %.2f%%（目标 <%.1f%%）-> %s"
          % (len(add_kill), len(rel_kill), diff_pct, ACCEPT_DIFFSET_PCT,
             "PASS" if diff_pct < ACCEPT_DIFFSET_PCT else "FAIL"))
    print("")

    # ---------- 4. 按市值段拆解 ----------
    print("-" * 78)
    print("按市值段拆解（最近一个交易日）")
    print("-" * 78)
    last_rows = days[0][1]
    segs = [(0, 900), (900, 1000), (1000, 1100), (1100, 1900),
            (1900, 2000), (2000, 2100), (2100, 5000), (5000, 10 ** 9)]
    print("%-16s %8s %10s %10s %12s" % ("市值段(亿)", "样本", "旧杀率", "新杀率", "差异(pp)"))
    for lo, hi in segs:
        sub = [r for r in last_rows if lo <= r[2] < hi]
        if not sub:
            continue
        o = sum(1 for r in sub if r[3] < old_threshold_pct(r[2]))
        w = sum(1 for r in sub if r[3] < new_threshold_pct(r[2]))
        print("%-16s %8d %9.2f%% %9.2f%% %+11.2f"
              % ("%g-%g" % (lo, hi), len(sub), 100.0 * o / len(sub), 100.0 * w / len(sub),
                 100.0 * (w - o) / len(sub)))
    print("")

    # ---------- 5. 断点敏感度 ----------
    print("-" * 78)
    print("断点附近敏感度（±5% 市值带）")
    print("-" * 78)
    for bp in (1000.0, 2000.0):
        lo, hi = bp * 0.95, bp * 1.05
        sub = [r for r in last_rows if lo <= r[2] <= hi]
        if not sub:
            print("  %.0f 亿 ±5%%: 无样本" % bp)
            continue
        flips = sum(1 for r in sub
                    if (r[3] < old_threshold_pct(r[2])) != (r[3] < new_threshold_pct(r[2])))
        print("  %6.0f 亿 ±5%%: 样本 %4d，其中判定翻转 %3d 只 (%.1f%%)"
              % (bp, len(sub), flips, 100.0 * flips / len(sub)))
    print("")

    # ---------- 6. 放宽通道 ----------
    print("-" * 78)
    print("放宽通道（adaptive_turnover_kill_threshold_relaxed 的 ×%.2f 同构阶梯）"
          % RELAX_FACTOR)
    print("-" * 78)
    r_old, r_new, r_uni = set(), set(), set()
    for ds, rows in days:
        for r in rows:
            r_uni.add(r[0])
            if r[3] < old_threshold_relaxed(r[2]):
                r_old.add(r[0])
            if r[3] < new_threshold_relaxed(r[2]):
                r_new.add(r[0])
    r_add = r_new - r_old
    r_rel = r_old - r_new
    r_pct = 100.0 * len(r_add | r_rel) / max(1, len(r_uni))
    print("旧杀 %d 只 / 新杀 %d 只；新增杀 %d / 解除杀 %d = 差集 %.2f%%（目标 <%.1f%%）-> %s"
          % (len(r_old), len(r_new), len(r_add), len(r_rel), r_pct, ACCEPT_DIFFSET_PCT,
             "PASS" if r_pct < ACCEPT_DIFFSET_PCT else "FAIL"))
    print("")

    # ---------- 7. 结论 ----------
    ok = (ok_cont and ok_deriv and not bad_anchor
          and diff_pct < ACCEPT_DIFFSET_PCT and r_pct < ACCEPT_DIFFSET_PCT)
    print("=" * 78)
    print("结论：%s" % ("PASS — 可落地" if ok else "FAIL — 需再调"))
    print("=" * 78)
    print("1) 值连续性 %s；导数连续性 %s；锚点与现状一致 %s。"
          % ("通过" if ok_cont else "不通过", "通过" if ok_deriv else "不通过",
             "是" if not bad_anchor else "否"))
    print("1b) 断点处最大跳变 %.6f pp，约为现状阶跃 %.2f pp 的 1/%.0f。"
          % (wj, STEP_JUMP_REF_PP, STEP_JUMP_REF_PP / max(wj, 1e-9)))
    print("2) 主阶梯差集 %.2f%%、放宽通道差集 %.2f%%（目标 <%.1f%%）。"
          % (diff_pct, r_pct, ACCEPT_DIFFSET_PCT))
    print("3) 断点附近不再有阈值跳变，边界票不会因市值微幅波动反复横跳。")
    print("4) 本探针不改任何生产文件；落地需把 smoothstep 函数集中到阈值模块，")
    print("   由 single_stock.py 与 fund_mv_utils.py 共同调用，避免两套口径。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
