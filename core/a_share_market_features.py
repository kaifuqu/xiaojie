# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.80 — A 股特色：财报时间窗与板块轮动深度分析

【A 股特色背景】
1. 财报披露季（4月年报、8月半年报、10月一季报+三季报）= 「地雷期」
   - 财报前 5 个交易日：主力通常谨慎，不愿拉升
   - 财报后第 1 天：兑现行情（业绩好→高开低走；业绩差→直接砸盘）
   - 「业绩雷」集中爆发：4 月最后一周、8 月最后一周

2. 板块轮动 = A 股个股表现 70% 由板块决定
   - 主线板块（持续 5+ 日）：机构抱团
   - 题材板块（持续 1~3 日）：游资接力
   - 退潮板块（持续下跌 5+ 日）：必须回避

3. 北向资金 = 外资动向，被视为「聪明钱」
   - 北向连续 3 日净流入 = 强信号
   - 北向单日大幅净买入（>50 亿）= 重点关注
   - 北向重仓股（持仓 > 5%）：享受估值溢价

【使用】
  from core.a_share_market_features import (
      get_financial_window_status,
      analyze_sector_rotation,
      analyze_northbound_flow,
  )
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ==================== 1. 财报披露时间窗识别 ====================

# A 股财报披露关键日期（基于历史经验，可按年份微调）
FINANCIAL_DISCLOSURE_PERIODS = {
    # 一季报披露期：4 月 1 日 - 4 月 30 日
    "Q1": {
        "start_md": "0401", "end_md": "0430",
        "danger_days_before": 5,        # 披露前 5 个交易日
        "danger_days_after": 1,         # 披露后 1 个交易日
        "high_risk_md": ["0425", "0430"],  # 4 月最后 5 日地雷高发
        "weight": 1.0,
    },
    # 半年报披露期：7 月 15 日 - 8 月 31 日
    "H1": {
        "start_md": "0715", "end_md": "0831",
        "danger_days_before": 5,
        "danger_days_after": 1,
        "high_risk_md": ["0825", "0831"],  # 8 月最后 5 日地雷高发
        "weight": 1.2,  # 半年报地雷更多
    },
    # 三季报披露期：10 月 1 日 - 10 月 31 日
    "Q3": {
        "start_md": "1001", "end_md": "1031",
        "danger_days_before": 5,
        "danger_days_after": 1,
        "high_risk_md": ["1025", "1031"],
        "weight": 0.8,
    },
    # 年报披露期：1 月 1 日 - 4 月 30 日（次年）
    "Y": {
        "start_md": "0101", "end_md": "0430",
        "danger_days_before": 10,       # 年报最严，预警窗口更长
        "danger_days_after": 2,
        "high_risk_md": ["0415", "0430"],
        "weight": 1.5,  # 年报地雷最严重
    },
}


def get_financial_window_status(today: Optional[str] = None) -> Dict[str, Any]:
    """
    判断当前是否处于财报披露敏感期。

    Args:
        today: 当前日期 YYYYMMDD，None 则取系统时间

    Returns:
        {
            "in_disclosure_period": 是否在披露期内,
            "period_type": 报告类型 "Q1"/"H1"/"Q3"/"Y",
            "days_to_period_end": 距披露期末尾天数,
            "risk_level": "高"/"中"/"低"/"无",
            "recommendation": 操作建议
        }
    """
    if today is None:
        today_dt = datetime.now()
    else:
        today_dt = datetime.strptime(today, "%Y%m%d")

    today_md = today_dt.strftime("%m%d")
    today_str = today_dt.strftime("%Y%m%d")

    # 找当前所在披露期
    for period_type, cfg in FINANCIAL_DISCLOSURE_PERIODS.items():
        start_md = cfg["start_md"]
        end_md = cfg["end_md"]

        # 跨年处理（年报 1-4 月）
        if start_md <= end_md:
            in_period = start_md <= today_md <= end_md
        else:
            in_period = today_md >= start_md or today_md <= end_md

        if in_period:
            # 计算距披露期结束天数
            year = today_dt.year
            end_date = datetime.strptime(f"{year}{end_md}", "%Y%m%d")
            if end_date < today_dt:
                end_date = datetime.strptime(f"{year + 1}{end_md}", "%Y%m%d")
            days_to_end = (end_date - today_dt).days

            # 高风险窗口（最后 5 天）
            in_high_risk = today_md in cfg["high_risk_md"] or (
                end_date - today_dt
            ).days <= 5

            # 风险评级
            if in_high_risk:
                risk_level = "高"
                advice = "⚠️财报地雷高发期：降低仓位，规避「业绩雷」个股"
            elif days_to_end <= 10:
                risk_level = "中"
                advice = "📅财报披露密集期：保持谨慎，关注业绩预告"
            else:
                risk_level = "低"
                advice = "✅披露初期：风险可控"

            return {
                "in_disclosure_period": True,
                "period_type": period_type,
                "days_to_period_end": days_to_end,
                "risk_level": risk_level,
                "recommendation": advice,
                "weight": cfg["weight"],
            }

    return {
        "in_disclosure_period": False,
        "period_type": None,
        "days_to_period_end": None,
        "risk_level": "无",
        "recommendation": "✅非财报披露期：正常操作",
        "weight": 0.0,
    }


