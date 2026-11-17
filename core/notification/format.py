# -*- coding: utf-8 -*-
"""
core/notification/format.py
原 core/notification_gateway.py L1308-L1527（format_wechat_markdown 主函数）

【职责】
- _safe_score_display(stock_dict)：综合分安全展示
- format_wechat_markdown(pool_key, stock_dict)：企微 markdown 主模板
  - 标题（按战区/直通车/观察池区分）
  - 代码/名称/现价/涨幅/综合分
  - 入池理由/热门板块加成/命中战法
  - 执行建议/建议仓位/风险标签/买入提示
  - 缩量观察池警示
  - 完整防御性兜底（finally 返回极简模板）

【依赖】
- core.notification.core（_POOL_ALERT_TITLES / _MAX_MARKDOWN_CONTENT_LEN / _esc 等）
- core.stock_name_utils.normalize_stock_display_name
"""
import logging
import math
from typing import Any, Dict

from core.notification.core import (
    _MAX_MARKDOWN_CONTENT_LEN,
    _POOL_ALERT_TITLES,
    _bj_now_str,
    _esc,
    _finite_float_or_none,
    _norm_ts_code,
    _parse_pct_to_float,
)
from core.stock_name_utils import normalize_stock_display_name

logger = logging.getLogger(__name__)


def _safe_score_display(stock_dict: Dict[str, Any]) -> str:
    """综合分：有限 float 则两位小数；否则 '--'。"""
    raw = stock_dict.get("综合分", None)
    x = _finite_float_or_none(raw)
    if x is None:
        try:
            s = str(raw).strip()
            return s if s else "--"
        except Exception:
            return "--"
    return f"{x:.2f}"


# 【P0-2 修复 2026-11-04】出场口径统一：推送文案不再硬编码 -5%。
#
# 原来的问题：这里写死「浮亏 -5% 无条件止损」，但模拟盘实际在 -3% 就已经卖出，
# 于是用户手机上看到的纪律（-5%）与系统真正执行的（-3%）**永远对不上**。
# 你按文案操作 → 与系统回测结果不一致 → 误以为「系统没有用」。
#
# 现在改为从模拟盘配置读取，保证「推送说的」= 「系统做的」。
# 若配置读取失败，回落到 -3.0%（与 PaperTradingConfig 默认值一致），
# 绝不回落到 -5% —— 因为那是已被证实与实盘不符的旧值。
_FALLBACK_STOP_LOSS_PCT = -3.0


def _resolve_hard_stop_pct() -> float:
    """硬止损百分比：优先取模拟盘配置的 stop_loss_pct，保证与实盘执行口径一致。"""
    try:
        from core.paper_trading.config import load_config

        pct = float(load_config().stop_loss_pct)
        if pct < 0:
            return pct
    except Exception:  # noqa: BLE001
        pass
    return _FALLBACK_STOP_LOSS_PCT


def _resolve_discipline_line() -> str:
    """纪律底线文案：止损百分比随配置走，避免与实盘执行口径冲突。"""
    pct = _resolve_hard_stop_pct()
    return f"单票≤8% · 总仓≤40% · 浮亏{pct:.0f}%无条件止损"


# 向后兼容：保留常量名，但值改为按配置解析（模块导入时解析一次）。
# 注意：交易日内若改了 config.yaml，需重启进程才会反映到文案上。
HARD_STOP_LOSS_PCT = _resolve_hard_stop_pct()
DISCIPLINE_LINE = _resolve_discipline_line()


def _fmt_num(v, digits=2, suffix=""):
    """安全数字格式化：非有限值返回 '--'。"""
    x = _finite_float_or_none(v)
    if x is None:
        return "--"
    return f"{x:.{digits}f}{suffix}"


