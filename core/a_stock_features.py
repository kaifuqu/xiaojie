# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.80 — A 股特色：涨停板生态分析器

【A 股特殊性】
T+1 + 涨停板 10%/20%/ST 5% 制度，形成独特生态：
  - 首板（1板）→ 二板 → 三板 → 高位板
  - 涨停封单量 = 主力实力
  - 涨停时间点（早盘 vs 尾盘）= 主力意图
  - 连板梯队共振（板块内多只涨停）

【7 个子维度】
  1. limit_grade：连板梯队（首板 1 分 / 2板 3 分 / 3板 5 分 / ≥4板 8 分）
  2. seal_strength：封单强度（封单量/成交量 × 100），>50% 极强
  3. first_seal_time：首封时间（9:30-10:00 极强，14:30 后极弱）
  4. open_count：开板次数（开 0 次 = 强，开 ≥3 次 = 弱）
  5. board_consensus：板块共识（同板块涨停数 ≥3 = 共振）
  6. broken_board_risk：炸板风险（昨日涨停今日低开 > 2% = 高风险）
  7. dragon_pullback：龙回头形态（涨停后回调 3-5 天缩量 + 再放量）

【使用】
  from core.a_stock_features import analyze_limit_features
  result = analyze_limit_features(df, rt, sector_df, today_date)
  # result: {"limit_grade_score": 0~10, "seal_strength_score": 0~5, ...}
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ==================== 1. 连板梯队 ====================
def calc_limit_grade(limit_times: float) -> float:
    """
    连板梯队打分（满分 10）

    - 0 板（未涨停）= 0
    - 1 板（首板）= 1
    - 2 板 = 3
    - 3 板 = 5
    - 4 板 = 7
    - ≥5 板 = 8（封顶，避免追高）
    """
    try:
        n = int(float(limit_times or 0))
    except (TypeError, ValueError):
        return 0.0
    if n <= 0:
        return 0.0
    if n == 1:
        return 1.0
    if n == 2:
        return 3.0
    if n == 3:
        return 5.0
    if n == 4:
        return 7.0
    return 8.0  # 5板及以上不再累加


# ==================== 2. 封单强度 ====================
def calc_seal_strength(seal_amount: float, vol_amount: float) -> float:
    """
    封单强度打分（满分 5）

    seal_amount/vol_amount 比例：
      - ≥1.0  = 5（极强，封单 = 全部成交）
      - 0.5~1.0 = 4（强）
      - 0.2~0.5 = 3（中性偏强）
      - 0.1~0.2 = 2（中性偏弱）
      - <0.1  = 1（弱）
      - 无数据 = 0
    """
    if seal_amount <= 0 or vol_amount <= 0:
        return 0.0
    ratio = seal_amount / vol_amount
    if ratio >= 1.0:
        return 5.0
    if ratio >= 0.5:
        return 4.0
    if ratio >= 0.2:
        return 3.0
    if ratio >= 0.1:
        return 2.0
    return 1.0


# ==================== 3. 首封时间 ====================
def calc_first_seal_time(hour: int, minute: int) -> float:
    """
    首封时间打分（满分 5）—— A 股实战共识：
      - 9:25-9:35（集合竞价后立刻封）= 极强信号
      - 9:30-10:00（开盘半小时）= 5 分
      - 10:00-11:30（上午盘）= 3 分
      - 13:00-14:00（下午盘）= 2 分
      - 14:00-14:30（尾盘前）= 1 分
      - 14:30+（尾盘）= 0 分（诱多嫌疑）

    注：数据源需提供 first_seal_time（首封时间戳）
    """
    if hour < 0 or minute < 0:
        return 0.0
    minutes_since_open = max(0, (hour - 9) * 60 + minute - 30)
    if minutes_since_open <= 5:
        return 5.0
    if minutes_since_open <= 30:
        return 5.0
    if minutes_since_open <= 120:
        return 3.0
    if minutes_since_open <= 270:
        return 2.0
    if minutes_since_open <= 300:
        return 1.0
    return 0.0


# ==================== 4. 开板次数 ====================
def calc_open_count_penalty(open_count: int) -> float:
    """
    开板次数惩罚（满分 0，越高越差）

      - 开 0 次 = 0 扣分
      - 开 1 次 = -1
      - 开 2 次 = -2
      - 开 ≥3 次 = -3（封板不稳）
    """
    if open_count <= 0:
        return 0.0
    if open_count == 1:
        return -1.0
    if open_count == 2:
        return -2.0
    return -3.0


