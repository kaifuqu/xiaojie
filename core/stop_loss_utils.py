# -*- coding: utf-8 -*-
"""
V26.88+9-22 优化28：A 股实战自动止损/止盈工具

4 个池的"买入提示"都是静态文本，本模块提供基于实时数据的动态止损/止盈计算。
实战中，"止损位"的精确性直接决定胜率：
- 3% 随机止损 ≠ 关键支撑位止损（A 股主力出货点常常就是 -3.99%）
- 5 日线为止损位 vs 前一交易日最低价*0.985 胜率差异显著

返回 dict 含:
  - stop_loss_pct: 止损百分比（负数，例如 -2.5 表示止损位为买入价下方 2.5%）
  - stop_loss_price: 绝对止损价位
  - take_profit_pct: 目标盈利百分比
  - risk_reward_ratio: 盈亏比
  - stop_type: 止损类型（关键支撑 / 5日线 / 前低 / 固定百分比）
  - trail_stop_rules: 移动止损规则列表（每条规则为 dict）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

__all__ = [
    "compute_a_share_stop_loss",
    "compute_a_share_take_profit",
    "evaluate_entry_quality",
    "compute_expected_value",
    "build_trailing_stop_rules",
    "resolve_exec_stop_pct",
    "resolve_exec_take_profit_pct",
]


# ============================ 执行口径（P0-2 统一） ============================
# 【问题】本模块原本按「关键支撑位」动态算止损（-1.5% ~ -3%），但模拟盘 engine.py
#   买入时只按 config 的固定比例挂止损（stop_px = price × cfg.stop_loss_ratio，全板 -3%），
#   **从不采用这里算出的动态止损**。于是推送说「止损 -2.5%」，系统实际到 -3% 才卖，
#   用户按文案操作必然与系统结果对不上。
#
# 【修复原则】显示必须等于执行。本模块改为：
#   · stop_loss_pct / stop_loss_price = **系统真正会执行的止损**（取配置）
#   · support_stop_pct / support_stop_price = 支撑位参考值（仅提示，不参与执行）
#   这样「推送说的」和「系统做的」永远一致，支撑位信息也不丢失。
#
# 【对 EV 的影响】risk_reward = 目标涨幅 / |止损|，止损由 -2.5% 变为 -3% 后赔率略降
#   → EV 略降 → 选股略偏保守。这是「让 EV 反映真实风险」的修正，不是退化。
_FALLBACK_EXEC_STOP_PCT = -3.0
_FALLBACK_EXEC_TAKE_PCT = 8.0

_EXEC_CFG_CACHE: Optional[tuple] = None


def _load_exec_cfg() -> tuple:
    """读模拟盘配置的（止损%, 止盈%）一次并缓存；失败回落 -3% / +8%。"""
    global _EXEC_CFG_CACHE
    if _EXEC_CFG_CACHE is not None:
        return _EXEC_CFG_CACHE
    sl, tp = _FALLBACK_EXEC_STOP_PCT, _FALLBACK_EXEC_TAKE_PCT
    try:
        from core.paper_trading.config import load_config

        c = load_config()
        _sl = float(c.stop_loss_pct)
        _tp = float(c.take_profit_pct)
        if _sl < 0:
            sl = _sl
        if _tp > 0:
            tp = _tp
    except Exception:  # noqa: BLE001
        pass
    _EXEC_CFG_CACHE = (sl, tp)
    return _EXEC_CFG_CACHE


def resolve_exec_stop_pct() -> float:
    """系统实际执行止损百分比（负），与模拟盘 engine 一致。"""
    return float(_load_exec_cfg()[0])


def resolve_exec_take_profit_pct() -> float:
    """系统实际执行止盈百分比（正），与模拟盘 engine 一致。"""
    return float(_load_exec_cfg()[1])


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        x = float(pd.to_numeric(v, errors="coerce"))
        return x if np.isfinite(x) else default
    except Exception:
        return default


def _get_low(df: pd.DataFrame, idx: int = -1) -> float:
    if df is None or len(df) < 1:
        return 0.0
    return _safe_float(df.iloc[idx].get("low"), 0.0) if idx >= -len(df) else 0.0


def _get_close(df: pd.DataFrame, idx: int = -1) -> float:
    if df is None or len(df) < 1:
        return 0.0
    return _safe_float(df.iloc[idx].get("close"), 0.0) if idx >= -len(df) else 0.0


def _get_ma(df: pd.DataFrame, name: str) -> float:
    if df is None or len(df) < 1:
        return 0.0
    return _safe_float(df.iloc[-1].get(name), 0.0)


def compute_a_share_stop_loss(
    df: Optional[pd.DataFrame],
    rt: Optional[Dict[str, Any]],
    *,
    strategy_type: str = "default",
) -> Dict[str, Any]:
    """
    V26.88+9-22 优化28：基于实时 K 线 + 实时快照的自动止损计算。

    参数:
        df: 历史日线窗口（截至 T-1 收盘）
        rt: 当日实时快照字典（含 price/close）
        strategy_type: 策略类型
            - "leader"/"连板"/"龙头": 龙头股（容忍更大波动）
            - "right_side"/"回踩"/"低吸": 回踩股（严止损）
            - "main_wave"/"主升": 主升浪（中等）

    返回:
        {
            "stop_loss_pct": -3.0,   # 系统真正执行的止损百分比（取配置，全池统一）
            "stop_loss_price": 9.70, # 执行止损价位
            "stop_type": "5日均线（执行口径-3.0%）",
            "support_stop_pct": -2.5,      # 支撑位参考（仅提示，不执行）
            "support_stop_price": 9.75,    # 支撑位参考价
            "exec_stop_pct": -3.0,
        }

    【P0-2 说明】stop_loss_pct/stop_loss_price 已改为「系统真正会执行的止损」，
    支撑位只作参考。原因见文件头注释：模拟盘从不执行动态止损，显示必须等于执行。
    """
    exec_pct = resolve_exec_stop_pct()

    def _exec_result(support_pct: float, support_px: float, source: str, now_px: float) -> Dict[str, Any]:
        """统一封装：执行口径为主，支撑位为参考。"""
        return {
            "stop_loss_pct": round(exec_pct, 2),
            "stop_loss_price": round(now_px * (1 + exec_pct / 100.0), 3) if now_px > 0 else 0.0,
            "stop_type": f"{source}（执行口径{exec_pct:.1f}%）",
            "support_stop_pct": round(support_pct, 2) if support_pct else None,
            "support_stop_price": round(support_px, 3) if support_px else None,
            "exec_stop_pct": round(exec_pct, 2),
        }

    now_px = _safe_float((rt or {}).get("price"), _safe_float((rt or {}).get("close"), 0.0))
    if now_px <= 0 or df is None or len(df) < 3:
        # 缺数据时降级：止损一律用执行口径（不再按 strategy_type 给 -2.0/-3.0 两套）
        return _exec_result(exec_pct, 0.0, "数据不全·默认百分比", now_px)

    # 1) 关键支撑位：MA5、MA10、前 5 日最低、prev_close
    ma5 = _get_ma(df, "ma5")
    ma10 = _get_ma(df, "ma10")
    prev_low = _get_low(df, -1)  # T-1 当日最低
    pre_close = _get_close(df, -1)

    # 2) 找出最接近现价且低于现价的关键支撑位
    candidates: List[tuple] = []  # (支撑价, 距现价百分比, 来源)
    if 0 < ma5 < now_px:
        candidates.append((ma5, (ma5 - now_px) / now_px * 100.0, "5日均线"))
    if 0 < ma10 < now_px:
        candidates.append((ma10, (ma10 - now_px) / now_px * 100.0, "10日均线"))
    if 0 < prev_low < now_px:
        candidates.append((prev_low * 0.985, (prev_low * 0.985 - now_px) / now_px * 100.0, "前低*0.985"))
    if 0 < pre_close < now_px:
        candidates.append((pre_close * 0.97, (pre_close * 0.97 - now_px) / now_px * 100.0, "昨收*0.97"))

    # 3) 龙头股允许更紧的回踩位置（取最大值）；回踩股要更宽松
    if not candidates:
        # 现价已低于所有均线 → 已破位，不应买入，但兜底返回执行口径止损
        return _exec_result(exec_pct, now_px * (1 + exec_pct / 100.0), "破位兜底", now_px)

    # 按"距现价跌幅"降序（越接近现价越严），但保留强支撑
    if strategy_type in ("leader", "连板", "龙头"):
        # 龙头股：取"最紧"的关键支撑（容忍小回撤）
        candidates.sort(key=lambda x: x[1], reverse=True)
        chosen_px, chosen_pct, source = candidates[0]
        # 支撑位仅作参考，上限对齐执行口径（不再硬编码 -3.0）
        chosen_pct = max(chosen_pct, exec_pct)
    elif strategy_type in ("right_side", "回踩", "低吸", "缩量"):
        # 回踩股：给足跑路空间。支撑参考值可略紧于执行口径，但仅为提示
        candidates.sort(key=lambda x: x[1])  # 升序
        chosen_px, chosen_pct, source = candidates[0]
        chosen_pct = min(chosen_pct, exec_pct / 2.0)  # 参考值紧于执行口径（advisory）
    else:
        # 主升浪/默认：取第二紧支撑做止损，更均衡
        candidates.sort(key=lambda x: x[1], reverse=True)
        chosen_px, chosen_pct, source = candidates[1] if len(candidates) >= 2 else candidates[0]
        chosen_pct = max(chosen_pct, exec_pct)  # 主升最大止损取执行口径

    # 【P0-2】最终返回：执行口径为主，支撑位作为参考（不参与执行）
    return _exec_result(chosen_pct, chosen_px, source, now_px)


def compute_a_share_take_profit(
    df: Optional[pd.DataFrame],
    rt: Optional[Dict[str, Any]],
    stop_pct: Optional[float] = None,
) -> Dict[str, Any]:
    """
    V26.88+9-22 优化28：A 股实战目标止盈位。

    stop_pct 留空时自动取**系统执行口径**（配置的止损），保证赔率口径一致。

    实战经验：
    - 涨停板封板龙头：目标 +5~+8%（次日大概率冲高回落）
    - 主升浪：目标 +3~+6%（A 股弱势行情 5% 即可能遇到套牢盘）
    - 回踩低吸：目标 +2~+4%（弱势行情 3% 止盈更优）
    - 默认 60 日新高股：目标 +5%
    """
    now_px = _safe_float((rt or {}).get("price"), _safe_float((rt or {}).get("close"), 0.0))
    if stop_pct is None:
        stop_pct = resolve_exec_stop_pct()  # 【P0-2】默认对齐执行口径

    # 阻力位 = 60 日最高 / 前 20 日最高
    resistance_candidates = []
    if df is not None and len(df) >= 20:
        high60 = _safe_float(df["high"].tail(60).max() if len(df) >= 60 else df["high"].max(), 0.0)
        if 0 < high60 > now_px:
            resistance_candidates.append((high60, (high60 - now_px) / now_px * 100.0, "60日高"))

        high20 = _safe_float(df["high"].tail(20).max(), 0.0)
        if 0 < high20 > now_px and high20 != high60:
            resistance_candidates.append((high20, (high20 - now_px) / now_px * 100.0, "20日高"))

    # 选距现价最近的阻力位作为目标
    if resistance_candidates:
        resistance_candidates.sort(key=lambda x: x[1])
        target_px, target_pct, source = resistance_candidates[0]
        target_pct = min(target_pct, 12.0)  # A 股短线最大目标 12%
    else:
        target_pct = 5.0  # 默认
        target_px = now_px * 1.05
        source = "默认5%"

    # 计算赔率
    risk_reward = round(target_pct / abs(stop_pct), 2) if stop_pct < 0 else 2.0

    return {
        "take_profit_pct": round(target_pct, 2),
        "take_profit_price": round(target_px, 3),
        "stop_type": source,
        "risk_reward_ratio": risk_reward,
    }


def build_trailing_stop_rules(
    strategy_type: str = "default",
    stop_pct: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    V26.88+9-22 优化28：移动止损规则列表。

    stop_pct 留空时取系统执行口径（【P0-2】不再硬编码 -2.5）。

    A 股实战经验（永远不要让浮盈回吐）：
    - 浮盈≥3%：止损上调至买入价（保本）
    - 浮盈≥5%：止损上调至今日最低价*0.97
    - 浮盈≥8%：止损上调至买入价上方2%（高弹性）
    """
    if strategy_type in ("leader", "连板", "龙头"):
        return [
            {"profit_threshold_pct": 4.0, "move_stop_to": "cost", "desc": "浮盈≥4%→上调到成本价"},
            {"profit_threshold_pct": 6.0, "move_stop_to": "low0.97", "desc": "浮盈≥6%→上调到今日最低*0.97"},
            {"profit_threshold_pct": 10.0, "move_stop_to": "cost2pct", "desc": "浮盈≥10%→上调到买入价+2%"},
        ]
    elif strategy_type in ("main_wave", "主升"):
        return [
            {"profit_threshold_pct": 3.0, "move_stop_to": "cost", "desc": "浮盈≥3%→上调到成本价"},
            {"profit_threshold_pct": 5.0, "move_stop_to": "low0.97", "desc": "浮盈≥5%→今日最低*0.97"},
        ]
    else:
        # 回踩/默认：保守移动
        return [
            {"profit_threshold_pct": 2.0, "move_stop_to": "cost", "desc": "浮盈≥2%→上调到成本价"},
            {"profit_threshold_pct": 4.0, "move_stop_to": "low0.98", "desc": "浮盈≥4%→今日最低*0.98"},
        ]


