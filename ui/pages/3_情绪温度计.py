# -*- coding: utf-8 -*-
"""
ui/pages/3_情绪温度计.py — 情绪温度计 Shadow 看板（Streamlit 原生 multipage · 只读）

把 H1 Shadow 记录（data/runtime/observability/sentiment_shadow.jsonl）和
T+1 信号质量接起来做只读展示：
  · 情绪状态时间线（分数走势）
  · 分状态：当日信号数 / 胜率 / 均收益 / 均超额
  · 吻合度三口径（Pearson / 看多 vs 看空质量差 / 命中率）
  · 样本 < 门槛时明确标注「仅预览，不作为接线依据」

数据来源：tools/sentiment_shadow_eval.py::shadow_quality_snapshot()
         （与 CLI 同一数据口径）。
红线：只读 —— 情绪侧只读 JSONL、信号侧 DuckDB read_only；
      不写库、不参与选股、不改任何阈值。接不接线仍由人拍板。
"""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# 与 ui/app.py 保持一致：import streamlit / pandas 之前先关 pyarrow（Win11 SAC 兼容 shim）。
try:
    from core import _pyarrow_compat as _pyarrow_compat

    _pyarrow_compat.disable_pyarrow()
except Exception:  # noqa: BLE001
    pass

import streamlit as st

st.set_page_config(page_title="情绪温度计 · 小杰AI选股系统", page_icon="🌡️", layout="wide")

st.markdown("## 🌡️ 情绪温度计（Shadow 只读）")
st.caption(
    "H1 Shadow：每次扫描记录「若把情绪系数接进 pass_line 会是什么状态」——"
    "**只记录，不拦截**。攒满 ≥20 个交易日后，用「系数方向 vs 后续信号质量」的吻合度"
    "决定是否接线。"
)
st.divider()

# daemon 在跑时库可能被独占：本页情绪侧仍可看，信号质量侧会自动降级（不是错误）。
_daemon_alive = False
try:
    from core.process_orchestrator import DAEMON_LOCK_FILE, check_peer_active

    _daemon_alive = bool(check_peer_active(DAEMON_LOCK_FILE)[0])
except Exception:
    _daemon_alive = False

if _daemon_alive:
    st.info(
        "ℹ️ 检测到守护进程正在运行：情绪时间线照常展示；"
        "「信号质量 / 吻合度」需要读库，若被占锁会自动跳过（daemon 停机后可完整评估）。"
    )

min_days = st.select_slider("接线评估门槛（交易日）", options=[10, 20, 30], value=20)


@st.cache_data(ttl=600, show_spinner=False)
def _snap(_min_days: int) -> dict:
    from tools.sentiment_shadow_eval import shadow_quality_snapshot

    return shadow_quality_snapshot(min_days=_min_days, retries=3, delay=1.5)


snap = _snap(int(min_days))
if not snap.get("ok"):
    st.warning(
        "数据暂不可用（可能 daemon 正在占用数据库，或 shadow 记录尚未生成）。"
        f"\n\n详情：{snap.get('error', '')}"
    )
    st.stop()

records = snap.get("records") or []
latest = records[-1] if records else {}
_mult = latest.get("multiplier") or {}

c1, c2, c3, c4 = st.columns(4)
c1.metric("已记录交易日", f"{snap.get('n_days', 0)} 天")
c2.metric("接线评估门槛", ("已达标 ✔" if snap.get("ready")
                           else f"还差 {max(0, int(min_days) - int(snap.get('n_days', 0)))} 天"))
c3.metric("最新情绪状态", f"{latest.get('state', '—')}（{latest.get('score', '—')} 分）")
c4.metric("可配对 T+1 信号日", f"{snap.get('n_matched_days', 0)} 天")

if not snap.get("ready"):
    st.warning(
        f"⚠️ 样本不足：{snap.get('n_days', 0)} / {min_days} 个交易日 —— "
        "以下仅为预览，**不作为接线依据**（Shadow 纪律）。"
    )
if snap.get("db_error"):
    st.info(f"读库受限：{snap['db_error']}")

