# -*- coding: utf-8 -*-
"""
V26.88 DuckDB 数据库全量体检脚本

目标
- 按 V26 分层架构（QUOTE / FEATURE / DIM / FACT / CACHE / LOG / VIEW）逐一检查所有业务表/视图是否存在
- 检查 daily_data 字段覆盖（含 V26 新增 change / rzmre / capital_resonance_score / fund_memory_score）
- 检查所有日期是否连续；各分层表日期是否一致；主键一致性
- 检查 vw_daily_data_compat 与 daily_data 抽样一致性（v2：含 change / rzmre）
- 检查 p1_cache / signal_log 等运行时表的「时效」与「连续性」（daemon 健康信号）
- 检查 dim_trade_calendar 是否填充（V26 应填交易日历，空表即 WARN）
- 输出可读报告 + 结构化 JSON + Markdown，并落盘 data/runtime/db_inspect_latest.{json,md}
- 严格只读（仅 ensure_v26_compat_view 做视图兜底，不改业务数据）

结果分级
- CRIT：阻断主流程，必须先修（缺主表、缺兼容视图、字段全缺、抽样不一致等）
- WARN：需关注（财报/AI 日志过旧、p1_cache 今日无数据、dim_trade_calendar 空等）
- OK：正常
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import duckdb
import pandas as pd


def _resolve_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(here).lower() == "tools":
        return os.path.dirname(here)
    return os.path.dirname(here)


ROOT_DIR = _resolve_root()
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from data.db_core import (
    ensure_v26_compat_view,
    get_duckdb_path,
    get_read_conn_singleton,
)


# ---------------------------------------------------------------------------
# 常量：V26 表分组与字段清单
# ---------------------------------------------------------------------------

# 主键
PK_COLS = ("ts_code", "trade_date")

# 分层架构定义（用于按层输出报告）
LAYER_GROUPS: Dict[str, List[str]] = {
    "QUOTE_LAYER": ["daily_data", "raw_daily_quotes", "bars_daily"],
    "FEATURE_LAYER": ["feat_daily_core", "feat_daily_capital", "feat_daily_memory"],
    "DIM_LAYER": ["dim_security", "stock_basic", "dim_trade_calendar"],
    "VIEW_LAYER": ["vw_daily_data_compat", "vw_ai_analysis_debug"],
    "CACHE_LAYER": ["p1_cache"],
    "LOG_LAYER": ["signal_log", "fact_ai_analysis_log"],
    "FACT_LAYER": ["fact_financial_reports"],
}

# 所有会被遍历检查的目标（去重保序）
CORE_TABLES: List[str] = []
for _tbls in LAYER_GROUPS.values():
    for _t in _tbls:
        if _t not in CORE_TABLES:
            CORE_TABLES.append(_t)

# 需要检查日期连续性的（主事实型表 + 兼容视图）
DATE_TABLES: List[str] = [
    "daily_data",
    "vw_daily_data_compat",
    "raw_daily_quotes",
    "bars_daily",
    "feat_daily_core",
    "feat_daily_capital",
    "feat_daily_memory",
    "p1_cache",
]

# daily_data 的字段清单（V26.88：含 change / rzmre / V26 资金记忆体系）
LEGACY_EXPECTED_COLUMNS: List[str] = [
    # 基础行情
    "ts_code", "trade_date",
    "open", "high", "low", "close",
    "pre_close", "pct_chg", "change",
    "vol", "amount",
    "turnover_rate_f", "vol_ratio",
    # 估值与市值
    "pe_ttm", "pb", "ps_ttm", "dv_ratio",
    "total_mv", "circ_mv",
    "adj_factor",
    # 均线 / 量均线
    "ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
    "vol_ma5", "vol_ma10", "vol_ma20",
    "ma20_slope_5", "high_20", "low_60",
    # 技术指标
    "macd", "macd_signal", "macd_hist",
    "rsi_14", "kdj_k", "kdj_d",
    "boll_upper", "boll_lower", "cci",
    "bias_20", "atr_pct",
    # 资金流（V26 沿用）
    "net_elg_amount", "net_main_amount", "inst_net_buy",
    "rz_net_buy", "rzmre",
    # 成本分布 / 筹码
    "cost_5th", "cost_50th", "cost_95th",
    "avg_cost", "winner_rate", "cyq_concentration",
    "nineturn_signal",
    # 股性 / 一字板 / 资金记忆（V26.88 体系）
    "limit_times", "strth", "forecast_type",
    "capital_resonance_score", "fund_memory_score",
]

# 各表推荐字段（用于 schema_alignment 比对：实际列 ⊇ 推荐列）
RECOMMENDED_COLUMNS: Dict[str, List[str]] = {
    "dim_trade_calendar": [
        "exchange", "trade_date", "is_open",
        "pretrade_date", "next_trade_date",
    ],
    "fact_financial_reports": [
        "ts_code", "ann_date", "end_date", "report_type",
        "revenue", "revenue_yoy", "net_profit", "net_profit_yoy",
        "deduct_net_profit_yoy", "op_cash_flow",
        "asset_liab_rate", "goodwill", "accounts_receivable", "inventories",
        "risk_flags", "summary_text",
    ],
    "fact_ai_analysis_log": [
        "id", "ts_code", "trade_date", "pool_key",
        "model", "latency_ms", "success", "error_msg", "created_at",
    ],
    "signal_log": [
        "trade_date", "ts_code", "pool", "strategy",
        "score", "regime", "score_breakdown",
    ],
    "p1_cache": ["trade_date", "ts_code", "p1_score"],
}

# 关键事件列（用于时效性检查）
FRESHNESS_KEY: Dict[str, str] = {
    "p1_cache": "trade_date",
    "signal_log": "trade_date",
    "fact_financial_reports": "ann_date",
    "fact_ai_analysis_log": "created_at",
}

# 状态分级
SEV_OK = "OK"
SEV_WARN = "WARN"
SEV_CRIT = "CRIT"


# ---------------------------------------------------------------------------
# 工具方法
# ---------------------------------------------------------------------------

def _print_block(title: str) -> None:
    print("=" * 100)
    print(title)
    print("=" * 100)


def _safe_int(v: Any) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def _table_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = ?", [name]
        ).fetchone()
        return bool(row and _safe_int(row[0]) > 0)
    except Exception:
        return False


def _view_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM duckdb_views() WHERE view_name = ?", [name]
        ).fetchone()
        return bool(row and _safe_int(row[0]) > 0)
    except Exception:
        return False


def _object_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return _table_exists(con, name) or _view_exists(con, name)


def _get_columns(con: duckdb.DuckDBPyConnection, name: str) -> List[str]:
    try:
        if not _object_exists(con, name):
            return []
        df = con.execute(f'DESCRIBE "{name}"').fetchdf()
        if df is None or df.empty:
            return []
        col = "column_name" if "column_name" in df.columns else "name"
        return [str(x) for x in df[col].tolist()]
    except Exception:
        return []


def _get_column_types(con: duckdb.DuckDBPyConnection, name: str) -> Dict[str, str]:
    try:
        if not _object_exists(con, name):
            return {}
        df = con.execute(f'DESCRIBE "{name}"').fetchdf()
        if df is None or df.empty:
            return {}
        n = "column_name" if "column_name" in df.columns else "name"
        t = "column_type" if "column_type" in df.columns else ("type" if "type" in df.columns else None)
        if not t:
            return {}
        return {str(r[n]): str(r[t]) for _, r in df.iterrows()}
    except Exception:
        return {}


def _safe_count(con: duckdb.DuckDBPyConnection, sql: str, params: Optional[Sequence[Any]] = None) -> int:
    try:
        row = con.execute(sql, list(params or [])).fetchone()
        return _safe_int(row[0]) if row else 0
    except Exception:
        return 0


def _load_df(con: duckdb.DuckDBPyConnection, sql: str, params: Optional[Sequence[Any]] = None) -> pd.DataFrame:
    try:
        return con.execute(sql, list(params or [])).fetchdf()
    except Exception:
        return pd.DataFrame()


def _sorted_unique(values: Iterable[Any]) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for v in values:
        s = str(v).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return sorted(out)


def _normalize_date_series(series: pd.Series) -> pd.Series:
    if series is None:
        return pd.Series(dtype="string")
    s = series.astype(str).str.replace(r"[^0-9]", "", regex=True).str[:8]
    return s[s.str.len() == 8]


def _date_range_report(dates: List[str]) -> Dict[str, Any]:
    if not dates:
        return {"count": 0, "start": "", "end": "", "gaps": []}
    d = (
        pd.to_datetime(pd.Series(dates), format="%Y%m%d", errors="coerce")
        .dropna()
        .sort_values()
        .reset_index(drop=True)
    )
    if d.empty:
        return {"count": 0, "start": "", "end": "", "gaps": []}
    expected = pd.date_range(d.iloc[0], d.iloc[-1], freq="B")
    missing = sorted(set(expected.strftime("%Y%m%d")) - set(d.dt.strftime("%Y%m%d")))
    return {
        "count": int(d.shape[0]),
        "start": d.iloc[0].strftime("%Y-%m-%d"),
        "end": d.iloc[-1].strftime("%Y-%m-%d"),
        "gaps": missing,
    }


def _max_date_str(con: duckdb.DuckDBPyConnection, table: str, col: str) -> str:
    try:
        if not _table_exists(con, table):
            return ""
        s = con.execute(f'SELECT MAX("{col}") FROM "{table}"').fetchone()
        if not s or s[0] is None:
            return ""
        return str(s[0]).strip()[:10]
    except Exception:
        return ""


def _now_bj_date_str() -> str:
    """返回北京时间今天 YYYY-MM-DD（用于时效性比对）。"""
    try:
        from datetime import datetime, timezone, timedelta
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    except Exception:
        try:
            return pd.Timestamp.now().strftime("%Y-%m-%d")
        except Exception:
            return ""


def _days_between(later: str, earlier: str) -> Optional[int]:
    if not later or not earlier:
        return None
    try:
        a = pd.to_datetime(later, errors="coerce")
        b = pd.to_datetime(earlier, errors="coerce")
        if pd.isna(a) or pd.isna(b):
            return None
        return int((a - b).days)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# 检查 1：表/视图存在性 + schema
# ---------------------------------------------------------------------------

def verify_schema_alignment(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    tables: Dict[str, Any] = {}
    for tbl in CORE_TABLES:
        exists = _object_exists(con, tbl)
        tables[tbl] = {
            "exists": exists,
            "is_view": _view_exists(con, tbl) and not _table_exists(con, tbl),
            "columns": _get_columns(con, tbl),
            "types": _get_column_types(con, tbl),
            "rows": _safe_count(con, f'SELECT COUNT(*) FROM "{tbl}"') if exists else 0,
        }
        recommended = RECOMMENDED_COLUMNS.get(tbl)
        if recommended and tables[tbl]["columns"]:
            present = set(tables[tbl]["columns"])
            tables[tbl]["missing_recommended"] = sorted(
                [c for c in recommended if c not in present]
            )
        else:
            tables[tbl]["missing_recommended"] = []
    return {"tables": tables}


# ---------------------------------------------------------------------------
# 检查 2：daily_data 字段覆盖
# ---------------------------------------------------------------------------

def verify_coverage(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    legacy_cols = _get_columns(con, "daily_data")
    view_cols = _get_columns(con, "vw_daily_data_compat")
    legacy_set = set(LEGACY_EXPECTED_COLUMNS)
    view_set = set(view_cols)
    missing_in_view = sorted([c for c in legacy_set if c not in view_set]) if view_set else sorted(legacy_set)
    extra_in_view = sorted([c for c in view_set if c not in legacy_set]) if view_set else []
    missing_in_legacy = sorted([c for c in legacy_set if c not in set(legacy_cols)])
    extra_in_legacy = sorted([c for c in set(legacy_cols) if c not in legacy_set])
    return {
        # 与「全量清单」的双向对比：主表 vs 兼容视图
        "ok": bool(view_set) and len(missing_in_view) == 0,
        "missing_in_view": missing_in_view,
        "extra_in_view": extra_in_view,
        # 主表内部：清单是否覆盖到位（缺则说明 LEGACY_EXPECTED_COLUMNS 没跟上生产字段）
        "missing_in_legacy": missing_in_legacy,
        "extra_in_legacy": extra_in_legacy,
        "legacy_column_count": len(legacy_cols),
        "view_column_count": len(view_cols),
    }


# ---------------------------------------------------------------------------
# 检查 3：维表一致性
# ---------------------------------------------------------------------------

def verify_dim_security_consistency(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": False,
        "severity": SEV_WARN,
        "rows_dim": 0,
        "rows_stock_basic": 0,
        "missing_in_dim": 0,
        "missing_in_stock_basic": 0,
        "reason": "",
    }
    if not _table_exists(con, "dim_security") and not _table_exists(con, "stock_basic"):
        result["severity"] = SEV_CRIT
        result["reason"] = "both_missing"
        return result
    if _table_exists(con, "dim_security"):
        result["rows_dim"] = _safe_count(con, "SELECT COUNT(*) FROM dim_security")
    if _table_exists(con, "stock_basic"):
        result["rows_stock_basic"] = _safe_count(con, "SELECT COUNT(*) FROM stock_basic")
    if _table_exists(con, "dim_security") and _table_exists(con, "stock_basic"):
        try:
            row = con.execute(
                """
                WITH d AS (SELECT ts_code FROM dim_security),
                     s AS (SELECT ts_code FROM stock_basic)
                SELECT
                    (SELECT COUNT(*) FROM (SELECT ts_code FROM s EXCEPT SELECT ts_code FROM d)),
                    (SELECT COUNT(*) FROM (SELECT ts_code FROM d EXCEPT SELECT ts_code FROM s))
                """
            ).fetchone()
            result["missing_in_dim"] = _safe_int(row[0])
            result["missing_in_stock_basic"] = _safe_int(row[1])
        except Exception as e:
            result["severity"] = SEV_CRIT
            result["reason"] = str(e)
            return result
    ok = (
        result["rows_dim"] > 0
        and result["rows_stock_basic"] > 0
        and result["missing_in_dim"] == 0
        and result["missing_in_stock_basic"] == 0
    )
    result["ok"] = ok
    result["severity"] = SEV_OK if ok else SEV_WARN
    return result


# ---------------------------------------------------------------------------
# 检查 4：日期完整性 + 各表日期与 daily_data 比对
# ---------------------------------------------------------------------------

def verify_date_integrity(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    report: Dict[str, Any] = {}
    for tbl in DATE_TABLES:
        if not _object_exists(con, tbl):
            report[tbl] = {
                "exists": False,
                "rows": 0,
                "date_count": 0,
                "start": "", "end": "", "gaps": [],
            }
            continue
        df = _load_df(con, f'SELECT DISTINCT trade_date FROM "{tbl}"')
        dates = (
            _sorted_unique(_normalize_date_series(df["trade_date"]).tolist())
            if not df.empty and "trade_date" in df.columns
            else []
        )
        rr = _date_range_report(dates)
        report[tbl] = {
            "exists": True,
            "rows": _safe_count(con, f'SELECT COUNT(*) FROM "{tbl}"'),
            "date_count": rr["count"],
            "start": rr["start"],
            "end": rr["end"],
            "gaps": rr["gaps"],
        }
    # 主表与分层表日期差集
    if _table_exists(con, "daily_data"):
        base_dates = set(
            _sorted_unique(
                _normalize_date_series(_load_df(con, "SELECT DISTINCT trade_date FROM daily_data")["trade_date"]).tolist()
            )
        )
        for tbl in [
            "raw_daily_quotes", "bars_daily",
            "feat_daily_core", "feat_daily_capital", "feat_daily_memory",
            "p1_cache",
        ]:
            if not _object_exists(con, tbl):
                continue
            tbl_dates = set(
                _sorted_unique(
                    _normalize_date_series(_load_df(con, f'SELECT DISTINCT trade_date FROM "{tbl}"')["trade_date"]).tolist()
                )
            )
            report[tbl]["missing_vs_daily_data"] = sorted(list(base_dates - tbl_dates))[:200]
            report[tbl]["extra_vs_daily_data"] = sorted(list(tbl_dates - base_dates))[:200]
    return report


# ---------------------------------------------------------------------------
# 检查 5：vw_daily_data_compat 抽样一致性（v2：含 change / rzmre / V26 资金记忆）
# ---------------------------------------------------------------------------

VERIFY_VIEW_KEY_COLS = [
    "ts_code", "trade_date",
    "close", "pct_chg",
    "change", "rzmre",
    "ma20", "capital_resonance_score", "fund_memory_score",
]


def verify_view_compat_consistency(con: duckdb.DuckDBPyConnection, sample_rows: int = 500) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "severity": SEV_OK,
        "legacy_rows": 0,
        "view_rows": 0,
        "missing_key_columns_in_view": [],
        "sample_diff_count": -1,
        "reason": "",
    }
    legacy_exists = _table_exists(con, "daily_data")
    view_exists = _view_exists(con, "vw_daily_data_compat")
    if legacy_exists:
        result["legacy_rows"] = _safe_count(con, "SELECT COUNT(*) FROM daily_data")
    if view_exists:
        result["view_rows"] = _safe_count(con, "SELECT COUNT(*) FROM vw_daily_data_compat")
    view_cols = _get_columns(con, "vw_daily_data_compat")
    result["missing_key_columns_in_view"] = sorted([c for c in VERIFY_VIEW_KEY_COLS if c not in view_cols])

    if not legacy_exists:
        result["severity"] = SEV_WARN
        result["reason"] = "legacy_table_missing"
        return result
    if not view_exists:
        result["severity"] = SEV_CRIT
        result["reason"] = "compat_view_missing"
        return result
    if result["missing_key_columns_in_view"]:
        result["severity"] = SEV_CRIT
        result["reason"] = "missing_key_columns"
        return result

    # 【V29 修复】原先直接 SELECT daily_data.change 与视图 change 比对：
    # 视图的 change 是派生列 (close - pre_close)，而 daily_data.change 是 Tushare 原始字段，
    # 二者本就不同（daily_data.change 有 97% 为 NULL，视图列永不 NULL），
    # 导致 EXCEPT 恒不为 0、每次体检都报 CRIT，属于假告警。
    # 现改为：daily_data 侧的 change 用与视图相同的派生口径重算后再比对。
    select_cols = ", ".join(VERIFY_VIEW_KEY_COLS)
    legacy_select_cols = select_cols
    if "change" in VERIFY_VIEW_KEY_COLS and "close" in VERIFY_VIEW_KEY_COLS and "pre_close" in (
        _get_columns(con, "daily_data")
    ):
        legacy_select_cols = select_cols.replace(
            "change",
            '(CAST(close AS DOUBLE) - CAST(pre_close AS DOUBLE)) AS change',
        )
    try:
        q = f"""
            WITH l AS (
                SELECT {legacy_select_cols}
                FROM daily_data
                ORDER BY ts_code, trade_date
                LIMIT {int(sample_rows)}
            ), v AS (
                SELECT {select_cols}
                FROM vw_daily_data_compat
                ORDER BY ts_code, trade_date
                LIMIT {int(sample_rows)}
            )
            SELECT COUNT(*) FROM (
                SELECT * FROM l
                EXCEPT
                SELECT * FROM v
            ) t
        """
        result["sample_diff_count"] = _safe_count(con, q)
    except Exception as e:
        result["severity"] = SEV_CRIT
        result["reason"] = str(e)
        return result

    if result["sample_diff_count"] != 0:
        result["severity"] = SEV_CRIT
    elif result["legacy_rows"] and result["view_rows"] and result["legacy_rows"] != result["view_rows"]:
        # 行数不一致也算不一致（视图丢行/多行），但抽样恰好无差异时按 WARN 提示，
        # 避免"抽样 500 行刚好一致"就漏掉整表行数漂移。
        result["severity"] = SEV_WARN
        result["reason"] = "row_count_mismatch"
    return result


# ---------------------------------------------------------------------------
# 检查 6：dim_trade_calendar 健康
# ---------------------------------------------------------------------------

def verify_dim_trade_calendar(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "exists": _table_exists(con, "dim_trade_calendar"),
        "rows": 0,
        "is_open_count": 0,
        "first_date": "",
        "last_date": "",
        "severity": SEV_OK,
        "reason": "",
    }
    if not result["exists"]:
        result["severity"] = SEV_CRIT
        result["reason"] = "table_missing"
        return result
    result["rows"] = _safe_count(con, "SELECT COUNT(*) FROM dim_trade_calendar")
    if result["rows"] == 0:
        result["severity"] = SEV_WARN
        result["reason"] = "empty_table"
        return result
    try:
        row = con.execute(
            """
            SELECT COUNT(*) FILTER (WHERE is_open),
                   MIN(trade_date), MAX(trade_date)
            FROM dim_trade_calendar
            """
        ).fetchone()
        result["is_open_count"] = _safe_int(row[0])
        result["first_date"] = str(row[1] or "").strip()[:10]
        result["last_date"] = str(row[2] or "").strip()[:10]
    except Exception as e:
        result["reason"] = f"describe_error: {e}"
        result["severity"] = SEV_WARN
    return result


# ---------------------------------------------------------------------------
# 检查 7：运行时表时效（p1_cache / signal_log / fact_*）
# ---------------------------------------------------------------------------

def verify_runtime_freshness(con: duckdb.DuckDBPyConnection, today_str: str) -> Dict[str, Any]:
    """对 CACHE / LOG / FACT 表做「最新日期 / 距今天数」检查。

    分级策略：
    - p1_cache 今日无数据  -> CRIT（daemon 漏斗核心）
    - signal_log 60 个交易日内无数据 -> WARN（战法写入异常）
    - fact_ai_analysis_log 7 日内无数据 -> WARN
    - fact_financial_reports 最新 ann_date 距今 > 90 日 -> WARN（财报断层）
    """
    out: Dict[str, Any] = {"today": today_str, "items": {}}
    sev_order = {SEV_OK: 0, SEV_WARN: 1, SEV_CRIT: 2}
    overall = SEV_OK

    def _bump(target: str, new: str) -> str:
        return target if sev_order[target] >= sev_order[new] else new

    for tbl, key_col in FRESHNESS_KEY.items():
        item: Dict[str, Any] = {
            "exists": _table_exists(con, tbl),
            "rows": 0,
            "max_date": "",
            "days_behind": None,
            "severity": SEV_OK,
            "reason": "",
        }
        if not item["exists"]:
            item["severity"] = SEV_CRIT if tbl in ("p1_cache", "signal_log") else SEV_WARN
            item["reason"] = "table_missing"
            overall = _bump(overall, item["severity"])
            out["items"][tbl] = item
            continue
        item["rows"] = _safe_count(con, f'SELECT COUNT(*) FROM "{tbl}"')
        if item["rows"] == 0:
            item["severity"] = SEV_WARN
            item["reason"] = "empty_table"
            overall = _bump(overall, item["severity"])
            out["items"][tbl] = item
            continue
        item["max_date"] = _max_date_str(con, tbl, key_col)
        item["days_behind"] = _days_between(today_str, item["max_date"])

        # 业务分级
        if tbl == "p1_cache":
            if item["max_date"] and item["max_date"] >= today_str:
                item["severity"] = SEV_OK
            else:
                # 今日未产：是非交易日还是漏算？统一 WARN（由人工 / daemon 日志决定）
                item["severity"] = SEV_WARN
                item["reason"] = "today_cache_missing"
        elif tbl == "signal_log":
            d = item["days_behind"]
            if d is None:
                item["severity"] = SEV_WARN
                item["reason"] = "max_date_unparseable"
            elif d <= 7:
                item["severity"] = SEV_OK
            elif d <= 30:
                item["severity"] = SEV_WARN
                item["reason"] = "stale_7_30d"
            else:
                item["severity"] = SEV_CRIT
                item["reason"] = "stale_30d_plus"
        elif tbl == "fact_ai_analysis_log":
            d = item["days_behind"]
            if d is None or d <= 7:
                item["severity"] = SEV_OK
            elif d <= 30:
                item["severity"] = SEV_WARN
                item["reason"] = "stale_7_30d"
            else:
                item["severity"] = SEV_WARN
                item["reason"] = "stale_30d_plus"
        elif tbl == "fact_financial_reports":
            d = item["days_behind"]
            if d is None:
                item["severity"] = SEV_WARN
                item["reason"] = "max_date_unparseable"
            elif d <= 90:
                item["severity"] = SEV_OK
            elif d <= 180:
                item["severity"] = SEV_WARN
                item["reason"] = "ann_gap_90_180d"
            else:
                item["severity"] = SEV_WARN
                item["reason"] = "ann_gap_180d_plus"
        overall = _bump(overall, item["severity"])
        out["items"][tbl] = item
    out["severity"] = overall
    return out


# ---------------------------------------------------------------------------
# 检查 8：关键源字段退化检测（全 0 / 全 NULL / 常量）
# ---------------------------------------------------------------------------

# 列名 -> 是否「退化即 CRIT」
CRITICAL_SOURCE_COLUMNS: Dict[str, bool] = {
    "total_mv": True,      # P1 市值闸 + 排序
    "circ_mv": True,       # P1 市值闸
    "adj_factor": True,    # 前复权口径，=0 会让所有历史价格失真
    "close": True,
    "vol": True,
    "amount": True,
    "pre_close": True,
    "pe_ttm": False,       # 估值接口可缺，缺失只降级不强杀
    "pb": False,
    "ps_ttm": False,
    "dv_ratio": False,
}


def verify_critical_column_degeneracy(con: duckdb.DuckDBPyConnection) -> Dict[str, Any]:
    """检测 daily_data 关键源字段是否整体退化为 0 / NULL / 常量。

    【P0-20261018 V30.5】历史事故：一次性回填脚本把 total_mv / adj_factor /
    pe_ttm / pb / ps_ttm / dv_ratio 整体写成 0，旧的体检只查「行数够不够」，
    把 0 当有效值全程报绿，直到策略层才发现市值闸与前复权全废。
    这里补上「值域体检」：全 0 / 全 NULL / 非 NULL 且方差为 0 都算退化。
    """
    out: Dict[str, Any] = {"table": "daily_data", "items": {}}
    if not _table_exists(con, "daily_data"):
        out["reason"] = "table_missing"
        return out
    cols = set(_get_columns(con, "daily_data"))
    for col, fatal in CRITICAL_SOURCE_COLUMNS.items():
        item: Dict[str, Any] = {"column": col, "fatal_if_zero": fatal, "severity": SEV_OK, "reason": ""}
        if col not in cols:
            item["severity"] = SEV_CRIT if fatal else SEV_WARN
            item["reason"] = "column_missing"
            out["items"][col] = item
            continue
        try:
            row = con.execute(
                f'SELECT COUNT(*), '
                f'  SUM(CASE WHEN "{col}" IS NULL THEN 1 ELSE 0 END), '
                f'  SUM(CASE WHEN "{col}" IS NOT NULL AND "{col}" = 0 THEN 1 ELSE 0 END), '
                f'  MIN("{col}"), MAX("{col}"), STDDEV_POP("{col}") '
                f'FROM daily_data'
            ).fetchone()
        except Exception as exc:  # noqa: BLE001
            item["severity"] = SEV_WARN
            item["reason"] = f"describe_error: {exc}"
            out["items"][col] = item
            continue
        n = int(row[0] or 0)
        nulls = int(row[1] or 0)
        zeros = int(row[2] or 0)
        mn, mx, sd = row[3], row[4], row[5]
        item.update({"rows": n, "nulls": nulls, "zeros": zeros,
                    "min": mn, "max": mx, "stddev": sd})
        nonnull = n - nulls
        if n <= 0:
            item["severity"] = SEV_WARN
            item["reason"] = "empty_table"
        elif nonnull <= 0:
            item["severity"] = SEV_CRIT if fatal else SEV_WARN
            item["reason"] = "all_null"
        elif zeros >= n:
            item["severity"] = SEV_CRIT if fatal else SEV_WARN
            item["reason"] = "all_zero"
        elif sd is not None and float(sd) == 0.0 and nonnull >= 1000:
            item["severity"] = SEV_CRIT if fatal else SEV_WARN
            item["reason"] = "constant_value"
        out["items"][col] = item
    return out

# ---------------------------------------------------------------------------
# 顶层汇总
# ---------------------------------------------------------------------------

def verify_v26_consistency(con: duckdb.DuckDBPyConnection, sample_rows: int = 500) -> Dict[str, Any]:
    today_str = _now_bj_date_str()
    schema = verify_schema_alignment(con)
    coverage = verify_coverage(con)
    dim_check = verify_dim_security_consistency(con)
    date_integrity = verify_date_integrity(con)
    view_check = verify_view_compat_consistency(con, sample_rows=sample_rows)
    cal_check = verify_dim_trade_calendar(con)
    fresh = verify_runtime_freshness(con, today_str)
    degenerate = verify_critical_column_degeneracy(con)

    # 顶层 ok：只有 critical 全消 才算「通过」；WARN 仍通过（人工按需处置）
    critical_items = []
    if coverage["missing_in_view"]:
        critical_items.append("coverage_missing_in_view")
    if view_check.get("severity") == SEV_CRIT:
        critical_items.append(f"view_compat:{view_check.get('reason') or 'sample_diff'}")
    if dim_check.get("severity") == SEV_CRIT:
        critical_items.append(f"dim_security:{dim_check.get('reason') or 'mismatch'}")
    if cal_check.get("severity") == SEV_CRIT:
        critical_items.append("dim_trade_calendar_missing")
    crits = [k for k, v in fresh["items"].items() if v.get("severity") == SEV_CRIT]
    if crits:
        critical_items.append("freshness:" + ",".join(crits))
    crit_cols = [k for k, v in degenerate["items"].items() if v.get("severity") == SEV_CRIT]
    if crit_cols:
        critical_items.append("degenerate_column:" + ",".join(crit_cols))

    ok = not critical_items
    return {
        "ok": ok,
        "critical_items": critical_items,
        "schema": schema,
        "coverage": coverage,
        "dim_security": dim_check,
        "date_integrity": date_integrity,
        "view_compat": view_check,
        "dim_trade_calendar": cal_check,
        "runtime_freshness": fresh,
        "critical_column_degeneracy": degenerate,
    }


# ---------------------------------------------------------------------------
# 字段逐列完整性（按交易日）
# ---------------------------------------------------------------------------

def _build_field_completeness_report(con: duckdb.DuckDBPyConnection, table_name: str = "daily_data") -> Dict[str, Any]:
    if not _table_exists(con, table_name):
        return {"table": table_name, "exists": False, "total_days": 0, "total_rows": 0, "rows": []}
    total_days = _safe_count(con, f'SELECT COUNT(DISTINCT trade_date) FROM "{table_name}"')
    total_rows = _safe_count(con, f'SELECT COUNT(*) FROM "{table_name}"')
    cols = _get_columns(con, table_name)
    rows: List[Dict[str, Any]] = []
    for col in cols:
        if col in PK_COLS:
            continue
        valid_days = _safe_count(
            con,
            f"""
            SELECT COUNT(DISTINCT trade_date)
            FROM "{table_name}"
            WHERE "{col}" IS NOT NULL
              AND TRIM(CAST("{col}" AS VARCHAR)) != ''
            """,
        )
        ratio = round((valid_days / total_days * 100.0), 2) if total_days else 0.0
        if ratio >= 99.0:
            status = "FULL"
        elif ratio >= 90.0:
            status = "GOOD"
        elif ratio > 0:
            status = "PART"
        else:
            status = "MISS"
        rows.append(
            {
                "column": col,
                "valid_days": valid_days,
                "total_days": total_days,
                "ratio": ratio,
                "status": status,
            }
        )
    rows.sort(key=lambda x: (x["status"] != "FULL", x["status"] != "GOOD", x["ratio"], x["column"]))
    return {"table": table_name, "exists": True, "total_days": total_days, "total_rows": total_rows, "rows": rows}


# ---------------------------------------------------------------------------
# 输出层
# ---------------------------------------------------------------------------

def _sev_icon(sev: str) -> str:
    return {SEV_OK: "[OK]   ", SEV_WARN: "[WARN] ", SEV_CRIT: "[CRIT] "}.get(sev, "[?]    ")


def _print_layer_summary(con: duckdb.DuckDBPyConnection) -> None:
    _print_block("1) V26 分层架构概览")
    for layer, tbls in LAYER_GROUPS.items():
        print(f"[{layer}]")
        for tbl in tbls:
            exists = _object_exists(con, tbl)
            if exists:
                cnt = _safe_count(con, f'SELECT COUNT(*) FROM "{tbl}"')
                kind = "view" if _view_exists(con, tbl) and not _table_exists(con, tbl) else "table"
                print(f"  [OK]   {tbl:<28} ({kind}) rows={cnt:,}")
            else:
                print(f"  [MISS] {tbl:<28} 不存在")
        print()


def _print_coverage(coverage: Dict[str, Any]) -> None:
    _print_block("2) 字段覆盖检查（daily_data <-> LEGACY_EXPECTED_COLUMNS / vw_daily_data_compat）")
    print(f"主表列数={coverage.get('legacy_column_count', 0)} | 兼容视图列数={coverage.get('view_column_count', 0)}")
    mv = coverage.get("missing_in_view", [])
    print(f"兼容视图缺失字段: {', '.join(mv) if mv else '无'}")
    ml = coverage.get("missing_in_legacy", [])
    print(f"主表缺清单字段: {'/'.join(ml) if ml else '无（清单已对齐）'}")
    ev = coverage.get("extra_in_view", [])
    print(f"兼容视图额外字段: {', '.join(ev) if ev else '无'}")
    el = coverage.get("extra_in_legacy", [])
    print(f"主表额外字段: {', '.join(el) if el else '无'}")
    print(f"覆盖结论: {'通过' if coverage.get('ok') else '未通过'}")


def _print_dim_check(d: Dict[str, Any]) -> None:
    _print_block("3) dim_security 一致性")
    print(f"{_sev_icon(d.get('severity', SEV_WARN))} dim(rows={d.get('rows_dim',0):,}) stock_basic(rows={d.get('rows_stock_basic',0):,}) "
          f"missing_in_dim={d.get('missing_in_dim',0)} missing_in_stock_basic={d.get('missing_in_stock_basic',0)}")
    if d.get("reason"):
        print(f"       reason={d['reason']}")


def _print_date_integrity(di: Dict[str, Any]) -> None:
    _print_block("4) 日期完整性（按表）")
    for tbl, info in di.items():
        if not isinstance(info, dict):
            continue
        if not info.get("exists"):
            print(f"[MISS] {tbl:<26} 不存在")
            continue
        print(
            f"[OK]   {tbl:<26} rows={info.get('rows', 0):,} | dates={info.get('date_count', 0)} | "
            f"{info.get('start', '')} -> {info.get('end', '')}"
        )
        gaps = info.get("gaps", []) or []
        if gaps:
            print(f"       gaps={len(gaps)} 例如: {', '.join(gaps[:10])}")
        m = info.get("missing_vs_daily_data") or []
        x = info.get("extra_vs_daily_data") or []
        if m or x:
            print(f"       vs daily_data missing={len(m)} extra={len(x)}")


def _print_view_check(vc: Dict[str, Any]) -> None:
    _print_block("5) vw_daily_data_compat 抽样一致性（v2）")
    print(f"{_sev_icon(vc.get('severity', SEV_WARN))} legacy_rows={vc.get('legacy_rows', 0):,} | "
          f"view_rows={vc.get('view_rows', 0):,} | sample_diff={vc.get('sample_diff_count', -1)}")
    mk = vc.get("missing_key_columns_in_view", [])
    if mk:
        print(f"       视图缺关键列: {', '.join(mk)}")
    if vc.get("reason"):
        print(f"       reason={vc['reason']}")


def _print_dim_calendar(c: Dict[str, Any]) -> None:
    _print_block("6) dim_trade_calendar 健康")
    if not c.get("exists"):
        print(f"{_sev_icon(SEV_CRIT)} 表不存在")
        return
    print(f"{_sev_icon(c.get('severity', SEV_WARN))} rows={c.get('rows', 0):,} | "
          f"is_open={c.get('is_open_count', 0):,} | "
          f"{c.get('first_date', '')} -> {c.get('last_date', '')}")
    if c.get("reason"):
        print(f"       reason={c['reason']}")


def _print_runtime_freshness(rf: Dict[str, Any]) -> None:
    _print_block("7) 运行时表时效（p1_cache / signal_log / fact_*）")
    today = rf.get("today", "")
    print(f"今日(北京时间): {today or '未知'}")
    for tbl, info in (rf.get("items") or {}).items():
        sev = info.get("severity", SEV_WARN)
        key_col = FRESHNESS_KEY.get(tbl, "")
        print(
            f"{_sev_icon(sev)} {tbl:<26} rows={info.get('rows', 0):>10,} "
            f"{key_col}={info.get('max_date', '')} 距今={info.get('days_behind')}"
            + (f"  reason={info.get('reason')}" if info.get("reason") else "")
        )


def _print_field_completeness(fc: Dict[str, Any]) -> None:
    _print_block("8) 字段逐列完整性（daily_data）")
    if not fc.get("exists"):
        print("[MISS] daily_data 不存在")
        return
    print(f"总记录数: {fc.get('total_rows', 0):,} | 交易日数: {fc.get('total_days', 0)}")
    print("-" * 100)


def _print_critical_column_degeneracy(dg: Dict[str, Any]) -> None:
    _print_block("8.5) 关键源字段退化检测（全 0 / 全 NULL / 常量）")
    items = dg.get("items") or {}
    if not items:
        print(f"[MISS] daily_data 不存在或无法检测（reason={dg.get('reason', '')}）")
        return
    print(f"{'':<4}{'字段':<16}{'行数':>10}{'NULL':>10}{'0 值':>10}{'最小':>16}{'最大':>16}  状态")
    for col, info in items.items():
        sev = info.get("severity", SEV_WARN)
        print(
            f"{_sev_icon(sev)} {col:<16}"
            f"{int(info.get('rows', 0)):>10,}"
            f"{int(info.get('nulls', 0)):>10,}"
            f"{int(info.get('zeros', 0)):>10,}"
            f"{_fmt_num(info.get('min')):>16}"
            f"{_fmt_num(info.get('max')):>16}"
            + (f"  {info.get('reason')}" if info.get("reason") else "")
        )


def _fmt_num(v: Any) -> str:
    try:
        if v is None:
            return "-"
        f = float(v)
        if abs(f) >= 1000:
            return f"{f:,.0f}"
        return f"{f:,.4f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "-"
    print(f"{'字段':<26} {'完整天数':>10} {'总天数':>10} {'完整率':>10} {'状态':>10}")
    print("-" * 100)
    for r in fc.get("rows", []):
        print(
            f"{r.get('column',''):<26} {int(r.get('valid_days',0)):>10,} {int(r.get('total_days',0)):>10,} "
            f"{float(r.get('ratio',0.0)):>9.2f}% {r.get('status',''):>10}"
        )
    print("-" * 100)


def _print_final(result: Dict[str, Any]) -> None:
    _print_block("9) 结论分级")
    crit = result.get("critical_items", [])
    cov_ok = result.get("coverage", {}).get("ok")
    view_sev = result.get("view_compat", {}).get("severity", "?")
    dim_sev = result.get("dim_security", {}).get("severity", "?")
    cal_sev = result.get("dim_trade_calendar", {}).get("severity", "?")
    fresh_sev = result.get("runtime_freshness", {}).get("severity", "?")
    degen = result.get("critical_column_degeneracy", {}) or {}
    degen_sev = SEV_OK
    degen_crit = [k for k, v in (degen.get("items") or {}).items() if v.get("severity") == SEV_CRIT]
    degen_warn = [k for k, v in (degen.get("items") or {}).items() if v.get("severity") == SEV_WARN]
    if degen_crit:
        degen_sev = SEV_CRIT
    elif degen_warn:
        degen_sev = SEV_WARN
    print(f"  coverage       : {'OK' if cov_ok else 'WARN'}")
    print(f"  view_compat    : {view_sev}")
    print(f"  dim_security   : {dim_sev}")
    print(f"  dim_calendar   : {cal_sev}")
    print(f"  freshness      : {fresh_sev}")
    print(f"  degenerate_cols: {degen_sev} ({len(degen_crit)} CRIT / {len(degen_warn)} WARN)")
    print(f"  critical_items : {crit if crit else '无'}")
    print(f"  ok={result.get('ok')}")
    if result.get("ok"):
        print("[OK] V26 主链路无 CRIT（可继续运行；WARN 项人工按需处置）")
    else:
        print("[CRIT] V26 存在阻断项，请按 critical_items 修复后再启动主流程")


# ---------------------------------------------------------------------------
# Markdown 输出
# ---------------------------------------------------------------------------

def _md_section(title: str, body: str) -> str:
    return f"\n## {title}\n\n{body}\n"


def _md_table(headers: List[str], rows: List[List[Any]]) -> str:
    if not rows:
        return "（无）\n"
    lines = ["| " + " | ".join(str(h) for h in headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    for r in rows:
        lines.append("| " + " | ".join(str(v) for v in r) + " |")
    return "\n".join(lines) + "\n"


def render_markdown(result: Dict[str, Any]) -> str:
    db_path = result.get("db_path", "")
    crit = result.get("critical_items", [])
    cov = result.get("coverage", {})
    vc = result.get("view_compat", {})
    ds = result.get("dim_security", {})
    cal = result.get("dim_trade_calendar", {})
    fresh = result.get("runtime_freshness", {})
    di = result.get("date_integrity", {})
    schema = result.get("schema", {}).get("tables", {})
    fc = result.get("field_completeness", {})

    out: List[str] = []
    out.append("# V26.88 DuckDB 体检报告\n")
    out.append(f"- 数据库: `{db_path}`")
    out.append(f"- 今日(北京时间): `{fresh.get('today', '')}`")
    out.append(f"- 总体 ok: **`{result.get('ok')}`**")
    out.append(f"- Critical: {', '.join(crit) if crit else '无'}")

    # 1. 分层概览
    out.append(_md_section("1. 分层架构概览", _md_table(
        ["layer", "table", "kind", "rows", "status"],
        [
            [
                layer,
                tbl,
                ("view" if schema.get(tbl, {}).get("is_view") else "table"),
                schema.get(tbl, {}).get("rows", 0),
                ("OK" if schema.get(tbl, {}).get("exists") else "MISS"),
            ]
            for layer, tbls in LAYER_GROUPS.items()
            for tbl in tbls
        ],
    )))

    # 2. 字段覆盖
    out.append(_md_section("2. 字段覆盖", _md_table(
        ["项", "结果"],
        [
            ["legacy 列数", cov.get("legacy_column_count", 0)],
            ["view 列数", cov.get("view_column_count", 0)],
            ["兼容视图缺失字段", ", ".join(cov.get("missing_in_view", [])) or "无"],
            ["主表缺清单字段", ", ".join(cov.get("missing_in_legacy", [])) or "无"],
            ["兼容视图额外字段", ", ".join(cov.get("extra_in_view", [])) or "无"],
            ["主表额外字段", ", ".join(cov.get("extra_in_legacy", [])) or "无"],
            ["结论", "通过" if cov.get("ok") else "未通过"],
        ],
    )))

    # 3. dim_security
    out.append(_md_section("3. dim_security", _md_table(
        ["项", "值"],
        [
            ["severity", ds.get("severity")],
            ["rows_dim", ds.get("rows_dim")],
            ["rows_stock_basic", ds.get("rows_stock_basic")],
            ["missing_in_dim", ds.get("missing_in_dim")],
            ["missing_in_stock_basic", ds.get("missing_in_stock_basic")],
            ["reason", ds.get("reason", "") or ""],
        ],
    )))

    # 4. 日期完整性
    di_rows: List[List[Any]] = []
    for tbl, info in di.items():
        if not isinstance(info, dict):
            continue
        if not info.get("exists"):
            di_rows.append([tbl, "MISS", 0, 0, "", ""])
            continue
        di_rows.append([
            tbl,
            "OK",
            info.get("rows", 0),
            info.get("date_count", 0),
            f"{info.get('start','')} -> {info.get('end','')}",
            len(info.get("gaps", []) or []),
        ])
    out.append(_md_section("4. 日期完整性", _md_table(
        ["表", "exists", "rows", "dates", "范围", "gaps"],
        di_rows,
    )))

    # 5. view_compat
    out.append(_md_section("5. vw_daily_data_compat 抽样一致性", _md_table(
        ["项", "值"],
        [
            ["severity", vc.get("severity")],
            ["legacy_rows", vc.get("legacy_rows")],
            ["view_rows", vc.get("view_rows")],
            ["sample_diff_count", vc.get("sample_diff_count")],
            ["missing_key_columns_in_view", ", ".join(vc.get("missing_key_columns_in_view", []) or []) or "无"],
            ["reason", vc.get("reason", "") or ""],
        ],
    )))

    # 6. dim_trade_calendar
    out.append(_md_section("6. dim_trade_calendar", _md_table(
        ["项", "值"],
        [
            ["exists", cal.get("exists")],
            ["severity", cal.get("severity")],
            ["rows", cal.get("rows")],
            ["is_open_count", cal.get("is_open_count")],
            ["first_date", cal.get("first_date", "")],
            ["last_date", cal.get("last_date", "")],
            ["reason", cal.get("reason", "") or ""],
        ],
    )))

    # 7. 运行时时效
    fresh_rows: List[List[Any]] = []
    for tbl, info in (fresh.get("items") or {}).items():
        key_col = FRESHNESS_KEY.get(tbl, "")
        fresh_rows.append([
            tbl,
            info.get("severity"),
            info.get("rows"),
            f"{key_col}={info.get('max_date','')}",
            info.get("days_behind"),
            info.get("reason", "") or "",
        ])
    out.append(_md_section("7. 运行时表时效", _md_table(
        ["表", "severity", "rows", "最新事件", "距今(天)", "reason"], fresh_rows,
    )))

    # 8. 字段逐列完整性（只截前 40 行，太长）
    fc_rows = fc.get("rows", []) or []
    head = fc_rows[:40]
    out.append(_md_section(
        "8. daily_data 字段逐列完整性（前 40 行）",
        _md_table(
            ["字段", "完整天数", "总天数", "完整率(%)", "状态"],
            [[r["column"], r["valid_days"], r["total_days"], r["ratio"], r["status"]] for r in head],
        ) + (f"\n…另有 {len(fc_rows)-len(head)} 个字段省略\n" if len(fc_rows) > len(head) else ""),
    ))

    return "\n".join(out)


# ---------------------------------------------------------------------------
# 落盘
# ---------------------------------------------------------------------------

def _runtime_dir() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.basename(here).lower() == "tools":
        project_root = os.path.dirname(here)
    else:
        project_root = os.path.dirname(here)
    d = os.path.join(project_root, "data", "runtime")
    os.makedirs(d, exist_ok=True)
    return d


def _save_report(result: Dict[str, Any], out_dir: str, write_json: bool, write_md: bool) -> Dict[str, str]:
    paths: Dict[str, str] = {}
    if write_json:
        p = os.path.join(out_dir, "db_inspect_latest.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        paths["json"] = p
    if write_md:
        p = os.path.join(out_dir, "db_inspect_latest.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(render_markdown(result))
        paths["md"] = p
    return paths


# ---------------------------------------------------------------------------
# 连接
# ---------------------------------------------------------------------------

def _duckdb_is_locked_error(exc: BaseException) -> bool:
    """识别 duckdb 文件已被另一进程占用的锁错误。"""
    msg = str(exc).lower() if exc else ""
    return (
        "already open" in msg
        or "cannot open file" in msg
        or "另一进程" in msg
        or "lock" in msg
    )


def _open_connection(wait_sec: float = 30.0) -> duckdb.DuckDBPyConnection:
    """
    优先复用本进程的读连接单例；不可用则建立只读直连。
    若 duckdb 文件被另一进程占用（streamlit / daemon 等长连接场景），按指数退避轮询，
    wait_sec 内仍未解锁则抛出 duckdb.IOException（不静默失败）。
    """
    try:
        con = get_read_conn_singleton(max_wait_sec=max(0.0, min(wait_sec, 5.0)))
        if con is not None:
            return con
    except Exception:
        pass

    db_path = get_duckdb_path()
    deadline = time.time() + max(0.0, wait_sec)
    attempt = 0
    while True:
        try:
            return duckdb.connect(db_path, read_only=True)
        except Exception as e:
            if not _duckdb_is_locked_error(e) or time.time() >= deadline:
                msg = (
                    f"\n[DUCKDB LOCKED] 无法打开 {db_path}\n"
                    f"  原因: {e}\n"
                    f"  提示: duckdb 文件被另一进程（streamlit / daemon / ui）排他持有。\n"
                    f"  处理:\n"
                    f"    1) 停止占用进程后重跑；或\n"
                    f"    2) 使用 --wait-sec 调长等待时间；或\n"
                    f"    3) 在 streamlit 内部调用 tools.db_inspector.run_full_inspection()（同进程复用连接）。\n"
                )
                raise RuntimeError(msg) from e
            attempt += 1
            delay = min(8.0, 0.4 * (1.45 ** min(attempt, 14)))
            time.sleep(delay)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run_full_inspection(sample_rows: int = 500, today_str: str = "", wait_sec: float = 30.0,
                          con: Optional[duckdb.DuckDBPyConnection] = None) -> Dict[str, Any]:
    """
    若传入 con，则复用外部连接（不会重复打开，避免 read_only 锁冲突 / view 元数据丢失）；
    否则自动按 _open_connection(wait_sec) 建立。
    """
    db_path = get_duckdb_path()
    if not os.path.exists(db_path):
        return {"ok": False, "critical_items": ["db_missing"], "db_path": db_path}

    # 仅做视图兜底，不动业务数据
    try:
        ensure_v26_compat_view(force=True)
    except Exception:
        pass

    if not today_str:
        today_str = _now_bj_date_str()

    own_con = False
    if con is None:
        con = _open_connection(wait_sec=wait_sec)
        own_con = True
    try:
        _print_block("DuckDB 数据库全景体检报告 (V26.88)")
        print(f"数据库路径: {db_path}")
        print(f"今日(北京时间): {today_str}")

        _print_layer_summary(con)
        coverage = verify_coverage(con)
        dim_check = verify_dim_security_consistency(con)
        date_integrity = verify_date_integrity(con)
        view_check = verify_view_compat_consistency(con, sample_rows=sample_rows)
        cal_check = verify_dim_trade_calendar(con)
        fresh = verify_runtime_freshness(con, today_str)
        field_report = _build_field_completeness_report(con, "daily_data")
        degenerate = verify_critical_column_degeneracy(con)

        _print_coverage(coverage)
        _print_dim_check(dim_check)
        _print_date_integrity(date_integrity)
        _print_view_check(view_check)
        _print_dim_calendar(cal_check)
        _print_runtime_freshness(fresh)
        _print_field_completeness(field_report)
        _print_critical_column_degeneracy(degenerate)

        # 顶层汇总
        critical_items = []
        if coverage["missing_in_view"]:
            critical_items.append("coverage_missing_in_view")
        if view_check.get("severity") == SEV_CRIT:
            critical_items.append(f"view_compat:{view_check.get('reason') or 'sample_diff'}")
        if dim_check.get("severity") == SEV_CRIT:
            critical_items.append(f"dim_security:{dim_check.get('reason') or 'mismatch'}")
        if cal_check.get("severity") == SEV_CRIT:
            critical_items.append("dim_trade_calendar_missing")
        crits = [k for k, v in fresh["items"].items() if v.get("severity") == SEV_CRIT]
        if crits:
            critical_items.append("freshness:" + ",".join(crits))
        crit_cols = [k for k, v in degenerate["items"].items() if v.get("severity") == SEV_CRIT]
        if crit_cols:
            critical_items.append("degenerate_column:" + ",".join(crit_cols))
        ok = not critical_items

        result: Dict[str, Any] = {
            "ok": ok,
            "critical_items": critical_items,
            "db_path": db_path,
            "today": today_str,
            "coverage": coverage,
            "dim_security": dim_check,
            "date_integrity": date_integrity,
            "view_compat": view_check,
            "dim_trade_calendar": cal_check,
            "runtime_freshness": fresh,
            "field_completeness": field_report,
            "critical_column_degeneracy": degenerate,
            "schema": verify_schema_alignment(con),
        }
        _print_final(result)
        return result
    finally:
        if own_con:
            try:
                con.close()
            except Exception:
                pass


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="V26.88 DuckDB database inspector")
    parser.add_argument("--sample-rows", type=int, default=500, help="view_compat 抽样行数")
    parser.add_argument("--json", action="store_true", help="在 stdout 打印 JSON 结果")
    parser.add_argument("--md", action="store_true", help="在 stdout 打印 Markdown 结果")
    parser.add_argument("--out-dir", type=str, default="", help="落盘目录；默认 data/runtime/")
    parser.add_argument("--save", action="store_true", help="落盘到 out-dir (默认 json+md)")
    parser.add_argument("--save-json", action="store_true", help="仅落盘 JSON")
    parser.add_argument("--save-md", action="store_true", help="仅落盘 Markdown")
    parser.add_argument("--quiet", action="store_true", help="不打印控制台报告（仅写文件）")
    parser.add_argument("--wait-sec", type=float, default=30.0, help="duckdb 文件被占时等待秒数（默认 30s）")
    args = parser.parse_args(argv)

    # 是否落盘
    want_json = bool(args.save_json or args.save or args.json)
    want_md = bool(args.save_md or args.save or args.md)
    # 当显式 --json / --md 时，落盘但额外也把内容打到 stdout
    out_dir = args.out_dir or _runtime_dir()

    sample_rows = max(10, int(args.sample_rows))
    try:
        result = run_full_inspection(sample_rows=sample_rows, wait_sec=max(0.0, float(args.wait_sec)))
    except RuntimeError as e:
        # duckdb 锁死时给出明确指引，但不打印 JSON / Markdown
        print(str(e), file=sys.stderr)
        return 3

    if args.save_json or args.save or args.save_md or args.save:
        paths = _save_report(result, out_dir, want_json, want_md)
        for k, v in paths.items():
            print(f"[SAVED] {k.upper()} -> {v}")

    if args.json and not args.quiet:
        print()
        print("===== JSON RESULT =====")
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.md and not args.quiet:
        print()
        print("===== MARKDOWN RESULT =====")
        print(render_markdown(result))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