def apply_financial_window_filter(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """
    在财报披露敏感期内，对个股应用评分调整。

    实战逻辑：
      - 高风险窗口 + 业绩预告差（forecast_type ≤ -1）= -5 分
      - 高风险窗口 + 无业绩预告 = -2 分
      - 高风险窗口 + 业绩好（forecast_type ≥ 1）= 0 分
      - 中风险窗口 = -1 分
      - 非披露期 = 0 分

    Args:
        df: 日线 DataFrame（用于回溯）
        rt: 实时行情（用于获取 forecast_type）
        today: 当前日期

    Returns:
        {"financial_window_penalty": -5 ~ 0, "reason": "..."}
    """
    status = get_financial_window_status(today)
    if not status["in_disclosure_period"]:
        return {"financial_window_penalty": 0.0, "reason": "非披露期"}

    forecast = float(rt.get("forecast_type", 0) or 0)
    risk = status["risk_level"]

    penalty = 0.0
    reason = ""

    if risk == "高":
        if forecast <= -1.0:
            penalty = -5.0
            reason = f"📅高风险披露期+业绩差({forecast})"
        elif forecast == 0:
            penalty = -2.0
            reason = "📅高风险披露期+无业绩预告"
        else:
            reason = "📅高风险披露期但业绩好"
    elif risk == "中":
        penalty = -1.0
        reason = "📅中风险披露期"

    return {
        "financial_window_penalty": round(penalty, 2),
        "reason": reason,
        "status": status,
    }


# ==================== 2. 板块轮动深度分析 ====================

def analyze_sector_rotation(
    sector_history_df: Optional[pd.DataFrame] = None,
    today: Optional[str] = None,
    lookback_days: int = 10,
) -> Dict[str, Any]:
    """
    板块轮动深度分析（判断主线 vs 题材 vs 退潮）

    Args:
        sector_history_df: 板块历史 DataFrame，需含 industry/trade_date/pct_chg/turnover
        today: 当前日期 YYYYMMDD
        lookback_days: 回看天数

    Returns:
        {
            "main_line_sectors": 主线板块列表,
            "hot_sectors": 热门题材板块,
            "fading_sectors": 退潮板块,
            "rotation_signal": "主线接力"/"题材轮动"/"板块退潮"/"震荡盘整",
            "sector_consensus": 板块共识度 0~100
        }
    """
    if sector_history_df is None or sector_history_df.empty:
        return {
            "main_line_sectors": [],
            "hot_sectors": [],
            "fading_sectors": [],
            "rotation_signal": "无数据",
            "sector_consensus": 0.0,
        }

    try:
        sh = sector_history_df.copy()
        sh["trade_date"] = pd.to_datetime(sh["trade_date"], errors="coerce")
        sh["pct_chg"] = pd.to_numeric(sh["pct_chg"], errors="coerce").fillna(0)
        sh["turnover"] = pd.to_numeric(sh.get("turnover", 0), errors="coerce").fillna(0)

        if today is None:
            today_dt = datetime.now()
        else:
            today_dt = datetime.strptime(today, "%Y%m%d")

        # 取最近 N 日的板块数据
        recent_sh = sh[sh["trade_date"] >= (today_dt - timedelta(days=lookback_days))]
        if recent_sh.empty:
            return {
                "main_line_sectors": [],
                "hot_sectors": [],
                "fading_sectors": [],
                "rotation_signal": "无数据",
                "sector_consensus": 0.0,
            }

        # 计算每个板块的累计涨幅、连续上涨天数、最大单日跌幅
        sector_stats = []
        for ind, grp in recent_sh.groupby("industry"):
            grp = grp.sort_values("trade_date")
            cumulative_pct = float(grp["pct_chg"].sum())
            consecutive_up = 0
            max_down = 0.0
            for _, row in grp[::-1].iterrows():  # 从近到远
                if row["pct_chg"] > 0:
                    consecutive_up += 1
                else:
                    break
            for _, row in grp.iterrows():
                if row["pct_chg"] < max_down:
                    max_down = float(row["pct_chg"])

            sector_stats.append({
                "industry": ind,
                "cumulative_pct": cumulative_pct,
                "consecutive_up_days": consecutive_up,
                "max_down_pct": max_down,
            })

        df_stats = pd.DataFrame(sector_stats)

        # 分类
        # 主线：累计涨幅 > 5% 且 连续上涨 >= 3 天
        main_line = df_stats[
            (df_stats["cumulative_pct"] > 5.0)
            & (df_stats["consecutive_up_days"] >= 3)
        ]["industry"].tolist()

        # 热门题材：累计涨幅 > 3% 但 连续上涨 < 3 天
        hot = df_stats[
            (df_stats["cumulative_pct"] > 3.0)
            & (df_stats["consecutive_up_days"] < 3)
        ]["industry"].tolist()

        # 退潮：累计跌幅 < -5% 或 最大单日跌幅 < -5%
        fading = df_stats[
            (df_stats["cumulative_pct"] < -5.0)
            | (df_stats["max_down_pct"] < -5.0)
        ]["industry"].tolist()

        # 轮动信号
        n_main = len(main_line)
        n_hot = len(hot)
        n_fading = len(fading)

        if n_main >= 3:
            signal = "主线接力"
            consensus = min(100.0, 60.0 + n_main * 5.0)
        elif n_hot >= 5:
            signal = "题材轮动"
            consensus = min(100.0, 40.0 + n_hot * 3.0)
        elif n_fading >= 5:
            signal = "板块退潮"
            consensus = min(100.0, 30.0 + n_fading * 5.0)
        else:
            signal = "震荡盘整"
            consensus = 30.0

        return {
            "main_line_sectors": main_line[:5],
            "hot_sectors": hot[:5],
            "fading_sectors": fading[:5],
            "rotation_signal": signal,
            "sector_consensus": round(consensus, 1),
        }

    except Exception as e:
        logger.debug(f"板块轮动分析异常: {e}")
        return {
            "main_line_sectors": [],
            "hot_sectors": [],
            "fading_sectors": [],
            "rotation_signal": "分析失败",
            "sector_consensus": 0.0,
        }


# ==================== 3. 北向资金深度分析 ====================

def analyze_northbound_flow(
    ts_code: str,
    hk_holding_df: Optional[pd.DataFrame] = None,  # noqa: ARG001 — 已于 V26.88+9-23 移除 hk_hold 数据源，参数保留仅为调用方兼容
    nflow_history: Optional[pd.DataFrame] = None,  # noqa: ARG001 — 北向资金流深分（重仓/趋势）已下线
) -> Dict[str, Any]:
    """
    北向资金深度分析（满分 6）

    【V26.88+9-23 移除】原依赖 Tushare `hk_hold`（北向个股持股明细）数据源
    已连续 61 个交易日返回空表（数据源 9 月初开始不再维护个股级持股明细），
    经用户决策彻底下线相关打分维度。

    当前实现：直接返回零分、空标签，无数据依赖。`hk_holding_df` /
    `nflow_history` 参数保留兼容以避免上层调用方报错。
    """
    return {
        "northbound_score": 0.0,
        "hold_pct": 0.0,
        "trend": "已下线",
        "tags": [],
    }


# ==================== 主入口：综合 A 股特色市场维度 ====================
def analyze_a_share_market_context(
    ts_code: str,
    rt: Dict[str, Any],
    sector_history_df: Optional[pd.DataFrame] = None,
    hk_holding_df: Optional[pd.DataFrame] = None,
    today: Optional[str] = None,
    industry: str = "",
) -> Dict[str, Any]:
    """
    综合 A 股特色市场维度分析。

    Returns:
        {
            "financial_window": {...},
            "sector_rotation": {...},
            "northbound": {...},
            "total_score": 综合评分 (-5 ~ +12),
            "market_tags": 标签列表
        }
    """
    fw = apply_financial_window_filter(pd.DataFrame(), rt, today)
    sr = analyze_sector_rotation(sector_history_df, today)
    nb = analyze_northbound_flow(ts_code, hk_holding_df)

    total_score = (
        fw["financial_window_penalty"]
        + min(3.0, len(sr["main_line_sectors"]) if industry in sr["main_line_sectors"] else 0)  # 主线板块个股 +3
        + nb["northbound_score"]
    )

    tags = []
    if fw.get("reason"):
        tags.append(fw["reason"])
    tags.extend(nb.get("tags", []))

    return {
        "financial_window": fw,
        "sector_rotation": sr,
        "northbound": nb,
        "total_score": round(total_score, 2),
        "market_tags": tags[:5],
    }
