# -*- coding: utf-8 -*-
"""tools/test_execution_realism.py — 真实成交口径 + 净口径 + 快照链路回归（Tier 2 · 2.1~2.7）

校验 8 组契约（全部只读、不联网、不写生产库）：
  ① core.execution_model 费率/涨跌停/T+1 规则
  ② backtest_runner.real_exec_returns 与逐笔手工计算完全一致
  ③ run_strategy_backtest 指标契约（代码级：合成行情 + 桩引擎，不依赖真实触发）
     ③b 可选：真实行情样本（--real 才跑，数据不足时 SKIP）
  ④ 风险指标向量（Sharpe/Sortino/Calmar/VaR）
  ⑤ Tier2.5 净口径落库契约（内存库：基准缺失→净超额 NULL，有基准→净超额=净收益-基准）
  ⑥ Tier2.5 净聚合与推荐表（strategy_effectiveness 净列 + optimize net_weight_recommendation）
  ⑦ Tier2.7 UI 快照契约（15 字段 / 与 one-shot 快照路径分离）
  ⑧ 回归守护：失败聚合到 FAIL 列表，任何环境性跳过都单独打 [SKIP]，不掩盖代码断言

用法：
    python tools/test_execution_realism.py            # 快跑（默认，≈10s）
    python tools/test_execution_realism.py --real     # 追加真实行情样本验证（较慢）
"""
from __future__ import annotations

import json
import math
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAIL = []
SKIPS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def skip(name: str, detail: str = "") -> None:
    print(f"  [SKIP] {name}" + (f"  {detail}" if detail else ""))
    SKIPS.append(name)


# ---------------------------------------------------------------- ① 成交规则
def part1_rules() -> None:
    print("=" * 78)
    print("① execution_model 成交规则")
    print("=" * 78)
    from core import execution_model as em

    check("佣金万3最低5元", abs(em.calc_cost(1000, "buy").commission - 5.0) < 1e-6)
    check("大额佣金按万3", abs(em.calc_cost(100000, "buy").commission - 30.0) < 1e-6)
    check("印花税仅卖出", em.calc_cost(100000, "buy").stamp_tax == 0.0
          and abs(em.calc_cost(100000, "sell").stamp_tax - 50.0) < 1e-6)
    check("过户费十万分之一", abs(em.calc_cost(100000, "sell").transfer_fee - 1.0) < 1e-6)
    check("主板涨跌停 10%", em.limit_prices(10.0, "600000.SH") == (11.0, 9.0))
    check("创业板 20%", em.limit_prices(10.0, "300001.SZ") == (12.0, 8.0))
    check("科创板 20%", em.limit_prices(10.0, "688001.SH") == (12.0, 8.0))
    check("ST 5%", em.limit_prices(10.0, "600000.SH", True) == (10.5, 9.5))
    check("一字涨停买不进", em.fill_price("buy", 11.0, 11.0, 11.0, 10.0, "600000.SH")[0] is None)
    check("一字跌停卖不出", em.fill_price("sell", 9.0, 9.0, 9.0, 10.0, "600000.SH")[0] is None)
    check("T+1 当日不可卖", em.is_sellable("20261021", "20261021") is False)
    check("T+1 次日可卖", em.is_sellable("20261021", "20261022") is True)
    check("整手 100 股", em.round_lot(357) == 300)
    px, _ = em.fill_price("buy", 10.0, 10.2, 9.8, 10.0, "600000.SH", slippage_bps=1000)
    check("滑点夹逼在 [low, high]", px is not None and px <= 10.2 + 1e-9, f"px={px}")
    # 净收益必须严格小于毛收益（费用侵蚀）
    gross = (11.0 - 10.0) / 10.0 * 100
    net = em.net_return_pct(10.0, 11.0, 10000)["net_pct"]
    check("净收益 < 毛收益（费用侵蚀）", 0 < net < gross, f"net={net:.4f} gross={gross:.4f}")


# ------------------------------------------------- ② real_exec_returns 数学一致
def _load_df():
    import tools._qa_env as qa
    qa.boot()
    from data.db_core import get_stock_data_qfq
    for code in ("600519.SH", "000001.SZ", "600036.SH"):
        try:
            df = get_stock_data_qfq(code, limit=400)
            if df is not None and len(df) >= 60:
                return code, df
        except Exception:
            continue
    return None, None


