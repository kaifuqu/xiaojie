# -*- coding: utf-8 -*-
"""
core/stop_rule_backtest.py — 按「真实止损/止盈规则」的策略回测器

【为什么需要这个模块（核心问题）】
系统原先把「战法有效性」算成「买入后死拿到 T+N 日收盘」的收益，
但模拟盘/实盘实际执行的是「+8% 止盈 / -3% 止损 / T+1 / 到期强平」。
两者是**完全不同的交易规则**——用 A 的数据去评价 B，结论会系统性失真：
  · 一个盘中冲高 +12% 又回落收平的战法，按「死拿」算出 0%，
    但按「+8% 就跑」其实早就落袋为安了；
  · 反之，一个慢涨但波动大的战法，死拿看着不错，按 -3% 止损却频繁被洗出去。

本模块用**日线 OHLC**还原真实成交，模拟用户实际要跑的规则：
  1. 信号日收盘后产生 → 次日**开盘价买入**（P5盘后/收盘后必然如此；不吃未来函数）
  2. 买入当日 T+1 锁定，**当天不可卖**
  3. 之后每个交易日：盘中触及 +X% 止盈价 / -Y% 止损价即成交
  3b.【2026-11-05 新增】当日跌幅离场：相对**昨收**跌到 -Z%（默认 -3%）即成交，
     与持仓盈亏无关（浮盈也卖）。它与 -Y% 止损是两条独立的线：
     止损看买入价，这条看「今天这只票的跌幅」。
  4. **跳空处理**（诚实关键）：早盘跳空低开到止损价下方 → 按开盘价成交（亏更多），
     不会让你「恰好」在 -3% 卖在一个根本触及不到的价格上；跳空高开越过止盈同理
  5. 同日既触止盈又触止损（高开回落）→ 保守按**先触止损**处理（不美化结果）
  6. 超过最大持有日仍未触发 → 最后一日收盘强平
  7. 买入/卖出均计滑点 + 佣金 + 印花税 + 过户费（净口径）

【输出】
  · 每笔交易明细（买卖价、出场原因、持有天数、净盈亏）
  · 按战法聚合：胜率、期望值、止盈率、止损率、盈利因子、最大单笔回撤
  · 全局汇总：总样本、总体胜率、总期望、盈亏比

【口径冻结】
  · 一律净口径（扣全部费用 + 滑点）。
  · 胜率 = 净收益 > 0 的笔数 / 总笔数。
  · 期望值 = 平均每笔净收益率（这是最该看的数：>0 才可能长期赚钱）。
  · 盈利因子 = 总盈利 / 总亏损绝对值（>1.5 才算健康，>2 优秀）。
  · 样本 < MIN_SAMPLE 不标「可信」。

本模块只读、不写库、不碰实盘，纯分析，绝对安全。
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# 允许以脚本方式直接运行（python core/stop_rule_backtest.py）
if __package__ in (None, ""):
    sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

_log = logging.getLogger(__name__)

MIN_SAMPLE = 10
STRONG_SAMPLE = 30
LOT_SIZE = 100
# 每笔回测的名义本金（用于把「百分比收益」换算成「赚多少钱」的直觉；不参与胜率统计）
NOMINAL_NOTIONAL = 100000.0


@dataclass
class StopRuleConfig:
    """回测参数（默认对齐用户实盘计划：+8%/-3%，T+1，最多持有10日）。"""

    take_profit_pct: float = 8.0
    stop_loss_pct: float = -3.0
    # 【2026-11-05】当日跌幅离场：相对昨收跌到该百分比即离场（负数启用，0=关闭）。
    # 与 stop_loss_pct 的区别：止损看买入价，这条看「当日跌幅」，浮盈也可能被打掉。
    day_drop_exit_pct: float = -3.0
    max_hold_days: int = 10
    # 滑点：买入上浮、卖出下调。默认 0.1%，比 0 诚实得多。
    slippage_pct: float = 0.1
    # 费用（对齐 config.yaml paper_trading.fee）
    commission_rate: float = 0.0003
    commission_min: float = 5.0
    stamp_tax_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    # 买入方式：'next_open'（次日开盘，最真实）| 'push_price'（信号价，偏乐观）
    entry_mode: str = "next_open"


@dataclass
class Trade:
    """一笔回测交易。"""

    ts_code: str
    strategy_ids: List[str]
    name: str
    pool: str
    signal_date: str
    entry_date: str = ""
    entry_price: float = 0.0
    exit_date: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""      # TAKE_PROFIT / STOP_LOSS / DAY_DROP / TIMEOUT
    holding_days: int = 0
    shares: int = 0
    net_pnl: float = 0.0
    net_pnl_pct: float = 0.0
    status: str = "ok"         # ok / pending（数据不足未可评估）


def _round_price(p: float) -> float:
    from core.paper_trading.fees import round_price

    return round_price(p)


def _slip(price: float, side: str, slippage_pct: float) -> float:
    from core.paper_trading.fees import apply_slippage

    return apply_slippage(price, side, slippage_pct)


def _fees(buy_px: float, sell_px: float, shares: int, cfg: StopRuleConfig):
    from core.paper_trading.fees import compute_fee

    fb = compute_fee(
        price=buy_px, shares=shares, side="BUY",
        commission_rate=cfg.commission_rate, commission_min=cfg.commission_min,
        stamp_tax_rate=cfg.stamp_tax_rate, transfer_fee_rate=cfg.transfer_fee_rate,
    )
    fs = compute_fee(
        price=sell_px, shares=shares, side="SELL",
        commission_rate=cfg.commission_rate, commission_min=cfg.commission_min,
        stamp_tax_rate=cfg.stamp_tax_rate, transfer_fee_rate=cfg.transfer_fee_rate,
    )
    return fb, fs


def simulate_one(
    entry: Dict[str, Any],
    bars: List[Dict[str, Any]],
    cfg: StopRuleConfig,
) -> Trade:
    """模拟单条信号的完整交易（bars 为该股票按日期升序的日线，含 open/high/low/close/trade_date）。"""
    ts_code = str(entry.get("ts_code") or "")
    strat = entry.get("strategy_ids") or []
    if isinstance(strat, str):
        strat = [x.strip() for x in strat.replace("|", ",").split(",") if x.strip()]
    tr = Trade(
        ts_code=ts_code, strategy_ids=list(strat),
        name=str(entry.get("name") or ""), pool=str(entry.get("pool") or ""),
        signal_date=str(entry.get("signal_date") or ""),
    )
    if len(bars) < 2:
        tr.status = "pending"
        return tr

    # 定位信号日在 bars 中的位置（信号日 = log_date）
    sig_idx = None
    for i, b in enumerate(bars):
        if str(b.get("trade_date") or "") >= tr.signal_date:
            # 用第一个 >= 信号日的 bar 作为基准；真正买入看 entry_mode
            sig_idx = i
            break
    if sig_idx is None:
        tr.status = "pending"
        return tr

    # 确定买入
    if cfg.entry_mode == "next_open":
        buy_idx = sig_idx + 1
        if buy_idx >= len(bars):
            tr.status = "pending"   # 信号太新，还没有次日数据
            return tr
        raw_buy = float(bars[buy_idx].get("open") or 0.0)
        if raw_buy <= 0:
            raw_buy = float(bars[buy_idx].get("close") or 0.0)
        if raw_buy <= 0:
            tr.status = "pending"
            return tr
        tr.entry_date = str(bars[buy_idx].get("trade_date") or "")
    else:  # push_price
        buy_idx = sig_idx
        raw_buy = float(entry.get("push_price") or 0.0)
        if raw_buy <= 0:
            tr.status = "pending"
            return tr
        tr.entry_date = str(bars[buy_idx].get("trade_date") or "")

    buy_px = _slip(raw_buy, "BUY", cfg.slippage_pct)
    if buy_px <= 0:
        tr.status = "pending"
        return tr
    tr.entry_price = buy_px
    take_px = _round_price(buy_px * (1.0 + cfg.take_profit_pct / 100.0))
    stop_px = _round_price(buy_px * (1.0 + cfg.stop_loss_pct / 100.0))

    # T+1：买入当日不可卖，从 buy_idx+1 开始评估
    exit_price = 0.0
    exit_reason = ""
    exit_idx = None
    last_idx = min(buy_idx + max(1, int(cfg.max_hold_days)), len(bars) - 1)
    for j in range(buy_idx + 1, last_idx + 1):
        o = float(bars[j].get("open") or 0.0)
        h = float(bars[j].get("high") or 0.0)
        low = float(bars[j].get("low") or 0.0)
        c = float(bars[j].get("close") or 0.0)
        if o <= 0 or h <= 0 or low <= 0:
            continue
        # 【2026-11-05】当日跌幅离场线：昨收 = 前一根 bar 的收盘（买入次日就取买入日收盘）。
        # 与 stop_px 的区别：stop_px 看买入价，这条看「当日跌幅」，浮盈也能被打掉。
        day_drop_px = 0.0
        if float(cfg.day_drop_exit_pct) < 0.0:
            _prev_close = float(bars[j - 1].get("close") or 0.0)
            if _prev_close > 0:
                day_drop_px = _round_price(_prev_close * (1.0 + float(cfg.day_drop_exit_pct) / 100.0))
        # 跳空优先判定（诚实：开盘已越过阈值，按开盘价成交）
        if o <= stop_px:
            exit_price, exit_reason, exit_idx = o, "STOP_LOSS", j
            break
        if o >= take_px:
            exit_price, exit_reason, exit_idx = o, "TAKE_PROFIT", j
            break
        if day_drop_px > 0 and o <= day_drop_px:
            exit_price, exit_reason, exit_idx = o, "DAY_DROP", j
            break
        hit_take = h >= take_px
        # 两条止损线各自独立判定（不能先取 min：持仓深套时 stop_px 远低于当日跌幅线，
        # 先取 min 会把「本该在当日跌幅线出掉」的仓位当成继续扛到深套，严重失真）。
        stop_hits: List[Tuple[float, int, str]] = []
        if low <= stop_px:
            stop_hits.append((stop_px, 0, "STOP_LOSS"))
        if day_drop_px > 0 and low <= day_drop_px:
            stop_hits.append((day_drop_px, 1, "DAY_DROP"))
        if stop_hits:
            # 同日双触（含止盈+止损）：保守按先触止损处理（不美化）；
            # 两条止损线都够到时取更低成交价（更差），同价则记 STOP_LOSS。
            exit_price, _prio, exit_reason = min(stop_hits)
            exit_idx = j
            break
        if hit_take:
            exit_price, exit_reason, exit_idx = take_px, "TAKE_PROFIT", j
            break

    if exit_idx is None:
        # 到期强平：最后一日收盘
        j = last_idx
        c = float(bars[j].get("close") or 0.0)
        if c <= 0:
            tr.status = "pending"
            return tr
        exit_price, exit_reason, exit_idx = c, "TIMEOUT", j
        # 数据没到 max_hold 就用完了 → 其实还没成熟，标 pending 不计入
        if (last_idx - buy_idx) < int(cfg.max_hold_days) and j == len(bars) - 1:
            tr.status = "pending"
            return tr

    sell_px = _slip(exit_price, "SELL", cfg.slippage_pct)
    if sell_px <= 0:
        tr.status = "pending"
        return tr
    tr.exit_price = sell_px
    tr.exit_reason = exit_reason
    tr.exit_date = str(bars[exit_idx].get("trade_date") or "")
    tr.holding_days = exit_idx - buy_idx

    # 按名义本金换成整手股数，计真实费用
    shares = int(NOMINAL_NOTIONAL / buy_px) // LOT_SIZE * LOT_SIZE
    if shares <= 0:
        shares = LOT_SIZE
    tr.shares = shares
    fb, fs = _fees(buy_px, sell_px, shares, cfg)
    tr.net_pnl = fs.cash_delta + fb.cash_delta   # 卖出流入 - 买入流出
    cost_basis = -fb.cash_delta                   # 买入总成本（正）
    tr.net_pnl_pct = (tr.net_pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0
    tr.status = "ok"
    return tr


def aggregate(trades: List[Trade], cfg: Optional[StopRuleConfig] = None) -> Dict[str, Any]:
    """按战法聚合已成交（status=ok）的交易。"""
    ok = [t for t in trades if t.status == "ok"]
    agg: Dict[str, Dict[str, Any]] = {}

    def _bucket(cid: str) -> Dict[str, Any]:
        if cid not in agg:
            agg[cid] = {"n": 0, "win": 0, "pnls": [], "pcts": [], "days": [],
                        "stop": 0, "day_drop": 0, "take": 0, "timeout": 0,
                        "profit": 0.0, "loss": 0.0}
        return agg[cid]

    for t in ok:
        for cid in t.strategy_ids:
            b = _bucket(cid)
            b["n"] += 1
            b["pnls"].append(t.net_pnl)
            b["pcts"].append(t.net_pnl_pct)
            b["days"].append(t.holding_days)
            if t.net_pnl > 0:
                b["win"] += 1
                b["profit"] += t.net_pnl
            else:
                b["loss"] += abs(t.net_pnl)
            if t.exit_reason == "STOP_LOSS":
                b["stop"] += 1
            elif t.exit_reason == "DAY_DROP":
                b["day_drop"] += 1
            elif t.exit_reason == "TAKE_PROFIT":
                b["take"] += 1
            elif t.exit_reason == "TIMEOUT":
                b["timeout"] += 1

    rows: List[Dict[str, Any]] = []
    for cid, b in agg.items():
        n = b["n"]
        avg_pct = (sum(b["pcts"]) / n) if n > 0 else 0.0
        profit_factor = (b["profit"] / b["loss"]) if b["loss"] > 0 else (float("inf") if b["profit"] > 0 else 0.0)
        rows.append({
            "strategy_id": cid,
            "n": n,
            "win_rate": (b["win"] / n * 100.0) if n > 0 else 0.0,
            "avg_pnl_pct": avg_pct,
            "avg_pnl_amt": (sum(b["pnls"]) / n) if n > 0 else 0.0,
            "total_pnl_amt": sum(b["pnls"]),
            "take_rate": (b["take"] / n * 100.0) if n > 0 else 0.0,
            "stop_rate": (b["stop"] / n * 100.0) if n > 0 else 0.0,
            "day_drop_rate": (b["day_drop"] / n * 100.0) if n > 0 else 0.0,
            "timeout_rate": (b["timeout"] / n * 100.0) if n > 0 else 0.0,
            "avg_hold_days": (sum(b["days"]) / n) if n > 0 else 0.0,
            "profit_factor": profit_factor,
            "sample_ok": n >= MIN_SAMPLE,
            "sample_label": ("样本充足" if n >= STRONG_SAMPLE else ("样本中等" if n >= MIN_SAMPLE else "样本不足")),
        })
    # 排序：可信样本优先 → 期望值（最该看的指标）
    rows.sort(key=lambda r: (r["sample_ok"], r["avg_pnl_pct"]), reverse=True)

    # 全局
    total_n = len(ok)
    total_win = sum(1 for t in ok if t.net_pnl > 0)
    total_profit = sum(t.net_pnl for t in ok if t.net_pnl > 0)
    total_loss = sum(-t.net_pnl for t in ok if t.net_pnl < 0)
    global_pf = (total_profit / total_loss) if total_loss > 0 else (float("inf") if total_profit > 0 else 0.0)
    global_avg_pct = (sum(t.net_pnl_pct for t in ok) / total_n) if total_n > 0 else 0.0
    return {
        "available": True,
        "rows": rows,
        "summary": {
            "n_trades": total_n,
            "win_rate": (total_win / total_n * 100.0) if total_n > 0 else 0.0,
            "avg_pnl_pct": global_avg_pct,
            "profit_factor": global_pf,
            "total_pnl_amt": sum(t.net_pnl for t in ok),
        },
        "config": cfg or StopRuleConfig(),
    }


def _load_bars(con: Any, codes: List[str], since: str) -> Dict[str, List[Dict[str, Any]]]:
    """按代码加载日线 OHLC（升序）。"""
    if not codes:
        return {}
    placeholders = ",".join(["?"] * len(codes))
    rows = con.execute(
        f"SELECT ts_code, CAST(trade_date AS VARCHAR) AS trade_date, open, high, low, close "
        f"FROM daily_data WHERE ts_code IN ({placeholders}) AND CAST(trade_date AS VARCHAR) >= ? "
        f"ORDER BY ts_code, trade_date",
        list(codes) + [since],
    ).fetchall()
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        code = str(r[0])
        out.setdefault(code, []).append({
            "trade_date": str(r[1] or "").replace("-", "")[:8],
            "open": float(r[2] or 0.0), "high": float(r[3] or 0.0),
            "low": float(r[4] or 0.0), "close": float(r[5] or 0.0),
        })
    return out


def run_stop_rule_backtest(
    con: Any,
    *,
    cfg: Optional[StopRuleConfig] = None,
    pool: Optional[str] = None,
    lookback_days: int = 180,
    use_paper_config: bool = True,
) -> Dict[str, Any]:
    """从 signal_log 读取信号，按真实止损规则回测并聚合。

    【关键】use_paper_config=True（默认）时，止盈/止损/滑点/持有天数/费率
    直接取自模拟盘配置（config.yaml 的 paper_trading 段），
    保证「回测用的规则」与「模拟盘/实盘跑的规则」**永远一致**。
    这是本改造的核心：口径统一，结论才有意义。
    """
    c = cfg or StopRuleConfig()
    if use_paper_config and cfg is None:
        try:
            from core.paper_trading.config import load_config

            pc = load_config()
            c = StopRuleConfig(
                take_profit_pct=pc.take_profit_pct,
                stop_loss_pct=pc.stop_loss_pct,
                day_drop_exit_pct=float(getattr(pc, "day_drop_exit_pct", -3.0)),
                max_hold_days=int(getattr(pc, "max_hold_days", 10)),
                slippage_pct=pc.slippage_pct,
                commission_rate=pc.commission_rate,
                commission_min=pc.commission_min,
                stamp_tax_rate=pc.stamp_tax_rate,
                transfer_fee_rate=pc.transfer_fee_rate,
            )
        except Exception as e:  # noqa: BLE001
            _log.warning("[止损规则回测] 读模拟盘配置失败，用默认值: %s", e)
    import datetime as _dt

    since = (_dt.date.today() - _dt.timedelta(days=int(lookback_days))).strftime("%Y%m%d")
    sql = (
        "SELECT ts_code, name, pool, strategy_ids, push_price, "
        "CAST(log_date AS VARCHAR) AS sig_date "
        "FROM signal_log WHERE push_price > 0 AND CAST(log_date AS VARCHAR) >= ? "
    )
    params: List[Any] = [since]
    if pool:
        sql += " AND pool = ?"
        params.append(pool)
    sql += " ORDER BY log_date"
    try:
        sig_rows = con.execute(sql, params).fetchall()
    except Exception as e:  # noqa: BLE001
        _log.warning("[止损规则回测] 读取 signal_log 失败: %s", e)
        return {"available": False, "error": str(e)[:200], "rows": [], "trades": []}

    entries: List[Dict[str, Any]] = []
    for r in sig_rows:
        entries.append({
            "ts_code": str(r[0] or ""),
            "name": str(r[1] or ""),
            "pool": str(r[2] or ""),
            "strategy_ids": str(r[3] or ""),
            "push_price": float(r[4] or 0.0),
            "signal_date": str(r[5] or "").replace("-", "")[:8],
        })
    codes = sorted({e["ts_code"] for e in entries if e["ts_code"]})
    bars = _load_bars(con, codes, since)

    trades: List[Trade] = []
    for e in entries:
        b = bars.get(e["ts_code"]) or []
        if not b:
            continue
        trades.append(simulate_one(e, b, c))

    result = aggregate(trades, c)
    result["trades"] = [
        {"ts_code": t.ts_code, "strategy_ids": t.strategy_ids, "entry_date": t.entry_date,
         "exit_date": t.exit_date, "exit_reason": t.exit_reason, "holding_days": t.holding_days,
         "net_pnl_pct": round(t.net_pnl_pct, 2), "status": t.status}
        for t in trades if t.status == "ok"
    ]
    result["pending_count"] = sum(1 for t in trades if t.status == "pending")
    return result


def format_report(result: Dict[str, Any]) -> str:
    """把回测结果格式化成可读文本。"""
    if not result.get("available"):
        return f"[止损规则回测] 不可用: {result.get('error')}"
    cfg: StopRuleConfig = result.get("config") or StopRuleConfig()
    s = result.get("summary") or {}
    lines = [
        "===== 按真实止损规则回测（+%.0f%%止盈 / %.0f%%止损 / 最多持有%d日 / 滑点%.2f%%）=====" % (
            cfg.take_profit_pct, cfg.stop_loss_pct, cfg.max_hold_days, cfg.slippage_pct),
        "总成交笔数: %d | 胜率: %.1f%% | 平均每笔净收益: %+.2f%% | 盈利因子: %s" % (
            int(s.get("n_trades", 0)), float(s.get("win_rate", 0.0)),
            float(s.get("avg_pnl_pct", 0.0)),
            ("%.2f" % s["profit_factor"]) if s.get("profit_factor") not in (None) and s.get("profit_factor") != float("inf") else "∞"),
        "（未成熟待观察: %d 条，不计入统计）" % int(result.get("pending_count", 0)),
        "",
        "%-10s %5s %7s %9s %7s %7s %8s %7s %8s" % (
            "战法", "样本", "胜率", "期望/笔", "止盈率", "止损率", "当日跌幅", "均持仓", "盈利因子"),
    ]
    for r in (result.get("rows") or [])[:20]:
        pf = r.get("profit_factor")
        pf_s = ("∞" if pf == float("inf") else ("%.2f" % pf)) if pf is not None else "—"
        lines.append("%-10s %5d %6.1f%% %8+.2f%% %6.0f%% %6.0f%% %6.0f%% %6.1f %8s  %s" % (
            r["strategy_id"], r["n"], r["win_rate"], r["avg_pnl_pct"],
            r["take_rate"], r["stop_rate"], r.get("day_drop_rate", 0.0),
            r["avg_hold_days"], pf_s, r["sample_label"]))
    lines.append("")
    lines.append("解读：期望/笔 >0 才有长期赚钱可能；盈利因子 >1.5 健康、>2 优秀；"
                 "样本不足的战法先别下结论。")
    return "\n".join(lines)


def _self_test() -> None:
    """纯逻辑自检：覆盖止盈、止损、跳空、到期、T+1 不卖。"""
    cfg = StopRuleConfig(take_profit_pct=8.0, stop_loss_pct=-3.0, max_hold_days=10, slippage_pct=0.0)
    # 入场价 = 次日 open = 10.0；止盈 10.80，止损 9.70
    base = [
        {"trade_date": "20260101", "open": 9.9, "high": 10.0, "low": 9.8, "close": 9.9},   # 信号日
        {"trade_date": "20260102", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1},  # 买入日
        {"trade_date": "20260103", "open": 10.1, "high": 10.6, "low": 10.0, "close": 10.4},  # 未触发
        {"trade_date": "20260104", "open": 10.5, "high": 10.9, "low": 10.4, "close": 10.8},  # 触止盈(10.8)
    ]
    tr = simulate_one({"ts_code": "600000", "strategy_ids": ["P3-01"], "signal_date": "20260101"}, base, cfg)
    assert tr.status == "ok", tr.status
    assert tr.entry_price == 10.0, tr.entry_price
    assert tr.exit_reason == "TAKE_PROFIT", tr.exit_reason
    assert tr.holding_days == 2, tr.holding_days   # 买入日+2
    assert tr.net_pnl_pct > 7.0, tr.net_pnl_pct

    # 止损：先跌到 9.6（<9.70）
    base2 = [
        {"trade_date": "20260101", "open": 9.9, "high": 10.0, "low": 9.8, "close": 9.9},
        {"trade_date": "20260102", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1},
        {"trade_date": "20260103", "open": 10.0, "high": 10.1, "low": 9.6, "close": 9.65},  # 触止损
    ]
    tr2 = simulate_one({"ts_code": "600001", "strategy_ids": ["P3-02"], "signal_date": "20260101"}, base2, cfg)
    assert tr2.exit_reason == "STOP_LOSS", tr2.exit_reason
    assert abs(tr2.exit_price - 9.70) < 0.001, tr2.exit_price
    assert tr2.net_pnl_pct < -2.0, tr2.net_pnl_pct

    # 跳空低开：次日直接开在 9.5（低于止损 9.70）→ 按开盘 9.5 成交，亏损更大
    base3 = [
        {"trade_date": "20260101", "open": 9.9, "high": 10.0, "low": 9.8, "close": 9.9},
        {"trade_date": "20260102", "open": 10.0, "high": 10.2, "low": 9.9, "close": 10.1},
        {"trade_date": "20260103", "open": 9.5, "high": 9.6, "low": 9.4, "close": 9.45},  # 跳空低开
    ]
    tr3 = simulate_one({"ts_code": "600002", "strategy_ids": ["P3-03"], "signal_date": "20260101"}, base3, cfg)
    assert tr3.exit_reason == "STOP_LOSS", tr3.exit_reason
    assert abs(tr3.exit_price - 9.5) < 0.001, ("跳空应按开盘价成交", tr3.exit_price)
    assert tr3.net_pnl_pct < -4.0, tr3.net_pnl_pct   # 比 -3% 更惨，诚实

    # 数据不足（信号太新）→ pending
    tr4 = simulate_one({"ts_code": "600003", "strategy_ids": ["P3-04"], "signal_date": "20260102"},
                       base2[:2], cfg)
    assert tr4.status == "pending", tr4.status

    # 到期强平：连续小涨没触发止盈，第10日收盘平
    base5 = [{"trade_date": "20260101", "open": 9.9, "high": 10.0, "low": 9.8, "close": 9.9},
             {"trade_date": "20260102", "open": 10.0, "high": 10.1, "low": 9.9, "close": 10.0}]
    for k in range(12):
        base5.append({"trade_date": "202601%02d" % (3 + k), "open": 10.0, "high": 10.2,
                      "low": 9.95, "close": 10.05})
    tr5 = simulate_one({"ts_code": "600004", "strategy_ids": ["P3-05"], "signal_date": "20260101"}, base5, cfg)
    assert tr5.exit_reason == "TIMEOUT", tr5.exit_reason
    assert tr5.holding_days == 10, tr5.holding_days

    # 【2026-11-05】当日跌幅离场：浮盈也会被这条规则打掉
    #   买入 10.0 → 买入日收盘 10.5；次日相对昨收回落 3%+（低点 10.15）
    #   当日跌幅线 = 10.5×0.97 = 10.185 → 命中后按 10.185 离场，此时相对买入价仍是浮盈
    base6 = [
        {"trade_date": "20260101", "open": 9.9, "high": 10.0, "low": 9.8, "close": 9.9},
        {"trade_date": "20260102", "open": 10.0, "high": 10.6, "low": 9.95, "close": 10.5},  # 买入日
        {"trade_date": "20260103", "open": 10.45, "high": 10.5, "low": 10.15, "close": 10.2},  # 当日跌幅离场
    ]
    tr6 = simulate_one({"ts_code": "600005", "strategy_ids": ["P3-06"], "signal_date": "20260101"}, base6, cfg)
    assert tr6.exit_reason == "DAY_DROP", tr6.exit_reason
    assert abs(tr6.exit_price - 10.19) < 0.011, ("当日跌幅线应取 10.5×0.97", tr6.exit_price)
    assert tr6.net_pnl_pct > 0, ("当日跌幅离场不应被算成亏损", tr6.net_pnl_pct)
    # 关闭该规则（0）后，同一根 K 线不应再因「当日跌幅」离场（数据太新 → pending）
    tr6b = simulate_one(
        {"ts_code": "600005", "strategy_ids": ["P3-06"], "signal_date": "20260101"},
        base6,
        StopRuleConfig(take_profit_pct=8.0, stop_loss_pct=-3.0, day_drop_exit_pct=0.0,
                       max_hold_days=10, slippage_pct=0.0),
    )
    assert tr6b.exit_reason != "DAY_DROP", ("关闭后不应按当日跌幅离场", tr6b.exit_reason)
    assert tr6b.status == "pending", (tr6b.status, tr6b.exit_reason)

    # 聚合
    agg = aggregate([tr, tr2, tr3, tr5, tr6])
    assert agg["summary"]["n_trades"] == 5, agg["summary"]
    print("[stop_rule_backtest] self-test OK | 止盈/止损/跳空/到期/当日跌幅/T+1 全通过")


if __name__ == "__main__":
    _self_test()


__all__ = [
    "StopRuleConfig",
    "Trade",
    "simulate_one",
    "aggregate",
    "run_stop_rule_backtest",
    "format_report",
    "MIN_SAMPLE",
    "STRONG_SAMPLE",
]
