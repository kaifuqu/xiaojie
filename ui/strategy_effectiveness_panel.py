# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V29.2 — Streamlit 战法胜率面板（纯只读）
================================================================================
【设计约束】
  · 唯一数据源 = core.strategy_effectiveness_dashboard.load_snapshot()（读 JSON 快照）
  · 不连 DuckDB、不写库、不触发回填、不改实盘权重、不自动应用
  · 面板内任何异常都不得冒泡到五池扫描主流程（全部 try/except 包裹）
  · 口径冻结：只展示 primary_n_days 主口径；禁止跨 N 档相加、禁止读 t5_win_rate
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import altair as alt
import pandas as pd
import streamlit as st

from core.strategy_effectiveness_dashboard import (
    history_for_strategy,
    load_snapshot,
)
from ui.display_labels import pool_cn

logger = logging.getLogger(__name__)

_POOL_ORDER = ["p1", "p2", "p3", "p4", "p5"]
_TIER_COLORS = {
    "样本充足": "#16a34a",
    "样本中等": "#0891b2",
    "样本偏少": "#d97706",
    "零样本": "#dc2626",
}
_REASON_COLORS = {
    "高胜率": "#16a34a",
    "低胜率": "#dc2626",
    "极低胜率-待审核": "#b91c1c",
    "样本不足-保持默认": "#64748b",
}


@st.cache_data(ttl=30, show_spinner=False)
def _cached_snapshot(_salt: float) -> Optional[Dict[str, Any]]:
    """缓存读盘 30 秒。_salt 只用于手动刷新时改变缓存键。"""
    return load_snapshot()


def _f(v: Any, digits: int = 2, suffix: str = "") -> str:
    if v is None:
        return "—"
    try:
        return ("%." + str(digits) + "f") % float(v) + suffix
    except (TypeError, ValueError):
        return "—"


def _pct(v: Any, digits: int = 1) -> str:
    if v is None:
        return "—"
    try:
        return ("%." + str(digits) + "f") % (float(v) * 100.0) + "%"
    except (TypeError, ValueError):
        return "—"


def render_empty_state(reason: str = "") -> None:
    """无快照 / 无战法数据时的空态。"""
    st.info(
        "📭 **暂无战法胜率数据**\n\n"
        "本面板数据由 daemon 每日 18:30 的「信号 T+N 回填」任务生成。\n\n"
        "- 首次使用：等待当日 18:30 任务跑完，或手动执行 "
        "`python -c \"from core.cli_view_recommendation import _load; _load()\"` 生成报表\n"
        "- 历史信号不足时，T+5 / T+10 档位会显示为「缺失」，属正常现象\n\n"
        "%s" % (reason or "")
    )


