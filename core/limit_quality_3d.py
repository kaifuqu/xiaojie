# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V28 — 封板质量三维评分
================================================================================
【V28 新增·D 项】在 V27 A4"炸板回封分级"基础上扩展。

三维评分（替换/扩展 A4）：
  ① 封板时间：09:30-09:45 +6 / 09:45-10:30 +3 / 10:30-14:30 +1 / 14:30 后 +0
  ② 开板次数：开 0 次 +2 / 开 1 次 +0 / 开 2 次 -2 / 开 ≥3 次 -8
  ③ 封单金额：≥ 1 亿 +3 / 5000w-1亿 +1 / < 5000w -2
  ④ 板块联袂度：板块涨停 ≥ 5 只 +4 / 3-4 只 +2 / 1-2 只 +0 / 0 只 -4

数据来源：HiThink limit_up_pool 字段
  - rt['limit_seal_time']     - 封板时间（HHMMSS）
  - rt['limit_open_count']    - 开板次数
  - rt['limit_seal_amount']   - 封单金额（元）
  - rt['sector_limit_count']  - 板块涨停数（HiThink sector_realtime）
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)


def _parse_hhmmss_to_minutes(s) -> float:
    """HHMMSS / HH:MM:SS 转分钟"""
    try:
        ss = str(s).strip().replace(":", "")
        if len(ss) >= 6:
            hh = int(ss[0:2])
            mm = int(ss[2:4])
            return hh * 60.0 + mm
        if len(ss) >= 4:
            hh = int(ss[0:2])
            mm = int(ss[2:4])
            return hh * 60.0 + mm
    except Exception:
        pass
    return -1.0


def compute_seal_time_bonus(seal_time) -> Tuple[float, str]:
    """① 封板时间加分"""
    m = _parse_hhmmss_to_minutes(seal_time)
    if m < 0:
        return 0.0, ""
    if 570 <= m <= 585:    # 09:30-09:45 (570=09:30, 585=09:45)
        return 6.0, "封板黄金9:30-09:45+6"
    if m < 600:            # 09:45-10:00 早板
        return 3.0, "封板早+3"
    if m < 870:            # 10:00-14:30 中板
        return 1.0, "封板中+1"
    return 0.0, "封板晚+0"


def compute_open_count_bonus(open_count) -> Tuple[float, str]:
    """② 开板次数加分"""
    try:
        n = int(open_count)
    except (TypeError, ValueError):
        return 0.0, ""
    if n <= 0:
        return 2.0, "封单稳定+2"
    if n == 1:
        return 0.0, "开1次+0"
    if n == 2:
        return -2.0, "开2次-2"
    return -8.0, f"开{n}次-8"


def compute_seal_amount_bonus(seal_amount) -> Tuple[float, str]:
    """③ 封单金额加分（金额单位：元）"""
    try:
        amt = float(seal_amount)
    except (TypeError, ValueError):
        return 0.0, ""
    if amt >= 1e8:                 # 1 亿
        return 3.0, "封单≥1亿+3"
    if amt >= 5e7:                 # 5 千万
        return 1.0, "封单5kw-1亿+1"
    return -2.0, "封单<5kw-2"


def compute_sector_link_bonus(sector_limit_count) -> Tuple[float, str]:
    """④ 板块联袂度加分"""
    try:
        n = int(sector_limit_count)
    except (TypeError, ValueError):
        return 0.0, ""
    if n >= 5:
        return 4.0, f"板块联袂{n}只+4"
    if n >= 3:
        return 2.0, f"板块联袂{n}只+2"
    if n >= 1:
        return 0.0, f"板块联袂{n}只+0"
    return -4.0, "孤板-4"


def compute_limit_quality_3d(rt: Dict[str, Any]) -> Tuple[float, str]:
    """
    综合三维封板质量分（满分 15 分，可负）。

    返回：(bonus, label)
    """
    if not isinstance(rt, dict):
        return 0.0, ""

    total = 0.0
    labels: list = []

    field_map = (
        (compute_seal_time_bonus, "limit_seal_time"),
        (compute_open_count_bonus, "limit_open_count"),
        (compute_seal_amount_bonus, "limit_seal_amount"),
        (compute_sector_link_bonus, "sector_limit_count"),
    )
    for fn, key in field_map:
        try:
            b, l = fn(rt.get(key))
            total += b
            if l:
                labels.append(l)
        except Exception:
            continue

    return float(total), "; ".join(labels)