def part2_math() -> None:
    print("\n" + "=" * 78)
    print("② real_exec_returns 与逐笔手工计算一致性")
    print("=" * 78)
    try:
        import numpy as np
        from core.backtest_runner import real_exec_returns
        from core import execution_model as em
    except Exception as e:
        check("import backtest_runner", False, f"{type(e).__name__}: {e}")
        return
    code, df = _load_df()
    if df is None:
        skip("② 真实行情数学一致", "QA 副本无行情（先跑 tools/make_qa_db.py）")
        return
    buy = np.arange(20, len(df) - 6, 7)
    sell = buy + 5
    r = real_exec_returns(code, df, buy, sell)
    check("有成交样本", r["n_realized"] > 0, f"n_realized={r['n_realized']}/{r['n_signal']}")
    if not r["n_realized"]:
        return
    b, s = int(buy[0]), int(sell[0])
    o, h, l, pc = (float(df[c].iloc[b]) for c in ("open", "high", "low", "pre_close"))
    so, sh, sl, spc = (float(df[c].iloc[s]) for c in ("open", "high", "low", "pre_close"))
    shares = em.round_lot(100000.0 / o) or em.LOT_SIZE
    bf = em.buy_fill(o, h, l, pc, code, shares)
    sf = em.sell_fill(so, sh, sl, spc, code, bf["shares"],
                      str(df["trade_date"].iloc[b]), str(df["trade_date"].iloc[s]))
    manual = em.net_return_pct(bf["price"], sf["price"], bf["shares"])["net_pct"]
    check("首笔与手工一致", abs(manual - float(r["net_returns"][0])) < 1e-9,
          f"batch={float(r['net_returns'][0]):.4f} manual={manual:.4f}")
    check("平均往返成本在合理区间", 0.05 <= r["cost_pct_avg"] <= 0.30,
          f"cost_pct_avg={r['cost_pct_avg']:.4f}%")


# --------------------------------------- ③ 回测指标契约（代码级，不依赖真实触发）
def _synthetic_df(n: int = 150, seed: int = 7):
    """造一段合成日线（字段与生产 qfq DataFrame 对齐）。"""
    import numpy as np
    import pandas as pd

    dates = pd.bdate_range("2026-01-01", periods=n)
    rng = np.random.default_rng(seed)
    close = np.maximum(10 + np.cumsum(rng.normal(0, 0.08, n)), 3.0)
    open_ = close * (1 + rng.normal(0, 0.004, n))
    return pd.DataFrame({
        "trade_date": [d.strftime("%Y%m%d") for d in dates],
        "open": open_,
        "high": np.maximum(open_, close) * 1.01,
        "low": np.minimum(open_, close) * 0.99,
        "close": close,
        "pre_close": np.concatenate([[close[0]], close[:-1]]),
        "vol": np.full(n, 5e6),
        "amount": close * 5e6,
        "total_mv": np.full(n, 3_000_000.0),
        "circ_mv": np.full(n, 2_000_000.0),
        "turnover_rate_f": np.full(n, 3.0),
    })


class _StubEngine:
    """桩引擎：对任意一行都返回命中，从而稳定触发回测指标计算分支。"""

    def __init__(self, key: str):
        self.key = key

    def run_all(self, sub_df, rt):
        return {"strategies": [self.key]}


def _run_contract(strategy_key: str, engine_attr: str):
    import core.backtest_runner as br

    df = _synthetic_df()
    saved = (br.get_stock_data_qfq, getattr(br, engine_attr), br.golden_engine)
    try:
        br.get_stock_data_qfq = lambda code, limit=400: df.copy()
        setattr(br, engine_attr, _StubEngine(strategy_key))
        br.golden_engine = None
        return br.run_strategy_backtest("600000.SH", strategy_key, generate_plot=False)
    finally:
        br.get_stock_data_qfq = saved[0]
        setattr(br, engine_attr, saved[1])
        br.golden_engine = saved[2]


