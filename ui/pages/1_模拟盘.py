# -*- coding: utf-8 -*-
"""
ui/pages/1_模拟盘.py — 模拟盘独立一级页面（Streamlit 原生 multipage）

【为什么单独开页】
  主页面 ui/app.py 是五池大屏 + 后勤工具箱，3000+ 行、每次 rerun 都重。
  持仓表需要「随时可查 + 独立刷新」，塞在页面底部 expander 里既不好找，也拖慢主页面。
  Streamlit 1.65 原生 multipage 只认 ui/pages/ 目录，因此这里做零耦合挂载：
  页面本身不 import 主 app 的任何逻辑，只调 render_paper_trading_panel()。

【数据来源】
  只读 core/paper_trading/reporting.build_snapshot()；不写库、不下单、不触发回填。
"""
from __future__ import annotations

import logging
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

st.set_page_config(page_title="模拟盘持仓 · 小杰AI选股系统", page_icon="📈", layout="wide")

# 【V29.3】模拟盘页面只读 DuckDB，但仍遵守「UI 与 daemon 不能同时跑」的单写者铁律：
# 复用 ui/app.py 的同一套 sentinel。若 daemon 正在运行，这里直接给出中止提示，
# 避免用户在看板时把 daemon 的 DuckDB 写连接顶掉（表现为 daemon 扫描落库失败）。
_daemon_alive = False
try:
    from core.process_orchestrator import (
        DAEMON_LOCK_FILE,
        check_peer_active,
    )

    _daemon_alive = bool(check_peer_active(DAEMON_LOCK_FILE)[0])
except Exception:
    _daemon_alive = False

st.markdown("## 📈 模拟盘持仓（P3 / P4 信号自动买卖 · 只读）")
st.caption(
    "买入：P3 / P4 池正式信号，按信号现价成交；"
    "卖出：现价 ≤ 买入价 × 0.97 止损清仓，或现价 ≥ 买入价 × 1.15 止盈清仓；"
    "强制 T+1；实验满 100 个交易日后停止新开仓，已有持仓继续按规则退出。"
)
st.divider()

if _daemon_alive:
    st.error(
        "⛔ **检测到守护进程正在运行**（DuckDB 单写者模式）。"
        "此时打开看板会与 daemon 争抢 DuckDB 写锁，可能导致扫描信号无法落库。"
        "请先关闭 `start_daemon_24x7.bat` 窗口（Ctrl+C），或只通过 daemon 日志观察模拟盘。"
    )
    st.stop()

try:
    from ui.paper_trading_panel import render_paper_trading_panel

    render_paper_trading_panel()
except Exception as _err:  # noqa: BLE001
    logging.getLogger(__name__).error("模拟盘页面挂载异常: %s", _err, exc_info=True)
    st.error("⚠️ 模拟盘页面加载失败，详情见日志。")
