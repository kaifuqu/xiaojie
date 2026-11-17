# -*- coding: utf-8 -*-
"""
core/paper_trading/reset.py — 模拟盘实验重置

【何时用】
参数口径变更（如仓位从「20%总资产」改为「固定5万」、止盈从 +15% 改为 +8%、
止损从「按板差异化」改为「统一 -3%」）后，旧持仓的止损止盈价是按旧口径算的，
混在新实验里会污染「按战法归因」的数据 —— 因为不同口径下的盈亏不可比。

【重置做什么】
  · 清空持仓（paper_position）
  · 清空订单（paper_order）  ← 归因数据源，必须一起清
  · 清空日净值（paper_equity_daily）
  · 账户资金复位为 initial_capital，status 复位 RUNNING
  · 保留表结构，不 drop 表

【安全】
  · 必须显式传 confirm=True，否则拒绝执行
  · 只能在本进程持有主库写连接时调用（DuckDB 单写者约束）
  · 全部操作在单个事务内，异常自动回滚
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from core.paper_trading import store
from core.paper_trading.schema import TABLE_EQUITY, TABLE_ORDER, TABLE_POSITION

_log = logging.getLogger(__name__)


def reset_experiment(
    *,
    initial_capital: float,
    confirm: bool = False,
) -> Dict[str, Any]:
    """重置模拟盘实验数据。confirm=False 时直接拒绝，不做任何写操作。"""
    if not confirm:
        return {
            "ok": False,
            "reason": "需要显式 confirm=True 才会执行重置",
        }
    try:
        @store.write_transaction
        def _tx(con: Any) -> Dict[str, Any]:
            n_pos = con.execute(f"SELECT COUNT(*) FROM {TABLE_POSITION}").fetchone()[0]
            n_ord = con.execute(f"SELECT COUNT(*) FROM {TABLE_ORDER}").fetchone()[0]
            n_eq = con.execute(f"SELECT COUNT(*) FROM {TABLE_EQUITY}").fetchone()[0]
            con.execute(f"DELETE FROM {TABLE_POSITION}")
            con.execute(f"DELETE FROM {TABLE_ORDER}")
            con.execute(f"DELETE FROM {TABLE_EQUITY}")
            # 账户复位
            con.execute(
                "UPDATE paper_account SET cash = ?, status = 'RUNNING', "
                "start_date = NULL, trading_day_index = 0, last_equity_date = NULL, "
                "updated_at = CURRENT_TIMESTAMP",
                [float(initial_capital)],
            )
            return {"positions": int(n_pos), "orders": int(n_ord), "equity": int(n_eq)}

        cleared = _tx() or {}
        _log.warning(
            "[模拟盘] 实验已重置：清空持仓 %d / 订单 %d / 净值 %d，账户复位至 %.2f",
            cleared.get("positions", 0), cleared.get("orders", 0),
            cleared.get("equity", 0), float(initial_capital),
        )
        return {"ok": True, "cleared": cleared, "initial_capital": float(initial_capital)}
    except Exception as e:  # noqa: BLE001
        _log.error("[模拟盘] 实验重置失败: %s", e, exc_info=True)
        return {"ok": False, "reason": str(e)[:300]}


__all__ = ["reset_experiment"]
