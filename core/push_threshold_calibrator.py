# -*- coding: utf-8 -*-
"""
core/push_threshold_calibrator.py
========================================================================
推送分数阈值自适应（push_min_score 动态化）
========================================================================

【背景 · 为什么不能写死】
当前五档阈值是硬编码的：
    P2 = 50.0    P3 = 50.0    P4 = 55.0    P5 = 70.0
这组数字的问题是：它既没有对齐各池的真实分数分布，
也没有对齐各池的真实样本外收益 —— 换句话说，
【分数门槛和效果之间没有任何因果关系】。

而系统其实已经具备做这件事的数据：
  · core.strategy_effectiveness_snapshot → 各战法样本外表现（Wilson 下界 / 超额）
  · signal_log → 每个分数段的真实收益分布

【本模块做什么】
按「池 × 分数段」的历史样本外收益，反推出每一档的推荐阈值：

    对每个池 P 和候选阈值 T：
      样本 = 该池所有 score >= T 的信号
      评价 = 该样本的 T+N 胜率 / Wilson 下界 / 超额 / 平均回撤
    取「在【样本量下限】之上，Wilson 下界最优」的最小 T
    → 分数门槛越高，胜率越高；但门槛太高样本太少、覆盖不足。
      取「下界最优」而非「胜率最优」，是为了防止小样本噪声。

【关键设计：绝不自动改生产阈值】
  · calc_recommended_thresholds() 只产出【建议值】
  · apply_thresholds() 必须显式调用、且默认 dry_run=True
  · 生产阈值改动属于策略行为变更，必须人工 review 后显式落地
  （与 core.strategy_weight_optimizer 的 auto_apply=False 同一原则）

【红线】
  · 只读 DuckDB
  · 不改任何生产配置（除非显式 apply_recommended_thresholds）
  · 样本不足时返回 None（不猜、不填默认值）
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BJ_TZ = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_FILE = os.path.join(ROOT, "data", "push_threshold_recommendation.json")

# 各池当前的生产硬编码阈值（作为对比基线）
CURRENT_THRESHOLDS: Dict[str, float] = {
    "p2": 50.0,
    "p3": 50.0,
    "p4": 55.0,
    "p5": 70.0,
}

# 候选阈值扫描网格
CANDIDATE_THRESHOLDS = (40.0, 45.0, 50.0, 55.0, 60.0, 65.0, 70.0, 75.0, 80.0, 85.0)


# ============================ 核心计算 ============================

def _score_buckets_stats(
    signals: List[Dict[str, Any]],
    cal: List[str],
    bm: Dict[str, Dict[str, float]],
    hold: Tuple[int, ...],
) -> Dict[str, Dict[int, Dict[str, Any]]]:
    """
    {pool: {threshold: stats}} —— 对每个池，扫描每个候选阈值下的样本表现。
    """
    from core.strategy_effectiveness_snapshot import Acc, _bench_code, _bench_cum, _dd_mp

    out: Dict[str, Dict[int, Dict[str, Any]]] = {}
    for sig in signals:
        pool = sig.get("pool") or ""
        score = sig.get("score")
        if score is None:
            continue
        score = float(score)
        i = sig["_i"]
        entry = float(sig["push_price"])
        bars = sig.get("_bars") or {}
        if not bars:
            continue
        bench_code = _bench_code(sig.get("ts_code") or "")

        for n in hold:
            j = i + n
            if j >= len(cal):
                continue
            b = bars.get(cal[j])
            if not b or b[3] is None or float(b[3]) <= 0:
                continue
            ret = (float(b[3]) / entry - 1.0) * 100.0
            dd, mp = _dd_mp(bars, cal, i, n, entry)
            bp = _bench_cum(bm, bench_code, i, n, cal)

            for thr in CANDIDATE_THRESHOLDS:
                if score < thr:
                    continue
                node = out.setdefault(pool, {}).setdefault(thr, {})
                acc = node.setdefault(n, Acc())
                acc.add(ret, dd, mp, sig.get("ts_code") or "", bp)
    return out


def calc_recommended_thresholds(
    *,
    db_path: Optional[str] = None,
    days: int = 120,
    hold: Tuple[int, ...] = (2, 5),
    min_samples: int = 8,
    max_threshold: float = 85.0,
    db_conn=None,
) -> Dict[str, Any]:
    """
    计算各池的推荐 push_min_score。

    选阈规则（按优先级）：
      1) 只看「已成熟档位」中的最优档（优先 T+5，其次 T+2）
      2) 样本量 >= min_samples
      3) 在满足 1)+2) 的候选中，取 Wilson 下界最高者
      4) 若多个阈值下界接近（差 < 0.02），取【更低】的阈值（覆盖更多信号）
      5) 若没有任何候选满足样本量 → 返回 None（不猜）

    返回：
      {
        "generated_at": str,
        "meta": {...},
        "recommendations": {pool: {"threshold": float|None, "reason": str, ...}},
        "current": {pool: float},
        "curves": {pool: {threshold: {stats}}},
      }
    """
    from core.strategy_effectiveness_snapshot import (
        _calendar, _bench_map, _load, _pick_price_table, _open_ro,
    )

    result: Dict[str, Any] = {
        "generated_at": datetime.now(BJ_TZ).isoformat(),
        "meta": {},
        "recommendations": {},
        "current": dict(CURRENT_THRESHOLDS),
        "curves": {},
        "error": None,
    }

    con = None
    _own_con = False
    try:
        db = db_path or os.path.join(ROOT, "data", "quant_data.duckdb")
        # 【V30.10】复用已有连接：DuckDB 不允许同文件同时存在不同配置的连接
        if db_conn is not None:
            con = db_conn
        else:
            con = _open_ro(db)
            _own_con = True
        src = _pick_price_table(con)
        signals, meta = _load(con, days, src)
        result["meta"] = meta
        if not signals:
            result["error"] = meta.get("error") or "窗口内无带推送价的信号"
            return result

        cal = _calendar(con)
        bm = _bench_map(con, meta.get("window_start") or cal[0])
        curves = _score_buckets_stats(signals, cal, bm, hold)

        for pool, per_thr in curves.items():
            curve_out: Dict[str, Any] = {}
            best: Optional[Tuple[float, int, Dict[str, Any]]] = None
            eligible: List[Tuple[float, int, Dict[str, Any]]] = []

            for thr, per_n in per_thr.items():
                # 取该阈值下表现最好的成熟档
                cand: Optional[Tuple[int, Dict[str, Any]]] = None
                for n in sorted(per_n.keys(), reverse=True):
                    s = per_n[n].summary()
                    if int(s.get("n", 0)) <= 0:
                        continue
                    cand = (n, s)
                    break
                if cand is None:
                    continue
                n, s = cand
                curve_out[str(int(thr))] = {
                    "n_days": n, **{k: v for k, v in s.items()},
                }
                if int(s.get("n", 0)) >= int(min_samples) and float(thr) <= float(max_threshold):
                    eligible.append((float(thr), n, s))

            if eligible:
                eligible.sort(key=lambda x: (float(x[2].get("wilson_lo") or 0.0),
                                             -x[0]), reverse=True)
                top_lo = float(eligible[0][2].get("wilson_lo") or 0.0)
                # 规则 4：下界接近时取更低阈值（覆盖更多信号）
                near = [e for e in eligible if float(e[2].get("wilson_lo") or 0.0) >= top_lo - 0.02]
                near.sort(key=lambda x: x[0])
                thr, n, s = near[0]
                best = (thr, n, s)

            result["curves"][pool] = curve_out

            if best is None:
                result["recommendations"][pool] = {
                    "threshold": None,
                    "current": CURRENT_THRESHOLDS.get(pool),
                    "reason": "样本不足（%d 条门槛下无足够样本），保持当前阈值" % int(min_samples),
                    "n": 0,
                }
            else:
                thr, n, s = best
                cur = CURRENT_THRESHOLDS.get(pool)
                delta = None if cur is None else round(thr - cur, 2)
                if cur is None:
                    reason = "推荐 %.0f（样本 %d，Wilson下界 %.1f%%）" % (
                        thr, s.get("n", 0), float(s.get("wilson_lo") or 0) * 100)
                elif abs(delta or 0.0) < 1e-6:
                    reason = "维持 %.0f（已是该档 Wilson 下界最优）" % thr
                elif delta and delta > 0:
                    reason = "建议上调 %.0f → %.0f（T+%d 胜率 %.1f%%，Wilson下界 %.1f%%，样本 %d）" % (
                        cur, thr, n, float(s.get("win_rate") or 0) * 100,
                        float(s.get("wilson_lo") or 0) * 100, s.get("n", 0))
                else:
                    reason = "建议下调 %.0f → %.0f（T+%d 胜率 %.1f%%，Wilson下界 %.1f%%，样本 %d）" % (
                        cur, thr, n, float(s.get("win_rate") or 0) * 100,
                        float(s.get("wilson_lo") or 0) * 100, s.get("n", 0))
                result["recommendations"][pool] = {
                    "threshold": thr,
                    "current": cur,
                    "delta": delta,
                    "reason": reason,
                    "n_days": n,
                    "n": s.get("n", 0),
                    "win_rate": s.get("win_rate"),
                    "wilson_lo": s.get("wilson_lo"),
                    "avg_return": s.get("avg_return"),
                    "avg_excess": s.get("avg_excess"),
                    "beat_bench_rate": s.get("beat_bench_rate"),
                    "avg_max_drawdown": s.get("avg_max_drawdown"),
                }

    except Exception as e:
        logger.warning("[push_calib] 计算失败: %s", e)
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        # 【V30.10】只关自己开的连接，借来的绝不关闭
        if con is not None and _own_con:
            try:
                con.close()
            except Exception:
                pass

    return result


def apply_recommended_thresholds(
    recommendations: Dict[str, Any],
    *,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    把推荐阈值写入 config.yaml。

    【默认 dry_run=True —— 必须显式传 dry_run=False 才会真正落盘】
    阈值变更属于【策略行为变更】，必须人工 review 后显式落地。

    返回：{"applied": bool, "changes": [...], "error": str|None}
    """
    recs = (recommendations or {}).get("recommendations") or {}
    changes: List[Dict[str, Any]] = []
    for pool, node in recs.items():
        thr = node.get("threshold")
        if thr is None:
            continue
        cur = node.get("current")
        if cur is None or abs(float(thr) - float(cur)) < 1e-6:
            continue
        changes.append({"pool": pool, "from": cur, "to": float(thr),
                        "reason": node.get("reason", "")})

    if dry_run:
        return {"applied": False, "dry_run": True, "changes": changes, "error": None}
    if not changes:
        return {"applied": False, "dry_run": False, "changes": [], "error": None}

    try:
        import yaml
        cfg_path = os.path.join(ROOT, "config.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        for ch in changes:
            node = cfg.setdefault("push_min_score", {})
            node[ch["pool"]] = float(ch["to"])
        tmp = cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, cfg_path)
        return {"applied": True, "dry_run": False, "changes": changes, "error": None}
    except Exception as e:
        logger.warning("[push_calib] 落地失败: %s", e)
        return {"applied": False, "dry_run": False, "changes": changes,
                "error": f"{type(e).__name__}: {e}"}


