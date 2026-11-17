# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V28 — 出场建议（4 档模板）
================================================================================
【V28 新增·E 项】对每只推送的票自动绑定一个最适合的出场模板。

4 档模板：
  ① 严止损型（次新 / 高位股 / 题材高潮期）：跌破 MA5 -1% 即止损
  ② 中止损型（普通票 / 上升期）：跌破 MA10 -2% 即止损
  ③ 宽止损型（反转票 / 退潮期 / 龙头趋势）：跌破 MA20 -3% 或前低 -3%
  ④ 持有型（强势趋势 / 主力高度控盘）：浮盈 > 5% 后止损上调至成本价

每档模板的止盈规则：
  - 浮盈 ≥ 8% 减半仓
  - 浮盈 ≥ 15% 清仓
  - 跌破 MA5 即清仓（不持有）
  - 持仓周期：龙头 5 日 / 普通 3 日 / 反转 2 日

【P0-2 重要变更 2026-11-04】
  以上「+8%减半 / +15%清仓 / MA止损 / 持仓3-5日」是**模板的设计意图描述**，
  但模拟盘 engine.py 实际执行的是统一的「+8% 全清 / -3% 止损 / 最多 10 交易日」。
  为避免「推送建议 ≠ 系统行为」长期误导用户，build_exit_plan 现已改为：
    · stop_loss_price / take_profit_full_price / max_hold_days 一律取**配置的执行口径**
    · 模板仅保留风格描述（stop_loss_rule / trailing_stop）作参考文案
  即：推送里出现的价格与持有天数，就是系统真正会执行的数字。

集成：
  - core/pool_signal_adapter_v28.append_signal_log_safe 推送前给 res 注入
    res['exit_template'] / res['stop_loss'] / res['take_profit'] /
    res['trailing_stop_rules']
  - 推送文案自动带上出场建议

注意：P4 已有入场质量评估和 trailing_stop_rules，本模块给 P1/P3/P5 也补上。
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


EXIT_TEMPLATES = {
    "tight": {
        "label": "严止损型",
        "emoji": "🔴",
        "stop_loss_rule": "跌破 MA5 且 -1%",
        "trailing_stop": "成本价 -1%",
        "take_profit_half": 8.0,
        "take_profit_full": 15.0,
        "max_hold_days": 3,
        "适用": "次新股 / 高位股 / 题材高潮期",
    },
    "medium": {
        "label": "中止损型",
        "emoji": "🟡",
        "stop_loss_rule": "跌破 MA10 且 -2%",
        "trailing_stop": "成本价 -2%",
        "take_profit_half": 8.0,
        "take_profit_full": 15.0,
        "max_hold_days": 3,
        "适用": "普通票 / 上升期 / 动量池",
    },
    "wide": {
        "label": "宽止损型",
        "emoji": "🟢",
        "stop_loss_rule": "跌破 MA20 -3% 或前低 -3%",
        "trailing_stop": "成本价 -3%",
        "take_profit_half": 10.0,
        "take_profit_full": 18.0,
        "max_hold_days": 5,
        "适用": "反转票 / 退潮期 / 龙头趋势 / P4 尾盘启动",
    },
    "hold": {
        "label": "持有型",
        "emoji": "🟣",
        "stop_loss_rule": "浮盈>5% 时 成本价兜底 / 否则 MA20 -3%",
        "trailing_stop": "成本价（浮盈>5% 后）",
        "take_profit_half": 12.0,
        "take_profit_full": 20.0,
        "max_hold_days": 5,
        "适用": "强势趋势 / 主力高度控盘 / wr ≥ 90 + 缩量上行",
    },
}


