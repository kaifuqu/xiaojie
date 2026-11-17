# -*- coding: utf-8 -*-
"""
core/paper_trading/fees.py — A 股交易费用计算

【口径说明】
  · 佣金（券商，双边）：max(成交额 × 佣金率, 最低佣金)
  · 印花税（仅卖出）：成交额 × 印花税率
  · 过户费（双边）：成交额 × 过户费率
  · 证管费 / 经手费：已含在券商佣金内，不重复计
  · 滑点：买入价上浮、卖出价下调（按 slippage_pct 百分比），默认 0 表示严格按信号价成交

【资金对账恒等式】
  买入现金流出 = 成交额 + 佣金 + 过户费
  卖出现金流入 = 成交额 − 佣金 − 印花税 − 过户费
  净盈亏 = 卖出现金流入 − 买入现金流出
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Dict

# 【V30.4 2026-10-18 A股合规】A 股最小价格变动单位是 0.01 元，
# 交易所的涨跌停价 / 成交价 / 委托价一律按「四舍五入到分」定价。
# 原实现用浮点乘法（如 pre_close * 0.9）再乘 1.001 容差判定跌停，
# 在 10.05 / 3.33 这类价格上会算出 9.044999999 之类的值，
# 导致「真跌停」被判成「未跌停」而错误成交。
PRICE_TICK = 0.01


def round_price(price: float) -> float:
    """按 A 股最小价位（0.01 元）四舍五入（ROUND_HALF_UP），返回 float。"""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return 0.0
    if p != p:  # NaN
        return 0.0
    return float(Decimal(str(p)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def limit_price(pre_close: float, pct: float) -> float:
    """涨跌停价 = 昨收 × (1 ± pct%)，按 0.01 元 ROUND_HALF_UP 取整。"""
    base = round_price(pre_close)
    return round_price(base * (1.0 + float(pct) / 100.0))


@dataclass
class FeeBreakdown:
    """单笔成交的费用拆解。"""

    gross_amount: float = 0.0
    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0
    total_fee: float = 0.0
    cash_delta: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        return {
            "gross_amount": round(float(self.gross_amount), 4),
            "commission": round(float(self.commission), 4),
            "stamp_tax": round(float(self.stamp_tax), 4),
            "transfer_fee": round(float(self.transfer_fee), 4),
            "total_fee": round(float(self.total_fee), 4),
            "cash_delta": round(float(self.cash_delta), 4),
        }


def compute_fee(
    *,
    price: float,
    shares: int,
    side: str,
    commission_rate: float,
    commission_min: float,
    stamp_tax_rate: float,
    transfer_fee_rate: float,
) -> FeeBreakdown:
    """计算单笔成交的费用与现金变动。

    side: "BUY" 或 "SELL"（大小写不敏感）
    """
    p = float(price)
    n = int(shares)
    if p <= 0 or n <= 0:
        return FeeBreakdown()
    s = str(side or "").strip().upper()
    gross = p * n
    commission = max(gross * float(commission_rate), float(commission_min))
    transfer = gross * float(transfer_fee_rate)
    if s == "BUY":
        stamp = 0.0
        cash_delta = -(gross + commission + transfer)
    else:
        stamp = gross * float(stamp_tax_rate)
        cash_delta = gross - commission - stamp - transfer
    return FeeBreakdown(
        gross_amount=gross,
        commission=commission,
        stamp_tax=stamp,
        transfer_fee=transfer,
        total_fee=commission + stamp + transfer,
        cash_delta=cash_delta,
    )


def apply_slippage(price: float, side: str, slippage_pct: float) -> float:
    """按滑点调整成交价：买入上浮、卖出下调；slippage_pct = 0 时原样返回。

    【V30.4】调整后统一按 0.01 元 ROUND_HALF_UP 取整 —— A 股不存在 0.001 元的成交价。
    """
    p = float(price)
    s = str(side or "").strip().upper()
    sp = abs(float(slippage_pct))
    if p <= 0 or sp <= 0:
        return round_price(p)
    if s == "BUY":
        return round_price(p * (1.0 + sp / 100.0))
    return round_price(p * (1.0 - sp / 100.0))


def _self_test() -> None:
    """内置自检：50 万买入 / 15% 卖出的全链路费用与资金恒等式。"""
    f = compute_fee(
        price=10.0, shares=1000, side="BUY",
        commission_rate=0.0003, commission_min=5.0,
        stamp_tax_rate=0.0005, transfer_fee_rate=0.00001,
    )
    assert abs(f.gross_amount - 10000.0) < 1e-6
    assert abs(f.commission - 5.0) < 1e-6, f.commission  # 10000*0.0003=3 元 < 最低 5 元
    assert abs(f.transfer_fee - 0.1) < 1e-6
    assert abs(f.stamp_tax - 0.0) < 1e-6
    assert abs(f.cash_delta + 10005.1) < 1e-6
    s = compute_fee(
        price=11.5, shares=1000, side="SELL",
        commission_rate=0.0003, commission_min=5.0,
        stamp_tax_rate=0.0005, transfer_fee_rate=0.00001,
    )
    assert abs(s.gross_amount - 11500.0) < 1e-6
    assert abs(s.commission - 5.0) < 1e-6, s.commission
    assert abs(s.stamp_tax - 5.75) < 1e-6, s.stamp_tax
    assert abs(s.transfer_fee - 0.115) < 1e-6
    assert abs(s.cash_delta - 11489.135) < 1e-3, s.cash_delta
    pnl = s.cash_delta + f.cash_delta
    assert pnl > 0
    print(f"[fees] self-test OK | buy_out=10005.10 sell_in=11489.135 net_pnl={pnl:.3f}")


if __name__ == "__main__":
    _self_test()


__all__ = ["FeeBreakdown", "PRICE_TICK", "apply_slippage", "compute_fee", "limit_price", "round_price"]
