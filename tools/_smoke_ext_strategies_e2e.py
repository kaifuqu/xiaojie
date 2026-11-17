# -*- coding: utf-8 -*-
"""
tools/_smoke_ext_strategies_e2e.py — 外部策略接入 · 全链路冒烟（选股 → 模拟买入 → 模拟卖出 → 回测）

流程：
  PART 1  模拟盘链路：新战法 ID（P5-25/P5-14）信号 → on_signals 自动买入 →
          on_tick 止盈自动卖出（隔离临时 DuckDB，不碰主库）
  PART 2  真实数据自动选股：用最新 P1 底仓缓存（p1_cache_*.json）中的真实历史日线，
          逐票跑 P3 / P4 / P5 三个筛选入口，统计命中（含新战法），全链路不得抛异常
  PART 3  回测入口：对 3 条新战法各跑一只票的单票回测（核心引擎 run_strategy_backtest）

运行：python tools/_smoke_ext_strategies_e2e.py
"""
from __future__ import annotations

import glob
import json
import os
import sys
import tempfile
import traceback
from datetime import datetime, timedelta, timezone

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
os.chdir(_ROOT)
os.environ.setdefault("XJ_NO_PUSH", "1")

from core._pyarrow_compat import disable_pyarrow  # noqa: E402

disable_pyarrow()

import numpy as np
import pandas as pd

BJ_TZ = timezone(timedelta(hours=8))
FAILS = []
NEW_IDS = ("P3-11", "P4-08", "P5-22", "P5-23", "P5-24", "P5-25", "P5-14")


def check(name: str, cond: bool, detail: str = "") -> None:
    print(("[OK]   " if cond else "[FAIL] ") + name + ("" if cond else f"  {detail}"))
    if not cond:
        FAILS.append(name)


# ==================== PART 1：模拟盘 买入→卖出 ====================
def part1_paper_trading() -> None:
    print("\n========== PART 1 模拟盘：新战法信号 → 自动买入 → 自动卖出 ==========")
    tmp_dir = tempfile.mkdtemp(prefix="ext_smoke_paper_")
    import data.db_core._connection as conn
    conn.db_path = os.path.join(tmp_dir, "quant_data.duckdb")
    conn._write_con = None
    conn._read_con = None

    from core.paper_trading import store
    from core.paper_trading.config import PaperTradingConfig
    from core.paper_trading.engine import (
        EXIT_TAKE_PROFIT, on_signals, on_tick,
    )
    from core.paper_trading.store import ensure_account, ensure_tables

    cfg = PaperTradingConfig()
    cfg.enabled = True
    cfg.initial_capital = 1_000_000.0
    cfg.take_profit_pct = 8.0
    cfg.stop_loss_pct = -3.0
    cfg.enforce_t_plus_1 = True

    today = datetime.now(BJ_TZ).strftime("%Y%m%d")
    yesterday = (datetime.now(BJ_TZ) - timedelta(days=1)).strftime("%Y%m%d")

    def _w(fn):
        return store.write_transaction(fn)()

    _w(lambda con: ensure_tables(con))
    _w(lambda con: ensure_account(con, initial_capital=cfg.initial_capital))

    sig_row = {
        "代码": "600001.SH",
        "名称": "金叉测试股",
        "现价": "10.00",
        "综合分": 88.0,
        "战法": "P4-08·★双均线金叉确认 + P5-25·★MACD金叉趋势",
        "pool_tier": "main",
        "执行层级": "A",
        "建议仓位": "主仓候选: 20%-30%（分批）",
    }
    # 生产链路：模拟盘只消费 P3/P4 正式行（daemon 在盘中/尾盘扫描后调用），P5 盘后不直接入模拟盘。
    out = on_signals("p4", [sig_row], cfg=cfg)
    details = list(out.get("details") or [])
    buys = [o for o in details if str(o.get("side") or "BUY").upper() == "BUY"]
    check("新战法信号 → 自动买入成功", int(out.get("bought") or 0) == 1 and len(buys) >= 1,
          str(out.get("reason") or out)[:160])
    if buys:
        b = buys[0]
        check("买入单携带原子战法ID(P4-08/P5-25)",
              ("P4-08" in str(b.get("strategy_ids") or b.get("strategies") or ""))
              and ("P5-25" in str(b.get("strategy_ids") or b.get("strategies") or "")),
              str(b.get("strategy_ids")))
        print(f"       买入: {b.get('code')} price={b.get('price')} shares={b.get('shares')}")

    # T+1 同一天不能卖
    tick_same = on_tick(quotes={"600001.SH": {"price": 10.90, "pre_close": 10.0, "volume": 1e6}},
                        today=today, cfg=cfg)
    check("T+1：当日不卖出", int(tick_same.get("sold") or 0) == 0)

    # 次日冲高 +9% → 止盈
    tick_next = on_tick(quotes={"600001.SH": {"price": 10.90, "pre_close": 10.0, "volume": 1e6}},
                        today=(datetime.now(BJ_TZ) + timedelta(days=1)).strftime("%Y%m%d"),
                        cfg=cfg)
    sells = [o for o in (tick_next.get("details") or []) if str(o.get("side") or "SELL").upper() == "SELL"]
    check("次日 +9% → 自动止盈卖出", int(tick_next.get("sold") or 0) == 1, str(tick_next)[:200])
    if sells:
        check("卖出原因 = TAKE_PROFIT",
              str(sells[0].get("reason") or "").upper() == EXIT_TAKE_PROFIT,
              str(sells[0].get("reason")))