def save_recommendation(result: Dict[str, Any], path: Optional[str] = None) -> str:
    """落盘推荐结果（供 UI / 人工 review）。"""
    p = path or OUT_FILE
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, p)
        return p
    except Exception as e:
        logger.warning("[push_calib] 落盘失败: %s", e)
        return ""


def format_report(result: Dict[str, Any]) -> str:
    """渲染成可读文本。"""
    L: List[str] = []
    A = L.append
    A("=" * 76)
    A("推送分数阈值自适应标定")
    A("=" * 76)
    m = result.get("meta") or {}
    A("窗口     : %s ~ %s（%s 交易日）" % (
        m.get("window_start"), m.get("window_end"), m.get("window_days")))
    A("样本     : %s 条可用" % m.get("signals_usable"))
    if result.get("error"):
        A("错误     : %s" % result["error"])
    A("-" * 76)
    for pool, node in (result.get("recommendations") or {}).items():
        A("池 %-4s 当前=%-6s 推荐=%-8s %s" % (
            pool,
            node.get("current"),
            (node.get("threshold") if node.get("threshold") is not None else "保持"),
            node.get("reason", "")))
    A("-" * 76)
    A("【阈值-收益曲线（各池）】")
    for pool, curve in (result.get("curves") or {}).items():
        A("  %s:" % pool)
        for thr in sorted(curve.keys(), key=lambda x: float(x)):
            s = curve[thr]
            A("    阈值 %-5s T+%-2s n=%-4s 胜率=%-7s Wilson下界=%-7s 均收益=%-8s 超额=%-8s 回撤=%s" % (
                thr, s.get("n_days"), s.get("n"),
                ("%.1f%%" % (float(s["win_rate"]) * 100)) if s.get("win_rate") is not None else "-",
                ("%.1f%%" % (float(s["wilson_lo"]) * 100)) if s.get("wilson_lo") is not None else "-",
                ("%+.2f%%" % float(s["avg_return"])) if s.get("avg_return") is not None else "-",
                ("%+.2f%%" % float(s["avg_excess"])) if s.get("avg_excess") is not None else "-",
                ("%.2f%%" % float(s["avg_max_drawdown"])) if s.get("avg_max_drawdown") is not None else "-",
            ))
    A("=" * 76)
    return "\n".join(L)


__all__ = [
    "CURRENT_THRESHOLDS",
    "CANDIDATE_THRESHOLDS",
    "calc_recommended_thresholds",
    "apply_recommended_thresholds",
    "save_recommendation",
    "format_report",
]