def _render_risk_card(stock_dict):
    """
    渲染「风控执行卡」：建议仓位 / 止损价 / 止盈价 / 最长持有 / 纪律底线。

    数据来源（任一缺失即整段省略，绝不显示半截卡片）：
      - core.exit_strategy.build_exit_plan 产出的 exit_plan
      - 或 stock_dict 直接携带 stop_loss_price / take_profit_* / max_hold_days
    """
    try:
        if not isinstance(stock_dict, dict):
            return ""
        ep = stock_dict.get("exit_plan") if isinstance(stock_dict.get("exit_plan"), dict) else {}

        def _first(*keys):
            for src in (ep, stock_dict):
                for k in keys:
                    v = src.get(k)
                    if v not in (None, ""):
                        return v
            return None

        pos = _first("建议仓位", "suggested_position", "position")
        sl_price = _finite_float_or_none(_first("stop_loss_price", "auto_stop_loss", "止损价"))
        sl_pct = _finite_float_or_none(_first("stop_loss_pct"))
        tp_half = _finite_float_or_none(_first("take_profit_half_price"))
        tp_full = _finite_float_or_none(_first("take_profit_full_price", "auto_take_profit"))
        hold = _finite_float_or_none(_first("max_hold_days"))
        tpl = _first("template_label", "exit_template", "出场模板")

        has_plan = any(v is not None for v in (sl_price, sl_pct, tp_half, tp_full, hold))
        if not has_plan and not pos:
            return ""

        bits = []
        if pos:
            bits.append("建议仓位 " + _esc(str(pos)))
        if tpl:
            bits.append("出场模板 " + _esc(str(tpl)))
        if sl_price is not None:
            seg = "战术止损 " + _fmt_num(sl_price)
            if sl_pct is not None:
                seg += "（" + _fmt_num(sl_pct, 2, "%") + "）"
            bits.append(seg)
        if tp_half is not None or tp_full is not None:
            seg = "止盈 "
            if tp_half is not None:
                seg += "半仓 " + _fmt_num(tp_half)
            if tp_full is not None:
                seg += (" / " if tp_half is not None else "") + "清仓 " + _fmt_num(tp_full)
            bits.append(seg)
        if hold is not None:
            bits.append("最长持有 " + str(int(hold)) + "日")

        lines = ['> <font color="warning">**📌 风控执行卡**</font>']
        lines.append('> <font color="comment">' + _esc(" · ".join(bits)) + "</font>")
        lines.append(
            '> <font color="warning">'
            + _esc("硬止损 " + _fmt_num(HARD_STOP_LOSS_PCT, 1, "%") + "（铁律无条件）｜" + DISCIPLINE_LINE)
            + "</font>"
        )
        return "\n".join(lines)
    except Exception as e:
        logger.debug("[risk_card] 渲染异常（忽略）: %s", e)
        return ""


