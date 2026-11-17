# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88+9-22 — HiThink 涨停板/炸板实时上下文模块

【功能】
盘中实时拉取 A 股涨停池 + 炸板池，按 ticker 索引，
供 P2 竞价池 + P3 盘中池注入 rt["_hithink_limit_up"] / rt["_hithink_limit_burst"]。

【数据源】
- HiThink special_data_limit_up_pool    → 涨停板池（含封单、连板数、涨停原因）
- HiThink special_data_limit_break_pool → 炸板池（含开板次数、换手率）

【注入时机】
- auto_sniper_daemon.run_p3_tick / run_p3_patrol 前：一次性加载 → 注入每只股票 rt
- 缓存 TTL：30 秒（盘中行情高频扫描时避免重复拉接口）

【rt 注入格式】
  rt["_hithink_limit_up"] = {
      "continue_day_cnt": int,   # 连板数（1=首板）
      "seal_money": float,      # 封单金额（元）
      "max_seal_money": float,  # 最大封单金额（元）
      "limit_up_reason": str,   # 涨停原因
      "is_st": bool,
      "is_new": bool,           # 次新股
      "limit_up_time": str,    # 封板时间（HH:MM）
  }
  rt["_hithink_limit_burst"] = {
      "open_times": int,        # 开板次数
      "turnover_ratio_pct": float,  # 换手率%
      "turnover": float,        # 成交额（元）
  }
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 缓存 TTL（秒）：避免同一次扫描内重复请求
_LIMIT_CACHE_TTL_SEC = 30.0

# 缓存结构
_limit_cache: Dict[str, Any] = {
    "ts": 0.0,
    "limit_up_map": {},   # {ticker -> limit_up_record}
    "burst_map": {},     # {ticker -> burst_record}
    "all_limit_tickers": set(),
    "all_burst_tickers": set(),
}


def _tick_key(code_or_ts: str) -> str:
    """统一转换为纯数字 ticker 格式（如 '600000' / '000001'）。"""
    s = str(code_or_ts or "").strip()
    # 去掉 .SH/.SZ 后缀
    if "." in s:
        s = s.split(".")[0]
    return s.strip()


def _ticker_from_thscode(thscode: str) -> str:
    """HiThink thscode → 纯数字 ticker（如 '600000.SH' → '600000'）。"""
    return _tick_key(thscode)


def _to_frame(obj: Any) -> Optional[pd.DataFrame]:
    """把 HiThink special_data 返回值安全归一为 DataFrame。

    【V30.2 上线前修复】客户端在熔断 / 网络失败时可能返回 list 或 None，
    原代码直接访问 df.empty 抛 AttributeError: 'list' object has no attribute 'empty'。
    这里只做容器归一，不改任何业务字段与过滤逻辑。
    """
    if obj is None:
        return None
    if isinstance(obj, pd.DataFrame):
        return obj
    if isinstance(obj, pd.Series):
        return obj.to_frame().T
    if isinstance(obj, list):
        return pd.DataFrame(obj) if obj else pd.DataFrame()
    if isinstance(obj, dict):
        item = obj.get("item") if isinstance(obj.get("item"), list) else None
        if item is not None:
            return pd.DataFrame(item) if item else pd.DataFrame()
        return pd.DataFrame([obj])
    try:
        return pd.DataFrame(obj)
    except Exception as e:
        logger.debug("[HiThink 涨停上下文] 返回值无法归一为 DataFrame（已忽略）: %s", e)
        return None


def _hithink_date_ms_for_trade_day(trade_date: Optional[str] = None) -> Optional[int]:
    """【ST-P0-18】把交易日转成 HiThink 端点要的 date_ms（毫秒）。

    HiThink 的 limit-up-pool / limit-break-pool 是「实时池」：收盘后若不带 date_ms，
    服务端返回空列表（实测盘后不带日期 → 0 行；带日期 → 当日全部涨停股）。
    晚间 P5（20:05）扫描正是盘后，所以必须显式带日期，否则涨停上下文恒为空。
    """
    try:
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI

        if trade_date:
            _d = _dt.strptime(str(trade_date)[:10], "%Y-%m-%d")
        else:
            _d = _dt.now(_ZI("Asia/Shanghai"))
        return int(_d.replace(tzinfo=_ZI("Asia/Shanghai")).timestamp() * 1000)
    except Exception as e:
        logger.debug("[HiThink 涨停上下文] date_ms 计算失败: %s", e)
        return None