def part3_metrics_contract() -> None:
    print("\n" + "=" * 78)
    print("③ run_strategy_backtest 指标契约（合成行情 + 桩引擎，不依赖真实触发）")
    print("=" * 78)
    try:
        from core.backtest_runner import _LOOKAHEAD_LEGACY_KEYS
    except Exception as e:
        check("import backtest_runner", False, f"{type(e).__name__}: {e}")
        return

    check("旧口径前视名单固定为 3 条",
          set(_LOOKAHEAD_LEGACY_KEYS) == {"01_一进二", "02_黄金缺口", "29_量比异动"},
          str(_LOOKAHEAD_LEGACY_KEYS))

    out, msg = _run_contract("01_一进二", "p2_engine")
    if not out:
        check("合成行情可跑通 01_一进二", False, f"msg={msg}")
        return
    m = out["metrics"]
    check("旧字段 win_rate/pnl_ratio/mdd/total 保留",
          all(k in m for k in ("win_rate", "pnl_ratio", "mdd", "total")),
          f"total={m.get('total')}")
    check("主成交口径 buy_delay == 1", m.get("buy_delay") == 1, f"{m.get('buy_delay')}")
    check("主口径无前视偏差 lookahead_risk is False", m.get("lookahead_risk") is False)
    check("旧战法标记 legacy_lookahead_risk is True",
          m.get("legacy_lookahead_risk") is True)
    for k in ("real_exec", "risk_legacy", "hold_days", "real_exec_delay1"):
        check(f"新字段 {k} 存在", k in m)
    re_ = m.get("real_exec") or {}
    for k in ("n_signal", "n_realized", "fill_rate_pct", "win_rate_net",
              "avg_net_ret", "cost_pct_avg", "n_buy_blocked", "n_sell_blocked", "reason_top"):
        check(f"real_exec.{k} 存在", k in re_, f"{re_.get(k)}")
    check("real_exec 有成交样本", int(re_.get("n_realized") or 0) > 0,
          f"{re_.get('n_realized')}/{re_.get('n_signal')}")
    check("real_exec 往返成本在 0.05%~0.30%",
          0.05 <= float(re_.get("cost_pct_avg") or 0) <= 0.30,
          f"{re_.get('cost_pct_avg')}")
    compat = m.get("real_exec_delay1") or {}
    check("real_exec_delay1 兼容字段指向主口径",
          compat.get("n_realized") == re_.get("n_realized")
          and "note" in compat)
    leg = m.get("legacy_delay0") or {}
    check("旧口径对照块 legacy_delay0 有 note 且不改主指标",
          bool(leg.get("note")) and int(leg.get("n_trades") or 0) > 0,
          f"n_trades={leg.get('n_trades')}")

    # 非旧战法：不得出现 legacy_delay0，且 legacy_lookahead_risk 必须为 False
    out2, msg2 = _run_contract("P3-07_合成样本", "p3_engine")
    if not out2:
        check("合成行情可跑通非旧战法", False, f"msg={msg2}")
    else:
        m2 = out2["metrics"]
        check("非旧战法 legacy_lookahead_risk is False",
              m2.get("legacy_lookahead_risk") is False)
        check("非旧战法不产 legacy_delay0", "legacy_delay0" not in m2)
        check("非旧战法主口径同样 buy_delay == 1", m2.get("buy_delay") == 1)


def part3b_real_sample() -> None:
    print("\n" + "=" * 78)
    print("③b 真实行情样本（--real 才跑；数据覆盖不足则 SKIP）")
    print("=" * 78)
    try:
        import tools._qa_env as qa
        qa.boot()
        from core.backtest_runner import run_strategy_backtest
    except Exception as e:
        check("import", False, f"{e}")
        return
    code, df = _load_df()
    if df is None:
        skip("③b 真实样本", "QA 副本无行情")
        return
    out, msg = run_strategy_backtest(code, "01_一进二", generate_plot=False)
    if not out:
        skip("③b 真实样本", f"{code} 未触发：{msg}（QA 日线仅覆盖约 10 个可回测窗口）")
        return
    m = out["metrics"]
    check("真实样本 buy_delay == 1", m.get("buy_delay") == 1)
    check("真实样本 real_exec 字段齐全",
          all(k in (m.get("real_exec") or {}) for k in ("n_realized", "win_rate_net")),
          f"n_realized={(m.get('real_exec') or {}).get('n_realized')}")


