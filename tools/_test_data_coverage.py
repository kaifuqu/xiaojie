# -*- coding: utf-8 -*-
"""V30.4 覆盖率验收：daily_data 派生列 + feat_daily_* 分层 + vma 映射 + 筹码列

运行：_pyenv\\venv\\Scripts\\python.exe tools\\_test_data_coverage.py
只读主库，不做任何写操作。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core._pyarrow_compat import disable_pyarrow

disable_pyarrow()

from data.db_core import get_read_conn_singleton

DERIVED_KEY = ["ma20", "macd", "rsi_14", "capital_resonance_score", "fund_memory_score"]
NULL_PROBE = ["atr_pct", "high_20", "low_60", "bias_20", "kdj_k", "boll_upper", "cci"]
DAYS = ["20261013", "20261014", "20261015", "20261016"]
LATEST = "20261016"
VMA_MAP = [("vma5", "vol_ma5"), ("vma10", "vol_ma10"), ("vma20", "vol_ma20")]

PASSED = 0
FAILED: list = []


def ck(name, cond, extra=""):
    global PASSED
    if cond:
        PASSED += 1
        print("  [PASS] %s %s" % (name, extra))
    else:
        FAILED.append((name, extra))
        print("  [FAIL] %s %s" % (name, extra))


def main():
    con = get_read_conn_singleton(max_wait_sec=30.0)
    if con is None:
        print("[FAIL] 无法获取主库只读连接")
        return 1

    print("== 1. daily_data 最近 4 个交易日派生列覆盖 ==")
    for d in DAYS:
        cols = ", ".join('COUNT("%s") AS "%s"' % (c, c) for c in DERIVED_KEY)
        r = con.execute(
            "SELECT COUNT(*) AS n, %s FROM daily_data "
            "WHERE REPLACE(CAST(trade_date AS VARCHAR), '-', '') = '%s'" % (cols, d)
        ).fetchdf().iloc[0]
        n = int(r["n"])
        ck("%s 行数>0" % d, n > 0, "n=%d" % n)
        miss = [c for c in DERIVED_KEY if int(r[c]) != n]
        ck("%s 五项关键派生列全覆盖" % d, not miss, "缺失=%s" % miss)

    print("== 2. 最新交易日技术派生列无 NULL ==")
    for c in NULL_PROBE:
        n = con.execute(
            'SELECT COUNT(*) FROM daily_data '
            "WHERE REPLACE(CAST(trade_date AS VARCHAR), '-', '') = '%s' AND \"%s\" IS NULL" % (LATEST, c)
        ).fetchone()[0]
        ck("%s %s 无 NULL" % (LATEST, c), int(n) == 0, "nulls=%d" % int(n))

    print("== 3. feat_daily_* 分层表最新日覆盖 ==")
    dd_latest = con.execute(
        "SELECT COUNT(*) FROM daily_data WHERE REPLACE(CAST(trade_date AS VARCHAR), '-', '') = '%s'" % LATEST
    ).fetchone()[0]
    for t in ("feat_daily_core", "feat_daily_capital", "feat_daily_memory"):
        n = con.execute(
            "SELECT COUNT(*) FROM %s WHERE REPLACE(CAST(trade_date AS VARCHAR), '-', '') = '%s'" % (t, LATEST)
        ).fetchone()[0]
        ck("%s %s 行数对齐 daily_data" % (t, LATEST), int(n) == int(dd_latest),
           "rows=%d vs daily=%d" % (int(n), int(dd_latest)))

    print("== 4. vma* -> vol_ma* 映射一致性 ==")
    join_on = (
        "JOIN daily_data d ON f.ts_code = d.ts_code AND "
        "REPLACE(CAST(f.trade_date AS VARCHAR), '-', '') = REPLACE(CAST(d.trade_date AS VARCHAR), '-', '')"
    )
    for vm, dm in VMA_MAP:
        mism = con.execute(
            'SELECT COUNT(*) FROM feat_daily_core f %s '
            'WHERE f."%s" IS NOT NULL AND d."%s" IS NOT NULL AND ABS(f."%s" - d."%s") > 0.000001'
            % (join_on, vm, dm, vm, dm)
        ).fetchone()[0]
        tot = con.execute(
            'SELECT COUNT(*) FROM feat_daily_core f %s WHERE f."%s" IS NOT NULL' % (join_on, vm)
        ).fetchone()[0]
        ck("feat_daily_core.%s == daily_data.%s" % (vm, dm), int(mism) == 0 and int(tot) > 0,
           "mism=%d/%d" % (int(mism), int(tot)))
        nn = con.execute('SELECT COUNT(*) FROM feat_daily_core WHERE "%s" IS NULL' % vm).fetchone()[0]
        tot2 = con.execute("SELECT COUNT(*) FROM feat_daily_core").fetchone()[0]
        ck("feat_daily_core.%s 非 NULL 占比 >= 90%%" % vm, int(tot2) > 0 and int(nn) <= int(tot2) * 0.10,
           "null=%d/%d" % (int(nn), int(tot2)))

    print("== 5. 筹码列真实值体检（只报告，不判定）==")
    r = con.execute(
        'SELECT COUNT(*) AS n, COUNT(CASE WHEN "cost_50th" > 0 THEN 1 END) AS pos '
        "FROM daily_data WHERE REPLACE(CAST(trade_date AS VARCHAR), '-', '') = '%s'" % LATEST
    ).fetchdf().iloc[0]
    print("  [INFO] %s cost_50th>0 行数 = %s / %s" % (LATEST, r["pos"], r["n"]))
    print("  [INFO] 0 值代表历史一次性补 0 的伪值，需真实 pro.cyq_perf 补抓")

    print("")
    print("  PASS = %d" % PASSED)
    print("  FAIL = %d" % len(FAILED))
    for nme, ex in FAILED:
        print("    - %s %s" % (nme, ex))
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
