# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V27 — P1 ↔ P2-P5 跨池接力指数
================================================================================
【V27 新增·A1】记录每只股票在 P2/P3/P4/P5 最近 N 个交易日的命中情况，
回填到 P1 打分最后一步的"接力分"。

接力分：
  - P2+P3+P4+P5 命中 2 池 → +3
  - 命中 3 池                  → +5
  - 命中 4 池                  → +8

数据来源：signal_log（由 data/signal_logger 写入）
集成位置：core/pool_manager._p1_final_sort_key 末尾加分
================================================================================
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

import pandas as pd

logger = logging.getLogger(__name__)

BJ_TZ = timezone(timedelta(hours=8))

RELAY_LOOKBACK_DAYS = 10  # 看最近 10 个交易日
RELAY_BONUS = {2: 3.0, 3: 5.0, 4: 8.0}


def compute_pool_relay_score(
    db_conn,
    ts_code: str,
    as_of_date: Optional[str] = None,
) -> tuple:
    """
    计算某只股票的跨池接力分。

    返回：(bonus: float, hit_pools: list, label: str)
      - hit_pools: ['p2','p3',...] 最近 N 日命中过的池
    """
    if db_conn is None or not ts_code:
        return 0.0, [], ""

    as_of = as_of_date or datetime.now(BJ_TZ).strftime("%Y%m%d")
    as_of_dt = pd.to_datetime(as_of)
    start_dt = (as_of_dt - pd.tseries.offsets.BDay(RELAY_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    as_of_iso = pd.to_datetime(as_of).strftime("%Y-%m-%d")

    try:
        df = db_conn.execute(
            """
            SELECT pool, MAX(push_time) AS last_push
            FROM signal_log
            WHERE ts_code = ?
              AND pool IN ('p2','p3','p4','p5')
              AND log_date >= CAST(? AS DATE)
              AND log_date <= CAST(? AS DATE)
            GROUP BY pool
            """,
            [ts_code, start_dt, as_of_iso],
        ).df()
    except Exception as e:
        logger.debug("[relay] 读取失败 %s: %s", ts_code, e)
        return 0.0, [], ""

    if df.empty:
        return 0.0, [], ""

    hit_pools = sorted(set(df["pool"].astype(str).tolist()))
    n = len(hit_pools)
    bonus = float(RELAY_BONUS.get(n, 0.0))
    label = f"接力{n}池+{bonus:.0f}" if bonus > 0 else ""
    return bonus, hit_pools, label


def batch_compute_pool_relay_score(
    db_conn,
    ts_codes: List[str],
    as_of_date: Optional[str] = None,
) -> Dict[str, tuple]:
    """
    批量计算多只股票的接力分；返回 {ts_code: (bonus, hit_pools, label)}
    """
    out: Dict[str, tuple] = {}
    for c in ts_codes:
        try:
            out[c] = compute_pool_relay_score(db_conn, c, as_of_date)
        except Exception as e:
            logger.debug("[relay] %s: %s", c, e)
            out[c] = (0.0, [], "")
    return out
