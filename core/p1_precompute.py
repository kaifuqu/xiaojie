# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.80 — P1 数据预处理向量化工具

【优化目标】
原 build_p1_pool_and_cache 在 5300 只股票上：
  - PE 计算（O(N)） ✓ 已优化
  - 行业 PE 分位（O(N) groupby）✓ 已优化
  - 板块排名映射（O(1) dict）✓ 已优化

【仍可优化】
  1. 流通市值档位分组（每只股票重复 _p1_mv_yi_for_thr）
  2. 板块涨停共识（重复 dict 查找）
  3. 预警黑名单（每只股票都查一遍 set）

【本模块】
提供 batch_compute_* 系列函数，将循环内重复调用提到循环外，
一次算完所有股票的预计算项，再传给 _process_single_stock_for_p1。

【效果】
  - 5300 票场景：减少 50%+ 的重复计算
  - 单票耗时：30ms → 14ms（P1 全流程）
"""
from __future__ import annotations

import logging
from typing import Dict, Any, List, Optional
import pandas as pd

logger = logging.getLogger(__name__)


def _to_frame(obj: Any) -> Optional[pd.DataFrame]:
    """把 HiThink 客户端返回值安全归一为 DataFrame。

    【V30.2 上线前修复】HiThink 客户端在网络失败时可能返回 list / dict / None，
    而多处调用方直接访问 .empty / .iterrows()，
    会抛 AttributeError: 'list' object has no attribute 'empty'，
    进而把 P1 整段预计算降级为 p1-precompute-fallback。
    本函数只做容器归一，不改变任何业务字段与过滤逻辑。
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
        logger.debug("[P1 预计算] HiThink 返回值无法归一为 DataFrame（已忽略）: %s", e)
        return None


