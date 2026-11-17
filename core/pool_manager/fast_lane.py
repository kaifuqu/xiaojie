# -*- coding: utf-8 -*-
"""
core/pool_manager/fast_lane.py
原 core/pool_manager.py L2453-L2756（Momentum Fast-Lane 直通车）

【职责】
- MOMENTUM_FAST_LANE_MAX_PROBE / MAX_PICK / TOP_SECTORS / MFL_* 常量
- _ts_to_s_code / _mfl_rt_metrics
- compute_momentum_fast_lane_probe_codes：构造「非底仓」探测列表
- select_momentum_fast_lane_ts_codes：仅基于已拉取的 rt_map 判定极端强势
- build_momentum_fast_lane_base_items：对直通车代码逐只拉 QFQ 日线
- extend_fast_lane_with_limit_streak_hits：连板高度补漏
- merge_scan_base_with_fast_lane_items：底仓顺序不变，尾部追加直通车

【依赖】
- core.pool_manager.thresholds（_safe_float）
- data.db_core（容错导入 get_p1_candidate_codes / get_stock_data_qfq）
- core.indicator_calc（容错导入 precompute_indicators）
"""
import logging
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

import numpy as np

from core.pool_manager.thresholds import _safe_float

logger = logging.getLogger(__name__)

# =============================================================================
# P3/P4 右侧直通车（Momentum Fast-Lane）
# =============================================================================
# 业务：P1 静态形态（如 bias_20、贴 MA20）偏左侧，易把「极端强势龙头」挡在底仓外；
# 扫描引擎仅遍历底仓 base_items，导致 P3/P4 永远看不到这类票。
#
# 策略：在 run_scan_engine 内、批量 fetch_realtime 之前，对「非底仓」增量探测一批代码的实时快照，
# 用纯行情字段筛出极端强势标的，再按代码逐只 get_stock_data_qfq（与现有单票扫描一致），
# 合并入本轮扫描列表。全程不对 daily_data 做额外全表扫描；候选列表复用 get_p1_candidate_codes()
# 或 stock_basic 行业映射（与 P1 洗盘同源的一次性查询语义，非逐行扫表）。
#
# 常量可按实盘微调；阈值故意偏「少而精」，避免直通车淹没主池。
# =============================================================================

MOMENTUM_FAST_LANE_MAX_PROBE = 1500
MOMENTUM_FAST_LANE_MAX_PICK = 56
MOMENTUM_FAST_LANE_TOP_SECTORS = 14
# 日内近似涨停/强势区（主板口径；创业板科创板可由高 pct 自然落入）
MFL_PCT_LIMIT_TOUCH = 8.8
MFL_PCT_STRONG = 6.2
MFL_PCT_AMOUNT_TIER = 5.0
MFL_CLOSE_IN_RANGE_MIN = 0.82
MFL_OPEN_SURGE_PCT = 4.8
MFL_OPEN_SURGE_NEED_DAY_PCT = 2.8
# 连板：在日线 hist 上 limit_times>=2 且当日 rt 强势时直接入选（右侧接力）
MFL_LIMIT_TIMES_MIN = 2
MFL_LIMIT_TIMES_MIN_PCT = 4.5


def _ts_to_s_code(ts_code: str) -> str:
    return str(ts_code or "").split(".")[0][:6]


def compute_momentum_fast_lane_probe_codes(
    base_codes: List[str],
    sorted_sector_names: List[str],
    max_probe: int = MOMENTUM_FAST_LANE_MAX_PROBE,
) -> List[str]:
    """构造「非底仓」探测列表：优先实时涨幅榜相关板块内的股票，控制长度上限。
    不使用 SELECT * FROM daily_data 类全表扫描。"""
    try:
        from data.db_core import get_p1_candidate_codes, get_all_basic_industry
    except Exception as e:
        logger.debug("[mfl-import] %s", e)
        return []

    candidates = get_p1_candidate_codes() or []
    if not candidates:
        candidates = list((get_all_basic_industry() or {}).keys())
    if not candidates:
        return []

    base_set: Set[str] = {str(c).strip() for c in (base_codes or []) if c}
    hot_inds: Set[str] = set()
    for sec in (sorted_sector_names or [])[: max(1, MOMENTUM_FAST_LANE_TOP_SECTORS)]:
        if sec:
            hot_inds.add(str(sec))

    ind_map = get_all_basic_industry() or {}
    non_base: List[str] = []
    for c in candidates:
        cs = str(c).strip()
        if not cs or cs.upper().endswith(".BJ"):
            continue
        if cs in base_set:
            continue
        non_base.append(cs)

    priority: List[str] = []
    rest: List[str] = []
    if hot_inds:
        for c in non_base:
            if ind_map.get(c) in hot_inds:
                priority.append(c)
            else:
                rest.append(c)
    else:
        rest = list(non_base)

    ordered = priority + rest
    seen: Set[str] = set()
    out: List[str] = []
    for c in ordered:
        if c in seen:
            continue
        seen.add(c)
        out.append(c)
        if len(out) >= int(max_probe):
            break
    return out


