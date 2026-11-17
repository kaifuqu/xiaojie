# -*- coding: utf-8 -*-
"""core/execution_model.py — A 股成交语义与费用模型（唯一真值）· Tier2-P0-2

设计来源：对标 hikyuu 的 CostRecord / buy_delay / 一字板递延 / 滑点夹逼。

【定位】
本文件是【新增文件】，不改动任何现有模块的评分权重、阈值、闸门与策略逻辑。
它只回答一个此前全系统有三种答案的问题：「这一笔成交，到底该按什么价、扣多少钱」。

现状（改造前）三套口径：
  1) core/backtest_runner.run_strategy_backtest  → 扁平买 +0.225% / 卖 -0.275%（≈真实 6 倍）
  2) core/backtest_runner.run_painpoint_backtest → 0 成本、close→close
  3) core/paper_trading/fees.py                  → 佣金万3最低5元 + 印花税万5 + 过户费十万分之1
本文件把 3) 的逐项口径固化为唯一真值，供 1) 2) 复用。

【A 股规则（本文件实现）】
  · T+1：买入当日不可卖，最早次一交易日（is_sellable 判定）
  · 涨跌停：主板 ±10% / ST ±5% / 创业板 300·301 ±20% / 科创板 688·689 ±20%
    （北交所不在本系统范围内，不实现）
  · 一手 = 100 股，买入必须整手
  · 佣金：双向，万 3，最低 5 元
  · 印花税：仅卖出，万 5
  · 过户费：双向，十万分之 1
  · 滑点：默认 0，按 bps 配置；成交价必须落在当日 [low, high]
  · 一字板：开盘即涨停（open==high==涨停价）买不进；开盘即跌停（open==low==跌停价）卖不出

【自检】python -m core.execution_model
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional, Tuple

# ============================ 费率常量 ============================
COMMISSION_RATE = 0.0003      # 佣金 万 3（双向）
COMMISSION_MIN = 5.0          # 佣金最低 5 元
STAMP_TAX_RATE = 0.0005       # 印花税 万 5（仅卖出）
TRANSFER_FEE_RATE = 0.00001   # 过户费 十万分之 1（双向）
DEFAULT_SLIPPAGE_BPS = 0.0    # 默认滑点 0 bps
LOT_SIZE = 100                # 一手 100 股
EPS = 1e-6


# ============================ 涨跌停 ============================
def limit_pct(ts_code: str, is_st: bool = False) -> float:
    """按板块返回涨跌停幅度。北交所不在本系统范围，不实现。"""
    code = (ts_code or "").strip().upper()
    if code.startswith(("300", "301")):
        return 0.20
    if code.startswith(("688", "689")):
        return 0.20
    if is_st:
        return 0.05
    return 0.10


def limit_prices(pre_close: float, ts_code: str, is_st: bool = False) -> Tuple[float, float]:
    """返回 (涨停价, 跌停价)，按 A 股四舍五入到分。"""
    if pre_close is None or pre_close <= 0:
        return 0.0, 0.0
    pct = limit_pct(ts_code, is_st)
    up = round(pre_close * (1.0 + pct) + EPS, 2)
    down = round(pre_close * (1.0 - pct) + EPS, 2)
    return up, down


# ============================ 费用 ============================
@dataclass
class CostRecord:
    """单边成本明细（买卖分开记账，与 hikyuu CostRecord 同构）。"""
    side: str            # 'buy' | 'sell'
    amount: float        # 成交金额（价 × 股）
    commission: float
    stamp_tax: float
    transfer_fee: float
    slippage_cost: float = 0.0

    @property
    def total(self) -> float:
        return round(self.commission + self.stamp_tax + self.transfer_fee + self.slippage_cost, 4)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["total"] = self.total
        return d


def calc_cost(amount: float, side: str, slippage_cost: float = 0.0) -> CostRecord:
    """计算单边费用。side: 'buy' / 'sell'。"""
    amount = max(0.0, float(amount or 0.0))
    side = (side or "").lower()
    commission = max(COMMISSION_MIN, amount * COMMISSION_RATE) if amount > 0 else 0.0
    stamp_tax = amount * STAMP_TAX_RATE if side == "sell" else 0.0
    transfer_fee = amount * TRANSFER_FEE_RATE
    return CostRecord(
        side=side,
        amount=round(amount, 2),
        commission=round(commission, 2),
        stamp_tax=round(stamp_tax, 2),
        transfer_fee=round(transfer_fee, 4),
        slippage_cost=round(float(slippage_cost or 0.0), 2),
    )


def round_lot(shares: float) -> int:
    """向下取整到一手（100 股）。"""
    try:
        n = int(float(shares))
    except Exception:
        return 0
    return max(0, (n // LOT_SIZE) * LOT_SIZE)


# ============================ 成交判定 ============================
def fill_price(
    side: str,
    open_px: float,
    high: float,
    low: float,
    pre_close: float,
    ts_code: str,
    is_st: bool = False,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
    suspended: bool = False,
) -> Tuple[Optional[float], str]:
    """返回 (成交价, 原因)。成交价 None 表示'无法成交'（一字板/停牌/无有效报价）。

    规则优先级：停牌 → 无有效报价 → 一字涨停/跌停 → 滑点 → 夹逼到 [low, high]。
    """
    if suspended:
        return None, "停牌不可成交"
    try:
        o, h, l = float(open_px), float(high), float(low)
    except Exception:
        return None, "无有效报价"
    if o <= 0 or h <= 0 or l <= 0:
        return None, "无有效报价"

    up, down = limit_prices(float(pre_close or 0.0), ts_code, is_st)
    side = (side or "").lower()

    if up > 0 and o >= up - EPS and h <= up + EPS and l >= up - EPS:
        return None, "一字涨停买不进"
    if down > 0 and o <= down + EPS and h <= down + EPS and l >= down - EPS:
        return None, "一字跌停卖不出"

    slip = float(slippage_bps or 0.0) / 10000.0
    px = o * (1.0 + slip) if side == "buy" else o * (1.0 - slip)
    px = min(max(px, l), h)          # 滑点夹逼：绝不越出当日真实区间
    return round(px, 3), "ok"


def is_sellable(buy_date: str, today: str) -> bool:
    """T+1：买入当日不可卖。日期格式 YYYY-MM-DD 或 YYYYMMDD。"""
    if not buy_date or not today:
        return False
    a = str(buy_date).replace("-", "")
    b = str(today).replace("-", "")
    return b > a


# ============================ 组合层计算 ============================
def net_return_pct(
    buy_px: float,
    sell_px: float,
    shares: int,
    slippage_bps: float = DEFAULT_SLIPPAGE_BPS,
) -> dict:
    """一笔完整交易的净收益（已扣佣金/印花税/过户费）。"""
    shares = int(shares or 0)
    if buy_px <= 0 or sell_px <= 0 or shares <= 0:
        return {"gross_pct": 0.0, "net_pct": 0.0, "cost_amt": 0.0, "cost_pct": 0.0}
    buy_amt = buy_px * shares
    sell_amt = sell_px * shares
    slip_cost = buy_amt * (float(slippage_bps or 0.0) / 10000.0) * 2
    cb = calc_cost(buy_amt, "buy")
    cs = calc_cost(sell_amt, "sell")
    gross = (sell_amt - buy_amt) / buy_amt * 100.0
    cost_amt = cb.total + cs.total + slip_cost
    net = (sell_amt - buy_amt - cost_amt) / buy_amt * 100.0
    return {
        "gross_pct": round(gross, 4),
        "net_pct": round(net, 4),
        "cost_amt": round(cost_amt, 2),
        "cost_pct": round(cost_amt / buy_amt * 100.0, 4),
    }


def position_size_by_risk(
    equity: float,
    risk_per_trade_pct: float,
    entry_px: float,
    stop_px: float,
    max_position_pct: float = 0.20,
) -> dict:
    """按「单笔最大亏损」反推股数（对标 day-trader-skill Budget/Risk）。
    返回 {shares, position_amt, est_loss, capped}。
    """
    equity = float(equity or 0.0)
    entry_px = float(entry_px or 0.0)
    stop_px = float(stop_px or 0.0)
    if equity <= 0 or entry_px <= 0 or stop_px <= 0 or stop_px >= entry_px:
        return {"shares": 0, "position_amt": 0.0, "est_loss": 0.0, "capped": False}
    risk_amt = equity * float(risk_per_trade_pct or 0.0) / 100.0
    per_share_risk = entry_px - stop_px
    raw = risk_amt / per_share_risk if per_share_risk > 0 else 0.0
    shares = round_lot(raw)
    cap_amt = equity * float(max_position_pct or 1.0)
    capped = False
    if shares * entry_px > cap_amt:
        shares = round_lot(cap_amt / entry_px)
        capped = True
    return {
        "shares": shares,
        "position_amt": round(shares * entry_px, 2),
        "est_loss": round(shares * per_share_risk, 2),
        "capped": capped,
    }


def buy_fill(open_px, high, low, pre_close, ts_code, shares, is_st=False,
             slippage_bps=DEFAULT_SLIPPAGE_BPS, suspended=False) -> dict:
    """模拟买入：返回 {filled, price, shares, amount, cost, total_outlay, reason}。"""
    px, reason = fill_price("buy", open_px, high, low, pre_close, ts_code,
                            is_st, slippage_bps, suspended)
    shares = round_lot(shares)
    if px is None or shares <= 0:
        return {"filled": False, "price": 0.0, "shares": 0, "amount": 0.0,
                "cost": 0.0, "total_outlay": 0.0, "reason": reason if px is None else "股数不足一手"}
    amt = px * shares
    c = calc_cost(amt, "buy")
    return {"filled": True, "price": px, "shares": shares, "amount": round(amt, 2),
            "cost": c.total, "total_outlay": round(amt + c.total, 2), "reason": "ok"}


def sell_fill(open_px, high, low, pre_close, ts_code, shares, buy_date, sell_date,
              is_st=False, slippage_bps=DEFAULT_SLIPPAGE_BPS, suspended=False) -> dict:
    """模拟卖出：T+1 校验 + 一字跌停不可卖。"""
    if not is_sellable(buy_date, sell_date):
        return {"filled": False, "price": 0.0, "shares": 0, "amount": 0.0,
                "cost": 0.0, "net_proceeds": 0.0, "reason": "T+1 当日不可卖"}
    px, reason = fill_price("sell", open_px, high, low, pre_close, ts_code,
                            is_st, slippage_bps, suspended)
    shares = int(shares or 0)
    if px is None or shares <= 0:
        return {"filled": False, "price": 0.0, "shares": 0, "amount": 0.0,
                "cost": 0.0, "net_proceeds": 0.0, "reason": reason}
    amt = px * shares
    c = calc_cost(amt, "sell")
    return {"filled": True, "price": px, "shares": shares, "amount": round(amt, 2),
            "cost": c.total, "net_proceeds": round(amt - c.total, 2), "reason": "ok"}


# ============================ 自检 ============================
if __name__ == "__main__":
    print("=== 涨跌停 ===")
    for code, st in [("600000.SH", False), ("300750.SZ", False), ("688981.SH", False),
                     ("000001.SZ", True)]:
        up, down = limit_prices(10.00, code, st)
        print(f"  {code:12s} ST={st!s:5s} 涨停={up:.2f} 跌停={down:.2f} 幅度={limit_pct(code, st):.0%}")

    print("\n=== 费用（买 10000 元 / 卖 10500 元）===")
    print("  买入:", calc_cost(10000, "buy").as_dict())
    print("  卖出:", calc_cost(10500, "sell").as_dict())

    print("\n=== 成交判定 ===")
    print("  一字涨停:", fill_price("buy", 11.00, 11.00, 11.00, 10.00, "600000.SH"))
    print("  一字跌停:", fill_price("sell", 9.00, 9.00, 9.00, 10.00, "600000.SH"))
    print("  正常买入:", fill_price("buy", 10.10, 10.50, 9.90, 10.00, "600000.SH"))
    print("  停牌:", fill_price("buy", 10.10, 10.50, 9.90, 10.00, "600000.SH", suspended=True))

    print("\n=== 一笔完整交易（买 10.00 × 1000 股 → 卖 10.50）===")
    print(" ", net_return_pct(10.00, 10.50, 1000))

    print("\n=== 按风险定仓（100 万本金，单笔风险 1%，买 10.00 止损 9.70）===")
    print(" ", position_size_by_risk(1_000_000, 1.0, 10.00, 9.70))

    print("\n=== 买入/卖出模拟 ===")
    print("  买:", buy_fill(10.10, 10.50, 9.90, 10.00, "600000.SH", 1234))
    print("  卖:", sell_fill(10.60, 10.80, 10.40, 10.50, "600000.SH", 1200, "2026-11-02", "2026-11-03"))
    print("  T+1拦截:", sell_fill(10.60, 10.80, 10.40, 10.50, "600000.SH", 1200, "2026-11-02", "2026-11-02"))
