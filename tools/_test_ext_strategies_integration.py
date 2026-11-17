# -*- coding: utf-8 -*-
"""
tools/_test_ext_strategies_integration.py — 外部策略接入完整性测试（离线·合成数据）

覆盖：
  A. 模块导入与注册：P3-11 / P4-08 / P5-22~P5-14 均出现在对应筛选器注册表
  B. P3-11 RSI 超跌反转：命中/否决用例
  C. P4-08 双均线金叉确认：命中/否决用例
  D. P5 prepare 滚动列 + P5-22~P5-14 mask 命中/不误伤
  E. 单票筛选入口不抛异常，strategy_checks / masks 均包含新战法名
  F. 模拟盘链路：含新战法名的信号行可解析（parse_signal_row）并带出原子 ID

运行：python tools/_test_ext_strategies_integration.py
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
os.environ.setdefault("TZ", "Asia/Shanghai")

import numpy as np
import pandas as pd

FAILS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"[OK]   {name}")
    else:
        print(f"[FAIL] {name}  {detail}")
        FAILS.append(name)


def _build_daily_df(closes: list, vols: list | None = None,
                    ts_code: str = "600000.SH") -> pd.DataFrame:
    """由收盘价序列合成 OHLCV + 全套均线/MACD 列（口径对齐 _recompute_p5_tail_indicators）。"""
    n = len(closes)
    close = pd.Series(closes, dtype=float)
    ret = close.pct_change().fillna(0.0)
    open_ = close.shift(1).fillna(close.iloc[0])
    high = pd.concat([close, open_], axis=1).max(axis=1) * 1.012
    low = pd.concat([close, open_], axis=1).min(axis=1) * 0.988
    vol = pd.Series(vols if vols is not None else [100000.0] * n, dtype=float)

    df = pd.DataFrame({
        "ts_code": ts_code,
        "trade_date": pd.date_range("2025-01-01", periods=n, freq="B").strftime("%Y%m%d"),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "pre_close": open_,
        "pct_chg": ret * 100.0,
        "vol": vol,
        "vol_ratio": (vol / vol.rolling(5, min_periods=1).mean()).clip(0.1, 9.9),
        "turnover_rate_f": 2.0,
        "pe_ttm": 20.0, "pb": 3.0, "ps_ttm": 3.0, "dv_ratio": 1.0,
        "circ_mv": 2_500_000.0,   # 万 = 250亿
        "winner_rate": 85.0,
        "net_main_amount": 2.0e7,
        "inst_net_buy": 1.0e7,
        "cost_50th": close * 0.97, "cost_95th": close * 1.05,
        "limit_times": 0, "strth": 60.0,
    })
    for w in (5, 10, 20, 60, 120, 250):
        df[f"ma{w}"] = close.rolling(w, min_periods=1).mean()
    for w in (5, 10, 20):
        df[f"vol_ma{w}"] = vol.rolling(w, min_periods=1).mean()
    df["ma20_slope_5"] = df["ma20"].pct_change(5).fillna(0.0) * 100.0
    df["high_20"] = high.rolling(20, min_periods=1).max()
    df["low_60"] = low.rolling(60, min_periods=1).min()
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    df["macd"] = dif
    df["macd_signal"] = dea
    df["macd_hist"] = dif - dea
    df["macd_bar"] = (dif - dea) * 2.0
    df["atr_pct"] = 2.0
    df["bias_20"] = (close - df["ma20"]) / df["ma20"] * 100.0
    return df


def _rt_from_row(y: pd.Series, **over) -> dict:
    rt = {
        "ts_code": str(y.get("ts_code", "")),
        "price": float(y["close"]),
        "open": float(y["open"]),
        "high": float(y["high"]),
        "low": float(y["low"]),
        "pre_close": float(y["pre_close"]),
        "volume": float(y["vol"]) * 100.0,
        "amount": float(y["vol"]) * 100.0 * float(y["close"]),
        "vol_ratio": float(y.get("vol_ratio", 1.5)),
        "turnover_rate_f": 2.0,
        "circ_mv": float(y.get("circ_mv", 2_500_000.0)),
        "winner_rate": float(y.get("winner_rate", 85.0)),
        "net_main_amount": float(y.get("net_main_amount", 2.0e7)),
        "inst_net_buy": float(y.get("inst_net_buy", 1.0e7)),
        "cost_50th": float(y.get("cost_50th", 0.0)),
        "cost_95th": float(y.get("cost_95th", 0.0)),
        "curr_min": 900,
        "is_backtest": True,
        "trade_date": str(y.get("trade_date", "")),
        "name": "测试股",
    }
    rt.update(over)
    return rt


# ==================== A. 注册表 ====================
def test_registration() -> None:
    print("\n========== A. 注册表 ==========")
    import inspect
    from core.strategies import p3_intraday_screener, p4_tail_screener, p5_postmarket_screener

    src3 = inspect.getsource(p3_intraday_screener.evaluate_p3_intraday_screener)
    check("P3-11 已注册", "P3-11·★RSI超跌反转" in src3)

    src4 = inspect.getsource(p4_tail_screener.evaluate_p4_tail_screener)
    check("P4-08 已注册", "P4-08·★双均线金叉确认" in src4)

    src5 = inspect.getsource(p5_postmarket_screener.strategy_masks_p5)
    for sid in ("P5-22·★双均线金叉", "P5-23·★布林上轨突破", "P5-24·★量价新高突破",
                "P5-25·★MACD金叉趋势", "P5-14·★欧奈尔趋势模板"):
        check(f"{sid} 已注册", sid in src5)

    from core.paper_trading.strategy_labels import strategy_label
    for sid in ("P3-11", "P4-08", "P5-22", "P5-23", "P5-24", "P5-25", "P5-14"):
        lab = strategy_label(sid)
        check(f"标签 {sid} 已登记", "待补充" not in lab and lab != "历史战法(已退役)", lab)

    from core.strategies.p5_postmarket_screener import _P5_ACTION_PRIORITY, p5_hits_are_actionable
    keys = [k for k, _ in _P5_ACTION_PRIORITY]
    for sid in ("P5-22", "P5-23", "P5-24", "P5-25", "P5-14"):
        check(f"动作分档 {sid}", any(sid in k for k in keys))
    check("P5-22 命中可进池(追涨档)",
          p5_hits_are_actionable(["P5-22·★双均线金叉"]))


# ==================== B. P3-11 RSI ====================
def test_p3_rsi() -> None:
    print("\n========== B. P3-11 RSI 超跌反转 ==========")
    from core.strategies.p3_intraday_screener import (
        P3IntradayScreenerConfig, _strategy_rsi_reversal, _rsi_sma,
    )

    cfg = P3IntradayScreenerConfig()
    # 连续温和下跌 25 天 → RSI 超卖但 bias 不深（防飞刀门槛内）
    closes = list(np.linspace(20.0, 16.5, 25))
    df = _build_daily_df(closes)
    y = df.iloc[-1]
    rsi = _rsi_sma(df["close"], 14)
    check("RSI 计算正常", np.isfinite(rsi), f"rsi={rsi}")
    check("下跌序列 RSI<30", rsi < 30.0, f"rsi={rsi:.2f}")

    now_px = float(y["close"]) * 1.012
    low_px = float(y["close"]) * 0.995
    ok, msg = _strategy_rsi_reversal(
        df, {"vol_ratio": 1.0}, y, cfg,
        pct=-0.5, now_px=now_px, low_px=low_px,
    )
    check("超跌+缩量+脱离低点 → 命中", ok, msg)

    ok2, msg2 = _strategy_rsi_reversal(
        df, {"vol_ratio": 2.5}, y, cfg,
        pct=-0.5, now_px=now_px, low_px=low_px,
    )
    check("放量下跌 → 否决", not ok2, msg2)

    ok3, msg3 = _strategy_rsi_reversal(
        df, {"vol_ratio": 1.0}, y, cfg,
        pct=4.5, now_px=now_px, low_px=low_px,
    )
    check("涨幅超上限 → 否决", not ok3, msg3)

    # 陡峭下跌 → bias_20 过深，防飞刀拦截
    closes_knife = list(np.linspace(20.0, 13.6, 25))
    df_k = _build_daily_df(closes_knife)
    y_k = df_k.iloc[-1]
    now_k = float(y_k["close"])
    ok4, msg4 = _strategy_rsi_reversal(
        df_k, {"vol_ratio": 1.0}, y_k, cfg,
        pct=-0.5, now_px=now_k, low_px=now_k * 0.99,
    )
    check("bias 过深切飞刀 → 否决", not ok4, msg4)


# ==================== C. P4-08 金叉 ====================
def test_p4_cross() -> None:
    print("\n========== C. P4-08 双均线金叉确认 ==========")
    from core.strategies.p4_tail_screener import (
        P4TailScreenerConfig, _strategy_ma_cross_confirmed,
    )

    cfg = P4TailScreenerConfig()
    # 26 根横盘 + 尾日小幅上行 → MA5 恰在尾日上穿 MA20
    closes = [10.0] * 26 + [10.05]
    df = _build_daily_df(closes)
    y = df.iloc[-1]
    cross_recent = any(
        df.iloc[-1 - k]["ma5"] > df.iloc[-1 - k]["ma20"]
        and df.iloc[-2 - k]["ma5"] <= df.iloc[-2 - k]["ma20"]
        for k in range(2)
    )
    check("构造序列含近 2 日金叉", cross_recent)

    close_live = float(y["close"]) * 1.012
    pct = 1.2  # 尾盘价格相对昨收 +1.2%
    rt = {"vol_ratio": 1.5}
    ok, msg = _strategy_ma_cross_confirmed(df, rt, y, close_live, pct, cfg)
    check("金叉+确认 → 命中(或给出具体原因)", ok, msg)

    if not ok:
        # 若 MACD 柱在此序列尚未转正，放宽后必须能命中，证明逻辑仅被动能条件拦住
        cfg2 = P4TailScreenerConfig()
        cfg2.s13_require_macd_positive = False
        ok_b, msg_b = _strategy_ma_cross_confirmed(df, rt, y, close_live, pct, cfg2)
        check("关闭 MACD 门槛后命中（说明其它条件已满足）", ok_b, msg_b)
        check("MACD 门槛确实由配置控制", "MACD" in msg)

    ok2, msg2 = _strategy_ma_cross_confirmed(df, {"vol_ratio": 1.5}, y,
                                             float(y["close"]) * 1.06,
                                             6.0, cfg)
    check("涨幅超上限 → 否决", not ok2, msg2)

    ok3, msg3 = _strategy_ma_cross_confirmed(df, {"vol_ratio": 0.5}, y,
                                             close_live, pct, cfg)
    check("量比不足 → 否决", not ok3, msg3)


# ==================== D. P5 masks ====================
def test_p5_masks() -> None:
    print("\n========== D. P5-22~P5-14 mask ==========")
    from core.strategies.p5_postmarket_screener import (
        P5PostmarketConfig, prepare_p5_lags, strategy_masks_p5,
    )

    cfg = P5PostmarketConfig()

    # 长上升趋势（300 根）→ P5-14 欧奈尔模板
    closes = list(10.0 * (1.003 ** np.arange(300)))
    vols = [100000.0] * 300
    df_raw = _build_daily_df(closes, vols)
    prep = prepare_p5_lags(df_raw)
    for col in ("ma5_prev_c", "ma20_prev_c", "macd_prev", "macd_signal_prev",
                "high_250w", "low_250w", "ma250_lag20", "boll_mid", "boll_up"):
        check(f"prepare 产出 {col}", col in prep.columns)
    check("boll_up > boll_mid", bool((prep["boll_up"].dropna() >= prep["boll_mid"].dropna()).all()))

    masks = strategy_masks_p5(prep, cfg, market_contraction_score=0.0)
    names = ["P5-22·★双均线金叉", "P5-23·★布林上轨突破", "P5-24·★量价新高突破",
             "P5-25·★MACD金叉趋势", "P5-14·★欧奈尔趋势模板"]
    for n in names:
        check(f"mask 存在 {n}", n in masks)
    check("单边慢牛 → P5-14 命中(任意行)",
          bool(masks["P5-14·★欧奈尔趋势模板"].iloc[-40:].any()),
          f"tail hits={int(masks['P5-14·★欧奈尔趋势模板'].iloc[-40:].sum())}")

    # 双均线金叉：先跌后急涨
    closes2 = list(10.0 * (0.995 ** np.arange(30))) + list(
        np.linspace(10.0 * 0.995 ** 29, 10.0 * 0.995 ** 29 * 1.09, 12))
    df2 = _build_daily_df(closes2)
    prep2 = prepare_p5_lags(df2)
    m2 = strategy_masks_p5(prep2, cfg)
    check("先跌后急涨 → P5-22 金叉命中(任意行)", bool(m2["P5-22·★双均线金叉"].any()),
          str(m2["P5-22·★双均线金叉"].sum()))

    # 布林上轨突破 + 量价新高：尾日放量大阳（涨幅约 +4.5%）
    closes3 = [10.0] * 30 + [10.0, 10.1, 10.55]
    vols3 = [100000.0] * 30 + [120000.0, 130000.0, 400000.0]
    df3 = _build_daily_df(closes3, vols3)
    prep3 = prepare_p5_lags(df3)
    m3 = strategy_masks_p5(prep3, cfg)
    check("放量大阳 → P5-23 布林突破命中(尾日)", bool(m3["P5-23·★布林上轨突破"].iloc[-1]),
          f"pct={prep3['pct_chg'].iloc[-1]:.2f}")
    check("放量大阳 → P5-24 量价新高命中(尾日)", bool(m3["P5-24·★量价新高突破"].iloc[-1]))

    # MACD 金叉（零轴上方）：长升势 → 短回调 → 再上攻
    closes4 = (list(10.0 * (1.004 ** np.arange(200)))
               + list(10.0 * (1.004 ** 199) * (0.995 ** np.arange(1, 13)))
               + list(10.0 * (1.004 ** 199) * (0.995 ** 12) * (1.01 ** np.arange(1, 11))))
    df4 = _build_daily_df(closes4)
    prep4 = prepare_p5_lags(df4)
    m4 = strategy_masks_p5(prep4, cfg)
    check("零轴上方金叉 → P5-25 命中(任意行)", bool(m4["P5-25·★MACD金叉趋势"].any()),
          str(m4["P5-25·★MACD金叉趋势"].sum()))

    # 平静横盘 → 全部不误伤
    closes5 = [10.0 + 0.01 * np.sin(i / 3.0) for i in range(60)]
    df5 = _build_daily_df(closes5)
    prep5 = prepare_p5_lags(df5)
    m5 = strategy_masks_p5(prep5, cfg)
    calm_hits = int(sum(bool(m5[n].iloc[-10:].any()) for n in names))
    check("平静横盘 → 近10行新战法零误伤", calm_hits == 0, f"hits={calm_hits}")


# ==================== E. 单票入口冒烟 ====================
def test_entrypoints() -> None:
    print("\n========== E. 单票筛选入口冒烟 ==========")
    from core.strategies.p3_intraday_screener import evaluate_p3_intraday_screener
    from core.strategies.p4_tail_screener import evaluate_p4_tail_screener
    from core.strategies.p5_postmarket_screener import (
        P5PostmarketConfig, build_single_stock_window_from_rt,
        evaluate_p5_single_prepared_row, prepare_p5_lags,
    )

    closes = list(10.0 * (1.004 ** np.arange(140)))
    closes[-1] = closes[-2] * 1.02
    df = _build_daily_df(closes)
    y = df.iloc[-1]
    rt = _rt_from_row(y, vol_ratio=1.6)

    r3 = evaluate_p3_intraday_screener(df, dict(rt), None)
    check("P3 入口不抛异常", isinstance(r3, dict))
    check("P3 strategy_checks 含 P3-11",
          any("P3-11" in k for k in (r3.get("strategy_checks") or {})))

    r4 = evaluate_p4_tail_screener(df, dict(rt), None)
    check("P4 入口不抛异常", isinstance(r4, dict))
    check("P4 strategy_checks 含 P4-08",
          any("P4-08" in k for k in (r4.get("strategy_checks") or {})))

    work = build_single_stock_window_from_rt(df, dict(rt), "600000.SH")
    prep = prepare_p5_lags(work)
    row = prep.iloc[-1]
    gp, hits, veto, _ = evaluate_p5_single_prepared_row(
        row, P5PostmarketConfig(), stock_prep_df=prep, rt=dict(rt),
    )
    check("P5 单票入口不抛异常", isinstance(hits, list))
    print(f"       P5: gp={gp} veto={veto!r} hits={hits}")


# ==================== F. 模拟盘链路 ====================
def test_paper_trading_signal() -> None:
    print("\n========== F. 模拟盘信号解析 ==========")
    from core.paper_trading.engine import parse_signal_row

    row = {
        "代码": "600000.SH", "名称": "测试股", "现价": 12.5, "综合分": 82.0,
        "战法": "P5-25·★MACD金叉趋势 + P4-08·★双均线金叉确认",
        "pool_tier": "main", "执行层级": "A", "建议仓位": "10%",
    }
    sig = parse_signal_row(row)
    check("含新战法名的信号行可解析", sig is not None)
    if sig:
        check("原子 ID 提取含 P5-25", "P5-25" in sig["strategy_ids"], str(sig["strategy_ids"]))
        check("原子 ID 提取含 P4-08", "P4-08" in sig["strategy_ids"])
        check("标签提取非空", all(bool(n) for n in sig["strategy_names"]), str(sig["strategy_names"]))


def main() -> int:
    test_registration()
    test_p3_rsi()
    test_p4_cross()
    test_p5_masks()
    test_entrypoints()
    test_paper_trading_signal()

    print("\n" + "=" * 56)
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败")
        for f in FAILS:
            print("  -", f)
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