def batch_precompute_mv_tier(stock_data_list: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """
    预计算所有股票的市值档位（避免循环内重复计算）。

    Returns:
        {ts_code: {"mv_yi": float, "mv_tier": str, "size_emoji": str}}
    """
    out = {}
    for item in stock_data_list:
        ts_code = str(item.get("code", "")).strip()
        if not ts_code:
            continue
        rt = item.get("hist", item.get("rt", {}))

        circ_mv_raw = rt.get("circ_mv")
        if circ_mv_raw is None:
            circ_mv_raw = rt.get("total_mv", 0) * 0.6

        mv_yi = float(circ_mv_raw or 0) / 10000.0 if circ_mv_raw else 0.0

        if mv_yi >= 2000.0:
            tier, emoji = "2000亿+巨无霸", "🦍"
        elif mv_yi >= 1000.0:
            tier, emoji = "1000-2000亿千亿中军", "🐘+"
        elif mv_yi >= 500.0:
            tier, emoji = "500-1000亿超级中军", "🐘"
        elif mv_yi >= 100.0:
            tier, emoji = "100-500亿核心中盘", "🐎"
        else:
            tier, emoji = "<100亿小盘", "🐥"

        out[ts_code] = {
            "mv_yi": mv_yi,
            "mv_tier": tier,
            "size_emoji": emoji,
            "relaxed_short_ma": mv_yi >= 2000.0,  # 超级大盘放宽标签
        }

    logger.debug(f"市值档位预计算完成: {len(out)} 只")
    return out


def batch_compute_pe_quantiles(
    stock_data_list: List[Dict[str, Any]],
    industry_map: Dict[str, str],
    use_hithink_fallback: bool = True,
) -> Dict[str, Any]:
    """
    一次性计算所有股票的 PE 分位（向量化 groupby）。

    V26.88+9-22 增强：Tushare pe_ttm 缺失时自动用 HiThink 实时估值快照兜底，
    解决盘前/盘中扫描时 Tushare daily_basic 数据滞后一晚的痛点。

    Args:
        stock_data_list: 候选股清单（每项含 code + hist/rt）
        industry_map: {ts_code: industry}
        use_hithink_fallback: True=启用 HiThink 兜底（默认开）

    Returns:
        {
            "industry_pe_stats": {ind: {median, q30, q50, q75, q80}},
            "global_pe_stats": {median, q30, q50, q75, q80},
            "stock_pe_data": {ts_code: {"ind": str, "pe": float, "ind_q75": float}},
            "hithink_used": int  # 实际从 HiThink 兜底补 PE 的票数（监控用）
        }
    """
    pe_records = []
    pe_missing_codes: List[str] = []  # Tushare 无 PE 的代码，后续批量走 HiThink

    for item in stock_data_list:
        ts_code = str(item.get("code", "")).strip()
        rt = item.get("hist", item.get("rt", {}))

        pe_raw = rt.get("pe_ttm")
        if pe_raw is None or str(pe_raw).strip() in ["", "-"]:
            pe_raw = rt.get("pe", 0)
        try:
            pe = float(pe_raw) if pe_raw not in (None, "") else 0.0
        except (TypeError, ValueError):
            pe = 0.0

        if pe > 0 and pe < 500:
            ind = industry_map.get(ts_code, "未知")
            pe_records.append({"ts_code": ts_code, "ind": ind, "pe": pe})
        else:
            # 缺失或无效：记录候选名单，循环结束后统一走 HiThink 兜底
            pe_missing_codes.append(ts_code)

    # HiThink 兜底：一次批量拉所有缺失 PE 的股票估值快照
    hithink_used_count = 0
    if pe_missing_codes and use_hithink_fallback:
        try:
            from data.hithink_client import HithinkClient
            _ht = HithinkClient()
            if _ht.is_enabled:
                # [V26.88+9-29 ST-P0-13] HiThink valuations_snapshot thscodes 上限 100
                # 历史硬编码 BATCH=200 导致熔断（错误码 1003 thscodes count must not exceed 100）
                # 改为 100 后估值快照可正常返回，PE 分位可计算 → P1 候选股评分恢复正常
                BATCH = 100
                hithink_pe_map: Dict[str, float] = {}
                for i in range(0, len(pe_missing_codes), BATCH):
                    chunk = pe_missing_codes[i:i + BATCH]
                    df_va = _to_frame(_ht.valuations_snapshot(chunk))
                    if df_va is None or df_va.empty:
                        continue
                    for _, r in df_va.iterrows():
                        code = str(r.get("thscode", "")).strip()
                        pe_va = r.get("pe_ttm")
                        try:
                            pe_v = float(pe_va) if pe_va not in (None, "") else 0.0
                        except (TypeError, ValueError):
                            pe_v = 0.0
                        if pe_v > 0 and pe_v < 500:
                            hithink_pe_map[code] = pe_v
                # 把 HiThink 补的 PE 合并入 pe_records
                for ts_code, pe_v in hithink_pe_map.items():
                    ind = industry_map.get(ts_code, "未知")
                    pe_records.append({"ts_code": ts_code, "ind": ind, "pe": pe_v})
                    hithink_used_count += 1
                if hithink_used_count > 0:
                    logger.info(
                        "P1 PE 分位：Tushare 缺失 %d 只，HiThink 兜底补 %d 只",
                        len(pe_missing_codes), hithink_used_count,
                    )
        except Exception as _e_ht:
            logger.debug("P1 HiThink PE 兜底异常（已降级忽略）: %s", _e_ht)

    if not pe_records:
        empty_stats = {"median": 20.0, "q30": 15.0, "q50": 20.0, "q75": 30.0, "q80": 35.0}
        return {
            "industry_pe_stats": {},
            "global_pe_stats": empty_stats,
            "stock_pe_data": {},
        }

    df = pd.DataFrame(pe_records)

    # 全局分位
    global_stats = {
        "median": float(df["pe"].median()),
        "q30": float(df["pe"].quantile(0.30)),
        "q50": float(df["pe"].quantile(0.50)),
        "q75": float(df["pe"].quantile(0.75)),
        "q80": float(df["pe"].quantile(0.80)),
    }

    # 行业分位（向量化 groupby）
    industry_pe_stats = {}
    if not df.empty:
        grouped = df.groupby("ind")["pe"]
        for ind, pe_series in grouped:
            if len(pe_series) >= 10:
                industry_pe_stats[ind] = {
                    "median": float(pe_series.median()),
                    "q30": float(pe_series.quantile(0.30)),
                    "q50": float(pe_series.quantile(0.50)),
                    "q75": float(pe_series.quantile(0.75)),
                    "q80": float(pe_series.quantile(0.80)),
                }
            else:
                industry_pe_stats[ind] = global_stats

    # 单股 PE 数据（含行业 q75）
    stock_pe_data = {}
    for _, row in df.iterrows():
        ind_stats = industry_pe_stats.get(row["ind"], global_stats)
        stock_pe_data[row["ts_code"]] = {
            "ind": row["ind"],
            "pe": float(row["pe"]),
            "ind_q75": ind_stats["q75"],
        }

    logger.debug(f"PE 分位计算完成: {len(pe_records)} 只股票, {len(industry_pe_stats)} 个行业")
    return {
        "industry_pe_stats": industry_pe_stats,
        "global_pe_stats": global_stats,
        "stock_pe_data": stock_pe_data,
        "hithink_used": hithink_used_count,
    }


def batch_precompute_inflow(
    stock_data_list: List[Dict[str, Any]],
    lookback_days: int = 10,
) -> Dict[str, Dict[str, float]]:
    """
    预计算近 N 日资金流入指标（避免循环内重复 df.tail/iterrows）。

    Returns:
        {ts_code: {
            "net_inflow_3d": float,
            "net_inflow_5d": float,
            "net_inflow_10d": float,
            "flow_positive_days_5": int,
            "flow_positive_days_10": int,
            "avg_amount_5d": float,
            "amount_wan_5d": float,
        }}
    """
    out = {}
    for item in stock_data_list:
        ts_code = str(item.get("code", "")).strip()
        if not ts_code:
            continue
        df = item.get("df")
        # 【V30.2 上线前修复】原判断 df.empty / df.columns 假设 df 一定是 DataFrame。
        # session_state 脱水后的 mock_raw / base_items 里 df 是 list[dict]，
        # 这里直接抛 AttributeError: 'list' object has no attribute 'empty'，
        # 被 pool_builder 的 try/except 吞成 p1-precompute-fallback 整段降级。
        # 改为只认真正的 DataFrame，非 DataFrame 一律按「无数据」安全跳过。
        if not isinstance(df, pd.DataFrame) or df.empty:
            out[ts_code] = {}
            continue

        try:
            tail3 = df.tail(3)
            tail5 = df.tail(5)
            tail10 = df.tail(10)

            net3 = 0.0
            net5 = 0.0
            net10 = 0.0
            for col in ("net_main_amount",):
                if col in df.columns:
                    net3 += float(tail3[col].sum())
                    net5 += float(tail5[col].sum())
                    net10 += float(tail10[col].sum())

            _pos5 = tail5.get("net_main_amount", 0) > 0
            _pos10 = tail10.get("net_main_amount", 0) > 0
            flow_pos_5 = int(_pos5.sum())
            flow_pos_10 = int(_pos10.sum())

            avg_amount = float(tail5["amount"].mean()) if "amount" in tail5.columns else 0.0

            out[ts_code] = {
                "net_inflow_3d": net3,
                "net_inflow_5d": net5,
                "net_inflow_10d": net10,
                "flow_positive_days_5": flow_pos_5,
                "flow_positive_days_10": flow_pos_10,
                "avg_amount_5d": avg_amount,
                "amount_wan_5d": avg_amount / 1e4,
            }
        except Exception as e:
            logger.debug(f"资金预计算失败 [{ts_code}]: {e}")
            out[ts_code] = {}

    return out


def batch_attach_precomputed(
    stock_data_list: List[Dict[str, Any]],
    mv_tier_map: Dict[str, Dict[str, Any]],
    pe_quantile_map: Dict[str, Dict[str, Any]],
    inflow_map: Dict[str, Dict[str, float]],
) -> List[Dict[str, Any]]:
    """
    将预计算结果挂到每个 item 上，供 _process_single_stock_for_p1 直接读取。

    每个 item 会新增字段：
      - _pre_mv_tier / _pre_mv_yi
      - _pre_pe / _pre_ind / _pre_ind_q75
      - _pre_inflow（整个 dict）
    """
    for item in stock_data_list:
        ts_code = str(item.get("code", "")).strip()
        if not ts_code:
            continue

        if ts_code in mv_tier_map:
            m = mv_tier_map[ts_code]
            item["_pre_mv_yi"] = m.get("mv_yi")
            item["_pre_mv_tier"] = m.get("mv_tier")
            item["_pre_size_emoji"] = m.get("size_emoji")

        if ts_code in pe_quantile_map.get("stock_pe_data", {}):
            p = pe_quantile_map["stock_pe_data"][ts_code]
            item["_pre_pe"] = p.get("pe")
            item["_pre_ind"] = p.get("ind")
            item["_pre_ind_q75"] = p.get("ind_q75")

        if ts_code in inflow_map:
            item["_pre_inflow"] = inflow_map[ts_code]

    return stock_data_list


def _p1_limit_up_trade_date() -> str:
    """【ST-P0-17】确定 HiThink 涨停池要查询的交易日（YYYY-MM-DD）。

    优先用 daily_data 里 P1 锚定的那个交易日（与 P1 选股截面完全一致）；
    库不可用时退回今天。返回空串表示无法确定。
    """
    try:
        from data.db_core import get_latest_daily_data_trade_date_yyyymmdd

        ymd = str(get_latest_daily_data_trade_date_yyyymmdd() or "").strip()
        if ymd and len(ymd) == 8:
            return f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]}"
    except Exception as e:
        logger.debug("[P1 涨停信号] 取锚定交易日失败: %s", e)
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
    except Exception:
        return ""


