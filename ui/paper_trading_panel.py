# -*- coding: utf-8 -*-
"""
ui/paper_trading_panel.py — 模拟盘只读面板（Streamlit）

【设计约束】
  · 唯一数据源 = core.paper_trading.reporting.build_snapshot()（只读 DuckDB）。
  · 不写库、不下单、不触发回填、不改任何实盘策略或权重。
  · 面板内任何异常都不得冒泡到五池扫描主流程（全部 try/except 包裹）。
  · 口径冻结：收益率一律「净」（含佣金 + 印花税 + 过户费）。

【展示结构（对标实盘券商 App）】
  ① 账户总览卡：总资产 / 现金 / 市值 / 持仓盈亏 / 今日盈亏 / 累计费用 / 实验进度
  ② 当前持仓表：含距止损、距止盈、仓位占比
  ③ 已平仓明细表：净收益、持有天数、出场原因
  ④ 战法排行榜：100 天实验的核心产出（样本重叠提示）
  ⑤ 日净值表 + 净值曲线
  ⑥ 费用汇总
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

import pandas as pd
import streamlit as st

logger = logging.getLogger(__name__)

_POOL_CN = {"p3": "P3 盘中", "p4": "P4 尾盘", "p5": "P5 盘后"}
# 【P0-2 2026-11-04 修正】止盈文案 +15% → +8%。
#   系统止盈口径自 V30.11 起已统一为 +8%（与回测、推送、止损 -3% 同源），
#   此处仍显示 +15% 会直接误导用户看模拟盘面板 —— 用户可见口径必须与执行口径一致。
_EXIT_CN = {
    "STOP_LOSS": "止损 -3%",
    "TAKE_PROFIT": "止盈 +8%",
    "DAY_DROP": "当日跌幅 -3%",
    "TIMEOUT": "到期强平",
    "END_OF_EXPERIMENT": "实验结束",
}
_STATUS_CN = {"RUNNING": "运行中", "PAUSED": "已暂停", "COHORT_CLOSED": "100 日实验已封板"}


def _f(v: Any, digits: int = 2, suffix: str = "") -> str:
    if v is None:
        return "—"
    try:
        return ("%." + str(digits) + "f") % float(v) + suffix
    except (TypeError, ValueError):
        return "—"


def _money(v: Any) -> str:
    if v is None:
        return "—"
    try:
        fv = float(v)
    except (TypeError, ValueError):
        return "—"
    return f"{fv:+,.2f}"


def _pool_cn(p: Any) -> str:
    return _POOL_CN.get(str(p or "").strip().lower(), str(p or "—"))


# 【V29.3 修复】原先把 @st.cache_data 定义在函数体内，每次 rerun 都会生成一个新的
# 缓存函数对象 → Streamlit 认为缓存 key 变了 → 缓存永远不命中，ttl=10 形同虚设，
# 且 _cached_snapshot.clear() 会作用在「临时对象」上，刷新按钮实际无效。
# 改为模块级缓存函数；强制刷新靠 cache_salt 变化绕过旧 key（旧条目由 ttl 淘汰）。
@st.cache_data(ttl=10, show_spinner=False)
def _cached_snapshot(cache_salt: float, force_quotes: bool):
    """只读快照缓存。cache_salt 由刷新按钮递增，用于强制绕过旧缓存条目。"""
    from core.paper_trading.reporting import build_snapshot

    quotes = None
    if force_quotes:
        try:
            from core.paper_trading.engine import _fetch_quotes_for_positions

            quotes = _fetch_quotes_for_positions()
        except Exception as e:  # noqa: BLE001
            logger.debug("[paper-panel] 拉取持仓实时报价失败: %s", e)
    return build_snapshot(quotes=quotes)


def _clear_snapshot_cache() -> None:
    """清空快照缓存（st.cache_data 全局 .clear()，模块级函数才可靠）。"""
    try:
        _cached_snapshot.clear()
    except Exception as e:  # noqa: BLE001
        logger.debug("[paper-panel] 清理快照缓存失败（忽略）: %s", e)


def _card_html(title: str, value: str, color: str = "#1e293b", sub: str = "") -> str:
    sub_html = (
        f'<div style="font-size:.75em;opacity:.85;margin-top:2px;">{sub}</div>' if sub else ""
    )
    return (
        '<div style="border-radius:8px;padding:10px 12px;color:#fff;text-align:center;'
        'box-shadow:0 4px 6px -1px rgba(0,0,0,.1);min-height:82px;'
        'display:flex;flex-direction:column;justify-content:center;background:%s;">'
        '<div style="font-size:.85em;opacity:.9;font-weight:600;">%s</div>'
        '<div style="font-size:1.3em;font-weight:700;margin-top:4px;">%s</div>%s</div>'
        % (color, title, value, sub_html)
    )


def render_status_banner(snap: Dict[str, Any]) -> None:
    """顶部开关 / 实验状态提示。"""
    cfg = snap.get("config")
    enabled = bool(snap.get("enabled"))
    acct = snap.get("account") or {}
    status = _STATUS_CN.get(str(acct.get("status") or "RUNNING"), str(acct.get("status") or ""))
    prog = snap.get("progress") or {}
    day = int(prog.get("day_index") or 0)
    total = int(prog.get("cohort_days") or 100)
    if not enabled:
        st.info(
            "ℹ️ 模拟盘当前为**关闭**状态。开启方式：把 `config.yaml` 的 "
            "`paper_trading.enabled` 置为 `true`，并重启守护进程。"
        )
        return
    if not acct:
        st.info(
            "ℹ️ 模拟盘已开启，但尚未建立账户（守护进程 08:40 的「模拟盘初始化」任务尚未跑过，"
            "或首次 P3 / P4 正式信号还没出现）。下方各表均为空态，属正常。"
        )
        return
    st.success(
        f"🟢 模拟盘运行中 · 状态：{status} · 实验进度：**第 {day} / {total} 个交易日**"
        + ("（已封板，停止新开仓）" if prog.get("closed") else "")
    )


def render_account_cards(snap: Dict[str, Any]) -> None:
    """① 账户总览：7 个指标卡。"""
    acct = snap.get("account")
    if not acct:
        st.caption("尚未建立模拟账户（首次买入后自动创建）。")
        return
    fee = snap.get("fee_summary") or {}
    prog = snap.get("progress") or {}
    day = int(prog.get("day_index") or 0)
    total = int(prog.get("cohort_days") or 100)
    pnl = float(acct.get("total_pnl") or 0.0)
    day_pnl = float(acct.get("day_pnl") or 0.0)
    c = st.columns(7)
    c[0].markdown(_card_html("总资产", _money(acct.get("total_asset")), "#1e293b"), unsafe_allow_html=True)
    c[1].markdown(_card_html("可用现金", _f(acct.get("cash")), "#334155"), unsafe_allow_html=True)
    c[2].markdown(_card_html("持仓市值", _f(acct.get("market_value")), "#334155"), unsafe_allow_html=True)
    c[3].markdown(
        _card_html(
            "持仓盈亏",
            _money(acct.get("unrealized_pnl")),
            "#dc2626" if float(acct.get("unrealized_pnl") or 0.0) < 0 else "#16a34a",
        ),
        unsafe_allow_html=True,
    )
    c[4].markdown(
        _card_html("今日盈亏", _money(day_pnl), "#dc2626" if day_pnl < 0 else "#16a34a"),
        unsafe_allow_html=True,
    )
    c[5].markdown(_card_html("累计费用", _f(fee.get("total")), "#b45309"), unsafe_allow_html=True)
    c[6].markdown(
        _card_html(
            "实验进度",
            f"{day} / {total}",
            "#0f766e",
            f"{_f(prog.get('pct'), 1, '%')} · 持仓 {int(acct.get('position_count') or 0)} 只",
        ),
        unsafe_allow_html=True,
    )


def render_positions_table(snap: Dict[str, Any]) -> None:
    """② 当前持仓表。"""
    rows: List[Dict[str, Any]] = snap.get("positions") or []
    st.markdown("#### 📦 当前持仓")
    if not rows:
        st.caption("当前无持仓。P3/P4 出现正式信号且现金充足时会自动买入。")
        return
    df = pd.DataFrame(
        [
            {
                "代码": r["code"],
                "名称": r["name"],
                "来源池": _pool_cn(r["pool"]),
                "触发战法": r["strategy_names"] or r["strategy_ids"] or "—",
                "买入日期": r["buy_date"],
                "买入时间": r["buy_time"],
                "买入价": float(r["buy_price"]),
                "现价": float(r["price"]),
                "股数": int(r["shares"]),
                "净成本": round(float(r["net_cost"]), 2),
                "市值": round(float(r["market_value"]), 2),
                "浮动盈亏": round(float(r["unrealized_pnl"]), 2),
                "浮动收益率%": round(float(r["unrealized_pnl_pct"]) * 100.0, 2),
                "仓位占比%": round(float(r["weight_pct"]), 2),
                "止损价": round(float(r["stop_loss_price"]), 2),
                "止盈价": round(float(r["take_profit_price"]), 2),
                "距止损%": round(float(r["dist_to_stop_pct"]), 2),
                "距止盈%": round(float(r["dist_to_take_pct"]), 2),
                "持有天数": int(r["holding_days"]),
            }
            for r in rows
        ]
    )
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        alt="模拟盘当前持仓明细，含买入价、现价、浮动盈亏与距止损止盈距离",
        column_config={
            "买入价": st.column_config.NumberColumn("买入价", format="%.2f"),
            "现价": st.column_config.NumberColumn("现价", format="%.2f"),
            "净成本": st.column_config.NumberColumn("净成本", format="%.2f"),
            "市值": st.column_config.NumberColumn("市值", format="%.2f"),
            "浮动盈亏": st.column_config.NumberColumn("浮动盈亏", format="%+.2f"),
            "浮动收益率%": st.column_config.NumberColumn("浮动收益率", format="%+.2f%%"),
            "仓位占比%": st.column_config.NumberColumn("仓位占比", format="%.2f%%"),
            "止损价": st.column_config.NumberColumn("止损价", format="%.2f"),
            "止盈价": st.column_config.NumberColumn("止盈价", format="%.2f"),
            "距止损%": st.column_config.NumberColumn("距止损", format="%+.2f%%"),
            "距止盈%": st.column_config.NumberColumn("距止盈", format="%+.2f%%"),
        },
    )


def render_trades_table(snap: Dict[str, Any]) -> None:
    """③ 已平仓明细表。"""
    rows: List[Dict[str, Any]] = snap.get("trades") or []
    st.markdown("#### 🧾 已平仓记录")
    if not rows:
        st.caption("尚无平仓记录。持仓触发 -3% 止损 / +8% 止盈 / 当日跌 3% 离场后自动记录。")
        return
    df = pd.DataFrame(
        [
            {
                "平仓日期": r["trade_date"],
                "代码": r["code"],
                "名称": r["name"],
                "来源池": _pool_cn(r["pool"]),
                "战法": r["strategy_names"] or r["strategy_ids"] or "—",
                "买入价": round(float(r["buy_price"]), 2),
                "卖出价": round(float(r["sell_price"]), 2),
                "持有天数": int(r["holding_days"]),
                "费用合计": round(float(r["total_fee"]), 2),
                "净收益": round(float(r["pnl"]), 2),
                "净收益率%": round(float(r["pnl_pct"]) * 100.0, 2),
                "出场原因": _EXIT_CN.get(str(r["exit_reason"]), str(r["exit_reason"] or "—")),
            }
            for r in rows
        ]
    )
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        alt="模拟盘已平仓交易明细，含净收益与出场原因",
        column_config={
            "买入价": st.column_config.NumberColumn("买入价", format="%.2f"),
            "卖出价": st.column_config.NumberColumn("卖出价", format="%.2f"),
            "费用合计": st.column_config.NumberColumn("费用合计", format="%.2f"),
            "净收益": st.column_config.NumberColumn("净收益", format="%+.2f"),
            "净收益率%": st.column_config.NumberColumn("净收益率", format="%+.2f%%"),
        },
    )


def render_leaderboard(snap: Dict[str, Any]) -> None:
    """④ 战法排行榜（100 天实验核心产出）。"""
    rows: List[Dict[str, Any]] = snap.get("leaderboard") or []
    st.markdown("#### 🏆 战法排行榜（按净收益率）")
    st.caption(
        "口径：收益率为**净收益**（已扣佣金 / 印花税 / 过户费）；胜率 = 净收益 > 0 的平仓占比。"
        "一个信号可能同时命中多个原子战法，因此**各战法样本相互重叠，累计盈亏不可相加**。"
        "样本 < 10 笔标记「样本不足」，不作可信排名。"
    )
    if not rows:
        st.caption("尚无已平仓样本，排行榜暂无数据。")
        return
    df = pd.DataFrame(
        [
            {
                "战法ID": r["strategy_id"],
                "战法名称": r["strategy_names"],
                "归属池": _pool_cn(r["pool"]),
                "信号数": int(r["signal_count"]),
                "已平仓": int(r["closed_count"]),
                "胜率%": round(float(r["win_rate"]), 2),
                "平均净收益": round(float(r["avg_pnl"]), 2),
                "中位净收益": round(float(r["median_pnl"]), 2),
                "平均净收益率%": round(float(r["avg_pnl_pct"]) * 100.0, 2),
                "累计净盈亏": round(float(r["total_pnl"]), 2),
                "止损率%": round(float(r["stop_rate"]), 2),
                "止盈率%": round(float(r["take_rate"]), 2),
                "当日跌幅离场率%": round(float(r.get("day_drop_rate") or 0.0), 2),
                "平均持有天数": round(float(r["avg_holding_days"]), 2),
                "样本可信度": r["sample_label"],
            }
            for r in rows
        ]
    )
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        alt="战法排行榜，按净收益率排序，含样本数与止损止盈比例",
        column_config={
            "胜率%": st.column_config.NumberColumn("胜率", format="%.2f%%"),
            "平均净收益": st.column_config.NumberColumn("平均净收益", format="%+.2f"),
            "中位净收益": st.column_config.NumberColumn("中位净收益", format="%+.2f"),
            "平均净收益率%": st.column_config.NumberColumn("平均净收益率", format="%+.2f%%"),
            "累计净盈亏": st.column_config.NumberColumn("累计净盈亏", format="%+.2f"),
            "止损率%": st.column_config.NumberColumn("止损率", format="%.2f%%"),
            "止盈率%": st.column_config.NumberColumn("止盈率", format="%.2f%%"),
            "当日跌幅离场率%": st.column_config.NumberColumn("当日跌幅离场率", format="%.2f%%"),
        },
    )


def render_equity_section(snap: Dict[str, Any]) -> None:
    """⑤ 日净值表 + 净值曲线。"""
    st.markdown("#### 📈 日净值")
    rows: List[Dict[str, Any]] = snap.get("equity") or []
    if not rows:
        st.caption("尚无收盘净值记录（每个交易日 15:05 自动结算一次）。")
        return
    df = pd.DataFrame(
        [
            {
                "交易日": r["trade_date"],
                "现金": round(float(r["cash"]), 2),
                "市值": round(float(r["market_value"]), 2),
                "总资产": round(float(r["total_asset"]), 2),
                "当日盈亏": round(float(r["daily_pnl"]), 2),
                "当日收益率%": round(float(r["daily_pnl_pct"]) * 100.0, 2),
                "累计盈亏": round(float(r["cum_pnl"]), 2),
                "累计收益率%": round(float(r["cum_return"]) * 100.0, 2),
                "回撤%": round(float(r["drawdown_pct"]) * 100.0, 2),
                "持仓数": int(r["position_count"]),
                "成交笔数": int(r["trade_count"]),
                "当日费用": round(float(r["total_fee"]), 2),
            }
            for r in rows
        ]
    ).sort_values("交易日")
    st.dataframe(
        df,
        hide_index=True,
        width="stretch",
        alt="模拟盘每日净值记录，含总资产、累计收益率与回撤",
        column_config={
            "现金": st.column_config.NumberColumn("现金", format="%.2f"),
            "市值": st.column_config.NumberColumn("市值", format="%.2f"),
            "总资产": st.column_config.NumberColumn("总资产", format="%.2f"),
            "当日盈亏": st.column_config.NumberColumn("当日盈亏", format="%+.2f"),
            "当日收益率%": st.column_config.NumberColumn("当日收益率", format="%+.2f%%"),
            "累计盈亏": st.column_config.NumberColumn("累计盈亏", format="%+.2f"),
            "累计收益率%": st.column_config.NumberColumn("累计收益率", format="%+.2f%%"),
            "回撤%": st.column_config.NumberColumn("回撤", format="%.2f%%"),
            "当日费用": st.column_config.NumberColumn("当日费用", format="%.2f"),
        },
    )
    chart_df = df[["交易日", "总资产"]].rename(columns={"交易日": "date", "总资产": "total_asset"})
    st.line_chart(
        chart_df,
        x="date",
        y="total_asset",
        alt="模拟盘总资产日净值曲线",
    )


def render_fee_section(snap: Dict[str, Any]) -> None:
    """⑥ 费用汇总。"""
    fee = snap.get("fee_summary") or {}
    st.markdown("#### 💰 费用汇总（A股实际口径）")
    c = st.columns(4)
    c[0].markdown(_card_html("佣金合计", _f(fee.get("commission")), "#334155"), unsafe_allow_html=True)
    c[1].markdown(_card_html("印花税合计", _f(fee.get("stamp_tax")), "#334155"), unsafe_allow_html=True)
    c[2].markdown(_card_html("过户费合计", _f(fee.get("transfer_fee")), "#334155"), unsafe_allow_html=True)
    c[3].markdown(_card_html("费用总计", _f(fee.get("total")), "#b45309"), unsafe_allow_html=True)
    st.caption(
        "口径：佣金 = max(成交额 × 佣金率, 最低 5 元)，买卖双边；印花税 = 成交额 × 0.05%，仅卖出；"
        "过户费 = 成交额 × 0.001%，买卖双边。参数可在 `config.yaml → paper_trading.fee` 调整。"
    )


def render_paper_trading_panel() -> None:
    """面板入口：账户总览 + 持仓 + 平仓 + 排行榜 + 净值 + 费用。全程异常隔离。"""
    try:
        import time as _t

        # 强制刷新开关放最前面：本次 rerun 就能拉实时报价，不再等到下一次交互
        force = st.button("♻️ 刷新实时报价", width="content")
        if force:
            _clear_snapshot_cache()
            st.session_state["paper_force_quotes"] = not bool(
                st.session_state.get("paper_force_quotes", False)
            )
        force_quotes = bool(st.session_state.get("paper_force_quotes", False))
        salt = float(st.session_state.get("paper_cache_salt", 0.0))
        snap = _cached_snapshot(salt, force_quotes)
        if not snap or not snap.get("available"):
            st.warning(
                "⚠️ 模拟盘数据读取异常：%s"
                % (str((snap or {}).get("error", "未知错误"))[:200])
            )
            return
        render_status_banner(snap)
        st.divider()
        render_account_cards(snap)
        st.divider()
        render_positions_table(snap)
        st.divider()
        render_trades_table(snap)
        st.divider()
        render_leaderboard(snap)
        st.divider()
        render_equity_section(snap)
        st.divider()
        render_fee_section(snap)
    except Exception as e:
        logger.exception("[paper-panel] 模拟盘面板异常（已吞掉，不影响五池扫描）: %s", e)
        st.warning("⚠️ 模拟盘面板渲染异常，已自动降级（不影响五池扫描）。详情见日志。")