def _fetch_limit_up_pool(client, page_size: int = 200) -> Dict[str, Dict[str, Any]]:
    """
    拉取完整涨停池（多页），返回 {ticker -> record}。

    【ST-P0-18 修复】必须带 date_ms，否则盘后恒空（详见 _hithink_date_ms_for_trade_day）。
    """
    result: Dict[str, Dict[str, Any]] = {}
    page = 1
    max_pages = 5  # 最多 5 页 × 200 = 1000 条，覆盖全天涨停
    _date_ms = _hithink_date_ms_for_trade_day()
    while page <= max_pages:
        df = _to_frame(client.special_data_limit_up_pool(
            page=page, size=page_size, sort_field="limit_up_time", sort_dir="desc",
            **({"date_ms": _date_ms} if _date_ms else {}),
        ))
        if df is None or df.empty:
            break
        for _, row in df.iterrows():
            t = _ticker_from_thscode(str(row.get("thscode", "")))
            if not t:
                continue
            try:
                seal_money = float(row.get("seal_money") or 0) or 0.0
            except (TypeError, ValueError):
                seal_money = 0.0
            try:
                max_seal = float(row.get("max_seal_money") or 0) or 0.0
            except (TypeError, ValueError):
                max_seal = 0.0
            try:
                cnt = int(float(row.get("continue_day_cnt") or 1) or 1)
            except (TypeError, ValueError):
                cnt = 1
            result[t] = {
                "thscode": str(row.get("thscode", "")),
                "name": str(row.get("name", "")),
                "continue_day_cnt": cnt,
                "continue_day_text": str(row.get("continue_day_text", "")),
                "seal_money": seal_money,
                "max_seal_money": max_seal,
                "limit_up_reason": str(row.get("limit_up_reason", "")),
                "is_st": bool(row.get("is_st", False)),
                "is_new": bool(row.get("is_new", False)),
                "limit_up_time": str(row.get("limit_up_time", "")),
                "pct_today": float(row.get("price_change_ratio_pct") or 0.0),
            }
        if len(df) < page_size:
            break
        page += 1
    logger.debug("[HiThink 涨停池] 共拉取 %d 只涨停股", len(result))
    return result


def _fetch_limit_break_pool(client, page_size: int = 200) -> Dict[str, Dict[str, Any]]:
    """
    拉取炸板池（多页），返回 {ticker -> record}。

    【ST-P0-18 修复】同样必须带 date_ms，否则盘后恒空。
    """
    result: Dict[str, Dict[str, Any]] = {}
    page = 1
    max_pages = 3
    _date_ms = _hithink_date_ms_for_trade_day()
    while page <= max_pages:
        df = _to_frame(client.special_data_limit_break_pool(
            page=page, size=page_size,
            **({"date_ms": _date_ms} if _date_ms else {}),
        ))
        if df is None or df.empty:
            break
        for _, row in df.iterrows():
            t = _ticker_from_thscode(str(row.get("thscode", "")))
            if not t:
                continue
            try:
                open_times = int(float(row.get("open_times") or 0) or 0)
            except (TypeError, ValueError):
                open_times = 0
            try:
                turnover_ratio = float(row.get("turnover_ratio_pct") or 0.0) or 0.0
            except (TypeError, ValueError):
                turnover_ratio = 0.0
            try:
                turnover = float(row.get("turnover") or 0.0) or 0.0
            except (TypeError, ValueError):
                turnover = 0.0
            result[t] = {
                "thscode": str(row.get("thscode", "")),
                "name": str(row.get("name", "")),
                "open_times": open_times,
                "turnover_ratio_pct": turnover_ratio,
                "turnover": turnover,
                "pct_today": float(row.get("price_change_ratio_pct") or 0.0),
            }
        if len(df) < page_size:
            break
        page += 1
    logger.debug("[HiThink 炸板池] 共拉取 %d 只炸板股", len(result))
    return result