def evaluate_entry_quality(
    df: Optional[pd.DataFrame],
    rt: Optional[Dict[str, Any]],
    *,
    risk_reward_min: float = 2.0,
    strategy_type: str = "default",
) -> Dict[str, Any]:
    """
    V26.88+9-22 优化21+27+28 综合评估：A 股入场质量评估。

    返回入场质量分 0~100，包含：
    - risk_reward_ratio: 盈亏比
    - pass_2_1: 是否满足 2:1 赔率（实战最低标准）
    - support_distance: 支撑距现价百分比（越小越安全）
    - resistance_distance: 阻力距现价百分比（越大越安全）
    - entry_quality_score: 综合质量分 0~100

    Use case: P2-P5 引擎调用此函数，结果写入 hit_res["entry_quality"]。
    """
    stop_info = compute_a_share_stop_loss(df, rt, strategy_type=strategy_type)
    profit_info = compute_a_share_take_profit(df, rt, stop_pct=stop_info["stop_loss_pct"])
    trailing_rules = build_trailing_stop_rules(strategy_type, stop_info["stop_loss_pct"])

    risk_reward = profit_info["risk_reward_ratio"]
    pass_2_1 = risk_reward >= risk_reward_min

    # 综合质量分
    quality = 50.0
    if pass_2_1:
        quality += min(30.0, (risk_reward - risk_reward_min) * 15.0)  # 赔率越高分越高
    else:
        quality -= min(20.0, (risk_reward_min - risk_reward) * 20.0)
    # 支撑距现价跌幅（越小越安全）
    sl_abs = abs(stop_info["stop_loss_pct"])
    if sl_abs < 1.5:
        quality += 10.0  # 严止损+紧支撑→优
    elif sl_abs > 3.5:
        quality -= 10.0  # 止损太宽→劣
    # 移动止损规则数（越多越主动保护）
    quality += min(10.0, len(trailing_rules) * 3.0)
    quality = max(0.0, min(100.0, quality))

    return {
        "stop_loss": stop_info,
        "take_profit": profit_info,
        "trailing_stop_rules": trailing_rules,
        "risk_reward_ratio": risk_reward,
        "pass_2_1": pass_2_1,
        "entry_quality_score": round(quality, 2),
    }