def render_maturity_cards(snap: Dict[str, Any]) -> None:
    """① 顶部数据成熟度条。"""
    m = snap.get("maturity") or {}
    primary = m.get("primary_n_days")
    avail = m.get("available_n_days") or []
    missing = m.get("missing_n_days") or []
    total_eval = m.get("total_evaluated") or snap["summary"].get("total_n_evaluated", 0)
    remain = m.get("next_auto_apply_remaining")
    min_apply = m.get("min_n_for_auto_apply") or 200

    c = st.columns(5)
    c[0].markdown(
        '<div style="border-radius:8px;padding:10px;text-align:center;background:#334155;color:#fff;">'
        '<div style="font-size:.85em;opacity:.9;">主口径</div>'
        '<div style="font-size:1.35em;font-weight:700;">T+%s</div></div>'
        % ("—" if primary is None else primary),
        unsafe_allow_html=True,
    )
    c[1].markdown(
        '<div style="border-radius:8px;padding:10px;text-align:center;background:#16a34a;color:#fff;">'
        '<div style="font-size:.85em;opacity:.9;">已成熟档位</div>'
        '<div style="font-size:1.15em;font-weight:700;">%s</div></div>'
        % ("、".join("T+%d" % int(x) for x in avail) or "—"),
        unsafe_allow_html=True,
    )
    c[2].markdown(
        '<div style="border-radius:8px;padding:10px;text-align:center;background:#d97706;color:#fff;">'
        '<div style="font-size:.85em;opacity:.9;">缺失档位</div>'
        '<div style="font-size:1.15em;font-weight:700;">%s</div></div>'
        % ("、".join("T+%d" % int(x) for x in missing) or "—"),
        unsafe_allow_html=True,
    )
    c[3].markdown(
        '<div style="border-radius:8px;padding:10px;text-align:center;background:#0891b2;color:#fff;">'
        '<div style="font-size:.85em;opacity:.9;">已评估信号</div>'
        '<div style="font-size:1.35em;font-weight:700;">%d</div></div>' % int(total_eval or 0),
        unsafe_allow_html=True,
    )
    c[4].markdown(
        '<div style="border-radius:8px;padding:10px;text-align:center;background:#64748b;color:#fff;">'
        '<div style="font-size:.85em;opacity:.9;">距自动应用</div>'
        '<div style="font-size:1.35em;font-weight:700;">%d</div></div>'
        % (0 if remain is None else int(remain)),
        unsafe_allow_html=True,
    )
    st.caption(
        "自动应用门槛 %d 个已评估样本 · 当前模式 `%s`（面板只读，绝不自动改权重）"
        % (int(min_apply), snap.get("mode", "statistics_only"))
    )


def render_freshness_banner(snap: Dict[str, Any]) -> None:
    """快照新鲜度提示（>24h 黄、>72h 红）。"""
    level = snap.get("freshness_level", "unknown")
    msg = snap.get("freshness_message", "")
    if level == "warn":
        st.warning(msg, icon="⚠️")
    elif level == "bad":
        st.error(msg, icon="🔴")
    elif level == "unknown":
        st.caption("ℹ️ " + msg)
    else:
        st.caption("🟢 " + msg)


def render_summary_metrics(snap: Dict[str, Any]) -> None:
    """② 核心指标卡（全部按 n_evaluated 加权）。"""
    s = snap["summary"]
    c = st.columns(5)
    c[0].markdown(_card_html("战法数", str(s.get("n_strategies", 0)), "#334155"), unsafe_allow_html=True)
    c[1].markdown(
        _card_html("加权平均胜率", _pct(s.get("avg_win_rate")), "#0891b2"), unsafe_allow_html=True
    )
    c[2].markdown(
        _card_html("加权平均收益", _f(s.get("avg_return"), 2, "%"), "#dc2626"
                   if (s.get("avg_return") or 0) < 0 else "#16a34a"),
        unsafe_allow_html=True,
    )
    c[3].markdown(_card_html("升权候选", str(s.get("n_promote", 0)), "#16a34a"), unsafe_allow_html=True)
    c[4].markdown(_card_html("降权候选", str(s.get("n_demote", 0)), "#dc2626"), unsafe_allow_html=True)


def _card_html(title: str, value: str, color: str = "#1e293b") -> str:
    return (
        '<div style="border-radius:8px;padding:10px 12px;color:#fff;text-align:center;'
        'box-shadow:0 4px 6px -1px rgba(0,0,0,.1);min-height:76px;'
        'display:flex;flex-direction:column;justify-content:center;background:%s;">'
        '<div style="font-size:.85em;opacity:.9;font-weight:600;">%s</div>'
        '<div style="font-size:1.35em;font-weight:700;margin-top:4px;">%s</div></div>'
        % (color, title, value)
    )