# ------------------------------------------------------------- ④ 风险指标向量
def part4_risk() -> None:
    print("\n" + "=" * 78)
    print("④ 风险指标向量")
    print("=" * 78)
    import numpy as np
    from core.backtest_runner import calc_risk_metrics

    arr = np.array([1.2, -0.8, 2.4, -1.5, 0.4, -2.2, 3.1, -0.3, 1.1, -0.9])
    r = calc_risk_metrics(arr, hold_days=5)
    print("   ", json.dumps(r, ensure_ascii=False))
    for k in ("sharpe_per_trade", "sharpe_annual", "sortino_annual", "calmar", "var95", "n"):
        check(f"{k} 存在且非 nan", k in r and not (isinstance(r[k], float) and math.isnan(r[k])))
    check("var95 为负", r["var95"] < 0, f"{r['var95']}")
    check("空序列不崩", calc_risk_metrics(np.array([]))["n"] == 0)


# ------------------------------------------------ ⑤ 净口径落库（内存库，隔离验证）
def _mk_mem_db(with_index: bool):
    """内存 DuckDB + 生产同构表结构 + 最小样本（绝不触碰任何落盘库）。"""
    import duckdb
    import pandas as pd

    from data.index_daily_store import DDL_INDEX_DAILY
    from data.signal_logger import ensure_schema

    con = duckdb.connect(":memory:")
    ensure_schema(con)
    con.execute(
        "INSERT INTO signal_log (id, log_date, trade_date, ts_code, pool, push_price, "
        "strategy_ids, strategies) VALUES "
        "(1, DATE '2026-10-05', '2026-10-05', '600000.SH', 'P3', 10.0, '[\"P3-01\"]', 'P3-01:测试战法')"
    )
    con.execute(
        "INSERT INTO signal_log (id, log_date, trade_date, ts_code, pool, push_price, "
        "strategy_ids, strategies) VALUES "
        "(2, DATE '2026-10-06', '2026-10-06', '600000.SH', 'P3', 10.2, '[\"P3-01\"]', 'P3-01:测试战法')"
    )
    dates = pd.bdate_range("2026-10-05", periods=16)
    con.execute(
        "CREATE TABLE IF NOT EXISTS daily_data (ts_code VARCHAR, trade_date VARCHAR, "
        "open DOUBLE, high DOUBLE, low DOUBLE, close DOUBLE)"
    )
    con.executemany(
        "INSERT INTO daily_data VALUES (?,?,?,?,?,?)",
        [("600000.SH", d.strftime("%Y-%m-%d"), 10.0 + 0.1 * i, 10.2 + 0.1 * i,
          9.9 + 0.1 * i, 10.05 + 0.1 * i) for i, d in enumerate(dates)],
    )
    if with_index:
        con.execute(DDL_INDEX_DAILY)
        con.executemany(
            "INSERT INTO index_daily VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(code, d.date(), base * (1 + 0.0008 * i), 0.0, 0.0,
              base * (1 + 0.0008 * i), base, 0.0, 0.0, 1e9)
             for code, base in (("000001.SH", 3200.0), ("399001.SZ", 10500.0),
                                ("399006.SZ", 2200.0), ("000300.SH", 3800.0))
             for i, d in enumerate(dates)],
        )
    return con


