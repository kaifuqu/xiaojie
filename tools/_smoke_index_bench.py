# -*- coding: utf-8 -*-
"""
V30 混合基准（指数超额）回归测试

用法：
    python tools/_smoke_index_bench.py

覆盖场景（12 组）：
  T1  幂等建表（连跑 3 次）
  T2  指数覆盖度（4 个指数均有数据）
  T3  upsert 幂等（同一批写两次，行数不变）
  T4  人工验算混合超额（已知样本误差 < 0.01%）
  T5  单指数收益计算正确性
  T6  缺天 → 前向填充最近可用交易日（非 NULL，避免节假日错位）
  T7  无任何可用数据 → 返回 None（NULL 语义，绝不 0.0）
  T8  板块归因映射（含科创板 / 北交所不产出归因）
  T9  NULL 安全（脏字符串 / None / NaN / db_conn=None）
  T10 扩列幂等 + 旧列保留
  T11 旧口径语义未被改动（沪深300 口径仍是 benchmark_pct）
  T12 快照与面板字段容错
  T13 pd.Timestamp 日期归一化（真实回填链路传入的是 Timestamp 而非字符串）
  T14 旧基准 SQL 必须显式限定沪深300 ts_code
  T15 daemon 钩子不得以 db_conn=None 静默跳过
  T16 start_date 过滤必须按 DATE 比较（带横线 ISO 串 vs 8 位串）
  T17 latest_index_quotes 日期不得被 str[:8] 截断成 '2026-10-'
  T18 旧沪深300 基准必须独立加载，不被 V30 面板就绪状态置 None

全部使用**内存 DuckDB**（:memory:），不碰生产库、不写生产表。
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import _pyarrow_compat as _pyarrow_compat  # noqa: E402,F401

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

PASS = 0
FAIL = 0
FAILED = []


def chk(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS", name)
    else:
        FAIL += 1
        FAILED.append(name)
        print("  FAIL", name, extra)


def _mem_con():
    return duckdb.connect(":memory:")


def _mk_series(dates, closes, code):
    return pd.DataFrame({
        "ts_code": [code] * len(dates),
        "trade_date": [d.replace("-", "") for d in dates],
        "close": [float(c) for c in closes],
    })


# 2026-01-05(一) ~ 2026-01-09(五)，刻意留 01-07 缺口（模拟 Tushare 偶发缺天）
DATES = ["2026-01-05", "2026-01-06", "2026-01-08", "2026-01-09"]
SH = [3000.0, 3010.0, 3020.0, 3030.0]
SZ = [10000.0, 9900.0, 10200.0, 10300.0]
CYB = [2000.0, 1980.0, 2010.0, 2040.0]
HS300 = [3500.0, 3510.0, 3520.0, 3530.0]


def main():
    print("=" * 62)
    print("V30 混合基准（指数超额）回归测试")
    print("=" * 62)

    import data.index_daily_store as S
    import data.signal_backtest as B
    import data.signal_logger as L

    # ---------------- T1 幂等建表 ----------------
    print("\n--- T1 幂等建表 ---")
    con = _mem_con()
    for i in range(3):
        chk("T1 第%d次建表成功" % (i + 1), S.ensure_index_table(con))
    cols = [c[1] for c in con.execute("PRAGMA table_info('index_daily')").fetchall()]
    chk("T1 列齐备", set(cols) >= {"ts_code", "trade_date", "close", "pct_chg"}, cols)

    # ---------------- T2/T3 写入 + 覆盖度 + upsert 幂等 ----------------
    print("\n--- T2/T3 写入与幂等 ---")
    frames = []
    for dates, closes, code in (
        (DATES, SH, "000001.SH"), (DATES, SZ, "399001.SZ"),
        (DATES, CYB, "399006.SZ"), (DATES, HS300, "000300.SH"),
    ):
        df = _mk_series(dates, closes, code)
        df["open"] = df["close"]
        df["high"] = df["close"]
        df["low"] = df["close"]
        df["pre_close"] = df["close"].shift(1).fillna(df["close"])
        df["pct_chg"] = 0.5
        df["vol"] = 1.0
        df["amount"] = 1.0
        frames.append(df)

    def _upsert(con, df):
        con.register("_df", df)
        con.execute(
            "DELETE FROM index_daily WHERE ts_code = ? AND trade_date BETWEEN "
            "STRPTIME(?, '%Y%m%d')::DATE AND STRPTIME(?, '%Y%m%d')::DATE",
            [df["ts_code"].iloc[0], df["trade_date"].iloc[0], df["trade_date"].iloc[-1]],
        )
        con.execute(
            "INSERT INTO index_daily (ts_code, trade_date, open, high, low, close, "
            "pre_close, pct_chg, vol, amount) "
            "SELECT ts_code, STRPTIME(trade_date, '%Y%m%d')::DATE, open, high, low, close, "
            "pre_close, pct_chg, vol, amount FROM _df"
        )
        con.unregister("_df")

    for df in frames:
        _upsert(con, df)
    n1 = con.execute("SELECT COUNT(*) FROM index_daily").fetchall()[0][0]
    chk("T2 写入 16 行", n1 == 16, n1)
    chk("T2 4 个指数", con.execute("SELECT COUNT(DISTINCT ts_code) FROM index_daily").fetchall()[0][0] == 4)
    for df in frames:
        _upsert(con, df)
    n2 = con.execute("SELECT COUNT(*) FROM index_daily").fetchall()[0][0]
    chk("T3 重复写入行数不变（幂等）", n1 == n2, "%s -> %s" % (n1, n2))

    # ---------------- T4/T5 收益计算 ----------------
    print("\n--- T4/T5 收益与混合计算 ---")
    panel = S.load_index_panel(con)
    chk("T4 面板 4 个指数", len(panel) == 4, list(panel.keys()))
    r_sh = S.single_index_return_pct(panel, "000001.SH", "20260105", "20260109")
    chk("T5 上证 T+N = +1.00%", r_sh is not None and abs(r_sh - 1.0) < 0.01, r_sh)
    r_sz = S.single_index_return_pct(panel, "399001.SZ", "20260105", "20260109")
    chk("T5 深证 T+N = +3.00%", r_sz is not None and abs(r_sz - 3.0) < 0.01, r_sz)
    r_cyb = S.single_index_return_pct(panel, "399006.SZ", "20260105", "20260109")
    chk("T5 创业板 T+N = +2.00%", r_cyb is not None and abs(r_cyb - 2.0) < 0.01, r_cyb)
    blend = S.blend_return_pct(panel, "20260105", "20260109")
    expect = (1.0 + 3.0 + 2.0) / 3
    chk("T4 混合 = (1+3+2)/3 = 2.00%",
        blend is not None and abs(blend - expect) < 0.01, (blend, expect))
    chk("T4 人工验算超额 = 5 - 2 = 3%", abs((5.0 - blend) - 3.0) < 0.01, 5.0 - blend)

    # ---------------- T6 缺天 → 前向填充（不是 NULL） ----------------
    print("\n--- T6 缺天前向填充 ---")
    ffill = S.single_index_return_pct(panel, "000001.SH", "20260107", "20260109")
    chk("T6 缺天走前向填充(有值)", ffill is not None, ffill)
    if ffill is not None:
        # 01-07 取 01-06 收盘 3010 → 01-09 收盘 3030 = +0.664%
        chk("T6 前向填充值正确(+0.66%)", abs(ffill - (3030.0 - 3010.0) / 3010.0 * 100) < 0.01, ffill)
    chk("T6 沪深300 同样可填充",
        S.single_index_return_pct(panel, "000300.SH", "20260107", "20260109") is not None)

    # ---------------- T7 全缺 → None（NULL 语义） ----------------
    print("\n--- T7 无可用数据必须 None ---")
    chk("T7 不存在的指数 → None",
        S.single_index_return_pct(panel, "999999.SH", "20260105", "20260109") is None)
    chk("T7 空面板 blend → None", S.blend_return_pct({}, "20260105", "20260109") is None)
    chk("T7 db_conn=None → 空 dict", S.load_index_panel(None) == {})
    chk("T7 早于首日 → None",
        S.single_index_return_pct(panel, "000001.SH", "20250101", "20250110") is None)

    # ---------------- T8 板块归因 ----------------
    print("\n--- T8 板块归因映射 ---")
    cases = [
        ("600000.SH", "000001.SH"), ("601398.SH", "000001.SH"),
        ("603288.SH", "000001.SH"), ("605499.SH", "000001.SH"),
        ("688981.SH", "399006.SZ"), ("689009.SH", "399006.SZ"),
        ("000001.SZ", "399001.SZ"), ("002415.SZ", "399001.SZ"),
        ("300750.SZ", "399006.SZ"), ("301029.SZ", "399006.SZ"),
    ]
    for code, want in cases:
        chk("T8 %s → %s" % (code, want), B._board_index_for_code(code) == want,
            B._board_index_for_code(code))
    for code in ("830799.BJ", "430047.BJ", "920002.BJ"):
        chk("T8 北交所 %s → None" % code, B._board_index_for_code(code) is None)
    chk("T8 裸代码 600519 → 上证", B._board_index_for_code("600519") == "000001.SH")
    chk("T8 裸代码 300750 → 创业板", B._board_index_for_code("300750") == "399006.SZ")
    chk("T8 裸代码 830799 → None", B._board_index_for_code("830799") is None)

    base_res = {
        "bench_sh": 1.0, "bench_sz": 3.0, "bench_cyb": 2.0,
        "bench_hs300": 1.0, "bench_blend": 2.0,
        "board_attributed": None, "board_excess": None,
    }
    r = dict(base_res)
    B._attach_board_attribution(r, "600000.SH", 5.0)
    chk("T8 沪市归因=上证指数", r["board_attributed"] == "上证指数", r)
    chk("T8 沪市超额 = 5 - 1 = 4", abs(r["board_excess"] - 4.0) < 0.01, r["board_excess"])
    r2 = dict(base_res)
    B._attach_board_attribution(r2, "300750.SZ", 5.0)
    chk("T8 创业板归因=创业板指", r2["board_attributed"] == "创业板指", r2)
    chk("T8 创业板超额 = 5 - 2 = 3", abs(r2["board_excess"] - 3.0) < 0.01, r2["board_excess"])
    r3 = dict(base_res)
    B._attach_board_attribution(r3, "830799.BJ", 5.0)
    chk("T8 北交所保持 None",
        r3["board_attributed"] is None and r3["board_excess"] is None, r3)

    # ---------------- T9 NULL 安全 ----------------
    print("\n--- T9 NULL 安全 ---")
    chk("T9 ensure_table(None) → False", S.ensure_index_table(None) is False)
    rr = S.sync_index_daily(None)
    chk("T9 sync(None) 不抛", isinstance(rr, dict) and rr.get("ok") is False, rr)
    chk("T9 sync_quietly 不抛", isinstance(S.sync_index_quietly(db_conn=None), dict))
    chk("T9 latest_quotes(None) → []", S.latest_index_quotes(None) == [])
    out = B._calc_index_returns({}, "20260105", "20260109")
    chk("T9 空面板 → 全 None",
        all(out[k] is None for k in ("bench_sh", "bench_sz", "bench_cyb",
                                     "bench_hs300", "bench_blend")), out)
    out2 = B._calc_index_returns(panel, "20260105", "20260109")
    chk("T9 有面板 → 有值", out2["bench_blend"] is not None, out2)
    chk("T9 beat_rate 空表 → None", B._beat_rate(pd.DataFrame()) is None)
    # 归因列 NULL 时不应产出 board_excess
    r4 = {"bench_sh": None, "bench_sz": None, "bench_cyb": None,
          "bench_hs300": None, "bench_blend": None,
          "board_attributed": None, "board_excess": None}
    B._attach_board_attribution(r4, "600000.SH", 5.0)
    chk("T9 指数收益 NULL 时 board_excess=None",
        r4["board_excess"] is None and r4["board_attributed"] == "上证指数", r4)

    # ---------------- T10 扩列幂等 ----------------
    print("\n--- T10 V30 扩列 ---")
    con2 = _mem_con()
    L.ensure_schema(con2)
    L._ensure_v30_index_columns(con2)
    pnl_cols = {str(c[1]).lower() for c in con2.execute("PRAGMA table_info('signal_pnl')").fetchall()}
    eff_cols = {str(c[1]).lower()
                for c in con2.execute("PRAGMA table_info('strategy_effectiveness')").fetchall()}
    for c in ("bench_sh_pct", "bench_sz_pct", "bench_cyb_pct", "bench_blend_pct",
              "excess_sh_pct", "excess_sz_pct", "excess_cyb_pct", "excess_blend_pct",
              "beat_blend", "board_attributed", "board_excess_pct"):
        chk("T10 signal_pnl.%s" % c, c in pnl_cols)
    for c in ("avg_excess_sh", "avg_excess_sz", "avg_excess_cyb", "avg_excess_blend",
              "beat_blend_rate"):
        chk("T10 strategy_effectiveness.%s" % c, c in eff_cols)
    before = len(pnl_cols)
    L._ensure_v30_index_columns(con2)
    L._ensure_v30_index_columns(con2)
    after = len({str(c[1]).lower()
                 for c in con2.execute("PRAGMA table_info('signal_pnl')").fetchall()})
    chk("T10 重复执行列数不变（幂等）", before == after, "%s -> %s" % (before, after))
    for c in ("benchmark_pct", "excess_pct", "beat_benchmark"):
        chk("T10 signal_pnl 旧列保留 %s" % c, c in pnl_cols)
    for c in ("avg_excess_t5", "win_rate", "avg_return"):
        chk("T10 strategy_effectiveness 旧列保留 %s" % c, c in eff_cols)
    chk("T10 db_conn=None 不抛", L._ensure_v30_index_columns(None) is None)

    # ---------------- T11 旧口径不变 ----------------
    print("\n--- T11 旧口径语义未被改动 ---")
    chk("T11 _load_benchmark_series 仍存在", callable(B._load_benchmark_series))
    src = open(os.path.join(ROOT, "data", "signal_backtest.py"), encoding="utf-8").read()
    chk("T11 benchmark_pct 口径仍是沪深300 单指数",
        'benchmark_pct = (' in src and 'excess_pct = return_pct - benchmark_pct' in src)
    chk("T11 未把旧 benchmark_pct 改成混合",
        "benchmark_pct = blend" not in src and "benchmark_pct = return_pct - bench_blend" not in src)
    chk("T11 blend 走新列 excess_blend_pct",
        '"excess_blend_pct": excess_blend' in src)
    chk("T11 旧 function 体未被删除",
        "SELECT trade_date, close FROM index_daily" in src)

    # ---------------- T13 日期归一化（真实回填链路） ----------------
    print("\n--- T13 pd.Timestamp 日期归一化 ---")
    import data.index_daily_store as I
    panel = {"000001.SH": pd.DataFrame({"td": ["20260105", "20260106", "20260107"],
                                        "close": [3000.0, 3030.0, 3060.0]})}
    r_str = I.single_index_return_pct(panel, "000001.SH", "20260105", "20260107")
    r_ts = I.single_index_return_pct(panel, "000001.SH",
                                     pd.Timestamp("2026-01-05"), pd.Timestamp("2026-01-07"))
    chk("T13 字符串入参结果正确(2.0%)", r_str is not None and abs(r_str - 2.0) < 1e-6, r_str)
    chk("T13 Timestamp 入参与字符串一致", r_ts is not None and abs(r_ts - r_str) < 1e-9,
        "%s vs %s" % (r_ts, r_str))
    r_blend_ts = I.blend_return_pct(panel, pd.Timestamp("2026-01-05"),
                                    pd.Timestamp("2026-01-07"))
    chk("T13 blend 接受 Timestamp(单指数→自身)", r_blend_ts is not None
        and abs(r_blend_ts - r_str) < 1e-9, r_blend_ts)
    chk("T13 _norm_td 归一 Timestamp", I._norm_td(pd.Timestamp("2026-01-05")) == "20260105")
    chk("T13 _norm_td 归一 '2026-01-05'", I._norm_td("2026-01-05") == "20260105")
    chk("T13 _norm_td 归一 '20260105'", I._norm_td("20260105") == "20260105")
    chk("T13 _norm_td 脏值→None", I._norm_td("not-a-date") is None and I._norm_td(None) is None)
    chk("T13 Timestamp 早于序列首日→None",
        I.single_index_return_pct(panel, "000001.SH",
                                  pd.Timestamp("2025-12-01"), pd.Timestamp("2026-01-07")) is None)

    # ---------------- T14 旧基准 SQL 明确限定沪深300 ----------------
    print("\n--- T14 旧基准 SQL 明确 000300.SH ---")
    chk("T14 SQL 含 ts_code = '000300.SH'",
        "WHERE ts_code = '000300.SH'" in src)
    con3 = _mem_con()
    I.ensure_index_table(con3)
    # 同一交易日塞 4 个指数，验证只取沪深300
    _mk_series(["20260105", "20260106"], [1000.0, 1010.0], "000001.SH")
    rows = []
    for code, c0, c1 in (("000001.SH", 3000.0, 3300.0), ("399001.SZ", 10000.0, 12000.0),
                         ("399006.SZ", 2000.0, 1800.0), ("000300.SH", 4000.0, 4040.0)):
        rows.append((code, "2026-01-05", c0))
        rows.append((code, "2026-01-06", c1))
    con3.executemany(
        "INSERT INTO index_daily (ts_code, trade_date, close) VALUES (?, ?, ?)", rows)
    bser = B._load_benchmark_series(con3, pd.Timestamp("2026-01-01"))
    chk("T14 旧基准只返回 1 条/日", bser is not None and len(bser) == 2,
        None if bser is None else len(bser))
    if bser is not None and len(bser) == 2:
        chk("T14 取到的收盘价是沪深300", abs(float(bser['close'].iloc[0]) - 4000.0) < 1e-6,
            bser['close'].tolist())
    con3.close()

    # ---------------- T15 daemon 钩子不再静默跳过 ----------------
    print("\n--- T15 daemon 钩子不再静默跳过 ---")
    dsrc = open(os.path.join(ROOT, "auto_sniper_daemon.py"), encoding="utf-8").read()
    chk("T15 钩子调用 sync_index_quietly", "sync_index_quietly(lookback_days=130)" in dsrc)
    _istore_src = open(os.path.join(ROOT, "data", "index_daily_store.py"), encoding="utf-8").read()
    _import_lines = [ln.strip() for ln in dsrc.splitlines() if "index_daily_store" in ln]
    chk("T15 daemon 只导入 sync_index_quietly",
        len(_import_lines) == 1 and "sync_index_quietly" in _import_lines[0], _import_lines)
    _code_only = "\n".join(
        ln for ln in dsrc.splitlines() if not ln.strip().startswith("#"))
    chk("T15 有效代码不再出现 sync_index_daily(db_conn=None",
        "sync_index_daily(db_conn=None" not in _code_only)
    chk("T15 sync_index_quietly 用 with 包裹 contextmanager",
        "with get_write_conn() as conn" in _istore_src)
    chk("T15 sync_index_quietly 优先复用全局写单例 get_conn",
        "conn = get_conn()" in _istore_src)
    chk("T15 sync_index_quietly 内可自行取写连接",
        "get_write_conn" in open(os.path.join(ROOT, "data", "index_daily_store.py"),
                                 encoding="utf-8").read())

    # ---------------- T16 start_date 过滤 ----------------
    print("\n--- T16 start_date 过滤按 DATE 比较 ---")
    con4 = _mem_con()
    I.ensure_index_table(con4)
    con4.executemany(
        "INSERT INTO index_daily (ts_code, trade_date, close) VALUES (?, ?, ?)",
        [("000001.SH", "2026-06-01", 3000.0), ("000001.SH", "2026-06-10", 3030.0),
         ("000001.SH", "2026-10-13", 3060.0)])
    p_all = I.load_index_panel(con4)
    p_flt = I.load_index_panel(con4, start_date="20260601")
    p_flt2 = I.load_index_panel(con4, start_date="2026-06-01")
    chk("T16 无过滤返回 3 行", len(p_all.get("000001.SH", [])) == 3,
        len(p_all.get("000001.SH", [])))
    chk("T16 8位入参过滤正确", len(p_flt.get("000001.SH", [])) == 3, len(p_flt.get("000001.SH", [])))
    chk("T16 ISO入参过滤正确", len(p_flt2.get("000001.SH", [])) == 3, len(p_flt2.get("000001.SH", [])))
    p_flt3 = I.load_index_panel(con4, start_date="20260610")
    chk("T16 过滤起点生效(2行)", len(p_flt3.get("000001.SH", [])) == 2,
        len(p_flt3.get("000001.SH", [])))
    chk("T16 日期列仍为 8 位串",
        list(p_flt2.get("000001.SH", {}).get("td", [])) == ["20260601", "20260610", "20261013"],
        list(p_flt2.get("000001.SH", {}).get("td", [])))
    q = I.latest_index_quotes(con4)
    chk("T17 行情日期 8 位（不被截断）",
        bool(q) and q[0]["trade_date"] == "20261013", q)
    con4.close()

    # ---------------- T18 旧基准独立加载 ----------------
    print("\n--- T18 旧沪深300 基准独立加载 ---")
    _bt_code = "\n".join(
        ln for ln in src.splitlines()
        if "if not index_panel" in ln and not ln.strip().startswith("#"))
    chk("T18 有效代码不含 if not index_panel 的耦合写法", not _bt_code, _bt_code)
    chk("T18 benchmark_df 独立 try 加载", "benchmark_df = _load_benchmark_series(db_conn, start_ts)" in src)

    # ---------------- T12 快照与面板字段容错 ----------------
    print("\n--- T12 快照与面板字段容错 ---")
    import json
    import tempfile

    import core.strategy_effectiveness_dashboard as D

    d = tempfile.mkdtemp()
    p = os.path.join(d, "snap.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({
            "schema_version": "1.0",
            "generated_at": "2026-10-13T15:00:00+08:00",
            "stats": {"A": {"n_evaluated": 5, "win_rate": 0.5,
                            "avg_excess_blend": 1.2, "avg_excess_sh": "null"}},
            "index_quotes": [
                {"ts_code": "000001.SH", "name": "上证指数", "trade_date": "2026-10-13",
                 "close": 3287.5, "pct_chg": -0.82, "chg_5d": "NaN", "chg_20d": None},
                "bad-row", {"ts_code": "399001.SZ"},
            ],
            "index_coverage": "not-a-dict",
        }, f, ensure_ascii=False)
    snap = D.load_snapshot(p)
    chk("T12 加载成功", snap is not None)
    chk("T12 脏 index_quotes 被剔除", len(snap["index_quotes"]) == 2, snap["index_quotes"])
    chk("T12 'NaN' → None", snap["index_quotes"][0]["chg_5d"] is None)
    chk("T12 index_coverage 非 dict → {}", snap["index_coverage"] == {})
    row = snap["stats"][0]
    chk("T12 avg_excess_blend 读取成功", row["avg_excess_blend"] == 1.2, row)
    chk("T12 avg_excess_sh='null' → None", row["avg_excess_sh"] is None, row)
    p2 = os.path.join(d, "old.json")
    with open(p2, "w", encoding="utf-8") as f:
        json.dump({"stats": {"A": {"n_evaluated": 1, "win_rate": 0.5}}}, f)
    s2 = D.load_snapshot(p2)
    chk("T12 老快照无 index_quotes → []", s2["index_quotes"] == [])
    chk("T12 老快照 blend_ready=False", s2["blend_ready"] is False)
    chk("T12 老快照 avg_excess_blend=None", s2["stats"][0]["avg_excess_blend"] is None)

    con.close()
    con2.close()

    print("\n" + "=" * 62)
    print("RESULT pass=%d fail=%d" % (PASS, FAIL))
    if FAILED:
        print("FAILED:", FAILED)
    print("=" * 62)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
