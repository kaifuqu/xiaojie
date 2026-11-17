# -*- coding: utf-8 -*-
"""
tools/_fix_sync_today.py —— 补齐缺失交易日日线（临时运维脚本）

背景 2026-11-05：20:10~20:15 Tushare 接口（api.waditu.com）临时不可达，
trade_cal 拉取失败 → 交易日历为空 → 被当成「跳过」→ 上层写成「晚间增量同步完成」，
结果 daily_data 缺 20261105，但 last_sync_ok_bj_date 记成了 20261105。
本脚本在接口恢复后把缺口补齐，并核验结果。
"""
import os
import sys

sys.stdout.reconfigure(errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 5


def main() -> int:
    from data.db_core import get_read_conn
    from data.fetcher import sync_recent_days

    def _status(msg):
        print("  [sync]", str(msg)[:160])

    print(f"=== 补日线：最近 {DAYS} 个交易日 ===")
    try:
        res = sync_recent_days(days=DAYS, status_callback=_status)
    except Exception as e:
        print("!! sync_recent_days 抛异常:", type(e).__name__, str(e)[:300])
        res = {}
    print("  downloaded:", (res or {}).get("downloaded"))
    print("  failed    :", (res or {}).get("failed"))

    with get_read_conn() as c:
        rows = c.execute(
            "SELECT trade_date, count(*) FROM daily_data GROUP BY trade_date ORDER BY trade_date DESC LIMIT 5"
        ).fetchall()
        print("=== 落库结果（近 5 日）===")
        for r in rows:
            print("  ", r[0], r[1])
        latest = c.execute("SELECT max(trade_date) FROM daily_data").fetchone()
        print("  daily_data 最新交易日 =", latest[0] if latest else None)
        hit = c.execute("SELECT count(*) FROM daily_data WHERE trade_date = ?", ["20261105"]).fetchone()
        print("  20261105 行数 =", hit[0] if hit else 0)
        ok = bool(latest and str(latest[0]).replace("-", "")[:8] >= "20261105")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
