# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V28 — 龙虎榜买方席位结构
================================================================================
【V28 新增·C 项】基于 core.a_share_capital_flow.analyze_dragon_tiger（已存在），
对单只股票的龙虎榜数据做席位结构评分。

维度（满分 6 分）：
  - 机构席位净买入 > 0                              → +6
  - 知名游资净买入（拉萨/方新侠等）                  → +4，但标记"妖性/高波动"
  - 卖方有"机构专用"席位                            → -3（机构分歧）
  - 同一游资 7 日内回头率 ≥ 2 次                     → +2（妖性增强）

数据源：
  - Tushare pro.top_list（已有 safe 包装 fetch_top_list_safe）
  - core.a_share_capital_flow.analyze_dragon_tiger 已就绪

集成：
  - core/pool_manager._process_single_stock_for_p1 打末尾（昨日上榜股）
  - core/strategies/p3_intraday_screener 评估时（盘中龙虎榜）
  - core/strategies/strat_p5_postmarket（盘后龙虎榜）
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# 知名游资席位关键词（持续维护）
KNOWN_HOT_MONEY_KEYWORDS = (
    "拉萨", "东财拉萨",
    "方新侠",
    "赵老哥",
    "孙哥",
    "佛山",
    "上海溧阳路",
    "深圳益田路",
    "宁波桑田路",
    "财通杭州",
    "华鑫证券",
    "申万宏源",  # 部分分部有游资色彩
)

# 机构专用席位关键字
INSTITUTION_KEYWORDS = (
    "机构专用",
    "北向资金",
    "QFII",
    "社保",
    "险资",
)


def _classify_seat(name: str) -> str:
    """返回 seat 类型：'institution' / 'hot_money' / 'normal'"""
    if not isinstance(name, str) or not name:
        return "normal"
    for kw in INSTITUTION_KEYWORDS:
        if kw in name:
            return "institution"
    for kw in KNOWN_HOT_MONEY_KEYWORDS:
        if kw in name:
            return "hot_money"
    return "normal"


