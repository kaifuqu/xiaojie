# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88+ — 放量阈值统一读取工具（V26.88+ 9-30 第9项落地）

【背景】朋友2指出 vol_ratio / vol_ma5_mult / crowding_leader_vr 三套口径散落在 P2/P3/P4 各池；
本模块把所有放量相关的阈值都收口到 constants.py 的统一常量。

【使用】
    from core.strategies.volume_threshold_utils import (
        get_crowding_vr_thresholds,    # 拥挤度 3 档 (ordinary/strong/leader)
        get_volume_floor_wan,           # 流通市值分层的绝对成交额下限（万元）
        get_turnover_f_bounds,          # 换手率区间 (turnover_rate_f)
        is_volume_spike,                # 综合判定"是否构成放量信号"
    )

【与 constants.py 对齐】
    constants.CROWDING_VR_ORDINARY/STRONG/LEADER  → crowding_ordinary/strong/leader
    constants.VOLUME_ABSOLUTE_FLOOR_WAN["large_cap"/"mid_cap"/"small_cap"/"tiny_cap"] → get_volume_floor_wan()
    constants.TURNOVER_F_MIN_PCT/MAX_PCT  → get_turnover_f_bounds()

【集成】
  - core/strategies/p3_intraday_screener.py P3IntradayScreenerConfig.__post_init__（已落地）
  - core/strategies/p4_tail_screener.py P4TailScreenerConfig（待）
  - core/strategies/p2_auction_screener.py P2ScreenerConfig（待）
