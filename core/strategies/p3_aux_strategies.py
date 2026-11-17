# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V27 — P3 辅助子策略合集（B1/B2）
================================================================================
【V27 新增；V35 编号重整】
- B1：均线粘合度战法（盘中"启动临界点"识别）→ 现编号 P3-10（旧编号 P3-15(B1)）
- B2：板块轮动识别器（轮动日 vs 延续日，工具函数，非战法）
- （B4 趋势延续强度两战法已随 V29 P3 精简从代码层彻底删除）

这些策略供 P3 调用，与 P3-01~P3-11 主战法并列（旧编号→新编号对照见 p3_intraday_screener 模块头）。
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ==================== B1：均线粘合度战法 ====================
def strategy_ma_convergence_breakout(
    df: pd.DataFrame,
    cfg: Optional[Dict[str, Any]] = None,
    rt: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, str]:
    """
    B1 盘中均线粘合 + 突破：
      - 当前 5 日均线粘合度（MA5/MA10/MA20 三者极差 / MA20）< 1.5%
      - 现价突破粘合区间 ≥2.0%

    【V29 修复】原实现用 df['close']（是昨收盘价）判断“今日盘中突破”，
    时序错位（盘中 09:30～盘中的信号使用了昨日收盘数据）。
    现改为优先取 rt['price'] 实时现价，缺失时回退到 df['close']。
    """
    cfg = cfg or {}
    rt = rt or {}
    if df is None or len(df) < 20:
        return False, ""

    try:
        ma5 = float(df["ma5"].iloc[-1])
        ma10 = float(df["ma10"].iloc[-1])
        ma20 = float(df["ma20"].iloc[-1])
        close = float(df["close"].iloc[-1])
    except (KeyError, TypeError, ValueError):
        return False, ""

    if ma20 <= 0:
        return False, ""

    now_px = rt.get("price", None)
    try:
        now_px = float(now_px)
    except (TypeError, ValueError):
        now_px = 0.0
    if now_px <= 0:
        now_px = close

    convergence = (max(ma5, ma10, ma20) - min(ma5, ma10, ma20)) / ma20 * 100.0
    if convergence >= 1.5:
        return False, ""

    breakout_pct = (now_px - ma20) / ma20 * 100.0
    if breakout_pct < 2.0:
        return False, ""

    return True, f"均线粘合{convergence:.2f}%+突破{breakout_pct:.1f}%"


# ==================== B2：板块轮动识别器 ====================
def detect_sector_rotation(
    sector_top_today: list,
    sector_top_5d: list,
) -> Tuple[str, float, str]:
    """
    输入：当日涨幅 Top3 板块列表 + 5 日累计涨幅 Top3 板块列表
    返回：(state, overlap_ratio, label)
      - state ∈ {"rotation", "continuation"}
    """
    if not sector_top_today or not sector_top_5d:
        return "unknown", 0.0, "板块数据不足"

    set_today = set(str(x) for x in sector_top_today[:3])
    set_5d = set(str(x) for x in sector_top_5d[:3])
    if not set_5d:
        return "unknown", 0.0, ""

    overlap = len(set_today & set_5d) / 3.0

    if overlap < 0.30:
        return "rotation", overlap, f"轮动日 重合{overlap:.0%}"
    return "continuation", overlap, f"延续日 重合{overlap:.0%}"
