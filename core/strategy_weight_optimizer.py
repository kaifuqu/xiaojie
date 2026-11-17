# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V28 — 战法权重自动调节
================================================================================
【V28 新增·F 项】每日读 strategy_effectiveness 表，根据过去 30 日战法胜率
自动调整战法权重。

规则：
  - 胜率 ≥ 70% 且 n ≥ 10  →  weight = ×1.2（升权）
  - 胜率 ≤ 45% 且 n ≥ 20  →  weight = ×0.7（降权）
  - 胜率 ≤ 30% 且 n ≥ 30  →  marked deprecated（不自动降权，等人工 review）

数据源：strategy_effectiveness 表（X-A 回测系统写入）

输出：
  - strategy_weights.json（运行时权重表）
  - deprecated_strategies.json（待人工审核清单）

集成：
  - auto_sniper_daemon._signal_backfill_pipeline() 每日 18:30 调用
    optimize_strategy_weights(auto_apply=False)，与 T+N 回填同一事务窗口完成
  - 后续 score_calibration 读取 strategy_weights.json 给每战法加权

【V29.1 关键修复】
  1) 旧版 SQL 只读 t5_win_rate 且要求 IS NOT NULL。系统刚上线时只有
     T+1 / T+2 数据，t5_win_rate 全为 NULL → 优化器把全部聚合行过滤掉，
     optimization 静默返回空、data/strategy_weight_recommendation.json 永不生成，
     表现为「回填已接通但权重链路无输出」。
     现改为：读取每个战法「当前最新已成熟 N 档」的 win_rate / avg_return /
     avg_max_drawdown，缺 T+5/T+10 不影响出报表。
  2) 新增 maturity（数据成熟度）块：明确当前采用哪一档、缺哪几档、
     还差多少样本才允许自动应用，避免「空结果 = 没数据」被误读。
  3) 统计口径与 signal_backtest 一致：同一 (pool, strategy, stat_n_days)
     只取 stat_date 最新的一行；跨池按样本量加权汇总，同一档位只计一次。
================================================================================
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional, Tuple

# T+N 档位从大到小：最新已成熟档优先（T+5 有数据就用 T+5，否则回退 T+2 / T+1）
N_DAYS_PRIORITY = [10, 5, 2, 1]
N_DAYS_ALL = [1, 2, 5, 10]

RECOMMENDATION_FILE = "data/strategy_weight_recommendation.json"

logger = logging.getLogger(__name__)


WEIGHTS_FILE = "data/strategy_weights.json"
DEPRECATED_FILE = "data/deprecated_strategies.json"


