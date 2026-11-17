# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V27 — 情绪温度计
================================================================================
【V27 新增·B3】基于"涨停家数 / 连板高度 / 炸板率"三因子 + 沪深上涨家数占比，
输出 8 类情绪状态与各池 pass_line 调整系数。

8 类情绪：
  1. 高潮期   - 涨停 ≥80 + 连板 ≥5 有 + 炸板率 < 15%
  2. 高潮末段 - 涨停 ≥60 + 炸板率 25-40%
  3. 回暖期   - 涨停 40-60 + 上涨家数 > 50%
  4. 平稳期   - 涨停 30-50 + 上涨家数 30-50%
  5. 混沌期   - 涨停家数波动 > 30%
  6. 退潮中段 - 涨停 < 30 + 跌停 > 30 + 上涨家数 < 30%
  7. 冰点期   - 涨停 < 20 + 跌停 > 40
  8. 抱团瓦解 - 涨停 < 30 + 北向连续 3 日净流出（占位）

数据来源：
- HiThink limit_up_pool（涨停池 + 炸板池 + 连板高度分布）
- 沪深 A 上涨家数占比（regime_analyzer 已有）
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


# 各情绪下各池 pass_line 调整系数
SENTIMENT_POOL_MULTIPLIER = {
    "高潮期":    {"p1": 1.10, "p2": 1.20, "p3": 1.20, "p4": 1.15, "p5": 1.10},
    "高潮末段":  {"p1": 1.05, "p2": 0.85, "p3": 0.85, "p4": 0.95, "p5": 0.90},
    "回暖期":    {"p1": 1.00, "p2": 1.05, "p3": 1.05, "p4": 1.05, "p5": 1.00},
    "平稳期":    {"p1": 1.00, "p2": 1.00, "p3": 1.00, "p4": 1.00, "p5": 1.00},
    "混沌期":    {"p1": 0.95, "p2": 0.90, "p3": 0.90, "p4": 0.95, "p5": 0.95},
    "退潮中段":  {"p1": 0.90, "p2": 0.60, "p3": 0.60, "p4": 0.70, "p5": 0.70},
    "冰点期":    {"p1": 0.85, "p2": 0.50, "p3": 0.50, "p4": 0.60, "p5": 0.60},
    "抱团瓦解":  {"p1": 0.85, "p2": 0.70, "p3": 0.70, "p4": 0.80, "p5": 0.85},
    "未知":      {"p1": 1.00, "p2": 1.00, "p3": 1.00, "p4": 1.00, "p5": 1.00},
}


def classify_market_sentiment(
    limit_up_count: int = 0,
    limit_down_count: int = 0,
    max_consecutive_limit: int = 0,
    limit_burst_rate: float = 0.0,
    up_ratio: float = 0.5,
) -> Dict[str, Any]:
    """
    返回：{"state": "高潮期"|..., "score": 0-100, "multiplier": {p1..p5}}
    """
    try:
        n_up = int(limit_up_count)
        n_down = int(limit_down_count)
        max_lb = int(max_consecutive_limit)
        burst = float(limit_burst_rate)
        upr = float(up_ratio)
    except (TypeError, ValueError):
        n_up, n_down, max_lb, burst, upr = 0, 0, 0, 0.0, 0.5

    state = "未知"

    if n_up >= 80 and max_lb >= 5 and burst < 0.15:
        state = "高潮期"
    elif n_up >= 60 and 0.25 <= burst <= 0.40:
        state = "高潮末段"
    elif 40 <= n_up < 60 and upr > 0.50:
        state = "回暖期"
    elif n_up < 20 and n_down > 40:
        state = "冰点期"
    elif n_up < 30 and n_down > 30 and upr < 0.30:
        state = "退潮中段"
    elif n_up < 30 and upr < 0.40:
        state = "抱团瓦解"
    elif n_up < 30:
        state = "混沌期"
    else:
        state = "平稳期"

    mult = SENTIMENT_POOL_MULTIPLIER.get(state, SENTIMENT_POOL_MULTIPLIER["未知"])

    score = 0
    if state == "高潮期":
        score = 95
    elif state == "高潮末段":
        score = 75
    elif state == "回暖期":
        score = 65
    elif state == "平稳期":
        score = 55
    elif state == "混沌期":
        score = 45
    elif state == "抱团瓦解":
        score = 35
    elif state == "退潮中段":
        score = 25
    elif state == "冰点期":
        score = 15

    return {
        "state": state,
        "score": score,
        "multiplier": mult,
        "metrics": {
            "limit_up_count": n_up,
            "limit_down_count": n_down,
            "max_consecutive_limit": max_lb,
            "limit_burst_rate": burst,
            "up_ratio": upr,
        },
    }


def get_pool_multiplier(state: str, pool: str) -> float:
    """便捷：根据情绪状态取某池的 pass_line 系数（缺省 1.0）。"""
    return float(SENTIMENT_POOL_MULTIPLIER.get(state, {}).get(pool, 1.0))
