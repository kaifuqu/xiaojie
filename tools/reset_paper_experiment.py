# -*- coding: utf-8 -*-
"""
tools/reset_paper_experiment.py — 重置模拟盘实验数据（需管理员权限运行）

【为什么需要】
仓位口径从「总资产×20%」改为「固定5万/只」、止盈从 +15% 改为 +8%、
止损从「按板差异化」改为「统一 -3%」之后，今天已成交的旧持仓用的
是旧口径的止损止盈价，混在新实验里会让「按战法归因」的收益不可比。

【前置条件】
DuckDB 单写者约束：daemon 运行时会独占数据库，本脚本无法连接。
所以必须先停掉 daemon，再运行本脚本。

【用法】
  1) 管理员 PowerShell：
     Stop-Process -Id <daemon_pid> -Force
  2) 管理员 PowerShell：
     .\_pyenv\venv\Scripts\python.exe tools\reset_paper_experiment.py
  3) 重新启动 daemon（start_daemon_24x7.bat）

【安全】
  · 必须显式传 --yes 才会执行
  · 默认 --dry-run，只打印将清空的内容
  · 单事务执行，异常自动回滚
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))


def main() -> int:
    ap = argparse.ArgumentParser(description="重置模拟盘实验数据")
    ap.add_argument("--yes", action="store_true", help="确认执行（不加则 dry-run）")
    args = ap.parse_args()

    from core.paper_trading.config import load_config
    from core.paper_trading.reset import reset_experiment

    cfg = load_config(reload=True)
    print(f"[模拟盘重置] 初始资金 = {cfg.initial_capital:,.2f}")
    print(f"[模拟盘重置] 仓位口径 = {cfg.position_mode} / 每只 {cfg.fixed_amount_per_position:,.0f}元")
    print(f"[模拟盘重置] 止盈 = +{cfg.take_profit_pct}%  止损 = {cfg.stop_loss_pct}%")
    print(f"[模拟盘重置] 最多持仓 = {cfg.max_positions} 只")

    if not args.yes:
        print("\n[DRY-RUN] 未加 --yes，仅预览。")
        print("[DRY-RUN] 实际执行会清空：paper_position / paper_order / paper_equity_daily")
        print("[DRY-RUN] 并把账户现金复位为 initial_capital，status 复位 RUNNING")
        print("[DRY-RUN] 确认无误后加 --yes 重跑本脚本")
        return 0

    out = reset_experiment(initial_capital=cfg.initial_capital, confirm=True)
    if not out.get("ok"):
        print(f"[模拟盘重置] 失败: {out.get('reason')}")
        return 1
    c = out.get("cleared") or {}
    print(
        "[模拟盘重置] 完成：清空持仓 {p} / 订单 {o} / 净值 {e}，账户复位 {c:,.2f}".format(
            p=c.get("positions", 0), o=c.get("orders", 0), e=c.get("equity", 0),
            c=float(out.get("initial_capital") or 0.0),
        )
    )
    print("[模拟盘重置] 现在可以重新启动 daemon 开始新实验")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
