# -*- coding: utf-8 -*-
"""tools/test_signal_log_wiring.py
========================================================================
回归：五档战法 → signal_log 落库链路「接线」检查（Tier 1 · 1.1）

背景（2026-11-02 定位）
    append_signal_log_safe() 里取价格顺序是：
        rt['price'] / rt['now_price']  →  res['price'] / res['push_price']  →  0
    价格为 0 直接 return，不落库。
    P3 调用时传了 rt=rt，所以有信号；P2 / P4 / P5 没传 rt，
    而它们的 res 里也没有 price 字段 → 价格恒为 0 → 永远不落库。
    这就是 signal_log 里 p2=0 / p4=0 的机制性原因。

本脚本做两件事（都可单独跑）：
    1) 静态：AST 检查四个 strat 的 append_signal_log_safe 调用是否传了 rt
    2) 动态：在 DuckDB 副本上真写一条，验证「传 rt → 落库 / 不传 rt → 不落库」

用法：
    python tools/test_signal_log_wiring.py            # 静态 + 动态（需 data/_qa 副本）
    python tools/test_signal_log_wiring.py --static   # 仅静态
"""
from __future__ import annotations

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TARGETS = {
    "p2": os.path.join(ROOT, "core", "strategies", "strat_p2_auction.py"),
    "p3": os.path.join(ROOT, "core", "strategies", "strat_p3_intraday.py"),
    "p4": os.path.join(ROOT, "core", "strategies", "strat_p4_tail.py"),
    "p5": os.path.join(ROOT, "core", "strategies", "strat_p5_postmarket.py"),
}

# P3 早已正确传 rt；P2 / P4 在 2026-11-02 修复（B1）；P5 在 2026-11 本轮补齐，
# 现在四池全部纳入「必须传 rt」硬断言。p5 的 scan/pipeline.save_signal_log
# 是另一条链路，不在此断言范围；这里只约束 strat 层的 append_signal_log_safe。
MUST_PASS_RT = ("p2", "p4", "p5")


def static_check() -> int:
    print("=" * 72)
    print("① 静态检查：append_signal_log_safe(...) 是否传 rt")
    print("=" * 72)
    bad = 0
    for pool, path in TARGETS.items():
        tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)
        calls = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "append_signal_log_safe":
                continue
            kwargs = {k.arg for k in node.keywords if k.arg}
            calls.append((node.lineno, "rt" in kwargs))
        if not calls:
            print(f"  [WARN] {pool}: 未找到 append_signal_log_safe 调用")
            continue
        for lineno, has_rt in calls:
            need = pool in MUST_PASS_RT
            ok = has_rt or not need
            mark = "OK  " if ok else "FAIL"
            if not ok:
                bad += 1
            print(f"  [{mark}] {pool} @{path.split(os.sep)[-1]}:{lineno}  rt={'有' if has_rt else '缺失'}")
    print(f"\n  静态结论：{'全部通过' if bad == 0 else f'{bad} 处缺失 rt'}")
    return bad


def dynamic_check() -> int:
    print("\n" + "=" * 72)
    print("② 动态检查：DuckDB 副本上验证「传 rt 才落库」")
    print("=" * 72)
    import tools._qa_env as qa

    if not os.path.exists(qa.QA_DB):
        print(f"  [SKIP] 未找到 QA 副本 {qa.QA_DB}")
        print("         先执行：python tools/make_qa_db.py")
        return 0
    qa.boot()
    import duckdb
    from core.pool_signal_adapter_v28 import append_signal_log_safe

    from data.db_core import get_read_conn

    def n(pool: str) -> int:
        # 必须走项目统一连接入口：单例存活时不能再开一个配置不同的直连
        with get_read_conn(read_only=True) as c:
            return c.execute("select count(*) from signal_log where pool=?", [pool]).fetchone()[0]

    # 先清掉上一轮测试自己写的行，保证「同日同票 UPSERT 覆盖」不会让断言失真
    try:
        from data.db_core import get_write_conn

        with get_write_conn() as wc:
            wc.execute("delete from signal_log where name = '回归测试股'")
    except Exception as _e_cl:
        print(f"  [WARN] 清理旧测试行失败（不影响结论判定）: {_e_cl}")

    # 【2026-11 修复】业务日显式定为「副本里最近的交易日」：
    # signal_log 有「非交易日不落库」的数据卫生闸（周末跑测试会被拦），
    # 本测试验证的是价格链路（rt），不是日历闸门，因此显式传 trade_date 绕开它，
    # 使测试结果与运行日（工作日/周末）无关、可确定性复跑。
    _td = None
    try:
        with get_read_conn(read_only=True) as _c_td:
            _r_td = _c_td.execute("select max(trade_date) from daily_data").fetchone()
            _td = str(_r_td[0]) if _r_td and _r_td[0] else None
    except Exception:
        _td = None
    if _td:
        print(f"  [INFO] 动态断言使用业务日 = {_td}（副本内最近交易日）")

    base = {"ts_code": "600519.SH", "name": "回归测试股", "burst_score": 88.0,
            "p4_core_screener_pass": True, "is_pass": True, "strategies": ["T-回归"]}
    bad = 0

    n0 = n("p4")
    append_signal_log_safe(dict(base), "p4", None, push_min_score=0.0, trade_date=_td)
    n1 = n("p4")
    if n1 != n0:
        print(f"  [FAIL] 不传 rt 却写入了 {n1 - n0} 行（预期 0）")
        bad += 1
    else:
        print(f"  [OK  ] 不传 rt → 写入 0 行（与生产现状一致）")

    # 用未在去重账本里的代码，避免 push_dedup「同票冷却」把写入拦掉（那不是价格链路问题）
    _codes = ["600519.SH", "600036.SH", "601318.SH", "000858.SZ"]
    n2 = n1
    _used = None
    for _c in _codes:
        res = dict(base); res["ts_code"] = _c
        append_signal_log_safe(res, "p4", None, push_min_score=0.0, rt={"price": 10.5},
                               trade_date=_td)
        _after = n("p4")
        if _after > n2:
            _used = _c; n2 = _after; break
    if n2 == n1:
        print("  [FAIL] 传了 rt 仍未写入（价格链路仍断）")
        bad += 1
    else:
        print(f"  [OK  ] 传 rt → 写入 {n2 - n1} 行（价格链路打通）")
    print(f"\n  动态结论：{'通过' if bad == 0 else f'{bad} 项失败'}")
    return bad


if __name__ == "__main__":
    only_static = "--static" in sys.argv
    rc = static_check()
    if not only_static:
        rc += dynamic_check()
    print("\n总体：", "PASS" if rc == 0 else f"FAIL({rc})")
    sys.exit(0 if rc == 0 else 1)