def load_hithink_limit_context() -> Dict[str, Any]:
    """
    主入口：一次性加载涨停池 + 炸板池，缓存在模块全局。
    同一次扫描内（TTL 内）不会重复拉接口。

    返回:
        {
            "enabled": bool,
            "limit_up_map": {ticker -> limit_up_record},
            "burst_map": {ticker -> burst_record},
            "all_limit_tickers": set,
            "all_burst_tickers": set,
        }
    """
    global _limit_cache

    now = time.time()
    if now - _limit_cache["ts"] < _LIMIT_CACHE_TTL_SEC and _limit_cache["ts"] > 0:
        logger.debug("[HiThink 涨停上下文] 使用缓存（%.0f 秒前）", now - _limit_cache["ts"])
        return {
            "enabled": True,
            "limit_up_map": _limit_cache["limit_up_map"],
            "burst_map": _limit_cache["burst_map"],
            "all_limit_tickers": _limit_cache["all_limit_tickers"],
            "all_burst_tickers": _limit_cache["all_burst_tickers"],
        }

    # 初始化 HiThink 客户端
    try:
        from data.hithink_client import HithinkClient
        client = HithinkClient()
    except Exception as e:
        logger.debug("[HiThink 涨停上下文] 客户端初始化失败: %s", e)
        return {
            "enabled": False,
            "limit_up_map": {},
            "burst_map": {},
            "all_limit_tickers": set(),
            "all_burst_tickers": set(),
        }

    if not client.is_enabled:
        logger.debug("[HiThink 涨停上下文] HiThink 未启用")
        return {
            "enabled": False,
            "limit_up_map": {},
            "burst_map": {},
            "all_limit_tickers": set(),
            "all_burst_tickers": set(),
        }

    # 涨停池 + 炸板池并行拉取（尽量减少延迟）
    import concurrent.futures

    limit_map: Dict[str, Dict[str, Any]] = {}
    burst_map: Dict[str, Dict[str, Any]] = {}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f_limit = executor.submit(_fetch_limit_up_pool, client)
        f_burst = executor.submit(_fetch_limit_break_pool, client)
        try:
            limit_map = f_limit.result(timeout=10.0)
        except Exception as e:
            logger.debug("[HiThink 涨停上下文] 涨停池拉取失败: %s", e)
        try:
            burst_map = f_burst.result(timeout=10.0)
        except Exception as e:
            logger.debug("[HiThink 涨停上下文] 炸板池拉取失败: %s", e)

    _limit_cache = {
        "ts": now,
        "limit_up_map": limit_map,
        "burst_map": burst_map,
        "all_limit_tickers": set(limit_map.keys()),
        "all_burst_tickers": set(burst_map.keys()),
    }
    logger.info(
        "[HiThink 涨停上下文] 加载完成: 涨停 %d 只, 炸板 %d 只",
        len(limit_map), len(burst_map),
    )
    return {
        "enabled": True,
        "limit_up_map": limit_map,
        "burst_map": burst_map,
        "all_limit_tickers": set(limit_map.keys()),
        "all_burst_tickers": set(burst_map.keys()),
    }


def inject_limit_context_to_base_items(
    base: list,
    limit_context: Optional[Dict[str, Any]] = None,
) -> None:
    """
    将涨停/炸板上下文注入 base items 的 rt 字典。

    Args:
        base: load_base_items_latest() 返回的 items 列表（每个含 code/name/rt）
        limit_context: 可选；若不传则自动调用 load_hithink_limit_context() 加载

    【ST-P0-15】调用后可用 `inject_limit_context_to_base_items.last_injected` /
    `.last_total` 读取真实命中数（供日志/告警使用）。
    """
    inject_limit_context_to_base_items.last_injected = 0  # type: ignore[attr-defined]
    inject_limit_context_to_base_items.last_total = len(base)  # type: ignore[attr-defined]
    if limit_context is None:
        limit_context = load_hithink_limit_context()

    if not limit_context.get("enabled"):
        return

    lu_map = limit_context.get("limit_up_map") or {}
    lb_map = limit_context.get("burst_map") or {}

    injected = 0
    for item in base:
        code = _tick_key(item.get("code", ""))
        rt = item.setdefault("rt", {})
        # 涨停信号
        if code in lu_map:
            rt["_hithink_limit_up"] = lu_map[code]
            injected += 1
        # 炸板信号（即使已在涨停池也注入，方便判断）
        if code in lb_map:
            rt["_hithink_limit_burst"] = lb_map[code]

    # 【ST-P0-15】把真实命中数回写到函数属性，供调用方取用（避免改返回值破坏
    # 既有的 None 返回契约）。daemon 的「涨停池命中: %d/%d」曾把 len(base) 传两遍
    # → 永远 100%，掩盖了涨停池整体失效的事实。
    inject_limit_context_to_base_items.last_injected = injected  # type: ignore[attr-defined]
    inject_limit_context_to_base_items.last_total = len(base)  # type: ignore[attr-defined]
    logger.info("[HiThink 涨停上下文] 注入完成: %d/%d 只命中涨停池", injected, len(base))


def is_in_limit_up_pool(ticker: str, limit_context: Optional[Dict[str, Any]] = None) -> bool:
    """判断股票是否在涨停池。"""
    if limit_context is None:
        limit_context = load_hithink_limit_context()
    # 【V30.2 上线前加固】limit_context 允许 None / list / str / 任意脏返回，
    # 之前直接 .get() 会在非 dict 时抛 AttributeError 并冒泡到 P2/P3 主链。
    if not isinstance(limit_context, dict):
        return False
    return _tick_key(ticker) in (limit_context.get("all_limit_tickers") or set())


def is_in_limit_burst_pool(ticker: str, limit_context: Optional[Dict[str, Any]] = None) -> bool:
    """判断股票是否在炸板池。"""
    if limit_context is None:
        limit_context = load_hithink_limit_context()
    if not isinstance(limit_context, dict):
        return False
    return _tick_key(ticker) in (limit_context.get("all_burst_tickers") or set())