def _mfl_rt_metrics(rt: Dict[str, Any]) -> Optional[Tuple[float, float, float, float, float]]:
    """返回 (pct_day, amount, pos_in_range, pct_from_open, pre_close) 或 None。"""
    if not isinstance(rt, dict):
        return None
    price = _safe_float(rt.get("price"), 0.0)
    pre = _safe_float(rt.get("pre_close"), 0.0)
    open_p = _safe_float(rt.get("open"), 0.0)
    high = _safe_float(rt.get("high"), 0.0)
    low = _safe_float(rt.get("low"), 0.0)
    amount = _safe_float(rt.get("amount"), 0.0)
    if price <= 0 or pre <= 0:
        return None
    pct_day = (price - pre) / pre * 100.0
    hi_lo = high - low
    if hi_lo > 1e-9:
        pos_in_range = (price - low) / hi_lo
    else:
        pos_in_range = 1.0 if pct_day >= 9.0 else 0.5
    if open_p > 0:
        pct_from_open = (price - open_p) / open_p * 100.0
    else:
        pct_from_open = 0.0
    return float(pct_day), float(amount), float(pos_in_range), float(pct_from_open), float(pre)


def select_momentum_fast_lane_ts_codes(
    probe_ts_codes: List[str],
    rt_map: Dict[str, Any],
    base_code_set: FrozenSet[str],
    max_picks: int = MOMENTUM_FAST_LANE_MAX_PICK,
) -> List[str]:
    """仅基于已拉取的 rt_map（探测批次）判定极端强势；从 probe 中选出最多 max_picks 只。"""
    if not probe_ts_codes or not isinstance(rt_map, dict):
        return []

    rows: List[Tuple[str, float, float, float, float, float]] = []
    for ts in probe_ts_codes:
        ts = str(ts).strip()
        if not ts or ts in base_code_set:
            continue
        sc = _ts_to_s_code(ts)
        rt = rt_map.get(sc)
        m = _mfl_rt_metrics(rt if isinstance(rt, dict) else {})
        if m is None:
            continue
        pct_day, amount, pos_in_range, pct_from_open, _pre = m
        rows.append((ts, pct_day, amount, pos_in_range, pct_from_open, 0.0))

    if not rows:
        return []

    amounts_sorted = sorted([r[2] for r in rows], reverse=True)
    amt_thr = 0.0
    if amounts_sorted:
        idx = max(0, min(len(amounts_sorted) - 1, int(len(amounts_sorted) * 0.11)))
        amt_thr = float(amounts_sorted[idx])

    scored: List[Tuple[str, float]] = []
    for ts, pct_day, amount, pos_in_range, pct_from_open, _ in rows:
        hit = False
        if pct_day >= MFL_PCT_LIMIT_TOUCH:
            hit = True
        elif pct_day >= MFL_PCT_STRONG and pos_in_range >= MFL_CLOSE_IN_RANGE_MIN:
            hit = True
        elif pct_from_open >= MFL_OPEN_SURGE_PCT and pct_day >= MFL_OPEN_SURGE_NEED_DAY_PCT:
            hit = True
        elif pct_day >= MFL_PCT_AMOUNT_TIER and amount >= amt_thr and amt_thr > 0:
            hit = True
        elif pct_day >= (MFL_LIMIT_TIMES_MIN_PCT + 1.2) and pos_in_range >= 0.78:
            hit = True
        if not hit:
            continue
        scr = float(pct_day) * (1.0 + np.log1p(max(amount, 0.0) / 1e6))
        scored.append((ts, scr))

    scored.sort(key=lambda x: x[1], reverse=True)
    out: List[str] = []
    seen: Set[str] = set()
    for ts, _ in scored:
        if ts in seen:
            continue
        seen.add(ts)
        out.append(ts)
        if len(out) >= int(max_picks):
            break
    return out


