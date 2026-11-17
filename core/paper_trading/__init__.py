# -*- coding: utf-8 -*-
"""
core/paper_trading/__init__.py — 模拟盘公开 API

设计目标：调用方只需 `from core import paper_trading as pt`，不感知子模块结构。

公开 API：
  is_enabled()                 模拟盘是否启用
  load_config()                读配置
  on_signals(pool, rows)       消费 P3/P4 正式信号 → 买入
  on_tick(quotes=None)         持仓阈值监控一帧 → 卖出
  daily_settle(trade_date)     收盘写日净值
  snapshot()                   UI 只读汇总（账户 + 持仓 + 净值 + 排行榜）
"""
from __future__ import annotations

from core.paper_trading.config import PaperTradingConfig, is_enabled, load_config
from core.paper_trading.engine import daily_settle, on_signals, on_tick
from core.paper_trading.reporting import build_snapshot

__all__ = [
    "PaperTradingConfig",
    "build_snapshot",
    "daily_settle",
    "is_enabled",
    "load_config",
    "on_signals",
    "on_tick",
    "snapshot",
]


def snapshot(**kwargs):
    """UI 只读汇总的薄封装（见 core.paper_trading.reporting.build_snapshot）。"""
    return build_snapshot(**kwargs)
