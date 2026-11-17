# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88+ — 板块涨跌停阈值统一工具（V26.88+ 9-30）

【背景】朋友2指出 one_word_board_pct_min=9.5 不能区分板块：
- 主板（沪深 000/600）：±10%
- 创业板（300）：±20%（注册制）
- 科创板（688）：±20%（注册制）
- 北交所（830/836/870）：±30%（注册制）
- ST / *ST：仅主板 ±5%；创业板/科创板/北交所风险警示股仍按
  注册制 ±20% / ±20% / ±30%（2026-10-18 修正，见 _ST_BOARD_PREFIXES）

P2/P3/P4 各池内部都有固定 9.4/9.5% 的"一字板" / "昨日跌停" / "T+1 涨停风险"硬阈值，
本模块集中维护并提供 LRU 缓存的快速访问，避免重复 ts_code 字符串解析。

【使用】
    from core.strategies.board_limit_utils import get_limit_up_pct_fast, is_chinext_star

    pct = get_limit_up_pct_fast("300750.SZ")  # → 19.5
    pct = get_limit_up_pct_fast("600000.SH")  # → 9.5
    pct = get_limit_up_pct_fast("830xxx.BJ")  # → 29.5
    pct = get_limit_up_pct_fast("000001.SZ", "ST华联")  # → 4.5（深主板 ST）
    pct = get_limit_up_pct_fast("300750.SZ", "*ST宁德")  # → 19.5（创业板 ST 不降档）

【集成】
  - core/strategies/p2_auction_screener.py 内部 _BOARD_LIMIT_UP_CACHE（已迁移）
  - core/strategies/p3_intraday_screener.py 一字板 / T+1 涨停风险
  - core/strategies/p4_tail_screener.py 一字板 / 跌停拦截
"""
from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

logger = logging.getLogger(__name__)

# 默认阈值（与 constants.py BOARD_LIMIT_UP_PCT 对齐；本模块内独立维护避免 import 循环）
_DEFAULT_LIMIT_UP_PCT = 9.5
_ST_LIMIT_UP_PCT = 4.5
_CHINEXT_LIMIT_UP_PCT = 19.5
_STAR_LIMIT_UP_PCT = 19.5
_BJ_LIMIT_UP_PCT = 29.5


@lru_cache(maxsize=8192)
def _cached_limit_up_pct(ts_code: str, name: str) -> float:
    """带 LRU 缓存的板块涨停阈值计算；签名 (str, str) 可哈希。"""
    name_upper = str(name or "").upper()
    code = str(ts_code or "").upper()
    if code.endswith(".BJ"):
        return _BJ_LIMIT_UP_PCT
    if code.endswith(".SZ") and code[:6].startswith("30"):
        return _CHINEXT_LIMIT_UP_PCT
    if code.endswith(".SH") and code[:6].startswith("688"):
        return _STAR_LIMIT_UP_PCT
    # 仅主板 ST / *ST 降为 ±5%；注册制板块风险警示股不降档
    if "ST" in name_upper:
        return _ST_LIMIT_UP_PCT
    return _DEFAULT_LIMIT_UP_PCT


def get_limit_up_pct_fast(ts_code: Any, name: str = "") -> float:
    """
    获取该股票适用的涨停阈值（百分比，正数）。

    优先级：
    1. 北交所 .BJ：29.5%
    2. 创业板 30xxxx / 科创板 688xxx：19.5%（含 ST，不降档）
    3. 主板 ST / *ST：4.5%
    4. 主板（沪深 .SH / .SZ 其它）：9.5%
    """
    try:
        return float(_cached_limit_up_pct(str(ts_code or ""), str(name or "")))
    except Exception as _e:
        logger.debug("[board_limit_utils] get_limit_up_pct_fast 异常: %s", _e)
        return _DEFAULT_LIMIT_UP_PCT


def get_limit_down_pct_fast(ts_code: Any, name: str = "") -> float:
    """跌停阈值（百分比，负数）；与涨停阈值对称。"""
    return -get_limit_up_pct_fast(ts_code, name)


def is_chinext_star_fast(ts_code: Any) -> bool:
    """是否为创业板（30xxxx）/ 科创板（688xxx）；注册制票。"""
    code = str(ts_code or "").upper()
    if code.endswith(".SZ") and code[:6].startswith("30"):
        return True
    if code.endswith(".SH") and code[:6].startswith("688"):
        return True
    return False


def is_st_fast(ts_code: Any, name: str = "") -> bool:
    """是否为 ST / *ST；根据名称前缀判断。"""
    name_upper = str(name or "").upper()
    return "ST" in name_upper or "*ST" in name_upper


def get_board_label(ts_code: Any, name: str = "") -> str:
    """返回板块标识字符串，便于日志展示。"""
    if is_st_fast(ts_code, name):
        return "ST"
    code = str(ts_code or "").upper()
    if code.endswith(".BJ"):
        return "北交所"
    if is_chinext_star_fast(ts_code):
        return "创业板/科创板"
    return "主板"
