# -*- coding: utf-8 -*-
"""
tools/run_stop_rule_backtest.py — 手动跑「按真实止损规则」的策略回测

【用途】
这是给用户自己用的体检工具：在模拟盘跑满一段时间后，
随时按真实执行规则（+8%止盈/-3%止损/T+1/次日开盘买/滑点+费用）
重算一遍，看看每个战法到底赚不赚。

【与每日推送的关系】
daemon 每天 18:30 会自动跑一遍并推送「策略诚实体检」；
本脚本用于随时手动复核，或在停 daemon 时单独跑。

【用法】
  # 需先停 daemon（DuckDB 单写者约束），或直接在 daemon 进程内调用
  .\_pyenv\venv\Scripts\python.exe tools\run_stop_rule_backtest.py
  .\_pyenv\venv\Scripts\python.exe tools\run_stop_rule_backtest.py --pool p5
  .\_pyenv\venv\Scripts\python.exe tools\run_stop_rule_backtest.py --tp 8 --sl -3 --slip 0.1
  .\_pyenv\venv\Scripts\python.exe tools\run_stop_rule_backtest.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def main() -> int:
    ap = argparse.ArgumentParser(description="按真实止损止盈规则回测战法")
    ap.add_argument("--pool", default=None, help="只测某个池（p3/p4/p5），默认全池")
    ap.add_argument("--tp", type=float, default=None, help="止盈%%（默认取模拟盘配置）")
    ap.add_argument("--sl", type=float, default=None, help="止损%%（负数，默认取模拟盘配置）")
    ap.add_argument("--hold", type=int, default=None, help="最长持有交易日（默认取模拟盘配置）")
    ap.add_argument("--slip", type=float, default=None, help="滑点%%（默认取模拟盘配置）")
    ap.add_argument("--lookback", type=int, default=180, help="回看天数")
    ap.add_argument("--json", default=None, help="把明细结果写到 JSON 文件")
    args = ap.parse_args()

    try:
        from data.db_core import get_read_conn
    except Exception as e:
        print(f"[错误] 导入 db_core 失败: {e}")
        return 1

    from contextlib import ExitStack

    from core.stop_rule_backtest import StopRuleConfig, run_stop_rule_backtest, format_report

    cfg = StopRuleConfig()
    if args.tp is not None:
        cfg.take_profit_pct = args.tp
    if args.sl is not None:
        cfg.stop_loss_pct = args.sl
    if args.hold is not None:
        cfg.max_hold_days = args.hold
    if args.slip is not None:
        cfg.slippage_pct = args.slip

    with ExitStack() as stack:
        try:
            # get_read_conn 是上下文管理器，必须 with 使用
            con = stack.enter_context(get_read_conn(read_only=True))
        except Exception as e:
            print("[错误] 无法连接数据库：", str(e)[:300])
            print("提示：DuckDB 单写者约束 —— daemon 运行时会独占数据库。")
            print("      请先停 daemon，或等 daemon 每天 18:30 自动跑（会自动推送）。")
            return 1

        res = run_stop_rule_backtest(con, cfg=cfg, pool=args.pool, lookback_days=args.lookback)
        print(format_report(res))

        if args.json:
            try:
                with open(args.json, "w", encoding="utf-8") as f:
                    json.dump(res, f, ensure_ascii=False, indent=2, default=str)
                print(f"\n已写入 {args.json}")
            except Exception as e:
                print(f"写 JSON 失败: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