def part5_net_metrics() -> None:
    print("\n" + "=" * 78)
    print("⑤ Tier2.5 净口径落库契约（内存库，不写生产库）")
    print("=" * 78)
    try:
        from data.signal_backtest import backfill_pnl
    except Exception as e:
        check("import signal_backtest", False, f"{type(e).__name__}: {e}")
        return

    # --- 5.1 无基准：净收益有值、净超额必须 NULL（绝不写 0 冒充超额）
    con = _mk_mem_db(with_index=False)
    try:
        n = backfill_pnl(con)
        check("无基准时仍能回填 T+N", n > 0, f"rows={n}")
        cols = {c[1].lower() for c in con.execute("PRAGMA table_info('signal_pnl')").fetchall()}
        need = {"net_return_pct", "net_win", "net_excess_pct", "net_excess_sh_pct",
                "net_excess_sz_pct", "net_excess_cyb_pct", "net_excess_blend_pct"}
        check("signal_pnl 7 个净口径列齐全", need.issubset(cols), str(sorted(need - cols)))
        row = con.execute(
            "SELECT COUNT(*) AS n, "
            "       SUM(CASE WHEN net_return_pct IS NOT NULL THEN 1 ELSE 0 END) AS n_net, "
            "       SUM(CASE WHEN benchmark_pct IS NULL AND net_excess_pct IS NOT NULL "
            "                THEN 1 ELSE 0 END) AS n_bad "
            "FROM signal_pnl"
        ).fetchone()
        check("净收益全部落库", row[1] == row[0], f"{row[1]}/{row[0]}")
        check("基准缺失时净超额为 NULL（不写 0）", row[2] == 0, f"违规={row[2]}")
        cmp = con.execute(
            "SELECT return_pct, net_return_pct FROM signal_pnl "
            "WHERE return_pct > 1 AND net_return_pct IS NOT NULL LIMIT 1"
        ).fetchone()
        check("净收益严格小于毛收益（费用侵蚀）",
              cmp is not None and cmp[1] < cmp[0], f"{cmp}")
    finally:
        con.close()

    # --- 5.2 有基准：净超额 = 净收益 - 基准
    con = _mk_mem_db(with_index=True)
    try:
        backfill_pnl(con)
        r = con.execute(
            "SELECT net_return_pct, net_excess_pct, net_excess_sh_pct, "
            "       net_excess_blend_pct, benchmark_pct "
            "FROM signal_pnl WHERE net_excess_pct IS NOT NULL LIMIT 1"
        ).fetchone()
        check("有基准时净超额非空", r is not None, str(r))
        if r:
            check("净超额 = 净收益 - 基准",
                  abs(r[0] - r[4] - r[1]) < 1e-6, f"{r[0]:.4f}-{r[4]:.4f} vs {r[1]:.4f}")
            check("净超额（上证/混合）同样落库",
                  r[2] is not None and r[3] is not None, f"sh={r[2]} blend={r[3]}")
    finally:
        con.close()


# ------------------------------------- ⑥ 净聚合 + 优化器净推荐（只建议、不改权重）
def part6_net_aggregate() -> None:
    print("\n" + "=" * 78)
    print("⑥ Tier2.5 净聚合（strategy_effectiveness）与优化器净推荐")
    print("=" * 78)
    import core.strategy_weight_optimizer as swo
    from data.signal_backtest import backfill_pnl, compute_strategy_effectiveness

    con = _mk_mem_db(with_index=True)
    tmp_rec = os.path.join(tempfile.mkdtemp(), "rec_probe.json")
    saved_file = swo.RECOMMENDATION_FILE
    try:
        backfill_pnl(con)
        n = compute_strategy_effectiveness(con, lookback_days=400, min_n_signals=1)
        check("战法有效性聚合可写入", n >= 1, f"rows={n}")
        cols = {c[1].lower() for c in con.execute(
            "PRAGMA table_info('strategy_effectiveness')").fetchall()}
        need = {"avg_net_return", "avg_net_excess", "avg_net_excess_blend", "net_win_rate"}
        check("strategy_effectiveness 4 个净聚合列齐全", need.issubset(cols),
              str(sorted(need - cols)))
        row = con.execute(
            "SELECT avg_net_return, avg_net_excess, avg_net_excess_blend, net_win_rate "
            "FROM strategy_effectiveness LIMIT 1"
        ).fetchone()
        check("净聚合值非空且净胜率在 [0,1]",
              row[0] is not None and row[3] is not None and 0.0 <= float(row[3]) <= 1.0,
              f"avg_net_return={row[0]} net_win_rate={row[3]}")

        swo.RECOMMENDATION_FILE = tmp_rec
        res = swo.optimize_strategy_weights(con, lookback_days=400, auto_apply=False)
        check("优化器 mode 为 statistics_only（不自动改权重）",
              res.get("mode") == "statistics_only", str(res.get("mode")))
        check("auto_apply=False 时不产出实际权重", (res.get("weights") or {}) == {},
              str(list((res.get("weights") or {}).keys())[:5]))
        netrec = res.get("net_weight_recommendation") or {}
        check("net_weight_recommendation 非空", len(netrec) >= 1, f"{len(netrec)} 条")
        if netrec:
            sample = next(iter(netrec.items()))
            check("净推荐条目含净口径字段",
                  isinstance(sample[1], dict)
                  and any(k.startswith("net") or k == "win_rate_net" for k in sample[1]),
                  str(sorted(sample[1].keys())[:8]))
        check("推荐报表已落盘（隔离临时路径）", os.path.exists(tmp_rec))
    finally:
        swo.RECOMMENDATION_FILE = saved_file
        con.close()