def pick_exit_template(res: Dict[str, Any]) -> str:
    """
    根据 res 的特征自动选档（4 档）。

    容错读取字段：
      - score → burst_score / score / p1_score
      - pool → res.pool / score_details.pool / res["push_pool"]
      - strategies → res.strategies（list 或 "|" 分隔字符串）
      - 题材生命周期 → score_details.题材生命周期
      - 动量/反转分类 → score_details.P1动量/反转分类
      - 缩量锁仓 → risk_tags / strategies / score_details 三个来源
    """
    try:
        pool = str(res.get("pool") or res.get("push_pool") or "").strip().lower() or "p1"
        score = float(res.get("burst_score") or res.get("score") or res.get("p1_score") or 0.0)
    except (TypeError, ValueError):
        pool, score = "p1", 0.0

    score_details = res.get("score_details") or {}
    if not isinstance(score_details, dict):
        score_details = {}

    risk_tags = res.get("risk_tags") or []
    if isinstance(risk_tags, str):
        risk_tags = [risk_tags]

    strategies = res.get("strategies") or []
    if isinstance(strategies, str):
        strategies = [s.strip() for s in strategies.replace("|", ",").split(",") if s.strip()]

    all_signals = (
        [str(s) for s in strategies]
        + [str(t) for t in risk_tags]
        + [str(v) for v in score_details.values() if v]
    )
    joined_signals = "|".join(all_signals)

    theme_lc = str(score_details.get("题材生命周期", "") or "")
    track = str(score_details.get("P1动量/反转分类", "") or "").strip().lower()

    # === ① 严止损型触发：次新 / 高位股 / 题材高潮期 / ST / 连续跌停 / 高位派发 ===
    # 【V29-A】改为更宽容的子串匹配，覆盖策略里所有"次新""ST"等变体
    tight_keywords = (
        "次新股", "次新",
        "ST", "*ST",
        "退市风险", "退",
        "连续跌停",
        "高位派发",
        "财报雷",
    )
    if "高潮期" in theme_lc:
        return "tight"
    if any(kw in joined_signals for kw in tight_keywords):
        return "tight"

    # === ④ 持有型触发：wr 高 + 缩量上行 + 主力控盘（先于宽止损）===
    # 【V29-A 修复】hold 关键词改为更宽容的多种触发条件
    # 真实策略里锁仓相关 tag 可能写成"锁仓" "缩量锁仓" "主力控盘" "高度控盘"
    # 或"高位缩量锁仓"等，因此用 `子串包含` 而非精确字符串匹配
    hold_keywords = (
        "锁仓",         # 缩量锁仓 / 高位锁仓 / 主力锁仓
        "缩量",         # 缩量上行 / 缩量新高
        "控盘",         # 主力控盘 / 高度控盘
        "持有型",       # 显式持有型标签
    )
    if score >= 88.0:
        if any(kw in joined_signals for kw in hold_keywords):
            return "hold"

    # === ② 宽止损型触发：反转票 / 龙头 / 退潮期 / P4 尾盘 ===
    if track == "reversal" or "反转" in str(score_details.get("P1动量/反转分类", "")):
        return "wide"
    if any(kw in joined_signals for kw in ("炸板回封龙头", "龙头", "退潮期", "混沌期", "尾盘", "主推")):
        return "wide"
    if pool == "p4":
        return "wide"

    # 【V26.88+10-3 P2-⑩】退出模板行业化：A 股不同行业最佳持有期差异极大
    # - defensive（消费/医药/公用）：默认 medium（保留原有默认）
    # - cyclical（有色/化工/钢铁/煤炭/化纤/造纸）：默认 fast（周期反转短平快）
    # - growth（半导体/AI/新能源车）：默认 slow（成长主题需要时间过渡）
    _industry = str(res.get("industry") or res.get("ind") or "").strip()
    if _industry:
        _cyclical = ("有色", "化工", "钢铁", "煤炭", "化纤", "造纸", "石油石化", "养殖", "航运", "基础化工")
        _growth = ("半导体", "电子", "计算机", "通信", "AI", "新能源", "汽车", "机械设备", "电力设备", "国防军工")
        _defensive = ("食品饮料", "医药生物", "公用事业", "社会服务", "农林牧渔", "家用电器", "传媒")
        if any(k in _industry for k in _cyclical):
            return "fast"
        if any(k in _industry for k in _growth):
            return "slow"
        if any(k in _industry for k in _defensive):
            return "medium"

    # === ② 中止损型（默认） ===
    return "medium"


