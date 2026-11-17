# -*- coding: utf-8 -*-
"""
tools/_test_p1_pool_v3014.py —— V30.14 主池不设上限 / 千亿蓝筹加分 / 防一日游 回归

【本次改动背景 2026-11-05】
用户看到很多股票大涨（如阳光电源 300274 连涨 3 日，流通市值 1288 亿）却没有任何提示。
根因排查结论：
  · 它不在当日 P1 主池的 30 只里 → P2/P3/P5 整链不会给它打分，自然没有推送；
  · 硬闸（120 亿市值 / PE / 价格红线 / MA20 前闸）其实**全部放行**，
    真正挡住它的是打分口径（旧逻辑还会在主池不足 30 只时下调 min_score 凑数）。

【本测试锁定的契约】
  1. 主池不设数量上限：分数达标就入池，不再「凑满 30 只」而降分
  2. 空池兜底仍然存在（池子为空时才放宽，避免空池打死下游）
  3. 平衡调整不再按 1.5x 截断分数达标的票
  4. 千亿蓝筹加分：≥1000 亿拿到加分且换手底线放宽；中小盘完全不受影响
  5. 防一日游：冲高回落 / 放量闷杀 → 否决
  6. 防一日游：真正的强势（收在当日高位）→ 放行，不误杀
  7. 防一日游：跌深后单日暴涨但未站上 MA20 → 只降分不否决（保留启动点）
  8. 配置缺失时行为与默认值一致（不让缺配置变成静默失效）
"""
from __future__ import annotations

import io
import os
import sys

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

_OUT = io.open(os.path.join(ROOT, "tools", "_p1_v3014_result.txt"), "w", encoding="utf-8")
FAILS = []


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    _OUT.write(s + "\n")


def check(label, ok, detail=""):
    if ok:
        p("  [PASS] %s  %s" % (label, detail))
    else:
        FAILS.append(label)
        p("  [FAIL] %s  %s" % (label, detail))
    return ok


def _mk_df(rows):
    """构造最小可用的日线 df（含形态判定所需列）。"""
    return pd.DataFrame(rows)


def _row(o, h, l, c, pct, vol, ma20=0.0, ma60=0.0, ma120=0.0, slope=0.0):
    return {
        "open": o, "high": h, "low": l, "close": c, "pct_chg": pct, "vol": vol,
        "ma20": ma20, "ma60": ma60, "ma120": ma120, "ma20_slope_5": slope,
    }