"""
from __future__ import annotations

import logging
from typing import Any, Tuple

logger = logging.getLogger(__name__)

# ====== 默认兜底值（与 constants.py 对齐；本模块内独立维护避免 import 循环） ======
_DEFAULT_CROWDING_ORDINARY = 2.5
_DEFAULT_CROWDING_STRONG = 3.5
_DEFAULT_CROWDING_LEADER = 4.2
_DEFAULT_TURNOVER_F_MIN_PCT = 1.5
_DEFAULT_TURNOVER_F_MAX_PCT = 15.0
_DEFAULT_VOLUME_FLOOR_WAN = {
    "large_cap": 20000.0,    # 300 亿+
    "mid_cap": 8000.0,       # 100-300 亿
    "small_cap": 3000.0,     # 50-100 亿
    "tiny_cap": 1500.0,      # <50 亿（120亿闸过滤后基本无）
}


def get_crowding_vr_thresholds() -> Tuple[float, float, float]:
    """
    获取拥挤度 3 档 VR 阈值：(ordinary, strong, leader)。
    优先读 constants.CROWDING_VR_*；缺失时回退默认值。
    """
    try:
        import constants as _C
        ord_v = float(getattr(_C, "CROWDING_VR_ORDINARY", _DEFAULT_CROWDING_ORDINARY))
        str_v = float(getattr(_C, "CROWDING_VR_STRONG", _DEFAULT_CROWDING_STRONG))
        lead_v = float(getattr(_C, "CROWDING_VR_LEADER", _DEFAULT_CROWDING_LEADER))
        return ord_v, str_v, lead_v
    except Exception as _e:
        logger.debug("[volume_threshold_utils] get_crowding_vr_thresholds 异常: %s", _e)
        return _DEFAULT_CROWDING_ORDINARY, _DEFAULT_CROWDING_STRONG, _DEFAULT_CROWDING_LEADER


def get_volume_floor_wan(circ_mv_wan: float) -> float:
    """
    根据流通市值（万元）返回适用的绝对成交额下限（万元）。

    阈值边界（统一以"万"为单位）：
      - ≥3_000_000 万（即 300 亿）→ large_cap 2 亿（即 20_000 万）
      - 1_000_000 ~ 3_000_000 万（100-300 亿）→ mid_cap 8000 万
      - 500_000 ~ 1_000_000 万（50-100 亿）→ small_cap 3000 万
      - <500_000 万（<50 亿）→ tiny_cap 1500 万
        （实际被 STRATEGY_MIN_CIRC_MV_WAN=1_200_000 万=120 亿过滤后罕见）

    注意：STRATEGY_MIN_CIRC_MV_WAN=120 亿（1_200_000 万）即最小票也是 mid_cap 起步。
    """
    try:
        import constants as _C
        floor_map = dict(getattr(_C, "VOLUME_ABSOLUTE_FLOOR_WAN", _DEFAULT_VOLUME_FLOOR_WAN))
    except Exception:
        floor_map = dict(_DEFAULT_VOLUME_FLOOR_WAN)

    if circ_mv_wan >= 3_000_000:        # ≥300 亿
        return float(floor_map.get("large_cap", 20000.0))
    if circ_mv_wan >= 1_000_000:        # 100-300 亿
        return float(floor_map.get("mid_cap", 8000.0))
    if circ_mv_wan >= 500_000:          # 50-100 亿
        return float(floor_map.get("small_cap", 3000.0))
    return float(floor_map.get("tiny_cap", 1500.0))


def get_turnover_f_bounds() -> Tuple[float, float]:
    """
    获取自由换手率区间 (turnover_rate_f_min, turnover_rate_f_max)，单位 %。
    - min 默认 1.5%（过滤绝对死水）
    - max 默认 15.0%（防妖股见顶）
    """
    try:
        import constants as _C
        f_min = float(getattr(_C, "TURNOVER_F_MIN_PCT", _DEFAULT_TURNOVER_F_MIN_PCT))
        f_max = float(getattr(_C, "TURNOVER_F_MAX_PCT", _DEFAULT_TURNOVER_F_MAX_PCT))
        return f_min, f_max
    except Exception as _e:
        logger.debug("[volume_threshold_utils] get_turnover_f_bounds 异常: %s", _e)
        return _DEFAULT_TURNOVER_F_MIN_PCT, _DEFAULT_TURNOVER_F_MAX_PCT


def is_volume_spike(
    vol_spike_ratio: float,
    amount_wan: float,
    circ_mv_wan: float,
    turnover_rate_f: float,
) -> Tuple[bool, str]:
    """
    综合判定"是否构成放量信号"。返回 (True/False, 原因)。
    规则（与朋友2建议对齐）：
      1. vol_spike_ratio = today_vol / max(vol_ma5, vol_ma20) ≥ 3×（基础放量门槛）
      2. amount_wan ≥ get_volume_floor_wan(circ_mv_wan)（绝对成交额下限，防小盘虚高）
      3. get_turnover_f_bounds()[0] ≤ turnover_rate_f ≤ get_turnover_f_bounds()[1]（换手率区间）
    """
    reasons = []
    # 1. 倍数
    if vol_spike_ratio < 3.0:
        reasons.append(f"放量倍数{vol_spike_ratio:.2f}×<3×")
    # 2. 绝对成交额
    floor = get_volume_floor_wan(circ_mv_wan)
    if amount_wan < floor:
        reasons.append(f"成交额{amount_wan:.0f}万<分层下限{floor:.0f}万")
    # 3. 换手率
    f_min, f_max = get_turnover_f_bounds()
    if turnover_rate_f < f_min:
        reasons.append(f"换手{turnover_rate_f:.2f}%<{f_min}%(死水)")
    if turnover_rate_f > f_max:
        reasons.append(f"换手{turnover_rate_f:.2f}%>{f_max}%(见顶)")

    if reasons:
        return False, "; ".join(reasons)
    return True, f"放量OK: {vol_spike_ratio:.2f}×+{amount_wan:.0f}万+换手{turnover_rate_f:.2f}%"


def get_p4_vol_ratio_min() -> float:
    """P4 尾盘 vol_ratio 下限（统一收口到 constants.P4_VOL_RATIO_MIN）。"""
    try:
        import constants as _C
        return float(getattr(_C, "P4_VOL_RATIO_MIN", 1.5))
    except Exception:
        return 1.5


def get_p3_vol_ma5_mult_s1() -> float:
    """P3 策略一 vol_ma5_mult 下限。"""
    try:
        import constants as _C
        return float(getattr(_C, "P3_VOL_MA5_MULT_S1", 1.5))
    except Exception:
        return 1.5


def get_p3_vol_ma5_mult_s8() -> float:
    """P3 策略八 vol_ma5_mult 下限（倍量启动）。"""
    try:
        import constants as _C
        return float(getattr(_C, "P3_VOL_MA5_MULT_S8", 1.85))
    except Exception:
        return 1.85