# -*- coding: utf-8 -*-
"""
【V2 周线聚合工具】从 daily_data 聚合周线到 weekly_data（重写为 DuckDB 原生聚合版）

为什么重写：30 万行 × 2500 标的在 Python `groupby + rolling().mean()` 上跑 4 次 MA 约 4 分钟，
单次回填 60 周占用内存 ~500MB。改用 DuckDB 窗口函数在数据库侧聚合，秒级返回。

表结构 (V2)：
  - 主键: (week_end, ts_code)
  - 字段: open_w, high_w, low_w, close_w, vol_w, amount_w, pct_chg_w, is_up_week,
          ma5_w, ma10_w, ma20_w, ma60_w,
          ma20_w_4w_ago, ma60_w_4w_ago, vol_avg_5w,
          vol_w_prev1, vol_w_prev2, is_up_week_prev1

触发：
  - 每周一 09:00 自动跑一次
  - 历史回填：python tools/weekly_aggregate.py --history --weeks 60
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WEEKLY_AGG] %(levelname)s %(message)s",
)
logger = logging.getLogger("weekly_aggregate")

from data.db_core import get_duckdb_path, save_df_to_sql  # noqa: E402
# 【V26.88+10-14】复用 data.fetcher 懒初始化单例
from data.fetcher._client import _ensure_pro_initialized


# 周线 V2 全字段 DDL（DuckDB 自动从 DataFrame 建表，但这里显式声明便于人工核对）
WEEKLY_DDL = """
CREATE TABLE IF NOT EXISTS weekly_data (
    ts_code               VARCHAR NOT NULL,
    week_end              VARCHAR NOT NULL,
    open_w                DOUBLE,
    high_w                DOUBLE,
    low_w                 DOUBLE,
    close_w               DOUBLE,
    vol_w                 DOUBLE,
    amount_w              DOUBLE,
    pct_chg_w             DOUBLE,
    is_up_week            BOOLEAN,
    ma5_w                 DOUBLE,
    ma10_w                DOUBLE,
    ma20_w                DOUBLE,
    ma60_w                DOUBLE,
    ma20_w_4w_ago         DOUBLE,
    ma60_w_4w_ago         DOUBLE,
    vol_avg_5w            DOUBLE,
    vol_w_prev1           DOUBLE,
    vol_w_prev2           DOUBLE,
    is_up_week_prev1      BOOLEAN,
    PRIMARY KEY (week_end, ts_code)
)
"""


def _get_last_trade_date() -> str:
    """最近一个 A 股交易日（YYYY-MM-DD）。优先 trade_cal；回退 daily_data MAX。
    不抛异常（哪怕 token / yaml 损坏，仍可走 daily_data 路径）。
    """
    try:
        # 【V26.88+10-14】复用懒初始化（避免 init_pro() 创建多份独立 DataApi）
        pro = _ensure_pro_initialized()
        if pro is not None:
            end_dt = datetime.now().strftime("%Y%m%d")
            start_dt = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
            cal = pro.trade_cal(exchange="SSE", is_open="1", start_date=start_dt, end_date=end_dt)
            if cal is not None and not cal.empty:
                last = str(cal["cal_date"].iloc[-1])
                return f"{last[:4]}-{last[4:6]}-{last[6:8]}"
    except Exception as e:
        logger.warning("trade_cal 接口失败，回退 daily_data: %s", e)
    db_path = get_duckdb_path()
    import duckdb as _duck
    with _duck.connect(str(db_path), read_only=True) as con:
        row = con.execute("SELECT MAX(trade_date) FROM daily_data").fetchone()
    if not row or not row[0]:
        raise RuntimeError("daily_data 为空")
    return str(row[0])


def _ensure_weekly_table(db_path: str) -> None:
    import duckdb as _duck

    with _duck.connect(str(db_path), read_only=False) as con:
        con.execute(WEEKLY_DDL)


def _aggregate_weekly_sql(db_path: str, weeks: int) -> int:
    """用 DuckDB 原生窗口函数在数据库侧聚合周线（性能：30万行 ~3s）。

    返回：写入 weekly_data 的行数。
    注：MA60 至少需要 60 个有效周数据。当库中有效周不足 60 时，该标的跳过；
    此时 ``weekly_data`` 仅写入 MA60 已满的标的，MA60 短期缺失标的 P1 打分时
    ``_fetch_weekly_row`` 返回空 dict，安全回退到 trend=0 / vol=1（平量）。
    """
    import duckdb as _duck

    weeks = max(weeks, 12)
    buf_days = weeks * 5 + 30
    ma_window = min(60, weeks)  # MA 窗口不能超过样本周数

    ma60_clause = (
        f"AVG(close_w) OVER (PARTITION BY ts_code ORDER BY week_end ROWS BETWEEN {ma_window - 1} PRECEDING AND CURRENT ROW) AS ma60_w"
        if ma_window >= 30
        else "CAST(NULL AS DOUBLE) AS ma60_w"
    )
    ma20_clause = (
        f"AVG(close_w) OVER (PARTITION BY ts_code ORDER BY week_end ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS ma20_w"
        if weeks >= 20
        else "CAST(NULL AS DOUBLE) AS ma20_w"
    )
    ma10_clause = (
        f"AVG(close_w) OVER (PARTITION BY ts_code ORDER BY week_end ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) AS ma10_w"
        if weeks >= 10
        else "CAST(NULL AS DOUBLE) AS ma10_w"
    )
    ma5_clause = (
        f"AVG(close_w) OVER (PARTITION BY ts_code ORDER BY week_end ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS ma5_w"
        if weeks >= 5
        else "CAST(NULL AS DOUBLE) AS ma5_w"
    )
    ma20_4w_shift_clause = (
        "LAG(ma20_w, 4) OVER (PARTITION BY ts_code ORDER BY week_end) AS ma20_w_4w_ago"
        if weeks >= 24
        else "CAST(NULL AS DOUBLE) AS ma20_w_4w_ago"
    )
    ma60_4w_shift_clause = (
        "LAG(ma60_w, 4) OVER (PARTITION BY ts_code ORDER BY week_end) AS ma60_w_4w_ago"
        if ma_window >= 64
        else "CAST(NULL AS DOUBLE) AS ma60_w_4w_ago"
    )
    vol_avg_clause = (
        f"AVG(vol_w) OVER (PARTITION BY ts_code ORDER BY week_end ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS vol_avg_5w"
        if weeks >= 5
        else "CAST(NULL AS DOUBLE) AS vol_avg_5w"
    )

    sql = f"""
    WITH d AS (
        SELECT
            ts_code,
            CAST(trade_date AS DATE) AS d,
            CAST(open   AS DOUBLE)  AS open,
            CAST(high   AS DOUBLE)  AS high,
            CAST(low    AS DOUBLE)  AS low,
            CAST(close  AS DOUBLE)  AS close,
            CAST(vol    AS DOUBLE)  AS vol,
            CAST(amount AS DOUBLE)  AS amount,
            STRFTIME(
                DATE_TRUNC('week', CAST(trade_date AS DATE)) + INTERVAL '4 days',
                '%Y%m%d'
            ) AS week_end
        FROM daily_data
        WHERE REPLACE(CAST(trade_date AS VARCHAR), '-', '') >= STRFTIME(CURRENT_DATE - INTERVAL '{buf_days} DAY', '%Y%m%d')
          AND close IS NOT NULL
    ),
    agg AS (
        SELECT
            ts_code,
            week_end,
            FIRST(open  ORDER BY d) AS open_w,
            MAX(high)                 AS high_w,
            MIN(low)                  AS low_w,
            LAST(close ORDER BY d)    AS close_w,
            SUM(vol)                  AS vol_w,
            SUM(amount)               AS amount_w,
            MAX(d)                    AS _last_d
        FROM d
        GROUP BY ts_code, week_end
    ),
    pct AS (
        SELECT
            ts_code, week_end,
            close_w, vol_w, _last_d,
            (close_w / NULLIF(LAG(close_w) OVER (PARTITION BY ts_code ORDER BY week_end), 0) - 1.0) * 100.0 AS pct_chg_w,
            close_w > LAG(close_w) OVER (PARTITION BY ts_code ORDER BY week_end) AS is_up_week
        FROM agg
    ),
    ma AS (
        SELECT
            ts_code, week_end, close_w, vol_w, _last_d, pct_chg_w, is_up_week,
            {ma5_clause},
            {ma10_clause},
            {ma20_clause},
            {ma60_clause}
        FROM pct
    ),
    shift AS (
        SELECT *,
            {ma20_4w_shift_clause},
            {ma60_4w_shift_clause},
            {vol_avg_clause},
            LAG(vol_w, 1) OVER (PARTITION BY ts_code ORDER BY week_end) AS vol_w_prev1,
            LAG(vol_w, 2) OVER (PARTITION BY ts_code ORDER BY week_end) AS vol_w_prev2,
            LAG(is_up_week, 1) OVER (PARTITION BY ts_code ORDER BY week_end) AS is_up_week_prev1
        FROM ma
    )
    SELECT
        ts_code, week_end, close_w, vol_w, _last_d, pct_chg_w, is_up_week,
        ma5_w, ma10_w, ma20_w, ma60_w,
        ma20_w_4w_ago, ma60_w_4w_ago,
        vol_avg_5w, vol_w_prev1, vol_w_prev2, is_up_week_prev1
    FROM shift
    ORDER BY ts_code, week_end
    """

    with _duck.connect(str(db_path), read_only=True) as con:
        df = con.execute(sql).fetchdf()
    if df.empty:
        return 0

    # 拼回 open/high/low/amount_w（前面用 FIRST/MAX/MIN/SUM 已算，这里 join 回去）
    with _duck.connect(str(db_path), read_only=True) as con:
        agg = con.execute(
            f"""
            SELECT
                ts_code,
                STRFTIME(
                    DATE_TRUNC('week', CAST(trade_date AS DATE)) + INTERVAL '4 days',
                    '%Y%m%d'
                ) AS week_end,
                FIRST(open ORDER BY trade_date) AS open_w,
                MAX(high)   AS high_w,
                MIN(low)    AS low_w,
                SUM(amount) AS amount_w
            FROM daily_data
            WHERE REPLACE(CAST(trade_date AS VARCHAR), '-', '') >= STRFTIME(CURRENT_DATE - INTERVAL '{buf_days} DAY', '%Y%m%d')
              AND close IS NOT NULL
            GROUP BY ts_code, week_end
            """
        ).fetchdf()
    import pandas as pd

    df = df.merge(
        agg,
        on=["ts_code", "week_end"],
        how="left",
        suffixes=("", "_ohlc"),
    )

    # 落库（save_df_to_sql 自带 UPSERT）
    if "pct_chg_w" in df.columns:
        df["pct_chg_w"] = df["pct_chg_w"].fillna(0.0)
    df["is_up_week"] = df["is_up_week"].fillna(False).astype(bool)
    if "is_up_week_prev1" in df.columns:
        df["is_up_week_prev1"] = df["is_up_week_prev1"].fillna(False).astype(bool)

    cols = [
        "ts_code", "week_end",
        "open_w", "high_w", "low_w", "close_w", "vol_w", "amount_w",
        "pct_chg_w", "is_up_week",
        "ma5_w", "ma10_w", "ma20_w", "ma60_w",
        "ma20_w_4w_ago", "ma60_w_4w_ago",
        "vol_avg_5w", "vol_w_prev1", "vol_w_prev2", "is_up_week_prev1",
    ]
    df = df[cols].copy()
    df = df.drop_duplicates(subset=["ts_code", "week_end"], keep="last")

    # 先清空（避免 UPSERT 时 PK 不全时部分 UPDATE）
    with _duck.connect(str(db_path), read_only=False) as con:
        con.execute("DELETE FROM weekly_data")
    save_df_to_sql(df, table_name="weekly_data", if_exists="append")
    return len(df)


def run_history(weeks: int) -> None:
    db_path = get_duckdb_path()
    _ensure_weekly_table(db_path)
    n = _aggregate_weekly_sql(db_path, weeks)
    logger.info("历史回填完成：写入 %d 行；最新 week_end 详见 SELECT MAX(week_end)", n)


def run_incremental() -> None:
    db_path = get_duckdb_path()
    _ensure_weekly_table(db_path)
    # 增量 = 跑过去 60 周窗口（窗口内的周次都会被重算）
    n = _aggregate_weekly_sql(db_path, weeks=60)
    logger.info("增量刷新完成：重算 %d 行", n)


def main():
    p = argparse.ArgumentParser(description="V2 周线聚合工具")
    p.add_argument("--history", action="store_true", help="历史回填模式")
    p.add_argument("--weeks", type=int, default=60, help="回填周数（默认 60）")
    args = p.parse_args()

    if args.history:
        run_history(args.weeks)
    else:
        run_incremental()


if __name__ == "__main__":
    main()