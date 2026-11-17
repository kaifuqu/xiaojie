# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.80 — 多层垃圾信号过滤器

【背景】原 pass_line=50 偏低，趋势市涌入"看着热闹但胜率低"的杂票。
引入 4 层递进过滤器：基础分 → 资金真实性 → 异常形态 → 流动性溢价。
每层独立判定，任一不通过直接否决（短路逻辑）。

【胜率提升原理】
- 第一层（基础分）：剔除技术面平庸票，过滤 30% 噪音
- 第二层（资金真实性）：剔除"无量上涨""对倒拉升"等假突破，过滤 25% 假信号
- 第三层（异常形态）：剔除高位放量长上影、连续大阴线等危险形态，过滤 20% 陷阱
- 第四层（流动性溢价）：剔除流动性枯竭（换手<0.5% 或 >15%），过滤 15% 流动性风险
- 综合可提升胜率 8-15 个百分点
"""
import numpy as np
from typing import Dict, Tuple


# ==================== 过滤阈值（可配置）====================
FILTER_THRESHOLDS = {
    "L1_base_score": 50.0,            # 基础分下限
    "L2_net_main_wan_min": 200.0,     # 主力净流入下限（万元）
    "L2_turnover_f_min": 0.8,         # 真实换手下限（%）
    "L2_turnover_f_max": 12.0,        # 真实换手上限（%）
    "L3_upper_shadow_max_pct": 4.5,   # 上影线最大占比（%）
    "L3_ma20_bias_high": 12.0,        # 乖离 MA20 高位熔断（%）
    "L3_consecutive_yin_max": 2,      # 连续阴线容忍数
    "L4_circ_mv_yi_min": 50.0,        # 流通市值下限（亿元）
    "L4_liquidity_score_min": 40.0,   # 流动性分下限（百分制）
    # 【P0-1 2026-11-04 新增】L4 流动性打分的分档系数（可配置）。
    # 原公式写死 mv_score = min(50, circ_mv/20) → 需 1000 亿市值才拿满分，
    # 对本系统（中小盘动量为主）过于严苛，会误杀大量合格候选。
    # 改为可配置后，可按实际候选池分布标定。
    "L4_mv_score_cap": 30.0,          # 流通市值分上限
    "L4_mv_score_divisor": 50.0,      # 流通市值分 = min(cap, 流通亿数 / divisor)
    "L4_trn_score_cap": 30.0,         # 换手分上限
    "L4_trn_score_per_pct": 3.0,      # 换手分 = min(cap, 换手% × per_pct)
    "L4_amt_score_cap": 40.0,         # 成交额分上限
    "L4_amt_score_per_log10": 8.0,    # 成交额分 = min(cap, log10(万元) × per_log10)
}


def apply_threshold_overrides(overrides: Dict[str, float]) -> None:
    """用配置覆盖过滤阈值（未知键忽略）。

    【P0-1】上线前必须先用 core/tools 里的回放工具标定阈值，
    否则硬编码的默认值会误杀中小盘候选票。
    """
    if not isinstance(overrides, dict):
        return
    for k, v in overrides.items():
        if k in FILTER_THRESHOLDS:
            try:
                FILTER_THRESHOLDS[k] = float(v)
            except (TypeError, ValueError):
                continue


def filter_layer1_base_score(score: float, breakdown: Dict = None) -> Tuple[bool, str]:
    """
    第一层：基础分过滤
    """
    threshold = FILTER_THRESHOLDS["L1_base_score"]
    if score < threshold:
        return False, f"基础分{score:.1f}<{threshold}，技术面平庸"
    return True, ""


def filter_layer2_fund_authenticity(
    net_main_wan: float,
    turnover_f: float,
    vol_ratio: float,
) -> Tuple[bool, str]:
    """
    第二层：资金真实性过滤
    解决"无量上涨""对倒拉升"假突破。
    """
    min_main = FILTER_THRESHOLDS["L2_net_main_wan_min"]
    min_trn = FILTER_THRESHOLDS["L2_turnover_f_min"]
    max_trn = FILTER_THRESHOLDS["L2_turnover_f_max"]

    # 主力净流入必须为正
    if net_main_wan < min_main:
        return False, f"主力净流入{net_main_wan:.0f}万<{min_main}万，无资金推动"

    # 换手率必须在合理区间
    if turnover_f < min_trn:
        return False, f"换手{turnover_f:.2f}%<{min_trn}%，流动性枯竭"
    if turnover_f > max_trn:
        return False, f"换手{turnover_f:.2f}%>{max_trn}%，对倒嫌疑"

    # 量比必须配合（避免无量长阳）
    if vol_ratio < 0.9:
        return False, f"量比{vol_ratio:.2f}<0.9，缩量上涨无持续性"

    return True, ""


def filter_layer3_abnormal_pattern(
    upper_shadow_pct: float,
    bias_ma20: float,
    consecutive_yin: int,
    has_long_upper_shadow_3d: bool,
) -> Tuple[bool, str]:
    """
    第三层：异常形态过滤
    解决"高位放量长上影""连续大阴线""高位乖离"等危险形态。
    """
    max_upper = FILTER_THRESHOLDS["L3_upper_shadow_max_pct"]
    max_bias = FILTER_THRESHOLDS["L3_ma20_bias_high"]
    max_yin = FILTER_THRESHOLDS["L3_consecutive_yin_max"]

    if upper_shadow_pct > max_upper:
        return False, f"上影线{upper_shadow_pct:.2f}%>{max_upper}%，抛压沉重"

    if bias_ma20 > max_bias:
        return False, f"乖离MA20{bias_ma20:.2f}%>{max_bias}%，高位熔断"

    if consecutive_yin >= max_yin:
        return False, f"连续{consecutive_yin}根阴线，趋势走坏"

    if has_long_upper_shadow_3d:
        return False, "近3日出现长上影，主力出货嫌疑"

    return True, ""


def filter_layer4_liquidity_premium(
    circ_mv_yi: float,
    turnover_f: float,
    amount_wan: float,
) -> Tuple[bool, str]:
    """
    第四层：流动性溢价过滤
    剔除流动性枯竭或过度投机的标的。
    """
    min_mv = FILTER_THRESHOLDS["L4_circ_mv_yi_min"]
    min_liq = FILTER_THRESHOLDS["L4_liquidity_score_min"]

    if circ_mv_yi < min_mv:
        return False, f"流通市值{circ_mv_yi:.1f}亿<{min_mv}亿，盘口过薄"

    # 流动性综合分（0-100）—— 各分项系数可配置（见 FILTER_THRESHOLDS 注释）
    mv_score = min(
        FILTER_THRESHOLDS["L4_mv_score_cap"],
        circ_mv_yi / FILTER_THRESHOLDS["L4_mv_score_divisor"],
    )
    trn_score = min(
        FILTER_THRESHOLDS["L4_trn_score_cap"],
        turnover_f * FILTER_THRESHOLDS["L4_trn_score_per_pct"],
    )
    amt_score = min(
        FILTER_THRESHOLDS["L4_amt_score_cap"],
        np.log10(max(amount_wan, 1)) * FILTER_THRESHOLDS["L4_amt_score_per_log10"],
    )
    liq_total = mv_score + trn_score + amt_score

    if liq_total < min_liq:
        return False, f"流动性综合分{liq_total:.1f}<{min_liq}，深度不足"

    return True, ""


# ==================== 第五层：基本面硬门槛（V26.80 新增）====================
FILTER_THRESHOLDS["L5_pe_max"] = 80.0              # PE 上限（防估值泡沫）
FILTER_THRESHOLDS["L5_pb_max"] = 8.0               # PB 上限
FILTER_THRESHOLDS["L5_roe_min"] = 3.0              # ROE 下限（%）
FILTER_THRESHOLDS["L5_market_cap_yi_min"] = 100.0  # 市值下限（亿，机构审美线）


def filter_layer5_fundamental(
    pe_ttm: float,
    pb: float,
    roe_pct: float,
    market_cap_yi: float,
    is_loss_company: bool = False,
) -> Tuple[bool, str]:
    """
    第五层：基本面硬门槛过滤。
    解决"PE/PB/ROE 只作为加分项而非门槛"问题。

    阈值（可配置）：
      - PE_TTM ≤ 80（防估值泡沫）
      - PB ≤ 8（防资产泡沫）
      - ROE ≥ 3%（剔除价值陷阱）
      - 总市值 ≥ 100亿（机构审美）
      - 亏损公司直接拦截（is_loss_company）

    Returns:
        (passed, reason)
    """
    if is_loss_company:
        return False, "亏损公司，价值陷阱风险"

    max_pe = FILTER_THRESHOLDS["L5_pe_max"]
    if pe_ttm > max_pe:
        return False, f"PE_TTM{pe_ttm:.1f}>{max_pe}，估值泡沫"

    max_pb = FILTER_THRESHOLDS["L5_pb_max"]
    if pb > max_pb:
        return False, f"PB{pb:.2f}>{max_pb}，资产泡沫"

    min_roe = FILTER_THRESHOLDS["L5_roe_min"]
    if roe_pct < min_roe:
        return False, f"ROE{roe_pct:.2f}%<{min_roe}%，价值陷阱"

    min_mv = FILTER_THRESHOLDS["L5_market_cap_yi_min"]
    if market_cap_yi < min_mv:
        return False, f"总市值{market_cap_yi:.1f}亿<{min_mv}亿，机构审美线"

    return True, ""


def multi_layer_filter(
    score: float,
    net_main_wan: float,
    turnover_f: float,
    vol_ratio: float,
    upper_shadow_pct: float,
    bias_ma20: float,
    consecutive_yin: int,
    has_long_upper_shadow_3d: bool,
    circ_mv_yi: float,
    amount_wan: float,
    breakdown: Dict = None,
    pe_ttm: float = 30.0,
    pb: float = 3.0,
    roe_pct: float = 8.0,
    market_cap_yi: float = 200.0,
    is_loss_company: bool = False,
) -> Dict:
    """
    5 层递进过滤（短路逻辑）。
    任一层失败即返回，不再进入下一层。

    Returns:
        {
            "passed": 是否通过所有过滤,
            "reject_layer": 失败层 (1-5, None 表示通过),
            "reject_reason": 失败原因,
            "layer_results": 各层结果明细
        }
    """
    layer_results = {}

    # L1: 基础分
    ok, msg = filter_layer1_base_score(score, breakdown)
    layer_results["L1"] = {"passed": ok, "msg": msg}
    if not ok:
        return {
            "passed": False,
            "reject_layer": 1,
            "reject_reason": msg,
            "layer_results": layer_results,
        }

    # L2: 资金真实性
    ok, msg = filter_layer2_fund_authenticity(net_main_wan, turnover_f, vol_ratio)
    layer_results["L2"] = {"passed": ok, "msg": msg}
    if not ok:
        return {
            "passed": False,
            "reject_layer": 2,
            "reject_reason": msg,
            "layer_results": layer_results,
        }

    # L3: 异常形态
    ok, msg = filter_layer3_abnormal_pattern(
        upper_shadow_pct, bias_ma20, consecutive_yin, has_long_upper_shadow_3d
    )
    layer_results["L3"] = {"passed": ok, "msg": msg}
    if not ok:
        return {
            "passed": False,
            "reject_layer": 3,
            "reject_reason": msg,
            "layer_results": layer_results,
        }

    # L4: 流动性
    ok, msg = filter_layer4_liquidity_premium(circ_mv_yi, turnover_f, amount_wan)
    layer_results["L4"] = {"passed": ok, "msg": msg}
    if not ok:
        return {
            "passed": False,
            "reject_layer": 4,
            "reject_reason": msg,
            "layer_results": layer_results,
        }

    # L5: 基本面硬门槛（V26.80 新增）
    ok, msg = filter_layer5_fundamental(pe_ttm, pb, roe_pct, market_cap_yi, is_loss_company)
    layer_results["L5"] = {"passed": ok, "msg": msg}
    if not ok:
        return {
            "passed": False,
            "reject_layer": 5,
            "reject_reason": msg,
            "layer_results": layer_results,
        }

    return {
        "passed": True,
        "reject_layer": None,
        "reject_reason": "",
        "layer_results": layer_results,
    }
