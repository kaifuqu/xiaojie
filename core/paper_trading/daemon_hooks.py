# -*- coding: utf-8 -*-
"""
core/paper_trading/daemon_hooks.py — 守护进程挂钩层

【职责】
把「模拟盘」接入 auto_sniper_daemon 的三件事收敛在本模块，daemon 侧只保留极薄的调用点，
这样模拟盘的任何异常都不会波及 P3/P4 扫描与企微推送主链路。

挂钩点：
  1. P3/P4 扫描出正式行之后 → paper_on_signals_safe(pool, rows)
  2. 每 monitor_interval_sec 一拍（交易时段） → paper_tick_job()
  3. 15:05 收盘 → paper_settle_job()
  4. 08:40 建表初始化 → paper_init_job()

【交易时段】
  09:15-11:30 / 13:00-15:00（含 14:57-15:00 收盘集合竞价尾段）。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

_log = logging.getLogger(__name__)

BJ_TZ = timezone(timedelta(hours=8))

# A 股交易时段（自当日 00:00 起的分钟数）
SESSION_MORNING_START = 9 * 60 + 15     # 09:15 集合竞价开始
SESSION_MORNING_END = 11 * 60 + 30      # 11:30
SESSION_AFTERNOON_START = 13 * 60       # 13:00
SESSION_AFTERNOON_END = 15 * 60         # 15:00（含收盘集合竞价）

_last_bucket: str = ""


def now_bj() -> datetime:
    return datetime.now(BJ_TZ)


def in_trading_session(n: Optional[datetime] = None) -> bool:
    """是否处于 A 股连续竞价/集合竞价时段。"""
    t = n or now_bj()
    m = int(t.hour) * 60 + int(t.minute)
    if SESSION_MORNING_START <= m <= SESSION_MORNING_END:
        return True
    if SESSION_AFTERNOON_START <= m <= SESSION_AFTERNOON_END:
        return True
    return False


def is_enabled() -> bool:
    """模拟盘是否启用（配置缺失/异常按关闭）。"""
    try:
        from core.paper_trading.config import is_enabled as _is

        return bool(_is())
    except Exception as e:  # noqa: BLE001
        _log.debug("[模拟盘] 开关检测失败（按关闭）: %s", e)
        return False


def paper_on_signals_safe(pool_key: str, rows: Any) -> Optional[Dict[str, Any]]:
    """扫描正式行 → 模拟买入。任何异常吞掉，返回 None。"""
    if not rows or not is_enabled():
        return None
    try:
        from core.paper_trading.engine import on_signals

        out = on_signals(pool_key, rows if isinstance(rows, list) else [])
        bought = int((out or {}).get("bought") or 0)
        skipped = int((out or {}).get("skipped") or 0)
        if bought or skipped:
            _log.info("[模拟盘][%s] 买入=%d 跳过=%d", pool_key, bought, skipped)
        return out
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘][%s] on_signals 外层异常（已吞）: %s", pool_key, e)
        return None


def paper_tick_job(*, force: bool = False) -> Optional[Dict[str, Any]]:
    """持仓阈值监控一拍（-3% 止损 / +8% 止盈 / 当日跌 3% / 到期，均 100% 清仓）。

    force=True 时忽略时间桶去重（仅测试用）。
    """
    global _last_bucket
    if not is_enabled():
        return None
    try:
        from core.paper_trading.config import load_config

        interval = max(5, int(load_config().monitor_interval_sec or 30))
    except Exception as e:  # noqa: BLE001
        _log.debug("[模拟盘] 读取监控间隔失败，回落 30s: %s", e)
        interval = 30
    n = now_bj()
    sec_of_day = int(n.hour) * 3600 + int(n.minute) * 60 + int(n.second)
    bucket = f"{n.strftime('%Y%m%d')}:{sec_of_day // interval}"
    if not force:
        if bucket == _last_bucket:
            return None
        _last_bucket = bucket
    if not force and not in_trading_session(n):
        return None
    try:
        from core.paper_trading.engine import on_tick

        out = on_tick(today=n.strftime("%Y%m%d"))
        sold = int((out or {}).get("sold") or 0)
        if sold:
            _log.info("[模拟盘] 本轮卖出 %d 笔", sold)
        return out
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘] on_tick 外层异常（已吞）: %s", e)
        return None


def paper_settle_job(trade_date: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """收盘写日净值（幂等）。

    【P1-7/8 2026-11-14】先补停机缺口：若上次结算距今跨过多个交易日
    （daemon 停机错过 15:05±90min 窗口），按历史收盘补写缺口净值行，再落当日。
    """
    if not is_enabled():
        return None
    try:
        from core.paper_trading.engine import backfill_equity_gap, daily_settle

        try:
            gap = backfill_equity_gap()
            if gap and gap.get("filled"):
                _log.warning("[模拟盘] 缺口回填完成：%s", gap)
        except Exception as e:  # noqa: BLE001 —— 回填失败绝不影响当日结算
            _log.warning("[模拟盘] 缺口回填异常（继续当日结算）: %s", e)
        return daily_settle(trade_date=trade_date)
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘] daily_settle 外层异常（已吞）: %s", e)
        return None


def paper_init_job() -> Optional[Dict[str, Any]]:
    """建表 + 初始化账户（保证当日首笔信号前表已就位）。"""
    if not is_enabled():
        return None
    try:
        from core.paper_trading.config import load_config
        from core.paper_trading.store import ensure_account, write_transaction

        cfg = load_config()

        @write_transaction
        def _init(con, initial_capital: float):
            return ensure_account(con, initial_capital=initial_capital)

        acct = _init(cfg.initial_capital) or {}
        _log.info(
            "[模拟盘] 账户就绪 cash=%.2f status=%s day=%s",
            float(acct.get("cash") or 0.0),
            acct.get("status"),
            acct.get("trading_day_index"),
        )
        # 【P1-7/8 2026-11-14】早晨顺手修复净值缺口：daemon 隔夜停机不会
        # 让 paper_equity_daily 断链（幂等，无缺口零成本）。
        try:
            from core.paper_trading.engine import backfill_equity_gap

            gap = backfill_equity_gap()
            if gap and gap.get("filled"):
                _log.warning("[模拟盘] 早晨缺口回填完成：%s", gap)
        except Exception as e:  # noqa: BLE001
            _log.warning("[模拟盘] 早晨缺口回填异常（已吞）: %s", e)
        return acct
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘] 初始化异常（已吞）: %s", e)
        return None


def _self_test() -> None:
    """纯逻辑自检：交易时段边界。"""
    assert in_trading_session(datetime(2026, 10, 17, 9, 15, tzinfo=BJ_TZ)) is True
    assert in_trading_session(datetime(2026, 10, 17, 9, 14, tzinfo=BJ_TZ)) is False
    assert in_trading_session(datetime(2026, 10, 17, 11, 30, tzinfo=BJ_TZ)) is True
    assert in_trading_session(datetime(2026, 10, 17, 11, 31, tzinfo=BJ_TZ)) is False
    assert in_trading_session(datetime(2026, 10, 17, 12, 0, tzinfo=BJ_TZ)) is False
    assert in_trading_session(datetime(2026, 10, 17, 13, 0, tzinfo=BJ_TZ)) is True
    assert in_trading_session(datetime(2026, 10, 17, 15, 0, tzinfo=BJ_TZ)) is True
    assert in_trading_session(datetime(2026, 10, 17, 15, 1, tzinfo=BJ_TZ)) is False
    print("[daemon_hooks] self-test OK | trading session boundaries verified")


if __name__ == "__main__":
    _self_test()


__all__ = [
    "in_trading_session",
    "is_enabled",
    "paper_init_job",
    "paper_on_signals_safe",
    "paper_settle_job",
    "paper_tick_job",
]
