# -*- coding: utf-8 -*-
r"""
tools/_test_list_days_chain.py — 次新股过滤链永久回归（不写主库）

【背景】
  dim_security.list_date 早已落库（5916 行全部非空），但生产消费链从未注入：
    - get_stock_data_qfq() 返回的日线没有 list_date / list_days
    - fetch_realtime_batch() 的实时快照没有 list_days
    - P1 event_filter / P2 次新拦截 / P5 新股掩码全部落到默认值 999 → 永不放行
  本测试锁死「注入 → 消费」两端，防止再次静默退化。

【运行】
  D:\xiaojie\_pyenv\venv\Scripts\python.exe tools\_test_list_days_chain.py

【覆盖】
  L1  _load_security_list_date_map 读 dim_security 上市日（只读）
  L2  annotate_list_days 注入 list_date / list_days
  L3  get_stock_data_qfq 真实路径返回带 list_days 列
  L4  P5 _is_new_stock_mask_p5 对注入后的列给出正确掩码
  L5  P2-P5 detect_special_stock_type 消费 rt["list_days"]
  L6  core.event_filter.is_new_stock 对注入后的 list_date 正确判定
  L7  维表缺码时 list_days 兜底为 len(df)，不产生 0 值假次新
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core._pyarrow_compat import disable_pyarrow  # noqa: E402

disable_pyarrow()

import pandas as pd  # noqa: E402

RESULTS = []


def _run(name, fn):
    try:
        fn()
        RESULTS.append((name, "PASS", ""))
        print("  [PASS] %s" % name)
    except AssertionError as e:
        RESULTS.append((name, "FAIL", str(e)))
        print("  [FAIL] %s: %s" % (name, e))
    except Exception as e:  # noqa: BLE001
        RESULTS.append((name, "FAIL", "%s: %s" % (type(e).__name__, e)))
        print("  [FAIL] %s: %s: %s" % (name, type(e).__name__, e))


def _t1_map():
    from data.db_core import _trade_dates as td

    m = td._load_security_list_date_map()
    assert isinstance(m, dict) and len(m) > 100, "上市日映射过小: %d" % len(m)
    bad = [v for v in m.values() if not (len(v) == 10 and v[4] == "-" and v[7] == "-")]
    assert not bad, "上市日格式非法样例: %s" % bad[:3]


def _t2_annotate():
    from data.db_core import _trade_dates as td

    df = pd.DataFrame({"trade_date": ["20261014", "20261015", "20261016"], "close": [1.0, 2.0, 3.0]})
    out = td.annotate_list_days(df.copy(), ts_code="600519.SH", as_of_date="20261016")
    assert "list_date" in out.columns and "list_days" in out.columns
    ld = str(out["list_date"].iloc[-1])
    lv = int(out["list_days"].iloc[-1])
    assert len(ld) == 10, "list_date 应为 YYYY-MM-DD, 实得 %s" % ld
    assert lv > 5000, "600519 上市天数应远大于 5000, 实得 %d" % lv


def _t3_real_path():
    from data.db_core import get_stock_data_qfq

    df = get_stock_data_qfq("600519.SH", limit=60)
    assert df is not None and not df.empty, "600519 日线为空（主库被占用？）"
    assert "list_days" in df.columns, "get_stock_data_qfq 未注入 list_days"
    assert int(df["list_days"].iloc[-1]) > 5000


def _t4_p5_mask():
    from core.strategies.p5_postmarket_screener import _is_new_stock_mask_p5

    new_df = pd.DataFrame({"trade_date": ["20261016"], "list_days": [3]})
    old_df = pd.DataFrame({"trade_date": ["20261016"], "list_days": [900]})
    ld_df = pd.DataFrame({"trade_date": ["20261016"], "list_date": ["2026-10-10"]})
    assert _is_new_stock_mask_p5(new_df).tolist() == [True]
    assert _is_new_stock_mask_p5(old_df).tolist() == [False]
    assert _is_new_stock_mask_p5(ld_df).tolist() == [True]


def _t5_special_type():
    from core.p2_p5_a_share_enhance import detect_special_stock_type

    assert detect_special_stock_type({"list_days": 3})["stock_type"] == "new_stock_30d"
    assert detect_special_stock_type({"list_days": 45})["stock_type"] == "new_stock_60d"
    assert detect_special_stock_type({"list_days": 999})["stock_type"] == "normal"


def _t6_event_filter():
    from core.event_filter import is_new_stock
    from datetime import datetime, timedelta

    # 次新判定以「日历日差」为口径，写死 list_date 会让本用例在 30 天后自动过期
    # （2026-10-10 在 2026-11-16 已满 37 天，旧写法必然误报 FAIL）。改为动态日期。
    recent = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    assert is_new_stock("301569.SZ", list_date=recent) is True
    assert is_new_stock("600519.SH", list_date="2001-08-27") is False


def _t7_no_false_new():
    from data.db_core import _trade_dates as td

    saved = dict(td._SECURITY_LIST_DATE_CACHE)
    saved_ts = td._SECURITY_LIST_DATE_CACHE_TS
    try:
        td._SECURITY_LIST_DATE_CACHE = {}
        td._SECURITY_LIST_DATE_CACHE_TS = 0.0
        td._load_security_list_date_map()
        df = pd.DataFrame({"trade_date": ["20261016"]})
        out = td.annotate_list_days(df.copy(), ts_code="__NOT_A_CODE__", as_of_date="20261016")
        assert "list_days" in out.columns
        assert int(out["list_days"].iloc[-1]) >= 1, "维表缺码时 list_days 不能为 0（会伪装成次新）"
    finally:
        td._SECURITY_LIST_DATE_CACHE = saved
        td._SECURITY_LIST_DATE_CACHE_TS = saved_ts


def main():
    print("=" * 64)
    print("次新股过滤链永久回归 tools/_test_list_days_chain.py")
    print("=" * 64)
    cases = (
        ("L1 dim_security 上市日映射可读且格式归一", _t1_map),
        ("L2 annotate_list_days 注入 list_date/list_days", _t2_annotate),
        ("L3 get_stock_data_qfq 真实路径带 list_days", _t3_real_path),
        ("L4 P5 新股掩码消费注入列", _t4_p5_mask),
        ("L5 P2-P5 次新检测消费 rt.list_days", _t5_special_type),
        ("L6 event_filter 消费注入的 list_date", _t6_event_filter),
        ("L7 维表缺码不产生 0 值假次新", _t7_no_false_new),
    )
    for name, fn in cases:
        _run(name, fn)
    passed = len([1 for _, s, _ in RESULTS if s == "PASS"])
    failed = len(RESULTS) - passed
    print("-" * 64)
    print("结果: %d PASS / %d FAIL" % (passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