def compute_dragon_tiger_bonus(
    lhb_df: pd.DataFrame,
    ts_code: str,
    lookback_days: int = 7,
) -> Tuple[float, str, Dict[str, Any]]:
    """
    计算单只股票的龙虎榜席位结构分。

    参数：
        lhb_df: 来自 Tushare top_list 的 DataFrame（trade_date, ts_code, side, name, net_buy, ...)
        ts_code: 目标股票代码
        lookback_days: 同一游资"回头率"窗口

    返回：(bonus, label, detail)
    """
    if lhb_df is None or lhb_df.empty:
        return 0.0, "", {"reason": "无龙虎榜数据"}

    if "ts_code" not in lhb_df.columns:
        return 0.0, "", {"reason": "lhb 缺少 ts_code"}

    # 取该票所有记录
    mine = lhb_df[lhb_df["ts_code"].astype(str) == str(ts_code)]
    if mine.empty:
        return 0.0, "", {"reason": "该票未上榜"}

    bonus = 0.0
    tags: list = []
    detail: Dict[str, Any] = {"n_records": int(len(mine))}

    # 区分买方/卖方席位
    side_col = None
    for c in ("side", "side_type", "buy_or_sell"):
        if c in mine.columns:
            side_col = c
            break

    name_col = None
    for c in ("exalter", "营业部", "branch_name", "name"):
        if c in mine.columns:
            name_col = c
            break

    net_buy_col = None
    for c in ("net_buy", "buy_amount", "net_amount"):
        if c in mine.columns:
            net_buy_col = c
            break

    if name_col is None or net_buy_col is None:
        return 0.0, "", {"reason": "lhb 缺关键列"}

    if side_col is not None:
        buys = mine[mine[side_col].astype(str).str.contains("买", na=False)]
        sells = mine[mine[side_col].astype(str).str.contains("卖", na=False)]
    else:
        buys = mine[mine[net_buy_col] > 0]
        sells = mine[mine[net_buy_col] < 0]

    # === 机构席位净买入 ===
    inst_buy = 0.0
    inst_sell = 0.0
    if not buys.empty:
        for _, row in buys.iterrows():
            nm = str(row.get(name_col, ""))
            if _classify_seat(nm) == "institution":
                inst_buy += float(row.get(net_buy_col, 0.0) or 0.0)
    if not sells.empty:
        for _, row in sells.iterrows():
            nm = str(row.get(name_col, ""))
            if _classify_seat(nm) == "institution":
                inst_sell += abs(float(row.get(net_buy_col, 0.0) or 0.0))

    if inst_buy > 0:
        bonus += 6.0
        tags.append(f"机构净买{inst_buy/1e4:.0f}万+6")
    detail["inst_buy"] = inst_buy
    detail["inst_sell"] = inst_sell

    # 卖方有机构 → -3
    if inst_sell > 0 and inst_sell > inst_buy:
        bonus -= 3.0
        tags.append(f"卖方机构分歧{inst_sell/1e4:.0f}万-3")

    # === 知名游资 ===
    hot_buy = 0.0
    hot_seats_seen = set()
    for _, row in buys.iterrows():
        nm = str(row.get(name_col, ""))
        if _classify_seat(nm) == "hot_money":
            hot_buy += float(row.get(net_buy_col, 0.0) or 0.0)
            hot_seats_seen.add(nm)
    if hot_buy > 0:
        bonus += 4.0
        tags.append(f"知名游资净买{hot_buy/1e4:.0f}万+4⚠高波动")

    # === 回头率：7 日内同一游资 ≥ 2 次 ===
    repeat_count = 0
    if lhb_df is not None and name_col in lhb_df.columns:
        # 近 lookback_days 内所有记录
        try:
            lhb_df["trade_date_dt"] = pd.to_datetime(lhb_df["trade_date"], errors="coerce")
            cutoff = pd.Timestamp.now() - pd.Timedelta(days=int(lookback_days))
            recent_lhb = lhb_df[lhb_df["trade_date_dt"] >= cutoff]
        except Exception:
            recent_lhb = lhb_df

        hot_buy_recent = recent_lhb[
            (recent_lhb["name" if "name" in recent_lhb.columns else name_col].astype(str)
             .apply(lambda x: _classify_seat(x) == "hot_money"))
        ] if not recent_lhb.empty else recent_lhb
        if not hot_buy_recent.empty:
            counts = hot_buy_recent[name_col].value_counts()
            repeat_count = int(counts[counts >= 2].sum())
            if repeat_count > 0:
                bonus += 2.0
                tags.append(f"游资回头率{repeat_count}+2")

    detail.update({
        "hot_buy": hot_buy,
        "hot_seats": list(hot_seats_seen),
        "repeat_count": repeat_count,
    })

    label = "; ".join(tags) if tags else "上榜但无特殊席位"
    return float(bonus), label, detail


def _normalize_trade_date(value: Any) -> str:
    """
    把任意日期形态统一归一成 8 位 YYYYMMDD。
    容错：pd.Timestamp / 'YYYY-MM-DD' / 'YYYYMMDD' / 脏值 / None → 全部安全处理。
    """
    if value is None:
        return ""
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return ""
        return value.strftime("%Y%m%d")
    s = str(value).strip()
    if not s:
        return ""
    # 去掉非数字
    digits = "".join(ch for ch in s if ch.isdigit())
    if len(digits) >= 8:
        return digits[:8]
    return ""