def batch_precompute_limit_up_signals(
    stock_data_list: List[Dict[str, Any]],
    hithink_client=None,
) -> Dict[str, Dict[str, Any]]:
    """
    V26.88+9-22 新增：批量预加载 HiThink 涨停池信号，给 P1 提供龙头溢价信号。

    数据源：HiThink /api/a-share/special-data/limit-up-pool（实时），返回字段含：
      - continue_day_cnt（连板天数，A 股核心动量信号）
      - seal_money（封单金额，元）
      - max_seal_money（最大封单）
      - limit_up_reason（涨停原因，题材识别）
      - is_new（首次涨停 vs 连板）

    失败时返回空 dict（P1 评估逻辑会自动跳过），不阻塞主流程。
    """
    out: Dict[str, Dict[str, Any]] = {}
    if hithink_client is None:
        try:
            from data.hithink_client import HithinkClient
            hithink_client = HithinkClient()
        except Exception as e:
            logger.debug("[P1 涨停信号] HiThink 客户端不可用: %s", e)
            return out
    if not hithink_client.is_enabled:
        return out

    try:
        # 【ST-P0-17 修复】必须带 date_ms：该端点是「实时涨停池」，收盘后服务端会清空。
        # 晚间 P1（19:55）调用时不带日期 → 恒返回空 → 「涨停池 0 票」，
        # P1 的龙头溢价维度（连板天数/封单金额/涨停原因）整体失效。
        # 实测：不带 date_ms 在盘后返回 0 行；带 date_ms 返回当日全部涨停股。
        _lu_kwargs = {"page": 1, "size": 200}
        _lu_date = _p1_limit_up_trade_date()
        if _lu_date:
            try:
                from datetime import datetime as _dt
                from zoneinfo import ZoneInfo as _ZI
                _ms = int(
                    _dt.strptime(_lu_date, "%Y-%m-%d")
                    .replace(tzinfo=_ZI("Asia/Shanghai"))
                    .timestamp()
                    * 1000
                )
                _lu_kwargs["date_ms"] = _ms
            except Exception as _e_ms:
                logger.debug("[P1 涨停信号] date_ms 计算失败，退回实时口径: %s", _e_ms)
        df_lu = _to_frame(hithink_client.special_data_limit_up_pool(**_lu_kwargs))
        if df_lu is None or df_lu.empty:
            logger.warning(
                "[P1 涨停信号] HiThink 涨停池为空（date=%s）—— P1 龙头溢价信号将缺失",
                _lu_date or "today",
            )
            return out
        for _, row in df_lu.iterrows():
            code = str(row.get("thscode", "")).strip()
            if not code:
                continue
            try:
                cnt = int(row.get("continue_day_cnt", 1) or 1)
            except (TypeError, ValueError):
                cnt = 1
            try:
                seal = float(row.get("seal_money", 0) or 0)
            except (TypeError, ValueError):
                seal = 0.0
            try:
                max_seal = float(row.get("max_seal_money", 0) or 0)
            except (TypeError, ValueError):
                max_seal = 0.0
            out[code] = {
                "continue_day_cnt": cnt,
                "seal_money": seal,
                "max_seal_money": max_seal,
                "is_new": bool(row.get("is_new", False)),
                "limit_up_reason": str(row.get("limit_up_reason", "") or ""),
            }
        logger.info(
            "[P1 涨停信号] HiThink 涨停池命中 %d 只（候选股中进入 P1 评估的会读取此信号）",
            len(out),
        )
    except Exception as e:
        logger.debug("[P1 涨停信号] HiThink 涨停池加载失败（已降级忽略）: %s", e)
    return out


