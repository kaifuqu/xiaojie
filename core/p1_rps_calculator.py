# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 — P1 RPS 相对强度计算器（V26.88+10-5 新增）
================================================================================
【用途】
为 P1 池的每只票算出「N 日相对强度百分位」，供 core/p1_rank_enhancer 的
calc_rps_penalty() 做过热回避（不是加分项 —— 原因见该函数注释）。

【RPS 定义】
    raw = (close / close.shift(period) - 1) * 100        # period 日涨幅(%)
    rps = 当日全市场横截面百分位排名(0~100)
RPS=95 表示「今天这只票的 N 日涨幅排在全市场前 5%」。

【为什么 period=60 而不是 Sequoia-X 的 120】
本项目 daily_data 起始于 2026-05-20，截至修改时仅 94 个交易日。
    period=120 → 需要 121 根 → 满足股票数 = 0  ⇒ 恒为 NaN，命中 0（Sequoia-X 的坑）
    period= 60 → 需要  61 根 → 满足股票数 = 1493/2015  ⇒ 可用
随着日线窗口扩到 121 根以上（MAX_DAYS=120 已接近），可把 RPS_PERIOD 提回 120。
届时请重新扫描 RPS 分层收益，确认「倒 U 形」结论是否仍成立。

【与 ret60 字段的关系】
daily_data 表中**没有** ret60/pct_chg_60 这类落库列（已核实），
因此本模块直接用 close.shift(60) 现算，不依赖任何预计算列。

【稳健性】
- 只读连接（不与 daemon 抢写锁）
- 任何异常都上抛给调用方，由 pool_builder 捕获后降级为「无 RPS 维度」
================================================================================
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# RPS 观察周期（交易日）。需满足 period + 1 <= 实际可用交易日数。
RPS_PERIOD = 60


def _norm_code(c: object) -> str:
    """Normalize to daily_data ts_code form: 6 digits + .SH/.SZ suffix."""
    s = str(c or "").strip().upper()
    if not s:
        return ""
    if "." in s:
        return s
    # 纯 6 位数字：按首位推断交易所（沿用 data.fetcher.get_p0_codes 的同一约定）
    if s.isdigit() and len(s) == 6:
        return f"{s}.SH" if s.startswith("6") else f"{s}.SZ"
    return s


def compute_rps_map(
    period: int = RPS_PERIOD,
    codes: Optional[List[str]] = None,
) -> Dict[str, float]:
    """
    计算「最新交易日」横截面的 RPS 百分位。

    Args:
        period: 观察周期（交易日）
        codes:  可选。若给出，只保留这些代码（仍按全市场做排名，口径与整体一致）

    Returns:
        {ts_code: rps_percentile(0~100)}；数据不足或库不可用时返回空 dict
    """
    if period is None or int(period) < 5:
        logger.debug("[RPS] period=%s 过小，跳过", period)
        return {}
    period = int(period)

    from data.db_core import get_read_conn_singleton

    con = get_read_conn_singleton(max_wait_sec=30.0)
    if con is None:
        logger.warning("[RPS] 只读连接不可用，跳过 RPS 计算")
        return {}

    df = con.execute(
        "SELECT ts_code, trade_date, close FROM daily_data ORDER BY ts_code, trade_date"
    ).df()
    if df is None or df.empty:
        logger.warning("[RPS] daily_data 为空，跳过 RPS 计算")
        return {}

    df["ts_code"] = df["ts_code"].astype(str).str.strip().str.upper()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df[(df["close"].notna()) & (df["close"] > 0)]
    if df.empty:
        return {}

    g = df.groupby("ts_code", sort=False)
    base = g["close"].shift(period)
    raw = (df["close"] / base - 1.0) * 100.0
    ok = base.notna() & (base > 0)
    if not ok.any():
        logger.warning(
            "[RPS] period=%d 需 %d 根 K 线，当前最多 %d 根 → 全部 NaN。"
            "请调大 constants.MAX_DAYS 或调小 RPS_PERIOD。",
            period, period + 1, int(g["close"].count().max()),
        )
        return {}

    work = df.assign(_rps_raw=raw)[ok.values]
    if work.empty:
        return {}

    # 只对「最新交易日」排名；多日则逐日排名后取每票最新值，避免用到未来数据
    work["trade_date"] = work["trade_date"].astype(str)
    work = work.sort_values(["trade_date", "ts_code"])
    work["_rps"] = work.groupby("trade_date")["_rps_raw"].rank(pct=True) * 100.0
    latest = work.groupby("ts_code", sort=False).tail(1)

    if codes:
        want = {_norm_code(c) for c in codes if str(c or "").strip()}
        latest = latest[latest["ts_code"].isin(want)]

    out = {
        str(r["ts_code"]): float(r["_rps"])
        for _, r in latest.iterrows()
        if r["_rps"] == r["_rps"]  # 过滤 NaN
    }
    logger.info(
        "[RPS] period=%d 覆盖 %d 只（有效 %d）", period,
        int(g["close"].count().size), len(out),
    )
    return out


def attach_rps_to_items(
    codes: List[str],
    period: int = RPS_PERIOD,
) -> Dict[str, float]:
    """
    对外入口：返回 {原始代码(已归一化): rps}，供 pool_builder 注入到池 item。

    同时兼容调用方传 '000001' 与 '000001.SZ' 两种写法（返回键统一为带后缀形态）。
    """
    rps = compute_rps_map(period=period)
    if not rps:
        return {}
    want = {_norm_code(c) for c in codes if str(c or "").strip()}
    return {c: rps[c] for c in want if c in rps}
