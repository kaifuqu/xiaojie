# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.80 — A 股特色：资金动向综合分析器

【4 大 A 股特色资金维度】
  1. 融资融券余额变化（杠杆资金）
  2. 龙虎榜机构/游资动向
  3. 大宗交易折溢价
  4. 解禁压力精确化

【数据源】（需要从 Tushare 同步）
  - margin（融资融券明细）：Tushare 接口 pro.margin
  - top_list（龙虎榜）：Tushare 接口 pro.top_list
  - block_trade（大宗交易）：Tushare 接口 pro.block_trade
  - share_float（解禁）：Tushare 接口 pro.share_float

【使用】
  from core.a_share_capital_flow import analyze_capital_flow
  result = analyze_capital_flow(ts_code, df, rt, today_date)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ==================== 1. 融资融券余额变化 ====================
def analyze_margin_balance(
    ts_code: str,
    margin_history: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """
    融资融券余额变化打分（满分 5，惩罚 -3）

    Args:
        ts_code: 股票代码
        margin_history: 融资融券历史 DataFrame，需含 trade_date / rzye（融资余额）/rqye（融券余额）

    返回:
        {
            "margin_score": 杠杆资金评分 (-3 ~ +5),
            "margin_trend": "加速流入" / "平稳" / "流出" / "无数据",
            "margin_alert": 异常告警列表
        }
    """
    if margin_history is None or margin_history.empty or len(margin_history) < 3:
        return {"margin_score": 0.0, "margin_trend": "无数据", "margin_alert": []}

    try:
        mh = margin_history.copy().sort_values("trade_date")
        if "rzye" not in mh.columns:
            return {"margin_score": 0.0, "margin_trend": "无数据", "margin_alert": []}

        # 最近 5 日融资余额变化率
        mh["rzye"] = pd.to_numeric(mh["rzye"], errors="coerce").fillna(0)
        recent_5 = mh.tail(5)
        if len(recent_5) < 2:
            return {"margin_score": 0.0, "margin_trend": "无数据", "margin_alert": []}

        rzye_start = float(recent_5["rzye"].iloc[0])
        rzye_end = float(recent_5["rzye"].iloc[-1])
        if rzye_start <= 0:
            return {"margin_score": 0.0, "margin_trend": "无数据", "margin_alert": []}

        change_pct = (rzye_end - rzye_start) / rzye_start * 100.0

        # 打分
        score = 0.0
        trend = "平稳"
        alerts = []

        if change_pct >= 20.0:
            score = 5.0
            trend = "加速流入"
        elif change_pct >= 10.0:
            score = 3.0
            trend = "明显流入"
        elif change_pct >= 3.0:
            score = 1.0
            trend = "温和流入"
        elif change_pct >= -3.0:
            score = 0.0
            trend = "平稳"
        elif change_pct >= -10.0:
            score = -1.0
            trend = "流出"
            alerts.append(f"融资余额 5 日下降 {abs(change_pct):.1f}%")
        else:
            score = -3.0
            trend = "大幅流出"
            alerts.append(f"⚠️融资余额 5 日暴跌 {abs(change_pct):.1f}%")

        # 融券异常增加（看空信号）
        if "rqye" in mh.columns:
            mh["rqye"] = pd.to_numeric(mh["rqye"], errors="coerce").fillna(0)
            rq_recent = mh["rqye"].tail(3)
            if len(rq_recent) >= 2:
                rq_change = (float(rq_recent.iloc[-1]) - float(rq_recent.iloc[0])) / max(float(rq_recent.iloc[0]), 1.0) * 100.0
                if rq_change > 50.0:
                    score -= 1.0
                    alerts.append(f"⚠️融券余额 3 日暴涨 {rq_change:.1f}%")

        return {
            "margin_score": round(score, 2),
            "margin_trend": trend,
            "margin_alert": alerts,
        }

    except Exception as e:
        logger.debug(f"融资融券分析异常 [{ts_code}]: {e}")
        return {"margin_score": 0.0, "margin_trend": "无数据", "margin_alert": []}


# ==================== 2. 龙虎榜分析 ====================
def analyze_dragon_tiger(
    ts_code: str,
    top_list_df: Optional[pd.DataFrame] = None,
) -> Dict[str, Any]:
    """
    龙虎榜机构/游资动向分析（满分 6，惩罚 -3）

    实战逻辑：
      - 机构席位净买入 ≥ 5000 万 = +3（强机构关注）
      - 机构席位净买入 ≥ 2000 万 = +2
      - 知名游资席位（如赵老哥系、欢乐海）出现 = +2
      - 机构 + 游资共振 = +1（额外）
      - 龙虎榜净卖出 = -2

    Args:
        ts_code: 股票代码
        top_list_df: 龙虎榜 DataFrame，需含 ts_code / net_buy / net_sell / side_type /营业部

    返回:
        {
            "longhubang_score": 龙虎榜评分 (-3 ~ +6),
            "is_on_longhubang": 是否上龙虎榜,
            "lhb_type": 上榜原因（"机构净买"/"游资接力"/"主力出货" 等）,
            "lhb_tags": 标签列表
        }
    """
    if top_list_df is None or top_list_df.empty:
        return {
            "longhubang_score": 0.0,
            "is_on_longhubang": False,
            "lhb_type": "无数据",
            "lhb_tags": [],
        }

    try:
        # 筛选该股龙虎榜记录
        my_lhb = top_list_df[top_list_df["ts_code"].astype(str) == str(ts_code)]
        if my_lhb.empty:
            return {
                "longhubang_score": 0.0,
                "is_on_longhubang": False,
                "lhb_type": "未上榜",
                "lhb_tags": [],
            }

        score = 0.0
        tags = []
        lhb_type = "上榜"

        # 计算机构净买入（net_buy_amount 字段名以 Tushare 文档为准）
        net_buy_col = None
        for col in ("net_buy_amount", "net_buy", "buy_amount"):
            if col in my_lhb.columns:
                net_buy_col = col
                break

        if net_buy_col:
            net_buy = float(pd.to_numeric(my_lhb[net_buy_col], errors="coerce").fillna(0).sum())
            net_buy_wan = net_buy / 1e4  # 元 → 万

            if net_buy_wan >= 5000:
                score += 3.0
                tags.append(f"🏛️机构净买 {net_buy_wan/1e4:.2f} 亿")
                lhb_type = "机构净买"
            elif net_buy_wan >= 2000:
                score += 2.0
                tags.append(f"机构净买 {net_buy_wan/1e4:.2f} 亿")
            elif net_buy_wan <= -2000:
                score -= 2.0
                tags.append(f"⚠️机构净卖 {abs(net_buy_wan)/1e4:.2f} 亿")
                lhb_type = "主力出货"

        # 检测知名游资席位（常见游资营业部）
        hot_seats = [
            "方新侠", "赵老哥", "孙哥", "欢乐海", "章盟主", "炒股养家",
            "佛山系", "宁波桑田路", "财通杭州", "中信上海分公司",
        ]
        if "营业部" in my_lhb.columns or "broker" in my_lhb.columns:
            seat_col = "营业部" if "营业部" in my_lhb.columns else "broker"
            seats = my_lhb[seat_col].astype(str).tolist()
            for seat in seats:
                for hot in hot_seats:
                    if hot in seat:
                        score += 1.0
                        tags.append(f"🔥{hot}")
                        break

        # 多个游资席位同时出现 = 共振
        hot_count = sum(1 for tag in tags if tag.startswith("🔥"))
        if hot_count >= 2:
            score += 1.0
            tags.append("游资共振")
            lhb_type = "游资接力"

        return {
            "longhubang_score": round(max(-3.0, min(6.0, score)), 2),
            "is_on_longhubang": True,
            "lhb_type": lhb_type,
            "lhb_tags": tags[:5],  # 最多 5 个标签
        }

    except Exception as e:
        logger.debug(f"龙虎榜分析异常 [{ts_code}]: {e}")
        return {
            "longhubang_score": 0.0,
            "is_on_longhubang": False,
            "lhb_type": "分析失败",
            "lhb_tags": [],
        }


# ==================== 3. 大宗交易分析 ====================
def analyze_block_trade(
    ts_code: str,
    block_trade_df: Optional[pd.DataFrame] = None,
    lookback_days: int = 30,
) -> Dict[str, Any]:
    """
    大宗交易分析（满分 3，惩罚 -4）

    实战逻辑：
      - 30 日内大宗交易净买入 ≥ 1 亿 = +3（主力看好）
      - 30 日内大宗交易净买入 ≥ 3000 万 = +2
      - 30 日内大宗交易净买入 0~3000 万 = +1
      - 高折价大宗（折价率 > 5%）= 视为出货，扣分
      - 大宗交易净卖出 > 5000 万 = -3

    Args:
        ts_code: 股票代码
        block_trade_df: 大宗交易 DataFrame，需含 ts_code / price / vol / amount
        lookback_days: 回看天数

    Returns:
        {
            "block_trade_score": 大宗交易评分 (-4 ~ +3),
            "net_block_buy_wan": 净买入（万元）,
            "discount_alert": 折价告警
        }
    """
    if block_trade_df is None or block_trade_df.empty:
        return {
            "block_trade_score": 0.0,
            "net_block_buy_wan": 0.0,
            "discount_alert": [],
        }

    try:
        bt = block_trade_df[block_trade_df["ts_code"].astype(str) == str(ts_code)]
        if bt.empty:
            return {
                "block_trade_score": 0.0,
                "net_block_buy_wan": 0.0,
                "discount_alert": [],
            }

        # 计算净买入（amount 字段：买方付的钱）
        if "amount" not in bt.columns:
            return {
                "block_trade_score": 0.0,
                "net_block_buy_wan": 0.0,
                "discount_alert": [],
            }

        amount_col = "amount"
        net_buy = float(pd.to_numeric(bt[amount_col], errors="coerce").fillna(0).sum()) / 1e4  # 元→万

        score = 0.0
        if net_buy >= 10000:
            score = 3.0
        elif net_buy >= 3000:
            score = 2.0
        elif net_buy >= 0:
            score = 1.0
        elif net_buy >= -5000:
            score = -2.0
        else:
            score = -4.0

        # 高折价告警
        alerts = []
        if "price" in bt.columns and "close" in bt.columns:
            bt = bt.copy()
            bt["discount_pct"] = (
                (pd.to_numeric(bt["price"], errors="coerce")
                 - pd.to_numeric(bt["close"], errors="coerce"))
                / pd.to_numeric(bt["close"], errors="coerce") * 100
            )
            high_discount = bt[bt["discount_pct"] < -5.0]
            if len(high_discount) > 0:
                score -= 1.0
                alerts.append(f"⚠️大宗交易高折价 {len(high_discount)} 笔")

        return {
            "block_trade_score": round(max(-4.0, min(3.0, score)), 2),
            "net_block_buy_wan": round(net_buy, 2),
            "discount_alert": alerts,
        }

    except Exception as e:
        logger.debug(f"大宗交易分析异常 [{ts_code}]: {e}")
        return {
            "block_trade_score": 0.0,
            "net_block_buy_wan": 0.0,
            "discount_alert": [],
        }


# ==================== 4. 解禁压力精确化 ====================
def analyze_unlock_pressure(
    ts_code: str,
    unlock_events: Optional[pd.DataFrame] = None,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """
    解禁压力精确分析（满分 +2，惩罚 -5）

    比原有 _get_unlock_blacklist 更精细：
      - 解禁比例 < 1%：忽略
      - 解禁比例 1~3%：扣 1 分（小压力）
      - 解禁比例 3~5%：扣 3 分（中压力）
      - 解禁比例 ≥ 5%：扣 5 分（大压力）—— 进入黑名单
      - 解禁后立即反弹 = +2（强势消化）
      - 解禁前 30 日跑赢板块 = +1（强势准备）

    Args:
        ts_code: 股票代码
        unlock_events: 解禁事件 DataFrame（ann_date / float_date / float_share / circ_mv）
        today: 当前日期 YYYYMMDD

    Returns:
        {
            "unlock_score": 解禁评分 (-5 ~ +2),
            "next_unlock_date": 下次解禁日期,
            "days_to_unlock": 距解禁天数,
            "unlock_pct": 解禁比例（%）
        }
    """
    if unlock_events is None or unlock_events.empty:
        return {
            "unlock_score": 0.0,
            "next_unlock_date": None,
            "days_to_unlock": None,
            "unlock_pct": 0.0,
        }

    try:
        ue = unlock_events.copy()
        ue["ts_code"] = ue["ts_code"].astype(str)
        ue = ue[ue["ts_code"] == str(ts_code)]
        if ue.empty:
            return {
                "unlock_score": 0.0,
                "next_unlock_date": None,
                "days_to_unlock": None,
                "unlock_pct": 0.0,
            }

        # 解析日期
        if today is None:
            today_dt = datetime.now()
        else:
            today_dt = datetime.strptime(today, "%Y%m%d")

        # 找未来 90 天内的解禁事件
        ue["float_date"] = pd.to_datetime(ue["float_date"], errors="coerce")
        future = ue[(ue["float_date"] >= today_dt) & (ue["float_date"] <= today_dt + timedelta(days=90))]
        if future.empty:
            return {
                "unlock_score": 0.0,
                "next_unlock_date": None,
                "days_to_unlock": None,
                "unlock_pct": 0.0,
            }

        # 取最近一次解禁
        nearest = future.sort_values("float_date").iloc[0]
        unlock_date = nearest["float_date"]
        days_to_unlock = (unlock_date - today_dt).days
        float_share = float(nearest.get("float_share", 0))
        total_share = float(nearest.get("total_share", 1))

        if total_share <= 0:
            unlock_pct = 0.0
        else:
            unlock_pct = float_share / total_share * 100.0

        score = 0.0
        if unlock_pct >= 5.0:
            score = -5.0
        elif unlock_pct >= 3.0:
            score = -3.0
        elif unlock_pct >= 1.0:
            score = -1.0

        # 距解禁 < 7 天加重
        if days_to_unlock <= 7 and unlock_pct >= 3.0:
            score -= 1.0

        return {
            "unlock_score": round(score, 2),
            "next_unlock_date": unlock_date.strftime("%Y%m%d"),
            "days_to_unlock": int(days_to_unlock),
            "unlock_pct": round(unlock_pct, 2),
        }

    except Exception as e:
        logger.debug(f"解禁分析异常 [{ts_code}]: {e}")
        return {
            "unlock_score": 0.0,
            "next_unlock_date": None,
            "days_to_unlock": None,
            "unlock_pct": 0.0,
        }


# ==================== 主入口：综合资金动向 ====================
def analyze_capital_flow(
    ts_code: str,
    margin_history: Optional[pd.DataFrame] = None,
    top_list_df: Optional[pd.DataFrame] = None,
    block_trade_df: Optional[pd.DataFrame] = None,
    unlock_events: Optional[pd.DataFrame] = None,
    today: Optional[str] = None,
) -> Dict[str, Any]:
    """
    A 股特色资金动向综合分析。

    Returns:
        {
            "margin": {...},
            "longhubang": {...},
            "block_trade": {...},
            "unlock": {...},
            "total_score": 综合评分 (-10 ~ +16),
            "capital_tags": 标签列表
        }
    """
    margin = analyze_margin_balance(ts_code, margin_history)
    lhb = analyze_dragon_tiger(ts_code, top_list_df)
    bt = analyze_block_trade(ts_code, block_trade_df)
    unlock = analyze_unlock_pressure(ts_code, unlock_events, today)

    total_score = (
        margin["margin_score"]
        + lhb["longhubang_score"]
        + bt["block_trade_score"]
        + unlock["unlock_score"]
    )

    # 合并标签
    tags = []
    if margin.get("margin_alert"):
        tags.extend(margin["margin_alert"])
    if lhb.get("lhb_tags"):
        tags.extend(lhb["lhb_tags"])
    if bt.get("discount_alert"):
        tags.extend(bt["discount_alert"])

    return {
        "margin": margin,
        "longhubang": lhb,
        "block_trade": bt,
        "unlock": unlock,
        "total_score": round(total_score, 2),
        "capital_tags": tags[:6],
    }


# ==================== Tushare 数据下载辅助函数 ====================
def fetch_margin_data_safe(pro_api, trade_date: str) -> Optional[pd.DataFrame]:
    """
    安全获取当日融资融券数据。
    """
    try:
        df = pro_api.margin(trade_date=trade_date)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logger.debug(f"margin 接口调用失败: {e}")
        return None


def fetch_top_list_safe(pro_api, trade_date: str) -> Optional[pd.DataFrame]:
    """
    安全获取当日龙虎榜数据。
    """
    try:
        df = pro_api.top_list(trade_date=trade_date)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logger.debug(f"top_list 接口调用失败: {e}")
        return None


def fetch_block_trade_safe(pro_api, trade_date: str) -> Optional[pd.DataFrame]:
    """
    安全获取当日大宗交易数据。
    """
    try:
        df = pro_api.block_trade(trade_date=trade_date)
        if df is None or df.empty:
            return None
        return df
    except Exception as e:
        logger.debug(f"block_trade 接口调用失败: {e}")
        return None