# ==================== 5. 板块涨停共振 ====================
def calc_sector_board_consensus(
    ts_code: str,
    industry: str,
    sector_df: Optional[pd.DataFrame],
) -> float:
    """
    板块涨停共振（满分 4）

    判断当天同板块（行业）涨停股数量：
      - ≥5 只 = 4（板块高潮）
      - 3~4 只 = 3（强共振）
      - 2 只 = 2
      - 1 只 = 1
      - 0 只 = 0
    """
    if sector_df is None or sector_df.empty:
        return 0.0
    if "industry" not in sector_df.columns:
        return 0.0
    if "limit_times" not in sector_df.columns and "is_limit_up" not in sector_df.columns:
        return 0.0

    same_ind = sector_df[sector_df["industry"] == industry]
    if same_ind.empty:
        return 0.0

    # 计算同板块涨停数（limit_times >= 1 或 is_limit_up == True）
    if "is_limit_up" in same_ind.columns:
        n_limit = int((same_ind["is_limit_up"].astype(bool)).sum())
    else:
        n_limit = int((pd.to_numeric(same_ind["limit_times"], errors="coerce").fillna(0) >= 1).sum())

    if n_limit >= 5:
        return 4.0
    if n_limit >= 3:
        return 3.0
    if n_limit == 2:
        return 2.0
    if n_limit == 1:
        return 1.0
    return 0.0


# ==================== 6. 炸板风险（昨日涨停今日低开）====================
def calc_broken_board_risk(df: pd.DataFrame) -> float:
    """
    昨日涨停今日低开风险（惩罚 0 ~ -5）

    实战逻辑：
      - 昨日涨停（pct_chg >= 9.5%），今日低开 > 3% = -5（炸板预警）
      - 昨日涨停，今日低开 1~3% = -3
      - 昨日涨停，今日高开 < 2% = -1（弱势）
      - 昨日涨停，今日高开 ≥ 2% = 0（健康）
    """
    if df is None or df.empty or len(df) < 2:
        return 0.0
    try:
        last = df.iloc[-1]
        prev = df.iloc[-2]

        prev_pct = float(pd.to_numeric(prev.get("pct_chg", 0), errors="coerce") or 0)
        today_open = float(pd.to_numeric(last.get("open", 0), errors="coerce") or 0)
        prev_close = float(pd.to_numeric(prev.get("close", 0), errors="coerce") or 0)

        # 仅当昨日涨停才计算
        if prev_pct < 9.0:
            return 0.0
        if prev_close <= 0:
            return 0.0

        gap_pct = (today_open - prev_close) / prev_close * 100.0
        if gap_pct <= -3.0:
            return -5.0
        if gap_pct <= -1.0:
            return -3.0
        if gap_pct < 2.0:
            return -1.0
        return 0.0
    except Exception as e:
        logger.debug("炸板风险计算异常: %s", e)
        return 0.0


# ==================== 7. 龙回头形态识别 ====================
def calc_dragon_pullback(df: pd.DataFrame) -> float:
    """
    龙回头形态识别（满分 4）

    实战逻辑（强势股洗盘后启动）：
      - 近 5 日内有涨停（pct_chg >= 9.5%）
      - 涨停后回调 2~3 天，回调幅度 < 5%
      - 回调期间缩量（量比 < 0.8）
      - 今日温和放量（vol_ratio 1.0~1.5）+ 收阳

    返回：命中 4 分，否则 0 分
    """
    if df is None or df.empty or len(df) < 6:
        return 0.0

    try:
        recent = df.tail(6).copy()
        recent["pct_chg"] = pd.to_numeric(recent["pct_chg"], errors="coerce").fillna(0)
        recent["vol_ratio"] = pd.to_numeric(recent["vol_ratio"], errors="coerce").fillna(1.0)

        # 找最近的涨停日（不包含今日）
        limit_days = recent[recent["pct_chg"] >= 9.5].index.tolist()
        if not limit_days:
            return 0.0

        # 取最后一次涨停距今的天数
        last_limit_idx = limit_days[-1]
        if last_limit_idx == recent.index[-1]:
            # 今日就涨停，不是「回头」形态
            return 0.0

        # 涨停后的回调天数
        pullback_days = recent.index.get_loc(recent.index[-1]) - recent.index.get_loc(last_limit_idx)
        if pullback_days < 2 or pullback_days > 4:
            return 0.0

        # 回调幅度
        limit_close = float(recent.loc[last_limit_idx, "close"])
        today_close = float(recent.iloc[-1]["close"])
        if limit_close <= 0:
            return 0.0
        pullback_pct = (today_close - limit_close) / limit_close * 100.0
        if pullback_pct < -8.0 or pullback_pct > 0:
            return 0.0

        # 回调期间缩量
        pullback_period = recent.iloc[recent.index.get_loc(last_limit_idx) + 1:]
        avg_vol_ratio = float(pullback_period["vol_ratio"].mean())
        if avg_vol_ratio > 0.9:
            return 0.0

        # 今日温和放量 + 收阳
        today_vol_ratio = float(recent.iloc[-1]["vol_ratio"])
        today_pct = float(recent.iloc[-1]["pct_chg"])
        if today_vol_ratio < 0.9 or today_vol_ratio > 1.8:
            return 0.0
        if today_pct <= 0:
            return 0.0

        return 4.0
    except Exception as e:
        logger.debug("龙回头识别异常: %s", e)
        return 0.0


