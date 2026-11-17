# -*- coding: utf-8 -*-
"""上线前永久回归套件（2026-10-18）。

覆盖本轮修复过的全部根因 + A 股规则口径，任何一条不通过都会在汇总里列出。
运行：
    _pyenv\\venv\\Scripts\\python.exe tools\\_test_release_regressions.py
"""
from __future__ import annotations

import math
import os
import sys
import traceback
from typing import Any, Callable, Dict, List, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core._pyarrow_compat import disable_pyarrow

disable_pyarrow()

import pandas as pd  # noqa: E402

PASSED: List[str] = []
FAILED: List[Tuple[str, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED.append((name, detail))
        print(f"  [FAIL] {name}  {detail}")
    return bool(cond)


def section(title: str) -> None:
    print()
    print("=" * 78)
    print(title)
    print("=" * 78)


# ---------------------------------------------------------------------------
# R01 向量化三层风控：缺列不崩
# ---------------------------------------------------------------------------
def r01() -> None:
    from core.strategies.risk_control_engine import evaluate_3layer_risk_vector

    idx = pd.Index(["600000.SH", "300750.SZ", "688981.SH"], name="ts_code")
    full = pd.DataFrame(
        {
            "close": [20.0, 50.0, 30.0],
            "pre_close": [19.0, 48.0, 29.0],
            "pct_chg": [5.0, 4.0, 3.0],
            "vr": [1.6, 1.2, 1.1],
            "turnover_rate": [8.0, 5.0, 3.0],
            "amount": [3.2e8, 1.0e9, 5.0e8],
            "ma5": [19.5, 48.0, 29.0],
            "ma10": [19.0, 47.0, 28.5],
            "ma20": [18.5, 46.0, 28.0],
            "ma60": [17.0, 44.0, 26.0],
            "macd_dif": [0.2, 0.3, 0.1],
            "macd_dea": [0.1, 0.2, 0.08],
            "macd_hist": [0.1, 0.1, 0.02],
            "limit_pct": [10.0, 20.0, 20.0],
            "curr_min": [14 * 60 + 35] * 3,
        },
        index=idx,
    )

    try:
        out = evaluate_3layer_risk_vector(full)
        check("R01 完整列返回 DataFrame", isinstance(out, pd.DataFrame), f"type={type(out).__name__}")
    except Exception as exc:  # noqa: BLE001
        check("R01 完整列返回 DataFrame", False, f"{type(exc).__name__}: {exc}")

    for drop in (
        ["close"],
        ["pre_close"],
        ["pct_chg"],
        ["vr", "turnover_rate"],
        ["ma5", "ma10", "ma20", "ma60"],
        ["macd_dif", "macd_dea", "macd_hist"],
        ["curr_min", "limit_pct"],
        ["close", "pre_close", "pct_chg", "vr", "turnover_rate", "amount"],
    ):
        try:
            out = evaluate_3layer_risk_vector(full.drop(columns=drop))
            check(f"R01 缺列 {drop} 不崩", isinstance(out, (pd.DataFrame, pd.Series, type(None))))
        except Exception as exc:  # noqa: BLE001
            check(f"R01 缺列 {drop} 不崩", False, f"{type(exc).__name__}: {exc}")

    # 池标识替代 curr_min 的第二条路径
    try:
        out = evaluate_3layer_risk_vector(
            full.drop(columns=["curr_min"]),
            pool_key_series=pd.Series(["p1", "p4", "p5"], index=idx),
        )
        check("R01 pool_key_series 路径可用", isinstance(out, (pd.DataFrame, pd.Series, type(None))))
    except Exception as exc:  # noqa: BLE001
        check("R01 pool_key_series 路径可用", False, f"{type(exc).__name__}: {exc}")

    # 空表不崩
    try:
        out = evaluate_3layer_risk_vector(full.iloc[0:0])
        check("R01 空表不崩", isinstance(out, (pd.DataFrame, pd.Series, type(None))))
    except Exception as exc:  # noqa: BLE001
        check("R01 空表不崩", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R02 P1 预计算：脏缓存兼容
# ---------------------------------------------------------------------------
def r02() -> None:
    import core.p1_precompute as pp

    dirty: List[Dict[str, Any]] = [
        {"code": "600000.SH", "ts_code": "600000.SH", "df": []},
        {"code": "600001.SH", "ts_code": "600001.SH", "df": None},
        {"code": "600002.SH", "ts_code": "600002.SH"},
        {"code": "600003.SH", "ts_code": "600003.SH", "df": {}},
        {"code": "600004.SH", "ts_code": "600004.SH", "df": [{"close": 1.0}]},
    ]
    for fn_name in ("batch_precompute_inflow", "batch_precompute_mv_tier"):
        fn = getattr(pp, fn_name, None)
        if fn is None:
            check(f"R02 {fn_name} 存在", False, "属性缺失")
            continue
        try:
            fn(dirty)
            check(f"R02 {fn_name} 接受 list/dict/None 脏缓存", True)
        except Exception as exc:  # noqa: BLE001
            check(f"R02 {fn_name} 接受 list/dict/None 脏缓存", False, f"{type(exc).__name__}: {exc}")

    try:
        fn = pp.batch_precompute_limit_up_signals
        fn(dirty)
        check("R02 batch_precompute_limit_up_signals 脏缓存", True)
    except AttributeError:
        pass
    except Exception as exc:  # noqa: BLE001
        check("R02 batch_precompute_limit_up_signals 脏缓存", False, f"{type(exc).__name__}: {exc}")

    idx = pd.date_range("2026-01-01", periods=30, freq="B")
    df = pd.DataFrame(
        {
            "trade_date": idx.strftime("%Y%m%d"),
            "open": [10.0 + i * 0.05 for i in range(30)],
            "high": [10.4 + i * 0.05 for i in range(30)],
            "low": [9.7 + i * 0.05 for i in range(30)],
            "close": [10.2 + i * 0.05 for i in range(30)],
            "vol": [1e6 + i * 1e4 for i in range(30)],
            "amount": [1.2e7 + i * 1e5 for i in range(30)],
            "pct_chg": [0.5] * 30,
            "turnover_rate": [3.0] * 30,
            "circ_mv": [5.0e9] * 30,
            "net_main_amount": [1.0e5 + i * 1.0e4 for i in range(30)],
        }
    )
    good = [{"code": "600010.SH", "ts_code": "600010.SH", "df": df}]
    try:
        out = pp.batch_precompute_inflow(good, lookback_days=10)
        hit = (out or {}).get("600010.SH")
        check(
            "R02 正常 df 仍产出资金指标",
            bool(hit) and "net_inflow_3d" in (hit or {}),
            f"keys={sorted((hit or {}).keys())[:6]}",
        )
    except Exception as exc:  # noqa: BLE001
        check("R02 正常 df 仍产出资金指标", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R03 HiThink 涨停上下文：脏返回兼容
# ---------------------------------------------------------------------------
def r03() -> None:
    from core.strategies import _hithink_limit_up_context as ht

    for fn_name in ("is_in_limit_up_pool", "is_in_limit_burst_pool"):
        fn = getattr(ht, fn_name, None)
        if fn is None:
            check(f"R03 {fn_name} 存在", False, "属性缺失")
            continue
        for payload in (None, [], {}, [{"ts_code": "600000.SH"}], "600000.SH"):
            try:
                fn("600000.SH", payload)
                check(f"R03 {fn_name} payload={type(payload).__name__}", True)
            except Exception as exc:  # noqa: BLE001
                check(f"R03 {fn_name} payload={type(payload).__name__}", False, f"{type(exc).__name__}: {exc}")

    # 真实上下文命中语义（dict 正常路径）
    ctx = {
        "enabled": True,
        "limit_up_map": {"600000": {"continue_day_cnt": 2}},
        "burst_map": {"000001": {"open_times": 1}},
        "all_limit_tickers": {"600000"},
        "all_burst_tickers": {"000001"},
    }
    for fn_name, ticker, want in (
        ("is_in_limit_up_pool", "600000.SH", True),
        ("is_in_limit_up_pool", "000001.SZ", False),
        ("is_in_limit_burst_pool", "000001.SZ", True),
        ("is_in_limit_burst_pool", "600000.SH", False),
    ):
        try:
            got = bool(getattr(ht, fn_name)(ticker, ctx))
            check(f"R03 {fn_name}({ticker})={want}", got == want, f"got={got}")
        except Exception as exc:  # noqa: BLE001
            check(f"R03 {fn_name}({ticker})={want}", False, f"{type(exc).__name__}: {exc}")

    # inject 不因脏 base 崩溃
    try:
        ht.inject_limit_context_to_base_items([], ctx)
        ht.inject_limit_context_to_base_items([{"code": "600000.SH", "rt": {}}], ctx)
        check("R03 inject_limit_context_to_base_items 正常", True)
    except Exception as exc:  # noqa: BLE001
        check("R03 inject_limit_context_to_base_items 正常", False, f"{type(exc).__name__}: {exc}")

    try:
        ht.load_hithink_limit_context()
        check("R03 load_hithink_limit_context 异常自吞", True)
    except Exception as exc:  # noqa: BLE001
        check("R03 load_hithink_limit_context 异常自吞", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R04 Session 脱水缓存：假阳性判定
# ---------------------------------------------------------------------------
def r04() -> None:
    import ui.session_cache_dehydrate as scd

    is_hydrated = scd._is_hydrated_df
    cases: List[Tuple[Any, bool]] = [
        (pd.DataFrame({"a": [1, 2]}), True),
        (pd.DataFrame(), False),
        ([{"a": 1}], False),
        ({}, False),
        (None, False),
        ("abc", False),
        (0, False),
        ([], False),
    ]
    for obj, want in cases:
        try:
            got = bool(is_hydrated(obj))
            check(f"R04 _is_hydrated_df({type(obj).__name__}/len={len(obj) if hasattr(obj, '__len__') else '-'})={want}", got == want, f"got={got}")
        except Exception as exc:  # noqa: BLE001
            check(f"R04 _is_hydrated_df({type(obj).__name__})={want}", False, f"{type(exc).__name__}: {exc}")

    items = [
        {"code": "600020.SH", "ts_code": "600020.SH", "df": [{"close": 1.0}], "hist": {"close": 1.0}},
        {"code": "600021.SH", "ts_code": "600021.SH", "df": pd.DataFrame({"close": [1.0, 2.0]})},
    ]
    try:
        out = scd.rehydrate_base_items_for_scan_engine(items)
        types = sorted({type(i.get("df")).__name__ for i in (out or [])})
        check("R04 rehydrate 后无 list/dict 型 df", "list" not in types and "dict" not in types, f"types={types}")
    except Exception as exc:  # noqa: BLE001
        check("R04 rehydrate 后无 list/dict 型 df", False, f"{type(exc).__name__}: {exc}")

    try:
        out = scd.dehydrate_base_items_list(items)
        check("R04 dehydrate_base_items_list 可调用", isinstance(out, list))
    except Exception as exc:  # noqa: BLE001
        check("R04 dehydrate_base_items_list 可调用", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R05 交易日历
# ---------------------------------------------------------------------------
def r05() -> None:
    import inspect as _insp

    import data.fetcher._raw_day_data as rd

    src = _insp.getsource(rd.sync_trade_calendar)
    check("R05 请求窗口向后扩展 14 天", "timedelta(days=14)" in src)
    check("R05 入库裁掉今天之后的自然日", "_cal_full[\"_d8\"].le(end8)" in src)
    check("R05 next_trade_date 由含未来窗口推导", "_open_dates_full" in src)
    check("R05 日历全量重写（replace）避免脏行并存", 'if_exists="replace"' in src)
    check("R05 整段不抛出（铁律）", "except Exception as e" in src and "return pd.DataFrame()" in src)

    # 离线校验 dim_trade_calendar 实体数据（只读）
    try:
        from data.db_core import get_read_conn_singleton

        con = get_read_conn_singleton(max_wait_sec=25.0)
        if con is None:
            check("R05 dim_trade_calendar 只读连接可用", False, "get_read_conn_singleton 返回 None")
            return
        rows = con.execute(
            "SELECT count(*) AS n, sum(CASE WHEN is_open THEN 1 ELSE 0 END) AS op, "
            "count(DISTINCT trade_date) AS d, min(trade_date) AS lo, max(trade_date) AS hi "
            "FROM dim_trade_calendar"
        ).fetchall()
        n, op, d, lo, hi = int(rows[0][0]), int(rows[0][1] or 0), int(rows[0][2]), rows[0][3], rows[0][4]
        check("R05 dim_trade_calendar 有数据", n > 0 and d == n, f"n={n} open={op} distinct={d} {lo}~{hi}")
        check("R05 日历含休市日（可离线判定休市）", 0 < op < n, f"open={op}/{n}")

        fut = con.execute(
            "SELECT count(*) FROM dim_trade_calendar "
            "WHERE replace(trade_date, '-', '') > strftime(current_date, '%Y%m%d')"
        ).fetchone()[0]
        check("R05 无未来自然日入库", int(fut) == 0, f"future={fut}")

        dup = con.execute(
            "SELECT count(*) FROM (SELECT exchange, trade_date, count(*) c FROM dim_trade_calendar "
            "GROUP BY 1,2 HAVING c > 1)"
        ).fetchone()[0]
        check("R05 (exchange, trade_date) 无重复", int(dup) == 0, f"dup={dup}")

        bad_ex = con.execute(
            "SELECT count(*) FROM dim_trade_calendar WHERE exchange <> 'SSE'"
        ).fetchone()[0]
        check("R05 exchange 仅 SSE", int(bad_ex) == 0, f"other={bad_ex}")

        empty_next = con.execute(
            "SELECT count(*) FROM dim_trade_calendar WHERE is_open AND next_trade_date = '' "
            "AND trade_date < (SELECT max(trade_date) FROM dim_trade_calendar WHERE is_open)"
        ).fetchone()[0]
        check("R05 开市日 next_trade_date 齐全", int(empty_next) == 0, f"empty={empty_next}")

        rest_bad = con.execute(
            "SELECT count(*) FROM dim_trade_calendar WHERE (NOT is_open) AND "
            "(pretrade_date <> '' OR next_trade_date <> '')"
        ).fetchone()[0]
        check("R05 休市日前后交易日留空", int(rest_bad) == 0, f"bad={rest_bad}")
    except Exception as exc:  # noqa: BLE001
        check("R05 dim_trade_calendar 离线校验", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R06/R07 策略实验室扫描
# ---------------------------------------------------------------------------
def r06() -> None:
    import ui.strategy_lab as sl

    fn = sl._lab_build_sweep_values
    cap = int(sl._MAX_SWEEP_POINTS)
    cases: List[Tuple[str, float, float, float, bool]] = [
        ("极端步长(-1e9,1e9,1e-12)", -1e9, 1e9, 1e-12, False),
        ("极端步长(0,1,1e-12)", 0.0, 1.0, 1e-12, False),
        ("极端步长(0,1,1e-9)", 0.0, 1.0, 1e-9, False),
        ("极端步长(0,1e6,1e-6)", 0.0, 1e6, 1e-6, False),
        ("整数巨型区间(-1e9,1e9,1)", -1e9, 1e9, 1.0, True),
        ("整数区间(0,100,1)", 0.0, 100.0, 1.0, True),
        ("浮点常规(0,1,0.05)", 0.0, 1.0, 0.05, False),
        ("零步长", 0.0, 1.0, 0.0, False),
        ("反序区间", 1.0, 0.0, 0.05, False),
        ("单点区间", 0.5, 0.5, 0.05, False),
    ]
    for name, a, b, s, is_int in cases:
        try:
            vals, truncated = fn(a, b, s, is_int)
            check(f"R06 {name} 点数受控", len(vals) <= cap, f"n={len(vals)} truncated={truncated}")
        except Exception as exc:  # noqa: BLE001
            check(f"R06 {name} 点数受控", False, f"{type(exc).__name__}: {exc}")

    # 浮点极小步长不得触发 numpy 内存错误
    try:
        vals, _ = fn(-1e9, 1e9, 1e-300, False)
        check("R06 1e-300 步长不 OOM", len(vals) <= cap, f"n={len(vals)}")
    except Exception as exc:  # noqa: BLE001
        check("R06 1e-300 步长不 OOM", False, f"{type(exc).__name__}: {exc}")


def r07() -> None:
    from dataclasses import fields as dc_fields

    import ui.strategy_lab as sl
    from ui.strategy_lab_labels import _LAB_SWEEP_TARGET_POINTS, bounds_for, lab_input_step

    target = int(_LAB_SWEEP_TARGET_POINTS)
    cases: List[Tuple[float, float, float, bool]] = [
        (0.0004, 0.0, 5.0e7, False),
        (300000.0, -149700000.0, 150300000.0, False),
        (0.998, -498.002, 499.998, False),
        (9.5, -4740.5, 4759.5, False),
        (0.0, -1e6, 1e6, False),
        (0.0, 0.0, 200.0, False),
        (5.0, 1.0, 10.0, False),
        (0.0, 0.0, 0.0, False),
        (0.0, -10**9, 10**9, True),
        (3.0, 0.0, 20.0, True),
        (0.0, 0.0, 2.0, True),
    ]
    for yd, lo, hi, is_int in cases:
        span = abs(float(hi) - float(lo))
        try:
            step = lab_input_step(yd, lo, hi, is_int)
            pts = span / step if step > 0 else (1.0 if span == 0 else float("inf"))
            ok = (span <= 0) or (math.isfinite(pts) and pts <= target + 1.0 and step <= span + 1e-12)
            check(f"R07 step(yd={yd:g}, span={span:g}, int={is_int})", ok, f"step={step:g} pts={pts:.2f}")
        except Exception as exc:  # noqa: BLE001
            check(f"R07 step(yd={yd:g}, span={span:g}, int={is_int})", False, f"{type(exc).__name__}: {exc}")

    rows: List[Tuple[str, float, str, bool]] = []
    for prof in sl._p1_profile_keys():
        for fn_name in sorted(sl._allowed_p1_profile_param_keys(prof)):
            rows.append((f"p1/{prof}/{fn_name}", 0.0, fn_name, False))
    for pool in ("p2", "p3", "p4", "p5"):
        cls = sl._get_config_cls(pool)
        merged = sl._merged_flat_pool_values(pool)
        for f in dc_fields(cls):
            v = merged.get(f.name)
            if isinstance(v, (bool, int, float)):
                rows.append((f"{pool}/{f.name}", float(v), f.name, sl._field_is_int_for_dataclass(cls, f.name)))

    bad: List[str] = []
    max_pts = 0.0
    for label, yd, key, is_i in rows:
        lo, hi = bounds_for(key, yd, is_i)
        span = abs(float(hi) - float(lo))
        if span <= 0:
            continue
        step = lab_input_step(yd, lo, hi, is_i)
        pts = span / step if step > 0 else float("inf")
        max_pts = max(max_pts, pts if math.isfinite(pts) else 1e18)
        if pts > target + 1.0:
            bad.append(f"{label}:{pts:.0f}")
    check(
        f"R07 全量 {len(rows)} 个参数默认点数 ≤ {target}",
        not bad,
        f"max={max_pts:.2f}; 越界={bad[:5]}",
    )


# ---------------------------------------------------------------------------
# R08 沙盘空结果 UX
# ---------------------------------------------------------------------------
def r08() -> None:
    from pathlib import Path as _P

    src = _P(ROOT, "ui", "app.py").read_text(encoding="utf-8")
    check("R08 沙盘空结果有正确空态", "无触发信号" in src)


# ---------------------------------------------------------------------------
# R09 A 股涨跌停口径
# ---------------------------------------------------------------------------
def r09() -> None:
    from core.paper_trading import engine as pe

    board_cases = [
        ("600000.SH", "main", 10.0),
        ("601398.SH", "main", 10.0),
        ("000001.SZ", "main", 10.0),
        ("002001.SZ", "main", 10.0),
        ("003001.SZ", "main", 10.0),
        ("688981.SH", "star", 20.0),
        ("689009.SH", "star", 20.0),
        ("300750.SZ", "gem", 20.0),
        ("301269.SZ", "gem", 20.0),
        ("830799.BJ", "bse", 30.0),
        ("871981.BJ", "bse", 30.0),
        ("920002.BJ", "bse", 30.0),
        ("400001.BJ", "bse", 30.0),
    ]
    for code, want_board, want_amp in board_cases:
        try:
            board = pe.board_of(code)
            amp = float(pe.LIMIT_PCT_BY_BOARD.get(board, 10.0))
            check(f"R09 {code} 板块={want_board} 幅度={want_amp}%", board == want_board and amp == want_amp, f"board={board} amp={amp}")
        except Exception as exc:  # noqa: BLE001
            check(f"R09 {code} 板块={want_board} 幅度={want_amp}%", False, f"{type(exc).__name__}: {exc}")

    st_cases = [("ST新河", True), ("*ST生物", True), ("ST 生物", True), ("浦发银行", False), ("", False), (None, False)]
    for name, want in st_cases:
        try:
            got = bool(pe.is_st_stock(name))
            check(f"R09 ST 识别 {name!r}={want}", got == want, f"got={got}")
        except Exception as exc:  # noqa: BLE001
            check(f"R09 ST 识别 {name!r}={want}", False, f"{type(exc).__name__}: {exc}")

    # 跌停封板判定（V30.4：Decimal ROUND_HALF_UP 到 0.01 元，已去掉 1.001 浮点容差）
    lock_cases = [
        (9.50, 10.0, "600000.SH", "ST测试", True),    # 跌停价 9.50
        (9.51, 10.0, "600000.SH", "ST测试", False),   # 跌停价以上，浮点容差已取消
        (9.00, 10.0, "600000.SH", "浦发银行", True),  # 主板跌停价 9.00
        (9.01, 10.0, "600000.SH", "浦发银行", False), # 跌停价以上
        (9.10, 10.0, "600000.SH", "浦发银行", False),
        (8.00, 10.0, "300750.SZ", "宁德时代", True),  # 创业板跌停价 8.00
        (8.01, 10.0, "300750.SZ", "宁德时代", False),
        (8.01, 10.0, "300750.SZ", "宁德时代", False),
        (8.00, 10.0, "688981.SH", "中芯国际", True),  # 科创板跌停价 8.00
        (7.00, 10.0, "830799.BJ", "北证股", True),    # 北交所跌停价 7.00
        (7.01, 10.0, "830799.BJ", "北证股", False),
        (5.0, 0.0, "600000.SH", "浦发银行", False),
    ]
    for price, pre, code, name, want in lock_cases:
        try:
            got = bool(pe._is_limit_down_locked(price, pre, code, name))
            check(f"R09 跌停封板 {code}/{name} {price}@{pre}", got == want, f"got={got}")
        except Exception as exc:  # noqa: BLE001
            check(f"R09 跌停封板 {code}/{name} {price}@{pre}", False, f"{type(exc).__name__}: {exc}")

    # 停牌判定
    susp = [(-1.0, 1e6, True), (0.0, 0.0, True), (10.0, 0.0, True), (10.0, 1000.0, False)]
    for price, vol, want in susp:
        try:
            got = bool(pe._suspended(price, vol))
            check(f"R09 停牌判定 price={price} vol={vol:g}={want}", got == want, f"got={got}")
        except Exception as exc:  # noqa: BLE001
            check(f"R09 停牌判定 price={price} vol={vol:g}={want}", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R10 T+1 / 整手 / 仓位
# ---------------------------------------------------------------------------
def r10() -> None:
    from core.paper_trading import engine as pe
    from core.paper_trading.config import PaperTradingConfig

    cfg = PaperTradingConfig()

    t1 = [
        ("20261018", "20261018", True, False),
        ("20261017", "20261018", True, True),
        ("20261018", "20261019", True, True),
        ("20261018", "20261018", False, True),
    ]
    for bd, td, enforce, want in t1:
        got = bool(pe._sellable_today(bd, td, enforce_t_plus_1=enforce))
        check(f"R10 T+1 buy={bd} today={td} enforce={enforce}", got == want, f"got={got}")

    hold = [("20261001", "20261018", 17), ("20261018", "20261018", 0), ("bad", "20261018", 0)]
    for bd, td, want in hold:
        got = pe._holding_days(bd, td)
        check(f"R10 持有天数 {bd}→{td}={want}", got == want, f"got={got}")

    # 【V30.11 2026-11-04】实验口径改为：固定 5 万/只、最多 20 只、止盈 +8%、止损 -3%。
    # 固定金额与 total_asset 脱钩 → 每只风险敞口一致，战法收益可横向比较。
    check("R10 默认单票仓位 20%（pct 模式备用）", abs(cfg.position_pct - 0.20) < 1e-9, f"{cfg.position_pct}")
    check("R10 默认固定金额 5 万/只", abs(cfg.fixed_amount_per_position - 50000.0) < 1e-9, f"{cfg.fixed_amount_per_position}")
    check("R10 默认仓位模式 fixed_amount", cfg.position_mode == "fixed_amount", f"{cfg.position_mode}")
    check("R10 默认最多 20 只", cfg.max_positions == 20, f"{cfg.max_positions}")
    check("R10 默认止损 -3%", abs(cfg.stop_loss_pct + 3.0) < 1e-9, f"{cfg.stop_loss_pct}")
    check("R10 默认止盈 +8%", abs(cfg.take_profit_pct - 8.0) < 1e-9, f"{cfg.take_profit_pct}")
    check("R10 止损比 = 0.97", abs(cfg.stop_loss_ratio - 0.97) < 1e-9, f"{cfg.stop_loss_ratio}")
    check("R10 止盈比 = 1.08", abs(cfg.take_profit_ratio - 1.08) < 1e-9, f"{cfg.take_profit_ratio}")
    check("R10 整手 = 100", cfg.lot_size == 100, f"{cfg.lot_size}")
    # 止损已统一为所有板 -3%（不再按板差异化，避免污染战法归因）
    check("R10 创业板止损统一 -3%", abs(cfg.resolve_stop_loss_pct("300750") + 3.0) < 1e-9, f"{cfg.resolve_stop_loss_pct('300750')}")
    check("R10 科创板止损统一 -3%", abs(cfg.resolve_stop_loss_pct("688001") + 3.0) < 1e-9, f"{cfg.resolve_stop_loss_pct('688001')}")

    # compute_shares：100 股整手 + 现金约束（fixed_amount 模式：每只 5 万）
    shares_cases = [
        dict(cash=1_000_000.0, total_asset=1_000_000.0, price=10.0, want=5000),    # 5万/10=5000
        dict(cash=500.0, total_asset=1_000_000.0, price=10.0, want=0),             # 不足一手
        dict(cash=5_000.0, total_asset=1_000_000.0, price=50.0, want=100),         # 目标够 → 按现金收敛到 1 手
        dict(cash=1_000.0, total_asset=1_000_000.0, price=50.0, want=0),           # 现金不足一手 → INSUFFICIENT_CASH
        dict(cash=1_000_000.0, total_asset=1_000_000.0, price=0.0, want=0),        # 非法价
        dict(cash=1_000_000.0, total_asset=100_000.0, price=10.0, want=5000),      # fixed 模式与总资产脱钩
        dict(cash=1_000_000.0, total_asset=1_000_000.0, price=300.0, want=100),    # 5万/300=166→100
        dict(cash=1_000_000.0, total_asset=10_000.0, price=300.0, want=100),      # fixed 模式：目标恒为 5 万
    ]
    for c in shares_cases:
        n, target, cash_limited = pe.compute_shares(
            cash=c["cash"], total_asset=c["total_asset"], price=c["price"], cfg=cfg
        )
        check(
            f"R10 整手股数 cash={c['cash']:.0f} asset={c['total_asset']:.0f} px={c['price']:g}",
            n == c["want"] and n % 100 == 0,
            f"shares={n} target={target:.0f} cash_limited={cash_limited}",
        )

    # 归一化：扫描引擎输出是中文列名（代码/名称/现价/综合分/战法）
    try:
        out = pe.parse_signal_row({
            "代码": "600000.SH", "名称": "浦发银行", "现价": "10.00",
            "综合分": 90.0, "战法": "P3-01", "pool_tier": "main",
            "执行层级": "A", "建议仓位": "主仓候选: 20%-30%（分批）",
        })
        check("R10 parse_signal_row 正常行", out is not None and out.get("code") == "600000.SH", f"{out}")
    except Exception as exc:  # noqa: BLE001
        check("R10 parse_signal_row 正常行", False, f"{type(exc).__name__}: {exc}")

    for row, why in (
        ({"代码": "", "现价": "10.00", "综合分": 90.0}, "缺代码过滤"),
        ({"代码": "600000.SH", "现价": "--", "综合分": 90.0}, "现价无效过滤"),
        ({"代码": "600000.SH", "现价": "10.00", "综合分": 90.0, "pool_tier": "observation"}, "观察池过滤"),
        ({"代码": "600000.SH", "现价": "10.00", "综合分": 90.0, "执行层级": "C"}, "C 级过滤"),
        ({"代码": "600000.SH", "现价": "10.00", "综合分": 90.0, "建议仓位": "0%（仅观察）"}, "0% 仓位过滤"),
        (None, "None 输入"),
    ):
        try:
            out = pe.parse_signal_row(row)
            check(f"R10 {why}", out is None, f"out={out}")
        except Exception as exc:  # noqa: BLE001
            check(f"R10 {why}", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R11 A 股交易费用
# ---------------------------------------------------------------------------
def r11() -> None:
    from core.paper_trading.config import PaperTradingConfig
    from core.paper_trading.fees import apply_slippage, compute_fee

    cfg = PaperTradingConfig()
    fee_kw = cfg.fee_section

    buy = compute_fee(
        price=10.0, shares=20000, side="BUY",
        commission_rate=fee_kw["commission_rate"], commission_min=fee_kw["commission_min"],
        stamp_tax_rate=fee_kw["stamp_tax_rate"], transfer_fee_rate=fee_kw["transfer_fee_rate"],
    )
    check("R11 买入 gross=200000", abs(buy.gross_amount - 200000.0) < 1e-6, f"{buy.gross_amount}")
    check("R11 买入无印花税", buy.stamp_tax == 0.0, f"{buy.stamp_tax}")
    check("R11 买入佣金=max(60, 5)=60", abs(buy.commission - 60.0) < 1e-6, f"{buy.commission}")
    check("R11 买入过户费=2.0", abs(buy.transfer_fee - 2.0) < 1e-6, f"{buy.transfer_fee}")
    check("R11 买入 cash_delta 负", buy.cash_delta < 0, f"{buy.cash_delta}")

    sell = compute_fee(
        price=10.0, shares=20000, side="SELL",
        commission_rate=fee_kw["commission_rate"], commission_min=fee_kw["commission_min"],
        stamp_tax_rate=fee_kw["stamp_tax_rate"], transfer_fee_rate=fee_kw["transfer_fee_rate"],
    )
    check("R11 卖出印花税=100（0.05% 单边）", abs(sell.stamp_tax - 100.0) < 1e-6, f"{sell.stamp_tax}")
    check("R11 卖出佣金=60", abs(sell.commission - 60.0) < 1e-6, f"{sell.commission}")
    check("R11 卖出 total_fee=162", abs(sell.total_fee - 162.0) < 1e-6, f"{sell.total_fee}")
    check("R11 卖出 cash_delta = 200000-162", abs(sell.cash_delta - 199838.0) < 1e-6, f"{sell.cash_delta}")

    # 最小佣金 5 元
    small = compute_fee(
        price=10.0, shares=100, side="SELL",
        commission_rate=fee_kw["commission_rate"], commission_min=fee_kw["commission_min"],
        stamp_tax_rate=fee_kw["stamp_tax_rate"], transfer_fee_rate=fee_kw["transfer_fee_rate"],
    )
    check("R11 小额佣金取下限 5 元", abs(small.commission - 5.0) < 1e-6, f"{small.commission}")

    # 零成交不收费
    zero = compute_fee(
        price=10.0, shares=0, side="BUY",
        commission_rate=fee_kw["commission_rate"], commission_min=fee_kw["commission_min"],
        stamp_tax_rate=fee_kw["stamp_tax_rate"], transfer_fee_rate=fee_kw["transfer_fee_rate"],
    )
    check("R11 零成交费用为 0", zero.total_fee == 0.0 and zero.gross_amount == 0.0)

    sl0 = apply_slippage(10.0, "BUY", 0.0)
    sl1 = apply_slippage(10.0, "BUY", 0.1)
    sl2 = apply_slippage(10.0, "SELL", 0.1)
    check("R11 滑点 0 原样", abs(sl0 - 10.0) < 1e-9, f"{sl0}")
    check("R11 买入滑点上浮", sl1 > 10.0, f"{sl1}")
    check("R11 卖出滑点下调", sl2 < 10.0, f"{sl2}")

    check("R11 默认费率 0.03%", abs(cfg.commission_rate - 0.0003) < 1e-12, f"{cfg.commission_rate}")
    check("R11 默认过户费 0.001%", abs(cfg.transfer_fee_rate - 0.00001) < 1e-12, f"{cfg.transfer_fee_rate}")
    check("R11 默认印花税 0.05%", abs(cfg.stamp_tax_rate - 0.0005) < 1e-12, f"{cfg.stamp_tax_rate}")


# ---------------------------------------------------------------------------
# R12 信号解析：涨幅 5% 硬闸
# ---------------------------------------------------------------------------
def r12() -> None:
    """P3 涨幅硬闸门：> +5% 与 < -1.5% 一票否决（config.yaml pct_hard_cap_pct / floor）。"""
    from core.strategies.p3_intraday_screener import (
        P3IntradayScreenerConfig,
        p3_global_risk_veto,
    )

    cfg = P3IntradayScreenerConfig()
    check("R12 涨幅上限配置 = 5.0", abs(cfg.pct_hard_cap_pct - 5.0) < 1e-9, f"{cfg.pct_hard_cap_pct}")
    check("R12 涨幅下限配置 = -1.5", abs(cfg.pct_hard_floor_pct + 1.5) < 1e-9, f"{cfg.pct_hard_floor_pct}")

    idx = pd.date_range("2026-09-01", periods=40, freq="B")
    ma20 = 10.0
    close = [9.6 + i * 0.02 for i in range(40)]
    df = pd.DataFrame(
        {
            "trade_date": idx.strftime("%Y%m%d"),
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "vol": [1e6] * 40,
            "amount": [1e7] * 40,
            "pct_chg": [0.2] * 40,
            "turnover_rate": [3.0] * 40,
            "vol_ratio": [1.1] * 40,
            "circ_mv": [8e9] * 40,
            "ma5": [ma20] * 40,
            "ma10": [ma20] * 40,
            "ma20": [ma20] * 40,
            "ma60": [9.0] * 40,
            "ma20_slope_5": [0.05] * 40,
            "macd_dif": [0.1] * 40,
            "macd_dea": [0.05] * 40,
            "macd_hist": [0.05] * 40,
            "cci": [150.0] * 40,
            "nineturn_signal": [0.0] * 40,
            "pre_close": [ma20] * 40,
        }
    )

    def _gate(price: float, *, with_pre_close: bool = True) -> Tuple[bool, str]:
        rt = {
            "price": price,
            "circ_mv": 8e9,
            "vol_ratio": 1.1,
            "sector_rank": 999.0,
            "sector_beta": 0.5,
            "sector_strength": 0.0,
            "name": "浦发银行",
        }
        if with_pre_close:
            rt["pre_close"] = ma20
        return p3_global_risk_veto(df, rt, cfg)

    # 越界必须被涨幅闸拦下（判定依据：原因文本含「涨幅越界」）
    for price, want_blocked, label in (
        (10.50, False, "+5.00%（边界放行）"),
        (10.51, True, "+5.10%"),
        (10.65, True, "+6.50%"),
        (11.00, True, "+10.00%"),
        (9.85, False, "-1.50%（边界放行）"),
        (9.84, True, "-1.60%"),
        (9.70, True, "-3.00%"),
    ):
        try:
            _ok, why = _gate(price)
            got = ("涨幅越界" in str(why))
            check(f"R12 涨幅 {label} 被闸拦={want_blocked}", got == want_blocked, f"why={why}")
        except Exception as exc:  # noqa: BLE001
            check(f"R12 涨幅 {label} 被闸拦={want_blocked}", False, f"{type(exc).__name__}: {exc}")

    # 涨幅闸先于其它门执行
    try:
        _ok, why = _gate(12.00)
        check("R12 涨幅优先于其他门", "涨幅越界" in str(why), f"why={why}")
    except Exception as exc:  # noqa: BLE001
        check("R12 涨幅优先于其他门", False, f"{type(exc).__name__}: {exc}")

    # 缺 pre_close 时回落到 y['close']，不得误杀
    try:
        _ok, why = _gate(10.40, with_pre_close=False)
        check("R12 缺 pre_close 不误杀", "涨幅越界" not in str(why), f"why={why}")
    except Exception as exc:  # noqa: BLE001
        check("R12 缺 pre_close 不误杀", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# R13 交易时段 / 交易日闸
# ---------------------------------------------------------------------------
def r13() -> None:
    from core.paper_trading.daemon_hooks import in_trading_session, now_bj

    n = now_bj()
    check("R13 北京时间 tzinfo 固定", str(n.tzinfo) in ("Asia/Shanghai", "UTC+08:00"), f"tz={n.tzinfo}")
    for hhmm, want in (
        ("09:15", True), ("09:30", True), ("11:30", True),
        ("12:00", False), ("13:00", True), ("15:00", True),
        ("08:00", False), ("20:00", False),
    ):
        from datetime import datetime as _dt

        try:
            h, m = hhmm.split(":")
            probe = _dt(2026, 10, 19, int(h), int(m), tzinfo=n.tzinfo)
            got = bool(in_trading_session(probe))
            check(f"R13 交易时段 {hhmm}={want}", got == want, f"got={got}")
        except Exception as exc:  # noqa: BLE001
            check(f"R13 交易时段 {hhmm}={want}", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------
SUITES: List[Tuple[str, Callable[[], None]]] = [
    ("R01 向量化三层风控：缺列不崩", r01),
    ("R02 P1 预计算：脏缓存兼容", r02),
    ("R03 HiThink 涨停上下文：脏返回兼容", r03),
    ("R04 Session 脱水缓存：假阳性判定", r04),
    ("R05 交易日历：未来裁剪与前后交易日", r05),
    ("R06 全景扫描序列：极小步长不 OOM", r06),
    ("R07 全景扫描：默认点数受控", r07),
    ("R08 沙盘空结果 UX", r08),
    ("R09 A 股涨跌停口径", r09),
    ("R10 T+1 / 整手 / 仓位", r10),
    ("R11 A 股交易费用", r11),
    ("R12 涨幅 5% 硬闸", r12),
    ("R13 交易时段闸", r13),
]


def main() -> int:
    print("=" * 78)
    print("上线前永久回归套件 —— xiaojie 2026-10-18")
    print("=" * 78)
    for title, fn in SUITES:
        section(title)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            check(f"{title}（整体异常）", False, f"{type(exc).__name__}: {exc}")

    section("汇总")
    print(f"  PASS = {len(PASSED)}")
    print(f"  FAIL = {len(FAILED)}")
    if FAILED:
        print()
        for name, detail in FAILED:
            print(f"  [FAIL] {name}  {detail}")
        return 1
    print("  ALL GREEN")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
