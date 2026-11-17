# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.80 — A 股特色维度集成到 P1 打分

【背景】新模块 a_stock_features / a_share_capital_flow / a_share_market_features 提供
A 股特色评分，但 P1 主打分函数 compute_p1_multi_dim_smooth_score 庞大且复杂，
直接改动风险大。本模块作为「外挂增强层」：

  - 在 compute_p1_multi_dim_smooth_score 之后调用
  - 根据开关 XIAOJIE_A_SHARE_FEATURES_ENABLED 决定是否启用
  - 异常时自动降级，不破坏主打分

【使用方式】
  1. 在 _process_single_stock_for_p1 中调用 apply_a_share_enhancement
  2. 或在 score_calibration.compute_p1_multi_dim_smooth_score 末尾接入

【增强分数范围】
  - 涨停板生态：-5 ~ +33（极端高连板）
  - 资金动向：-10 ~ +16
  - 市场维度：-5 ~ +12

  实战中应用 0.3 系数（不喧宾夺主），理论加成 ±10 分
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)


def is_a_share_features_enabled() -> bool:
    """检查开关。"""
    flag = os.environ.get("XIAOJIE_A_SHARE_FEATURES_ENABLED", "0").strip().lower()
    return flag in ("1", "true", "yes", "on")


def apply_a_share_enhancement(
    ts_code: str,
    df,
    rt: Dict[str, Any],
    sector_df=None,
    sector_history_df=None,
    hk_holding_df=None,
    margin_history=None,
    top_list_df=None,
    block_trade_df=None,
    unlock_events=None,
    industry: str = "",
    today: Optional[str] = None,
    enhancement_weight: float = 0.3,
) -> Dict[str, Any]:
    """
    对 P1 评分施加 A 股特色增强。

    Args:
        ts_code: 股票代码
        df: 日线 DataFrame
        rt: 实时行情
        sector_df: 全市场 DataFrame（板块涨停共振用）
        sector_history_df: 板块历史（板块轮动用）
        hk_holding_df: 北向持仓
        margin_history: 融资融券历史
        top_list_df: 龙虎榜
        block_trade_df: 大宗交易
        unlock_events: 解禁计划
        industry: 行业
        today: 当前日期
        enhancement_weight: 增强权重（默认 0.3，即理论加成 ±10 分）

    Returns:
        {
            "enabled": bool,
            "base_enhancement": 原始加成（未乘权重）,
            "weighted_enhancement": 加权后加成,
            "details": {
                "limit": 涨停板加成,
                "capital": 资金动向加成,
                "market": 市场维度加成
            },
            "tags": 所有标签列表
        }
    """
    if not is_a_share_features_enabled():
        return {
            "enabled": False,
            "base_enhancement": 0.0,
            "weighted_enhancement": 0.0,
            "details": {},
            "tags": [],
        }

    if today is None:
        today = datetime.now().strftime("%Y%m%d")

    try:
        # 1. 涨停板生态
        from core.a_stock_features import analyze_limit_features

        limit_result = analyze_limit_features(
            df=df, rt=rt, sector_df=sector_df, industry=industry
        )

        # 2. 资金动向
        from core.a_share_capital_flow import analyze_capital_flow

        capital_result = analyze_capital_flow(
            ts_code=ts_code,
            margin_history=margin_history,
            top_list_df=top_list_df,
            block_trade_df=block_trade_df,
            unlock_events=unlock_events,
            today=today,
        )

        # 3. 市场维度（财报窗 + 板块轮动 + 北向）
        from core.a_share_market_features import analyze_a_share_market_context

        market_result = analyze_a_share_market_context(
            ts_code=ts_code,
            rt=rt,
            sector_history_df=sector_history_df,
            hk_holding_df=hk_holding_df,
            today=today,
            industry=industry,
        )

        # 汇总原始加成
        base_enhancement = (
            limit_result["total_bonus"]
            + capital_result["total_score"]
            + market_result["total_score"]
        )

        # 加权（默认 0.3，避免特色分数喧宾夺主）
        weighted = base_enhancement * enhancement_weight

        # 合并所有标签
        tags = []
        tags.extend(limit_result["limit_tags"])
        tags.extend(capital_result["capital_tags"])
        tags.extend(market_result["market_tags"])

        return {
            "enabled": True,
            "base_enhancement": round(base_enhancement, 2),
            "weighted_enhancement": round(weighted, 2),
            "details": {
                "limit": limit_result,
                "capital": capital_result,
                "market": market_result,
            },
            "tags": tags[:8],
        }

    except Exception as e:
        logger.warning(f"A 股特色增强计算失败 [{ts_code}]: {e}")
        return {
            "enabled": True,
            "base_enhancement": 0.0,
            "weighted_enhancement": 0.0,
            "details": {"error": str(e)},
            "tags": [],
        }
