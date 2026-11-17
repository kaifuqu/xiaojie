# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V27 — 推送时 signal_log 写入适配器
================================================================================
【V27 新增】统一封装：5 个池在推送完成后调用 safe_log_pool_signal(pool, items, db_conn)，
无需关心表结构 / 时区 / 异常处理；表不存在则自动创建，连接为 None 则跳过。

集成示例（strat_pX_*.py run_all 末尾）：
    from core.pool_signal_adapter import safe_log_pool_signal
    safe_log_pool_signal("p1", items_list, db_conn)

items 每条 dict 至少含 ts_code, name, score, strategies（list 或逗号分隔字符串）
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def safe_log_pool_signal(
    pool: str,
    items: List[Dict[str, Any]],
    db_conn=None,
    *,
    push_price_lookup=None,
    force_observation: bool = False,
    trade_date: Optional[str] = None,
) -> int:
    """
    安全写入 signal_log；任何异常都被吞掉（不阻塞推送主流程）。
    返回：成功写入条数（异常或无数据返回 0）
    """
    if not items:
        return 0
    if db_conn is None:
        return 0
    try:
        from data.signal_logger import log_signal
        return int(
            log_signal(
                pool=pool,
                items=items,
                db_conn=db_conn,
                push_price_lookup=push_price_lookup,
                force_observation=force_observation,
                # 【V30.4 2026-10-18 P0-F4】此前 log_signal 的 trade_date 参数从未被转发，
                # 导致调用方（scan pipeline）传入的业务日被丢弃，log_signal 静默回退墙钟日期。
                # 周末补跑 P5 时信号被打到补跑当天（2026-10-17 周六出现 9 条 P5 信号），
                # T+N 回看 / 战法有效性 / 模拟盘建仓日全部错位。
                trade_date=trade_date,
            )
        )
    except Exception as e:
        logger.debug("[safe_log_pool_signal] %s 写入失败: %s", pool, e)
        return 0


def build_items_from_hits(
    hit_rows: List[Dict[str, Any]],
    *,
    pool: str,
    default_strategies_key: str = "strategies",
) -> List[Dict[str, Any]]:
    """
    把各池 run_all 返回的 hit_rows 统一转为 log_signal items。
    容忍字段缺失；strategies 接受 list / 字符串。
    """
    out: List[Dict[str, Any]] = []
    for row in hit_rows:
        if not isinstance(row, dict):
            continue
        ts_code = str(row.get("ts_code") or row.get("code") or "").strip()
        if not ts_code:
            continue
        strategies = row.get(default_strategies_key) or []
        if isinstance(strategies, list):
            pass
        elif isinstance(strategies, str):
            strategies = [s.strip() for s in strategies.replace("|", ",").split(",") if s.strip()]
        else:
            strategies = []

        out.append({
            "ts_code": ts_code,
            "name": str(row.get("name") or ""),
            "score": row.get("score") or row.get("burst_score") or 0.0,
            "burst_score": row.get("burst_score") or row.get("score") or 0.0,
            "strategies": strategies,
            "price": row.get("price") or row.get("now_price") or row.get("push_price") or 0.0,
            "pre_close": row.get("pre_close") or 0.0,
            "rank": row.get("rank") or row.get("rank_in_pool") or 0,
            "observation": bool(row.get("observation") or row.get("is_observation") or False),
            "pushed": bool(row.get("pushed", True)),
            "remark": str(row.get("remark") or ""),
        })
    return out
