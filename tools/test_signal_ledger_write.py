# -*- coding: utf-8 -*-
"""tools/test_signal_ledger_write.py
========================================================================
回归：signal_log → signal_strategy_ledger 写入链路（Tier 0/1 · B2 修复后）

背景
----
DuckDB 2.0.0：同一连接上若上一次 execute() 的结果集没被读完
（典型：execute(...).fetchone() 只取首行），后续 register()/unregister()
会抛 Resource In Use Error: connection has an unfinished result from execute()。
该异常发生在 write_strategy_ledger() 里被 try/except 吞掉，
表现为「signal_log 落库了，但战法账本一行没有」，战法有效性面板全空。

本脚本在 data/_qa 副本上验证：
    1) 写一条 P3 信号（带 2 个原子战法）→ signal_log +1 行
    2) signal_strategy_ledger +2 行，且 signal_uid / log_id 与 signal_log 一致
    3) 重复写同一信号（幂等）→ 不产生重复 ledger 行
    4) 全程无 'unfinished result' 警告

用法：python tools/test_signal_ledger_write.py
"""
from __future__ import annotations

import logging
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TEST_CODE = "000099.SZ"
TEST_NAME = "账本回归测试股"
TEST_POOL = "p3"
TEST_STRATS = ["P3-01", "P3-04"]


class _CatchWarnings(logging.Handler):
    """捕获 WARNING 级日志，用于断言 'unfinished result' 不再出现。"""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record):
        try:
            self.records.append(record.getMessage())
        except Exception:
            pass


def main() -> int:
    import tools._qa_env as qa

    if not os.path.exists(qa.QA_DB):
        print(f"[SKIP] 未找到 QA 副本 {qa.QA_DB}")
        print("       先执行：python tools/make_qa_db.py")
        return 0

    qa.boot()

    from data.db_core import get_read_conn, get_write_conn

    # 【2026-11 修复】业务日显式定为「副本里最近的交易日」：
    # signal_log 有「非交易日不落库」的数据卫生闸（周末跑测试会被拦），
    # 本测试验证的是账本写入链路，不是日历闸门，显式传 trade_date 绕开它。
    _td = None
    try:
        with get_read_conn(read_only=True) as _c_td:
            _r_td = _c_td.execute("select max(trade_date) from daily_data").fetchone()
            _td = str(_r_td[0]) if _r_td and _r_td[0] else None
    except Exception:
        _td = None
    if _td:
        print(f"  [INFO] 账本写入使用业务日 = {_td}（副本内最近交易日）")

    # --- 捕获 warning ---
    handler = _CatchWarnings()
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    prev_level = root_logger.level
    root_logger.setLevel(logging.WARNING)

    bad = 0
    try:
        with get_write_conn() as wc:
            wc.execute("DELETE FROM signal_log WHERE ts_code = ?", [TEST_CODE])
            wc.execute("DELETE FROM signal_strategy_ledger WHERE ts_code = ?", [TEST_CODE])

        def counts():
            with get_read_conn(read_only=True) as c:
                a = c.execute(
                    "SELECT COUNT(*) FROM signal_log WHERE ts_code = ?", [TEST_CODE]
                ).fetchone()[0]
                b = c.execute(
                    "SELECT COUNT(*) FROM signal_strategy_ledger WHERE ts_code = ?",
                    [TEST_CODE],
                ).fetchone()[0]
                return int(a), int(b)

        from core.pool_signal_adapter_v28 import append_signal_log_safe

        res = {
            "ts_code": TEST_CODE,
            "name": TEST_NAME,
            "burst_score": 88.0,
            "score": 88.0,
            "is_pass": True,
            "p3_core_screener_pass": True,
            "strategies": list(TEST_STRATS),
            "pre_close": 10.0,
            "rank": 1,
        }

        n0 = counts()
        append_signal_log_safe(dict(res), TEST_POOL, None, push_min_score=0.0,
                               rt={"price": 11.5}, trade_date=_td)
        n1 = counts()
        print(f"  signal_log {n0[0]} -> {n1[0]}；ledger {n0[1]} -> {n1[1]}")
        if n1[0] != n0[0] + 1:
            print("  [FAIL] signal_log 未增加 1 行")
            bad += 1
        else:
            print("  [OK  ] signal_log +1 行")
        if n1[1] != n0[1] + len(TEST_STRATS):
            print(f"  [FAIL] ledger 行数增量 {n1[1] - n0[1]} != {len(TEST_STRATS)}")
            bad += 1
        else:
            print(f"  [OK  ] ledger +{len(TEST_STRATS)} 行（每个原子战法一行）")

        # --- 一致性：signal_uid / log_id 与 signal_log 对齐 ---
        with get_read_conn(read_only=True) as c:
            log_rows = c.execute(
                "SELECT id, signal_uid FROM signal_log WHERE ts_code = ?", [TEST_CODE]
            ).fetchall()
            led_rows = c.execute(
                "SELECT signal_uid, log_id, strategy_id FROM signal_strategy_ledger "
                "WHERE ts_code = ? ORDER BY strategy_id",
                [TEST_CODE],
            ).fetchall()
        if not log_rows or not led_rows:
            print("  [FAIL] 取不到对照行")
            bad += 1
        else:
            log_id, log_uid = int(log_rows[0][0]), str(log_rows[0][1])
            uid_ok = all(str(r[0]) == log_uid for r in led_rows)
            id_ok = all(int(r[1]) == log_id for r in led_rows)
            ids = sorted(str(r[2]) for r in led_rows)
            print(f"  log_id={log_id} signal_uid={log_uid}")
            print(f"  ledger strategy_ids={ids}")
            if not uid_ok:
                print("  [FAIL] ledger.signal_uid 与 signal_log 不一致")
                bad += 1
            else:
                print("  [OK  ] ledger.signal_uid 一致")
            if not id_ok:
                print("  [FAIL] ledger.log_id 与 signal_log.id 不一致")
                bad += 1
            else:
                print("  [OK  ] ledger.log_id 一致")
            if ids != sorted(TEST_STRATS):
                print(f"  [FAIL] 战法集合不符：{ids} != {sorted(TEST_STRATS)}")
                bad += 1
            else:
                print("  [OK  ] 战法集合一致")

        # --- 幂等：重复写同一信号 ---
        append_signal_log_safe(dict(res), TEST_POOL, None, push_min_score=0.0,
                               rt={"price": 11.5}, trade_date=_td)
        n2 = counts()
        print(f"  重写后 signal_log {n2[0]}；ledger {n2[1]}")
        if n2[1] != n1[1]:
            print(f"  [FAIL] 重复写产生重复 ledger 行（{n1[1]} -> {n2[1]}）")
            bad += 1
        else:
            print("  [OK  ] 重复写幂等，ledger 未重复")

        # --- 无 unfinished result 警告 ---
        unfinished = [m for m in handler.records if "unfinished result" in m]
        if unfinished:
            print(f"  [FAIL] 仍出现 {len(unfinished)} 条 'unfinished result' 警告")
            for m in unfinished[:3]:
                print(f"         {m}")
            bad += 1
        else:
            print("  [OK  ] 无 'unfinished result' 警告")

        # --- 清理 ---
        with get_write_conn() as wc:
            wc.execute("DELETE FROM signal_log WHERE ts_code = ?", [TEST_CODE])
            wc.execute("DELETE FROM signal_strategy_ledger WHERE ts_code = ?", [TEST_CODE])
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(prev_level)

    print("\n总体：", "PASS" if bad == 0 else f"FAIL({bad})")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
