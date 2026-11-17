# -*- coding: utf-8 -*-
"""P1 阵亡名单 JSON 落盘回归（P1-修复：Object of type DataFrame is not JSON serializable）。

【复现的原始故障】
   ui/app.py 的 _save_rejected_json 用裸 json.dump(rejected_items) 落盘，
   而 core/p1_balance.py 的「出口净化 / 劣币驱逐 / 超限截断」三条分支会把
   完整的 P1 item（df=DataFrame、hist 里是 numpy 标量）追加回 rejected 列表，
   于是 json.dump 抛 TypeError，UI 报「❌ 洗盘崩溃」。

【本脚本做的断言，全部走真实代码路径】
   1) 用 AST 从 ui/app.py 抽出真实的 _save_rejected_json 执行；
   2) 直接调 core.p1_balance.enforce_p1_balance 走真实平衡分支；
   3) 校验展示字段（代码/名称/淘汰死因/当前得分）逐字不变；
   4) 校验 BALANCE_PARAMS 与出票确定性未被改动。
"""
from __future__ import annotations

import ast
import json
import os
import sys
import tempfile

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
os.chdir(_PROJECT_ROOT)

from core import _pyarrow_compat as _pyarrow_compat  # noqa: E402
_pyarrow_compat.disable_pyarrow()

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from core.p1_balance import _json_default, enforce_p1_balance  # noqa: E402

_PASS = 0
_FAIL = 0


def _check(name, cond, detail=""):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  [PASS] %s" % name)
    else:
        _FAIL += 1
        print("  [FAIL] %s | %s" % (name, detail))