def main():
    from core.pool_manager.thresholds import (
        _p1_large_cap_bonus,
        _p1_one_day_trip_check,
    )
    from core.config_manager import (
        get_p1_large_cap_bonus_config,
        get_p1_one_day_trip_config,
    )

    p("=" * 74)
    p("【V30.14 主池不设上限 / 千亿蓝筹加分 / 防一日游 回归】")
    p("=" * 74)

    # ---------------- 1. 主池数量上限已取消 ----------------
    p("\n=== 1. 主池不再封顶（分数达标即入池）===")
    src = io.open("core/pool_manager/pool_builder.py", encoding="utf-8").read()
    check("1a 不存在「<30 只就降分凑数」的硬目标",
          "_MIN_P1_POOL_TARGET = 0" in src and "凑够 30 只" not in src)
    check("1b 空池兜底仍然保留", "if len(p1_pool) == 0:" in src)
    check("1c 兜底文案已改为空池语义", "空池兜底" in src)

    from core.p1_balance import BALANCE_PARAMS
    check("1d 平衡调整不再按 1.5x 截断", float(BALANCE_PARAMS.get("max_pool_size_ratio", 1.5)) <= 0,
          "ratio=%s" % BALANCE_PARAMS.get("max_pool_size_ratio"))

    # ---------------- 2. 平衡调整不截断分数达标的票 ----------------
    p("\n=== 2. 平衡调整：分数达标的回调票不再被池上限丢掉 ===")
    from core.p1_balance import enforce_p1_balance

    # 30 只全是大盘股（触发「大盘占比 > 50%」不平衡），另备 8 只中盘待回调
    pool = [
        {"code": "6000%02d.SH" % i, "p1_score": 70.0, "hist": {"circ_mv": 6000000.0, "name": "大%d" % i},
         "df": pd.DataFrame([{"close": 10.0}])}
        for i in range(30)
    ]
    rejected = [
        {"代码": "0000%02d.SZ" % i, "名称": "中%d" % i, "当前得分": 60.0}
        for i in range(8)
    ]
    src_map = {}
    for r, i in zip(rejected, range(8)):
        src_map[id(r)] = {
            "code": "0000%02d.SZ" % i, "p1_score": 60.0,
            "hist": {"circ_mv": 2000000.0, "name": "中%d" % i},
            "df": pd.DataFrame([{"close": 10.0}]),
        }
    merged, rej = enforce_p1_balance(p1_pool=pool, p1_rejected=rejected, target_size=len(pool), rejected_src=src_map)
    p("  平衡后池大小 = %d（原 30，不含上限截断则应 > 30）" % len(merged))
    check("2a 回调票未被 1.5x 上限截断", len(merged) >= 30, "len=%d" % len(merged))

    # ---------------- 3. 千亿蓝筹加分 ----------------
    p("\n=== 3. 千亿蓝筹加分：≥1000 亿得分，中小盘零影响 ===")
    lc_cfg = get_p1_large_cap_bonus_config()
    p("  配置: min_circ_mv_yi=%.0f max_bonus=%.1f relax=%.2f"
      % (lc_cfg["min_circ_mv_yi"], lc_cfg["max_bonus"], lc_cfg["turnover_relax_factor"]))

    bull_row = _row(80, 82, 79, 81.5, 3.0, 1e6, ma20=75.0, ma60=70.0, ma120=65.0, slope=0.5)
    bull_df = _mk_df([_row(78, 79, 77, 78.5, 1.0, 8e5), bull_row])

    big = _p1_large_cap_bonus(1288.5, bull_row, bull_df, vol_ratio=1.6,
                              pct_chg_today=3.0, flow_positive_days_5=4, cfg=lc_cfg)
    p("  1288 亿: bonus=%.1f labels=%s" % (big["bonus"], big["labels"]))
    check("3a 千亿蓝筹拿到加分", big["bonus"] > 0, "bonus=%.1f" % big["bonus"])
    check("3b 加分不超过上限", big["bonus"] <= lc_cfg["max_bonus"])
    check("3c 换手底线被放宽", big["turnover_relax"] < 1.0, "relax=%.2f" % big["turnover_relax"])

    small = _p1_large_cap_bonus(300.0, bull_row, bull_df, vol_ratio=1.6,
                                pct_chg_today=3.0, flow_positive_days_5=4, cfg=lc_cfg)
    check("3d 中小盘不受影响（0 分 + 不放宽换手）",
          small["bonus"] == 0.0 and small["turnover_relax"] == 1.0)

    # 阳光电源 11-04 的形态复刻：连涨 3 日、收盘贴近最高
    sun_row = _row(76.5, 81.5, 74.5, 81.17, 7.2, 1094003.0, ma20=74.54, ma60=70.0, ma120=68.0, slope=0.3)
    sun_df = _mk_df([
        _row(62.0, 72.0, 62.0, 71.54, 9.57, 1021437.0, ma20=74.96),
        _row(72.03, 76.98, 71.54, 75.72, 5.84, 961468.0, ma20=74.6),
        sun_row,
    ])
    sun = _p1_large_cap_bonus(1288.5, sun_row, sun_df, vol_ratio=1.73,
                              pct_chg_today=7.2, flow_positive_days_5=3, cfg=lc_cfg)
    p("  阳光电源形态: bonus=%.1f labels=%s" % (sun["bonus"], sun["labels"]))
    check("3e 阳光电源这类形态能拿到蓝筹加分", sun["bonus"] > 0, "bonus=%.1f" % sun["bonus"])

    # ---------------- 4. 防一日游：否决 ----------------
    p("\n=== 4. 防一日游：拉高回落 / 放量闷杀 → 否决 ===")
    odt_cfg = get_p1_one_day_trip_config()

    # 冲高回落：涨 7% 但收盘回到当日下三分之一
    dump_row = _row(100.0, 108.0, 99.5, 100.2, 6.5, 3e6, ma20=95.0)
    dump_df = _mk_df([
        _row(94, 96, 93, 95.5, 1.2, 1e6),
        _row(95.5, 97, 95, 96.5, 1.0, 1e6),
        dump_row,
    ])
    r1 = _p1_one_day_trip_check(dump_df, dump_row, pct_chg_today=6.5, cfg=odt_cfg)
    p("  冲高回落: veto=%s" % (r1["veto"],))
    check("4a 冲高回落被否决", bool(r1["veto"]))

    # 放量闷杀：量能 3 倍但收 -5%
    kill_row = _row(100.0, 101.0, 94.0, 95.0, -5.0, 3.2e6, ma20=98.0)
    kill_df = _mk_df([
        _row(97, 99, 96, 98.5, 1.5, 1e6),
        _row(99, 101, 98, 100.0, 2.0, 1e6),
        kill_row,
    ])
    r2 = _p1_one_day_trip_check(kill_df, kill_row, pct_chg_today=-5.0, cfg=odt_cfg)
    p("  放量闷杀: veto=%s" % (r2["veto"],))
    check("4b 放量闷杀被否决", bool(r2["veto"]))

    # ---------------- 5. 防一日游：不误杀强势 ----------------
    p("\n=== 5. 防一日游：收在高位的强势票必须放行（不能误杀）===")
    r3 = _p1_one_day_trip_check(sun_df, sun_row, pct_chg_today=7.2, cfg=odt_cfg)
    p("  阳光电源形态: veto=%s penalty=%.1f labels=%s"
      % (r3["veto"], r3["penalty"], r3["labels"]))
    check("5a 收在高位的连涨票不被否决", not r3["veto"])

    # 跌深后第一根放量反转阳线：不否决（这是要抓的启动点）
    revive_row = _row(66.0, 71.0, 65.5, 70.6, 8.0, 1.5e6, ma20=74.0)
    revive_df = _mk_df([
        _row(72, 73, 68, 68.5, -3.5, 5e5),
        _row(68.5, 69, 66, 66.2, -4.0, 5e5),
        _row(66.2, 67, 64.5, 65.3, -2.0, 6e5),
        _row(65.5, 66, 64.0, 65.0, -1.5, 5e5),
        _row(65.0, 66, 64.5, 65.4, -1.0, 5e5),
        revive_row,
    ])
    r4 = _p1_one_day_trip_check(revive_df, revive_row, pct_chg_today=8.0, cfg=odt_cfg)
    p("  跌深反转: veto=%s penalty=%.1f labels=%s"
      % (r4["veto"], r4["penalty"], r4["labels"]))
    check("5b 跌深反转阳线不被否决（只降分）", not r4["veto"])
    check("5c 未站上 MA20 的暴涨记入降分", r4["penalty"] > 0, "penalty=%.1f" % r4["penalty"])

    # ---------------- 6. 配置缺失时的默认行为 ----------------
    p("\n=== 6. 缺配置时走默认值（不静默失效）===")
    empty_cfg = {}
    r5 = _p1_one_day_trip_check(dump_df, dump_row, pct_chg_today=6.5, cfg=empty_cfg)
    check("6a 缺配置时一日游判定仍生效", bool(r5["veto"]))
    big2 = _p1_large_cap_bonus(1288.5, bull_row, bull_df, vol_ratio=1.6,
                               pct_chg_today=3.0, flow_positive_days_5=4, cfg=empty_cfg)
    check("6b 缺配置时蓝筹加分仍生效", big2["bonus"] > 0, "bonus=%.1f" % big2["bonus"])

    # ---------------- 7. 异常输入不阻断主链路 ----------------
    p("\n=== 7. 异常输入 fail-open（绝不阻断 P1）===")
    r6 = _p1_one_day_trip_check(pd.DataFrame(), None, cfg=odt_cfg)
    check("7a 空 df 不否决", not r6["veto"])
    r7 = _p1_one_day_trip_check(None, None, cfg=odt_cfg)
    check("7b df=None 不抛异常", not r7["veto"])
    big3 = _p1_large_cap_bonus(0.0, {}, None, cfg=lc_cfg)
    check("7c 缺数据时零加分不抛异常", big3["bonus"] == 0.0)

    p("\n" + "=" * 74)
    if FAILS:
        p("失败 %d 项：%s" % (len(FAILS), FAILS))
        return 1
    p("✅ V30.14 主池不限量 / 千亿蓝筹加分 / 防一日游 回归全部通过")
    p("=" * 74)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        try:
            _OUT.close()
        except Exception:
            pass