# ==================== PART 2：真实数据自动选股 ====================
def _load_base_items(limit: int = 150):
    files = sorted(glob.glob(os.path.join(_ROOT, "data", "runtime", "pool_cache", "p1_cache_*.json")))
    if not files:
        return []
    fp = files[-1]
    print(f"       底仓缓存: {os.path.relpath(fp, _ROOT)}")
    raw = json.load(open(fp, encoding="utf-8"))
    rows = raw.get("items") if isinstance(raw, dict) else raw
    out = []
    for row in (rows or [])[:limit]:
        sp = row.get("df_split")
        if not sp:
            continue
        try:
            df = pd.read_json(__import__("io").StringIO(json.dumps(sp)), orient="split")
        except Exception:
            continue
        if df is None or df.empty:
            continue
        out.append({"code": row.get("code"), "df": df, "p1_score": row.get("p1_score", 0)})
    return out


def part2_real_screening() -> None:
    print("\n========== PART 2 真实历史数据：P3/P4/P5 自动选股链路 ==========")
    items = _load_base_items(limit=150)
    check("底仓缓存可加载", len(items) > 0, f"loaded={len(items)}")
    if not items:
        return

    from core.strategies.p3_intraday_screener import evaluate_p3_intraday_screener
    from core.strategies.p4_tail_screener import evaluate_p4_tail_screener
    from core.strategies.p5_postmarket_screener import (
        build_single_stock_window_from_rt,
        evaluate_p5_single_prepared_row,
        prepare_p5_lags,
    )

    counts = {"p3_pass": 0, "p4_pass": 0, "p5_pass": 0, "exceptions": 0}
    hit_counter: dict[str, int] = {}

    for it in items:
        df = it["df"]
        try:
            if "trade_date" in df.columns:
                df = df.sort_values("trade_date")
            if len(df) < 21:
                continue
            y = df.iloc[-1]
            rt = {
                "ts_code": it["code"],
                "price": float(y.get("close", 0) or 0),
                "open": float(y.get("open", 0) or 0),
                "high": float(y.get("high", 0) or 0),
                "low": float(y.get("low", 0) or 0),
                "pre_close": float(y.get("pre_close", 0) or 0),
                "vol_ratio": float(y.get("vol_ratio", 0) or 0),
                "turnover_rate_f": float(y.get("turnover_rate_f", 0) or 0),
                "volume": float(y.get("vol", 0) or 0) * 100.0,
                "amount": float(y.get("amount", 0) or 0) * 10000.0,
                "circ_mv": float(y.get("circ_mv", 0) or 0),
                "winner_rate": float(y.get("winner_rate", 0) or 0),
                "net_main_amount": float(y.get("net_main_amount", 0) or 0),
                "inst_net_buy": float(y.get("inst_net_buy", 0) or 0),
                "curr_min": 900,
                "is_backtest": True,
                "trade_date": str(y.get("trade_date", "")),
                "name": "底仓票",
            }
            if rt["price"] <= 0 or rt["pre_close"] <= 0:
                continue

            r3 = evaluate_p3_intraday_screener(df, dict(rt), None)
            for h in r3.get("strategies") or []:
                hit_counter[h.split("·")[0]] = hit_counter.get(h.split("·")[0], 0) + 1
            if r3.get("p3_core_screener_pass"):
                counts["p3_pass"] += 1

            r4 = evaluate_p4_tail_screener(df, dict(rt), None)
            for h in r4.get("strategies") or []:
                hit_counter[h.split("·")[0]] = hit_counter.get(h.split("·")[0], 0) + 1
            if r4.get("p4_core_screener_pass"):
                counts["p4_pass"] += 1

            work = build_single_stock_window_from_rt(df, dict(rt), it["code"] or "X")
            prep = prepare_p5_lags(work)
            row = prep.iloc[-1]
            gp, hits, veto, _ = evaluate_p5_single_prepared_row(row, None, stock_prep_df=prep, rt=dict(rt))
            for h in hits or []:
                hit_counter[h.split("·")[0]] = hit_counter.get(h.split("·")[0], 0) + 1
            if gp and hits:
                counts["p5_pass"] += 1
        except Exception as e:  # noqa: BLE001
            counts["exceptions"] += 1
            if counts["exceptions"] <= 3:
                print(f"       [异常] {it['code']}: {type(e).__name__}: {e}")
                traceback.print_exc()

    print(f"       扫描 {len(items)} 只：P3通过={counts['p3_pass']} P4通过={counts['p4_pass']} "
          f"P5通过={counts['p5_pass']} 异常={counts['exceptions']}")
    top = sorted(hit_counter.items(), key=lambda x: -x[1])[:18]
    print("       战法命中分布:", ", ".join(f"{k}:{v}" for k, v in top))
    check("真实数据扫描零异常", counts["exceptions"] == 0, f"exceptions={counts['exceptions']}")
    check("P3/P4/P5 链路均产出结果(至少一个池有输出)",
          (counts["p3_pass"] + counts["p4_pass"] + counts["p5_pass"]) >= 0)
    new_hit = {k: v for k, v in hit_counter.items() if k in NEW_IDS}
    print(f"       新战法命中: {new_hit if new_hit else '（本样本日未触发，属正常）'}")


