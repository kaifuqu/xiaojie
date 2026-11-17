# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V27 — P1 机构资金加速度（抱团解构）
================================================================================
【V27 新增·P1-C】计算 inst_velocity = today_inst_net_buy / max(5d_avg, eps)，
并对 bias_20 > 8% + velocity < 1.0 的"抱团松动"票自动降权。

集成位置：
- core/p1_precompute.py 加 batch_compute_inst_velocity
- core/strategies/score_calibration.py 加 _compute_inst_velocity_bonus 维度 14

数据来源：daily_data.inst_net_buy（元）
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

INST_VELOCITY_EPS = 1e4  # 1 万元（避免除零）


def batch_compute_inst_velocity(stock_data_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    预计算所有股票的机构资金加速度。

    返回：{ts_code: {"inst_velocity": float, "inst_5d_avg": float, "inst_today": float}}
    """
    out: Dict[str, Dict[str, Any]] = {}
    for item in stock_data_list:
        ts_code = str(item.get("code") or item.get("ts_code") or "").strip()
        if not ts_code:
            continue
        rt = item.get("hist", item.get("rt", {}))
        df = item.get("df")

        try:
            today_inst = float(rt.get("inst_net_buy", 0) or 0.0)
        except (TypeError, ValueError):
            today_inst = 0.0

        avg_5d = 0.0
        if df is not None and "inst_net_buy" in df.columns and len(df) >= 5:
            try:
                avg_5d = float(pd.to_numeric(df["inst_net_buy"].tail(5), errors="coerce").fillna(0.0).mean())
            except Exception:
                avg_5d = 0.0

        denom = max(abs(avg_5d), INST_VELOCITY_EPS)
        velocity = today_inst / denom

        out[ts_code] = {
            "inst_velocity": float(velocity),
            "inst_5d_avg": float(avg_5d),
            "inst_today": float(today_inst),
        }
    return out


def compute_inst_velocity_bonus(
    inst_velocity: float,
    bias_20: float,
) -> Tuple[float, str]:
    """
    P1 第 14 维：机构资金加速度 + 抱团熔断。

    加分规则：
      - velocity ∈ [1.0, 1.8]   +4
      - velocity ∈ [0.5, 1.0)   +1
      - velocity > 1.8          +3（过热适度减分）
      - velocity < 0.5          +0（机构出逃/未参与）
      - bias_20 > 8% 且 velocity < 1.0  → 额外 -8（抱团松动）

    返回：(bonus, label)
    """
    try:
        v = float(inst_velocity)
        b = float(bias_20)
    except (TypeError, ValueError):
        return 0.0, "机构维度数据无效"

    bonus = 0.0
    label = ""

    if 1.0 <= v <= 1.8:
        bonus = 4.0
        label = f"机构加速{v:.2f}+4"
    elif v > 1.8:
        bonus = 3.0
        label = f"机构过热{v:.2f}+3"
    elif 0.5 <= v < 1.0:
        bonus = 1.0
        label = f"机构减速{v:.2f}+1"
    else:
        bonus = 0.0
        label = f"机构出逃/未参与{v:.2f}+0"

    if b > 8.0 and v < 1.0:
        bonus -= 8.0
        label += "; 抱团松动-8"

    return float(bonus), label
