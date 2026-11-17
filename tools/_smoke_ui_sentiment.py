# -*- coding: utf-8 -*-
"""tools/_smoke_ui_sentiment.py — 情绪温度计 Shadow 看板 AppTest 冒烟

做法（不碰生产库 / 不碰生产 JSONL）：
  · 用 QA 副本库的真实 signal_log 日期，现场生成一份临时 shadow JSONL；
  · monkeypatch 评估器的 SHADOW_JSONL 与 _ro_conn.open_ro，让 UI 页
    走同一套真实代码路径；
  · AppTest 跑两遍：有记录（完整渲染）+ 无记录（降级提示），
    断言均无 exception。

用法：python tools/_smoke_ui_sentiment.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

QA_DB = os.path.join(ROOT, "data", "_qa", "quant_data.duckdb")
PAGE = os.path.join(ROOT, "ui", "pages", "3_情绪温度计.py")


def _qa_dates(n: int = 8):
    import duckdb

    con = duckdb.connect(QA_DB, read_only=True)
    try:
        rows = con.execute(
            "SELECT DISTINCT CAST(log_date AS VARCHAR) FROM signal_log "
            "ORDER BY 1 DESC LIMIT ?", [int(n)]
        ).fetchall()
    finally:
        con.close()
    return sorted(str(r[0]) for r in rows)


def _make_shadow_jsonl(dates) -> str:
    states = ["高潮期", "回暖期", "平稳期", "混沌期", "退潮中段", "冰点期"]
    mult = {"高潮期": 1.2, "回暖期": 1.05, "平稳期": 1.0,
            "混沌期": 0.9, "退潮中段": 0.6, "冰点期": 0.5}
    scores = {"高潮期": 95, "回暖期": 65, "平稳期": 55,
              "混沌期": 45, "退潮中段": 25, "冰点期": 15}
    fd, path = tempfile.mkstemp(prefix="shadow_smoke_", suffix=".jsonl")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for i, d in enumerate(dates):
            st_ = states[i % len(states)]
            m = mult[st_]
            rec = {
                "ts": f"{d}T14:30:00", "business_date": d,
                "state": st_, "score": scores[st_],
                "multiplier": {"p1": 1.0, "p2": m, "p3": m,
                               "p4": round(m + 0.05, 2), "p5": round(m + 0.05, 2)},
                "inputs": {}, "kept_n": {"p2": 2, "p3": 2, "p4": 3, "p5": 4},
                "shadow_only": True,
            }
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return path


def main() -> int:
    try:
        from streamlit.testing.v1 import AppTest
    except Exception as e:
        print(f"[SKIP] streamlit.testing 不可用: {e}")
        return 0

    import duckdb
    import tools.sentiment_shadow_eval as sse
    import tools._ro_conn as ro

    real_open_ro = ro.open_ro

    def fake_open_ro(db_path="", retries=6, delay=2.0):
        path = db_path or QA_DB
        return duckdb.connect(path, read_only=True)

    ro.open_ro = fake_open_ro
    tmp_jsonl = _make_shadow_jsonl(_qa_dates(8))
    fails = []

    try:
        # ---- 场景 1：有记录（完整渲染）----
        sse.SHADOW_JSONL = tmp_jsonl
        at = AppTest.from_file(PAGE, default_timeout=120)
        at.run()
        if at.exception:
            fails.append(f"场景1 exception: {[str(e.value) for e in at.exception]}")
        n_metrics = len(at.metric)
        n_frames = len(at.dataframe)
        print(f"[场景1] 有记录渲染：metrics={n_metrics} tables={n_frames} "
              f"exception={len(at.exception)}")
        if n_metrics < 4:
            fails.append(f"场景1 metrics 数异常: {n_metrics}")
        try:
            import streamlit as st

            st.cache_data.clear()
        except Exception:
            pass

        # ---- 场景 2：无记录（降级提示，不得崩）----
        sse.SHADOW_JSONL = os.path.join(tempfile.gettempdir(), "no_such_shadow.jsonl")
        at2 = AppTest.from_file(PAGE, default_timeout=120)
        at2.run()
        if at2.exception:
            fails.append(f"场景2 exception: {[str(e.value) for e in at2.exception]}")
        n_warn = len(at2.warning)
        print(f"[场景2] 无记录降级：warning={n_warn} exception={len(at2.exception)}")
        if n_warn < 1:
            fails.append("场景2 应给出 warning 提示")
    finally:
        sse.SHADOW_JSONL = os.path.join(
            ROOT, "data", "runtime", "observability", "sentiment_shadow.jsonl")
        ro.open_ro = real_open_ro
        try:
            os.remove(tmp_jsonl)
        except Exception:
            pass

    if fails:
        print("\n[FAIL]")
        for x in fails:
            print("  -", x)
        return 1
    print("\n[PASS] 情绪温度计看板 AppTest 冒烟通过（2/2 场景）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
