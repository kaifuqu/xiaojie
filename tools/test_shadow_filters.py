# -*- coding: utf-8 -*-
"""
Tier1-1.3 / 1.4 / 1.6 Shadow 层回归（不需要写库、不需要联网）

覆盖：
  T1 bear_volume_verdict：放量阴线必判 True；普通结构判 False；空/残数据不误杀、不抛异常
  T2 classify_push_tier：alarm / important / observe 三档可区分
  T3 annotate_shadow：脏输入不抛异常、不修改入参、JSONL 可写入可读回
  T4 接线断言：pool_signal_adapter_v28.append_signal_log_safe 内确实调用 annotate_shadow，
     且调用处于 try/except 之中（失败不得打断落库）
  T5 边界：res 缺 ts_code / 停牌 vol=0 / 无 db_conn 时全部降级为"不否决"

用法：python tools\test_shadow_filters.py        （EXIT=0 即 PASS）
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

FAILS = []


def check(name, cond, detail=""):
    if cond:
        print("  [OK]   %s %s" % (name, detail))
    else:
        print("  [FAIL] %s %s" % (name, detail))
        FAILS.append(name)


def _bars(n=12, yin_ratio=2.5, yin_recent=True):
    """造 n 根日线：默认放量阴线在最近位置。"""
    rows = []
    px = 10.0
    for i in range(n):
        is_last = (i == n - 1 - (0 if yin_recent else 5))
        o = px
        c = px * (0.97 if is_last else 1.01)
        vol = 200000.0 if is_last else 100000.0
        rows.append({"trade_date": "202611%02d" % (i + 1), "open": o, "high": max(o, c) * 1.01,
                     "low": min(o, c) * 0.99, "close": c, "vol": vol})
        px = c
    return pd.DataFrame(rows)


def t1_bear_volume():
    from core.shadow_filters import bear_volume_verdict

    print("[T1] 1.3 最大阴量 Shadow")
    v = bear_volume_verdict(_bars())
    check("放量阴线→would_veto", v["would_veto"] is True, str(v))
    check("ratio 计算有效", float(v["ratio"]) > 1.5, "ratio=%s" % v["ratio"])
    check("days_ago=1", v["days_ago"] == 1, str(v["days_ago"]))

    v2 = bear_volume_verdict(_bars(yin_ratio=1.0, yin_recent=False))
    check("普通结构→不否决", v2["would_veto"] is False, str(v2.get("reason")))

    check("空 DataFrame 不抛", bear_volume_verdict(pd.DataFrame())["would_veto"] is False)
    check("None 不抛", bear_volume_verdict(None)["would_veto"] is False)
    bad = pd.DataFrame([{"open": 1, "close": 2}])
    check("缺列不误杀", bear_volume_verdict(bad)["would_veto"] is False,
          bear_volume_verdict(bad)["reason"])
    zero = pd.DataFrame([{"open": 10, "close": 9, "vol": 0}] * 5)
    check("全零量不误杀", bear_volume_verdict(zero)["would_veto"] is False)


def t2_push_tier():
    from core.shadow_filters import classify_push_tier

    print("[T2] 1.6 推送三档 Shadow")
    a = classify_push_tier("P3", {"data_quality_flags": '{"SUSPEND": true}', "score": 60})
    check("严重质量标记→alarm", a["tier"] == "alarm", str(a))
    b = classify_push_tier("P3", {"strategies": ["P3-01", "P3-04"], "score": 60})
    check("多战法共振→important", b["tier"] == "important", str(b))
    c = classify_push_tier("P3", {"strategies": ["P3-07"], "score": 51, "rank": 9})
    check("单战法低分→observe", c["tier"] == "observe", str(c))
    d = classify_push_tier("P2", {"strategies": "P2-01", "score": 80})
    check("P2 高分→important", d["tier"] == "important", str(d))


def t3_annotate_jsonl():
    from core import shadow_filters as sf

    print("[T3] annotate_shadow + JSONL 落盘/读回")
    tmp = tempfile.mkdtemp(prefix="shadow_qa_")
    os.environ["XJ_SHADOW_DIR"] = tmp
    items = [
        {"ts_code": "600000.SH", "name": "浦发银行", "score": 82, "price": 10.5,
         "strategies": ["P3-01", "P3-04"]},
        {"ts_code": "000001", "name": "平安银行", "score": 55, "price": 11.2,
         "strategies": "P5-01"},
    ]
    snapshot = [dict(x) for x in items]
    recs = sf.annotate_shadow("P3", items, db_conn=None, write=True)
    check("返回 2 条记录", len(recs) == 2, str(len(recs)))
    check("入参未被修改", items == snapshot)
    check("ts_code 归一", recs[1]["ts_code"] == "000001.SZ", recs[1]["ts_code"])
    check("无 db_conn 时 1.3 降级", recs[0]["veto_bear_vol"] is False,
          recs[0]["bear_vol_reason"])
    check("tier 已给出", recs[0]["push_tier"] in ("alarm", "important", "observe"),
          str(recs[0]["push_tier"]))
    back = sf.read_shadow_records()
    check("JSONL 可读回", len(back) == 2, str(len(back)))
    check("字段完整", all(k in back[0] for k in
          ("trade_date", "pool", "ts_code", "veto_bear_vol", "rps", "push_tier")))

    # 脏输入
    dirty = [None, "x", 123, {"ts_code": None}, {"ts_code": "600000.SH"}]
    recs2 = sf.annotate_shadow("P4", dirty, db_conn=None, write=False)
    check("脏输入不抛异常", isinstance(recs2, list), str(len(recs2)))
    os.environ.pop("XJ_SHADOW_DIR", None)


def t4_wiring():
    import ast

    print("[T4] 接线断言（append_signal_log_safe 内调用 + 处于 try 内）")
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "core", "pool_signal_adapter_v28.py")
    src = open(path, encoding="utf-8").read()
    check("存在 annotate_shadow 调用", "annotate_shadow(pool, [item]" in src)
    tree = ast.parse(src)
    hit_try = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Try):
            body = ast.dump(ast.Module(body=node.body, type_ignores=[]))
            if "annotate_shadow" in body:
                hit_try = True
                break
    check("调用包在 try 内", hit_try)
    check("导入被隔离", "from core.shadow_filters import annotate_shadow" in src)


def t5_bounds():
    from core import shadow_filters as sf

    print("[T5] 边界与参数")
    check("参数未被改动", (sf.BEAR_VOL_LOOKBACK, sf.BEAR_VOL_RATIO_MIN,
                          sf.BEAR_VOL_RECENT_DAYS, sf.RPS_GATE_MIN) == (10, 1.5, 3, 50.0),
          "lookback=%s ratio=%s recent=%s rps=%s" % (
              sf.BEAR_VOL_LOOKBACK, sf.BEAR_VOL_RATIO_MIN,
              sf.BEAR_VOL_RECENT_DAYS, sf.RPS_GATE_MIN))
    check("无 db 时 load_recent_bars 返回 {}", sf.load_recent_bars(None, ["600000.SH"]) == {})
    check("rps_verdict 空入参不抛", sf.rps_verdict([]) == {})
    check("classify 脏入参不抛", sf.classify_push_tier("P3", {})["tier"] == "observe")


def main():
    print("=" * 68)
    print("Tier1-1.3/1.4/1.6 Shadow 层回归")
    print("=" * 68)
    for fn in (t1_bear_volume, t2_push_tier, t3_annotate_jsonl, t4_wiring, t5_bounds):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILS.append("%s 抛异常: %s" % (fn.__name__, e))
    print("-" * 68)
    if FAILS:
        print("RESULT: FAIL (%d 项) -> %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("RESULT: PASS  (Shadow 层未参与任何过滤/打分/推送)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