def build_exit_plan(template_key: str, push_price: float) -> Dict[str, Any]:
    """
    生成具体出场计划（含价格点位）。

    【P0-2 修复 2026-11-04】原实现按模板给 -1%/-2%/-3% 止损、+8%减半/+15%清仓，
    但模拟盘 engine.py 实际只执行「+8% 全清 / -3% 止损 / 最多 10 交易日」，
    两者完全不同 → 推送建议与系统行为长期矛盾。

    现改为：**价格点位一律取系统执行口径**（配置的 stop_loss_pct / take_profit_pct /
    max_hold_days），模板仅保留「风格描述」作为参考文案。
    这样推送里的止损价、止盈价、最长持有日，就是系统真正会执行的数字。

    输入：
        template_key: tight / medium / wide / hold（仅影响风格描述与参考规则）
        push_price: 推送时价格（元）
    """
    info = EXIT_TEMPLATES.get(template_key, EXIT_TEMPLATES["medium"])

    # 执行口径（与 engine.py 完全一致）
    try:
        from core.paper_trading.config import load_config

        _c = load_config()
        exec_sl = float(_c.stop_loss_pct)
        exec_tp = float(_c.take_profit_pct)
        exec_hold = int(getattr(_c, "max_hold_days", 10))
        # 【2026-11-05】当日跌幅离场（相对昨收）；0/正数 = 未启用
        exec_drop = float(getattr(_c, "day_drop_exit_pct", 0.0) or 0.0)
        if exec_drop >= 0:
            exec_drop = 0.0
    except Exception:  # noqa: BLE001
        exec_sl, exec_tp, exec_hold, exec_drop = -3.0, 8.0, 10, -3.0

    px = float(push_price)
    stop_px = round(px * (1.0 + exec_sl / 100.0), 2)
    take_px = round(px * (1.0 + exec_tp / 100.0), 2)
    drop_txt = f" / 当日跌{abs(exec_drop):.0f}%离场（相对昨收）" if exec_drop < 0 else ""

    return {
        "template": template_key,
        "template_label": f"{info['emoji']}{info['label']}",
        # —— 执行口径价格（系统真正会用的）——
        "stop_loss_price": stop_px,
        "take_profit_full_price": take_px,
        # 系统是一次性止盈、不分批减仓，故 half 与 full 同价（避免展示出并不存在的减半动作）
        "take_profit_half_price": take_px,
        "max_hold_days": exec_hold,
        "exec_stop_pct": exec_sl,
        "exec_take_profit_pct": exec_tp,
        "exec_max_hold_days": exec_hold,
        # 【2026-11-05】当日跌幅离场阈值（负数=启用，0=关闭）；相对昨收，不是相对成本
        "exec_day_drop_pct": exec_drop,
        # —— 风格参考（仅文案，不参与执行）——
        "stop_loss_rule": info["stop_loss_rule"],
        "trailing_stop_rule": info["trailing_stop"],
        "rule_text": (
            f"{info['emoji']}{info['label']}｜执行："
            f"{exec_sl:.0f}%止损 / +{exec_tp:.0f}%止盈清仓 / 最多持有{exec_hold}交易日"
            f"{drop_txt}"
            f"（参考风格：{info['stop_loss_rule']}）"
        ),
    }


def attach_exit_plan_to_res(res: Dict[str, Any], push_price: Optional[float] = None) -> Dict[str, Any]:
    """
    给 res 注入 exit_plan 字段（不改 res 的其他部分）。
    push_price 默认从 rt.price / res.price 取。
    """
    try:
        if not isinstance(res, dict):
            return res
        if push_price is None or push_price <= 0:
            push_price = float(res.get("price") or res.get("push_price") or 0.0)
        if push_price <= 0:
            return res

        template_key = pick_exit_template(res)
        plan = build_exit_plan(template_key, push_price)
        res["exit_plan"] = plan
        res["exit_template"] = template_key
        res["auto_stop_loss"] = plan["stop_loss_price"]
        res["auto_take_profit"] = plan["take_profit_full_price"]
        res["trailing_stop_rules"] = plan["trailing_stop_rule"]
    except Exception as e:
        logger.debug("[exit_plan] 注入异常: %s", e)
    return res


def render_exit_hint(res: Dict[str, Any]) -> str:
    """
    生成推送文案的"出场建议"段。
    """
    if not isinstance(res, dict):
        return ""
    ep = res.get("exit_plan")
    if not ep:
        return ""
    return f"\n📌出场：{ep.get('rule_text','')}"