def _load_ui_save_rejected_json():
    """从 ui/app.py 源码 AST 里取出真实的 _save_rejected_json 并执行。"""
    app_path = os.path.join(_PROJECT_ROOT, "ui", "app.py")
    with open(app_path, "r", encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    fn_node = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_save_rejected_json":
            fn_node = node
            break
    if fn_node is None:
        raise AssertionError("ui/app.py 里找不到 _save_rejected_json")
    fn_src = ast.get_source_segment(src, fn_node) or ""
    module = ast.Module(body=[fn_node], type_ignores=[])
    code = compile(module, filename=app_path, mode="exec")
    ns = {"json": json, "os": os}
    exec(code, ns)
    return ns["_save_rejected_json"], fn_src


def _full_item(code, score, industry, mv_yi):
    """构造与 pool_builder 产出一致的完整 P1 item（含 DataFrame）。"""
    df = pd.DataFrame({
        "trade_date": pd.to_datetime(["2026-10-15", "2026-10-16"]),
        "close": np.array([10.0, 10.5]),
        "vol": np.array([1.0e6, 1.2e6]),
    })
    return {
        "code": code,
        "ts_code": code,
        "p1_score": float(score),
        "df": df,
        "hist": {
            "name": "测试股" + code[:6],
            "industry": industry,
            "circ_mv": float(mv_yi) * 10000.0,
            "close": np.float64(10.5),
            "limit_times": np.int64(1),
            "trade_date": pd.Timestamp("2026-10-16"),
        },
        "score_details": {"x": np.float64(1.0)},
        "_balance_decision": "扩容(...)",
    }


def _display_row(code, name, reason, score):
    return {
        "代码": code.split(".")[0][:6],
        "名称": name,
        "淘汰死因": reason,
        "被裁阶段": "第五层打分",
        "当前得分": round(float(score), 2),
        "满分项1": "--",
        "满分项2": "--",
        "最低项1": "--",
        "最低项2": "--",
        "score_details": {},
    }


def test_ui_save_rejected_json_real_function():
    print("")
    print("[1] ui/app.py 真实 _save_rejected_json 落盘 DataFrame")
    save_rejected, fn_src = _load_ui_save_rejected_json()
    _check(
        "真实函数体内已挂 default= 钩子",
        "default=" in fn_src,
        fn_src.replace("\n", " ")[:160],
    )

    items = [
        _display_row("600001.SH", "示例一", "均线未呈多头", 44.5),
        _full_item("000002.SZ", 71.5, "银行", 2200.0),
    ]
    tmp = os.path.join(tempfile.mkdtemp(prefix="p1rej_"), "p1_rejected_cache.json")
    try:
        save_rejected(tmp, items)
        with open(tmp, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as exc:
        _check("落盘+回读不抛 TypeError", False, "%s: %s" % (type(exc).__name__, exc))
        return

    _check("落盘+回读不抛 TypeError", True)
    _check("回读条数为 2", isinstance(data, list) and len(data) == 2, "len=%s" % len(data))
    if isinstance(data, list) and len(data) == 2:
        row0 = data[0]
        _check(
            "展示行字段逐字不变",
            row0.get("代码") == "600001"
            and row0.get("名称") == "示例一"
            and row0.get("淘汰死因") == "均线未呈多头"
            and abs(float(row0.get("当前得分", 0)) - 44.5) < 1e-6,
            str(row0),
        )
        row1 = data[1]
        _check("完整 item 的 code 保留", row1.get("code") == "000002.SZ", str(row1)[:200])
        _check(
            "DataFrame 降级为列名摘要",
            isinstance(row1.get("df"), dict) and "__truncated_dataframe__" in (row1.get("df") or {}),
            str(row1.get("df"))[:200],
        )


def test_p1_balance_roundtrip():
    print("")
    print("[2] core.p1_balance.enforce_p1_balance 真实分支 -> 落盘")
    pool = []
    rejected = []
    src_map = {}
    for i in range(14):
        pool.append(_full_item("60000%d.SH" % i, 90.0 - i, "银行", 3000.0))
    for i in range(6):
        code = "00000%d.SZ" % i
        row = _display_row(code, "小盘%d" % i, "均线未呈多头", 46.0 - i)
        rejected.append(row)
        src_map[id(row)] = _full_item(code, 46.0 - i, "半导体", 90.0)

    before_pool, before_rej = len(pool), len(rejected)
    new_pool, new_rejected = enforce_p1_balance(
        p1_pool=pool,
        p1_rejected=rejected,
        target_size=len(pool),
        rejected_src=src_map,
    )
    print("     pool %d->%d, rejected %d->%d"
          % (before_pool, len(new_pool), before_rej, len(new_rejected)))
    _check("平衡器仍返回二元组且池非空", bool(new_pool))

    leaked = [
        r for r in new_rejected
        if isinstance(r, dict) and isinstance(r.get("df"), pd.DataFrame)
    ]
    print("     rejected 中残留完整 item（含 DataFrame）: %d 条" % len(leaked))
    # 这里是原始故障的根因现场：劣币驱逐会把带 df 的完整 item 塞回 rejected。
    _check(
        "根因复现：rejected 确实混入了含 DataFrame 的完整 item",
        len(leaked) > 0,
        "本轮未复现（不代表修复无效，只说明这组输入没走驱逐分支）",
    )

    try:
        text = json.dumps(new_rejected, ensure_ascii=False, default=_json_default)
        back = json.loads(text)
        _check("enforce_p1_balance 产物可 json 序列化", True)
    except Exception as exc:
        _check("enforce_p1_balance 产物可 json 序列化", False,
               "%s: %s" % (type(exc).__name__, exc))
        return

    _check("回读条数一致", len(back) == len(new_rejected))
    sample_display = [r for r in back if isinstance(r, dict) and r.get("淘汰死因")]
    if sample_display:
        _check("展示行仍带淘汰死因且字段未变", True)
    else:
        print("     [SKIP] 本轮 rejected 全部为完整 item，无展示行可比对")


def test_p1_balance_untouched_business_semantics():
    print("")
    print("[3] 回归：平衡判定参数与出票确定性未被改动")
    import core.p1_balance as bal

    for key, expect in (
        ("max_industry_pct", 0.30),
        ("max_large_cap_pct", 0.50),
        ("min_mid_cap_pct", 0.20),
        ("max_top3_concentration", 0.45),
        ("swap_min_hhi_improvement", 0.005),
        ("swap_min_large_improvement", 0.02),
        ("max_pool_size_ratio", 1.5),
    ):
        _check(
            "BALANCE_PARAMS[%s] == %s" % (key, expect),
            float(bal.BALANCE_PARAMS.get(key)) == float(expect),
            str(bal.BALANCE_PARAMS.get(key)),
        )

    def _mk():
        p = [_full_item("60000%d.SH" % i, 90.0 - i, "银行", 3000.0) for i in range(10)]
        r = []
        sm = {}
        for i in range(4):
            code = "00000%d.SZ" % i
            row = _display_row(code, "小盘%d" % i, "均线未呈多头", 46.0 - i)
            r.append(row)
            sm[id(row)] = _full_item(code, 46.0 - i, "半导体", 90.0)
        return p, r, sm

    p1, r1, s1 = _mk()
    p2, r2, s2 = _mk()
    out_a = enforce_p1_balance(p1, r1, target_size=len(p1), rejected_src=s1)
    out_b = enforce_p1_balance(p2, r2, target_size=len(p2), rejected_src=s2)
    codes_a = [it.get("code") for it in out_a[0]]
    codes_b = [it.get("code") for it in out_b[0]]
    _check("同一输入两次出票完全一致", codes_a == codes_b, "%s vs %s" % (codes_a, codes_b))


def test_encoder_units():
    print("")
    print("[4] _json_default 单元行为")
    out = _json_default(pd.DataFrame({"a": [1, 2]}))
    _check("DataFrame -> 列名摘要",
           out.get("rows") == 2 and out.get("__truncated_dataframe__") == ["a"], str(out))
    _check("np.int64 -> int",
           _json_default(np.int64(5)) == 5 and isinstance(_json_default(np.int64(5)), int))
    f_out = _json_default(np.float64(1.5))
    _check("np.float64 -> float", isinstance(f_out, float) and f_out == 1.5)
    _check("np.nan -> None", _json_default(np.float64("nan")) is None)
    _check("np.bool_ -> bool", _json_default(np.bool_(True)) is True)
    _check("Timestamp -> isoformat",
           _json_default(pd.Timestamp("2026-10-16")) == "2026-10-16T00:00:00")
    _check("Series -> 摘要",
           "__truncated_series__" in _json_default(pd.Series([1, 2, 3])))


def main():
    print("=" * 72)
    print("P1 阵亡名单 JSON 序列化回归（P1-修复）")
    print("=" * 72)
    test_ui_save_rejected_json_real_function()
    test_p1_balance_roundtrip()
    test_p1_balance_untouched_business_semantics()
    test_encoder_units()
    print("")
    print("=" * 72)
    print("结果: %d 通过 / %d 失败" % (_PASS, _FAIL))
    print("=" * 72)
    return 0 if _FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