# ------------------------------------------------------- ⑦ Tier2.7 UI 快照契约
def part7_ui_snapshot() -> None:
    print("\n" + "=" * 78)
    print("⑦ Tier2.7 UI 快照契约（15 字段 / 路径与 one-shot 分离）")
    print("=" * 78)
    import core.strategy_effectiveness_snapshot as ses

    need = {"schema_version", "generated_at", "stat_date", "mode", "maturity",
            "strategy_maturity", "recommendation", "net_recommendation", "deprecated",
            "stats", "history", "index_quotes", "index_coverage", "source", "notice"}
    check("UI 快照契约字段数 == 15", len(need) == 15, str(sorted(need)))
    check("UI 快照路径与 one-shot 路径分离",
          os.path.basename(ses.UI_SNAPSHOT_JSON) == "strategy_effectiveness_ui_snapshot.json"
          and "one_shot" not in ses.UI_SNAPSHOT_JSON)

    qa_db = os.path.join(ROOT, "data", "_qa", "quant_data.duckdb")
    if not os.path.exists(qa_db):
        skip("⑦ UI 快照实测", "无 QA 副本")
        return
    import duckdb

    tmp = os.path.join(tempfile.mkdtemp(), "ui_snapshot_probe.json")
    saved = ses.UI_SNAPSHOT_JSON
    # 同进程内 QA 库可能已有读写连接；DuckDB 不允许同文件不同配置并行打开，
    # 因此这里直接沿用「读写」配置打开并仅执行 SELECT（不写任何表）。
    try:
        con = duckdb.connect(qa_db, read_only=True)
    except Exception:
        con = duckdb.connect(qa_db, read_only=False)
    try:
        ses.UI_SNAPSHOT_JSON = tmp
        path = ses.build_ui_snapshot(con, lookback_days=30)
        with open(tmp, encoding="utf-8") as f:
            doc = json.load(f)
        check("UI 快照落盘到指定路径", os.path.abspath(path) == os.path.abspath(tmp))
        check("UI 快照 15 个顶层字段齐全", need.issubset(set(doc.keys())),
              str(sorted(need - set(doc.keys()))))
        check("stats 非空（战法统计）", len(doc.get("stats") or {}) >= 1,
              f"{len(doc.get('stats') or {})} 条")
        check("history / index_quotes / index_coverage 结构齐全",
              isinstance(doc.get("history"), list)
              and isinstance(doc.get("index_quotes"), list)
              and isinstance(doc.get("index_coverage"), dict),
              f"history={len(doc.get('history') or [])} idx={len(doc.get('index_quotes') or [])}")
        check("net_recommendation 透传", isinstance(doc.get("net_recommendation"), dict),
              f"{len(doc.get('net_recommendation') or {})} 条")
        check("mode 合法", str(doc.get("mode")) in
              ("statistics_only", "auto_applied"), str(doc.get("mode")))
        check("快照带只读声明 notice", bool(doc.get("notice")))
    finally:
        ses.UI_SNAPSHOT_JSON = saved
        con.close()
        if os.path.exists(tmp):
            os.remove(tmp)


if __name__ == "__main__":
    run_real = "--real" in sys.argv
    part1_rules()
    try:
        part2_math()
        part3_metrics_contract()
        if run_real:
            part3b_real_sample()
    except Exception as e:
        import traceback
        check("part2/3 未抛异常", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
    part4_risk()
    try:
        part5_net_metrics()
        part6_net_aggregate()
        part7_ui_snapshot()
    except Exception as e:
        import traceback
        check("part5/6/7 未抛异常", False, f"{type(e).__name__}: {e}")
        traceback.print_exc()
    print("\n" + "=" * 78)
    if SKIPS:
        print(f"跳过（环境性，不计失败）：{SKIPS}")
    print("总体：", "PASS" if not FAIL else f"FAIL({len(FAIL)}) -> {FAIL}")
    sys.exit(0 if not FAIL else 1)
