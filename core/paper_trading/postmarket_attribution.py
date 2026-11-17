# -*- coding: utf-8 -*-
"""
core/paper_trading/postmarket_attribution.py — 盘后（P5）等各池的「战法效果榜」

【目的】
用户的实验目标：用 1-2 个月模拟盘选出最好的策略和战法。
盘后选股（P5）与盘中（P3/P4）是完全不同的逻辑，必须分开统计，
否则盘中战法的高频样本会淹没盘后战法的样本。

【为什么不用模拟盘口径】
模拟盘只记录「实际买入并平仓」的样本 → 样本少（每天 4 笔），
且受仓位/止盈止损约束。战法优选需要的是「信号层面的正确率」：
所有被选中的信号，后来涨了没有？—— 这由 signal_log / signal_pnl /
strategy_effectiveness 三张表承载，覆盖全量信号（不管有没有买）。

【数据来源】
  signal_log            每条信号（含战法编号、得分、推送价）
  signal_pnl            每条信号的 T+1/T+2/T+5/T+10 收益（后验）
  strategy_effectiveness 按 (pool, strategy) 聚合的胜率矩阵（每天 18:30 重算）
  signal_strategy_ledger 信号↔战法的多对多账本

【口径冻结】
  · 胜率一律「净口径」（net_win_rate，扣佣金+印花税+过户费）。
  · 主档位取 primary_n_days（当前是 T+10）；同时展示 T+1/T+5 供参考。
  · 样本数 < MIN_SAMPLE 不标「可信」，不参与「最优」评选。
  · 展示「超额」= 相对混合基准（沪深300+深成+创业板+上证），回答
    「这战法是真的选得好，还是只是大盘涨了」——这是选股战法最容易被
    忽略的陷阱：牛市里随便选都赚。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

# 可信样本下限
MIN_SAMPLE = 10
# 参与「最优战法」评选的样本下限（更严）
STRONG_SAMPLE = 20

# 【P0 修复 2026-11-13】红牌门槛：样本充足却净收益为负 → 榜上直接打标停用。
# 为什么不靠「读者自己看负号」：榜单每天推送，读者会默认「上榜 = 可用」。
# 门槛保守（30 条 + 均笔 ≤ -1%），避免样本不足就误杀战法。
RED_CARD_MIN_SIGNALS = 30
RED_CARD_MAX_RETURN = -1.0

# 池 → 中文名（推送展示用）
POOL_LABELS = {
    "p1": "P1 底仓",
    "p2": "P2 竞价",
    "p3": "P3 盘中",
    "p4": "P4 尾盘",
    "p5": "P5 盘后",
}

# 池 → 展示名（突出盘后）
POOL_TITLES = {
    "p3": "盘中战法榜",
    "p4": "尾盘战法榜",
    "p5": "盘后战法榜",
}


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None:
            return d
        fv = float(v)
        return d if fv != fv else fv
    except (TypeError, ValueError):
        return d


def build_pool_leaderboard(
    *,
    pool: str = "p5",
    con: Any = None,
) -> Dict[str, Any]:
    """构建指定池的战法效果榜。返回 available=False 表示不可用。"""
    try:
        if con is not None:
            try:
                return _build(con, pool)
            except Exception as e_shared:  # noqa: BLE001
                # 【V30.18 2026-11-10】DuckDB 单连接不支持并发 cursor：调用方传入的
                # 共享连接若正被另一线程执行查询，会抛
                # "Resource In Use Error: connection is busy with another call"，
                # 导致 p4 战法榜整榜跳过（2026-11-10 20:16 实测）。战法榜是只读统计，
                # 不值得为一个共享连接被占用就放弃 → 换独立只读连接重试一次。
                _msg = str(e_shared).lower()
                if "busy" not in _msg and "resource in use" not in _msg:
                    raise
                _log.info("[战法榜] pool=%s 共享连接被占用，改用独立只读连接重试", pool)
        from data.db_core import get_read_conn

        with get_read_conn(read_only=True) as c:
            return _build(c, pool)
    except Exception as e:  # noqa: BLE001
        _log.warning("[战法榜] 构建失败 pool=%s: %s", pool, e)
        return {"available": False, "error": str(e)[:200], "rows": [], "pool": pool}


def _build(con: Any, pool: str) -> Dict[str, Any]:
    pool = str(pool or "p5").strip().lower()
    # 取该池每个战法的最新一条统计（stat_date 最大）
    rows_raw = con.execute(
        """
        SELECT strategy, strategy_label, stat_date, n_signals, n_evaluated, n_win,
               win_rate, net_win_rate, avg_return, avg_net_return,
               avg_excess, avg_net_excess, avg_net_excess_blend, beat_blend_rate,
               stat_n_days, primary_n_days, available_n_days,
               t1_win_rate, t2_win_rate, t5_win_rate, t10_win_rate
        FROM strategy_effectiveness
        WHERE pool = ?
        QUALIFY ROW_NUMBER() OVER (
            PARTITION BY strategy ORDER BY stat_date DESC, stat_n_days DESC
        ) = 1
        ORDER BY n_signals DESC
        """,
        [pool],
    ).fetchall()
    if not rows_raw:
        return {
            "available": True,
            "pool": pool,
            "rows": [],
            "best": None,
            "note": "暂无该池的战法统计数据（可能尚未产生信号或后验未成熟）",
        }

    cols = [
        "strategy", "strategy_label", "stat_date", "n_signals", "n_evaluated", "n_win",
        "win_rate", "net_win_rate", "avg_return", "avg_net_return",
        "avg_excess", "avg_net_excess", "avg_net_excess_blend", "beat_blend_rate",
        "stat_n_days", "primary_n_days", "available_n_days",
        "t1_win_rate", "t2_win_rate", "t5_win_rate", "t10_win_rate",
    ]
    rows: List[Dict[str, Any]] = []
    for r in rows_raw:
        d = {c: r[i] for i, c in enumerate(cols)}
        cid = str(d.get("strategy") or "")
        if not cid:
            continue
        n = int(_f(d.get("n_signals"), 0))
        # 净胜率优先；缺失时回落毛胜率
        net_wr = d.get("net_win_rate")
        wr = _f(net_wr if net_wr is not None else d.get("win_rate"), 0.0)
        rows.append({
            "strategy_id": cid,
            "strategy_label": str(d.get("strategy_label") or ""),
            "stat_date": str(d.get("stat_date") or ""),
            "n_signals": n,
            "n_evaluated": int(_f(d.get("n_evaluated"), 0)),
            "n_win": int(_f(d.get("n_win"), 0)),
            "win_rate": wr * 100.0,
            # 【口径】return_pct / excess_pct 在 signal_pnl 里已按百分数存储
            #   （(eval_close - push_price) / push_price * 100.0），此处不可再乘 100。
            "avg_return": _f(d.get("avg_net_return"), _f(d.get("avg_return"), 0.0)),
            "avg_excess": _f(d.get("avg_net_excess"), _f(d.get("avg_excess"), 0.0)),
            "avg_excess_blend": _f(d.get("avg_net_excess_blend"), 0.0),
            # 【口径】beat_blend_rate 在库里已是百分数（_beat_rate 内部 *100），不可再乘。
            "beat_blend_rate": _f(d.get("beat_blend_rate"), 0.0),
            "stat_n_days": int(_f(d.get("stat_n_days"), 0)),
            "primary_n_days": int(_f(d.get("primary_n_days"), 0)),
            "t1_win_rate": (_f(d.get("t1_win_rate"), 0.0) * 100.0) if d.get("t1_win_rate") is not None else None,
            "t5_win_rate": (_f(d.get("t5_win_rate"), 0.0) * 100.0) if d.get("t5_win_rate") is not None else None,
            "sample_ok": n >= MIN_SAMPLE,
            "sample_label": ("样本充足" if n >= STRONG_SAMPLE else ("样本中等" if n >= MIN_SAMPLE else "样本不足")),
        })

    # 排序：样本够的优先 → 净胜率 → 累计超额
    rows.sort(key=lambda r: (r["sample_ok"], r["win_rate"], r["avg_excess"]), reverse=True)

    # 【P0 修复 2026-11-13】红牌：样本充足却净收益为负的战法，榜上直接打标。
    # 为什么要落在这一层：这是用户每天真正读的那份榜（18:45 / 09:05 推送），
    # 亏损战法不标出来，读者会默认「上榜 = 可用」。
    # 门槛用 n_signals >= RED_CARD_MIN_SIGNALS 且 avg_return <= RED_CARD_MAX_RETURN。
    red_cards: List[Dict[str, Any]] = []
    for r in rows:
        if int(r["n_signals"]) >= RED_CARD_MIN_SIGNALS and _f(r["avg_return"], 0.0) <= RED_CARD_MAX_RETURN:
            r["red_card"] = True
            r["red_card_reason"] = (
                f"样本 {int(r['n_signals'])} 条且均笔净收益 {_f(r['avg_return']):+.2f}%"
                f"（≤{RED_CARD_MAX_RETURN}%），停用"
            )
            red_cards.append({
                "strategy_id": r["strategy_id"],
                "strategy_label": r["strategy_label"],
                "n_signals": int(r["n_signals"]),
                "avg_return": round(_f(r["avg_return"]), 2),
                "win_rate": round(_f(r["win_rate"]), 1),
                "reason": r["red_card_reason"],
            })
        else:
            r["red_card"] = False
            r["red_card_reason"] = ""
    red_cards.sort(key=lambda x: x["avg_return"])

    # 「最优」必须排除红牌：样本够但净亏的战法绝不可能是最优
    strong = [r for r in rows if r["n_signals"] >= STRONG_SAMPLE and not r["red_card"]
              and _f(r["avg_return"], 0.0) > 0]
    best = strong[0] if strong else None

    return {
        "available": True,
        "pool": pool,
        "pool_label": POOL_LABELS.get(pool, pool.upper()),
        "rows": rows,
        "best": best,
        "red_cards": red_cards,
        "total_signals": sum(r["n_signals"] for r in rows),
        "min_sample": MIN_SAMPLE,
        "strong_sample": STRONG_SAMPLE,
        "stat_date": rows[0]["stat_date"] if rows else "",
    }


def format_leaderboard_markdown(lb: Dict[str, Any]) -> str:
    """把战法榜格式化成企微 markdown。"""
    try:
        from core.notification.core import _esc
    except Exception:  # noqa: BLE001
        def _esc(x: Any) -> str:
            return str(x)

    try:
        from core.paper_trading.strategy_labels import strategy_label

        def _lab(cid: str, raw: str) -> str:
            t = strategy_label(cid)
            if t and not t.startswith("策略信号触发") and not t.startswith("历史战法"):
                return t
            return raw or ""
    except Exception:  # noqa: BLE001
        def _lab(cid: str, raw: str) -> str:
            return raw or ""

    if not lb.get("available"):
        return f"## 📊 {POOL_LABELS.get(lb.get('pool') or '', '战法')}效果榜\n\n> 数据读取失败：{lb.get('error')}"

    pool = str(lb.get("pool") or "")
    title = POOL_TITLES.get(pool, f"{POOL_LABELS.get(pool, pool.upper())}效果榜")
    rows = list(lb.get("rows") or [])

    lines: List[str] = [f"## 📊 {title}"]
    lines.append("")
    if not rows:
        lines.append(str(lb.get("note") or "暂无数据"))
        return "\n".join(lines)

    stat_date = str(lb.get("stat_date") or "")
    lines.append(
        f"> 统计日 {stat_date} | 战法 **{len(rows)}** 个 | 覆盖信号 **{int(lb.get('total_signals') or 0)}** 条"
    )
    lines.append("> 口径：净胜率（扣费后），主档位 T+%s" % (rows[0].get("primary_n_days") or 10))
    lines.append("")

    best = lb.get("best")
    if best:
        lab = _lab(str(best.get("strategy_id") or ""), str(best.get("strategy_label") or ""))
        lines.append(f"🏆 **当前最优：{best.get('strategy_id')} {lab}**")
        lines.append(
            "　胜率 {wr:.1f}% | 均笔 {ar:+.2f}% | 超额 {ae:+.2f}% | 样本 {n} 条".format(
                wr=float(best.get("win_rate") or 0.0),
                ar=float(best.get("avg_return") or 0.0),
                ae=float(best.get("avg_excess") or 0.0),
                n=int(best.get("n_signals") or 0),
            )
        )
        lines.append("")
    else:
        mx = max([int(r.get("n_signals") or 0) for r in rows], default=0)
        lines.append(
            f"⏳ **样本积累中**：最高样本仅 {mx} 条，需满 {int(lb.get('strong_sample') or STRONG_SAMPLE)} 条才评选最优战法"
            "（样本太少时胜率是噪声，易选错）。"
        )
        lines.append("")

    lines.append("| 战法 | 样本 | 胜率 | 均笔 | 超额 | 跑赢大盘 | T+1胜率 | T+5胜率 | 评价 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows[:12]:
        cid = str(r.get("strategy_id") or "")
        lab = _lab(cid, str(r.get("strategy_label") or ""))
        t1 = r.get("t1_win_rate")
        t5 = r.get("t5_win_rate")
        # 【P0 修复 2026-11-13】红牌战法在榜上直接标 🚫，评价列替换成停用理由。
        # 只在「样本充足且净亏」时出现，读者一眼就能看出「这个不该再用」。
        if r.get("red_card"):
            cid_cell = f"🚫 {cid} {lab}".strip()
            label_cell = "**红牌停用**"
        else:
            cid_cell = f"{cid} {lab}".strip()
            label_cell = str(r.get("sample_label") or "")
        lines.append(
            "| {cid} | {n} | {wr:.0f}% | {ar:+.1f}% | {ae:+.1f}% | {bb:.0f}% | {t1} | {t5} | {sl} |".format(
                cid=_esc(cid_cell),
                n=int(r.get("n_signals") or 0),
                wr=float(r.get("win_rate") or 0.0),
                ar=float(r.get("avg_return") or 0.0),
                ae=float(r.get("avg_excess") or 0.0),
                bb=float(r.get("beat_blend_rate") or 0.0),
                t1=("%.0f%%" % t1) if t1 is not None else "—",
                t5=("%.0f%%" % t5) if t5 is not None else "—",
                sl=_esc(label_cell),
            )
        )
    if len(rows) > 12:
        lines.append(f"> 另有 {len(rows) - 12} 个战法未展示。")

    # 【P0 修复 2026-11-13】红牌战法单独成段披露（不藏在表格里）。
    # 表格只显示前 12 行，红牌可能正好被截断；这一段保证「停用了哪些」永远不会漏。
    reds = list(lb.get("red_cards") or [])
    if reds:
        lines.append("")
        lines.append(f"🚫 **红牌停用（{len(reds)} 个）：样本充足但净亏**")
        for c in reds[:8]:
            lab = _lab(str(c.get("strategy_id") or ""), str(c.get("strategy_label") or ""))
            lines.append(
                "　· {sid} {lab}：均笔 {ar:+.2f}%，{n} 条信号，胜率 {wr:.0f}%".format(
                    sid=_esc(str(c.get("strategy_id") or "")),
                    lab=_esc(lab),
                    ar=float(c.get("avg_return") or 0.0),
                    n=int(c.get("n_signals") or 0),
                    wr=float(c.get("win_rate") or 0.0),
                )
            )
        lines.append(
            f"　→ 判定线：样本 ≥{RED_CARD_MIN_SIGNALS} 条且均笔净收益 ≤{RED_CARD_MAX_RETURN}%。"
            "这些战法当前是净亏的，别再指望靠它们翻身。"
        )

    weak = [r for r in rows if not r["sample_ok"]]
    if weak:
        lines.append("")
        lines.append(
            f"ℹ️ **{len(weak)}** 个战法样本不足 {MIN_SAMPLE} 条，胜率波动大，暂不作选策依据。"
        )
    lines.append("")
    lines.append("> 说明：超额=相对沪深300/深成/创业板混合基准；仅看绝对收益会被大盘涨跌误导。")
    lines.append("> 模拟统计，仅供策略验证参考，不构成投资建议")
    return "\n".join(lines)


def notify_pool_leaderboard(
    *,
    pool: str = "p5",
    force: bool = False,
) -> bool:
    """构建并推送某池的战法效果榜。返回是否推送成功。"""
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

        lb = build_pool_leaderboard(pool=pool)
        if not lb.get("available"):
            return False
        body = format_leaderboard_markdown(lb)
        from core.notification.gateway import get_wechat_gateway

        get_wechat_gateway(url).push_markdown_async(body)
        _log.info(
            "[战法榜] 已推送 %s 战法效果榜（战法 %d 个 / 信号 %d 条）",
            pool, len(lb.get("rows") or []), int(lb.get("total_signals") or 0),
        )
        return True
    except Exception as e:  # noqa: BLE001
        _log.warning("[战法榜] 推送异常 pool=%s: %s", pool, e)
        return False


def _self_test() -> None:
    """战法榜聚合纯逻辑自检（不碰 DuckDB）。"""
    # 口径：win_rate 是 0~1 比例；avg_return / avg_excess / beat_blend_rate 已是百分数
    FAKE = [
        ("P5-02", "★量价放量", "2026-11-04", 45, 45, 30, 0.667, 0.644,
         2.90, 2.87, 1.10, 1.05, 0.95, 71.0, 10, 10, "[1,2,5,10]",
         0.71, 0.69, 0.68, 0.667),
        ("P5-19", "★均线发散确认", "2026-11-04", 8, 8, 6, 0.75, 0.75,
         4.10, 4.05, 1.90, 1.85, 1.70, 88.0, 10, 10, "[1,2,5,10]",
         0.75, 0.75, 0.75, 0.75),
        # 【P0 修复 2026-11-13】样本充足（40 条）但净亏 -3.5% → 必须红牌
        ("P5-07", "★趋势质量", "2026-11-04", 40, 40, 6, 0.15, 0.15,
         -3.20, -3.50, -4.10, -4.30, -4.20, 12.0, 10, 10, "[1,2,5,10]",
         0.18, 0.16, 0.15, 0.15),
    ]

    class _Cur:
        def fetchall(self):
            return FAKE

    class _Con:
        def execute(self, sql, params=None):
            return _Cur()

    lb = _build(_Con(), "p5")
    assert lb["available"] is True
    assert len(lb["rows"]) == 3, lb["rows"]
    r = {x["strategy_id"]: x for x in lb["rows"]}
    # 胜率：0.644 → 64.4%
    assert abs(r["P5-02"]["win_rate"] - 64.4) < 0.2, r["P5-02"]
    # 均笔 / 超额：已是百分数，不再放大
    assert abs(r["P5-02"]["avg_return"] - 2.87) < 0.01, r["P5-02"]
    assert abs(r["P5-02"]["avg_excess"] - 1.05) < 0.01, r["P5-02"]
    # 跑赢大盘：已是百分数，不再放大
    assert abs(r["P5-02"]["beat_blend_rate"] - 71.0) < 0.5, r["P5-02"]
    # T+1/T+5 胜率：0~1 比例 → 百分数
    assert abs(r["P5-02"]["t1_win_rate"] - 71.0) < 0.5, r["P5-02"]
    # 样本门槛
    assert r["P5-02"]["sample_ok"] is True
    assert r["P5-19"]["sample_ok"] is False
    # 最优战法只从样本充足里选
    assert lb["best"]["strategy_id"] == "P5-02", lb["best"]

    # 【P0 修复 2026-11-13】红牌回归：样本充足但净亏的战法必须被打标并排除出「最优」
    assert r["P5-07"]["red_card"] is True, r["P5-07"]
    assert r["P5-02"]["red_card"] is False
    assert r["P5-19"]["red_card"] is False, "样本不足不得红牌"
    assert len(lb["red_cards"]) == 1, lb["red_cards"]
    assert lb["red_cards"][0]["strategy_id"] == "P5-07"
    assert lb["best"]["strategy_id"] != "P5-07"

    body = format_leaderboard_markdown(lb)
    assert "盘后战法榜" in body and "P5-02" in body
    # 红牌必须在正文里显式披露（表格可能截断，独立段落保证不漏）
    assert "红牌停用" in body, body
    assert "🚫" in body, body
    # 净亏战法绝不能被评成「当前最优」：最优段（到"判读"/表格之前）里不得出现它
    best_seg = body.split("当前最优", 1)[-1].split("| 战法 |", 1)[0]
    assert "P5-07" not in best_seg, ("净亏战法不得被选为最优", best_seg)
    # 但它在表格里必须出现（带 🚫 标记），而不是被静默隐藏
    assert "🚫 P5-07" in body, ("红牌战法必须可见", body)
    print("[postmarket_attribution] self-test OK | leaderboard aggregation & scales & red-cards verified")


if __name__ == "__main__":
    _self_test()


__all__ = [
    "MIN_SAMPLE",
    "STRONG_SAMPLE",
    "POOL_LABELS",
    "POOL_TITLES",
    "build_pool_leaderboard",
    "format_leaderboard_markdown",
    "notify_pool_leaderboard",
]
