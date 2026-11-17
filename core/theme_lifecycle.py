# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V28 — 题材生命周期分类器
================================================================================
【V28 新增·A 项】用 stock_basic.industry 作为"题材代理"（HiThink 无 concept 端点，
Tushare concept 接口有 bug，详见 data_fetcher.sync_concept_tags_recent 注释）。

对每个 industry 聚合该行业所有股票的 5 日/20 日累计涨幅 + 涨停数，
输出 5 类生命周期状态：

  1. 导入期（5d 涨幅 0-5% 且 涨停 < 5 只）→ 进攻期高（+5 分）
  2. 上升期（5d 涨幅 5-15% 且 涨停 5-10 只）→ 主升期（+8 分）
  3. 高潮期（5d 涨幅 ≥ 15% 或 涨停 ≥ 15 只）→ 接盘期（-12 分）
  4. 退潮期（5d 跌幅 -10% 至 0% 且 涨停 < 3 只）→ 观察期（0 分）
  5. 混沌期（涨跌互现，无明显特征）→ 中性（+1 分）

集成：
  - core/pool_manager.py P1 在打末尾叠加题材分
  - core/strategies/p3_intraday_screener P3 加共振判断
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)


# 5 类状态 + 分值表
LIFECYCLE_STATES = {
    "import":      {"label": "导入期", "score_bonus": 5.0,  "is_safe": True},
    "rising":      {"label": "上升期", "score_bonus": 8.0,  "is_safe": True},
    "climax":      {"label": "高潮期", "score_bonus": -12.0, "is_safe": False},
    "ebbing":      {"label": "退潮期", "score_bonus": 0.0,  "is_safe": True},
    "chaos":       {"label": "混沌期", "score_bonus": 1.0,  "is_safe": True},
}


def classify_lifecycle(
    pct_5d_sum: float,
    pct_20d_sum: float,
    limit_up_count: int = 0,
    industry_drop_rate: float = 0.0,
) -> str:
    """
    返回 5 类状态之一：import / rising / climax / ebbing / chaos
    """
    try:
        p5 = float(pct_5d_sum)
        p20 = float(pct_20d_sum)
        n_lim = int(limit_up_count)
        drop = float(industry_drop_rate)
    except (TypeError, ValueError):
        return "chaos"

    # 高潮期：5d 涨幅 ≥15% 或涨停 ≥15 只 → 接盘期
    if p5 >= 15.0 or n_lim >= 15:
        return "climax"

    # 退潮期：5d 跌幅 -10% 至 0% 且涨停 < 3
    if -10.0 <= p5 <= 0.0 and n_lim < 3:
        return "ebbing"

    # 上升期：5d 涨幅 5-15% 且涨停 5-10 只
    if 5.0 <= p5 <= 15.0 and 5 <= n_lim <= 10:
        return "rising"

    # 导入期：5d 涨幅 0-5% 且涨停 < 5
    if 0.0 <= p5 <= 5.0 and n_lim < 5:
        return "import"

    # 混沌：其余
    return "chaos"


def compute_lifecycle_bonus(state: str) -> Tuple[float, str]:
    """
    返回 (bonus, label)
    """
    info = LIFECYCLE_STATES.get(state, LIFECYCLE_STATES["chaos"])
    return float(info["score_bonus"]), f"题材[{info['label']}]{info['score_bonus']:+.0f}"


