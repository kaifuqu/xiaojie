# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V27 — P1 涨停炸板前置拦截
================================================================================
【V27 新增·P1-B】用 HiThink special_data_limit_break_pool 把昨日炸板的票
在 P1 候选生成阶段直接降权（或踢到观察池）。

数据来源：
- core/strategies/_hithink_limit_up_context.py 的 load_hithink_limit_context
  已含 limit_burst_pool 字段，本模块直接复用。

集成位置：
- core/pool_manager.py 候选生成入口
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _load_limit_burst_set() -> set:
    """
    安全加载昨日炸板池；失败返回空集（不阻塞主流程）。
    """
    try:
        from core.strategies._hithink_limit_up_context import load_hithink_limit_context, _tick_key
        ctx = load_hithink_limit_context() or {}
        # 【P1-B 修复】真实键名是 all_burst_tickers（纯数字 ticker 集合），
        # 原代码读 limit_burst_pool 恒为空 dict → 炸板拦截整条链路从不生效。
        burst = ctx.get("all_burst_tickers") or set()
        codes = set()
        for k in burst:
            codes.add(_tick_key(k))
        return codes
    except Exception as e:
        logger.debug("[P1-B] 涨停炸板池加载失败，跳过拦截: %s", e)
        return set()


def prefilter_yesterday_limit_break(
    stock_data_list: List[Dict[str, Any]],
    *,
    penalty_score: float = 30.0,
    drop_to_observation: bool = False,
) -> List[Dict[str, Any]]:
    """
    涨停炸板前置拦截：
      - penalty_score: 降权分数（默认 30）
      - drop_to_observation: True=直接踢到观察池

    返回：处理后的 stock_data_list（同一对象 in-place 修改 + 复制）
    """
    if not stock_data_list:
        return stock_data_list

    burst_set = _load_limit_burst_set()
    if not burst_set:
        return stock_data_list

    out: List[Dict[str, Any]] = []
    hit_count = 0
    for item in stock_data_list:
        raw_code = str(item.get("ts_code") or item.get("code") or "").strip()
        # 【P1-B 修复】burst_set 存的是纯 6 位数字（_tick_key 去掉了 .SH/.SZ），
        # 这里必须同样归一化，否则 '600519.SH' 永远匹配不上 '600519'。
        ts_code = raw_code.split(".")[0] if "." in raw_code else raw_code
        if ts_code and ts_code in burst_set:
            hit_count += 1
            cur_penalty = float(item.get("_p1_penalty", 0.0) or 0.0)
            item["_p1_penalty"] = cur_penalty + float(penalty_score)
            item["_p1_limit_break_yesterday"] = True
            if drop_to_observation:
                item["_p1_drop_to_observation"] = True
        out.append(item)

    if hit_count:
        logger.info(
            "[P1-B] 涨停炸板前置拦截命中 %d 只，降权 %.1f 分（drop_to_observation=%s）",
            hit_count, penalty_score, drop_to_observation,
        )

    return out
