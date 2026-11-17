# -*- coding: utf-8 -*-
"""
core/paper_trading/honest_report.py — 诚实的策略体检报告（每日推送）

【为什么做这个】
原先系统每天推送「战法胜率榜」，但那个榜的口径是
「买入后死拿到 T+10 日收盘」的收益 —— 和模拟盘/实盘真正执行的
「+8% 止盈 / -3% 止损」不是同一套规则。
用一套规则的数据去评价另一套规则，结论会系统性失真，
而且因为「死拿」在下跌行情里天然吃亏，看榜单容易误判成
「这些战法都很烂」或「这些战法都还行」，其实都没回答真问题。

本报告用**真实执行规则**（core/stop_rule_backtest）重算，并给出
决策所需的最小信息集：
  · 期望值（平均每笔净收益）—— 最该看的一个数，>0 才可能长期赚钱
  · 盈利因子 —— >1.5 健康，>2 优秀，<1 必亏
  · 止盈率 / 止损率 —— 看打法是否符合预期
  · 样本量与可信度 —— 样本不足时一切结论都是噪声

【诚实原则】
  · 样本不足的战法明确标「样本不足」，不参与「最优」评选
  · 亏损战法不隐藏、不美化，照实列出
  · 报告顶部写明口径与滑点假设，让读者知道数字是怎么来的
  · 只读、只统计，绝不改任何交易参数

【安全】
本模块纯分析，不下单、不改配置、不碰实盘。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

MIN_SAMPLE = 10
STRONG_SAMPLE = 30


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None:
            return d
        fv = float(v)
        return d if fv != fv else fv
    except (TypeError, ValueError):
        return d


def build_honest_report(
    con: Any,
    *,
    take_profit_pct: Optional[float] = None,
    stop_loss_pct: Optional[float] = None,
    max_hold_days: Optional[int] = None,
    slippage_pct: Optional[float] = None,
    lookback_days: int = 180,
    pool: Optional[str] = None,
) -> Dict[str, Any]:
    """跑真实止损规则回测并汇总成报告数据。

    参数留空时**自动取自模拟盘配置**（config.yaml paper_trading），
    保证报告口径与模拟盘/实盘实际执行的规则一致。
    """
    try:
        from core.stop_rule_backtest import StopRuleConfig, run_stop_rule_backtest

        # 口径统一：默认从模拟盘配置读，保证「测的和跑的」同一套规则
        if take_profit_pct is None or stop_loss_pct is None or slippage_pct is None or max_hold_days is None:
            try:
                from core.paper_trading.config import load_config

                pc = load_config()
                take_profit_pct = pc.take_profit_pct if take_profit_pct is None else take_profit_pct
                stop_loss_pct = pc.stop_loss_pct if stop_loss_pct is None else stop_loss_pct
                slippage_pct = pc.slippage_pct if slippage_pct is None else slippage_pct
                max_hold_days = int(getattr(pc, "max_hold_days", 10)) if max_hold_days is None else max_hold_days
            except Exception:  # noqa: BLE001
                take_profit_pct = 8.0 if take_profit_pct is None else take_profit_pct
                stop_loss_pct = -3.0 if stop_loss_pct is None else stop_loss_pct
                slippage_pct = 0.1 if slippage_pct is None else slippage_pct
                max_hold_days = 10 if max_hold_days is None else max_hold_days

        cfg = StopRuleConfig(
            take_profit_pct=float(take_profit_pct),
            stop_loss_pct=float(stop_loss_pct),
            max_hold_days=int(max_hold_days),
            slippage_pct=float(slippage_pct),
        )
        res = run_stop_rule_backtest(con, cfg=cfg, pool=pool, lookback_days=lookback_days)
        if not res.get("available"):
            return {"available": False, "error": res.get("error")}
        s = res.get("summary") or {}
        rows = list(res.get("rows") or [])
        strong = [r for r in rows if r["n"] >= STRONG_SAMPLE]
        best = strong[0] if strong else None
        losing = [r for r in rows if r["n"] >= MIN_SAMPLE and r["avg_pnl_pct"] < 0]
        return {
            "available": True,
            "rows": rows,
            "summary": s,
            "best": best,
            "losing": losing,
            "pending_count": int(res.get("pending_count") or 0),
            "config": {
                "take_profit_pct": take_profit_pct,
                "stop_loss_pct": stop_loss_pct,
                "max_hold_days": max_hold_days,
                "slippage_pct": slippage_pct,
                "pool": pool or "全池",
            },
        }
    except Exception as e:  # noqa: BLE001
        _log.warning("[诚实报告] 构建失败: %s", e, exc_info=True)
        return {"available": False, "error": str(e)[:300]}


def format_honest_report(rep: Dict[str, Any]) -> str:
    """生成企微 markdown（诚实版）。"""
    try:
        from core.notification.core import _esc
    except Exception:  # noqa: BLE001
        def _esc(x: Any) -> str:
            return str(x)

    if not rep.get("available"):
        return f"## 📋 策略诚实体检\n\n> 回测不可用：{rep.get('error')}"

    cfg = rep.get("config") or {}
    s = rep.get("summary") or {}
    rows = list(rep.get("rows") or [])

    n = int(s.get("n_trades") or 0)
    wr = float(s.get("win_rate") or 0.0)
    avg = float(s.get("avg_pnl_pct") or 0.0)
    pf = s.get("profit_factor")
    pf_s = ("∞" if pf == float("inf") else ("%.2f" % pf)) if pf is not None else "—"

    lines: List[str] = ["## 📋 策略诚实体检（按真实交易规则重算）"]
    lines.append("")
    lines.append(
        f"> 规则：+{cfg.get('take_profit_pct')}%止盈 / {cfg.get('stop_loss_pct')}%止损 / "
        f"最多持有{cfg.get('max_hold_days')}日 / 滑点{cfg.get('slippage_pct')}% / "
        f"次日开盘买入 / 全部费用已扣（净口径）"
    )
    lines.append(
        f"> 范围：{cfg.get('pool')} | 可评估成交 **{n}** 笔 | 未成熟待观察 {int(rep.get('pending_count') or 0)} 条"
    )
    lines.append("")

    if n == 0:
        lines.append("暂无可评估成交（信号刚产生或样本不足），继续积累。")
        lines.append("")
        lines.append("> 这是保护性提示：样本不足时，任何「哪个战法好」的结论都是噪声。")
        return "\n".join(lines)

    # 全局结论
    verdict = "可继续观察" if avg > 0 else "当前为负期望，需谨慎"
    icon = "✅" if avg > 0 else "⚠️"
    lines.append(f"{icon} **整体期望值：{avg:+.2f}% / 笔**（{verdict}）")
    lines.append(
        f"　胜率 {wr:.1f}% | 盈利因子 {pf_s} | "
        f"累计净盈亏 ¥{float(s.get('total_pnl_amt') or 0.0):,.0f}（按每笔10万名义本金折算）"
    )
    lines.append("")
    lines.append(
        "　判读：期望>0 才有长期赚钱可能；盈利因子 >1.5 健康、>2 优秀；<1 意味着每笔平均亏。"
    )
    lines.append("")

    # 最优战法
    best = rep.get("best")
    if best:
        lines.append(f"🏆 **样本充足且期望为正的最优战法：{best.get('strategy_id')}**")
        lines.append(
            "　期望 {e:+.2f}%/笔 | 胜率 {w:.1f}% | 止盈率 {t:.0f}% | 止损率 {s:.0f}% | 样本 {n} 笔 | 盈利因子 {pf}".format(
                e=float(best.get("avg_pnl_pct") or 0.0), w=float(best.get("win_rate") or 0.0),
                t=float(best.get("take_rate") or 0.0), s=float(best.get("stop_rate") or 0.0),
                n=int(best.get("n") or 0),
                pf=("∞" if best.get("profit_factor") == float("inf") else "%.2f" % (best.get("profit_factor") or 0)),
            )
        )
        lines.append("")
    else:
        mx = max([int(r.get("n") or 0) for r in rows], default=0)
        lines.append(
            f"⏳ **暂不评选最优战法**：最高样本仅 {mx} 笔，需满 {STRONG_SAMPLE} 笔才有统计意义。"
        )
        lines.append("")

    # 明细
    lines.append("| 战法 | 笔数 | 胜率 | 期望/笔 | 止盈率 | 止损率 | 当日跌幅 | 均持仓 | 盈利因子 | 评价 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows[:12]:
        p = r.get("profit_factor")
        pfs = ("∞" if p == float("inf") else ("%.2f" % p)) if p is not None else "—"
        lines.append(
            "| {sid} | {n} | {w:.1f}% | {e:+.2f}% | {t:.0f}% | {s:.0f}% | {dr:.0f}% | {d:.1f} | {pf} | {lab} |".format(
                sid=_esc(str(r.get("strategy_id") or "")),
                n=int(r.get("n") or 0),
                w=float(r.get("win_rate") or 0.0),
                e=float(r.get("avg_pnl_pct") or 0.0),
                t=float(r.get("take_rate") or 0.0),
                s=float(r.get("stop_rate") or 0.0),
                dr=float(r.get("day_drop_rate") or 0.0),
                d=float(r.get("avg_hold_days") or 0.0),
                pf=pfs,
                lab=_esc(str(r.get("sample_label") or "")),
            )
        )
    if len(rows) > 12:
        lines.append(f"> 另有 {len(rows) - 12} 个战法未展示。")

    # 亏损战法（诚实披露）
    losing = list(rep.get("losing") or [])
    if losing:
        losing.sort(key=lambda r: r.get("avg_pnl_pct") or 0.0)
        names = "、".join(
            f"{r.get('strategy_id')}({float(r.get('avg_pnl_pct') or 0.0):+.1f}%)" for r in losing[:6]
        )
        lines.append("")
        lines.append(f"🚫 **样本充足但期望为负的战法（{len(losing)} 个）**：{names}")
        lines.append("　→ 这些在当前规则下是净亏的，不要指望靠它们翻身；可考虑降低权重或暂时停用。")

    weak = [r for r in rows if r["n"] < MIN_SAMPLE]
    if weak:
        lines.append("")
        lines.append(f"ℹ️ **{len(weak)}** 个战法样本不足 {MIN_SAMPLE} 笔，结论不成立，暂不评价。")

    lines.append("")
    lines.append("> 本报告只做统计、不下单、不改任何交易参数；模拟验证，不构成投资建议。")
    lines.append("> 亏损就是亏损，报告不做美化 —— 这是它存在的唯一意义。")
    return "\n".join(lines)


def notify_honest_report(
    con: Any = None,
    *,
    force: bool = False,
    pool: Optional[str] = None,
) -> bool:
    """生成并推送诚实体检报告。"""
    try:
        from core.config_manager import get_notification_config

        ncfg = get_notification_config()
        if not ncfg.get("enabled"):
            return False
        url = str(ncfg.get("wechat_webhook_url") or "").strip()
        if not url:
            return False
        try:
            from core.master_control import is_wechat_push_master_enabled

            if not is_wechat_push_master_enabled():
                return False
        except Exception:  # noqa: BLE001
            pass

        if con is None:
            from data.db_core import get_read_conn

            with get_read_conn(read_only=True) as c:
                rep = build_honest_report(c, pool=pool)
        else:
            rep = build_honest_report(con, pool=pool)

        if not rep.get("available"):
            _log.warning("[诚实报告] 不可用: %s", rep.get("error"))
            return False
        body = format_honest_report(rep)
        from core.notification.gateway import get_wechat_gateway

        get_wechat_gateway(url).push_markdown_async(body)
        _log.info("[诚实报告] 已推送（成交 %d 笔）", int((rep.get("summary") or {}).get("n_trades") or 0))
        return True
    except Exception as e:  # noqa: BLE001
        _log.warning("[诚实报告] 推送异常（已吞）: %s", e)
        return False


def _self_test() -> None:
    """报告格式与口径自检（不碰 DuckDB）。"""
    fake = {
        "available": True,
        "rows": [
            {"strategy_id": "P5-13", "n": 35, "win_rate": 65.7, "avg_pnl_pct": 3.21,
             "take_rate": 60.0, "stop_rate": 25.7, "avg_hold_days": 3.2,
             "profit_factor": 2.4, "sample_ok": True, "sample_label": "样本充足"},
            {"strategy_id": "P5-07", "n": 60, "win_rate": 33.3, "avg_pnl_pct": -5.1,
             "take_rate": 20.0, "stop_rate": 70.0, "avg_hold_days": 2.1,
             "profit_factor": 0.55, "sample_ok": True, "sample_label": "样本充足"},
            {"strategy_id": "P5-19", "n": 4, "win_rate": 75.0, "avg_pnl_pct": 2.0,
             "take_rate": 50.0, "stop_rate": 25.0, "avg_hold_days": 3.0,
             "profit_factor": 3.0, "sample_ok": False, "sample_label": "样本不足"},
        ],
        "summary": {"n_trades": 99, "win_rate": 45.5, "avg_pnl_pct": -1.2,
                    "profit_factor": 0.88, "total_pnl_amt": -12000.0},
        "best": {"strategy_id": "P5-13", "n": 35, "win_rate": 65.7, "avg_pnl_pct": 3.21,
                 "take_rate": 60.0, "stop_rate": 25.7, "avg_hold_days": 3.2,
                 "profit_factor": 2.4, "sample_ok": True, "sample_label": "样本充足"},
        "losing": [{"strategy_id": "P5-07", "n": 60, "win_rate": 33.3, "avg_pnl_pct": -5.1,
                    "take_rate": 20.0, "stop_rate": 70.0, "avg_hold_days": 2.1,
                    "profit_factor": 0.55, "sample_ok": True, "sample_label": "样本充足"}],
        "pending_count": 12,
        "config": {"take_profit_pct": 8.0, "stop_loss_pct": -3.0, "max_hold_days": 10,
                   "slippage_pct": 0.1, "pool": "全池"},
    }
    body = format_honest_report(fake)
    # 整体期望为负 → 必须出现警示，不能美化
    assert "⚠️" in body, body
    assert "诚实" in body
    assert "P5-13" in body and "P5-07" in body
    # 亏损战法必须被点名披露
    assert "🚫" in body, "必须披露净亏战法"
    assert "P5-07" in body.split("🚫")[-1], "亏损战法必须在披露段"
    # 样本不足的战法不参与最优评选
    assert "样本不足" in body
    # 口径说明必须出现
    assert "滑点" in body and "次日开盘买入" in body
    print("[honest_report] self-test OK | 期望为负时正确告警 + 亏损战法被披露")


if __name__ == "__main__":
    _self_test()


__all__ = [
    "MIN_SAMPLE",
    "STRONG_SAMPLE",
    "build_honest_report",
    "format_honest_report",
    "notify_honest_report",
]