def _rows_to_frame(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(
        [
            {
                "战法ID": r.get("strategy", ""),
                "战法名称": r.get("strategy_label", "") or "—",
                "档位": "、".join(pool_cn(p) for p in (r.get("pools") or [])) or "—",
                "主口径": ("T+%d" % r["primary_n_days"]) if r.get("primary_n_days") else "—",
                "信号数": r.get("n_signals", 0),
                "已评估": r.get("n_evaluated", 0),
                "胜率": r.get("win_rate"),
                "平均收益%": r.get("avg_return"),
                "平均超额%": r.get("avg_excess"),
                "回撤%": r.get("avg_max_drawdown"),
                "浮盈%": r.get("avg_max_profit"),
                "推荐权重": r.get("_weight", "—"),
                "推荐理由": r.get("_reason", "—"),
                "成熟度": r.get("sample_tier", "—"),
                "健康度": r.get("health_label", "—"),
                "健康度说明": r.get("health_reason", "") or "—",
                # ---- Tier2.5 净口径（扣费后，与旧口径并列展示）----
                "净收益%": r.get("avg_net_return"),
                "净超额%（混合）": r.get("avg_net_excess_blend"),
                "净胜率": r.get("net_win_rate"),
            }
            for r in rows
        ]
    )
    return df


def _attach_rec(rows: List[Dict[str, Any]], rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把推荐权重/理由挂到行上，供表格与详情使用。"""
    out = []
    for r in rows:
        item = dict(r)
        d = rec.get(r.get("strategy", ""))
        d = d if isinstance(d, dict) else {}
        item["_weight"] = _f(d.get("weight"), 2)
        item["_reason"] = str(d.get("reason") or "—")
        out.append(item)
    return out


def render_filters(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """③ 筛选器。"""
    pools_present = sorted({p for r in rows for p in (r.get("pools") or [])})
    ordered = [p for p in _POOL_ORDER if p in pools_present] + [
        p for p in pools_present if p not in _POOL_ORDER
    ]
    options = ["全部"] + [pool_cn(p) for p in ordered]

    c1, c2, c3 = st.columns([1.2, 1.2, 1.6])
    with c1:
        pool_sel = st.selectbox("档位", options, index=0, key="se_panel_pool")
    with c2:
        min_n = st.number_input(
            "最小已评估样本", min_value=0, max_value=999, value=0, step=1, key="se_panel_min_n"
        )
    with c3:
        kw = st.text_input("战法搜索（ID 或名称）", value="", key="se_panel_kw", placeholder="如 P5-09 / 均线")

    c4, c5 = st.columns(2)
    with c4:
        view_sel = st.selectbox(
            "只看",
            ["全部", "升权候选", "升权/降权候选", "样本不足", "净负向"],
            index=0,
            key="se_panel_view",
        )
    with c5:
        st.caption(
            "ℹ️ 主口径与成熟度由 daemon 每日 18:30 汇总；面板不重算、不加权改权。\n\n"
            "⚠️ 样本不足时胜率波动大，本面板只描述样本厚度，不给「有效/无效」结论。"
        )

    out = list(rows)
    if pool_sel != "全部":
        target = next((p for p in ordered if pool_cn(p) == pool_sel), "")
        out = [r for r in out if target in (r.get("pools") or [])]
    if min_n:
        out = [r for r in out if r.get("n_evaluated", 0) >= int(min_n)]
    if kw.strip():
        k = kw.strip().lower()
        out = [
            r for r in out
            if k in str(r.get("strategy", "")).lower()
            or k in str(r.get("strategy_label", "")).lower()
        ]
    if view_sel != "全部":
        if view_sel == "升权候选":
            out = [r for r in out if _weight_float(r) > 1.0]
        elif view_sel == "升权/降权候选":
            out = [r for r in out if _weight_float(r) != 1.0]
        elif view_sel == "样本不足":
            out = [r for r in out if r.get("sample_tier") in ("样本偏少", "零样本")]
        elif view_sel == "净负向":
            # 净口径为负：扣费后平均收益为负，或净超额（混合基准）为负。
            # 缺净数据的战法不计入（None ≠ 负向）。
            def _net_negative(r: Dict[str, Any]) -> bool:
                _nr = r.get("avg_net_return")
                _ne = r.get("avg_net_excess_blend")
                vals = [v for v in (_nr, _ne) if v is not None]
                if not vals:
                    return False
                return any(float(v) < 0 for v in vals)

            out = [r for r in out if _net_negative(r)]
    return out


def _weight_float(r: Dict[str, Any]) -> float:
    try:
        return float(r.get("_weight"))
    except (TypeError, ValueError):
        return 1.0


def render_effectiveness_table(rows: List[Dict[str, Any]]) -> None:
    """④ 主表（15 列，可排序/隐藏列/导出）。"""
    if not rows:
        st.warning("当前筛选条件下没有战法数据。")
        return
    df = _rows_to_frame(rows)
    fmt = {
        "胜率": "%.1f%%",
        "平均收益%": "%.2f",
        "平均超额%": "%.2f",
        "回撤%": "%.2f",
        "浮盈%": "%.2f",
        "净收益%": "%.2f",
        "净超额%（混合）": "%.2f",
        "净胜率": "%.1f%%",
    }
    with st.expander("📋 战法表现明细表", expanded=True):
        try:
            view = df.style.format(fmt, na_rep="—")
        except Exception:
            view = df
        st.dataframe(view, width="stretch", hide_index=True)
        st.download_button(
            "⬇️ 导出当前筛选结果 CSV",
            data=df.to_csv(index=False).encode("utf-8-sig"),
            file_name="strategy_effectiveness.csv",
            mime="text/csv",
            key="se_panel_dl",
        )


# ---------------------------------------------------------------- V30 新增
_BLEND_X_OPTIONS = {
    "混合超额%（主口径）": "avg_excess_blend",
    "平均收益%（绝对）": "avg_return",
    "上证超额%": "avg_excess_sh",
    "深证超额%": "avg_excess_sz",
    "创业板超额%": "avg_excess_cyb",
}


def render_market_quotes(snap: Dict[str, Any]) -> None:
    """
    【V30 新增】市场水位条：上证 / 深证 / 创业板 / 沪深300 的最新涨跌。

    设计原则：
      · 只展示指数自身涨跌（市场水位），**不参与任何选股或打分决策**
      · 指数数据未就绪时给明确空态 + 补齐方式，而不是显示 0
    """
    quotes = snap.get("index_quotes") or []
    cov = snap.get("index_coverage") or {}
    if not quotes:
        with st.expander("🌏 市场水位（上证 / 深证 / 创业板 / 沪深300）", expanded=False):
            st.info(
                "📭 **基准指数数据尚未就绪**\n\n"
                "「平均超额%」需要 4 个基准指数做参照：上证指数、深证成指、创业板指、沪深300。\n\n"
                "补齐方式（任选其一）：\n"
                "- 等 daemon 晚间 **19:45** 日线增量链自动同步（推荐，无需干预）\n"
                "- 手动执行：`python -c \"from data.index_daily_store import sync_index_quietly; "
                "sync_index_quietly()\"`\n\n"
                "补齐后，下一次 **18:30** 回填会把超额收益写回 signal_pnl，"
                "本面板「平均超额%」列与水位条即会有值。"
            )
        return

    cols = st.columns(len(quotes))
    for col, q in zip(cols, quotes):
        pct = q.get("pct_chg")
        if pct is None:
            color = "#64748b"
        elif float(pct) >= 0:
            color = "#dc2626"
        else:
            color = "#16a34a"
        col.markdown(
            (
                '<div style="border-radius:8px;padding:10px 12px;color:#fff;text-align:center;'
                'box-shadow:0 4px 6px -1px rgba(0,0,0,.1);min-height:96px;'
                'display:flex;flex-direction:column;justify-content:center;background:%s;">'
                '<div style="font-size:.9em;font-weight:600;opacity:.95;">%s</div>'
                '<div style="font-size:1.3em;font-weight:700;margin-top:2px;">%s</div>'
                '<div style="font-size:.8em;opacity:.9;margin-top:2px;">5日 %s | 20日 %s</div>'
                "</div>"
            )
            % (
                color,
                q.get("name") or q.get("ts_code") or "-",
                _f(q.get("close"), 2),
                _f(q.get("chg_5d"), 2, "%"),
                _f(q.get("chg_20d"), 2, "%"),
            ),
            unsafe_allow_html=True,
        )

    st.caption(
        "数据日 %s | 涨红跌绿（A 股惯例） | **混合超额 = 战法收益 - (上证+深证+创业板)/3 等权基准**"
        " | 指数仅作统计参照，不参与选股与打分"
        % (quotes[0].get("trade_date") or "-")
    )

    per = cov.get("per_index") or {}
    if per:
        lack = [k for k, v in per.items() if int((v or {}).get("n", 0)) < 2]
        if lack:
            st.caption("注意：以下指数数据不足（<2 个交易日），相关超额列显示「-」：%s" % "、".join(lack))


def render_blend_table(rows: List[Dict[str, Any]]) -> None:
    """【V30 新增】混合基准归因表：4 列超额 + 跑赢占比。"""
    if not rows:
        return
    items = [
        {
            "战法ID": r.get("strategy", ""),
            "战法名称": r.get("strategy_label", "") or "-",
            "档位": "、".join(pool_cn(p) for p in (r.get("pools") or [])) or "-",
            "已评估": r.get("n_evaluated", 0),
            "上证超额%": r.get("avg_excess_sh"),
            "深证超额%": r.get("avg_excess_sz"),
            "创业板超额%": r.get("avg_excess_cyb"),
            "混合超额%": r.get("avg_excess_blend"),
            "跑赢混合基准%": r.get("beat_blend_rate"),
        }
        for r in rows
    ]
    df = pd.DataFrame(items)
    fmt = {
        "上证超额%": "%.2f",
        "深证超额%": "%.2f",
        "创业板超额%": "%.2f",
        "混合超额%": "%.2f",
        "跑赢混合基准%": "%.1f",
    }
    with st.expander("🌏 混合基准归因（上证 / 深证 / 创业板 / 等权混合）", expanded=False):
        cols = ["上证超额%", "深证超额%", "创业板超额%", "混合超额%"]
        if df[cols].isna().all().all():
            st.info(
                "混合超额需要基准指数数据支撑。当前快照 4 个指数均未就绪，"
                "因此超额列全部为「-」（NULL 语义，不是 0）。补齐指数后，"
                "等待下一次 18:30 回填即可看到数值。"
            )
            return
        try:
            view = df.style.format(fmt, na_rep="-")
        except Exception:
            view = df
        st.dataframe(view, width="stretch", hide_index=True)
        st.caption(
            "口径：超额 = 战法 T+N 收益 - 对应指数同期收益；混合 = 上证/深证/创业板等权。"
            "指数缺失时该列写 NULL（显示「-」），绝不用 0 冒充。"
        )
def render_scatter_chart(rows: List[Dict[str, Any]]) -> None:
    """
    ⑤ 散点图：Y=胜率，X 可切换（平均收益 / 混合超额 / 单指数超额），
    点大小 = 已评估样本量，颜色 = 档位。

    【V30】X 轴新增下拉：默认「混合超额%（主口径）」。
    若超额数据全为 NULL（指数未就绪），自动回退到「平均收益%（绝对）」
    并给出说明，避免出现一张空图。
    """
    if not rows:
        st.info("ℹ️ 当前筛选条件下没有可绘图的胜率/收益数据。")
        return

    x_options = list(_BLEND_X_OPTIONS.keys())
    default_idx = 0
    if rows and all(r.get("avg_excess_blend") is None for r in rows):
        # 指数未就绪 → 默认落在平均收益口径
        default_idx = x_options.index("平均收益%（绝对）") if "平均收益%（绝对）" in x_options else 0

    x_label = st.selectbox(
        "散点图 X 轴口径",
        x_options,
        index=default_idx,
        key="se_panel_scatter_x",
        help="混合超额 = 战法收益 − (上证+深证+创业板)/3 等权基准；指数未就绪时该项为 NULL",
    )
    x_field = _BLEND_X_OPTIONS.get(x_label, "avg_return")

    pts = []
    for r in rows:
        xv = r.get(x_field)
        if xv is None or r.get("win_rate") is None:
            continue
        pts.append({
            "战法": "%s %s" % (r.get("strategy", ""), r.get("strategy_label", "")),
            "档位": "、".join(pool_cn(p) for p in (r.get("pools") or [])) or "未分类",
            "X值": float(xv),
            "胜率": r.get("win_rate"),
            "样本": r.get("n_evaluated", 0),
        })
    if not pts:
        st.info(
            "ℹ️ 当前口径「%s」下没有可绘制的数据。"
            "若选择的是混合/指数超额，说明基准指数尚未就绪（超额列为 NULL）。" % x_label
        )
        return
    try:
        df = pd.DataFrame(pts)
        base = alt.Chart(df).encode(
            x=alt.X("X值:Q", title=x_label),
            y=alt.Y("胜率:Q", axis=alt.Axis(format=".0%")),
            size=alt.Size("样本:Q", scale=alt.Scale(range=[40, 900]), legend=None),
            color=alt.Color("档位:N", title="档位"),
            tooltip=[
                alt.Tooltip("战法:N"),
                alt.Tooltip("档位:N"),
                alt.Tooltip("X值:Q", format=".2f"),
                alt.Tooltip("胜率:Q", format=".1%"),
                alt.Tooltip("样本:Q"),
            ],
        )
        zero_line = alt.Chart(pd.DataFrame({"y": [0.0]})).mark_rule(
            color="#dc2626", strokeDash=[4, 4]
        ).encode(y="y:Q")
        half_line = alt.Chart(pd.DataFrame({"x": [0.5]})).mark_rule(
            color="#94a3b8", strokeDash=[2, 4]
        ).encode(x="x:Q")
        chart = (base.mark_circle(opacity=0.75) + zero_line + half_line).properties(
            height=360,
            title="战法胜率 × %s（点大小 = 已评估样本量）" % x_label,
        )
        st.altair_chart(chart, width="stretch")
    except Exception as e:
        logger.warning("[se-panel] 散点图渲染失败: %s", e)
        st.info("ℹ️ 图表渲染失败，已降级为表格展示（不影响其他功能）。")
    """⑤ 散点图：X=平均收益，Y=胜率，点大小=样本量，颜色=档位。"""
    pts = [
        {
            "战法": "%s %s" % (r.get("strategy", ""), r.get("strategy_label", "")),
            "档位": "、".join(pool_cn(p) for p in (r.get("pools") or [])) or "未分类",
            "平均收益%": r.get("avg_return"),
            "胜率": r.get("win_rate"),
            "样本": r.get("n_evaluated", 0),
        }
        for r in rows
        if r.get("avg_return") is not None and r.get("win_rate") is not None
    ]
    if not pts:
        st.info("ℹ️ 当前筛选条件下没有可绘图的胜率/收益数据。")
        return
    try:
        df = pd.DataFrame(pts)
        base = alt.Chart(df).encode(
            x=alt.X("平均收益%:Q", title="平均收益 %"),
            y=alt.Y("胜率:Q", axis=alt.Axis(format=".0%")),
            size=alt.Size("样本:Q", scale=alt.Scale(range=[40, 900]), legend=None),
            color=alt.Color("档位:N", title="档位"),
            tooltip=[
                alt.Tooltip("战法:N"),
                alt.Tooltip("档位:N"),
                alt.Tooltip("平均收益%:Q", format=".2f"),
                alt.Tooltip("胜率:Q", format=".1%"),
                alt.Tooltip("样本:Q"),
            ],
        )
        zero_line = alt.Chart(pd.DataFrame({"y": [0.0]})).mark_rule(
            color="#dc2626", strokeDash=[4, 4]
        ).encode(y="y:Q")
        half_line = alt.Chart(pd.DataFrame({"x": [0.5]})).mark_rule(
            color="#94a3b8", strokeDash=[2, 4]
        ).encode(x="x:Q")
        chart = (base.mark_circle(opacity=0.75) + zero_line + half_line).properties(
            height=360, title="战法胜率 × 平均收益（点大小 = 已评估样本量）"
        )
        st.altair_chart(chart, width="stretch")
    except Exception as e:
        logger.warning("[se-panel] 散点图渲染失败: %s", e)
        st.info("ℹ️ 图表渲染失败，已降级为表格展示（不影响其他功能）。")


def render_trend_chart(rows: List[Dict[str, Any]], history: List[Dict[str, Any]]) -> None:
    """⑥ 每日趋势（仅在快照带 history 时展示）。"""
    if not history:
        st.info(
            "ℹ️ **暂无历史趋势数据**\n\n"
            "趋势数据由 daemon 18:30 回填任务生成快照时一并写入。"
            "当前使用的是优化器推荐报表（无 history 字段），"
            "待 daemon 下一次 18:30 任务跑完后即可看到趋势图。"
        )
        return
    strategy = st.selectbox(
        "选择战法查看趋势",
        [r.get("strategy", "") for r in rows] or sorted({h.get("strategy", "") for h in history}),
        key="se_panel_trend_sel",
    )
    series = history_for_strategy(history, strategy)
    if not series:
        st.info("ℹ️ 该战法暂无历史趋势记录。")
        return
    try:
        df = pd.DataFrame(series)
        chart = alt.Chart(df).mark_line(point=True).encode(
            x=alt.X("stat_date:O", title="统计日"),
            y=alt.Y("win_rate:Q", axis=alt.Axis(format=".0%")),
            tooltip=[
                alt.Tooltip("stat_date:N"),
                alt.Tooltip("win_rate:Q", format=".1%"),
                alt.Tooltip("avg_return:Q", format=".2f"),
                alt.Tooltip("n_evaluated:Q"),
            ],
        ).properties(height=280, title="%s 每日胜率趋势" % strategy)
        st.altair_chart(chart, width="stretch")
    except Exception as e:
        logger.warning("[se-panel] 趋势图渲染失败: %s", e)
        st.info("ℹ️ 趋势图渲染失败，已降级。")


def render_strategy_detail(rows: List[Dict[str, Any]], snap: Dict[str, Any]) -> None:
    """⑦ 单战法详情。"""
    if not rows:
        return
    strategy = st.selectbox(
        "选择战法查看详情",
        [r.get("strategy", "") for r in rows],
        key="se_panel_detail_sel",
    )
    row = next((r for r in rows if r.get("strategy") == strategy), None)
    if not row:
        return

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("信号数", row.get("n_signals", 0))
    c2.metric("已评估", row.get("n_evaluated", 0))
    c3.metric("胜率", _pct(row.get("win_rate")))
    c4.metric("平均收益", _f(row.get("avg_return"), 2, "%"))

    # 【Tier2.5】净口径（扣费后）指标卡：与上方毛口径并列展示
    n1, n2, n3, n4 = st.columns(4)
    n1.metric("净胜率", _pct(row.get("net_win_rate")))
    n2.metric("净收益", _f(row.get("avg_net_return"), 2, "%"))
    n3.metric("净超额（混合）", _f(row.get("avg_net_excess_blend"), 2, "%"))
    n4.metric("净超额（沪深300）", _f(row.get("avg_net_excess"), 2, "%"))
    st.caption(
        "ℹ️ 净口径 = 扣除佣金（万3，最低5元）+ 印花税（万5，卖出）+ 过户费（十万分之1）后的真实收益；"
        "与上方毛口径并列，不替换。"
    )

    st.markdown(
        "**成熟度**：%s ｜ 已成熟 %s ｜ 缺失 %s ｜ 推荐权重 `%s`（%s）"
        % (
            "T+%d" % row["primary_n_days"] if row.get("primary_n_days") else "—",
            "、".join("T+%d" % int(x) for x in row.get("available_n_days", [])) or "—",
            "、".join("T+%d" % int(x) for x in row.get("missing_n_days", [])) or "—",
            row.get("_weight", "—"),
            row.get("_reason", "—"),
        )
    )

    pool_rows = row.get("pool_rows") or []
    if pool_rows:
        st.markdown("**各档位分布**")
        st.dataframe(
            pd.DataFrame(pool_rows).rename(
                columns={"pool": "档位", "n_days": "N档", "n_signals": "信号数", "win_rate": "胜率"}
            ),
            width="stretch",
            hide_index=True,
        )

    mat = snap.get("strategy_maturity") or {}
    sm = mat.get(strategy) if isinstance(mat, dict) else None
    if isinstance(sm, dict):
        st.caption(
            "距自动应用门槛还差 %s 个已评估样本 · 淘汰候选：%s"
            % (
                sm.get("remaining_to_auto_apply", "—"),
                "、".join(snap.get("deprecated") or []) or "无",
            )
        )


def render_duplicates_warning(snap: Dict[str, Any]) -> None:
    """疑似重复统计提示（只提示，不自动修数据）。"""
    dups = snap.get("duplicates") or []
    if not dups:
        return
    with st.expander("⚠️ 疑似重复统计（%d 组）", expanded=False):
        st.caption(
            "以下战法的样本数/胜率/收益完全一致，可能是同一次扫描被重复记账。"
            "本面板仅提示，修复需人工确认后再调整 signal_strategy_ledger。"
        )
        for d in dups:
            st.write(
                "· **%s** — 信号 %s / 已评估 %s / 胜率 %s / 收益 %s"
                % (
                    "、".join(d["strategies"]),
                    d["n_signals"],
                    d["n_evaluated"],
                    _pct(d["win_rate"]),
                    _f(d["avg_return"], 2, "%"),
                )
            )


def render_refresh_button() -> None:
    """只清缓存、不触发回填。"""
    if st.button("♻️ 刷新战法面板数据", key="se_panel_refresh", width="stretch"):
        _cached_snapshot.clear()
        st.toast("已刷新面板缓存（未触发回填）", icon="♻️")
        st.rerun()


def render_strategy_effectiveness_panel() -> None:
    """面板入口。全程 try/except 包裹，异常绝不冒泡到五池扫描。"""
    try:
        import time as _t

        salt = _t.time()
        snap = _cached_snapshot(salt)
        if not snap:
            render_empty_state()
            return
        if not snap.get("stats"):
            render_empty_state("快照存在但当前无任何战法统计记录。")
            return

        rows = _attach_rec(snap["stats"], snap.get("recommendation") or {})
        render_refresh_button()
        st.caption(
            "数据源 `%s` · schema %s · 统计日 %s"
            % (snap.get("source_file", "—"), snap.get("schema_version", "—"),
               snap.get("stat_date") or "—")
        )
        render_freshness_banner(snap)
        render_market_quotes(snap)
        st.divider()
        render_maturity_cards(snap)
        st.divider()
        render_summary_metrics(snap)
        st.divider()
        filtered = render_filters(rows)
        st.divider()
        render_effectiveness_table(filtered)
        st.divider()
        render_blend_table(filtered)
        st.divider()
        render_scatter_chart(filtered)
        st.divider()
        render_trend_chart(filtered, snap.get("history") or [])
        st.divider()
        render_strategy_detail(filtered, snap)
        render_duplicates_warning(snap)
    except Exception as e:
        logger.exception("[se-panel] 战法面板异常（已吞掉，不影响五池扫描）: %s", e)
        st.warning("⚠️ 战法胜率面板渲染异常，已自动降级（不影响五池扫描）。详情见日志。")