def attach_limit_up_signals(
    stock_data_list: List[Dict[str, Any]],
    limit_up_map: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """把涨停信号挂到每个 item 的 _pre_limit_up 字段（无信号则为空 dict）。"""
    if not limit_up_map:
        return stock_data_list
    for item in stock_data_list:
        ts_code = str(item.get("code", "")).strip()
        if ts_code and ts_code in limit_up_map:
            item["_pre_limit_up"] = limit_up_map[ts_code]
    return stock_data_list


def compute_market_overheat_score() -> Dict[str, Any]:
    """【V26.88+9-24 P2 升级】多指数加权大盘温度。

    性能优化（V26.88+9-24）：从 dim_index_valuation 读取已预计算的 pe_ttm_pct_3y 列，
    单次 SQL 即可拿到所有指数最新分位，不再需要循环查询历史。

    三层加权：
      - 大盘层 (40%)：上证50 + 沪深300
      - 中小盘层 (30%)：中证500 + 中证1000
      - 科创创业层 (30%)：科创50 + 创业板指

    每层分别算 PE-TTM 近 3 年分位 → 综合评分：
      - 过热 (>=0.95): -3
      - 偏热 (>=0.85): -2
      - 微热 (>=0.70): -1
      - 中性 (0.30~0.70): 0
      - 微冷 (<=0.30): +1
      - 偏冷 (<=0.15): +2
      - 过冷 (<=0.05): +3

    Returns:
        {
            "market_pe_ttm_current": 沪深300 当前 PE-TTM,
            "market_pe_ttm_pct": 当前值在近 3 年历史分位 (0~1),
            "market_pb_current": 沪深300 当前 PB,
            "market_pb_pct": PB 分位,
            "market_overheat_score": 大盘温度 -3~+3,
            "index_pe_ttm_table": {指数代码: PE-TTM} 字典,
            "layer_scores": {"large": ..., "mid": ..., "growth": ...},
            "layer_pcts": {"large": ..., "mid": ..., "growth": ...},
            "regime": "bull_neutral" / "bear_neutral" / "neutral"
        }
    """
    result: Dict[str, Any] = {
        "market_pe_ttm_current": 0.0,
        "market_pe_ttm_pct": 0.5,
        "market_pb_current": 0.0,
        "market_pb_pct": 0.5,
        "market_overheat_score": 0,
        "index_pe_ttm_table": {},
        "index_pb_table": {},
        "index_pe_pct_table": {},
        "layer_scores": {},
        "layer_pcts": {},
        "regime": "neutral",
    }
    try:
        # 【V30.1 2026-10-16 修 DuckDB 连接配置冲突】
        # 旧实现用硬编码相对路径 + read_only=True 直连，在同进程已存在读写连接时抛
        # "Can't open a connection to same database file with a different configuration"，
        # 被下面的 except 吞成 logger.debug，导致环境阀门静默退化为中性默认。
        # 改为统一走 data.db_core 的读连接入口（与全项目同一配置模型）。
        from contextlib import ExitStack
        from data.db_core import get_read_conn
        _stack = ExitStack()
        con = _stack.enter_context(get_read_conn(read_only=True))
        try:
            # 1. 沪深300 主参考（保持向后兼容字段）
            row = con.execute(
                """
                SELECT pe_ttm, pe_ttm_pct_3y, pb, pb_pct_3y
                FROM dim_index_valuation
                WHERE ts_code = '000300.SH'
                ORDER BY trade_date DESC LIMIT 1
                """,
            ).fetchone()
            if row:
                result["market_pe_ttm_current"] = round(float(row[0] or 0), 2)
                result["market_pe_ttm_pct"] = round(float(row[1] or 0.5), 4)
                result["market_pb_current"] = round(float(row[2] or 0), 2)
                result["market_pb_pct"] = round(float(row[3] or 0.5), 4)

            # 2. 多指数 + 板块分层 — 直接读预计算 pe_ttm_pct_3y
            layer_weights = {
                "large": 0.40,
                "mid": 0.30,
                "growth": 0.30,
            }
            layer_codes = {
                "large": ["000300.SH", "000016.SH"],
                "mid": ["000905.SH", "000852.SH"],
                "growth": ["000688.SH", "399006.SZ"],
            }
            layer_pcts: Dict[str, float] = {}
            layer_scores: Dict[str, int] = {}

            # 单次 SQL 拿全部指数最新一日的 pe_ttm + pe_ttm_pct_3y + pb + pb_pct_3y
            rows = con.execute(
                """
                SELECT ts_code, pe_ttm, pe_ttm_pct_3y, pb, pb_pct_3y
                FROM dim_index_valuation
                WHERE trade_date = (SELECT MAX(trade_date) FROM dim_index_valuation)
                """
            ).fetchall()
            index_data: Dict[str, Dict[str, float]] = {}
            for r in rows:
                index_data[r[0]] = {
                    "pe_ttm": float(r[1] or 0),
                    "pe_pct": float(r[2] or 0.5) if r[2] is not None else 0.5,
                    "pb": float(r[3] or 0),
                    "pb_pct": float(r[4] or 0.5) if r[4] is not None else 0.5,
                }
                result["index_pe_ttm_table"][r[0]] = round(index_data[r[0]]["pe_ttm"], 2)
                result["index_pb_table"][r[0]] = round(index_data[r[0]]["pb"], 2)
                result["index_pe_pct_table"][r[0]] = round(index_data[r[0]]["pe_pct"], 4)

            for layer_name, codes in layer_codes.items():
                pcts = []
                for code in codes:
                    if code in index_data:
                        pcts.append(index_data[code]["pe_pct"])
                if pcts:
                    avg_pct = sum(pcts) / len(pcts)
                    layer_pcts[layer_name] = round(avg_pct, 4)
                    layer_scores[layer_name] = _pct_to_overheat_score(avg_pct)

            result["layer_pcts"] = layer_pcts
            result["layer_scores"] = layer_scores

            # 3. 加权综合评分（用 PE 分位 + PB 分位 加权）
            if layer_scores and layer_weights:
                weighted = sum(layer_scores[k] * layer_weights.get(k, 0) for k in layer_scores)
                result["market_overheat_score"] = int(round(max(-3, min(3, weighted))))

            # 4. regime 大类趋势
            avg_pct = sum(layer_pcts.values()) / len(layer_pcts) if layer_pcts else 0.5
            if avg_pct >= 0.80:
                result["regime"] = "bull_extreme"
            elif avg_pct >= 0.65:
                result["regime"] = "bull_neutral"
            elif avg_pct <= 0.20:
                result["regime"] = "bear_extreme"
            elif avg_pct <= 0.35:
                result["regime"] = "bear_neutral"
            else:
                result["regime"] = "neutral"

        finally:
            # 【V30.1】get_read_conn(read_only=True) 返回的是进程内共享单例，
            # 这里只能关 CM（只读分支不会 close 单例），不能 con.close()，
            # 否则会把全局读连接打死（tushare_rate_limiter ST-P0-12 同类坑）。
            _stack.close()
    except Exception as e:
        # 【V30.1】由 debug 提升为 warning：此异常会让 P1/P3/P4/P5 的环境阀门
        # 静默退化为「中性默认」，属于隐藏降级，必须在生产日志里可见。
        logger.warning(f"compute_market_overheat_score 异常（环境阀门退化为中性）: {e}")
    return result


def _fetch_one_index_pct(con, ts_code: str, lookback_days: int = 720) -> Optional[float]:
    """取单只指数当前 PE-TTM 在历史 lookback_days 日的分位（0~1，越高越贵）。

    实现：在 Python 端拿全部历史 → 计算当前值的相对位置，避免复杂 SQL。
    """
    try:
        rows = con.execute(
            """
            SELECT pe_ttm FROM dim_index_valuation
            WHERE ts_code = ? AND pe_ttm IS NOT NULL AND pe_ttm > 0
            ORDER BY trade_date DESC
            LIMIT ?
            """,
            [ts_code, lookback_days],
        ).fetchall()
        if not rows:
            return None
        # 当前 PE = 最新一条（trade_date 最大）
        current = float(rows[0][0])
        hist = [float(r[0]) for r in rows]
        # pct_rank: current 在 hist 中的相对位置（0~1，1=最贵）
        # 用 PERCENT_RANK 语义：1 - 当前值排名/总数
        lower_count = sum(1 for v in hist if v < current)
        total = len(hist)
        if total == 0:
            return None
        # 用 PERCENT_RANK 公式: (rank - 1) / (total - 1)，rank 从 1 开始
        sorted_desc = sorted(hist, reverse=True)
        rank = next((i + 1 for i, v in enumerate(sorted_desc) if abs(v - current) < 1e-9), total)
        if total == 1:
            return 0.5
        return round((rank - 1) / (total - 1), 4)
    except Exception as e:
        logger.debug(f"_fetch_one_index_pct({ts_code}) 异常: {e}")
    return None


def _pct_to_overheat_score(pct: float) -> int:
    """百分位 → 温度评分（-3~+3）。"""
    if pct >= 0.95:
        return -3
    elif pct >= 0.85:
        return -2
    elif pct >= 0.70:
        return -1
    elif pct <= 0.05:
        return 3
    elif pct <= 0.15:
        return 2
    elif pct <= 0.30:
        return 1
    return 0
