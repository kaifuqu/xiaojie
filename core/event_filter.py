# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V28 — 特殊事件拦截（避雷）
================================================================================
【V28 新增 B 项】A 股必跌事件防御层，零数据依赖版本：

  ① 次新股：上市 < 30 日 → 踢出主池（波动过大）
  ② ST / 退市风险：名称含 ST / 退 / B股 → 踢出
  ③ 连续跌停：近 3 日有 2 日跌 ≤ -9.5% → 跳过
  ④ 高位派发：bias_20 > 15% + 连续 3 日放量 → -15
  ⑤ 财报披露窗：3/31 / 6/30 / 9/30 / 12/31 前 3 日 → -3

数据源：纯 df + rt，不依赖 Tushare share_float / anns_d 接口
（接口接入需要积分，不在本次范围）

集成：
  - core/pool_manager._process_single_stock_for_p1 前置过滤
  - core/strategies/strat_p3_intraday / strat_p4_tail 打分加权
================================================================================
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


ST_KEYWORDS = ("ST", "退", "B股")


def is_new_stock(ts_code: str, list_days: int = 30, list_date: Optional[str] = None) -> bool:
    """是否次新股（上市 < list_days）。"""
    if not list_date:
        return False
    try:
        ld_str = str(list_date)[:10].replace("-", "")
        ld = datetime.strptime(ld_str, "%Y%m%d")
    except Exception:
        return False
    return (datetime.now() - ld).days < int(list_days)


def is_st_or_delist(name: str) -> bool:
    """名称含 ST / *ST / 退 / B股"""
    if not isinstance(name, str):
        return False
    upper = name.upper()
    for kw in ST_KEYWORDS:
        if kw in upper:
            return True
    if "ST" in upper:
        return True
    return False


def is_consecutive_limit_down(df, n_days: int = 3, threshold: float = -9.5) -> bool:
    """近 n_days 内有 ≥2 日跌幅 ≤ threshold (默认 -9.5%)"""
    if df is None or len(df) < n_days:
        return False
    try:
        recent = pd.to_numeric(df["pct_chg"].tail(n_days), errors="coerce").fillna(0.0).tolist()
    except Exception:
        return False
    return sum(1 for x in recent if x <= threshold) >= 2


def is_high_position_distribution(df, bias_threshold: float = 15.0, n_days: int = 3, vol_ratio_min: float = 1.3) -> bool:
    """bias_20 > 15% + 近 3 日均量比 ≥ 1.3"""
    if df is None or len(df) < n_days:
        return False
    try:
        bias20 = float(df["bias_20"].iloc[-1]) if "bias_20" in df.columns else 0.0
    except Exception:
        bias20 = 0.0
    if bias20 <= bias_threshold:
        return False
    try:
        vr_recent = pd.to_numeric(df["vol_ratio"].tail(n_days), errors="coerce").fillna(0.0)
        return float(vr_recent.mean()) >= vol_ratio_min
    except Exception:
        return False


FINANCE_REPORT_DATES = ("0331", "0630", "0930", "1231")


def near_finance_report_window(days_before: int = 3) -> bool:
    """当前日期是否在财报披露窗前 N 日内"""
    today = datetime.now()
    for d in FINANCE_REPORT_DATES:
        try:
            month = int(d[:2])
            day = int(d[2:])
            yr = today.year
            fr = datetime(yr, month, day)
            delta = (fr - today).days
            if 0 <= delta <= days_before:
                return True
        except Exception:
            continue
    return False


def compute_event_penalty(
    df,
    rt: Dict[str, Any],
    list_date: Optional[str] = None,
    ts_code: Optional[str] = None,
    name: Optional[str] = None,
) -> Tuple[float, str, bool]:
    """
    综合事件拦截。

    返回：(penalty_score, label, should_veto)
      - penalty_score: 负分（扣分）或正分（反向激励）
      - label: 文字描述
      - should_veto: True=踢出主池 / False=仅扣分
    """
    try:
        import pandas as pd  # 本地延迟 import
    except ImportError:
        pd = None

    _name = name or (rt.get("name") if isinstance(rt, dict) else "")
    penalty = 0.0
    tags: List[str] = []
    veto = False

    # ① 次新
    if is_new_stock(ts_code or "", list_date=list_date):
        veto = True
        tags.append("次新股")

    # ② ST
    if is_st_or_delist(str(_name)):
        veto = True
        tags.append("ST/退市风险")

    # ③ 连续跌停
    if is_consecutive_limit_down(df):
        veto = True
        tags.append("连续跌停")

    # ④ 高位派发
    if is_high_position_distribution(df):
        penalty -= 15.0
        tags.append("高位派发-15")

    # ⑤ 财报披露窗
    if near_finance_report_window(days_before=3):
        penalty -= 3.0
        tags.append("财报窗-3")

    label = "; ".join(tags) if tags else "无事件"
    return float(penalty), label, bool(veto)