def compute_expected_value(
    *,
    risk_reward: float,
    signal_strength: float,  # 0~100
    base_success_rate: float = 0.55,
    penalty: float = 0.0,
) -> Dict[str, Any]:
    """
    V26.88+9-22 优化27：Expected Value 评分。

    公式: EV = p_success × avg_gain - (1-p_success) × avg_loss
        = p_success × 1.0×risk_reward + (1-p_success) × 1.0

    Args:
        risk_reward: 盈亏比（>=2.0 合格）
        signal_strength: 信号强度 0~100
        base_success_rate: 基础胜率（0.55）
        penalty: 减分项（0~30）

    Returns:
        {
            "p_success": 0.62,        # 调整后胜率
            "expected_value": 0.95,   # EV（单位：期望收益倍数）
            "expected_value_norm": 76.0,  # 归一化 0~100（60 为分水岭）
            "trade_grade": "B+",       # 推荐等级 A/B/C/D
        }

    核心思想: 信号越强(信号强度越高)→胜率越高；赔率越好(盈亏比越大)→EV 越高。
    """
    p_success = base_success_rate + (signal_strength - 50.0) / 200.0 - penalty / 300.0
    p_success = max(0.30, min(0.85, p_success))

    # EV 公式（设 take_profit = +risk_reward 单位, stop_loss = -1 单位）
    # EV = p * RR - (1-p) * 1
    ev = p_success * risk_reward - (1.0 - p_success) * 1.0
    # 归一化到 0~100：EV=1.0 → 80 分（强信号）
    ev_norm = max(0.0, min(100.0, 50.0 + ev * 30.0))

    if ev >= 1.5:
        grade = "A"
    elif ev >= 0.8:
        grade = "B+"
    elif ev >= 0.4:
        grade = "B"
    elif ev >= 0.0:
        grade = "C"
    else:
        grade = "D"

    return {
        "p_success": round(p_success, 4),
        "expected_value": round(ev, 3),
        "expected_value_norm": round(ev_norm, 2),
        "trade_grade": grade,
    }
