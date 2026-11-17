# -*- coding: utf-8 -*-
"""V30.4 上线前第三批回归：本轮 P0 修复的防回归护栏

覆盖：
  S1  派生列重算 SQL 含未来数据闸（trade_date <= max(target_dates)）
  S2  派生列重算幂等（同日重跑两次结果完全一致）
  S3  _sync_v26_feature_layers 接受 target_dates 并返回 bool
  S4  vma5/vma10/vma20 别名双向映射正确
  S5  _sync_daily_features 全成功语义（ok_layers 参与总判定）
  S6  单日 UPSERT 派生失败 -> 返回 False（不放行）
  S7  daemon 派生列闸先于 last_sync_ok_bj_date 写盘
  S8  pool_signal_adapter 显式透传 trade_date
  S9  signal_logger 非交易日落库闸
  S10 量化指标覆盖（daily_data + feat_daily_*）
运行：_pyenv\\venv\\Scripts\\python.exe tools\\_test_release_regressions3.py
"""
from __future__ import annotations

import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core._pyarrow_compat import disable_pyarrow

disable_pyarrow()

PASSED: list = []
FAILED: list = []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print("  [PASS] %s%s" % (name, ("  -> " + str(extra)) if extra else ""))
    else:
        FAILED.append((name, str(extra)))
        print("  [FAIL] %s  -> %s" % (name, extra))


# ---------------------------------------------------------------- S1
def s01() -> None:
    print("== S1 派生列重算的未来数据闸 ==")
    from data.fetcher import _sync_entry as se

    src = inspect.getsource(se._recompute_daily_derived_features_for_dates)
    check("S1 历史窗口含 trade_date <= max(target_dates) 闸",
          "REPLACE(CAST(trade_date AS VARCHAR), '-', '') <= " in src and "max(dates)" in src)
    check("S1 仍只取目标日 ts_code 的历史窗口", "ROW_NUMBER() OVER" in src and "rn <= " in src)
    check("S1 只 UPDATE 派生列不覆盖原始列", "_DERIVED_FEATURE_COLS" in src and "net_main_amount" not in src.split("UPDATE daily_data")[1][:600])


# ---------------------------------------------------------------- S2
def s02() -> None:
    print("== S2 派生列重算幂等 ==")
    from data.db_core import get_read_conn_singleton
    from data.fetcher._sync_entry import _recompute_daily_derived_features_for_dates

    con = get_read_conn_singleton(max_wait_sec=30.0)
    if con is None:
        check("S2 只读连接可用", False, "None")
        return
    col_expr = 'SUM("ma20"), SUM("macd"), SUM("rsi_14"), SUM("atr_pct")'
    day = "20261016"
    q = (
        "SELECT " + col_expr + " FROM daily_data WHERE REPLACE("
        "CAST(trade_date AS VARCHAR), '-', '') = '" + day + "'"
    )
    def _sums():
        # _recompute_daily_derived_features_for_dates 内部会走 checkpoint，
        # 可能让进程内只读单例失效，所以每次都重新取连接。
        c = get_read_conn_singleton(max_wait_sec=30.0)
        return tuple(c.execute(q).fetchone())

    before = _sums()
    ok1 = _recompute_daily_derived_features_for_dates([day], status_callback=lambda m: None)
    mid = _sums()
    ok2 = _recompute_daily_derived_features_for_dates([day], status_callback=lambda m: None)
    after = _sums()
    check("S2 第一次重算幂等（与重算前一致）", tuple(before) == tuple(mid), "%s vs %s" % (tuple(before), tuple(mid)))
    check("S2 第二次重算幂等（与第一次一致）", tuple(mid) == tuple(after), "%s vs %s" % (tuple(mid), tuple(after)))


# ---------------------------------------------------------------- S3
def s03() -> None:
    print("== S3 分层表同步签名与返回值 ==")
    from data.fetcher import _sync_entry as se

    sig = inspect.signature(se._sync_v26_feature_layers)
    check("S3 接受 target_dates 参数", "target_dates" in sig.parameters, str(sig))
    src = inspect.getsource(se._sync_v26_feature_layers)
    ret = str(sig.return_annotation)
    check("S3 返回 bool 而非 None", ret in ("bool", "<class 'bool'>"), ret)
    check("S3 不再只取 MAX(trade_date) 单日",
          "CAST(trade_date AS VARCHAR), '-', '') = (" not in src.replace("\"", ""))
    check("S3 反向映射 vol_ma* -> vma*",
          "{v: k for k, v in _FEAT_LAYER_SRC_ALIAS.items()}" in src)
    check("S3 缺列清单/缺表 -> all_ok=False", src.count("all_ok = False") >= 2)


