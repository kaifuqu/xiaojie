# -*- coding: utf-8 -*-
"""
scripts/migrate_signal_log_tscode.py  (一次性迁移脚本，幂等)
========================================================================
把 signal_log / signal_strategy_ledger 里【存量】的裸 6 位 ts_code
统一迁成带交易所后缀（000001.SZ / 600000.SH / 920xxx.BJ），与 daily_data 对齐。

【为什么必须做】
生产库实测（2026-10-21）：
  · daily_data.ts_code    227158/227158 行【全部带后缀】
  · signal_log.ts_code      120/120 行【全部裸码】  ← 不一致
  · JOIN signal_log × daily_data（现状）命中 = 0 行
  · JOIN signal_log × daily_data（补后缀后）命中 = 16454 行
也就是说：T+N 收益回看、战法有效性统计、胜率归因，对这 120 条存量候选
【全部 join miss】——它们被当成"没数据"而静默丢弃。

【红线】
  · 只改 signal_log.ts_code / signal_strategy_ledger.ts_code 两列
  · 不改 score / strategy / trade_date / pushed 等任何其它列
  · 幂等：已带后缀的行原样跳过
  · 迁移前自动备份原 ts_code 到 signal_log_tscode_backup 表
  · 不删任何行
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import duckdb

from core.signal_ledger_bridge import with_exchange_suffix

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(ROOT, "data", "quant_data.duckdb")


def migrate(table: str, con) -> int:
    cols = {r[1] for r in con.execute(f"PRAGMA table_info('{table}')").fetchall()}
    if "ts_code" not in cols:
        print(f"  [SKIP] {table} 无 ts_code 列")
        return 0
    rows = con.execute(f"SELECT ts_code FROM '{table}'").fetchall()
    mapping = {}
    for (raw,) in rows:
        if raw is None:
            continue
        s = str(raw).strip()
        new = with_exchange_suffix(s)
        if new and new != s:
            mapping[s] = new
    if not mapping:
        print(f"  [OK] {table} 已是标准格式，无需迁移")
        return 0

    # 备份原值（DDL 在 duckdb 会自动提交，无需显式 COMMIT）
    # 备份表按源表分名：两张表列数不同，共用一张会报 Binder Error
    backup = f"{table}_tscode_backup"
    con.execute(
        f"CREATE TABLE IF NOT EXISTS {backup} AS "
        f"SELECT * FROM '{table}' WHERE FALSE"
    )
    con.execute(f"DELETE FROM {backup}")
    con.execute(f"INSERT INTO {backup} SELECT * FROM '{table}'")

    n = 0
    for old, new in mapping.items():
        # 幂等：只在目标值仍等于旧值时才改
        cur = con.execute(
            f"UPDATE '{table}' SET ts_code = ? WHERE ts_code = ? AND ts_code <> ?",
            [new, old, new],
        )
        n += cur.fetchone()[0]
    print(f"  [OK] {table} 迁移 {len(mapping)} 个代码，{n} 行更新")
    return n


def main() -> int:
    print("=" * 64)
    print("signal_log / signal_strategy_ledger ts_code 归一迁移")
    print("=" * 64)
    con = duckdb.connect(DB)
    try:
        for t in ("signal_log", "signal_strategy_ledger"):
            print(f"[{t}]")
            migrate(t, con)

        # 校验
        print("\n=== 校验 ===")
        for t in ("signal_log", "signal_strategy_ledger"):
            try:
                total = con.execute(f"SELECT COUNT(*) FROM '{t}'").fetchone()[0]
                bare = con.execute(
                    f"SELECT COUNT(*) FROM '{t}' WHERE ts_code NOT LIKE '%.%'").fetchone()[0]
                j = con.execute(
                    f"SELECT COUNT(*) FROM '{t}' s JOIN daily_data d ON s.ts_code = d.ts_code"
                ).fetchone()[0]
                print(f"  {t}: 总 {total} 行 / 裸码剩余 {bare} / 与 daily_data join 命中 {j}")
            except Exception as e:
                print(f"  {t}: 校验跳过 ({e})")
    finally:
        con.close()
    print("\n完成 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
