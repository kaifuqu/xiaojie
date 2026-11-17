# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V27 — 炸板回封强度分级
================================================================================
【V27 新增·A4】对单只股票按「炸板次数 + 封单金额占成交比 + 板块联袂涨停数」做
三档分级：
  - 炸板回封龙头（炸板 ≥2 + 封单≥成交80% + 板块联袂≥3只涨停）：+8
  - 假回封诱多（炸板 ≥3 + 封单<成交30% + 板块不同步）：-8
  - 兜底（其他炸板）：0

数据来源：
  - rt['limit_burst_count']（炸板次数，HiThink）
  - rt['limit_seal_ratio']（封单占成交比，0-1）
  - rt['sector_limit_count']（所属板块联袂涨停数）
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)


def classify_limit_break_quality(rt: Dict[str, Any]) -> Tuple[str, float, str]:
    """
    返回：(level, bonus, label)
      level ∈ {"dragon", "fake", "neutral"}
    """
    if not isinstance(rt, dict):
        return "neutral", 0.0, ""

    try:
        burst_count = int(rt.get("limit_burst_count", 0) or 0)
    except (TypeError, ValueError):
        burst_count = 0
    try:
        seal_ratio = float(rt.get("limit_seal_ratio", 0.0) or 0.0)
    except (TypeError, ValueError):
        seal_ratio = 0.0
    try:
        sector_limit = int(rt.get("sector_limit_count", 0) or 0)
    except (TypeError, ValueError):
        sector_limit = 0

    if burst_count <= 0:
        return "neutral", 0.0, ""

    if burst_count >= 2 and seal_ratio >= 0.80 and sector_limit >= 3:
        return "dragon", 8.0, f"炸板回封龙头×{burst_count}+封单{seal_ratio:.0%}+板块{sector_limit}只"

    if burst_count >= 3 and seal_ratio < 0.30 and sector_limit < 2:
        return "fake", -8.0, f"假回封诱多×{burst_count}+封单{seal_ratio:.0%}"

    return "neutral", 0.0, f"炸板×{burst_count}+封单{seal_ratio:.0%}"