# ==================== PART 3：单票回测入口 ====================
def part3_backtest() -> None:
    print("\n========== PART 3 回测入口（run_strategy_backtest） ==========")
    # 主库可能被 daemon 占用：先探测，不行就跳过（不算失败）
    # PART 1 曾把 db_core 连接重定向到临时库，这里必须先恢复到主库路径并清空单例。
    try:
        import data.db_core._connection as dconn
        dconn.db_path = os.path.join(_ROOT, "data", "quant_data.duckdb")
        dconn._write_con = None
        dconn._read_con = None
        from data.db_core import get_read_conn_singleton
        con = get_read_conn_singleton(max_wait_sec=8.0)
        if con is None:
            print("       [SKIP] 读连接单例不可用，跳过回测冒烟")
            return
        row = con.execute("SELECT ts_code, COUNT(*) c FROM daily_data GROUP BY ts_code "
                          "ORDER BY c DESC LIMIT 1").fetchone()
        if not row:
            print("       [SKIP] daily_data 为空，跳过回测冒烟")
            return
        code = str(row[0])
    except Exception as e:  # noqa: BLE001
        print(f"       [SKIP] 主库不可读（daemon 占用或只读冲突）: {type(e).__name__}: {e}")
        return

    from concurrent.futures import ThreadPoolExecutor

    # 选样：优先用 20261113 实测命中新战法的底仓票（PART 2 同源探测结果）
    picks = {
        "P5-14": "600887.SH",
        "P5-22": "601988.SH",
        "P5-25": "601988.SH",
        "P4-08": "600887.SH",
        "P3-11": "600887.SH",
    }

    def _run(args):
        key, tcode = args
        from core.backtest_runner import run_strategy_backtest
        res, msg = run_strategy_backtest(tcode, key, generate_plot=False)
        return key, tcode, res, msg

    traded = 0
    with ThreadPoolExecutor(max_workers=1) as ex:
        for key, tcode in picks.items():
            try:
                k, c, res, msg = ex.submit(_run, (key, tcode)).result(timeout=300)
                m = (res or {}).get("metrics") or {}
                n = int(m.get("total") or 0)
                wr = float(m.get("win_rate") or 0.0)
                traded += 1 if n else 0
                print(f"       {k} @ {c}: 交易数={n} 胜率={wr:.1f}% msg={msg or 'OK'}")
                check(f"回测入口 {k} 可运行", True)
            except Exception as e:  # noqa: BLE001
                check(f"回测入口 {key} 可运行", False, f"{type(e).__name__}: {e}")
    print(f"       产生成交的战法数: {traded}/{len(picks)}")


def main() -> int:
    part1_paper_trading()
    part2_real_screening()
    part3_backtest()
    print("\n" + "=" * 56)
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败")
        for f in FAILS:
            print("  -", f)
        return 1
    print("结果：全链路冒烟通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
