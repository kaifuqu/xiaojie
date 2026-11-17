# -*- coding: utf-8 -*-
"""
core/paper_trading/reporting.py — 模拟盘只读汇总与战法排行榜

【职责】
  build_snapshot() 一次性返回 UI 需要的全部只读数据：
    account     账户资金与实验进度
    positions   当前持仓（附实时价、距止损/止盈）
    trades      已平仓明细（净收益含费）
    equity      日净值序列
    leaderboard 战法排行榜（原子战法粒度）
    fee_summary 费用汇总

【口径冻结（重要）】
  · 收益率一律「净」（含佣金 + 印花税 + 过户费）。
  · 胜率 = 净收益 > 0 的平仓笔数 / 总平仓笔数。
  · 一个信号可能命中多个原子战法 → 各战法样本相互重叠，
    各战法累计盈亏「不可相加」；UI 需明示。
  · 样本数 < MIN_SAMPLE 时不标「可信」，仅列「样本不足」。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.paper_trading import store
from core.paper_trading.config import PaperTradingConfig, load_config
from core.paper_trading.schema import ALL_TABLES

_log = logging.getLogger(__name__)

MIN_SAMPLE = 10


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None:
            return d
        fv = float(v)
        return d if fv != fv else fv
    except (TypeError, ValueError):
        return d


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _dist_to_threshold(price: float, threshold: float) -> float:
    """现价距离阈值的百分比（正=还有空间，负=已越过）。"""
    if price <= 0:
        return 0.0
    return (threshold - price) / price * 100.0


def build_snapshot(
    *,
    quotes: Optional[Dict[str, Dict[str, Any]]] = None,
    cfg: Optional[PaperTradingConfig] = None,
) -> Dict[str, Any]:
    """构建只读快照。任何异常都吞掉并返回 available=False。"""
    c = cfg or load_config()
    try:
        out = _read(lambda con: _build(con, c, quotes))
        # _read 在「表未建立 / 连接异常」时返回 None —— 这是「还没初始化」的正常空态，
        # 不是错误。统一返回可渲染的空快照，UI 走空态分支而不是红色报错。
        if out is None:
            return _empty_snapshot(c)
        return out
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘] build_snapshot 异常（已吞）: %s", e, exc_info=True)
        return {
            "available": False,
            "error": str(e)[:300],
            "enabled": bool(c.enabled),
            "config": c,
        }


def _empty_snapshot(cfg: PaperTradingConfig) -> Dict[str, Any]:
    """模拟盘尚未初始化（表未建 / 账户未建）时的空快照，UI 可直接渲染。"""
    return {
        "available": True,
        "initialized": False,
        "enabled": bool(cfg.enabled),
        "config": cfg,
        "account": None,
        "positions": [],
        "trades": [],
        "equity": [],
        "leaderboard": [],
        "fee_summary": {"commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0, "total": 0.0},
        "progress": {"day_index": 0, "cohort_days": int(cfg.cohort_days), "closed": False},
    }


def _read(fn):
    """走只读连接执行 fn(con)；缺库/缺表时返回 None 而不抛。"""
    try:
        from data.db_core import get_read_conn

        with get_read_conn(read_only=True) as con:
            # 【V29.3 修复】主库尚未建模拟盘四表时，只读连接不能建表
            # （CREATE TABLE 在 read_only 连接上必然失败）。这里只判断「表在不在」，
            # 不在就直接返回 None，让 _build 之外的上层走「未初始化」分支，
            # 而不是把 Catalog Error 抛到日志里、面板误报「数据读取异常」。
            try:
                names = {
                    str(r[0])
                    for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()
                }
            except Exception as _e:  # noqa: BLE001
                _log.debug("[模拟盘] 枚举表失败（跳过表存在性判断）: %s", _e)
                names = set()
            if names and not set(ALL_TABLES).issubset(names):
                missing = sorted(set(ALL_TABLES) - names)
                _log.debug("[模拟盘] 模拟盘表尚未建立（缺 %s），按未初始化处理", missing)
                return None
            return fn(con)
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘] 只读查询失败: %s", e)
        return None


def _build(
    con: Any,
    cfg: PaperTradingConfig,
    quotes: Optional[Dict[str, Dict[str, Any]]],
) -> Dict[str, Any]:
    acct = store.load_account(con)
    if not acct:
        # 账户还没建立：回一个可渲染的空态
        return {
            "available": True,
            "enabled": bool(cfg.enabled),
            "config": cfg,
            "account": None,
            "positions": [],
            "trades": [],
            "equity": [],
            "leaderboard": [],
            "fee_summary": {"commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0, "total": 0.0},
            "progress": {"day_index": 0, "cohort_days": int(cfg.cohort_days), "closed": False},
        }

    positions = store.load_positions(con)
    trades = store.load_closed_positions(con)
    equity = store.load_equity_series(con, limit=500)
    equity = list(reversed(equity))  # 时间正序，UI 画图用

    # ---------- 持仓 ----------
    pos_rows: List[Dict[str, Any]] = []
    mv_total = 0.0
    unreal = 0.0
    for p in positions:
        code = str(p.get("code") or "")
        q = (quotes or {}).get(code) or {}
        px = _f(q.get("price"), 0.0)
        if px <= 0:
            px = _f(p.get("last_price"), 0.0)
        buy_px = _f(p.get("buy_price_raw"), 0.0)
        shares = int(p.get("shares") or 0)
        net_cost = _f(p.get("net_cost"), 0.0)
        stop_px = _f(p.get("stop_loss_price"), buy_px * cfg.stop_loss_ratio)
        take_px = _f(p.get("take_profit_price"), buy_px * cfg.take_profit_ratio)
        mv = px * shares
        pnl = mv - net_cost
        mv_total += mv
        unreal += pnl
        pos_rows.append(
            {
                "code": code,
                "name": str(p.get("name") or ""),
                "pool": str(p.get("pool") or ""),
                "strategy_ids": str(p.get("strategy_ids") or ""),
                "strategy_names": str(p.get("strategy_names") or ""),
                "buy_date": str(p.get("buy_date") or ""),
                "buy_time": str(p.get("buy_time") or ""),
                "buy_price": buy_px,
                "price": px,
                "price_ts": str(p.get("price_ts") or ""),
                "shares": shares,
                "net_cost": net_cost,
                "market_value": mv,
                "unrealized_pnl": pnl,
                "unrealized_pnl_pct": (pnl / net_cost) if net_cost > 0 else 0.0,
                "stop_loss_price": stop_px,
                "take_profit_price": take_px,
                "dist_to_stop_pct": _dist_to_threshold(px, stop_px),
                "dist_to_take_pct": _dist_to_threshold(take_px, px),
                "holding_days": _days_between(str(p.get("buy_date") or "")),
            }
        )
    pos_rows.sort(key=lambda r: r["buy_date"] + r["buy_time"])

    cash = _f(acct.get("cash"), 0.0)
    total_asset = cash + mv_total
    init_cap = _f(acct.get("initial_capital"), 0.0)

    # 仓位占比
    for r in pos_rows:
        r["weight_pct"] = (r["market_value"] / total_asset * 100.0) if total_asset > 0 else 0.0

    # ---------- 今日盈亏 ----------
    today = store.today_yyyymmdd()
    day_pnl = 0.0
    day_fee = 0.0
    day_trades = 0
    try:
        from core.paper_trading.schema import TABLE_ORDER

        r = store._row(
            con,
            f"SELECT COUNT(*) AS n, COALESCE(SUM(total_fee),0) AS fee, "
            f"COALESCE(SUM(cash_delta),0) AS cd FROM {TABLE_ORDER} "
            f"WHERE trade_date = ? AND side IN ('BUY','SELL')",
            [today],
        )
        day_trades = int((r or {}).get("n") or 0)
        day_fee = _f((r or {}).get("fee"), 0.0)
    except Exception as e:  # noqa: BLE001
        _log.debug("统计当日成交失败: %s", e)
    prev_eq = None
    for row in equity:
        if str(row.get("trade_date")) != today:
            prev_eq = row
    day_pnl = unreal + day_fee
    day_pct = 0.0
    if prev_eq is not None:
        prev_total = _f(prev_eq.get("total_asset"), 0.0)
        day_pnl = total_asset - prev_total
        if prev_total > 0:
            day_pct = day_pnl / prev_total

    # ---------- 费用汇总 ----------
    fee_summary = {"commission": 0.0, "stamp_tax": 0.0, "transfer_fee": 0.0, "total": 0.0}
    try:
        from core.paper_trading.schema import TABLE_ORDER

        r = store._row(
            con,
            f"SELECT COALESCE(SUM(commission),0) AS c, COALESCE(SUM(stamp_tax),0) AS s, "
            f"COALESCE(SUM(transfer_fee),0) AS t FROM {TABLE_ORDER} WHERE side IN ('BUY','SELL')",
        )
        fee_summary = {
            "commission": _f((r or {}).get("c"), 0.0),
            "stamp_tax": _f((r or {}).get("s"), 0.0),
            "transfer_fee": _f((r or {}).get("t"), 0.0),
        }
        fee_summary["total"] = fee_summary["commission"] + fee_summary["stamp_tax"] + fee_summary["transfer_fee"]
    except Exception as e:  # noqa: BLE001
        _log.debug("费用汇总失败: %s", e)

    # ---------- 已平仓明细 ----------
    trade_rows: List[Dict[str, Any]] = []
    for t in trades:
        trade_rows.append(
            {
                "trade_date": str(t.get("trade_date") or ""),
                "sell_ts": str(t.get("order_ts") or ""),
                "code": str(t.get("code") or ""),
                "name": str(t.get("name") or ""),
                "pool": str(t.get("pool") or ""),
                "strategy_ids": str(t.get("strategy_ids") or ""),
                "strategy_names": str(t.get("strategy_names") or ""),
                "buy_price": _f(t.get("buy_price_raw"), 0.0),
                "sell_price": _f(t.get("exec_price"), 0.0),
                "shares": int(t.get("shares") or 0),
                "gross_amount": _f(t.get("gross_amount"), 0.0),
                "total_fee": _f(t.get("total_fee"), 0.0),
                "pnl": _f(t.get("pnl"), 0.0),
                "pnl_pct": _f(t.get("pnl_pct"), 0.0),
                "holding_days": int(t.get("holding_days") or 0),
                "exit_reason": str(t.get("exit_reason") or ""),
            }
        )

    # ---------- 战法排行榜 ----------
    leaderboard = _build_leaderboard(trade_rows, pos_rows)

    # ---------- 累计盈亏（已实现 + 浮动） ----------
    realized = sum(r["pnl"] for r in trade_rows)
    cum_pnl = realized + unreal
    cum_ret = (cum_pnl / init_cap) if init_cap > 0 else 0.0

    day_index = int(acct.get("trading_day_index") or 0)
    return {
        "available": True,
        "enabled": bool(cfg.enabled),
        "config": cfg,
        "account": {
            "initial_capital": init_cap,
            "cash": cash,
            "market_value": mv_total,
            "total_asset": total_asset,
            "realized_pnl": realized,
            "unrealized_pnl": unreal,
            "total_pnl": cum_pnl,
            "total_return": cum_ret,
            "day_pnl": day_pnl,
            "day_return": day_pct,
            "position_count": len(pos_rows),
            "day_trades": day_trades,
            "day_fee": day_fee,
            "status": str(acct.get("status") or "RUNNING"),
            "start_date": str(acct.get("start_date") or ""),
        },
        "positions": pos_rows,
        "trades": trade_rows,
        "equity": equity,
        "leaderboard": leaderboard,
        "fee_summary": fee_summary,
        "progress": {
            "day_index": day_index,
            "cohort_days": int(cfg.cohort_days),
            "closed": str(acct.get("status") or "") == "COHORT_CLOSED",
            "pct": min(100.0, (day_index / float(cfg.cohort_days)) * 100.0) if cfg.cohort_days > 0 else 0.0,
        },
    }


def _days_between(buy_date: str) -> int:
    from datetime import datetime

    try:
        b = datetime.strptime(str(buy_date), "%Y%m%d").date()
        t = store.now_bj().date()
        return max(0, (t - b).days)
    except Exception:  # noqa: BLE001
        return 0


def _build_leaderboard(
    trades: List[Dict[str, Any]],
    positions: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """按原子战法聚合已平仓交易 → 排行榜。

    信号数 = 已平仓笔数 + 当前持仓数（同一票当前持仓只计一次）。
    """
    agg: Dict[str, Dict[str, Any]] = {}

    def _bucket(cid: str) -> Dict[str, Any]:
        if cid not in agg:
            agg[cid] = {
                "strategy_id": cid,
                "strategy_names": [],
                "pool": "",
                "signal_count": 0,
                "closed_count": 0,
                "win_count": 0,
                "pnl_list": [],
                "pnl_pct_list": [],
                "total_pnl": 0.0,
                "stop_count": 0,
                "take_count": 0,
                # 【2026-11-05】当日跌幅离场（相对昨收 -3%），与止损分列统计
                "day_drop_count": 0,
                "holding_days_list": [],
            }
        return agg[cid]

    def _split(raw: str) -> List[str]:
        return [x.strip() for x in str(raw or "").split(",") if x.strip()]

    for t in trades:
        ids = _split(t.get("strategy_ids"))
        names = _split(t.get("strategy_names"))
        name_map = dict(zip(ids, names))
        for cid in ids:
            b = _bucket(cid)
            b["signal_count"] += 1
            b["closed_count"] += 1
            pnl = float(t["pnl"])
            b["pnl_list"].append(pnl)
            b["pnl_pct_list"].append(float(t.get("pnl_pct") or 0.0))
            b["total_pnl"] += pnl
            if pnl > 0:
                b["win_count"] += 1
            if t.get("exit_reason") == "STOP_LOSS":
                b["stop_count"] += 1
            elif t.get("exit_reason") == "TAKE_PROFIT":
                b["take_count"] += 1
            elif t.get("exit_reason") == "DAY_DROP":
                b["day_drop_count"] += 1
            b["holding_days_list"].append(int(t.get("holding_days") or 0))
            b["pool"] = b["pool"] or str(t.get("pool") or "")
            lbl = name_map.get(cid) or cid
            if lbl and lbl not in b["strategy_names"]:
                b["strategy_names"].append(lbl)

    for p in positions:
        ids = _split(p.get("strategy_ids"))
        for cid in ids:
            b = _bucket(cid)
            b["signal_count"] += 1
            b["pool"] = b["pool"] or str(p.get("pool") or "")

    out: List[Dict[str, Any]] = []
    for cid, b in agg.items():
        n = b["closed_count"]
        avg = (b["total_pnl"] / n) if n > 0 else 0.0
        row = {
            "strategy_id": cid,
            "strategy_names": " / ".join(b["strategy_names"]) or cid,
            "pool": b["pool"] or "—",
            "signal_count": b["signal_count"],
            "closed_count": n,
            "win_rate": (b["win_count"] / n * 100.0) if n > 0 else 0.0,
            "avg_pnl": avg,
            "median_pnl": _median(b["pnl_list"]),
            "avg_pnl_pct": (sum(b["pnl_pct_list"]) / n) if n > 0 else 0.0,
            "median_pnl_pct": _median(b["pnl_pct_list"]),
            "total_pnl": b["total_pnl"],
            "stop_rate": (b["stop_count"] / n * 100.0) if n > 0 else 0.0,
            "take_rate": (b["take_count"] / n * 100.0) if n > 0 else 0.0,
            "day_drop_rate": (b["day_drop_count"] / n * 100.0) if n > 0 else 0.0,
            "avg_holding_days": (sum(b["holding_days_list"]) / n) if n > 0 else 0.0,
            "sample_ok": n >= MIN_SAMPLE,
            "sample_label": ("样本充足" if n >= 30 else ("样本中等" if n >= MIN_SAMPLE else "样本不足")),
        }
        out.append(row)
    out.sort(key=lambda r: (r["sample_ok"], r["win_rate"], r["total_pnl"]), reverse=True)
    return out


def _self_test() -> None:
    """排行榜聚合纯逻辑自检。"""
    trades = [
        {"strategy_ids": "P3-01,P3-02", "strategy_names": "A,B", "pool": "p3",
         "pnl": 100.0, "exit_reason": "TAKE_PROFIT", "holding_days": 3},
        {"strategy_ids": "P3-01", "strategy_names": "A", "pool": "p3",
         "pnl": -80.0, "exit_reason": "STOP_LOSS", "holding_days": 1},
    ]
    positions = [{"strategy_ids": "P3-02", "pool": "p3"}]
    lb = _build_leaderboard(trades, positions)
    assert len(lb) == 2, lb
    p301 = [r for r in lb if r["strategy_id"] == "P3-01"][0]
    p302 = [r for r in lb if r["strategy_id"] == "P3-02"][0]
    assert p301["closed_count"] == 2
    assert abs(p301["win_rate"] - 50.0) < 1e-6
    assert abs(p301["total_pnl"] - 20.0) < 1e-6
    assert p301["stop_rate"] == 50.0 and p301["take_rate"] == 50.0
    assert abs(p301["avg_holding_days"] - 2.0) < 1e-6
    # P3-02：已平仓 1 笔 + 当前持仓 1 笔 = 2 个信号
    assert p302["closed_count"] == 1 and p302["signal_count"] == 2, p302
    assert p301["sample_ok"] is False
    print("[reporting] self-test OK | leaderboard aggregation verified")


if __name__ == "__main__":
    _self_test()


__all__ = ["MIN_SAMPLE", "build_snapshot"]
