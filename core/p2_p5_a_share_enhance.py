# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.80 — P2-P5 统一 A 股特色标签库

【背景】
P1 已接入涨停板/资金动向/市场维度。但 P2-P5 各自独立，A 股特色标签分散：
  - P2 有"连板预期"
  - P3 有"涨停炸板"
  - P4 有"尾盘无量拉升"
  - P5 有"T+1收割"

本模块提供 4 个池通用的 A 股特色评分与标签，让 4 个池风格一致。

【核心能力】
  1. 统一涨停板状态识别（首板/连板/炸板/回封）
  2. 统一次新股/ST 股专项拦截
  3. 统一集合竞价定级（9:25-9:30）
  4. 统一"次日表现记忆"接口
  5. 统一"板块联动力度"量化
"""
from __future__ import annotations

import logging
from typing import Dict, Any, Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ==================== 1. 涨停板状态统一识别 ====================

def unified_limit_up_status(
    df: Optional[pd.DataFrame],
    rt: Dict[str, Any],
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """
    统一识别涨停板状态（A 股实战必备）。

    Returns:
        {
            "limit_status": "首板"/"连板N板"/"炸板"/"回封"/"未涨停",
            "consecutive_limit_days": N,
            "is_burst": bool,             # 是否炸板
            "is_re_seal": bool,            # 是否回封
            "burst_count": int,
            "today_seal_strength": 0~1,   # 今日封板强度（封单/成交量）
            "tags": ["..."]
        }
    """
    if df is None or df.empty:
        return {
            "limit_status": "无数据",
            "consecutive_limit_days": 0,
            "is_burst": False,
            "is_re_seal": False,
            "burst_count": 0,
            "today_seal_strength": 0.0,
            "tags": [],
        }

    try:
        # 当前涨停天数（从日线累积）
        limit_streak = 0
        for i in range(len(df) - 1, max(len(df) - 10, -1), -1):
            pct = float(df.iloc[i].get("pct_chg", 0) or 0)
            if pct >= 9.5:  # 涨停
                limit_streak += 1
            else:
                break

        # 今日实时涨停状态（盘中可能变化）
        now_price = float(rt.get("price", 0) or 0)
        pre_close = float(rt.get("pre_close", df.iloc[-1].get("close", 0) if len(df) > 0 else 0) or 0)
        if pre_close <= 0:
            pre_close = float(df.iloc[-1].get("close", 0) or 0)
        today_pct = ((now_price - pre_close) / pre_close * 100.0) if pre_close > 0 else 0.0

        # 炸板信息（来自 rt 注入）
        burst_count = int(rt.get("limit_up_burst_count", 0) or 0)
        retry_count = int(rt.get("limit_up_retry_count", 0) or 0)
        seal_vol = float(rt.get("limit_up_seal_volume", 0) or 0)
        vol_rt = float(rt.get("volume", 1) or 1)

        # 封板强度
        today_seal_strength = seal_vol / vol_rt if vol_rt > 0 else 0.0

        # 状态判定
        tags = []
        is_burst = False
        is_re_seal = False

        if today_pct >= 9.5:
            # 今日涨停
            if burst_count == 0 and limit_streak >= 1:
                # 一字板 / 强势封板
                status = f"连板{limit_streak}板" if limit_streak >= 2 else "首板"
                tags.append(f"🔥{status}")
            elif burst_count >= 1:
                # 炸板
                is_burst = True
                status = f"炸板(连续{limit_streak}板)"
                tags.append(f"💥{status}")
                if retry_count >= 1:
                    # 炸板后回封
                    is_re_seal = True
                    tags.append("✅回封成功")
            else:
                status = f"涨停(连续{limit_streak}板)"
        elif today_pct >= 5.0 and limit_streak >= 1:
            # 昨日涨停，今日分歧
            status = f"分歧日(昨涨停)"
            tags.append("⚡分歧日")
        else:
            status = "未涨停"

        return {
            "limit_status": status,
            "consecutive_limit_days": limit_streak,
            "is_burst": is_burst,
            "is_re_seal": is_re_seal,
            "burst_count": burst_count,
            "today_seal_strength": round(today_seal_strength, 3),
            "tags": tags,
        }

    except Exception as e:
        logger.debug(f"统一涨停板状态识别异常: {e}")
        return {
            "limit_status": "识别失败",
            "consecutive_limit_days": 0,
            "is_burst": False,
            "is_re_seal": False,
            "burst_count": 0,
            "today_seal_strength": 0.0,
            "tags": [],
        }


# ==================== 2. 次新股 / ST 股专项拦截 ====================

def detect_special_stock_type(rt: Dict[str, Any]) -> Dict[str, Any]:
    """
    检测特殊股类型（A 股独有规则）。

    Returns:
        {
            "stock_type": "normal"/"new_stock_60d"/"new_stock_30d"/"st"/"*st"/"suspend_recover",
            "is_subnew": bool,        # 次新股（上市<60 日）
            "is_st": bool,            # ST/*ST
            "is_suspend_recover": bool,  # 复牌首日
            "warning_tags": ["..."]
        }
    """
    stock_type = "normal"
    is_subnew = False
    is_st = False
    is_suspend_recover = False
    warning_tags = []

    # 次新股判断
    list_days = int(rt.get("list_days", 999) or 999)
    if list_days <= 30:
        stock_type = "new_stock_30d"
        is_subnew = True
        warning_tags.append("🐣[次新30日内]波动极大")
    elif list_days <= 60:
        stock_type = "new_stock_60d"
        is_subnew = True
        warning_tags.append("🐥[次新60日内]波动大")
    elif list_days <= 90:
        stock_type = "new_stock_90d"

    # ST 股判断
    name = str(rt.get("name", "") or "").upper()
    if name.startswith("*ST") or "*ST" in name[:5]:
        stock_type = "st_asterisk"
        is_st = True
        warning_tags.append("🚨[*ST]±5%涨停板，规则不同")
    elif name.startswith("ST") or "ST" in name[:3]:
        stock_type = "st"
        is_st = True
        warning_tags.append("⚠️[ST]±5%涨停板")

    # 复牌首日
    if rt.get("is_resume_first_day", False) or rt.get("is_suspend_recover", False):
        is_suspend_recover = True
        warning_tags.append("🔓[复牌首日]波动巨大")

    return {
        "stock_type": stock_type,
        "is_subnew": is_subnew,
        "is_st": is_st,
        "is_suspend_recover": is_suspend_recover,
        "warning_tags": warning_tags,
    }


# ==================== 3. 集合竞价定级 ====================

def grade_auction(
    rt: Dict[str, Any],
    df: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """
    集合竞价定级（A 股 9:25-9:30 关键窗口）。

    实战分级：
      S 级（100 分）：一字涨停 / 一字跌停（确定性极高）
      A 级（90 分）：高开 ≥5% + 爆量（vr≥5）
      B 级（80 分）：高开 2-5% + 放量（vr≥2）
      C 级（70 分）：平高开 0-2% + 温和放量
      D 级（50 分）：低开 0-3% + 缩量
      E 级（30 分）：大幅低开（>-3%）

    Returns:
        {
            "auction_grade": "S"/"A"/"B"/"C"/"D"/"E",
            "auction_score": 0~100,
            "tags": ["..."]
        }
    """
    open_price = float(rt.get("open", 0) or 0)
    pre_close = float(rt.get("pre_close", 0) or 0)
    if pre_close <= 0 and df is not None and len(df) > 0:
        pre_close = float(df.iloc[-1].get("close", 0) or 0)
    if open_price <= 0 or pre_close <= 0:
        return {"auction_grade": "未知", "auction_score": 0.0, "tags": []}

    try:
        open_pct = (open_price - pre_close) / pre_close * 100.0
        vol_ratio = float(rt.get("vol_ratio", 1.0) or 1.0)
        amount_at_auction = float(rt.get("auction_amount", 0) or 0)  # 9:25 竞价成交额

        tags = []

        if open_pct >= 9.5:
            grade = "S"
            score = 100.0
            tags.append(f"🔥[一字涨停]高开{open_pct:.1f}%")
        elif open_pct >= 5.0:
            if vol_ratio >= 5.0:
                grade = "A+"
                score = 95.0
                tags.append(f"🚀[强势高开]高开{open_pct:.1f}%, vr={vol_ratio:.1f}")
            else:
                grade = "A"
                score = 88.0
                tags.append(f"📈[高开]高开{open_pct:.1f}%")
        elif open_pct >= 2.0:
            if vol_ratio >= 2.0:
                grade = "B+"
                score = 82.0
                tags.append(f"📈[放量高开]高开{open_pct:.1f}%, vr={vol_ratio:.1f}")
            else:
                grade = "B"
                score = 75.0
                tags.append(f"📊[温和高开]高开{open_pct:.1f}%")
        elif open_pct >= 0:
            if vol_ratio >= 1.5:
                grade = "C+"
                score = 70.0
                tags.append(f"⚖️[平高开放量]高开{open_pct:.1f}%")
            else:
                grade = "C"
                score = 65.0
                tags.append(f"⚖️[平高开]高开{open_pct:.1f}%")
        elif open_pct >= -3.0:
            grade = "D"
            score = 50.0
            tags.append(f"⚠️[低开]低开{open_pct:.1f}%")
        else:
            grade = "E"
            score = 30.0
            tags.append(f"🚨[大幅低开]低开{open_pct:.1f}%")

        # 集合竞价成交额过亿 = 强信号
        if amount_at_auction >= 1e8:
            tags.append(f"💰[竞价巨量]{amount_at_auction/1e8:.2f}亿")

        return {
            "auction_grade": grade,
            "auction_score": score,
            "tags": tags,
        }

    except Exception as e:
        logger.debug(f"集合竞价定级异常: {e}")
        return {"auction_grade": "异常", "auction_score": 0.0, "tags": []}


# ==================== 4. 板块联动力度量化 ====================

def calc_sector_linkage_strength(
    sector_avg_pct: float,
    sector_n_stocks: int,
    sector_n_limit_up: int,
    sector_n_burst: int,
    sector_top3_pct: float,
    stock_pct: float,
) -> Dict[str, Any]:
    """
    板块联动力度量化（区别于单纯 sector_beta）。

    Args:
        sector_avg_pct: 板块平均涨幅
        sector_n_stocks: 板块股票数
        sector_n_limit_up: 板块涨停股数
        sector_n_burst: 板块炸板股数
        sector_top3_pct: 板块涨幅 Top3 累计
        stock_pct: 当前股票涨幅

    Returns:
        {
            "linkage_score": -10 ~ +10,
            "is_leader": bool,           # 是否龙头
            "is_laggard": bool,          # 是否跟风
            "tags": ["..."]
        }
    """
    try:
        score = 0.0
        tags = []

        # 1. 板块涨停数量（核心联动指标）
        if sector_n_limit_up >= 5:
            score += 5.0
            tags.append(f"🔥[板块高潮]{sector_n_limit_up}只涨停")
        elif sector_n_limit_up >= 3:
            score += 3.0
            tags.append(f"🌊[板块共振]{sector_n_limit_up}只涨停")
        elif sector_n_limit_up >= 1:
            score += 1.0

        # 2. 板块炸板（风险信号）
        if sector_n_burst >= 3:
            score -= 2.0
            tags.append(f"💥[板块炸板]{sector_n_burst}只炸板")

        # 3. 板块平均涨幅
        if sector_avg_pct >= 5.0:
            score += 2.0
        elif sector_avg_pct >= 2.0:
            score += 1.0
        elif sector_avg_pct <= -3.0:
            score -= 3.0
            tags.append(f"❄️[板块普跌]{sector_avg_pct:.1f}%")

        # 4. 是否龙头（个股涨幅远超板块平均）
        excess = stock_pct - sector_avg_pct
        is_leader = excess >= 3.0
        is_laggard = excess <= -3.0

        if is_leader:
            score += 2.0
            tags.append(f"👑[龙头]超额{excess:.1f}%")
        elif is_laggard:
            score -= 1.0
            tags.append(f"🐢[跟风]落后{excess:.1f}%")

        # 5. 板块 Top3 集中度
        if sector_top3_pct >= 10.0 and sector_n_limit_up >= 3:
            score += 1.0
            tags.append(f"🎯[集中爆发]Top3涨{sector_top3_pct:.1f}%")

        score = float(np.clip(score, -10.0, 10.0))

        return {
            "linkage_score": score,
            "is_leader": is_leader,
            "is_laggard": is_laggard,
            "tags": tags,
        }

    except Exception as e:
        logger.debug(f"板块联动力度量化异常: {e}")
        return {
            "linkage_score": 0.0,
            "is_leader": False,
            "is_laggard": False,
            "tags": [],
        }


# ==================== 5. 9:30-9:35 集合竞价异常识别 ====================

def detect_open_moment_anomaly(
    df: Optional[pd.DataFrame],
    rt: Dict[str, Any],
) -> Dict[str, Any]:
    """
    9:30 开盘后 5 分钟内的异常检测（A 股实战独有窗口）。

    异常场景：
      - 集合竞价高开 + 9:30 立即下跌（高开低走陷阱）
      - 集合竞价低开 + 9:30 立即拉升（弱转强）
      - 9:30-9:35 异常放量（多空剧烈博弈）

    Returns:
        {
            "open_anomaly": "高开低走"/"弱转强"/"放量"/"正常",
            "open_score": -5 ~ +5,
            "tags": ["..."]
        }
    """
    if df is None or df.empty or len(df) < 5:
        return {"open_anomaly": "无数据", "open_score": 0.0, "tags": []}

    try:
        # 集合竞价 vs 9:30 开盘价比较
        auction_price = float(rt.get("auction_price", 0) or 0)  # 9:25 集合竞价价
        open_price = float(rt.get("open", 0) or 0)  # 9:30 开盘价
        now_price = float(rt.get("price", 0) or 0)

        tags = []
        score = 0.0

        if auction_price > 0 and open_price > 0:
            auction_to_open_pct = (open_price - auction_price) / auction_price * 100.0

            # 高开低走陷阱
            if auction_to_open_pct >= 2.0 and open_price > 0:
                # 9:30 价格远低于集合竞价价
                if now_price < open_price * 0.99:
                    score -= 3.0
                    tags.append(f"🚨[高开低走]竞价高{auction_to_open_pct:.1f}%, 开盘走低")

            # 弱转强
            if auction_to_open_pct <= -1.5:
                if now_price > open_price * 1.005:
                    score += 2.0
                    tags.append(f"✅[弱转强]竞价低开{abs(auction_to_open_pct):.1f}%, 开盘反弹")

        # 9:30-9:35 放量
        vr_5min = float(rt.get("vr_5min", 0) or 0)
        if vr_5min >= 3.0:
            score += 1.0
            tags.append(f"⚡[开盘放量]5分钟 vr={vr_5min:.1f}")
        elif vr_5min <= 0.5 and vr_5min > 0:
            score -= 1.0
            tags.append(f"😴[开盘冷清]5分钟 vr={vr_5min:.1f}")

        anomaly = "高开低走" if score < 0 else ("弱转强" if score > 0 else "正常")
        return {
            "open_anomaly": anomaly,
            "open_score": float(np.clip(score, -5.0, 5.0)),
            "tags": tags,
        }

    except Exception as e:
        logger.debug(f"开盘异常识别异常: {e}")
        return {"open_anomaly": "异常", "open_score": 0.0, "tags": []}


# ==================== 6. 炸板修复识别 ====================

def detect_burst_recovery(
    rt: Dict[str, Any],
    df: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """
    炸板修复识别（A 股实战核心策略）。

    实战逻辑：
      - 炸板次数 = 0：强封板（+5 分）
      - 炸板次数 = 1-2：分歧（0 分）
      - 炸板次数 ≥ 3：弱势（-5 分）
      - 炸板后回封 = 极强（+10 分）
      - 封单/成交量 > 50% = 强（+3 分）

    Returns:
        {
            "burst_recovery_score": -5 ~ +15,
            "burst_recovery_tags": ["..."]
        }
    """
    burst_count = int(rt.get("limit_up_burst_count", 0) or 0)
    retry_count = int(rt.get("limit_up_retry_count", 0) or 0)
    seal_vol = float(rt.get("limit_up_seal_volume", 0) or 0)
    vol_rt = float(rt.get("volume", 1) or 1)

    score = 0.0
    tags = []

    if burst_count == 0:
        score += 5.0
        tags.append("💎[强封板]无炸板")
    elif burst_count == 1:
        score += 0.0
        tags.append("⚡[1次分歧]")
    elif burst_count == 2:
        score -= 2.0
        tags.append("⚠️[2次炸板]")
    else:
        score -= 5.0
        tags.append(f"💥[频繁炸板]{burst_count}次")

    # 炸板后回封
    if burst_count >= 1 and retry_count >= 1:
        score += 10.0
        tags.append(f"✅[回封成功]{retry_count}次回封")

    # 封单强度
    if vol_rt > 0:
        seal_ratio = seal_vol / vol_rt
        if seal_ratio >= 0.5:
            score += 3.0
            tags.append(f"💪[封单强]{seal_ratio*100:.0f}%")
        elif seal_ratio < 0.2 and seal_vol > 0:
            score -= 2.0
            tags.append(f"😰[封单弱]{seal_ratio*100:.0f}%")

    return {
        "burst_recovery_score": float(np.clip(score, -10.0, 15.0)),
        "burst_recovery_tags": tags,
    }


# ==================== 主入口：综合 A 股特色评分 ====================

def unified_a_share_score(
    df: Optional[pd.DataFrame],
    rt: Dict[str, Any],
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """
    P2-P5 统一 A 股特色评分（与 P1 的 a_share_integration 接口一致）。

    Returns:
        {
            "limit_status": 涨停板状态,
            "special_stock": 次新/ST 检测,
            "auction_grade": 集合竞价定级,
            "open_anomaly": 开盘异常,
            "burst_recovery": 炸板修复,
            "total_boost": 综合加成（-10 ~ +20）,
            "all_tags": 所有标签,
            "is_p2_p5_veto": bool,      # 是否一票否决
            "veto_reason": str
        }
    """
    try:
        # 1. 涨停板状态
        limit_status = unified_limit_up_status(df, rt, today)

        # 2. 特殊股票
        special = detect_special_stock_type(rt)

        # 3. 集合竞价定级
        auction = grade_auction(rt, df)

        # 4. 开盘异常
        open_anomaly = detect_open_moment_anomaly(df, rt)

        # 5. 炸板修复
        burst_rec = detect_burst_recovery(rt, df)

        # 综合加成
        total_boost = 0.0
        all_tags = []

        # 涨停板加分
        if limit_status["is_re_seal"]:
            total_boost += 8.0
        elif limit_status["consecutive_limit_days"] >= 3:
            total_boost += 3.0
        elif limit_status["consecutive_limit_days"] >= 1:
            total_boost += 1.0
        all_tags.extend(limit_status["tags"])

        # 炸板修复
        total_boost += burst_rec["burst_recovery_score"]
        all_tags.extend(burst_rec["burst_recovery_tags"])

        # 集合竞价
        auction_score = auction["auction_score"]
        if auction_score >= 90:
            total_boost += 3.0
        elif auction_score >= 70:
            total_boost += 1.0
        elif auction_score <= 40:
            total_boost -= 2.0
        all_tags.extend(auction["tags"])

        # 开盘异常
        total_boost += open_anomaly["open_score"]
        all_tags.extend(open_anomaly["tags"])

        # 次新/ST 警告（扣分）
        if special["stock_type"] == "new_stock_30d":
            total_boost -= 5.0
        elif special["stock_type"] == "new_stock_60d":
            total_boost -= 2.0
        if special["is_st"]:
            total_boost -= 3.0
        all_tags.extend(special["warning_tags"])

        # 一票否决判定
        is_veto = False
        veto_reason = ""

        # 次新 30 日内 + ST + 复牌 = 风险极高
        if special["stock_type"] in ("new_stock_30d", "st_asterisk", "st") and special["is_suspend_recover"]:
            is_veto = True
            veto_reason = f"高风险股:{special['stock_type']} + 复牌"

        total_boost = float(np.clip(total_boost, -10.0, 20.0))

        return {
            "limit_status": limit_status,
            "special_stock": special,
            "auction_grade": auction,
            "open_anomaly": open_anomaly,
            "burst_recovery": burst_rec,
            "total_boost": round(total_boost, 2),
            "all_tags": all_tags,
            "is_p2_p5_veto": is_veto,
            "veto_reason": veto_reason,
        }

    except Exception as e:
        logger.warning(f"P2-P5 A 股特色评分异常: {e}")
        return {
            "limit_status": {},
            "special_stock": {},
            "auction_grade": {},
            "open_anomaly": {},
            "burst_recovery": {},
            "total_boost": 0.0,
            "all_tags": [],
            "is_p2_p5_veto": False,
            "veto_reason": "",
        }
