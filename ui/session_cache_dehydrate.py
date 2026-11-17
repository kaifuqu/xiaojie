# -*- coding: utf-8 -*-
"""
ui/session_cache_dehydrate.py
session_state 与后台进程 (async_scan_bridge) 共用的"脱水 / 再水化"层。

V26.88+10-8 复活说明：
- 原 V26.88+9-29 大改造后, 真实实现本应位于此; 但 V26.88+10-3 "UI 清理" 误将本文件删除,
  engine_lazy.py 退化为占位 stub, 导致 funnel / observation / scan_results 等嵌套结构
  被误转为 list, 触发 AttributeError: 'list' object has no attribute 'get'.
- 本次修复: 在此文件下重新实现 5 个公开函数, 语义按"调用上下文反推"严格对齐, 保证
  dehydrate 后数据可存 session_state, rehydrate 后数据可被 run_scan_engine / mock_raw 消费.

【公开 API】
- dehydrate_base_items_list(items)              list of {code, p1_score, df, hist, ...} -> list of {code, p1_score, hist} (去 df)
- rehydrate_base_items_for_scan_engine(items)  list of {code, p1_score, hist}       -> list of {code, p1_score, df, hist} (按 code 重新拉 K 线)
- dehydrate_scan_nested_fragment(fragment)      任意嵌套 dict/list                    -> 同构 dict/list, 把 numpy / datetime / DataFrame 等降级为 JSON 友好值
- dehydrate_scan_results_list(items)            list of dict (扫描结果)               -> list of dict, 递归清理每条
- dehydrate_lab_mock_raw_for_session(mock_raw)  strategy_lab 缓存的 mock_raw, 见 strategy_lab.py
- rehydrate_lab_mock_raw_for_compute(cached)    rehydrate 后供 compute 消费

【不变性】
- dehydrate 永远只做"降级", 不会改变 dict/list 的结构层级.
- rehydrate 会**真的重新拉 K 线** (DuckDB), 不保证与 dehydrate 前完全一致 (DDE 同名漂移容忍).
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional


# ==================== 内部通用: 标量化 ====================

def _to_jsonable(value: Any) -> Any:
    """
    把不可直接 JSON 化的对象 (numpy 标量, datetime, pd.Timestamp, ndarray, DataFrame) 降级
    为 Python 原生类型. 容器 (dict / list / tuple) 递归处理.
    仅做"最薄一层"递归, 不深入 (容器内字段仍为原始引用, 由调用方按需再降级).
    """
    # 1) None / 原生类型直通
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    # 2) 容器: 递归
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return [_to_jsonable(v) for v in value]
    # 3) datetime / date
    if isinstance(value, (datetime, date)):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    # 4) numpy / pandas 等第三方 - 懒检测 (避免 import 失败阻断)
    try:
        import numpy as _np  # noqa
        if isinstance(value, _np.ndarray):
            try:
                return value.tolist()
            except Exception:
                return None
        if isinstance(value, (_np.integer,)):
            return int(value)
        if isinstance(value, (_np.floating,)):
            try:
                return float(value)
            except Exception:
                return None
        if isinstance(value, (_np.bool_,)):
            return bool(value)
    except ImportError:
        pass
    try:
        import pandas as _pd  # noqa
        if isinstance(value, _pd.Timestamp):
            try:
                return value.isoformat()
            except Exception:
                return str(value)
        if isinstance(value, _pd.DataFrame):
            try:
                return value.to_dict(orient="records")
            except Exception:
                return None
        if isinstance(value, _pd.Series):
            try:
                return value.to_dict()
            except Exception:
                return None
    except ImportError:
        pass
    # 5) 兜底: 转字符串 (不抛异常, 让 UI 渲染层自己处理不可显示的对象)
    try:
        return str(value)
    except Exception:
        return None


# ==================== 公开 API ====================

def dehydrate_base_items_list(items: Any) -> Optional[List[Dict[str, Any]]]:
    """
    把 list of base_items (含 DataFrame) 降级为 list of {code, p1_score, hist, ...}.
    - 关键剥离: 'df' / 'df_split' (DataFrame) 不进 session_state.
    - 其他字段 (p1_score / hist / 自定义) 走 _to_jsonable 浅降级.
    - 输入若非 list (如 None) 直接原样返回 (保留 engine_lazy.py 的 "占位 stub" 行为).
    """
    if not isinstance(items, list):
        return items
    out: List[Dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        slim: Dict[str, Any] = {}
        for k, v in it.items():
            if k in ("df", "df_split"):
                # DataFrame 不入 session_state (DuckDB 写完已落盘, rehydrate 时按 code 重新拉)
                continue
            slim[k] = _to_jsonable(v)
        out.append(slim)
    return out


def rehydrate_base_items_for_scan_engine(items: Any, progress_callback=None) -> Optional[List[Dict[str, Any]]]:
    """
    把 dehydrate 后的 base_items 重新加载 DataFrame, 供 run_scan_engine 消费.
    - 行为: 按 item['code'] 重新 get_stock_data_qfq() + precompute_indicators(), 重建 hist.
    - 缺 code / K 线为空 -> 跳过该票 (与原版保持一致, 不抛异常避免整轮扫描崩).
    - 若 items 已被加载过 (已含 'df' 且非空) -> 视为已 rehydrate, 直接返回.
    """
    if not isinstance(items, list) or not items:
        return items
    # 兜底: 进程内 import, 避免顶层硬依赖引发循环
    try:
        from data.db_core import get_stock_data_qfq  # noqa
        from core.indicator_calc import precompute_indicators  # noqa
    except Exception as e:
        logging.warning("session_cache_dehydrate: rehydrate 依赖导入失败, 返回原 items: %s", e)
        return items

    out: List[Dict[str, Any]] = []
    n = len(items)
    for idx, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        # 已是 hydrate 后的完整版 (含 df) -> 直接透传
        if _is_hydrated_df(it.get("df")):
            out.append(it)
            continue
        code = it.get("code")
        if not code:
            continue
        try:
            df = get_stock_data_qfq(code, limit=120)
            if df is None or len(df) == 0:
                continue
            df = precompute_indicators(df)
            hist = df.iloc[-1].to_dict() if len(df) > 0 else {}
            # 保留原 item 的标量字段 (p1_score 等), 仅覆盖 df / hist
            new_it = dict(it)
            new_it["df"] = df
            new_it["hist"] = hist
            out.append(new_it)
            if progress_callback and (idx % 5 == 0):
                try:
                    progress_callback(idx + 1, n)
                except Exception:
                    pass
        except Exception as e:
            logging.debug("session_cache_dehydrate: rehydrate %s 失败: %s", code, e)
            continue
    return out


def _is_empty_df(obj: Any) -> bool:
    """兼容判定: pandas DataFrame 是否为空"""
    try:
        # 已是 DataFrame?
        import pandas as _pd  # noqa
        if isinstance(obj, _pd.DataFrame):
            return len(obj) == 0
    except ImportError:
        pass
    return False


def _is_hydrated_df(obj: Any) -> bool:
    """判定 item['df'] 是否已经是可用的 pandas DataFrame。

    【V30.2 上线前修复】原判断写成 isinstance(it.get("df"), object)，
    该表达式对任何值都为 True（object 是所有类的基类），
    于是「dehydrate 后残留的 list[dict]」被误判成「已水合」直接透传，
    下游 batch_precompute_inflow / single_stock 访问 df.empty / df.tail 时抛
    AttributeError: 'list' object has no attribute 'empty'，
    进而把 P1 整个预计算阶段降级（实盘日志 p1-precompute-fallback）。
    修复：只认真正的非空 DataFrame，其余（list / dict / None / 空 DataFrame）一律重新拉 K 线。
    """
    try:
        import pandas as _pd  # noqa
    except ImportError:
        return False
    return isinstance(obj, _pd.DataFrame) and len(obj) > 0


def dehydrate_scan_nested_fragment(fragment: Any) -> Any:
    """
    把嵌套结构 (funnel / observation / sop_market_breaker) 递归降级为 JSON 友好形式.
    **保持原结构层级**: dict 仍是 dict, list 仍是 list, 仅替换叶子为 JSON 友好值.
    这是修复 V26.88+10-3 占位 stub 误转 list 的核心函数.
    """
    if fragment is None:
        return None
    if isinstance(fragment, dict):
        return {str(k): dehydrate_scan_nested_fragment(v) for k, v in fragment.items()}
    if isinstance(fragment, (list, tuple, set, frozenset)):
        return [dehydrate_scan_nested_fragment(v) for v in fragment]
    return _to_jsonable(fragment)


def dehydrate_scan_results_list(items: Any) -> Optional[List[Any]]:
    """
    把 list of dict (扫描结果) 降级为 list of dict.
    每条 dict 内部的 DataFrame / numpy 等按 dehydrate_scan_nested_fragment 处理.
    """
    if not isinstance(items, list):
        return items
    return [dehydrate_scan_nested_fragment(it) for it in items]


def dehydrate_lab_mock_raw_for_session(mock_raw: Any) -> Any:
    """
    strategy_lab 的 mock_raw 缓存降级 (与 base_items 类似, 但保留全字段, 仅降级不可 JSON 化值).
    """
    if mock_raw is None:
        return None
    if not isinstance(mock_raw, list):
        return mock_raw
    return [dehydrate_scan_nested_fragment(it) for it in mock_raw]


def rehydrate_lab_mock_raw_for_compute(cached: Any, progress_callback=None) -> Any:
    """
    rehydrate lab mock_raw: 补齐 df / hist. 与 rehydrate_base_items_for_scan_engine 几乎相同, 但
    保留全字段, 不剥离任何东西.
    """
    return rehydrate_base_items_for_scan_engine(cached, progress_callback=progress_callback)
