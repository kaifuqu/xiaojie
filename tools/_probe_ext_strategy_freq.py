# -*- coding: utf-8 -*-
"""统计新战法在最近窗口的信号频率（用于 3 个月观察期前的基线评估）。"""
import glob
import io
import json
import os
import sys

_ROOT = r'd:\xiaojie'
sys.path.insert(0, _ROOT)
os.chdir(_ROOT)

import numpy as np
import pandas as pd

from core.strategies.p3_intraday_screener import _rsi_sma
from core.strategies.p4_tail_screener import P4TailScreenerConfig, _strategy_ma_cross_confirmed
from core.strategies.p5_postmarket_screener import (
    P5PostmarketConfig, prepare_p5_lags, strategy_masks_p5,
)

files = sorted(glob.glob(os.path.join(_ROOT, "data", "runtime", "pool_cache", "p1_cache_*.json")))
raw = json.load(open(files[-1], encoding="utf-8"))
rows = raw.get("items") if isinstance(raw, dict) else raw

LOOKBACK = 120  # 最近 120 个交易日
cnt = {k: 0 for k in ("P5-22", "P5-23", "P5-24", "P5-25", "P5-14", "P4-08", "P3-11")}
stock_days = 0
names = ["P5-22·★双均线金叉", "P5-23·★布林上轨突破", "P5-24·★量价新高突破",
         "P5-25·★MACD金叉趋势", "P5-14·★欧奈尔趋势模板"]

cfg4 = P4TailScreenerConfig()
cfg5 = P5PostmarketConfig()

n_stk = 0
for row in rows or []:
    sp = row.get("df_split")
    if not sp:
        continue
    df = pd.read_json(io.StringIO(json.dumps(sp)), orient="split")
    if df is None or df.empty or len(df) < 60:
        continue
    df = df.sort_values("trade_date").reset_index(drop=True)
    n_stk += 1
    tail = df.tail(LOOKBACK).reset_index(drop=True)
    stock_days += len(tail)

    # P5 系列（向量化）
    try:
        prep = prepare_p5_lags(df.copy())
        masks = strategy_masks_p5(prep, cfg5)
        n_prep = len(prep)
        m_tail = np.arange(n_prep) >= max(0, n_prep - LOOKBACK)
        for name, sid in zip(names, ("P5-22", "P5-23", "P5-24", "P5-25", "P5-14")):
            cnt[sid] += int(masks[name].values[m_tail].sum())
    except Exception:
        pass

    # P4-08（逐日尾盘近似：用当日收盘价作为尾盘现价）
    try:
        for i in range(max(2, len(df) - LOOKBACK), len(df)):
            sub = df.iloc[: i + 1]
            y = sub.iloc[-1]
            close = float(y.get("close") or 0)
            prev = float(sub.iloc[-2].get("close") or 0)
            if close <= 0 or prev <= 0:
                continue
            vr = float(y.get("vol_ratio") or 0)
            rt = {"price": close, "vol_ratio": vr}
            ok, _msg = _strategy_ma_cross_confirmed(sub, rt, y, close, (close - prev) / prev * 100.0, cfg4)
            cnt["P4-08"] += 1 if ok else 0
    except Exception:
        pass

    # P3-11（RSI 逐日近似）
    try:
        closes = pd.to_numeric(df["close"], errors="coerce")
        pct = closes.pct_change() * 100.0
        vr = pd.to_numeric(df.get("vol_ratio"), errors="coerce")
        for i in range(max(20, len(df) - LOOKBACK), len(df)):
            win = closes.iloc[: i + 1]
            rsi = _rsi_sma(win, 14)
            if not np.isfinite(rsi) or rsi > 30.0:
                continue
            p = float(pct.iloc[i] or 0)
            v = float(vr.iloc[i] or 0)
            if not (-1.5 <= p <= 2.0) or v <= 0 or v > 1.2:
                continue
            low_i = float(df["low"].iloc[i] or 0)
            c_i = float(closes.iloc[i] or 0)
            if low_i <= 0 or c_i <= 0:
                continue
            if (c_i - low_i) / low_i * 100.0 < 1.0:
                continue
            ma20 = float(df["ma20"].iloc[i] or 0)
            if ma20 > 0 and (c_i - ma20) / ma20 * 100.0 < -12.0:
                continue
            cnt["P3-11"] += 1
    except Exception:
        pass

print(f"样本：{n_stk} 只股票 × 最近 {LOOKBACK} 个交易日 = {stock_days} 个股票日")
for k in ("P5-22", "P5-23", "P5-24", "P5-25", "P5-14", "P4-08", "P3-11"):
    rate = cnt[k] / max(stock_days, 1)
    print(f"{k}: 命中 {cnt[k]} 次  （平均每千个股票日 {rate * 1000:.1f} 次）")
