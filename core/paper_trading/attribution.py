# -*- coding: utf-8 -*-
"""
core/paper_trading/attribution.py — 按战法的收益归因报告 + 企微推送

【为什么需要】
用户的实验目标：用 1-2 个月模拟盘选出「最好的战法」。
因此不能只看总盈亏，必须能回答：
  · 哪个战法胜率最高？
  · 哪个战法平均单笔赚得多？
  · 哪个战法最容易止损（是废策还是只是回撤深）？
  · 哪个战法资金效率高（持仓天数少但收益高）？

【口径冻结】
  · 收益率一律「净」（含佣金 + 印花税 + 过户费），与 reporting.py 一致。
  · 胜率 = 净收益 > 0 的平仓笔数 / 总平仓笔数。
  · 止盈率 / 止损率：分别统计 TAKE_PROFIT / STOP_LOSS 占比，
    二者之和 < 100% 的部分为「持仓中/其它结束方式」。
  · 一个信号可能命中多个原子战法 → 各战法样本相互重叠，
    各战法累计盈亏「不可相加」；报告顶部必须明示这一点。
  · 样本数 < MIN_SAMPLE 的战法不参与「最佳战法」评选，只列在「样本不足」区。

【数据来源】
paper_order 表的 SELL 记录（已平仓），按 strategy_ids 原子战法聚合。
持仓中的票计入「信号数」但不计盈亏（避免用浮盈冒充已实现收益）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from core.paper_trading import store
from core.paper_trading.config import PaperTradingConfig, load_config

_log = logging.getLogger(__name__)

# 与 reporting.MIN_SAMPLE 保持一致：低于此样本量的战法不做可信度背书
MIN_SAMPLE = 10
# 参与「最佳战法」评选的最低样本量（比 MIN_SAMPLE 更严）
STRONG_SAMPLE = 20


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None:
            return d
        fv = float(v)
        return d if fv != fv else fv
    except (TypeError, ValueError):
        return d


def _split(raw: Any) -> List[str]:
    return [x.strip() for x in str(raw or "").split(",") if x.strip()]


def _median(values: List[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def build_attribution(
    *,
    con: Any = None,
    cfg: Optional[PaperTradingConfig] = None,
) -> Dict[str, Any]:
    """构建按战法的收益归因数据。任何异常吞掉，返回 available=False。"""
    c = cfg or load_config()
    try:
        if con is None:
            from data.db_core import get_read_conn

            with get_read_conn(read_only=True) as c2:
                return _build(c2, c)
        return _build(con, c)
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘归因] build_attribution 异常（已吞）: %s", e, exc_info=True)
        return {"available": False, "error": str(e)[:300], "rows": [], "best": None}


def _build(con: Any, cfg: PaperTradingConfig) -> Dict[str, Any]:
    closed = store.load_closed_positions(con)
    positions = store.load_positions(con)

    agg: Dict[str, Dict[str, Any]] = {}

    def _bucket(cid: str) -> Dict[str, Any]:
        if cid not in agg:
            agg[cid] = {
                "strategy_id": cid,
                "names": [],
                "pool": "",
                "closed": 0,
                "win": 0,
                "stop": 0,
                "take": 0,
                "day_drop": 0,
                "pnl": 0.0,
                "pnl_pcts": [],
                "pnls": [],
                "days": [],
                "holding": 0,
            }
        return agg[cid]

    for t in closed:
        ids = _split(t.get("strategy_ids"))
        names = _split(t.get("strategy_names"))
        name_map = dict(zip(ids, names))
        pnl = _f(t.get("pnl"), 0.0)
        pct = _f(t.get("pnl_pct"), 0.0)
        reason = str(t.get("exit_reason") or "")
        days = int(t.get("holding_days") or 0)
        for cid in ids:
            b = _bucket(cid)
            b["closed"] += 1
            b["pnl"] += pnl
            b["pnls"].append(pnl)
            b["pnl_pcts"].append(pct)
            b["days"].append(days)
            if pnl > 0:
                b["win"] += 1
            if reason == "STOP_LOSS":
                b["stop"] += 1
            elif reason == "TAKE_PROFIT":
                b["take"] += 1
            elif reason == "DAY_DROP":
                b["day_drop"] += 1
            b["pool"] = b["pool"] or str(t.get("pool") or "")
            lbl = name_map.get(cid) or cid
            if lbl and lbl not in b["names"]:
                b["names"].append(lbl)

    for p in positions:
        for cid in _split(p.get("strategy_ids")):
            b = _bucket(cid)
            b["holding"] += 1
            b["pool"] = b["pool"] or str(p.get("pool") or "")

    rows: List[Dict[str, Any]] = []
    for cid, b in agg.items():
        n = b["closed"]
        rows.append({
            "strategy_id": cid,
            "strategy_names": "/".join(b["names"]) or cid,
            "pool": b["pool"] or "—",
            "closed_count": n,
            "holding_count": b["holding"],
            "signal_count": n + b["holding"],
            "win_rate": (b["win"] / n * 100.0) if n > 0 else 0.0,
            "stop_rate": (b["stop"] / n * 100.0) if n > 0 else 0.0,
            "take_rate": (b["take"] / n * 100.0) if n > 0 else 0.0,
            "day_drop_rate": (b["day_drop"] / n * 100.0) if n > 0 else 0.0,
            "total_pnl": b["pnl"],
            "avg_pnl": (b["pnl"] / n) if n > 0 else 0.0,
            "median_pnl": _median(b["pnls"]),
            "avg_pnl_pct": (sum(b["pnl_pcts"]) / n) if n > 0 else 0.0,
            "avg_holding_days": (sum(b["days"]) / n) if n > 0 else 0.0,
            "sample_ok": n >= MIN_SAMPLE,
            "sample_label": ("样本充足" if n >= STRONG_SAMPLE else ("样本中等" if n >= MIN_SAMPLE else "样本不足")),
        })

    # 排序：样本足够的优先，其次按胜率，再按累计盈亏
    rows.sort(key=lambda r: (r["sample_ok"], r["win_rate"], r["total_pnl"]), reverse=True)
    # 【重要】最佳战法只在样本充足（≥STRONG_SAMPLE）时评选。
    # 否则会出现「只赢了1笔的战法被标为最优」这种误导性结论 ——
    # 1-2 个月的实验里，这正是最容易让人选错战法的地方。
    strong = [r for r in rows if r["closed_count"] >= STRONG_SAMPLE]
    best = strong[0] if strong else None

    realized = sum(_f(t.get("pnl"), 0.0) for t in closed)
    return {
        "available": True,
        "rows": rows,
        "best": best,
        "total_closed": len(closed),
        "total_positions": len(positions),
        "realized_pnl": realized,
        "min_sample": MIN_SAMPLE,
        "strong_sample": STRONG_SAMPLE,
    }


def format_attribution_markdown(attr: Dict[str, Any]) -> str:
    """把归因数据格式化成企微 markdown。"""
    try:
        from core.notification.core import _esc
    except Exception:  # noqa: BLE001
        def _esc(x: Any) -> str:
            return str(x)

    try:
        from core.paper_trading.strategy_labels import strategy_label

        def _label(cid: str) -> str:
            try:
                return strategy_label(cid)
            except Exception:  # noqa: BLE001
                return ""
    except Exception:  # noqa: BLE001
        def _label(cid: str) -> str:
            return ""

    rows = list((attr or {}).get("rows") or [])
    lines: List[str] = ["## 📊 模拟盘战法归因报告"]
    lines.append("")
    lines.append(
        f"> 已平仓 **{int(attr.get('total_closed') or 0)}** 笔 | "
        f"持仓中 **{int(attr.get('total_positions') or 0)}** 只 | "
        f"累计已实现盈亏 **¥{float(attr.get('realized_pnl') or 0.0):,.2f}**"
    )
    lines.append(">")
    lines.append("> 各战法样本可能重叠（一个信号命中多个战法），**累计盈亏不可相加**。")
    lines.append("")

    if not rows:
        lines.append("暂无已平仓交易，实验进行中（平仓后开始有统计）。")
        return "\n".join(lines)

    best = attr.get("best")
    if best and int(best.get("closed_count") or 0) >= MIN_SAMPLE:
        lab = _label(str(best.get("strategy_id") or "")) or str(best.get("strategy_names") or "")
        lines.append(f"🏆 **当前最优战法：{best.get('strategy_id')} {lab}**")
        lines.append(
            f"　胜率 {float(best.get('win_rate') or 0.0):.1f}% | "
            f"均笔 ¥{float(best.get('avg_pnl') or 0.0):,.0f} | "
            f"止盈率 {float(best.get('take_rate') or 0.0):.0f}% | "
            f"止损率 {float(best.get('stop_rate') or 0.0):.0f}% | "
            f"当日跌幅 {float(best.get('day_drop_rate') or 0.0):.0f}% | "
            f"样本 {int(best.get('closed_count') or 0)} 笔"
        )
        lines.append("")
    else:
        max_n = max([int(r.get("closed_count") or 0) for r in rows], default=0)
        lines.append(
            f"⏳ **样本积累中**：目前最多战法仅 {max_n} 笔平仓，"
            f"需满 {int(attr.get('strong_sample') or STRONG_SAMPLE)} 笔才评选「最优战法」"
            "（样本太少时胜率纯属噪声，易选错）。"
        )
        lines.append("")

    lines.append("### 战法明细（按可信度排序）")
    lines.append("")
    lines.append("| 战法 | 笔数 | 胜率 | 均笔盈亏 | 累计盈亏 | 止盈率 | 止损率 | 当日跌幅 | 均持仓天 | 评价 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows[:15]:
        cid = str(r.get("strategy_id") or "")
        lab = _label(cid)
        # 标签本身就是编号（strategy_names 回填成了编号）时不重复展示
        if lab.strip() == cid.strip():
            lab = ""
        if not lab:
            lab = str(r.get("strategy_names") or "")
            if lab.strip() == cid.strip():
                lab = ""
        lines.append(
            "| {cid} {lab} | {n} | {wr:.0f}% | {ap:,.0f} | {tp:,.0f} | "
            "{tr:.0f}% | {sr:.0f}% | {dr:.0f}% | {hd:.1f} | {sl} |".format(
                cid=_esc(cid),
                lab=_esc(lab),
                n=int(r.get("closed_count") or 0),
                wr=float(r.get("win_rate") or 0.0),
                ap=float(r.get("avg_pnl") or 0.0),
                tp=float(r.get("total_pnl") or 0.0),
                tr=float(r.get("take_rate") or 0.0),
                sr=float(r.get("stop_rate") or 0.0),
                dr=float(r.get("day_drop_rate") or 0.0),
                hd=float(r.get("avg_holding_days") or 0.0),
                sl=_esc(str(r.get("sample_label") or "")),
            )
        )
    if len(rows) > 15:
        lines.append(f"> 另有 {len(rows) - 15} 个战法未展示。")

    insufficient = [r for r in rows if not r["sample_ok"]]
    if insufficient:
        lines.append("")
        lines.append(
            f"ℹ️ 其中 **{len(insufficient)}** 个战法样本不足 {MIN_SAMPLE} 笔，"
            "胜率波动大，暂不可作为选策依据（继续积累）。"
        )
    lines.append("")
    lines.append("> 模拟盘统计，仅供策略验证参考，不构成投资建议")
    return "\n".join(lines)


def notify_attribution_report(
    *,
    cfg: Optional[PaperTradingConfig] = None,
    force: bool = False,
) -> bool:
    """生成并推送战法归因报告。返回是否推送成功（是否真的发了）。"""
    c = cfg or load_config()
    try:
        if not c.enabled:
            return False
        if not force and not bool(getattr(c, "notify_attribution", True)):
            return False
        try:
            from core.config_manager import get_notification_config

            ncfg = get_notification_config()
            if not ncfg.get("enabled"):
                return False
            url = str(ncfg.get("wechat_webhook_url") or "").strip()
            if not url:
                return False
        except Exception as e:  # noqa: BLE001
            _log.debug("[模拟盘归因] 读取通知配置失败: %s", e)
            return False
        try:
            from core.master_control import is_wechat_push_master_enabled

            if not is_wechat_push_master_enabled():
                return False
        except Exception:  # noqa: BLE001
            pass

        attr = build_attribution(cfg=c)
        if not attr.get("available"):
            return False
        body = format_attribution_markdown(attr)
        from core.notification.gateway import get_wechat_gateway

        get_wechat_gateway(url).push_markdown_async(body)
        _log.info(
            "[模拟盘归因] 已推送战法归因报告（已平仓 %d 笔）",
            int(attr.get("total_closed") or 0),
        )
        return True
    except Exception as e:  # noqa: BLE001
        _log.warning("[模拟盘归因] 推送异常（已吞）: %s", e)
        return False


__all__ = [
    "MIN_SAMPLE",
    "STRONG_SAMPLE",
    "build_attribution",
    "format_attribution_markdown",
    "notify_attribution_report",
]