def optimize_strategy_weights(
    db_conn,
    lookback_days: int = 30,
    min_signals_promote: int = 10,
    min_signals_demote: int = 20,
    min_signals_deprecate: int = 30,
    promote_threshold: float = 0.70,
    demote_threshold: float = 0.45,
    deprecate_threshold: float = 0.30,
    auto_apply: bool = False,
    min_n_for_auto_apply: int = 200,
) -> Dict[str, Any]:
    """
    从 strategy_effectiveness 表读取胜率，自动调整权重。

    【V28.5 重要修改：默认 auto_apply=False（只统计，不自动改权重）】
    理由：
      - 样本 < 200 时，胜率波动极大（30 个样本翻盘仅需 6 个）
      - 权重震荡会污染 signal_log（无法追溯战法真实有效性）
      - 慢牛行情下升权等于全体 ×1.2，失去调节意义

    auto_apply=True 且 n_signals >= min_n_for_auto_apply 时才会写入 strategy_weights.json
    其他情况只产出 weight_recommendation 报表，供人工 review。

    返回：{
      weights: {strategy: float},        # 仅在 auto_apply 时填
      weight_recommendation: {...},      # 统计输出（推荐权重）
      deprecated: [strategy],
      stats: {...},
      maturity: {...},                   # 数据成熟度（V29.1 新增）
      strategy_maturity: {...},          # 每个战法的成熟档位与样本缺口（V29.1 新增）
      mode: "statistics_only" | "auto_applied"
    }
    """
    weights: Dict[str, float] = {}
    weight_recommendation: Dict[str, Dict[str, Any]] = {}
    deprecated: List[str] = []
    stats: Dict[str, Dict[str, Any]] = {}
    maturity: Dict[str, Any] = {}
    strategy_maturity: Dict[str, Dict[str, Any]] = {}
    # 【Tier2.5】净口径（扣费后）并行推荐表：与 weight_recommendation 并列输出，
    # **不覆盖**旧字段，也不参与 auto_apply（不改变任何实盘权重）。
    net_weight_recommendation: Dict[str, Dict[str, Any]] = {}
    mode = "statistics_only"

    if db_conn is None:
        logger.warning("[optimize] db_conn=None，跳过")
        return {
            "weights": weights,
            "deprecated": deprecated,
            "stats": stats,
            "weight_recommendation": weight_recommendation,
            "net_weight_recommendation": net_weight_recommendation,
            "maturity": maturity,
            "strategy_maturity": strategy_maturity,
            "mode": mode,
        }

    # 【Tier2.5】净口径列（净收益/净超额/净胜率）。
    # 旧库可能还没有这些列 → 先探测再加进 SELECT，绝不让整个优化器因缺列失败。
    _net_select = ""
    try:
        _existing_cols = {
            str(c[1]).lower()
            for c in db_conn.execute("PRAGMA table_info('strategy_effectiveness')").fetchall()
        }
    except Exception:
        _existing_cols = set()
    if _existing_cols and {
        "avg_net_return",
        "avg_net_excess",
        "avg_net_excess_blend",
        "net_win_rate",
    }.issubset(_existing_cols):
        _net_select = (
            ",\n                   -- 【Tier2.5 新增】净口径统计量（扣费后）\n"
            "                   avg_net_return, avg_net_excess,\n"
            "                   avg_net_excess_blend, net_win_rate"
        )
    elif _existing_cols:
        logger.info("[optimize] strategy_effectiveness 缺少 Tier2.5 净列，按旧口径统计")

    try:
        rows = db_conn.execute(
            f"""
            SELECT pool, strategy, strategy_label, stat_n_days, CAST(stat_date AS VARCHAR),
                   n_signals, n_evaluated, n_win, win_rate,
                   avg_return, avg_excess, avg_max_drawdown, avg_max_profit,
                   available_n_days,
                   -- 【V30 新增】混合基准统计量（纯加列，不改既有列口径）
                   avg_excess_sh, avg_excess_sz, avg_excess_cyb,
                   avg_excess_blend, beat_blend_rate
                   {_net_select}
            FROM strategy_effectiveness
            WHERE n_signals > 0
              AND win_rate IS NOT NULL
              AND stat_date >= CURRENT_DATE - INTERVAL '{int(lookback_days)} days'
            """,
        ).fetchall()
    except Exception as e:
        logger.warning("[optimize] 读取 strategy_effectiveness 失败: %s", e)
        return {
            "weights": weights,
            "deprecated": deprecated,
            "stats": stats,
            "weight_recommendation": weight_recommendation,
            "net_weight_recommendation": net_weight_recommendation,
            "maturity": maturity,
            "strategy_maturity": strategy_maturity,
            "mode": mode,
        }

    # ---- 去重口径 ----
    # strategy_effectiveness 每天写一次，同一 (pool, strategy, stat_n_days)
    # 会在不同 stat_date 重复出现；这里只保留该键 stat_date 最大的一行。
    latest: Dict[Tuple[str, str, Optional[int]], Tuple] = {}
    for r in rows:
        key = (str(r[0]), str(r[1]), None if r[3] is None else int(r[3]))
        prev = latest.get(key)
        if prev is None:
            latest[key] = r
            continue
        prev_date = "" if prev[4] is None else str(prev[4])
        cand_date = "" if r[4] is None else str(r[4])
        if cand_date > prev_date:
            latest[key] = r

    # 【上线前修复 2026-10-18】跨档重复计数。
    # 根因：下面按战法聚合时，T+2 与 T+5 两行都被保留，n_signals / n_evaluated
    #       直接相加，胜率 = Σ(win_rate_i × n_signals_i) / Σ n_signals_i。
    #       同一批信号在两个持有档各出现一次 → 分母翻倍、样本不独立，
    #       于是 P5-02（旧P5-02B）这类小样本战法的胜率被抬高到 83.9%（真实 T+5 仅 50%）。
    # 修复：分两步。
    #   ① 先给每个战法定出唯一「采用档位」own_primary = max(available_n_days ∪ {stat_n_days})；
    #   ② 聚合时只接受 stat_n_days == own_primary 的行，跨池也只合并同一 N 档。
    # 影响面：仅 data/strategy_weight_recommendation.json 与 UI 面板的展示数字，
    #         不涉及评分权重（auto_apply 默认 False）、不涉及任何策略逻辑。
    primary_by_strat: Dict[str, int] = {}
    for (pool, strat, n_days), r in latest.items():
        avail: set = set()
        raw_avail = r[13]
        if raw_avail is not None:
            for token in str(raw_avail).split(","):
                token = token.strip()
                if token.isdigit():
                    avail.add(int(token))
        if n_days is not None:
            avail.add(int(n_days))
        if not avail:
            continue
        cand = max(avail)
        if strat not in primary_by_strat or cand > primary_by_strat[strat]:
            primary_by_strat[strat] = cand

    # ---- 跨池汇总（同一战法只取「最新已成熟档位」）----
    by_strat: Dict[str, Dict[str, Any]] = {}
    for (pool, strat, n_days), r in latest.items():
        # 只聚合「该战法采用档位」的行，避免跨档重复计数（见上方 primary_by_strat）
        if primary_by_strat.get(strat) is not None and n_days is not None and int(n_days) != int(primary_by_strat[strat]):
            continue
        try:
            n_sig = int(r[5] or 0)
            n_eval = int(r[6] or 0)
            n_win_cnt = int(r[7] or 0)
            wr = float(r[8])
            avg_ret = None if r[9] is None else float(r[9])
            avg_exc = None if r[10] is None else float(r[10])
            avg_dd = None if r[11] is None else float(r[11])
            avg_mp = None if r[12] is None else float(r[12])
            label = "" if r[2] is None else str(r[2])
            avail_raw = r[13]
            # V30：缺失/旧库无此列时全部 None，绝不用 0.0 冒充超额
            v_sh = None if r[14] is None else float(r[14])
            v_sz = None if r[15] is None else float(r[15])
            v_cyb = None if r[16] is None else float(r[16])
            v_blend = None if r[17] is None else float(r[17])
            v_beat = None if r[18] is None else float(r[18])
            # 【Tier2.5】净口径（净列被追加在 SELECT 末尾，索引 19~22）
            if _net_select and len(r) > 22:
                n_ret = None if r[19] is None else float(r[19])
                n_exc = None if r[20] is None else float(r[20])
                n_exc_blend = None if r[21] is None else float(r[21])
                n_wr = None if r[22] is None else float(r[22])
            else:
                n_ret = n_exc = n_exc_blend = n_wr = None
        except Exception:
            continue
        if n_sig <= 0:
            continue
        s = by_strat.setdefault(
            strat,
            {
                "total_n": 0,
                "weighted_sum": 0.0,
                "pools": set(),
                "pool_rows": [],
                "label": label,
                "best_n_days": None,
                "ret_num": 0.0,
                "ret_den": 0,
                "dd_num": 0.0,
                "dd_den": 0,
                "mp_num": 0.0,
                "mp_den": 0,
                "n_evaluated_total": 0,
                "n_win_total": 0,
                "available_n_days": set(),
                "exc_num": 0.0,
                "exc_den": 0,
                # V30：每个基准各自独立的加权累加器（各自算自己的分母）
                "v_sh_num": 0.0, "v_sh_den": 0,
                "v_sz_num": 0.0, "v_sz_den": 0,
                "v_cyb_num": 0.0, "v_cyb_den": 0,
                "v_blend_num": 0.0, "v_blend_den": 0,
                "v_beat_num": 0.0, "v_beat_den": 0,
                # Tier2.5：净口径独立累加器（各自算自己的分母）
                "n_ret_num": 0.0, "n_ret_den": 0,
                "n_exc_num": 0.0, "n_exc_den": 0,
                "n_exc_blend_num": 0.0, "n_exc_blend_den": 0,
                "n_wr_num": 0.0, "n_wr_den": 0,
            },
        )
        s["total_n"] += n_sig
        s["weighted_sum"] += wr * n_sig
        s["pools"].add(pool)
        s["pool_rows"].append(
            {"pool": pool, "n_days": n_days, "n_signals": n_sig, "win_rate": wr}
        )
        s["n_evaluated_total"] += n_eval
        s["n_win_total"] += n_win_cnt
        if n_days is not None:
            for token in str(avail_raw or "").split(","):
                token = token.strip()
                if token.isdigit():
                    s["available_n_days"].add(int(token))
            s["available_n_days"].add(int(n_days))
            if s["best_n_days"] is None or int(n_days) > int(s["best_n_days"]):
                s["best_n_days"] = int(n_days)
        if avg_ret is not None:
            s["ret_num"] += avg_ret * n_eval
            s["ret_den"] += n_eval
        if avg_dd is not None:
            s["dd_num"] += avg_dd * n_eval
            s["dd_den"] += n_eval
        if avg_mp is not None:
            s["mp_num"] += avg_mp * n_eval
            s["mp_den"] += n_eval
        if avg_exc is not None:
            s["exc_num"] += avg_exc * n_eval
            s["exc_den"] += n_eval
        # V30：各基准分开累加，避免某列缺数时把 0 混进别的基准均值
        for _key, _val in (("v_sh", v_sh), ("v_sz", v_sz), ("v_cyb", v_cyb),
                           ("v_blend", v_blend), ("v_beat", v_beat)):
            if _val is not None:
                s["%s_num" % _key] += _val * n_eval
                s["%s_den" % _key] += n_eval
        # 【Tier2.5】净口径累加（缺列时静默跳过）
        for _key, _val in (("n_ret", n_ret), ("n_exc", n_exc),
                           ("n_exc_blend", n_exc_blend), ("n_wr", n_wr)):
            if _val is not None:
                s["%s_num" % _key] += _val * n_eval
                s["%s_den" % _key] += n_eval

    # ---- 全局成熟度 ----
    all_available = sorted(
        {int(n) for s in by_strat.values() for n in s["available_n_days"]}
    )
    global_primary = None
    for n in N_DAYS_PRIORITY:
        if n in all_available:
            global_primary = n
            break
    missing_n = [n for n in N_DAYS_ALL if n not in all_available]
    # 低于主口径的档位其实已有数据（例如主口径 T+2、T+1 也已成熟），
    # 但权重决策只用主口径，故不列入「缺失」。
    missing_n = [n for n in missing_n if global_primary is None or n > global_primary]

    for strat, s in by_strat.items():
        if s["total_n"] <= 0:
            continue
        avg_wr = s["weighted_sum"] / s["total_n"]
        # 本战法实际采用的档位 = 其可用档位里最新的一档
        own_available = sorted(s["available_n_days"], reverse=True)
        own_primary = own_available[0] if own_available else None
        avg_ret = (s["ret_num"] / s["ret_den"]) if s["ret_den"] > 0 else None
        avg_exc = (s["exc_num"] / s["exc_den"]) if s["exc_den"] > 0 else None
        avg_dd = (s["dd_num"] / s["dd_den"]) if s["dd_den"] > 0 else None
        avg_mp = (s["mp_num"] / s["mp_den"]) if s["mp_den"] > 0 else None
        stats[strat] = {
            "n_signals": s["total_n"],
            "win_rate": avg_wr,
            "t5_win_rate": avg_wr if own_primary == 5 else None,  # 兼容旧字段
            "primary_n_days": own_primary,
            "available_n_days": own_available,
            "missing_n_days": [n for n in N_DAYS_ALL if n not in own_available],
            "n_evaluated": s["n_evaluated_total"],
            "n_win": s["n_win_total"],
            "avg_return": avg_ret,
            "avg_excess": avg_exc,
            "avg_max_drawdown": avg_dd,
            "avg_max_profit": avg_mp,
            # ---- V30 混合基准（无分母 → None，绝不 0.0）----
            "avg_excess_sh": (s["v_sh_num"] / s["v_sh_den"]) if s["v_sh_den"] > 0 else None,
            "avg_excess_sz": (s["v_sz_num"] / s["v_sz_den"]) if s["v_sz_den"] > 0 else None,
            "avg_excess_cyb": (s["v_cyb_num"] / s["v_cyb_den"]) if s["v_cyb_den"] > 0 else None,
            "avg_excess_blend": (s["v_blend_num"] / s["v_blend_den"]) if s["v_blend_den"] > 0 else None,
            "beat_blend_rate": (s["v_beat_num"] / s["v_beat_den"]) if s["v_beat_den"] > 0 else None,
            # ---- Tier2.5 净口径（无分母 → None，绝不 0.0）----
            "avg_net_return": (s["n_ret_num"] / s["n_ret_den"]) if s["n_ret_den"] > 0 else None,
            "avg_net_excess": (s["n_exc_num"] / s["n_exc_den"]) if s["n_exc_den"] > 0 else None,
            "avg_net_excess_blend": (s["n_exc_blend_num"] / s["n_exc_blend_den"]) if s["n_exc_blend_den"] > 0 else None,
            "net_win_rate": (s["n_wr_num"] / s["n_wr_den"]) if s["n_wr_den"] > 0 else None,
            "strategy_label": s["label"],
            "pools": sorted(s["pools"]),
            "pool_rows": s["pool_rows"],
        }
        strategy_maturity[strat] = {
            "primary_n_days": own_primary,
            "available_n_days": own_available,
            "missing_n_days": [n for n in N_DAYS_ALL if n not in own_available],
            "n_signals": s["total_n"],
            "n_evaluated": s["n_evaluated_total"],
            "remaining_to_auto_apply": max(0, min_n_for_auto_apply - s["total_n"]),
        }

        # 升权
        if avg_wr >= promote_threshold and s["total_n"] >= min_signals_promote:
            recommended = 1.2
            weight_recommendation[strat] = {"weight": recommended, "reason": "高胜率"}
        # 降权
        elif avg_wr <= demote_threshold and s["total_n"] >= min_signals_demote:
            recommended = 0.7
            weight_recommendation[strat] = {"weight": recommended, "reason": "低胜率"}
        # 弃用候选（不自动降权，标记 deprecated）
        elif avg_wr <= deprecate_threshold and s["total_n"] >= min_signals_deprecate:
            recommended = 1.0
            weight_recommendation[strat] = {"weight": recommended, "reason": "极低胜率-待审核"}
            deprecated.append(strat)
        # 默认权重
        else:
            recommended = 1.0
            weight_recommendation[strat] = {"weight": recommended, "reason": "样本不足-保持默认"}

        # 仅在 auto_apply=True 且满足样本门槛时才真正改权重
        if auto_apply and s["total_n"] >= min_n_for_auto_apply:
            weights[strat] = recommended
            mode = "auto_applied"

        # 【Tier2.5】净口径推荐（扣费后胜率 + 净超额为主序）。
        # 原则：**只输出建议，不改权重**（auto_apply 仍走旧口径），
        # 净数据缺失时标记为「数据不足」，绝不用 0 冒充。
        _n_wr = (s["n_wr_num"] / s["n_wr_den"]) if s["n_wr_den"] > 0 else None
        _n_ret = (s["n_ret_num"] / s["n_ret_den"]) if s["n_ret_den"] > 0 else None
        _n_exc_blend = (s["n_exc_blend_num"] / s["n_exc_blend_den"]) if s["n_exc_blend_den"] > 0 else None
        if _n_wr is None:
            net_weight_recommendation[strat] = {
                "weight": 1.0,
                "reason": "净口径数据不足",
                "basis": "net",
                "net_win_rate": None,
                "avg_net_return": None,
                "avg_net_excess_blend": None,
            }
        else:
            if _n_wr >= promote_threshold and s["total_n"] >= min_signals_promote:
                _net_w, _net_reason = 1.2, "净胜率高"
            elif _n_wr <= demote_threshold and s["total_n"] >= min_signals_demote:
                _net_w, _net_reason = 0.7, "净胜率低"
            elif _n_wr <= deprecate_threshold and s["total_n"] >= min_signals_deprecate:
                _net_w, _net_reason = 1.0, "净胜率极低-待审核"
            else:
                _net_w, _net_reason = 1.0, "样本不足-保持默认"
            # 净超额为负时，无论净胜率多高都不建议升权（赚不回手续费）
            if _net_w > 1.0 and _n_exc_blend is not None and _n_exc_blend <= 0:
                _net_w, _net_reason = 1.0, "净超额为负-不升权"
            net_weight_recommendation[strat] = {
                "weight": _net_w,
                "reason": _net_reason,
                "basis": "net",
                "net_win_rate": _n_wr,
                "avg_net_return": _n_ret,
                "avg_net_excess_blend": _n_exc_blend,
            }

    # 有效样本（唯一信号口径）汇总：用于说明还差多少样本才允许自动应用
    try:
        pnl_stat = db_conn.execute(
            "SELECT COUNT(DISTINCT log_id) AS n_eval, COUNT(*) AS n_rows FROM signal_pnl"
        ).fetchone()
        log_stat = db_conn.execute(
            "SELECT COUNT(*) AS n_signals, "
            "SUM(CASE WHEN push_price > 0 THEN 1 ELSE 0 END) AS n_priced "
            "FROM signal_log"
        ).fetchone()
        n_evaluated_total = int(pnl_stat[0] or 0) if pnl_stat else 0
        n_pnl_rows = int(pnl_stat[1] or 0) if pnl_stat else 0
        n_log_total = int(log_stat[0] or 0) if log_stat else 0
        n_priced = int(log_stat[1] or 0) if log_stat else 0
    except Exception as e:
        logger.debug("[optimize] 成熟度样本统计失败（忽略）: %s", e)
        n_evaluated_total = 0
        n_pnl_rows = 0
        n_log_total = 0
        n_priced = 0

    maturity = {
        "available_n_days": all_available,
        "primary_n_days": global_primary,
        "missing_n_days": missing_n,
        "n_days_priority": list(N_DAYS_PRIORITY),
        "total_evaluated": n_evaluated_total,
        "total_pnl_rows": n_pnl_rows,
        "signals_total": n_log_total,
        "signals_ready": n_priced,
        "signals_pending": max(0, n_log_total - n_priced),
        "lookback_days": int(lookback_days),
        "min_n_for_auto_apply": int(min_n_for_auto_apply),
        "auto_apply_enabled": bool(auto_apply),
        "next_auto_apply_remaining": max(0, min_n_for_auto_apply - n_evaluated_total),
        "stat_basis": "每个战法取最新已成熟 N 档；同一 (pool, strategy, stat_n_days) 只取最新 stat_date",
        "updated_at": _now_iso(),
    }

    # 落盘 weight_recommendation（始终落盘，便于人工 review）
    try:
        os.makedirs(os.path.dirname(RECOMMENDATION_FILE) or ".", exist_ok=True)
        with open(RECOMMENDATION_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "recommendation": weight_recommendation,
                    "net_recommendation": net_weight_recommendation,
                    "stats": stats,
                    "maturity": maturity,
                    "strategy_maturity": strategy_maturity,
                    "deprecated": deprecated,
                    "mode": mode,
                    "updated_at": _now_iso(),
                },
                f,
                ensure_ascii=False,
                indent=2,
            )
    except Exception as e:
        logger.warning("[optimize] 落盘 recommendation 失败: %s", e)

    # 仅在 auto_applied 时才写 weights / deprecated
    if mode == "auto_applied":
        try:
            with open(WEIGHTS_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {"weights": weights, "stats": stats, "updated_at": _now_iso()},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            with open(DEPRECATED_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {"deprecated": deprecated, "stats": stats, "updated_at": _now_iso()},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        except Exception as e:
            logger.warning("[optimize] 落盘失败: %s", e)

    logger.info(
        "[optimize] 战法权重：mode=%s, 推荐=%d, 弃用候选=%d, 成熟档位=%s, 缺失档位=%s, "
        "距自动应用还差=%d 条",
        mode, len(weight_recommendation), len(deprecated),
        maturity.get("available_n_days"), maturity.get("missing_n_days"),
        maturity.get("next_auto_apply_remaining", 0),
    )

    return {
        "weights": weights,
        "weight_recommendation": weight_recommendation,
        "net_weight_recommendation": net_weight_recommendation,
        "deprecated": deprecated,
        "stats": stats,
        "maturity": maturity,
        "strategy_maturity": strategy_maturity,
        "mode": mode,
    }


def _now_iso() -> str:
    from datetime import datetime, timezone, timedelta
    return datetime.now(timezone(timedelta(hours=8))).isoformat()


def get_strategy_weight(strategy: str, default: float = 1.0) -> float:
    """
    从 strategy_weights.json 读取某战法的权重（缺失则 default）。
    """
    try:
        if not os.path.exists(WEIGHTS_FILE):
            return default
        with open(WEIGHTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return float(data.get("weights", {}).get(strategy, default))
    except Exception:
        return default


def list_deprecated_strategies() -> List[str]:
    """读取待人工审核的弃用战法清单"""
    try:
        if not os.path.exists(DEPRECATED_FILE):
            return []
        with open(DEPRECATED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return list(data.get("deprecated", []) or [])
    except Exception:
        return []