try:
    import pandas as pd

    # ---- 情绪分数时间线 ----
    if records:
        st.markdown("#### 情绪状态时间线")
        df_line = pd.DataFrame(
            [{"日期": r["date"], "情绪分数": r["score"], "状态": r["state"]} for r in records])
        st.line_chart(df_line.set_index("日期")[["情绪分数"]], height=200)

        df_mult = pd.DataFrame(
            [{"日期": r["date"],
              "P2系数": float((r.get("multiplier") or {}).get("p2", 1.0) or 1.0),
              "P3系数": float((r.get("multiplier") or {}).get("p3", 1.0) or 1.0)}
             for r in records])
        st.markdown("#### P2 / P3 pass_line 系数（若接线的折算）")
        st.line_chart(df_mult.set_index("日期"), height=180)

    # ---- 分状态统计 ----
    stats = snap.get("state_stats") or []
    if stats:
        st.markdown("#### 分状态：情绪状态 × 当日信号 T+1 质量")
        rows = [{
            "情绪状态": s["state"],
            "天数": s["days"],
            "平均分数": s["avg_score"],
            "P2系数": s["mult_p2"],
            "信号数": s["n_sig"],
            "胜率": "—" if s["win_rate"] is None else f"{s['win_rate'] * 100:.1f}%",
            "均收益%": s["avg_ret"],
            "均超额%": s["avg_excess"],
        } for s in stats]
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    # ---- 吻合度 ----
    cons = snap.get("consistency") or {}
    if cons.get("n_pairs"):
        st.markdown("#### 吻合度：系数方向 vs 后续信号质量")
        k1, k2, k3 = st.columns(3)
        r = cons.get("pearson_score_vs_quality")
        k1.metric("Pearson r（分数×质量）", "—" if r is None else f"{r:.3f}")
        hr = cons.get("hit_rate")
        k2.metric("命中率（0.5=抛硬币）", "—" if hr is None else f"{hr:.3f}")
        dok = cons.get("direction_ok")
        k3.metric("方向判定", "—" if dok is None else ("吻合" if dok else "不吻合"))
        st.caption(
            f"看多日 {cons.get('n_bull_days', 0)} 天 均质量 "
            f"{'—' if cons.get('bull_quality') is None else round(cons['bull_quality'], 3)} ｜ "
            f"看空日 {cons.get('n_bear_days', 0)} 天 均质量 "
            f"{'—' if cons.get('bear_quality') is None else round(cons['bear_quality'], 3)}"
        )
        per_pool = cons.get("per_pool") or {}
        pool_rows = []
        for pool, label in (("p2", "P2"), ("p3", "P3"), ("p4", "P4"), ("p5", "P5")):
            pp = per_pool.get(pool) or {}
            pool_rows.append({
                "池": label,
                "看多日": pp.get("bull_n", 0),
                "看多均质量": pp.get("bull_quality"),
                "看空日": pp.get("bear_n", 0),
                "看空均质量": pp.get("bear_quality"),
            })
        st.dataframe(pd.DataFrame(pool_rows), use_container_width=True, hide_index=True)
    else:
        st.info("暂无可配对的 T+1 信号（signal_pnl 尚未回填这些日期，或读库被占锁）。")

    with st.expander("口径说明"):
        st.markdown(
            "- 情绪侧来自 `data/runtime/observability/sentiment_shadow.jsonl`（每次扫描一行，"
            "同一业务日取最后一条；自动读 `.1` 轮转档）；\n"
            "- 信号质量 = `signal_log ⋈ signal_pnl`（T+1, n_days=1）：胜率看 `win`，"
            "收益看 `return_pct`，超额看 `excess_blend_pct`（缺失回退 `excess_pct`）；\n"
            "- 吻合度三口径：逐日 Pearson r、看多日（均系数>1）vs 看空日（<1）质量差、"
            "命中率（看多日质量≥中位 / 看空日≤中位）；\n"
            "- **样本 < 门槛（默认 20 个交易日）时仅预览，不作为接线依据**；\n"
            "- 本页只读展示，**不构成任何选股依据**；数据缓存 10 分钟。"
        )
except Exception as _err:  # noqa: BLE001
    st.error(f"⚠️ 页面渲染失败：{type(_err).__name__}: {_err}")