# ---------------------------------------------------------------- S4
def s04() -> None:
    print("== S4 vma 别名映射 ==")
    from data.fetcher import _sync_entry as se

    check("S4 别名表三列齐全",
          se._FEAT_LAYER_SRC_ALIAS == {"vma5": "vol_ma5", "vma10": "vol_ma10", "vma20": "vol_ma20"},
          se._FEAT_LAYER_SRC_ALIAS)
    check("S4 feat_daily_core 列清单含 vma5/vma10/vma20",
          all(c in se._FEAT_LAYER_COLS["feat_daily_core"] for c in ("vma5", "vma10", "vma20")))
    check("S4 feat_daily_core 列清单已去掉不存在的 ma30", "ma30" not in se._FEAT_LAYER_COLS["feat_daily_core"])


# ---------------------------------------------------------------- S5
def s05() -> None:
    print("== S5 _sync_daily_features 全成功语义 ==")
    from data.fetcher import _sync_entry as se

    src = inspect.getsource(se._sync_daily_features)
    check("S5 ok_layers 直接取分层返回值", "ok_layers = _sync_v26_feature_layers(" in src)
    check("S5 总判定含 ok_layers", "ok_derived and ok_cap and ok_mem and ok_layers" in src)
    check("S5 已移除 bool(a or b) 宽松语义", "bool(ok_cap or ok_mem)" not in src)


# ---------------------------------------------------------------- S6
def s06() -> None:
    print("== S6 单日 UPSERT 失败语义 ==")
    from data.fetcher import _sync_entry as se

    src = inspect.getsource(se._upsert_incremental_single_day)
    check("S6 派生失败返回 False", "return False" in src.split("派生列修补失败")[-1][:800])
    check("S6 派生成功后刷 feat_daily_*", "_sync_v26_feature_layers(" in src)


# ---------------------------------------------------------------- S7
def s07() -> None:
    print("== S7 daemon 派生列闸位 ==")
    import auto_sniper_daemon as d

    src_gate = inspect.getsource(d._sync_daily_derived_features_after_daily)
    ret7 = str(inspect.signature(d._sync_daily_derived_features_after_daily).return_annotation)
    check("S7 闸函数返回 bool",
          ret7 in ("bool", "<class 'bool'>"), ret7)
    check("S7 失败时 logger.error 且不放行", "-> 19:55 P1 / 20:05 P5 不放行" in src_gate)

    src_ev = inspect.getsource(d._evening_sync_only_pipeline)
    i_gate = src_ev.index("_sync_daily_derived_features_after_daily()")
    i_ok = src_ev.index("_write_pipeline_state_patch(last_sync_ok_bj_date=today")
    check("S7 派生列闸早于 last_sync_ok 写盘", i_gate < i_ok,
          "gate@%d ok_write@%d" % (i_gate, i_ok))
    check("S7 跳过拉取分支同样过闸", src_ev.count("_sync_daily_derived_features_after_daily()") >= 2)
    check("S7 失败分支写 last_sync_fail", 'last_sync_ok_bj_date="", last_sync_fail_bj_date=today' in src_ev)


# ---------------------------------------------------------------- S8
def s08() -> None:
    print("== S8 信号业务日透传 ==")
    import inspect as _i

    from core import pool_signal_adapter as psa

    src = _i.getsource(psa.safe_log_pool_signal)
    check("S8 显式传 trade_date=trade_date", "trade_date=trade_date" in src)


# ---------------------------------------------------------------- S9
def s09() -> None:
    print("== S9 非交易日落库闸 ==")
    import inspect as _i

    from data import signal_logger as sl

    check("S9 存在 _is_non_trading_day", hasattr(sl, "_is_non_trading_day"))
    src = _i.getsource(sl.log_signal)
    check("S9 log_signal 调用非交易日闸", "_is_non_trading_day(" in src)


# ---------------------------------------------------------------- S10
def s10() -> None:
    print("== S10 量化覆盖率 ==")
    # 同一进程内直接调用：DuckDB 单写者锁会让子进程拿不到连接
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_t_data_coverage", os.path.join(ROOT, "tools", "_test_data_coverage.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rc = mod.main()
    check("S10 tools/_test_data_coverage.py 全通过", rc == 0, "rc=%s" % rc)


def main() -> int:
    for fn in (s01, s02, s03, s04, s05, s06, s07, s08, s09, s10):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            check("%s 未抛异常" % fn.__name__, False, "%s: %s" % (type(exc).__name__, exc))
    print("")
    print("  PASS = %d" % len(PASSED))
    print("  FAIL = %d" % len(FAILED))
    for n, e in FAILED:
        print("    - %s : %s" % (n, e))
    return 1 if FAILED else 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    raise SystemExit(main())

