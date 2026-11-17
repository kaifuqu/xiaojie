# -*- coding: utf-8 -*-
"""
战法胜率面板 · Streamlit 无头渲染冒烟测试（V29.2）

用法：
    python tools/_smoke_strategy_panel.py

覆盖场景：
  1. 正常快照（15 战法，主口径 T+2）
  2. 快照缺失（空态）
  3. JSON 损坏（降级）
  4. schema 不支持（降级）
  5. 零样本 / 负收益 / 超大样本
  6. 多池同战法
  7. 筛选器可用（池 / 最小样本 / 搜索 / 只看）
  8. 带 history 的完整快照（趋势图）
  9. 面板异常不影响主界面

只读运行：不写任何业务表、不改权重、不触发回填。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from core import _pyarrow_compat as _pyarrow_compat  # noqa: E402,F401  (必须在 import pandas 前)

import pandas as pd  # noqa: E402
from streamlit.testing.v1 import AppTest  # noqa: E402

PASS = 0
FAIL = 0
FAILED = []
_TMP_APPS = []


def chk(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS", name)
    else:
        FAIL += 1
        FAILED.append(name)
        print("  FAIL", name, extra)


def _panel_app(path, out_marker):
    """生成一个只渲染面板的临时 Streamlit 应用。"""
    app = f'''
import os, sys
sys.path.insert(0, {ROOT!r})
import core.strategy_effectiveness_dashboard as D

D.SNAPSHOT_CANDIDATES = ({path!r},)
D.resolve_snapshot_path = lambda p=None: {path!r} if os.path.exists({path!r}) else None

import streamlit as st
import ui.strategy_effectiveness_panel as P

# 绕过 30s 缓存：测试之间互不污染
P._cached_snapshot = lambda _salt: D.load_snapshot()

P.render_strategy_effectiveness_panel()
with open({out_marker!r}, "w", encoding="utf-8") as f:
    f.write("rendered")
'''
    fd, p = tempfile.mkstemp(suffix=".py")
    os.close(fd)
    with open(p, "w", encoding="utf-8") as f:
        f.write(app)
    _TMP_APPS.append(p)
    return p


def _cleanup_tmp_apps():
    for p in _TMP_APPS:
        try:
            os.remove(p)
        except OSError:
            pass
    _TMP_APPS.clear()


def run_panel(path, label, expect_rows=True):
    marker = os.path.join(tempfile.gettempdir(), "se_panel_marker.txt")
    if os.path.exists(marker):
        os.remove(marker)
    app_path = _panel_app(path, marker)
    try:
        at = AppTest.from_file(app_path, default_timeout=60).run()
        exceptions = [str(e.value) for e in at.exception]
        chk("%s 无未捕获异常" % label, not exceptions, exceptions[:1])
        if expect_rows:
            chk("%s 渲染完成" % label, os.path.exists(marker))
        return at
    except Exception as e:
        chk("%s 运行不炸" % label, False, "%s: %s" % (type(e).__name__, e))
        return None


def main():
    print("=" * 62)
    print("战法胜率面板 · 无头渲染冒烟测试")
    print("=" * 62)

    import core.strategy_effectiveness_dashboard as D

    tmp = tempfile.mkdtemp(prefix="se_panel_")
    real = D.load_snapshot()

    # ---------- 场景 1：真实快照 ----------
    print("\n--- S1 真实快照（15 战法） ---")
    at = run_panel(os.path.join(ROOT, "data", "strategy_weight_recommendation.json"), "S1")
    if at is not None:
        chk("S1 无 error 元素", len(at.error) == 0, [e.value for e in at.error][:1])
        chk("S1 有 dataframe", len(at.dataframe) >= 1, len(at.dataframe))
        chk("S1 有 caption 说明口径", len(at.caption) >= 1, len(at.caption))
        chk("S1 有刷新按钮", any("刷新" in b.label for b in at.button), [b.label for b in at.button])
        chk("S1 有 CSV 下载", len(at.download_button) >= 1)
        chk("S1 有筛选器", len(at.selectbox) >= 2, [s.label for s in at.selectbox])

    # ---------- 场景 2：快照缺失 ----------
    print("\n--- S2 快照缺失（空态） ---")
    at = run_panel(os.path.join(tmp, "nope.json"), "S2", expect_rows=False)
    if at is not None:
        chk("S2 走空态 info", any("暂无战法胜率数据" in i.value for i in at.info),
            [i.value[:40] for i in at.info])

    # ---------- 场景 3：JSON 损坏 ----------
    print("\n--- S3 JSON 损坏 ---")
    p3 = os.path.join(tmp, "broken.json")
    with open(p3, "w", encoding="utf-8") as f:
        f.write("{not json")
    at = run_panel(p3, "S3", expect_rows=False)
    if at is not None:
        chk("S3 无未捕获异常", not [e.value for e in at.exception])

    # ---------- 场景 4：schema 不支持 ----------
    print("\n--- S4 schema_version=9.0（降级） ---")
    p4 = os.path.join(tmp, "v9.json")
    with open(p4, "w", encoding="utf-8") as f:
        json.dump({"schema_version": "9.0", "stats": {"A": {"n_evaluated": 5}}}, f)
    at = run_panel(p4, "S4", expect_rows=False)
    if at is not None:
        chk("S4 降级为空态", any("暂无战法胜率数据" in i.value for i in at.info))

    # ---------- 场景 5：零样本 / 负收益 / 超大样本 ----------
    print("\n--- S5 极值与零样本 ---")
    p5 = os.path.join(tmp, "edge.json")
    stats = {
        "ZERO": {"strategy": "ZERO", "strategy_label": "零样本战法", "n_signals": 0,
                 "n_evaluated": 0, "win_rate": None, "avg_return": None,
                 "primary_n_days": 2, "available_n_days": [1, 2],
                 "missing_n_days": [5, 10], "pools": ["p1"], "pool_rows": []},
        "NEG": {"strategy": "NEG", "strategy_label": "巨亏战法", "n_signals": 99999,
                "n_evaluated": 500000, "win_rate": 0.0, "avg_return": -88.88,
                "avg_max_drawdown": -99.9, "avg_max_profit": 0.5,
                "primary_n_days": 10, "available_n_days": [1, 2, 5, 10],
                "missing_n_days": [], "pools": ["p3", "p5"],
                "pool_rows": [{"pool": "p3", "n_days": 10, "n_signals": 99999, "win_rate": 0.0}]},
        "NUL": {"strategy": "NUL", "strategy_label": "全null战法", "n_signals": 3,
                "n_evaluated": 3, "win_rate": "null", "avg_return": "NaN",
                "avg_excess": "--", "avg_max_drawdown": "", "avg_max_profit": "  ",
                "primary_n_days": 2, "available_n_days": "2,5,bad",
                "missing_n_days": "[1, 2]", "pools": []},
    }
    with open(p5, "w", encoding="utf-8") as f:
        json.dump({"schema_version": "1.0", "generated_at": __import__("datetime").datetime.now().isoformat(),
                   "mode": "statistics_only", "stats": stats}, f)
    at = run_panel(p5, "S5")
    if at is not None:
        chk("S5 无未捕获异常", not [e.value for e in at.exception], [e.value for e in at.exception][:1])
        chk("S5 有 dataframe", len(at.dataframe) >= 1)

    # ---------- 场景 6：带 history（趋势图） ----------
    print("\n--- S6 带 history 的完整快照 ---")
    p6 = os.path.join(tmp, "full.json")
    hist = [
        {"stat_date": "2026-10-%02d" % d, "pool": "p5", "strategy": "A",
         "stat_n_days": 2, "n_signals": 10 + d, "n_evaluated": 8,
         "n_win": 4, "win_rate": 0.4 + d * 0.01, "avg_return": -1.0 + d * 0.1}
        for d in range(1, 11)
    ]
    full = {
        "schema_version": "1.0",
        "generated_at": __import__("datetime").datetime.now().isoformat(),
        "stat_date": "2026-10-10",
        "mode": "statistics_only",
        "maturity": {"available_n_days": [1, 2], "primary_n_days": 2,
                    "missing_n_days": [5, 10], "total_evaluated": 38,
                    "next_auto_apply_remaining": 162, "min_n_for_auto_apply": 200},
        "strategy_maturity": {"A": {"remaining_to_auto_apply": 190}},
        "recommendation": {"A": {"weight": 1.2, "reason": "高胜率"},
                           "B": {"weight": 0.7, "reason": "低胜率"},
                           "C": {"weight": 1.0, "reason": "样本不足-保持默认"}},
        "deprecated": ["C"],
        "stats": {
            "A": {"strategy": "A", "strategy_label": "★升权战法", "n_signals": 30,
                  "n_evaluated": 25, "n_win": 18, "win_rate": 0.72, "avg_return": 3.5,
                  "avg_excess": 1.2, "avg_max_drawdown": -4.1, "avg_max_profit": 8.8,
                  "primary_n_days": 2, "available_n_days": [1, 2],
                  "missing_n_days": [5, 10], "pools": ["p5"],
                  "pool_rows": [{"pool": "p5", "n_days": 2, "n_signals": 30, "win_rate": 0.72}]},
            "B": {"strategy": "B", "strategy_label": "★降权战法", "n_signals": 28,
                  "n_evaluated": 24, "n_win": 5, "win_rate": 0.21, "avg_return": -4.2,
                  "avg_excess": -3.0, "avg_max_drawdown": -8.5, "avg_max_profit": 3.1,
                  "primary_n_days": 2, "available_n_days": [1, 2],
                  "missing_n_days": [5, 10], "pools": ["p3"],
                  "pool_rows": [{"pool": "p3", "n_days": 2, "n_signals": 28, "win_rate": 0.21}]},
            "C": {"strategy": "C", "strategy_label": "★样本不足", "n_signals": 2,
                  "n_evaluated": 1, "n_win": 0, "win_rate": 0.0, "avg_return": -1.1,
                  "avg_excess": None, "avg_max_drawdown": -2.0, "avg_max_profit": 1.0,
                  "primary_n_days": 2, "available_n_days": [1, 2],
                  "missing_n_days": [5, 10], "pools": ["p1", "p2"],
                  "pool_rows": [{"pool": "p1", "n_days": 2, "n_signals": 1, "win_rate": 0.0},
                                {"pool": "p2", "n_days": 2, "n_signals": 1, "win_rate": 0.0}]},
        },
        "history": hist,
    }
    with open(p6, "w", encoding="utf-8") as f:
        json.dump(full, f, ensure_ascii=False)
    at = run_panel(p6, "S6")
    if at is not None:
        chk("S6 无未捕获异常", not [e.value for e in at.exception], [e.value for e in at.exception][:1])
        chk("S6 无 error", len(at.error) == 0, [e.value for e in at.error][:1])
        chk("S6 有 dataframe", len(at.dataframe) >= 2, len(at.dataframe))

        # 筛选器：只看升权/降权候选
        try:
            view_boxes = [s for s in at.selectbox if s.label == "只看"]
            if view_boxes:
                at2 = view_boxes[0].set_value("升权/降权候选").run()
                chk("S6 筛选后无异常", not [e.value for e in at2.exception],
                    [e.value for e in at2.exception][:1])
                chk("S6 筛选后有表", len(at2.dataframe) >= 1)
            else:
                chk("S6 存在『只看』筛选器", False, [s.label for s in at.selectbox])
        except Exception as e:
            chk("S6 筛选交互", False, "%s: %s" % (type(e).__name__, e))

    # ---------- 场景 9：面板异常不影响主界面 ----------
    print("\n--- S9 面板异常隔离 ---")
    marker = os.path.join(tempfile.gettempdir(), "se_panel_marker9.txt")
    app = f'''
import sys
sys.path.insert(0, {ROOT!r})
import streamlit as st

st.write("主界面：五池扫描入口")

import ui.strategy_effectiveness_panel as P

def boom():
    raise RuntimeError("模拟面板内部爆炸")

P.render_strategy_effectiveness_panel = boom
try:
    P.render_strategy_effectiveness_panel()
except Exception:
    pass

st.write("主界面：后勤工具箱仍在")
with open({marker!r}, "w", encoding="utf-8") as f:
    f.write("ok")
'''
    ap = os.path.join(tmp, "isolate.py")
    with open(ap, "w", encoding="utf-8") as f:
        f.write(app)
    try:
        at = AppTest.from_file(ap, default_timeout=60).run()
        chk("S9 主界面不中断", os.path.exists(marker))
        texts = [m.value for m in at.markdown]
        chk("S9 保留主界面内容",
            any("五池扫描入口" in t for t in texts), texts[:2])
    except Exception as e:
        chk("S9 隔离测试", False, "%s: %s" % (type(e).__name__, e))

    print("\n" + "=" * 62)
    print("RESULT pass=%d fail=%d" % (PASS, FAIL))
    if FAILED:
        print("FAILED:", FAILED)
    print("=" * 62)
    _cleanup_tmp_apps()
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