def format_wechat_markdown(pool_key: str, stock_dict: Dict[str, Any]) -> str:
    """企业微信 markdown 正文（msgtype=markdown 的 content 字段）。
    颜色：涨幅>0 用 warning（橙红），否则 info（绿）；辅助说明用 comment（灰）。
    战法行整体加粗展示；观察池带【缩量期备选】时追加醒目提示行。

    【防爆】stock_dict 非 dict、缺键、NaN、嵌套异常时一律降级为占位符，不向外抛导致整条推送作废。
    """
    try:
        if not isinstance(stock_dict, dict):
            stock_dict = {}

        pool_name = str(pool_key or "").strip().upper()
        pk = pool_name.lower()
        emoji, zone_name = _POOL_ALERT_TITLES.get(pk, ("🚨", pool_name or "未知战区"))
        tier = str(stock_dict.get("pool_tier", "") or "")
        pool_source = str(stock_dict.get("pool_source", "") or "")
        zhanfa = str(stock_dict.get("战法", "") or "")
        version_tag = "V32.0"
        if tier == "observation" or "【缩量期备选】" in zhanfa:
            title = f"{version_tag} {emoji} 【{zone_name}·观察池】信号触发"
        elif pool_source == "直通车" or tier == "fastlane":
            title = f"{version_tag} {emoji} 【{zone_name}·直通车】信号触发"
        else:
            title = f"{version_tag} {emoji} 【{zone_name}】信号触发"

        code = _esc(_norm_ts_code(stock_dict.get("代码")) or stock_dict.get("代码", "--"))
        name_raw = normalize_stock_display_name(stock_dict.get("名称", "--"))
        code_plain = _norm_ts_code(stock_dict.get("代码")) or str(stock_dict.get("代码", "") or "").strip()
        if not code_plain:
            code_plain = "--"
        name_combined = f"{name_raw if name_raw is not None else '--'}"
        name = _esc(name_combined)

        price_raw = stock_dict.get("现价", "--")
        price = _esc(price_raw if price_raw is not None else "--")

        pct_raw = stock_dict.get("涨幅", "--")
        pct_f = _parse_pct_to_float(pct_raw)
        try:
            if isinstance(pct_raw, float) and (math.isnan(pct_raw) or math.isinf(pct_raw)):
                pct_disp = "--"
            else:
                pct_disp = str(pct_raw).strip() if pct_raw is not None else "--"
        except Exception:
            pct_disp = "--"
        if not pct_disp or pct_disp.lower() == "nan":
            pct_disp = "--"
        pct_disp_esc = _esc(pct_disp)

        if pct_f > 0:
            pct_line = f'<font color="warning">{pct_disp_esc}</font>'
        else:
            pct_line = f'<font color="info">{pct_disp_esc}</font>'

        score_show = _safe_score_display(stock_dict)
        score_line = _esc(score_show)
        score_num = _finite_float_or_none(stock_dict.get("综合分", None))
        if score_num is None:
            score_num = _finite_float_or_none(stock_dict.get("burst_score", stock_dict.get("score", None)))
        if score_num is None:
            score_num = 0.0
        if score_num >= 85.0:
            template_label = "可以继续看"
        elif score_num >= 40.0:
            template_label = "先观察"
        else:
            template_label = "暂不追高"
        if pool_name == "P1":
            if score_num >= 80.0:
                template_label = "可以继续看"
            elif score_num >= 65.0:
                template_label = "先观察"
            else:
                template_label = "暂不追高"

        hot_sector_bonus = _finite_float_or_none(stock_dict.get("hot_sector_bonus", stock_dict.get("热门板块加成", None)))
        hot_sector_text = None
        if hot_sector_bonus is not None and abs(hot_sector_bonus) > 1e-9:
            hot_sector_text = f"{hot_sector_bonus:+.2f}".rstrip("0").rstrip(".")

        tactic_raw = stock_dict.get("战法", "--")
        tactic = _esc(tactic_raw if tactic_raw is not None else "--")
        buy_hint_raw = (
            stock_dict.get("买入提示")
            or stock_dict.get("wechat_hint")
            or stock_dict.get("buy_hint")
            or ""
        )
        buy_hint_text = str(buy_hint_raw).strip() if buy_hint_raw else ""
        buy_hint = _esc(buy_hint_text) if buy_hint_text else ""
        exec_tier_raw = str(stock_dict.get("执行层级", "") or "").strip()
        position_raw = str(stock_dict.get("建议仓位", "") or "").strip()
        risk_tags_raw = str(stock_dict.get("风险标签", "") or "").strip()
        exec_tier = _esc(exec_tier_raw) if exec_tier_raw and exec_tier_raw != "--" else ""
        position_hint = _esc(position_raw) if position_raw and position_raw != "--" else ""
        risk_map = {
            "竞价放量确认": "走势稳，承接不错，可以继续看",
            "贴线温和放量(稳健)": "走势稳，承接不错，可以继续看",
            "尾盘增量确认": "走势稳，承接不错，可以继续看",
            "板块共振(主线同步)": "走势稳，承接不错，可以继续看",
            "⚡[强爆发]": "走势稳，承接不错，可以继续看",
            "主线共振": "走势稳，承接不错，可以继续看",
            "承接不错": "走势稳，承接不错，可以继续看",
        }
        risk_tags = _esc(risk_map.get(risk_tags_raw, "走势稳，承接不错，可以继续看")) if risk_tags_raw and risk_tags_raw != "--" else ""
        entry_reason_raw = (
            stock_dict.get("入池理由")
            or stock_dict.get("买入原因")
            or stock_dict.get("why_selected")
            or stock_dict.get("selection_reason")
            or ""
        )
        if not entry_reason_raw:
            detail = stock_dict.get("detail") if isinstance(stock_dict.get("detail"), dict) else {}
            if isinstance(detail, dict):
                p2_screener = detail.get("p2_screener")
                entry_reason_raw = (
                    detail.get("mainline_reason")
                    or (p2_screener.get("mainline_reason") if isinstance(p2_screener, dict) else "")
                )
        entry_reason = _esc(str(entry_reason_raw).strip()) if entry_reason_raw else ""

        now_str = _bj_now_str("%Y-%m-%d %H:%M:%S")
        header_time = f'<font color="comment">{_esc(now_str)}</font>'
        ts_short = _esc(_bj_now_str("%H:%M:%S"))
        title = f"{title} [{ts_short}]"
        lines = [
            title,
            "",
            header_time,
            "",
            f'- <font color="comment">代码</font>：<font color="comment">{code}</font>',
            f'- <font color="comment">名称</font>：{name}',
            f'- <font color="comment">现价</font>：<font color="comment">{price}</font>',
            f'- <font color="comment">涨幅</font>：<font color="comment">{pct_line}</font>',
            f'- <font color="comment">综合分</font>：<font color="comment">{score_line}</font>',
        ]
        if entry_reason:
            lines.append(f'- <font color="comment">入池理由</font>：<font color="warning">{entry_reason}</font>')
        if hot_sector_text:
            lines.append(f'- <font color="comment">热门板块加成</font>：<font color="warning">{hot_sector_text}</font>')
        lines.append(f'- <font color="comment">命中战法</font>：**{tactic}**')
        if pool_source == "直通车" or tier == "fastlane":
            lines.append('- <font color="warning">来源</font>：**直通车**')
        elif pool_name in {"P3", "P4"}:
            lines.append('- <font color="info">来源</font>：**底仓池**')
        if exec_tier:
            tier_text = exec_tier
            tier_map = {
                "A": "可以重点看",
                "B": "可以小仓跟踪",
                "C": "先观察为主",
            }
            tier_text = tier_map.get(exec_tier.upper(), exec_tier)
            lines.append(f'- <font color="comment">执行建议</font>：<font color="warning">{tier_text}</font>')
        if position_hint:
            pos_text = position_hint
            pos_map = {
                "主仓候选: 20%-30%（分批）": "可以按主仓思路分批看",
                "试错仓位: 8%-15%": "先小仓试一下",
                "观察仓位: 0%-8%": "先观察，不急着上",
                "主仓候选: 20%-30%": "可以按主仓思路分批看",
                "试错仓位: 8%-15%（轻仓）": "先小仓试一下",
                "观察仓位: 0%-8%（轻仓）": "先观察，不急着上",
            }
            pos_text = pos_map.get(position_raw, position_hint)
            lines.append(f'- <font color="comment">建议仓位</font>：<font color="warning">{pos_text}</font>')
        if risk_tags:
            lines.append(f'- <font color="comment">风险标签</font>：<font color="warning">{risk_tags}</font>')
        if buy_hint:
            buy_text = buy_hint_text
            buy_map = {
                "次日先看能否站稳关键位，确认后再考虑加仓。": "先看能不能站稳关键位，再决定要不要跟",
                "均线发散确认后再跟，先看承接是否持续。": "确认站稳后再跟，先看承接是否持续",
                "尾盘确认后可跟踪，先看次日能否站稳关键位。": "先看次日能不能站稳关键位，再决定要不要跟",
                "尾盘偏强但需防兑现，建议次日确认站稳再介入。": "先确认站稳，再考虑介入",
                "主线真龙可次日轻仓跟随，回踩VWAP或5日线确认后再加。": "先轻仓跟随，等回踩确认后再加",
            }
            buy_text = buy_map.get(buy_hint_text, buy_hint_text)
            lines.append(f'- <font color="comment">买入提示</font>：<font color="warning">{_esc(buy_text)}</font>')
        if tier == "observation" or "【缩量期备选】" in zhanfa:
            lines.append("")
            lines.append(
                '> <font color="warning">**【缩量观察池】** 非主池共识信号，高风险备选，请人工复核！</font>'
            )
        lines.append("")
        if buy_hint_text:
            buy_text = buy_hint_text
            buy_map = {
                "次日先看能否站稳关键位，确认后再考虑加仓。": "先看能不能站稳关键位，再决定要不要跟",
                "均线发散确认后再跟，先看承接是否持续。": "确认站稳后再跟，先看承接是否持续",
                "尾盘确认后可跟踪，先看次日能否站稳关键位。": "先看次日能不能站稳关键位，再决定要不要跟",
                "尾盘偏强但需防兑现，建议次日确认站稳再介入。": "先确认站稳，再考虑介入",
                "主线真龙可次日轻仓跟随，回踩VWAP或5日线确认后再加。": "先轻仓跟随，等回踩确认后再加",
            }
            buy_text = buy_map.get(buy_hint_text, buy_hint_text)
            lines.append(
                f'> <font color="warning">**买入提示：{_esc(buy_text)}**</font>'
            )
        lines.append(
            '> <font color="comment">温馨提示：炒股有风险，决策需谨慎；本消息仅作策略信号参考，不构成投资建议。</font>'
        )

        lines.append(_render_risk_card(stock_dict))

        body = "\n".join(lines)
        if len(body) > _MAX_MARKDOWN_CONTENT_LEN:
            body = body[: _MAX_MARKDOWN_CONTENT_LEN - 20] + "\n…(已截断)"
        return body
    except Exception as e:
        # 最后防线：仍返回一条可读的极简消息，避免调用方完全无内容可发
        logger.warning("format_wechat_markdown 降级为极简模板: %s", e, exc_info=True)
        code_f = _esc(_norm_ts_code(stock_dict.get("代码")) if isinstance(stock_dict, dict) else "--")
        return "\n".join(
            [
                "🚨 【信号】格式化降级",
                f"- 代码：{code_f}",
                "- 详情：字段异常已兜底，请至终端查看日志",
            ]
        )


__all__ = [
    "_safe_score_display",
    "_render_risk_card",
    "format_wechat_markdown",
]
