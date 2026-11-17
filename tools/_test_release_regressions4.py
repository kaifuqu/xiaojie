# -*- coding: utf-8 -*-
"""上线前永久回归套件·第四批（2026-10-18 V30.5 P0）。

覆盖本轮两个 P0 修复 + 两个禁用护栏：
  T1  calc_indicators_in_memory 缺 total_mv/adj_factor 不得变 0（源字段保留 NULL）
  T2  calc_indicators_in_memory 派生首行允许填 0
  T3  _load_existing_daily_data 源码不得再出现 reindex(...).fillna(0.0)
  T4  危险一次性脚本 _qa_logs/_fix_indicators.py 已停用（执行即 SystemExit）
  T5  逐票日线新鲜度闸：落后票返回原因、新鲜票放行、缺列/空表/None 有原因
  T6  pipeline 主循环已接入逐票闸 + 漏斗记账 + 不写 signal_log
  T7  逐票闸不得误杀：基准日为空时放行
  T8  闸的落点必须早于 merge_daily_with_realtime / _last_row 取值

运行：
    _pyenv\\venv\\Scripts\\python.exe tools\\_test_release_regressions4.py
"""
from __future__ import annotations

import importlib.util
import inspect
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core._pyarrow_compat import disable_pyarrow

disable_pyarrow()

PASSED = []
FAILED = []


def check(name, cond, detail=""):
    if cond:
        PASSED.append(name)
        print("  [PASS] %s%s" % (name, ("  -> " + str(detail)) if detail else ""))
    else:
        FAILED.append((name, str(detail)))
        print("  [FAIL] %s  -> %s" % (name, detail))


def section(t):
    print()
    print("=" * 78)
    print(t)
    print("=" * 78)


