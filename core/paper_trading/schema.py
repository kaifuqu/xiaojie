# -*- coding: utf-8 -*-
"""
core/paper_trading/schema.py — 模拟盘 DuckDB 表结构

【四张表】
  paper_account      单例账户（资金、状态、实验进度）
  paper_position     当前持仓（只存未平仓）
  paper_order        不可变流水账（只 append，用于审计与对账）
  paper_equity_daily 每日一行收盘净值

【设计约束】
  · 与主库同库（quant_data.duckdb），沿用 data.db_core 的单写者连接。
  · paper_order.client_order_id 建唯一索引 → 幂等兜底。
  · 建表幂等（IF NOT EXISTS），可在 daemon 启动时安全重复执行。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

ACCOUNT_ID = "PAPER_MAIN"

TABLE_ACCOUNT = "paper_account"
TABLE_POSITION = "paper_position"
TABLE_ORDER = "paper_order"
TABLE_EQUITY = "paper_equity_daily"

ALL_TABLES = (TABLE_ACCOUNT, TABLE_POSITION, TABLE_ORDER, TABLE_EQUITY)

_DDL_ACCOUNT = f"""
CREATE TABLE IF NOT EXISTS {TABLE_ACCOUNT} (
    account_id VARCHAR PRIMARY KEY,
    initial_capital DOUBLE,
    cash DOUBLE,
    status VARCHAR,
    start_date VARCHAR,
    start_cash DOUBLE,
    trading_day_index INTEGER,
    last_equity_date VARCHAR,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
)
"""

_DDL_POSITION = f"""
CREATE TABLE IF NOT EXISTS {TABLE_POSITION} (
    account_id VARCHAR,
    code VARCHAR,
    name VARCHAR,
    pool VARCHAR,
    strategy_ids VARCHAR,
    strategy_names VARCHAR,
    signal_score DOUBLE,
    buy_date VARCHAR,
    buy_time VARCHAR,
    buy_price_raw DOUBLE,
    buy_price_exec DOUBLE,
    shares INTEGER,
    buy_amount DOUBLE,
    buy_commission DOUBLE,
    buy_stamp_tax DOUBLE,
    buy_transfer_fee DOUBLE,
    net_cost DOUBLE,
    stop_loss_price DOUBLE,
    take_profit_price DOUBLE,
    last_price DOUBLE,
    price_ts VARCHAR,
    unrealized_pnl DOUBLE,
    unrealized_pnl_pct DOUBLE,
    entry_signal_json VARCHAR,
    -- 【P0 修复 2026-11-13】仓位口径标签：fixed_amount:50000 / pct:20 等。
    -- 背景：2026-11-04 从「总资产20%」切到「固定5万/只」时，存量 3 笔仓位
    -- 仍是 19.7 万/只却与 5 万的仓位混在同一份归因里算收益，
    -- 一笔大仓位的金额收益会把整体结论带偏（口径漂移）。
    sizing_scheme VARCHAR,
    created_at TIMESTAMP,
    updated_at TIMESTAMP,
    PRIMARY KEY (account_id, code)
)
"""

_DDL_ORDER = f"""
CREATE TABLE IF NOT EXISTS {TABLE_ORDER} (
    order_id VARCHAR,
    client_order_id VARCHAR,
    account_id VARCHAR,
    side VARCHAR,
    order_ts VARCHAR,
    trade_date VARCHAR,
    pool VARCHAR,
    code VARCHAR,
    name VARCHAR,
    strategy_ids VARCHAR,
    strategy_names VARCHAR,
    signal_score DOUBLE,
    exec_price DOUBLE,
    shares INTEGER,
    gross_amount DOUBLE,
    commission DOUBLE,
    stamp_tax DOUBLE,
    transfer_fee DOUBLE,
    total_fee DOUBLE,
    cash_delta DOUBLE,
    buy_price_raw DOUBLE,
    pnl DOUBLE,
    pnl_pct DOUBLE,
    holding_days INTEGER,
    exit_reason VARCHAR,
    skip_reason VARCHAR,
    note VARCHAR,
    -- 【P0 修复 2026-11-13】沿用建仓时的仓位口径，平仓流水也能按口径分组归因
    sizing_scheme VARCHAR,
    created_at TIMESTAMP,
    PRIMARY KEY (order_id)
)
"""

_DDL_EQUITY = f"""
CREATE TABLE IF NOT EXISTS {TABLE_EQUITY} (
    account_id VARCHAR,
    trade_date VARCHAR,
    cash DOUBLE,
    market_value DOUBLE,
    total_asset DOUBLE,
    daily_pnl DOUBLE,
    daily_pnl_pct DOUBLE,
    cum_pnl DOUBLE,
    cum_return DOUBLE,
    drawdown_pct DOUBLE,
    position_count INTEGER,
    trade_count INTEGER,
    total_fee DOUBLE,
    created_at TIMESTAMP,
    PRIMARY KEY (account_id, trade_date)
)
"""


def ensure_paper_tables(con: Any, *, migrate: bool = False) -> None:
    """建四张表 + 幂等索引。DDL 幂等，可重复调用。

    migrate=True（仅在写连接上）时额外执行向后兼容的列迁移：
    老库的 paper_position / paper_order 没有 sizing_scheme 列，
    只读连接上执行 ALTER 会直接报错，故默认 False，由 store.write_transaction 传 True。
    """
    con.execute(_DDL_ACCOUNT)
    con.execute(_DDL_POSITION)
    con.execute(_DDL_ORDER)
    con.execute(_DDL_EQUITY)
    if migrate:
        _migrate_columns(con)
    con.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS idx_paper_order_client_id "
        f"ON {TABLE_ORDER}(client_order_id)"
    )
    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_paper_order_code_date "
        f"ON {TABLE_ORDER}(code, trade_date)"
    )
    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_paper_position_pool "
        f"ON {TABLE_POSITION}(pool)"
    )


def _migrate_columns(con: Any) -> None:
    """幂等列迁移（只在写连接上调用）。全部 IF NOT EXISTS，重复执行安全。"""
    for ddl in (
        f"ALTER TABLE {TABLE_POSITION} ADD COLUMN IF NOT EXISTS sizing_scheme VARCHAR",
        f"ALTER TABLE {TABLE_ORDER} ADD COLUMN IF NOT EXISTS sizing_scheme VARCHAR",
    ):
        try:
            con.execute(ddl)
        except Exception as e:  # noqa: BLE001
            _log.warning("[模拟盘] 列迁移跳过（%s）: %s", ddl.split("ADD COLUMN")[-1].strip(), e)


def _rows(con: Any, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
    """小工具：查询并转成 dict 列表（避免各模块重复写 cursor.description 解析）。"""
    cur = con.execute(sql, params or [])
    cols = [d[0] for d in (cur.description or [])]
    out: List[Dict[str, Any]] = []
    for r in cur.fetchall():
        out.append({c: r[i] for i, c in enumerate(cols)})
    return out


def _self_test() -> None:
    """内置自检：内存 DuckDB 建表 + 幂等二次执行 + 唯一索引拦截重复。"""
    import duckdb

    con = duckdb.connect(":memory:")
    ensure_paper_tables(con, migrate=True)
    ensure_paper_tables(con, migrate=True)
    names = {
        r[0]
        for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()
    }
    missing = set(ALL_TABLES) - names
    assert not missing, f"缺表: {missing}"
    con.execute(
        f"INSERT INTO {TABLE_ORDER} "
        "(order_id, client_order_id, account_id, side, order_ts, trade_date, code, shares) "
        "VALUES ('o1','c1','PAPER_MAIN','BUY','2026-10-17 10:00:00','20261017','600000',100)"
    )
    try:
        con.execute(
            f"INSERT INTO {TABLE_ORDER} "
            "(order_id, client_order_id, account_id, side, order_ts, trade_date, code, shares) "
            "VALUES ('o2','c1','PAPER_MAIN','BUY','2026-10-17 10:00:00','20261017','600000',100)"
        )
        raise AssertionError("唯一索引未拦截重复 client_order_id")
    except duckdb.ConstraintException:
        pass
    row = _rows(con, f"SELECT order_id, side, code, shares FROM {TABLE_ORDER}")
    assert row and row[0]["order_id"] == "o1", row
    con.close()
    print("[schema] self-test OK | 4 tables + unique index verified")


if __name__ == "__main__":
    _self_test()


__all__ = [
    "ACCOUNT_ID",
    "ALL_TABLES",
    "TABLE_ACCOUNT",
    "TABLE_EQUITY",
    "TABLE_ORDER",
    "TABLE_POSITION",
    "ensure_paper_tables",
]
