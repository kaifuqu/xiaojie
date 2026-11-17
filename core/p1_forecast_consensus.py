# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V27 — P1 业绩预告-预期剪刀差
================================================================================
【V27 新增·P1-D】弱方案实现（不依赖同花顺一致预期新接口）：
- 基于已有 daily_data.forecast_type + 历史 EPS 同比变化
- 估算"业绩剪刀差"分值（满分 6 分）

打分规则：
  实际增速 ≥30% AND 预期 EPS 同比 >5%  →  +6
  实际增速 ≥30% OR  预期 EPS 同比 >5%   →  +3
  预告类型"预增"但数据缺失              →  +1.5
  其他                                  →  +0

集成位置：
- core/p1_precompute.py 加 batch_compute_forecast_consensus_score
- core/strategies/score_calibration.py 加业绩预告-预期剪刀差 维度
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import pandas as pd

logger = logging.getLogger(__name__)


def batch_compute_forecast_consensus_score(stock_data_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    预计算所有股票的业绩预告-预期剪刀差分。
    返回：{ts_code: {"forecast_consensus_score": float, "np_yoy_latest": float, "eps_yoy_4q": float}}
    """
    out: Dict[str, Dict[str, Any]] = {}
    for item in stock_data_list:
        ts_code = str(item.get("code") or item.get("ts_code") or "").strip()
        if not ts_code:
            continue
        rt = item.get("hist", item.get("rt", {}))
        df = item.get("df")

        forecast_type = str(rt.get("forecast_type", "") or "")
        np_yoy_latest = 0.0
        try:
            np_yoy_latest = float(rt.get("np_yoy_latest", 0.0) or 0.0)
        except (TypeError, ValueError):
            np_yoy_latest = 0.0

        eps_yoy_4q = 0.0
        if df is not None and "eps" in df.columns and len(df) >= 4:
            try:
                s = pd.to_numeric(df["eps"].tail(4), errors="coerce").dropna()
                if len(s) >= 2:
                    base = float(s.iloc[0])
                    last = float(s.iloc[-1])
                    if abs(base) > 1e-6:
                        eps_yoy_4q = (last - base) / abs(base) * 100.0
            except Exception:
                eps_yoy_4q = 0.0

        score = 0.0
        if np_yoy_latest >= 30.0 and eps_yoy_4q > 5.0:
            score = 6.0
        elif np_yoy_latest >= 30.0 or eps_yoy_4q > 5.0:
            score = 3.0
        elif "预增" in forecast_type or "续盈" in forecast_type \
             or "pre_increase" in forecast_type.lower() \
             or "continuing" in forecast_type.lower():
            score = 1.5

        out[ts_code] = {
            "forecast_consensus_score": float(score),
            "np_yoy_latest": float(np_yoy_latest),
            "eps_yoy_4q": float(eps_yoy_4q),
        }
    return out
