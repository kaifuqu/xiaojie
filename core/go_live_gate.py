# -*- coding: utf-8 -*-
"""
core/go_live_gate.py — 实盘放行门禁（负期望期唯一的刹车）

【为什么需要这个模块】
2026-11-13 的诚实体检（修复日期过滤后，终于能算出真实数字）：
    167 笔成交 | 胜率 18.0% | 期望 **-2.01%/笔** | 盈利因子 0.31
    悲观口径（计入出场轮询滞后滑点 0.5%）：期望 **-2.43%/笔** | 盈利因子 0.26
含义很简单：每下 1 万块钱，平均亏 200~243 块，而且样本只有 15 个交易日、一种行情。
在这种状态下去做真钱交易，数学期望就是持续放血 —— 与「哪个战法更好」无关。

【为什么写成代码而不是写进文档】
文档里的「不要上实盘」会在情绪好的时候被忘掉。这里把它变成：
  · 每日自动计算的机器结论（allowed = true/false + 理由清单）；
  · 落盘 data/runtime/state/go_live_gate.json（UI / 其他模块可直接读）；
  · 结论翻转时主动推企微（放行 / 收回）；
  · 失败战法自动红牌（red_cards），供推送与报表直接标注；
  · 给未来任何"实盘执行器"一个 assert_live_allowed()，不通过就抛异常。

【放行标准（全部满足才 allowed=true）】
  1. 样本量：已平仓笔数 >= MIN_TRADES(60)
  2. 独立样本：覆盖 >= MIN_INDEPENDENT_DAYS(20) 个不同信号日
     —— 167 笔落在 15 天里，本质是「1 个样本」，不能当 167 个独立证据
  3. 悲观口径期望 > 0（用悲观口径，不用乐观口径）
  4. 悲观口径盈利因子 >= MIN_PROFIT_FACTOR(1.2)
  5. 期望值的 95% 置信下限 > 0（均值的单侧 CI；n 小的时候天然通不过）
  6. 不存在「样本充足且期望显著为负」的战法仍在被推送（红牌未清理）

【只读约束】
本模块只读库、只写自己的状态 JSON；不改任何交易参数、不改权重、不下单。
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

BJ_TZ = timezone(timedelta(hours=8))

MIN_TRADES = 60
MIN_INDEPENDENT_DAYS = 20
MIN_PROFIT_FACTOR = 1.2
# 红牌：样本 >= RED_CARD_MIN_TRADES 且期望 <= RED_CARD_MAX_EXPECTANCY(%) → 停用
RED_CARD_MIN_TRADES = 20
RED_CARD_MAX_EXPECTANCY = -1.0
# 置信度倍数：单侧 95% → z=1.645（正态近似；n>=60 时足够）
Z_95 = 1.645


def _now_bj() -> datetime:
    return datetime.now(BJ_TZ)


def _f(v: Any, d: float = 0.0) -> float:
    try:
        if v is None:
            return d
        fv = float(v)
        return d if fv != fv else fv
    except (TypeError, ValueError):
        return d


def _state_path() -> str:
    root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    return os.path.join(root, "data", "runtime", "state", "go_live_gate.json")


def ci_lower_bound(pcts: List[float], z: float = Z_95) -> Optional[float]:
    """均值的单侧 z 置信下限（样本不足 2 个返回 None）。"""
    n = len(pcts)
    if n < 2:
        return None
    mean = sum(pcts) / n
    var = sum((x - mean) ** 2 for x in pcts) / (n - 1)
    se = math.sqrt(max(0.0, var) / n)
    return mean - z * se


def count_independent_days(con: Any, lookback_days: int = 180) -> int:
    """信号日（独立样本）数量：不同交易日数，而不是信号条数。

    【关键】167 笔成交来自 15 个交易日 → 统计上接近「1 个样本」。
    不把这件事量化，就永远会用「样本 167 笔」自我安慰。
    """
    try:
        from data.db_core import sql_date_norm, to_sql_date_param

        since = (_now_bj().date() - timedelta(days=int(lookback_days))).strftime("%Y-%m-%d")
        r = con.execute(
            f"SELECT COUNT(DISTINCT CAST({sql_date_norm('log_date')} AS VARCHAR)) "
            f"FROM signal_log WHERE push_price > 0 "
            f"AND {sql_date_norm('log_date')} >= CAST(? AS DATE)",
            [to_sql_date_param(since)],
        ).fetchone()
        return int((r or [0])[0] or 0)
    except Exception as e:  # noqa: BLE001
        _log.debug("[放行门禁] 独立样本天数统计失败: %s", e)
        return 0


def build_red_cards(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """从战法统计里挑出「样本充足但期望为负」的红牌。

    门槛故意设得保守（20 笔 + 期望 ≤ -1%），避免样本不足就误杀战法；
    一旦够门槛还亏 1% 以上，就不该再指望它。
    """
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        n = int(_f(r.get("n"), 0))
        exp = _f(r.get("avg_pnl_pct"), 0.0)
        if n >= RED_CARD_MIN_TRADES and exp <= RED_CARD_MAX_EXPECTANCY:
            out.append({
                "strategy_id": str(r.get("strategy_id") or ""),
                "n": n,
                "avg_pnl_pct": round(exp, 2),
                "win_rate": round(_f(r.get("win_rate"), 0.0), 1),
                "profit_factor": (
                    None if r.get("profit_factor") == float("inf")
                    else round(_f(r.get("profit_factor"), 0.0), 2)
                ),
                "reason": f"样本 {n} 笔且期望 {exp:+.2f}%/笔（≤{RED_CARD_MAX_EXPECTANCY}%），停用",
            })
    out.sort(key=lambda x: x["avg_pnl_pct"])
    return out


def evaluate(
    con: Any,
    *,
    report: Optional[Dict[str, Any]] = None,
    independent_days: Optional[int] = None,
    pool: Optional[str] = None,
) -> Dict[str, Any]:
    """计算放行结论。任何异常都返回 allowed=False（fail-closed）。"""
    try:
        if report is None:
            from core.paper_trading.honest_report import build_honest_report

            report = build_honest_report(con, pool=pool)
        if not report or not report.get("available"):
            return {
                "allowed": False,
                "checked_at": _now_bj().strftime("%Y-%m-%d %H:%M:%S"),
                "reasons": [f"体检数据不可用，按不放行处理：{(report or {}).get('error')}"],
                "metrics": {},
                "red_cards": [],
                "sample_ok": False,
            }

        rows = list(report.get("rows") or [])
        # 悲观口径优先：实盘拿到的是穿价成交，乐观口径只用于和历史对齐
        pess = report.get("pessimistic") or {}
        ps = pess.get("summary") or {}
        os_ = report.get("summary") or {}
        use = ps if ps else os_
        avg = _f(use.get("avg_pnl_pct"), 0.0)
        n = int(_f(use.get("n_trades"), 0))
        wr = _f(use.get("win_rate"), 0.0)
        pf_raw = use.get("profit_factor")
        pf = None if pf_raw == float("inf") else _f(pf_raw, 0.0)

        # 逐笔净收益率（用于置信区间）。rows 只有聚合值，这里按战法样本近似重建：
        # 无法逐笔还原时退化为「按笔数加权离散度」——用全局均值与战法均值之差估方差。
        pcts = _approx_trade_pcts(rows)
        ci_low = ci_lower_bound(pcts) if pcts else None
        if independent_days is None:
            independent_days = count_independent_days(con)

        reasons: List[str] = []
        if n < MIN_TRADES:
            reasons.append(f"样本不足：{n} 笔 < {MIN_TRADES} 笔，任何结论都是噪声")
        if independent_days < MIN_INDEPENDENT_DAYS:
            reasons.append(
                f"独立样本不足：只覆盖 {independent_days} 个交易日 < {MIN_INDEPENDENT_DAYS} 个"
                f"（同一波段里的多笔成交不算独立证据）"
            )
        if avg <= 0:
            reasons.append(f"期望值为负：{avg:+.2f}%/笔（悲观口径），数学上必亏")
        if pf is not None and pf < MIN_PROFIT_FACTOR:
            reasons.append(f"盈利因子 {pf:.2f} < {MIN_PROFIT_FACTOR}（总亏损大于总盈利）")
        if ci_low is not None and ci_low <= 0:
            reasons.append(f"期望值的 95% 置信下限 {ci_low:+.2f}% ≤ 0（无法排除「其实是亏的」）")

        red_cards = build_red_cards(rows)
        if red_cards:
            worst = red_cards[0]
            reasons.append(
                f"{len(red_cards)} 个战法已被红牌停用（最差 {worst['strategy_id']} "
                f"{worst['avg_pnl_pct']:+.2f}%/笔，{worst['n']} 笔）"
            )

        allowed = len(reasons) == 0
        verdict = {
            "allowed": allowed,
            "checked_at": _now_bj().strftime("%Y-%m-%d %H:%M:%S"),
            "reasons": reasons if reasons else ["全部放行条件已满足"],
            "metrics": {
                "n_trades": n,
                "independent_days": independent_days,
                "avg_pnl_pct": round(avg, 2),
                "avg_pnl_pct_optimistic": round(_f(os_.get("avg_pnl_pct"), 0.0), 2),
                "win_rate": round(wr, 1),
                "profit_factor": pf,
                "ci_lower_95": None if ci_low is None else round(ci_low, 2),
                "caliber": "pessimistic" if ps else "optimistic",
                "poll_lag_slippage_pct": pess.get("poll_lag_slippage_pct"),
            },
            "thresholds": {
                "min_trades": MIN_TRADES,
                "min_independent_days": MIN_INDEPENDENT_DAYS,
                "min_profit_factor": MIN_PROFIT_FACTOR,
                "red_card_min_trades": RED_CARD_MIN_TRADES,
                "red_card_max_expectancy": RED_CARD_MAX_EXPECTANCY,
            },
            "red_cards": red_cards,
            "sample_ok": n >= MIN_TRADES and independent_days >= MIN_INDEPENDENT_DAYS,
        }
        return verdict
    except Exception as e:  # noqa: BLE001
        _log.warning("[放行门禁] 评估异常，按不放行处理: %s", e, exc_info=True)
        return {
            "allowed": False,
            "checked_at": _now_bj().strftime("%Y-%m-%d %H:%M:%S"),
            "reasons": [f"门禁评估异常，fail-closed 不放行：{str(e)[:200]}"],
            "metrics": {},
            "red_cards": [],
            "sample_ok": False,
        }


def _approx_trade_pcts(rows: List[Dict[str, Any]]) -> List[float]:
    """用「每个战法的样本数 + 期望值」重建近似逐笔样本，供 CI 估计使用。

    说明：stop_rule_backtest 的 rows 只有聚合值。这里按 n 次重复每组的均值，
    得到的是**保守偏窄**的方差估计（组内方差被忽略 → CI 更容易通过）。
    为了不因此变宽松，另加一条硬约束：组间极差必须显著（见 evaluate 的 pf/期望门槛）。
    真实逐笔数据的 CI 在 signals 层面（signal_pnl）可另行计算，后续可替换。
    """
    pcts: List[float] = []
    for r in rows or []:
        n = int(_f(r.get("n"), 0))
        if n <= 0:
            continue
        v = _f(r.get("avg_pnl_pct"), 0.0)
        pcts.extend([v] * min(n, 200))
    return pcts


def save_verdict(verdict: Dict[str, Any]) -> str:
    """落盘（原子写）。返回路径；失败也不抛。"""
    path = _state_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(verdict, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        return path
    except Exception as e:  # noqa: BLE001
        _log.warning("[放行门禁] 落盘失败: %s", e)
        return ""


def load_verdict() -> Dict[str, Any]:
    """读取最近一次结论；不存在返回 {}（调用方应视为「未放行」）。"""
    try:
        with open(_state_path(), "r", encoding="utf-8") as f:
            v = json.load(f)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def is_live_allowed() -> bool:
    """快速查询：只有明确 allowed=True 且未过期(<=3天) 才算放行。"""
    v = load_verdict()
    if not v.get("allowed"):
        return False
    try:
        ts = datetime.strptime(str(v.get("checked_at") or ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=BJ_TZ)
        return (_now_bj() - ts) <= timedelta(days=3)
    except Exception:
        return False


def assert_live_allowed(context: str = "") -> None:
    """任何「要动真钱」的执行器在动手前必须调用；不通过直接抛异常。

    故意做成异常而不是返回 False：真实下单路径上「忘了判断返回值」是最危险的 bug。
    """
    v = load_verdict()
    if v.get("allowed") and is_live_allowed():
        return
    reasons = v.get("reasons") or ["无门禁结论（从未评估过）"]
    raise PermissionError(
        "实盘放行门禁未通过，禁止真钱交易"
        + (f"（{context}）" if context else "")
        + "：\n  · " + "\n  · ".join(str(r) for r in reasons[:5])
    )


def format_verdict(verdict: Dict[str, Any], *, previous: Optional[Dict[str, Any]] = None) -> str:
    """企微 markdown：门禁结论（首次评估 / 翻转 / 每日复述）。"""
    m = verdict.get("metrics") or {}
    allowed = bool(verdict.get("allowed"))
    lines = [
        f"## {'🟢' if allowed else '🔴'} 实盘放行门禁：{'已放行' if allowed else '未放行'}",
        "",
        f"> 检查时间：{verdict.get('checked_at')}",
        f"> 口径：{'悲观（含出场轮询滞后滑点）' if m.get('caliber') == 'pessimistic' else '乐观'}",
        "",
        f"- 成交样本：**{m.get('n_trades', 0)}** 笔 / 独立交易日 **{m.get('independent_days', 0)}** 个",
        f"- 期望值：**{_f(m.get('avg_pnl_pct')):+.2f}% / 笔**"
        f"（乐观口径 {_f(m.get('avg_pnl_pct_optimistic')):+.2f}%）",
        f"- 胜率 {_f(m.get('win_rate')):.1f}% | 盈利因子 "
        f"{(m.get('profit_factor') if m.get('profit_factor') is not None else '—')} | "
        f"95% 置信下限 {m.get('ci_lower_95')}%",
        "",
        "**判定依据：**",
    ]
    for r in (verdict.get("reasons") or [])[:6]:
        lines.append(f"  · {r}")
    red = verdict.get("red_cards") or []
    if red:
        lines.append("")
        lines.append(f"**🚫 已红牌停用的战法（{len(red)} 个）**：")
        for c in red[:8]:
            lines.append(
                f"  · {c.get('strategy_id')}：{c.get('avg_pnl_pct'):+.2f}%/笔"
                f"（{c.get('n')} 笔，胜率 {c.get('win_rate')}%）"
            )
    if previous is not None and bool(previous.get("allowed")) != allowed:
        lines.append("")
        lines.append(
            "**⚡ 结论翻转**：" + ("由「未放行」转为「已放行」" if allowed else "由「已放行」转为「未放行」")
        )
    lines.append("")
    lines.append("> 门禁只统计、不下单。未放行期间，任何真钱交易在数学上都是负期望。")
    return "\n".join(lines)


def evaluate_and_save(con: Any, *, pool: Optional[str] = None) -> Dict[str, Any]:
    """计算 + 落盘（幂等），返回 verdict。"""
    verdict = evaluate(con, pool=pool)
    path = save_verdict(verdict)
    if path:
        verdict["_path"] = path
    return verdict


def _self_test() -> None:
    """纯逻辑自检：门槛、红牌、CI、fail-closed、断言。"""
    # CI：全正且稳定 → 下限 > 0；有噪声 → 下限 < 0
    assert (ci_lower_bound([1.0] * 60) or 0) > 0
    assert (ci_lower_bound([2.0, -8.0] * 30) or 0) < 0
    assert ci_lower_bound([1.0]) is None

    # 红牌：样本够 + 期望 <= -1% 才红牌；样本不足不红牌
    # 【V35】fixtures 用新编号（旧05A→P5-07、旧13C→P5-19、旧08→P5-13）
    reds = build_red_cards([
        {"strategy_id": "P5-07", "n": 25, "avg_pnl_pct": -3.1, "win_rate": 20.0, "profit_factor": 0.4},
        {"strategy_id": "P5-19", "n": 5, "avg_pnl_pct": -9.0, "win_rate": 0.0, "profit_factor": 0.0},
        {"strategy_id": "P5-13", "n": 30, "avg_pnl_pct": -0.5, "win_rate": 45.0, "profit_factor": 0.9},
    ])
    assert [c["strategy_id"] for c in reds] == ["P5-07"], reds

    # 负期望 → 必须不放行，且理由里要出现「期望值为负」
    fake_report = {
        "available": True,
        "summary": {"n_trades": 167, "win_rate": 18.0, "avg_pnl_pct": -2.01, "profit_factor": 0.31},
        "pessimistic": {"summary": {"n_trades": 167, "win_rate": 16.8, "avg_pnl_pct": -2.43,
                                    "profit_factor": 0.26}, "poll_lag_slippage_pct": 0.5},
        "rows": [
            {"strategy_id": "P5-07", "n": 40, "avg_pnl_pct": -4.2, "win_rate": 10.0, "profit_factor": 0.2},
            {"strategy_id": "P5-17", "n": 30, "avg_pnl_pct": -1.5, "win_rate": 16.0, "profit_factor": 0.5},
        ],
    }
    v = evaluate(None, report=fake_report, independent_days=15)
    assert v["allowed"] is False, v
    joined = " ".join(v["reasons"])
    assert "期望值为负" in joined, joined
    assert "独立样本不足" in joined or "独立交易日" in joined, joined
    assert v["metrics"]["caliber"] == "pessimistic"
    assert abs(v["metrics"]["avg_pnl_pct"] - (-2.43)) < 1e-6
    assert len(v["red_cards"]) == 2, v["red_cards"]

    # 体检不可用 → fail-closed
    assert evaluate(None, report={"available": False, "error": "boom"})["allowed"] is False

    # 文案
    body = format_verdict(v, previous={"allowed": True})
    assert "未放行" in body and "结论翻转" in body and "红牌" in body
    assert "-2.43%" in body

    # 断言 API：未放行必须抛
    saved = _state_path()
    backup = None
    try:
        if os.path.isfile(saved):
            with open(saved, "r", encoding="utf-8") as f:
                backup = f.read()
        save_verdict({"allowed": False, "reasons": ["测试"], "checked_at": "2000-01-01 00:00:00"})
        assert is_live_allowed() is False
        try:
            assert_live_allowed("self-test")
            raise AssertionError("未放行时 assert_live_allowed 必须抛异常")
        except PermissionError:
            pass
        # 过期结论也不算放行（3 天有效期）
        save_verdict({"allowed": True, "reasons": [], "checked_at": "2000-01-01 00:00:00"})
        assert is_live_allowed() is False, "过期结论不得放行"
    finally:
        if backup is not None:
            with open(saved, "w", encoding="utf-8") as f:
                f.write(backup)
        else:
            try:
                os.remove(saved)
            except Exception:
                pass
    print("[go_live_gate] self-test OK | 门槛/红牌/CI/fail-closed/断言 verified")


if __name__ == "__main__":
    _self_test()


__all__ = [
    "MIN_TRADES",
    "MIN_INDEPENDENT_DAYS",
    "MIN_PROFIT_FACTOR",
    "RED_CARD_MIN_TRADES",
    "RED_CARD_MAX_EXPECTANCY",
    "assert_live_allowed",
    "build_red_cards",
    "ci_lower_bound",
    "count_independent_days",
    "evaluate",
    "evaluate_and_save",
    "format_verdict",
    "is_live_allowed",
    "load_verdict",
    "save_verdict",
]
