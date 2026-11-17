# -*- coding: utf-8 -*-
"""ui/data_health_badge.py — 顶部「数据健康度」徽章（Tier 1 · 1.5）

一行回答「今天这份数据能不能信」：
    🟢 数据可用 / 🟡 需关注 / 🔴 不可信

纯只读、纯展示。任何异常都降级成一句黄条，绝不阻塞主界面。
数据来源：tools/system_health.data_health_snapshot()
"""
from __future__ import annotations

import logging
from typing import Any, Dict

import streamlit as st

logger = logging.getLogger(__name__)

_CACHE_TTL = 600  # 10 分钟


@st.cache_data(ttl=_CACHE_TTL, show_spinner=False)
def _snapshot_cached(_salt: float) -> Dict[str, Any]:
    try:
        from tools.system_health import data_health_snapshot

        return data_health_snapshot() or {}
    except Exception as e:                                   # pragma: no cover
        logger.warning("[data_health_badge] 取快照失败: %s", e)
        return {"status": "WARN", "reasons": [f"体检脚本异常: {type(e).__name__}"]}


def render_data_health_badge() -> None:
    try:
        import time

        snap = _snapshot_cached(round(time.time() / _CACHE_TTL))
        status = str(snap.get("status") or "WARN")
        icon = {"OK": "🟢", "WARN": "🟡", "FAIL": "🔴"}.get(status, "🟡")
        word = {"OK": "数据可信", "WARN": "需关注", "FAIL": "数据不可信"}.get(status, "需关注")

        parts = []
        if snap.get("latest_trade_date"):
            lag = snap.get("lag_days")
            parts.append(f"最新交易日 {snap['latest_trade_date']}（T-{lag}）" if lag is not None
                         else f"最新交易日 {snap['latest_trade_date']}")
        # 【2026-11-03 修复】原显示「覆盖 1380/5917 = 23%」会被误读成缺数据；
        # 日线库按设计只跟踪市值 >= 120 亿的票，健康度应看「相对上一交易日有没有骤降」。
        if snap.get("covered_n"):
            _txt = f"跟踪域 {snap.get('covered_n', 0)} 只"
            if snap.get("tracked_prev_n"):
                _txt += f"（较上一交易日 {snap.get('breadth_pct', 0):.0f}%）"
            parts.append(_txt)
        if snap.get("daemon_last_log"):
            parts.append(f"daemon 最后日志 {snap['daemon_last_log']}（{snap.get('daemon_lag_hours')}h 前）")
        sc = snap.get("signal_pool_counts") or {}
        if sc:
            parts.append("信号样本 " + " ".join(f"{k}={v}" for k, v in sorted(sc.items())))
        if snap.get("calendar_max"):
            parts.append(f"交易日历至 {snap['calendar_max']}")

        head = f"{icon} **{word}**"
        if status == "OK":
            st.success(head + " ｜ " + " ｜ ".join(parts), icon=None)
        elif status == "WARN":
            st.warning(head + " ｜ " + " ｜ ".join(parts), icon=None)
        else:
            st.error(head + " ｜ " + " ｜ ".join(parts), icon=None)

        # 全市场口径单独说明，避免把「设计上不跟踪小市值」当成「数据缺失」
        try:
            if (snap.get("coverage_pct") and snap.get("coverage_pct") < 60
                    and (snap.get("breadth_pct") or 0) >= 85):
                st.caption(
                    f"　· 全市场口径 {snap.get('covered_n', 0)}/{snap.get('in_market_n', 0)}"
                    f" = {snap.get('coverage_pct', 0):.0f}%：日线库按设计只跟踪"
                    f"总市值/流通市值 ≥ {int(snap.get('mv_gate_yi') or 120)} 亿的票，非缺数据"
                )
        except Exception:
            pass
        reasons = [r for r in (snap.get("reasons") or []) if r]
        if reasons:
            st.caption("　· " + "\n　· ".join(reasons))
        if snap.get("empty_key_tables"):
            st.caption("　· 空表：" + "、".join(snap["empty_key_tables"]))
    except Exception as e:                                   # pragma: no cover
        logger.warning("[data_health_badge] 渲染失败（已降级）: %s", e)
        try:
            st.caption("🟡 数据健康度徽章暂不可用（不影响选股）")
        except Exception:
            pass
