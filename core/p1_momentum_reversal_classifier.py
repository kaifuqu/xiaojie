# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V27 — P1 动量/反转双轨分类器
================================================================================
【V27 新增·P1-A】基于单只股票的均线/量比/5日涨幅/bias_20 把候选划分为
"momentum"（动量）/ "reversal"（反转）/ "neutral"（中性），并产出双轨独立分。

集成位置：
- core/pool_manager.py _process_single_stock_for_p1 中，先分类再走对应打分线
- compute_momentum_track_score / compute_reversal_track_score 在 score_calibration
  调用 base_smooth 后再附加 15% 加权（动量加权/反转加权）
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict

import numpy as np

logger = logging.getLogger(__name__)


def classify_momentum_vs_reversal(row: Dict[str, Any]) -> str:
    """
    输入：单只股票的 rt/curr 字典（已含 ma5/ma10/ma20/ma60、pct_chg_5d/20d、vol_ratio、bias_20）
    返回："momentum" / "reversal" / "neutral"
    """
    try:
        ma5 = float(row.get("ma5", 0.0) or 0.0)
        ma10 = float(row.get("ma10", 0.0) or 0.0)
        ma20 = float(row.get("ma20", 0.0) or 0.0)
        ma60 = float(row.get("ma60", 0.0) or 0.0)
        pct_5d = float(row.get("pct_chg_5d", 0.0) or 0.0)
        pct_20d = float(row.get("pct_chg_20d", 0.0) or 0.0)
        vol_ratio = float(row.get("vol_ratio", 1.0) or 1.0)
        bias_20 = float(row.get("bias_20", 0.0) or 0.0)
    except (TypeError, ValueError):
        return "neutral"

    if ma20 <= 0 or ma60 <= 0:
        return "neutral"

    is_aligned = (ma5 > ma10 > ma20) and (ma20 > ma60)
    is_breakout = vol_ratio >= 1.5
    is_trending = pct_5d >= 5.0 and pct_20d >= 10.0

    if is_aligned and is_breakout and is_trending:
        return "momentum"

    is_pullback = abs(bias_20) <= 3.0
    is_shrink = vol_ratio <= 0.8
    is_support = ma20 >= ma60 * 0.98

    if is_pullback and is_shrink and is_support:
        return "reversal"

    return "neutral"


def compute_momentum_track_score(
    base_smooth: float,
    row: Dict[str, Any],
    max_total: float = 100.0,
) -> float:
    """
    动量线打分：base_smooth × 0.85 + 4 项动量加权（每项最高 4-5 分，上限 100）
    """
    try:
        score = float(base_smooth) * 0.85
    except (TypeError, ValueError):
        score = 0.0

    vol_ratio_3d_slope = float(row.get("vol_ratio_3d_slope", 0.0) or 0.0)
    score += min(3.0, max(0.0, vol_ratio_3d_slope * 5.0))

    slope_5d = float(row.get("ma20_slope_5", 0.0) or 0.0)
    slope_20d = float(row.get("ma20_slope_20", 0.0) or 0.0)
    slope_accel = slope_5d - slope_20d * 0.25
    if slope_accel > 0:
        score += min(4.0, slope_accel * 3.0)

    ma5_ma60_dist = float(row.get("ma5_ma60_dist_pct", 0.0) or 0.0)
    if ma5_ma60_dist > 1.0:
        score += min(4.0, ma5_ma60_dist - 1.0)

    vol_ratio = float(row.get("vol_ratio", 1.0) or 1.0)
    pct_chg = float(row.get("pct_chg", 0.0) or 0.0)
    if vol_ratio >= 2.0 and pct_chg >= 3.0:
        score += 4.0

    return min(max_total, max(0.0, score))


def compute_reversal_track_score(
    base_smooth: float,
    row: Dict[str, Any],
    max_total: float = 100.0,
) -> float:
    """
    反转线打分：base_smooth × 0.85 + 4 项反转加权
    """
    try:
        score = float(base_smooth) * 0.85
    except (TypeError, ValueError):
        score = 0.0

    bias_20 = float(row.get("bias_20", 0.0) or 0.0)
    vol_ratio = float(row.get("vol_ratio", 1.0) or 1.0)
    if bias_20 >= -2.0 and vol_ratio <= 0.8:
        score += 4.0

    max_dd_3d = float(row.get("max_drawdown_3d", 0.0) or 0.0)
    pct_chg = float(row.get("pct_chg", 0.0) or 0.0)
    if max_dd_3d < -5.0 and pct_chg >= -1.0:
        score += 3.0

    winner_rate = float(row.get("winner_rate", 0.0) or 0.0)
    cyq_conc = float(row.get("cyq_concentration", 0.0) or 0.0)
    if winner_rate >= 80.0 and cyq_conc >= 70.0:
        score += 4.0

    net_main_3d_sum = float(row.get("net_main_3d_sum", 0.0) or 0.0)
    if net_main_3d_sum > 0.0:
        score += 4.0

    # 【V26.88+10-3 P1-⑥】刚启动反转票识别：3 条规则任 2 条命中 → +8 分（用户硬约束：包括刚启动的票）
    try:
        _df = row.get("_df")
        _startup_hits = 0
        # 规则①：最近 5 日内有 2 日"下影线/实体 ≥ 1.5"
        if _df is not None and hasattr(_df, "empty") and not _df.empty and len(_df) >= 5:
            tail5 = _df.tail(5)
            _shadow_hits = 0
            for _, _r in tail5.iterrows():
                try:
                    o = float(_r.get("open", 0.0))
                    c = float(_r.get("close", 0.0))
                    h = float(_r.get("high", 0.0))
                    l = float(_r.get("low", 0.0))
                    body = abs(c - o)
                    if body > 1e-6:
                        lower_shadow = min(o, c) - l
                        if lower_shadow >= body * 1.5:
                            _shadow_hits += 1
                except (TypeError, ValueError):
                    pass
            if _shadow_hits >= 2:
                _startup_hits += 1

        # 规则②：最低点距 MA60 偏离 ≤ 3%
        ma60_val = float(row.get("ma60", 0.0) or 0.0)
        if _df is not None and hasattr(_df, "empty") and not _df.empty and ma60_val > 0:
            tail5_low = float(_df["low"].tail(5).min()) if "low" in _df.columns else 0.0
            if tail5_low > 0:
                _dev = abs(tail5_low - ma60_val) / ma60_val
                if _dev <= 0.03:
                    _startup_hits += 1

        # 规则③：近 3 日 VR 1.0~1.5（温和放大）
        if 1.0 <= float(row.get("vol_ratio", 0.0) or 0.0) <= 1.5:
            _startup_hits += 1

        if _startup_hits >= 2:
            score += 8.0
    except Exception:
        pass

    return min(max_total, max(0.0, score))
