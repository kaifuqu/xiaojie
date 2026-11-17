# -*- coding: utf-8 -*-
"""
ui/pages/2_题材热度.py — 题材热度榜（Streamlit 原生 multipage · 只读）

把长期「只写不读」的 fact_limit_theme（KPL 涨停题材归类）盘活成只读看板：
  · 最近 N 个交易日题材涨停家数排行
  · 当日代表个股（连板状态）
  · 昨日涨停 → 今日晋级率
  · 每日涨停家数趋势

数据来源：tools/theme_heat.py::theme_heat_snapshot()（与 CLI 同一数据口径）。
红线：只读 —— 不写库、不参与选股、不改任何策略阈值。
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

st.set_page_config(page_title="题材热度榜 · 小杰AI选股系统", page_icon="🔥", layout="wide")

# 遵守「UI 与 daemon 不能同时跑」的单写者铁律（与 1_模拟盘 页同一套 sentinel）。
_daemon_alive = False
try:
    from core.process_orchestrator import DAEMON_LOCK_FILE, check_peer_active

    _daemon_alive = bool(check_peer_active(DAEMON_LOCK_FILE)[0])
except Exception:
    _daemon_alive = False

st.markdown("## 🔥 题材热度榜（只读）")
st.caption(
    "数据源：fact_limit_theme（KPL 涨停原因归类）。"
    "只展示「涨停家数 / 晋级率 / 代表个股」，不参与选股、不改任何阈值。"
)
st.divider()

if _daemon_alive:
    st.error(
        "⛔ **检测到守护进程正在运行**（DuckDB 单写者模式）。"
        "为避免与 daemon 争抢写锁，本页暂停读取；请关闭 daemon 窗口后再看，"
        "或过几分钟重试（daemon 任务间隙不占锁）。"
    )
    st.stop()

days = st.select_slider("统计窗口（交易日）", options=[5, 10, 15, 20, 30], value=10)


@st.cache_data(ttl=600, show_spinner=False)
def _snap(_days: int) -> dict:
    from tools.theme_heat import theme_heat_snapshot

    return theme_heat_snapshot(days=_days, top_n=20, retries=3, delay=1.5)


snap = _snap(int(days))
if not snap.get("ok"):
    st.warning(
        "数据暂不可用（可能 daemon 正在占用数据库，稍后会自动恢复）。"
        f"\n\n详情：{snap.get('error', '')}"
    )
    st.stop()

_pr = "—" if snap.get("promote_pct") is None else f"{snap['promote_pct']}%"
c1, c2, c3, c4 = st.columns(4)
c1.metric("数据截至", snap.get("as_of") or "—")
c2.metric("当日涨停", f"{snap.get('n_latest', 0)} 家")
c3.metric("昨日→今日晋级率", _pr)
c4.metric("统计窗口", f"{len(snap.get('dates', []))} 个交易日")

try:
    import pandas as pd

    # ---- 每日涨停家数趋势 ----
    dc = snap.get("day_counts") or {}
    if dc:
        df_day = pd.DataFrame(
            [{"日期": k, "涨停家数": v} for k, v in sorted(dc.items())])
        st.markdown("#### 每日涨停家数")
        st.bar_chart(df_day.set_index("日期"), height=180)

    # ---- 题材排行 ----
    themes = snap.get("themes") or []
    if themes:
        st.markdown(f"#### 题材排行（近 {len(snap.get('dates', []))} 个交易日）")
        rows = []
        for i, t in enumerate(themes, 1):
            rep = "、".join(
                f"{s['ts_code'].split('.')[0]}({s['status'] or '涨停'})"
                for s in t.get("today_stocks", [])[:5]) or "—"
            rows.append({
                "#": i,
                "题材": t["theme"],
                "当日家数": t["today_n"],
                "窗口累计": t["total_n"],
                "活跃天数": t["active_days"],
                "最近上榜": t["last_date"],
                "今日代表个股": rep,
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

        # ---- 窗口累计 Top10 柱状图 ----
        df_top = pd.DataFrame(
            [{"题材": t["theme"], "窗口累计涨停": t["total_n"]} for t in themes[:10]])
        st.markdown("#### 累计热度 Top 10")
        st.bar_chart(df_top.set_index("题材"), height=220)
    else:
        st.info("窗口内没有题材数据。")

    with st.expander("口径说明"):
        st.markdown(
            "- 题材来自 KPL 涨停原因文本归类（`fact_limit_theme`），一股可归多个题材；\n"
            "- **晋级率** = 上一交易日涨停股中，今日继续涨停的比例；\n"
            "- 本页仅供观察「题材轮动/情绪温度」，**不构成任何选股依据**；\n"
            "- 数据缓存 10 分钟。"
        )
except Exception as _err:  # noqa: BLE001
    st.error(f"⚠️ 页面渲染失败：{type(_err).__name__}: {_err}")