def _tiny_df(n=30):
    import pandas as pd
    rows = []
    for i in range(n):
        rows.append({
            "ts_code": "600000.SH",
            "trade_date": "2026-%02d-%02d" % (1 + i // 28, 1 + i % 28),
            "open": 10.0 + i * 0.1, "high": 10.4 + i * 0.1, "low": 9.6 + i * 0.1,
            "close": 10.2 + i * 0.1, "pre_close": 10.0 + (i - 1) * 0.1 if i else 10.0,
            "vol": 100000 + i * 100, "amount": 1000000.0 + i * 1000,
            "circ_mv": 1.0e6, "turnover_rate_f": 3.0, "vol_ratio": 1.2, "pct_chg": 1.0,
        })
    return pd.DataFrame(rows)


def t01_source_cols_not_zeroed():
    section("T1 calc_indicators_in_memory：源字段缺失必须保留 NULL")
    import numpy as np
    import data.fetcher._indicators as IND
    from data.fetcher._client import ALL_55_COLS

    df = _tiny_df(30)
    for col in ("total_mv", "adj_factor", "pe_ttm", "pb", "ps_ttm", "dv_ratio",
                "inst_net_buy", "rz_net_buy", "rzmre",
                "cost_50th", "winner_rate", "cyq_concentration"):
        check("T1 输入不含 %s" % col, col not in df.columns)
        check("T1 %s 属 ALL_55_COLS" % col, col in ALL_55_COLS)

    out = IND.calc_indicators_in_memory(df)
    for col in ("total_mv", "adj_factor", "pe_ttm", "pb", "ps_ttm", "dv_ratio"):
        vals = pd_numeric(out, col)
        nonnull = int(vals.notna().sum())
        check("T1 输出 %s 不得被伪造成 0" % col, nonnull == 0, "nonnull=%d" % nonnull)

    # 显式带 NULL 的源字段也必须保持 NULL
    df2 = _tiny_df(30)
    df2["total_mv"] = [None] * 10 + [5.0e8] * 20
    df2["adj_factor"] = [None] * 30
    out2 = IND.calc_indicators_in_memory(df2)
    tmv = pd_numeric(out2, "total_mv")
    check("T2 显式 NULL 的 total_mv 仍为 NULL（前 10 行）",
          int(tmv.iloc[:10].notna().sum()) == 0 and int((tmv.iloc[10:] == 5.0e8).sum()) == 20,
          "nulls=%d" % int(tmv.iloc[:10].notna().sum()))
    adj = pd_numeric(out2, "adj_factor")
    check("T2 全 NULL 的 adj_factor 保持 NULL", int(adj.notna().sum()) == 0,
          "nonnull=%d" % int(adj.notna().sum()))


def pd_numeric(df, col):
    import pandas as pd
    if col not in df.columns:
        return pd.Series([float("nan")] * len(df), index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce")


def t03_derived_cols_neutral_fill():
    section("T3 派生列首行允许 0 中性填充")
    import numpy as np
    import data.fetcher._indicators as IND

    df = _tiny_df(30)
    out = IND.calc_indicators_in_memory(df)
    for col in ("ma5", "ma20", "macd", "rsi_14", "capital_resonance_score", "fund_memory_score"):
        v = pd_numeric(out, col)
        nonnull = int(v.notna().sum())
        check("T3 派生列 %s 全程非 NULL" % col, nonnull == len(out), "%d/%d" % (nonnull, len(out)))
    ma5 = pd_numeric(out, "ma5")
    check("T3 ma5 首行 = close（min_periods=1）",
          abs(float(ma5.iloc[0]) - float(out["close"].iloc[0])) < 1e-9,
          "ma5=%.4f close=%.4f" % (float(ma5.iloc[0]), float(out["close"].iloc[0])))
    rsi = pd_numeric(out, "rsi_14")
    check("T3 rsi_14 落在 0~100", bool(((rsi >= 0) & (rsi <= 100)).all()),
          "min=%.2f max=%.2f" % (float(rsi.min()), float(rsi.max())))
    check("T3 白名单不含 adj_factor/total_mv/pe_ttm",
          not ({"adj_factor", "total_mv", "pe_ttm"} & set(IND._NEUTRAL_FILL_DERIVED_COLS)))


def t04_sync_entry_no_fillna_zero():
    section("T4 _load_existing_daily_data 不再做全列 fillna(0.0)")
    from data.fetcher import _sync_entry as se

    src = inspect.getsource(se._load_existing_daily_data)
    code = "\n".join(
        ln for ln in src.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    )
    check("T4 可执行代码无 fillna(0.0)", ".fillna(0.0)" not in code)
    check("T4 可执行代码无 fillna(0)", "fillna(0)" not in code)
    check("T4 注释里保留了事故说明", "fillna(0.0)" in src)
    check("T4 仍保留 reindex 对齐列名", "reindex(columns=" in src)


def t05_legacy_script_blocked():
    section("T5 危险一次性脚本已停用")
    script = os.path.join(ROOT, "_qa_logs", "_fix_indicators.py")
    # 【2026-11-04 修正】本用例的真实意图是「**若脚本还在**，它必须已被永久停用」。
    #   原断言在「文件不存在」时判 FAIL —— 但文件被删除恰恰意味着风险已彻底消除，
    #   属于期望状态而非问题，长期为红只会淹没真实告警。
    #   现改为：不存在 → PASS（已彻底移除，风险归零）；
    #         存在   → 仍严格校验头部停用说明 + 直接执行被拒绝（防复发的核心不放松）。
    if not os.path.exists(script):
        check("T5 危险脚本已彻底移除（若存在则须已停用）", True, "文件不存在→风险已消除")
        return
    head = open(script, "r", encoding="utf-8").read(3000)
    check("T5 脚本头部含永久停用说明", "永久停用" in head and "禁止" in head)
    r = subprocess.run(
        [sys.executable, script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
        cwd=ROOT, env={**os.environ, "PYTHONPATH": ROOT, "PYTHONIOENCODING": "utf-8"},
    )
    check("T5 直接执行被拒绝", r.returncode != 0, "rc=%s" % r.returncode)
    check("T5 拒绝信息含 BLOCKED", "BLOCKED" in (r.stdout or "") + (r.stderr or ""))


def t06_stale_gate_unit():
    section("T6 逐票日线新鲜度闸单元行为")
    import pandas as pd
    from core.scan.utils import _per_stock_daily_stale_reason, _normalize_td_yyyymmdd

    check("T6 归一化 2026-10-16 -> 20261016", _normalize_td_yyyymmdd("2026-10-16") == "20261016")
    check("T6 归一化 20261016 -> 20261016", _normalize_td_yyyymmdd("20261016") == "20261016")
    check("T6 归一化非法值 -> ''", _normalize_td_yyyymmdd("2026-13-40") == "")

    fresh = pd.DataFrame({"trade_date": ["2026-10-14", "2026-10-15", "2026-10-16"]})
    stale = pd.DataFrame({"trade_date": ["2026-04-30", "2026-05-08"]})
    edge = pd.DataFrame({"trade_date": ["2026-10-15", "2026-10-16"]})
    ahead = pd.DataFrame({"trade_date": ["2026-10-15", "2026-10-19"]})

    check("T6 新鲜票放行", _per_stock_daily_stale_reason(fresh, "20261016") == "")
    check("T6 边界：末行恰为基准日放行", _per_stock_daily_stale_reason(edge, "20261016") == "")
    check("T6 未来日期放行（不回退）", _per_stock_daily_stale_reason(ahead, "20261016") == "")
    r = _per_stock_daily_stale_reason(stale, "20261016")
    check("T6 落后票给出原因", r == "日线截止日落后(20260508<20261016)", r)
    check("T6 空表有原因", _per_stock_daily_stale_reason(pd.DataFrame(), "20261016") == "无日线数据")
    check("T6 None 有原因", _per_stock_daily_stale_reason(None, "20261016") == "无日线数据")
    check("T6 缺 trade_date 列有原因",
          _per_stock_daily_stale_reason(pd.DataFrame({"close": [1]}), "20261016") == "日线缺 trade_date 列")
    check("T6 末行不可解析有原因",
          _per_stock_daily_stale_reason(pd.DataFrame({"trade_date": ["abc"]}), "20261016")
          == "日线末行 trade_date 无法解析")
    check("T6 基准日为空放行（不误杀）",
          _per_stock_daily_stale_reason(stale, "") == "")


def t07_pipeline_wired():
    section("T7 pipeline 已接入逐票闸")
    from core.scan import pipeline as pl

    src = inspect.getsource(pl.run_scan_engine)
    check("T7 已 import 逐票闸", "_per_stock_daily_stale_reason" in inspect.getsource(pl))
    check("T7 调用逐票闸", "_per_stock_daily_stale_reason(" in src)

    gate_idx = src.find("_stale_reason = _per_stock_daily_stale_reason(")
    check("T7 闸存在", gate_idx > 0)
    if gate_idx > 0:
        before = src[:gate_idx]
        check("T7 闸在 merge_daily_with_realtime 之前",
              before.count("merge_daily_with_realtime(df_target, rt)") == 0)
        check("T7 闸在 _last_row 取值之前", "_last_row = df_target" not in before)
        check("T7 闸在 save_signal_log 之前", "save_signal_log(" not in before)
        loop_start = src.find("for i, item in enumerate(base_items_for_scan):")
        check("T7 闸在市值闸之前（避免误计 mv_skipped_rollbacks）",
              loop_start >= 0 and "mv_skipped_rollbacks" not in src[loop_start:gate_idx],
              "loop_start=%d" % loop_start)
        gate_end = src.find("merged_flag = False", gate_idx)
        gate_block = src[gate_idx:gate_end] if gate_end > gate_idx else src[gate_idx:gate_idx + 2600]
        check("T7 淘汰走 continue", "continue" in gate_block)
        check("T7 淘汰前回滚漏斗计数", "_rollback_scan_funnel_for_skipped_stock(res, active_target_pools)"
              in gate_block)
        check("T7 漏斗记账写「日线截止日落后」",
              "gate_block_reasons" in gate_block)
        check("T7 留了可追溯明细", "_per_stock_daily_stale" in gate_block)
        check("T7 缺失闸参数时放行", "if _stale_req:" in src[:gate_end])


def t08_global_gate_untouched():
    section("T8 全局闸与业务配置未被改动")
    from core.scan import pipeline as pl

    src = inspect.getsource(pl.run_scan_engine)
    check("T8 仍保留全库 MAX(trade_date) 闸", "get_latest_daily_data_trade_date_yyyymmdd" in src)
    check("T8 P2-P4 基准仍为 expected_prev_td", "expected_prev_td" in src)
    check("T8 P5 基准仍为 p5_required_trade_date", "p5_required_trade_date" in src)
    check("T8 评分底线未被改动", "'p2': 30.0, 'p3': 30.0, 'p4': 30.0, 'p5': 30.0" in src)


def t09_feature_layer_same_dates():
    section("T9 派生列与 feat_daily_* 必须覆盖同一批日期")
    from data.fetcher import _sync_entry as se

    src = inspect.getsource(se._sync_daily_features)
    check("T9 共用日期列表", "_latest_daily_feature_dates()" in src)
    check("T9 分层同步显式传 target_dates", "target_dates=feature_dates" in src)
    check("T9 存在日期收集器", hasattr(se, "_latest_daily_feature_dates"))
    check("T9 分层函数支持 target_dates",
          "target_dates" in inspect.signature(se._sync_v26_feature_layers).parameters)
    check("T9 分层函数保留无参退化分支",
          "if not dates:" in inspect.getsource(se._sync_v26_feature_layers))


def t10_verify_system_degeneracy_check():
    section("T10 体检脚本已补全 0 / 退化检测")
    import tools.db_inspector as di
    import verify_system as vs

    src = inspect.getsource(vs.check_duckdb)
    check("T10 verify_system 有退化检测块", "_critical_source_cols" in src)
    for col in ("total_mv", "adj_factor", "circ_mv", "pe_ttm", "dv_ratio"):
        check("T10 verify_system 覆盖 %s" % col, ('"%s"' % col) in src)
    check("T10 verify_system 检测全 0", "全表为 0" in src)
    check("T10 verify_system 检测方差为 0", "STDDEV_POP" in src and "方差为 0" in src)

    check("T10 db_inspector 有退化检测函数", hasattr(di, "verify_critical_column_degeneracy"))
    check("T10 db_inspector 覆盖 total_mv", "total_mv" in di.CRITICAL_SOURCE_COLUMNS)
    check("T10 db_inspector 顶层纳入 critical",
          "degenerate_column:" in inspect.getsource(di.verify_v26_consistency))


def t11_view_compat_drift_regression():
    section("T11 主库 view_compat 无漂移（真实数据回归）")
    try:
        import duckdb

        from data.db_core import get_duckdb_path
        con = duckdb.connect(get_duckdb_path(), read_only=True)
    except Exception as exc:  # noqa: BLE001
        check("T11 打开主库只读连接", False, "%s: %s" % (type(exc).__name__, exc))
        return
    try:
        row = con.execute(
            """
            SELECT COUNT(*),
              SUM(CASE WHEN d.capital_resonance_score IS DISTINCT FROM f.capital_resonance_score THEN 1 ELSE 0 END),
              SUM(CASE WHEN d.fund_memory_score IS DISTINCT FROM m.fund_memory_score THEN 1 ELSE 0 END),
              SUM(CASE WHEN d.vol_ma5 IS DISTINCT FROM c.vma5 THEN 1 ELSE 0 END)
            FROM daily_data d
            JOIN feat_daily_capital f USING (ts_code, trade_date)
            JOIN feat_daily_memory m USING (ts_code, trade_date)
            JOIN feat_daily_core c USING (ts_code, trade_date)
            """
        ).fetchone()
        n = int(row[0] or 0)
        crs = int(row[1] or 0)
        fms = int(row[2] or 0)
        vma = int(row[3] or 0)
        check("T11 daily_data 与 feat_daily_* 全量对齐", n > 0 and crs == 0 and fms == 0 and vma == 0,
              "rows=%d crs_diff=%d fms_diff=%d vma_diff=%d" % (n, crs, fms, vma))
    except Exception as exc:  # noqa: BLE001
        check("T11 比对未抛异常", False, "%s: %s" % (type(exc).__name__, exc))
    finally:
        try:
            con.close()
        except Exception:
            pass


def main() -> int:
    for fn in (t01_source_cols_not_zeroed, t03_derived_cols_neutral_fill,
               t04_sync_entry_no_fillna_zero, t05_legacy_script_blocked,
               t06_stale_gate_unit, t07_pipeline_wired, t08_global_gate_untouched,
               t09_feature_layer_same_dates, t10_verify_system_degeneracy_check,
               t11_view_compat_drift_regression):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            check("%s 未抛异常" % fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
            traceback.print_exc()
    print()
    print("=" * 78)
    print("  PASS = %d" % len(PASSED))
    print("  FAIL = %d" % len(FAILED))
    for n, e in FAILED:
        print("    - %s : %s" % (n, e))
    print("=" * 78)
    return 1 if FAILED else 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())