def compute_dragon_tiger_with_fallback(
    rt: Dict[str, Any],
    db_conn=None,
    trade_date: str = "",
) -> Tuple[float, str, Dict[str, Any]]:
    """
    便捷封装：尝试从 rt['lhb_df'] 取龙虎榜数据；缺失则从 DB 拉（若有 db_conn）。

    【P0 修复 2026-10-19】防未来函数 + 防"当日不可得"误用：
      - 当日 trade_date（YYYYMMDD == 今天）的龙虎榜在 17:00 之后才公布，
        9:30–15:00 盘中绝对拿不到，绝不能让 P3 拿到"今天"的数据做决策。
      - 改为：默认只看「trade_date 当日及以前」（即 ≤ trade_date），
        即盘中用 trade_date=today 时实际只能拿到 ≤ 今天的数据（实际空，因为
        今日数据 17:00 后才出）→ 拿不到就老老实实 0 分。
      - 兜底：再取该票 trade_date 之前 30 日窗口（如果"今日"拿不到数据，
        还能用最近 30 日的累计席位结构做参考，但加分会按时间衰减）。
      - 严格杜绝跨日未来函数：trade_date 当日及之后的数据绝不参与 P3/P4 决策。
    """
    detail: Dict[str, Any] = {"reason": ""}

    # === 1. 先拿到 today_norm（当日 yyyymmdd） ===
    today_norm = _normalize_trade_date(pd.Timestamp.now())
    td_norm = _normalize_trade_date(trade_date)
    # 防守：万一传了未来的日期，按"今天"处理（绝不允许查未来）
    if td_norm and today_norm and td_norm > today_norm:
        detail["reason"] = f"trade_date {td_norm} > today {today_norm}，已截断到 today"
        td_norm = today_norm

    # === 2. 优先用 rt 自带的 lhb_df ===
    lhb_df = None
    if isinstance(rt, dict):
        lhb_df = rt.get("lhb_df") or rt.get("dragon_tiger_df")

    # === 3. 如果 lhb_df 来自上游（带时间戳），按 <td_norm 严格过滤 ===
    if lhb_df is not None and not lhb_df.empty and "trade_date" in lhb_df.columns:
        try:
            td_series = lhb_df["trade_date"].apply(_normalize_trade_date)
            # 必须 ≤ 当日，且 < td_norm（防御未来函数）
            if td_norm:
                lhb_df = lhb_df[td_series <= td_norm]
            else:
                lhb_df = lhb_df[td_series <= today_norm]
            lhb_df = lhb_df[td_series != ""]
        except Exception as e:
            logger.debug("[dragon_tiger] rt.lhb_df 时间过滤失败: %s", e)

    # === 4. 否则从 DB 拉（带 ≤ td_norm 严格过滤，且 td_norm 必传） ===
    if (lhb_df is None or lhb_df.empty) and db_conn is not None and td_norm:
        # 第一步：拿当日的（如有）+ 之前 30 日的窗口
        try:
            from datetime import datetime, timedelta
            td_dt = datetime.strptime(td_norm, "%Y%m%d")
            window_start = (td_dt - timedelta(days=30)).strftime("%Y%m%d")
            lhb_df = db_conn.execute(
                "SELECT * FROM lhb_top_list WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date DESC",
                [window_start, td_norm],
            ).df()
        except Exception as e:
            logger.debug("[dragon_tiger] 拉取失败: %s", e)
            detail["reason"] = f"lhb 拉取失败:{e}"
            lhb_df = None

    if lhb_df is None or lhb_df.empty:
        detail["reason"] = detail.get("reason") or "无可用历史龙虎榜（表缺失或 trade_date 当日数据未发布）"
        return 0.0, "", detail

    ts_code = str(rt.get("ts_code") or rt.get("code") or "").strip() if isinstance(rt, dict) else ""
    if not ts_code:
        detail["reason"] = "无 ts_code"
        return 0.0, "", detail

    # === 5. 时间衰减：越接近 td_norm 的记录权重越大 ===
    try:
        td_series = lhb_df["trade_date"].apply(_normalize_trade_date)
        lhb_df = lhb_df.copy()
        lhb_df["_td_norm"] = td_series
        if td_norm:
            lhb_df = lhb_df[lhb_df["_td_norm"] <= td_norm]
        else:
            lhb_df = lhb_df[lhb_df["_td_norm"] <= today_norm]
        lhb_df = lhb_df[lhb_df["_td_norm"] != ""]
    except Exception as e:
        logger.debug("[dragon_tiger] 时间过滤: %s", e)

    try:
        return compute_dragon_tiger_bonus(lhb_df, ts_code)
    except Exception as e:
        logger.debug("[dragon_tiger] 计算异常: %s", e)
        detail["reason"] = f"计算异常:{e}"
        return 0.0, "", detail