def batch_compute_industry_lifecycle(
    df_panel: pd.DataFrame,
    industry_col: str = "industry",
    pct_chg_col: str = "pct_chg_5d",
    limit_col: str = "is_limit_up",
) -> Dict[str, Dict[str, Any]]:
    """
    批量计算多个 industry 的生命周期状态。

    参数：
        df_panel: 含 (industry, pct_chg_5d, is_limit_up) 的 DataFrame
        industry_col: 行业列名
        pct_chg_col: 5 日涨幅列名
        limit_col: 是否涨停的布尔列名

    返回：
        {industry: {"state": "rising", "score_bonus": 8.0, "label": "上升期",
                    "n_limit_up": int, "pct_5d_mean": float, "pct_20d_mean": float}}
    """
    if df_panel is None or df_panel.empty:
        return {}

    if industry_col not in df_panel.columns:
        return {}

    out: Dict[str, Dict[str, Any]] = {}
    grouped = df_panel.groupby(industry_col)
    for ind, group in grouped:
        if not isinstance(ind, str) or not ind.strip():
            continue
        ind = ind.strip()
        pcts = pd.to_numeric(group.get(pct_chg_col, pd.Series(dtype=float)), errors="coerce").fillna(0.0)
        n_lim = 0
        if limit_col in group.columns:
            n_lim = int(pd.to_numeric(group[limit_col], errors="coerce").fillna(0.0).sum())

        p5_mean = float(pcts.mean()) if len(pcts) > 0 else 0.0
        p20_mean = p5_mean * 4.0  # 简单估算（无 20 日列时退化为 4 倍 5 日）

        state = classify_lifecycle(p5_mean, p20_mean, n_lim)
        bonus, label = compute_lifecycle_bonus(state)

        out[ind] = {
            "state": state,
            "score_bonus": bonus,
            "label": label,
            "n_limit_up": n_lim,
            "pct_5d_mean": p5_mean,
            "pct_20d_mean": p20_mean,
            "n_stocks": len(group),
        }

    return out


def get_industry_lifecycle(db_conn, industry: str, lookback_days: int = 30) -> Dict[str, Any]:
    """
    从 daily_data + dim_security 实算某行业的 5 日涨幅 + 涨停数。

    数据源：
      - daily_data (ts_code, trade_date, close, pct_chg)
      - dim_security (ts_code, industry)

    返回：{state, score_bonus, label, n_limit_up, pct_5d_mean, ...}
    """
    if db_conn is None or not industry:
        return {"state": "chaos", "score_bonus": 0.0, "label": "无行业"}

    try:
        df = db_conn.execute(
            """
            SELECT d.ts_code, d.trade_date, d.close, d.pct_chg,
                   d.high, d.low
            FROM daily_data d
            JOIN dim_security s ON d.ts_code = s.ts_code
            WHERE s.industry = ?
              AND d.trade_date >= CURRENT_DATE - INTERVAL '%d days'
            ORDER BY d.ts_code, d.trade_date
            """ % lookback_days,
            [str(industry)],
        ).df()
    except Exception as e:
        logger.debug("[lifecycle] 查询失败 %s: %s", industry, e)
        return {"state": "chaos", "score_bonus": 0.0, "label": "查询失败"}

    if df.empty:
        return {"state": "chaos", "score_bonus": 0.0, "label": "无数据"}

    # 计算每只股票近 5 日累计涨幅
    per_stock = []
    n_lim_total = 0
    for code, grp in df.groupby("ts_code"):
        grp = grp.sort_values("trade_date")
        if len(grp) < 2:
            continue
        last_close = float(grp["close"].iloc[-1])
        first_close_5d = float(grp["close"].iloc[-min(5, len(grp))])
        if first_close_5d <= 0:
            continue
        pct_5d = (last_close / first_close_5d - 1.0) * 100.0
        # 涨停（涨跌幅 ≥ 9.5%）
        is_lim = (pd.to_numeric(grp["pct_chg"], errors="coerce").fillna(0.0) >= 9.5)
        n_lim_total += int(is_lim.sum())
        per_stock.append(pct_5d)

    if not per_stock:
        return {"state": "chaos", "score_bonus": 0.0, "label": "样本不足"}

    p5_mean = float(sum(per_stock) / len(per_stock))
    p20_mean = p5_mean * 4.0
    state = classify_lifecycle(p5_mean, p20_mean, n_lim_total)
    bonus, label = compute_lifecycle_bonus(state)
    return {
        "state": state,
        "score_bonus": bonus,
        "label": label,
        "n_limit_up": n_lim_total,
        "pct_5d_mean": p5_mean,
        "pct_20d_mean": p20_mean,
        "n_stocks": len(per_stock),
    }