# ==================== 主入口：综合涨停板分析 ====================
def analyze_limit_features(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    sector_df: Optional[pd.DataFrame] = None,
    industry: str = "",
) -> Dict[str, Any]:
    """
    综合涨停板生态分析（A 股特色核心维度）。

    Args:
        df: 日线 DataFrame
        rt: 实时行情字典（含 limit_times/strth/seal_amount/open_count 等）
        sector_df: 全市场 DataFrame（用于板块共识判断）
        industry: 个股所属行业

    Returns:
        {
            "limit_grade_score": 连板梯队 (0~10),
            "seal_strength_score": 封单强度 (0~5),
            "first_seal_time_score": 首封时间 (0~5),
            "open_count_penalty": 开板次数 (负值 0~-3),
            "sector_consensus_score": 板块共振 (0~4),
            "broken_board_penalty": 炸板风险 (负值 0~-5),
            "dragon_pullback_score": 龙回头 (0~4),
            "total_bonus": 综合加成 (-5 ~ +33),
            "limit_tags": 涨停标签列表
        }
    """
    try:
        # 1. 连板梯队
        limit_times = float(rt.get("limit_times", 0) or 0)
        limit_grade = calc_limit_grade(limit_times)

        # 2. 封单强度
        seal_amount = float(rt.get("seal_amount", 0) or 0)
        vol_amount = float(rt.get("amount", 0) or 0)
        seal_str = calc_seal_strength(seal_amount, vol_amount)

        # 3. 首封时间
        seal_hour = int(rt.get("first_seal_hour", -1) or -1)
        seal_min = int(rt.get("first_seal_minute", -1) or -1)
        seal_time = calc_first_seal_time(seal_hour, seal_min)

        # 4. 开板次数
        open_cnt = int(rt.get("open_count", 0) or 0)
        open_penalty = calc_open_count_penalty(open_cnt)

        # 5. 板块涨停共振
        sector_consensus = calc_sector_board_consensus(
            rt.get("ts_code", ""), industry, sector_df
        )

        # 6. 炸板风险
        broken = calc_broken_board_risk(df)

        # 7. 龙回头
        dragon = calc_dragon_pullback(df)

        total = (
            limit_grade
            + seal_str
            + seal_time
            + open_penalty
            + sector_consensus
            + broken
            + dragon
        )

        # 生成标签
        tags = []
        if limit_grade >= 5:
            tags.append(f"🔥{int(limit_times)}连板")
        elif limit_times >= 1:
            tags.append("首板启动")
        if seal_str >= 4:
            tags.append("💪封单极强")
        if seal_time >= 4:
            tags.append("⚡早盘秒封")
        if open_penalty < -2:
            tags.append("⚠️开板不稳")
        if sector_consensus >= 3:
            tags.append("🌊板块共振")
        if broken < -3:
            tags.append("💣炸板预警")
        if dragon >= 4:
            tags.append("🐉龙回头启动")

        return {
            "limit_grade_score": round(limit_grade, 2),
            "seal_strength_score": round(seal_str, 2),
            "first_seal_time_score": round(seal_time, 2),
            "open_count_penalty": round(open_penalty, 2),
            "sector_consensus_score": round(sector_consensus, 2),
            "broken_board_penalty": round(broken, 2),
            "dragon_pullback_score": round(dragon, 2),
            "total_bonus": round(total, 2),
            "limit_tags": tags,
        }

    except Exception as e:
        logger.warning(f"涨停板分析异常: {e}")
        return {
            "limit_grade_score": 0.0,
            "seal_strength_score": 0.0,
            "first_seal_time_score": 0.0,
            "open_count_penalty": 0.0,
            "sector_consensus_score": 0.0,
            "broken_board_penalty": 0.0,
            "dragon_pullback_score": 0.0,
            "total_bonus": 0.0,
            "limit_tags": [],
        }
