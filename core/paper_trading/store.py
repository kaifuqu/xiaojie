# -*- coding: utf-8 -*-
"""
core/paper_trading/store.py — 模拟盘 DuckDB 读写层

【职责】
  · 只做「读账户 / 读持仓 / 读写订单 / 写净值」的最小持久化封装。
  · 不含任何业务判断（买卖决策在 engine.py），避免职责混杂。
  · 全部走 data.db_core 的连接管理，遵守 DuckDB 全机单进程写约束。

【事务约定】
  引擎层的「下单 + 扣现金 + 写持仓/订单」必须在同一个 with 块内完成，
  由 store 提供 load_* / insert_* 原子操作配合使用。
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.paper_trading.schema import (
    ACCOUNT_ID,
    TABLE_ACCOUNT,
    TABLE_EQUITY,
    TABLE_ORDER,
    TABLE_POSITION,
    ensure_paper_tables,
)

_log = logging.getLogger(__name__)

BJ_TZ = timezone(timedelta(hours=8))

_write_lock = threading.RLock()


def now_bj() -> datetime:
    """北京时间（Asia/Shanghai 固定 +08:00）。"""
    return datetime.now(BJ_TZ)


def today_yyyymmdd() -> str:
    return now_bj().strftime("%Y%m%d")


def _rows(con: Any, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in (cur.description or [])]
    out: List[Dict[str, Any]] = []
    for r in cur.fetchall():
        out.append({c: r[i] for i, c in enumerate(cols)})
    return out


def _row(con: Any, sql: str, params: Optional[List[Any]] = None) -> Optional[Dict[str, Any]]:
    r = _rows(con, sql, params)
    return r[0] if r else None


# ==================== 账户 ====================

def ensure_tables(con: Any, *, migrate: bool = False) -> None:
    """建表（幂等）。migrate=True 时顺带做列迁移（只能在写连接上）。"""
    ensure_paper_tables(con, migrate=migrate)


def ensure_account(con: Any, *, initial_capital: float) -> Dict[str, Any]:
    """确保账户存在；不存在则以 initial_capital 建立。返回账户 dict。"""
    acct = _row(con, f"SELECT * FROM {TABLE_ACCOUNT} WHERE account_id = ?", [ACCOUNT_ID])
    if acct:
        return acct
    ts = datetime.now()
    con.execute(
        f"INSERT INTO {TABLE_ACCOUNT} "
        "(account_id, initial_capital, cash, status, start_date, start_cash, "
        " trading_day_index, last_equity_date, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            ACCOUNT_ID,
            float(initial_capital),
            float(initial_capital),
            "RUNNING",
            None,
            float(initial_capital),
            0,
            None,
            ts,
            ts,
        ],
    )
    return _row(con, f"SELECT * FROM {TABLE_ACCOUNT} WHERE account_id = ?", [ACCOUNT_ID]) or {}


def load_account(con: Any) -> Optional[Dict[str, Any]]:
    return _row(con, f"SELECT * FROM {TABLE_ACCOUNT} WHERE account_id = ?", [ACCOUNT_ID])


def update_account(
    con: Any,
    *,
    cash: Optional[float] = None,
    status: Optional[str] = None,
    start_date: Optional[str] = None,
    trading_day_index: Optional[int] = None,
    last_equity_date: Optional[str] = None,
) -> None:
    """按需更新账户字段（只更新传入项）。"""
    sets: List[str] = []
    params: List[Any] = []
    if cash is not None:
        sets.append("cash = ?")
        params.append(float(cash))
    if status is not None:
        sets.append("status = ?")
        params.append(str(status))
    if start_date is not None:
        sets.append("start_date = ?")
        params.append(str(start_date))
    if trading_day_index is not None:
        sets.append("trading_day_index = ?")
        params.append(int(trading_day_index))
    if last_equity_date is not None:
        sets.append("last_equity_date = ?")
        params.append(str(last_equity_date))
    if not sets:
        return
    sets.append("updated_at = ?")
    params.append(datetime.now())
    params.append(ACCOUNT_ID)
    con.execute(f"UPDATE {TABLE_ACCOUNT} SET {', '.join(sets)} WHERE account_id = ?", params)


# ==================== 持仓 ====================

def load_positions(con: Any) -> List[Dict[str, Any]]:
    """全部当前持仓（按买入时间升序）。"""
    return _rows(
        con,
        f"SELECT * FROM {TABLE_POSITION} WHERE account_id = ? ORDER BY buy_date, buy_time, code",
        [ACCOUNT_ID],
    )


def load_position(con: Any, code: str) -> Optional[Dict[str, Any]]:
    return _row(
        con,
        f"SELECT * FROM {TABLE_POSITION} WHERE account_id = ? AND code = ?",
        [ACCOUNT_ID, str(code)],
    )


def count_positions(con: Any) -> int:
    r = _row(con, f"SELECT COUNT(*) AS n FROM {TABLE_POSITION} WHERE account_id = ?", [ACCOUNT_ID])
    return int((r or {}).get("n") or 0)


def insert_position(con: Any, pos: Dict[str, Any]) -> None:
    cols = [
        "account_id", "code", "name", "pool", "strategy_ids", "strategy_names",
        "signal_score", "buy_date", "buy_time", "buy_price_raw", "buy_price_exec",
        "shares", "buy_amount", "buy_commission", "buy_stamp_tax", "buy_transfer_fee",
        "net_cost", "stop_loss_price", "take_profit_price", "last_price", "price_ts",
        "unrealized_pnl", "unrealized_pnl_pct", "entry_signal_json", "sizing_scheme",
        "created_at", "updated_at",
    ]
    ts = datetime.now()
    data = dict(pos)
    data.setdefault("account_id", ACCOUNT_ID)
    data.setdefault("created_at", ts)
    data["updated_at"] = ts
    payload = [data.get(c) for c in cols]
    ph = ", ".join(["?"] * len(cols))
    con.execute(
        f"INSERT INTO {TABLE_POSITION} ({', '.join(cols)}) VALUES ({ph})",
        payload,
    )


def update_position_quote(con: Any, code: str, *, last_price: float, price_ts: str) -> None:
    """只更新最新价与浮动盈亏（不改动成本与阈值字段）。"""
    pos = load_position(con, code)
    if not pos:
        return
    shares = int(pos.get("shares") or 0)
    net_cost = float(pos.get("net_cost") or 0.0)
    pnl = (float(last_price) * shares) - net_cost
    pnl_pct = (pnl / net_cost) if net_cost > 0 else 0.0
    con.execute(
        f"UPDATE {TABLE_POSITION} SET last_price = ?, price_ts = ?, unrealized_pnl = ?, "
        f"unrealized_pnl_pct = ?, updated_at = ? WHERE account_id = ? AND code = ?",
        [float(last_price), str(price_ts), pnl, pnl_pct, datetime.now(), ACCOUNT_ID, str(code)],
    )


def delete_position(con: Any, code: str) -> None:
    con.execute(
        f"DELETE FROM {TABLE_POSITION} WHERE account_id = ? AND code = ?",
        [ACCOUNT_ID, str(code)],
    )


# ==================== 订单流水 ====================

def insert_order(con: Any, order: Dict[str, Any]) -> None:
    """插入一条订单流水（只 append）。"""
    cols = [
        "order_id", "client_order_id", "account_id", "side", "order_ts", "trade_date",
        "pool", "code", "name", "strategy_ids", "strategy_names", "signal_score",
        "exec_price", "shares", "gross_amount", "commission", "stamp_tax", "transfer_fee",
        "total_fee", "cash_delta", "buy_price_raw", "pnl", "pnl_pct", "holding_days",
        "exit_reason", "skip_reason", "note", "sizing_scheme", "created_at",
    ]
    data = dict(order)
    data.setdefault("account_id", ACCOUNT_ID)
    data.setdefault("created_at", datetime.now())
    payload = [data.get(c) for c in cols]
    ph = ", ".join(["?"] * len(cols))
    con.execute(
        f"INSERT INTO {TABLE_ORDER} ({', '.join(cols)}) VALUES ({ph})",
        payload,
    )


def order_exists(con: Any, client_order_id: str) -> bool:
    r = _row(
        con,
        f"SELECT 1 AS x FROM {TABLE_ORDER} WHERE client_order_id = ? LIMIT 1",
        [str(client_order_id)],
    )
    return bool(r)


def load_orders(
    con: Any,
    *,
    side: Optional[str] = None,
    limit: int = 2000,
) -> List[Dict[str, Any]]:
    sql = f"SELECT * FROM {TABLE_ORDER} WHERE account_id = ?"
    params: List[Any] = [ACCOUNT_ID]
    if side:
        sql += " AND side = ?"
        params.append(str(side).upper())
    sql += " ORDER BY order_ts DESC, order_id DESC LIMIT ?"
    params.append(int(limit))
    return _rows(con, sql, params)


def load_sell_trades(con: Any, *, limit: int = 2000) -> List[Dict[str, Any]]:
    """已平仓交易（SELL 且有 pnl）。"""
    return _rows(
        con,
        f"SELECT * FROM {TABLE_ORDER} WHERE account_id = ? AND side = 'SELL' "
        f"AND pnl IS NOT NULL ORDER BY order_ts DESC LIMIT ?",
        [ACCOUNT_ID, int(limit)],
    )


def load_closed_positions(con: Any) -> List[Dict[str, Any]]:
    """历史平仓明细（由 SELL 流水还原；仅供 UI 与排行榜统计）。"""
    return load_sell_trades(con, limit=100000)


# ==================== 仓位口径校准（P0-5）====================
LEGACY_SCHEME_PREFIX = "legacy_"


def backfill_sizing_scheme(con: Any, cfg: Any) -> Dict[str, Any]:
    """给历史仓位/流水补口径标签，并把「口径切换期」的大仓位显式标成 legacy。

    【P0 修复 2026-11-13】背景：
      2026-11-04 单票口径从「总资产 20%」（约 19.7 万/只）切成「固定 5 万/只」。
      切换当天的 3 笔存量仓位（603667 / 000875 / 002738，各约 19.7 万）与之后的
      5 万仓位混在同一份「按战法归因」里统计：金额口径的盈亏被大仓位主导，
      百分比口径没问题，两者结论互相矛盾 —— 这就是口径漂移。

    处理原则（不删数据、不改成交价、不影响已实现盈亏）：
      1. sizing_scheme 为空的持仓/流水 → 按「建仓金额是否明显偏离当前目标」判定；
      2. 偏离 > 1.5 倍目标 → 标 legacy_pct（旧比例口径），否则标当前口径；
      3. 幂等：已有标签的不再改写（保留第一手事实）。
    """
    out: Dict[str, Any] = {"positions_tagged": 0, "orders_tagged": 0, "legacy_positions": 0}
    try:
        target = float(cfg.position_amount(0.0)) if hasattr(cfg, "position_amount") else 0.0
        if target <= 0:
            target = float(getattr(cfg, "fixed_amount_per_position", 50000.0) or 50000.0)
        current = f"{getattr(cfg, 'position_mode', 'fixed_amount')}:{target:g}"
        # 目标金额的多少倍以上算「明显不是当前口径」
        legacy_floor = target * 1.5

        rows = _rows(
            con,
            f"SELECT code, net_cost FROM {TABLE_POSITION} "
            f"WHERE account_id = ? AND (sizing_scheme IS NULL OR TRIM(sizing_scheme) = '')",
            [ACCOUNT_ID],
        )
        for r in rows:
            cost = float(r.get("net_cost") or 0.0)
            scheme = f"{LEGACY_SCHEME_PREFIX}pct" if cost >= legacy_floor else current
            con.execute(
                f"UPDATE {TABLE_POSITION} SET sizing_scheme = ?, updated_at = ? "
                f"WHERE account_id = ? AND code = ?",
                [scheme, datetime.now(), ACCOUNT_ID, str(r.get("code"))],
            )
            out["positions_tagged"] += 1
            if scheme.startswith(LEGACY_SCHEME_PREFIX):
                out["legacy_positions"] += 1

        orows = _rows(
            con,
            f"SELECT order_id, side, gross_amount FROM {TABLE_ORDER} "
            f"WHERE account_id = ? AND (sizing_scheme IS NULL OR TRIM(sizing_scheme) = '')",
            [ACCOUNT_ID],
        )
        for r in orows:
            amt = float(r.get("gross_amount") or 0.0)
            scheme = f"{LEGACY_SCHEME_PREFIX}pct" if amt >= legacy_floor else current
            con.execute(
                f"UPDATE {TABLE_ORDER} SET sizing_scheme = ? WHERE order_id = ?",
                [scheme, str(r.get("order_id"))],
            )
            out["orders_tagged"] += 1
        if out["positions_tagged"] or out["orders_tagged"]:
            _log.info(
                "[模拟盘] 仓位口径标签补齐：持仓 %d（其中 legacy %d）/ 流水 %d | 当前口径 %s",
                out["positions_tagged"], out["legacy_positions"], out["orders_tagged"], current,
            )
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘] 仓位口径校准失败（不影响交易）: %s", e)
        out["error"] = str(e)[:200]
    return out


# ==================== 日净值 ====================

def insert_or_update_equity(con: Any, row: Dict[str, Any]) -> None:
    cols = [
        "account_id", "trade_date", "cash", "market_value", "total_asset",
        "daily_pnl", "daily_pnl_pct", "cum_pnl", "cum_return", "drawdown_pct",
        "position_count", "trade_count", "total_fee", "created_at",
    ]
    data = dict(row)
    data.setdefault("account_id", ACCOUNT_ID)
    data.setdefault("created_at", datetime.now())
    payload = [data.get(c) for c in cols]
    ph = ", ".join(["?"] * len(cols))
    con.execute(
        f"INSERT OR REPLACE INTO {TABLE_EQUITY} ({', '.join(cols)}) VALUES ({ph})",
        payload,
    )


def load_equity_series(con: Any, *, limit: int = 500) -> List[Dict[str, Any]]:
    return _rows(
        con,
        f"SELECT * FROM {TABLE_EQUITY} WHERE account_id = ? ORDER BY trade_date DESC LIMIT ?",
        [ACCOUNT_ID, int(limit)],
    )


def latest_equity(con: Any) -> Optional[Dict[str, Any]]:
    return _row(
        con,
        f"SELECT * FROM {TABLE_EQUITY} WHERE account_id = ? ORDER BY trade_date DESC LIMIT 1",
        [ACCOUNT_ID],
    )


# ==================== 写连接封装 ====================

def write_transaction(fn):
    """装饰器：开写连接 + 建表 + 事务体。fn(con) 返回任意值。

    异常时 DuckDB 自动回滚该连接内未提交语句。
    """

    def wrapper(*args, **kwargs):
        from data.db_core import get_write_conn  # 局部 import，避免循环依赖

        with _write_lock:
            with get_write_conn() as con:
                con.execute("BEGIN TRANSACTION")
                try:
                    ensure_tables(con, migrate=True)
                    out = fn(con, *args, **kwargs)
                    con.execute("COMMIT")
                    return out
                except Exception:
                    try:
                        con.execute("ROLLBACK")
                    except Exception:  # noqa: BLE001
                        pass
                    raise

    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = fn.__doc__
    return wrapper


def read_query(fn):
    """装饰器：只读连接 + 建表只读检查。fn(con) 返回任意值。"""

    def wrapper(*args, **kwargs):
        from data.db_core import get_read_conn

        with get_read_conn(read_only=True) as con:
            try:
                ensure_tables(con)
            except Exception as e:  # noqa: BLE001
                _log.debug("模拟盘只读连接无法确保表存在（可能尚未初始化）: %s", e)
            return fn(con, *args, **kwargs)

    wrapper.__name__ = getattr(fn, "__name__", "wrapper")
    wrapper.__doc__ = fn.__doc__
    return wrapper


def dumps(obj: Any) -> str:
    """信号快照 JSON 序列化（DataFrame 等不可序列化对象退化为 repr）。"""
    return json.dumps(obj, ensure_ascii=False, default=str)


def _self_test() -> None:
    """内置自检：内存 DuckDB 走一遍建表→建账户→持仓→订单→净值。"""
    import duckdb

    con = duckdb.connect(":memory:")
    ensure_tables(con)
    acct = ensure_account(con, initial_capital=1000000.0)
    assert abs(float(acct["cash"]) - 1000000.0) < 1e-6, acct
    assert acct["status"] == "RUNNING"
    insert_position(
        con,
        {
            "code": "600000",
            "name": "浦发银行",
            "pool": "p3",
            "buy_date": "20261017",
            "buy_time": "10:00:00",
            "buy_price_raw": 10.0,
            "shares": 1000,
            "net_cost": 10005.1,
        },
    )
    assert count_positions(con) == 1
    update_position_quote(con, "600000", last_price=9.7, price_ts="2026-10-17 10:05:00")
    pos = load_position(con, "600000")
    assert abs(float(pos["unrealized_pnl"]) - (9.7 * 1000 - 10005.1)) < 1e-6, pos
    insert_order(
        con,
        {
            "order_id": "o1",
            "client_order_id": "c1",
            "side": "BUY",
            "order_ts": "2026-10-17 10:00:00",
            "trade_date": "20261017",
            "code": "600000",
            "shares": 1000,
        },
    )
    assert order_exists(con, "c1")
    insert_or_update_equity(
        con,
        {
            "trade_date": "20261017",
            "cash": 900000.0,
            "market_value": 9700.0,
            "total_asset": 909700.0,
        },
    )
    eq = latest_equity(con)
    assert eq["trade_date"] == "20261017"
    update_account(con, cash=900000.0, trading_day_index=1)
    assert int(load_account(con)["trading_day_index"]) == 1
    delete_position(con, "600000")
    assert count_positions(con) == 0
    con.close()
    print("[store] self-test OK | account+position+order+equity verified")


if __name__ == "__main__":
    _self_test()


__all__ = [
    "ensure_account",
    "ensure_tables",
    "count_positions",
    "load_account",
    "load_closed_positions",
    "load_equity_series",
    "load_orders",
    "load_position",
    "load_positions",
    "load_sell_trades",
    "latest_equity",
    "now_bj",
    "order_exists",
    "read_query",
    "today_yyyymmdd",
    "update_account",
    "write_transaction",
]