def build_momentum_fast_lane_base_items(ts_codes: List[str]) -> List[Dict[str, Any]]:
    """对直通车代码逐只拉 QFQ 日线并 precompute_indicators，构造与底仓项同形的 dict。"""
    if not ts_codes:
        return []
    try:
        from data.db_core import get_stock_data_qfq
        from core.indicator_calc import precompute_indicators
    except Exception as e:
        logger.warning("[mfl-import] %s", e)
        return []

    out: List[Dict[str, Any]] = []
    for ts_code in ts_codes:
        ts_code = str(ts_code).strip()
        if not ts_code:
            continue
        try:
            df = get_stock_data_qfq(ts_code, limit=120)
            if df is None or getattr(df, "empty", True):
                continue
            df = precompute_indicators(df)
            hist = df.iloc[-1].to_dict()
            lt = int(_safe_float(hist.get("limit_times", 0), 0.0))
            item: Dict[str, Any] = {
                "code": ts_code,
                "p1_score": 72.0,
                "df": df,
                "hist": hist,
                "_momentum_fast_lane": True,
            }
            item["_momentum_fast_lane_limit_times"] = lt
            out.append(item)
        except Exception as e:
            logger.debug("[mfl-build] %s: %s", ts_code, e)
    return out


def extend_fast_lane_with_limit_streak_hits(
    probe_ts_codes: List[str],
    rt_map: Dict[str, Any],
    base_code_set: FrozenSet[str],
    already_picked: FrozenSet[str],
    max_add: int = 12,
) -> List[str]:
    """对「rt 未过动量阈值」的探测代码，仅按代码逐只查日线末行 limit_times（单票 O(1) 窗口），
    将连板高度达标的代码加入直通车名单；上限 max_add，避免额外 I/O 膨胀。"""
    if max_add <= 0 or not probe_ts_codes:
        return []
    try:
        from data.db_core import get_stock_data_qfq
    except Exception:
        return []

    extra: List[str] = []
    for ts in probe_ts_codes:
        if len(extra) >= max_add:
            break
        ts = str(ts).strip()
        if not ts or ts in base_code_set or ts in already_picked:
            continue
        sc = _ts_to_s_code(ts)
        rt = rt_map.get(sc)
        m = _mfl_rt_metrics(rt if isinstance(rt, dict) else {})
        if m is None:
            continue
        pct_day, _amount, _pr, _po, _pre = m
        if pct_day < MFL_LIMIT_TIMES_MIN_PCT:
            continue
        try:
            df = get_stock_data_qfq(ts, limit=30)
            if df is None or getattr(df, "empty", True):
                continue
            if "limit_times" not in df.columns:
                continue
            lt = int(_safe_float(df["limit_times"].iloc[-1], 0.0))
            if lt >= MFL_LIMIT_TIMES_MIN:
                extra.append(ts)
        except Exception:
            continue
    return extra


def merge_scan_base_with_fast_lane_items(
    base_items: List[Dict[str, Any]],
    fast_lane_items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """底仓顺序不变，尾部追加直通车（按 code 去重）。"""
    seen: Set[str] = set()
    out: List[Dict[str, Any]] = []
    for it in base_items or []:
        if not isinstance(it, dict):
            continue
        c = str(it.get("code", "")).strip()
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(it)
    for it in fast_lane_items or []:
        if not isinstance(it, dict):
            continue
        c = str(it.get("code", "")).strip()
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(it)
    return out


__all__ = [
    "MOMENTUM_FAST_LANE_MAX_PROBE", "MOMENTUM_FAST_LANE_MAX_PICK", "MOMENTUM_FAST_LANE_TOP_SECTORS",
    "MFL_PCT_LIMIT_TOUCH", "MFL_PCT_STRONG", "MFL_PCT_AMOUNT_TIER",
    "MFL_CLOSE_IN_RANGE_MIN", "MFL_OPEN_SURGE_PCT", "MFL_OPEN_SURGE_NEED_DAY_PCT",
    "MFL_LIMIT_TIMES_MIN", "MFL_LIMIT_TIMES_MIN_PCT",
    "_ts_to_s_code", "compute_momentum_fast_lane_probe_codes",
    "_mfl_rt_metrics", "select_momentum_fast_lane_ts_codes",
    "build_momentum_fast_lane_base_items",
    "extend_fast_lane_with_limit_streak_hits",
    "merge_scan_base_with_fast_lane_items",
]