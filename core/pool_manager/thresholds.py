# -*- coding: utf-8 -*-
"""
core/pool_manager/thresholds.py
原 core/pool_manager.py L96-L578（配置常量 + 排序 + 容错补偿 + P1 阈值读取 + 行为判定）

【职责】
- P1 前置防线阈值常量（已迁移至 config.yaml；此处保留文档/旧代码兼容占位）
- _p1_min_circ_mv_yi_pool / _p1_mv_yi_for_thr / _load_p1_min_score（市值/分数闸）
- _p1_regime_profile_key / get_p1_threshold_summary / get_p1_threshold_profile_label（环境档位）
- _p1_final_sort_key / _build_sector_rank_map（排序与排名映射）
- _compute_p1_compensation_tier / _effective_p1_thresholds（容错补偿档 + 串联阈值）
- _p1_is_high_cooldown / _p1_is_two_day_below_ma20 / _p1_is_single_peak_dense / _p1_behavior_penalty_hits
  （行为判定：高位冷却 + 深海线 + 单峰密集 + 长上影/冲高回落）
- _safe_float / _finite_clip01（数值防爆工具）

【跨包依赖】
- core.pool_manager.paths（暂不直接依赖；仅留 placeholder）
- constants（P1_INSTITUTION_MIN_PRICE_YUAN / STRATEGY_MIN_CIRC_MV_WAN / FUND_MEMORY_WEIGHT_P1）
"""
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

import constants

logger = logging.getLogger(__name__)

# ==================== P1 前置防线阈值 ====================
# 数值已迁移至 config.yaml strategies.p1.profiles；此处常量仅作文档/旧代码兼容占位。
P1_TREND_MA120_MIN_RATIO = 0.98
P1_TREND_SLOPE_FASTPASS = 0.25
P1_NEAR_MA20_MIN_RATIO = 0.985
# 【F3-1】MA20 之上前闸的窗口/站上日数（已迁移至 config.yaml profiles.<档>，此处仅作文档占位）
P1_MA20_PREGATE_LOOKBACK = 3
P1_MA20_PREGATE_ABOVE_DAYS = 2
P1_MACD_BAR_KILL = -0.13
P1_VOL_DIVERGENCE_RATIO = 0.85
P1_PASS_LINE = 50.0

# ---------- 资金共振截面分（已废弃；保留空壳函数名，避免旧快照/外链 import 报错）----------
# 流通市值单位：亿元（由 TuShare circ_mv 万元 / 10000）；≥300 亿仍用于「超级大盘」短线均线/MACD 放宽。
P1_MV_SUPER_YI = 300.0
# 资金特赦 is_amnesty：dynamic_inflow_threshold 在按市值比例上浮后硬性封顶（元），防止千亿超级中军线性比例过高导致特赦失效
P1_AMNESTY_DYNAMIC_INFLOW_CAP_YUAN = 1_500_000_000.0  # 15 亿


def p1_capital_resonance_hard_gate(circ_mv_yi: float, crs: float) -> Optional[str]:
    """历史接口：P1 曾按 capital_resonance_score 分层硬闸；现已废止，始终放行。"""
    return None


def p1_capital_resonance_scan_secondary_gate(
    circ_mv_yi: float, crs: float, curr_row: Any
) -> Optional[str]:
    """历史接口：scan_engine 曾用 CRS 二次闸；现已废止，始终放行。"""
    return None


def _p1_final_sort_key(hit_item: Dict[str, Any]) -> Tuple[float, float, float, float, float, float]:
    """
    P1 主池/观察池排序：5 因子加权（与 p1_rank_enhancer.SORT_WEIGHTS 对齐）。

    主键 p1_score（权重 1.00）+ 次键资金流入复合（0.10）+ 5 个 V26.80 增强因子：
      - 连板高度（0.15）
      - 题材新颖度（0.20）
      - 北向加持（0.10）
      - 时间衰减（0.05）

    【V26.88+10-1 P2-1 优化】把 p1_rank_enhancer.calc_* 系列真正接到排序键，
    避免 5300 票内"同分随机排序"导致的题材集中 / 老龙头卡位问题。
    旧版只有 (p1_score, inflow_ratio*0.6) 两键。
    """
    from core.p1_rank_enhancer import (
        calc_limit_grade_bonus,
        calc_sector_momentum_bonus,
        calc_north_buy_bonus,
        calc_recency_bonus,
    )

    p1 = float(hit_item.get("p1_score", 0.0) or 0.0)
    # 资金流入复合（原次键）
    inf = float(hit_item.get("inflow_ratio", 0.0) or 0.0) + float(hit_item.get("inflow_10d_ratio", 0.0) or 0.0) * 0.6
    inf_term = inf * 0.10

    # 5 个增强因子（每次排序按当前 hit_item 实时计算）
    limit_term = calc_limit_grade_bonus(hit_item) * 0.15
    sector_term = calc_sector_momentum_bonus(hit_item) * 0.20
    north_term = calc_north_buy_bonus(hit_item) * 0.10
    recency_term = calc_recency_bonus(hit_item) * 0.05

    return (p1, inf_term + limit_term + sector_term + north_term + recency_term, 0.0, 0.0, 0.0, 0.0)


def _build_sector_rank_map(sorted_sectors: list) -> Dict[str, int]:
    """预构建行业名 -> 排名映射，避免在单票循环中重复 index() 扫描。"""
    if not sorted_sectors:
        return {}
    return {str(name): idx + 1 for idx, name in enumerate(sorted_sectors) if name}


def _get_regime_thresholds(regime_name=None):
    """按市场环境返回阈值配置（自 config.yaml strategies.p1 读取，含策略实验室会话覆写）。"""
    from core.config_manager import get_p1_regime_thresholds

    return get_p1_regime_thresholds(regime_name)


def get_p1_threshold_profile_label(regime_name=None):
    """与 _get_regime_thresholds 档位一致的人类可读标签（供侧边栏展示）。"""
    name = str(regime_name or "").strip()
    if any(k in name for k in ["主升", "趋势"]):
        return "趋势市·严格精选"
    if any(k in name for k in ["退潮", "空头", "主跌"]):
        return "退潮/空头·适度放宽"
    return "震荡市·稳健中性"


def _p1_regime_profile_key(regime_name=None) -> str:
    """与 config_manager._profile_key_for_regime 一致，供 P1 单股处理线程识别 strict/neutral/relaxed。"""
    name = str(regime_name or "").strip()
    if any(k in name for k in ["主升", "趋势"]):
        return "strict"
    if any(k in name for k in ["退潮", "空头", "主跌"]):
        return "relaxed"
    return "neutral"


def get_p1_threshold_summary(regime_name=None):
    """供 UI 展示：当前大盘环境对应的 P1 阈值档与数值（与 build_p1_pool_and_cache 使用同一套逻辑）。"""
    t = _get_regime_thresholds(regime_name)
    return {
        "regime_input": str(regime_name or "").strip() or "(默认)",
        "profile_label": get_p1_threshold_profile_label(regime_name),
        "thresholds": t.copy(),
    }


# ==================== 1. 跨部门资源调配 (任督二脉贯通) ====================
def _p1_min_circ_mv_yi_pool() -> float:
    """与 scan_engine / db_core.get_p1_candidate_codes 共用；读 config 后回退 constants。

    【V26.88+ 9-26】双闸重构：流通下限读 STRATEGY_MIN_CIRC_MV_WAN（默认 120 亿），
    数据下载下限读 DATA_FETCH_MIN_TOTAL_MV_WAN（默认 120 亿）；二者解耦。
    """
    try:
        from core.config_manager import get_strategy_min_circ_mv_wan

        return float(get_strategy_min_circ_mv_wan()) / 10000.0
    except Exception:
        return float(getattr(constants, "STRATEGY_MIN_CIRC_MV_WAN", 1_200_000)) / 10000.0


def _p1_mv_yi_for_thr(thresholds: dict) -> float:
    if isinstance(thresholds, dict):
        v = thresholds.get("_p1_min_circ_mv_yi")
        if v is not None:
            try:
                return float(v)
            except (TypeError, ValueError):
                pass
    return _p1_min_circ_mv_yi_pool()


# [V26.88+9-29 ST-P1-N 改造] P1 池准入分数闸读取器
def _load_p1_min_score(regime_name=None) -> Optional[float]:
    """读 config.yaml 的 min_score。【F1-1】按 regime 联动：
    - profiles.<profile>.min_score 优先
    - 退而求其次：strategies.p1.min_score（兜底兼容旧配置）
    - 缺失 / 解析失败 / ≤ 0 → 返回 None（视为不启用分数闸）

    与 pass_line 的分工：
    - pass_line 是「单票打分量身定制的环境及格线」（按大盘环境三档 45/48/50）
    - min_score 是「入池的绝对底线」（F1-1 改造后与 regime 联动：strict=65/neutral=58/relaxed=50）
    """
    profile_key = _p1_regime_profile_key(regime_name)
    try:
        from core.config_manager import get_config
        cfg = get_config()
        strategies = (cfg.get("strategies") or {}) if isinstance(cfg, dict) else {}
        p1_cfg = (strategies.get("p1") or {}) if isinstance(strategies, dict) else {}
        # 【F1-1】优先 profiles.<profile>.min_score
        profiles = (p1_cfg.get("profiles") or {}) if isinstance(p1_cfg, dict) else {}
        prof_cfg = profiles.get(profile_key) if isinstance(profiles, dict) else None
        v = None
        if isinstance(prof_cfg, dict) and prof_cfg.get("min_score") is not None:
            v = prof_cfg.get("min_score")
        else:
            # 兜底：旧版 strategies.p1.min_score
            v = p1_cfg.get("min_score")
        if v is None:
            return None
        score = float(v)
        if score <= 0.0:
            return None
        return score
    except Exception:
        return None


def _p1_mv_fail_reason_for(mv_yi_bar: float) -> str:
    return f"流通市值不足{int(mv_yi_bar)}亿"


def _read_observation_streak() -> int:
    """【V26.88+10-3 P3-⑪】读取当前观察池激活 streak（连续多少天观察池被启用）。"""
    try:
        import os
        import json
        from core.pool_manager.paths import path_wash_metrics_json
        p = path_wash_metrics_json()
        if not os.path.isfile(p):
            return 0
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return 0
        meta = data.get("__tier_pool_meta__")
        if not isinstance(meta, dict):
            return 0
        return int(meta.get("p1_obs_streak", 0) or 0)
    except Exception:
        return 0


# ============ 容错导入：fund_mv_utils / db_core / fallback ============
try:
    from core.strategies.fund_mv_utils import (
        infer_turnover_rate_f_pct,
        compute_market_contraction_context,
        adaptive_turnover_kill_threshold_relaxed,
        turnover_kill_threshold_pct,
    )
except ImportError:

    def infer_turnover_rate_f_pct(vol_hand, close, circ_mv_wan):
        if vol_hand <= 0 or close <= 0 or circ_mv_wan <= 0:
            return 0.0
        return vol_hand * close / circ_mv_wan

    def compute_market_contraction_context(base_items, rt_map=None):
        return {"score": 0.0, "adaptive_reason": ""}

    def adaptive_turnover_kill_threshold_relaxed(
        circ_mv_yi, net_main_amount=None, net_elg_amount=None
    ):
        return 0.8

    def turnover_kill_threshold_pct(circ_mv_yi):
        """V31-B2 容错导入兜底：退回旧硬阶梯，保证无 fund_mv_utils 时行为不退化。"""
        try:
            mv = float(circ_mv_yi)
        except (TypeError, ValueError):
            return 1.50
        if mv >= 2000.0:
            return 0.65
        if mv >= 1000.0:
            return 1.05
        return 1.50


try:
    from data.db_core import (
        get_all_basic_industry,
        get_latest_sector_ranking,
        get_latest_daily_data_trade_date_yyyymmdd,
    )
except ImportError:
    def get_all_basic_industry(): return {}
    def get_latest_sector_ranking(): return {}
    def get_latest_daily_data_trade_date_yyyymmdd(): return ""


# ============ 数值防爆工具 ============
def _safe_float(val, default=0.0):
    if val is None: return default
    try:
        if pd.isna(val) or str(val).strip() in ['', '-']: return default
        return float(val)
    except (ValueError, TypeError):
        return default


def _macd_hist_1x(row) -> float:
    """
    MACD 柱（1× 统一口径）。

    【V30.10 统一 MACD 口径 2026-10-21】
    生产 daily_data 的 macd_hist 是【1× 口径】(macd - macd_signal)，
    而 indicator_calc 算出的 macd_bar 是【2× 口径】(diff - dea) * 2。
    绝对值阈值（如 macd_bar_kill = -0.09 / -0.13 / -0.22）只能在同一尺度下比较，
    否则宽严整整差一倍。这里统一折算成 1×。
    """
    try:
        from core.strategies.macd_unified import macd_hist_normalized

        v = macd_hist_normalized(row)
        return float(v) if v is not None else 0.0
    except Exception:
        return _safe_float(row.get("macd_hist", 0.0)) if hasattr(row, "get") else 0.0


def _finite_clip01(x):
    """把任意标量压成有限数，避免 np.interp 或加减分链条被 NaN/inf 污染。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not np.isfinite(v):
        return 0.0
    return v


def _compute_p1_compensation_tier(
    curr,
    df,
    rt,
    now_price,
    net_inflow_3d,
    net_inflow_5d_early,
    vr,
    ind_rank,
    dynamic_inflow_threshold,
    is_amnesty,
    ma20,
    ma60,
    ma120,
    ma20_slope_5,
    circ_mv_yi=0.0,  # 【V26.88+10-3 P1-⑤】新增：流通市值（亿元），用于"市值占比"型 Tier 2 判定
):
    """
    P1 容错补偿档位（0/1/2），用于在「形态好 + 资金真抢」时，适度放宽 MACD 绿柱、MA120 粘合度、量价背离等硬阈值。

    设计意图（用人话说清楚）：
    - 老逻辑像一排串联闸门，任何一扇关死就出局；洗盘刚结束、均线刚多头排列但 MACD 还在水下缓慢抬头的票，经常被误杀。
    - 这里不推翻闸门，而是：当你已经满足「资金明显流入」或「直通车/特赦同类信号」时，允许闸门略放宽一点，减少漏掉真龙骨的概率。
    - 档位 capped 在 2，避免无上限放宽把垃圾股全放进来。

    返回:
        tier: int, 0 表示不补偿，1 轻度，2 更强（需强资金或直通车类信号支撑）。
        note: str, 供入选理由拼接，方便复盘「为什么这条票享受了放宽」。
    """
    tier = 0
    notes = []

    # 【V26.88+10-3 P1-⑤】容错 Tier 2 拆 3 选 2：
    # 三个独立条件任一命中即可获得 Tier 2（实战触发率从 ~5% 提升到 ~25%）
    #   - 资金特赦：原 is_amnesty（连续 5 日净流入 + VR > 2.5 + 行业 Top 5 + 价格保险）
    #   - 强流入形态：近 3 日净流入 > 流通市值 × 0.035 + 价格回踩 MA20 不破位 + 当日放量
    #   - 板块龙头加速：VR > 2.0 + 行业排名 ≤ 8 + 5 日连续正流入
    _tier2_hit_a = bool(is_amnesty)
    _tier2_hit_b = (
        net_inflow_3d > 0
        and circ_mv_yi > 0
        and (net_inflow_3d / max(circ_mv_yi * 1e8, 1.0)) >= 0.035
        and ma20 > 0
        and now_price >= ma20 * 0.985
        and vr > 1.5
    )
    _tier2_hit_c = bool(
        ind_rank is not None and 0 < int(ind_rank) <= 8
        and vr > 2.0
        and net_inflow_3d > 0
    )
    if _tier2_hit_a or _tier2_hit_b or _tier2_hit_c:
        tier = 2
        if _tier2_hit_a:
            notes.append("资金特赦档")
        if _tier2_hit_b:
            notes.append("强流入形态档")
        if _tier2_hit_c:
            notes.append("板块龙头加速档")

    # 筹码单峰密集：95%成本位与50%成本位高度接近，且现价贴近成本中枢时，给更高容错
    c50 = _safe_float(curr.get("cost_50th", rt.get("cost_50th", 0.0)))
    c95 = _safe_float(curr.get("cost_95th", rt.get("cost_95th", 0.0)))
    now_px = _safe_float(now_price, 0.0)
    if c50 > 0 and c95 > 0:
        peak_gap_pct = (c95 - c50) / max(c50, 1e-9) * 100.0
        if peak_gap_pct <= 8.0 and now_px >= c50 * 0.97 and now_px <= c95 * 1.02:
            tier = max(tier, 1)
            notes.append("筹码单峰密集")

    th_dyn = _finite_clip01(dynamic_inflow_threshold)
    ni3 = _finite_clip01(net_inflow_3d)
    if th_dyn > 0 and ni3 >= th_dyn * 0.52:
        tier = max(tier, 2)
        notes.append("三日净流入达动态门槛约52%以上")
    elif ni3 > 0 and vr >= 1.85 and ind_rank <= 7:
        tier = max(tier, 1)
        notes.append("放量配合且行业排名靠前")

    m20, m60 = _safe_float(ma20), _safe_float(ma60)
    slope5 = _safe_float(ma20_slope_5)
    px = _safe_float(now_price)

    if m20 > 0 and m60 > 0:
        if m20 > m60 and slope5 >= 0.11:
            tier = max(tier, 1)
            notes.append("短均线多头且20斜率抬头")
        if px >= m60 * 0.997 and m20 >= m60 * 0.992 and px >= m20 * 0.985 and px <= m20 * 1.028:
            if _finite_clip01(net_inflow_5d_early) > 0:
                tier = max(tier, 1)
                notes.append("贴均线洗盘区五日资金为正")

    # 【V30.10 统一 MACD 口径 2026-10-21】原代码 macd_now > -0.09 是【绝对值阈值】，
    # 而取数 macd_bar(2× 口径) 优先、macd_hist(1× 口径) 兜底 → 同一阈值在两种尺度下
    # 宽严整整差一倍。现统一取【1× 归一口径】柱体（与生产 daily_data.macd_hist 对齐），
    # 阈值 -0.09 也按 1× 口径解释。
    macd_now = _macd_hist_1x(curr)
    macd_prev = 0.0
    if isinstance(df, pd.DataFrame) and len(df) >= 2:
        macd_prev = _macd_hist_1x(df.iloc[-2])
    if macd_now > macd_prev and macd_now > -0.09:
        tier = max(tier, 1)
        notes.append("MACD动能柱拐头改善")

    tier = int(max(0, min(2, tier)))
    note = "；".join(notes[:4]) if notes else ""
    return tier, note


def _effective_p1_thresholds(comp_tier, trend_ma120_min_ratio, macd_bar_kill, vol_divergence_ratio, near_ma20_min_ratio):
    """
    根据容错档位，生成「实际生效」的串联阈值。数值方向说明：
    - macd_bar_kill 越负越宽松（例如 -0.22 比 -0.13 更容易放过略弱的柱）。
    - trend_ma120_min_ratio 略降：允许 MA60 与 MA120 粘合度稍差一丝（主升浪初期常见）。
    - vol_divergence_ratio 略降：上涨日均量略低于下跌日均量时，仍可通过（修复刚反转时统计窗偏短的问题）。
    - near_ma20_min_ratio 略降：回踩 MA20 略深一点仍算「贴线企稳」。
    """
    ct = int(max(0, min(2, comp_tier)))
    t120 = float(trend_ma120_min_ratio) - 0.005 * (1 if ct >= 1 else 0) - 0.006 * (1 if ct >= 2 else 0)
    t120 = max(0.964, min(1.0, t120))

    mk = float(macd_bar_kill) - 0.055 * (1 if ct >= 1 else 0) - 0.065 * (1 if ct >= 2 else 0)
    mk = max(-0.40, min(0.0, mk))

    vd = float(vol_divergence_ratio) - 0.055 * (1 if ct >= 1 else 0) - 0.065 * (1 if ct >= 2 else 0)
    vd = max(0.60, min(0.95, vd))

    nm = float(near_ma20_min_ratio) - 0.0035 * (1 if ct >= 1 else 0) - 0.004 * (1 if ct >= 2 else 0)
    nm = max(0.975, min(0.995, nm))

    return t120, mk, vd, nm


def _p1_is_high_cooldown(bias20_cool: float, avg_trn: float) -> bool:
    return (15.0 <= float(bias20_cool) <= 25.0) and (float(avg_trn) <= 2.2)


def _p1_is_two_day_below_ma20(close_today: float, ma20_today: float, close_yesterday: float, ma20_yesterday: float) -> bool:
    return (ma20_today > 0) and (close_today < ma20_today) and (close_yesterday < ma20_yesterday)


def _p1_is_single_peak_dense(curr, rt, now_price: float) -> bool:
    c50_p1 = _safe_float(curr.get("cost_50th"), _safe_float(rt.get("cost_50th"), 0.0))
    c95_p1 = _safe_float(curr.get("cost_95th"), _safe_float(rt.get("cost_95th"), 0.0))
    if c50_p1 <= 0 or c95_p1 <= 0:
        return False
    peak_gap = (c95_p1 - c50_p1) / max(c50_p1, 1e-9) * 100.0
    return (peak_gap <= 8.0) and (now_price >= c50_p1 * 0.97) and (now_price <= c95_p1 * 1.02)


def _p1_behavior_penalty_hits(df, lookback_days: int = 20) -> Tuple[int, int]:
    long_upper_shadow_hits = 0
    intraday_dump_hits = 0
    recent_20 = df.tail(int(lookback_days)).copy() if isinstance(df, pd.DataFrame) else pd.DataFrame()
    if not recent_20.empty:
        for _, rr in recent_20.iterrows():
            high_v = _safe_float(rr.get("high", 0.0))
            low_v = _safe_float(rr.get("low", 0.0))
            open_v = _safe_float(rr.get("open", 0.0))
            close_v = _safe_float(rr.get("close", 0.0))
            if high_v > 0 and low_v > 0 and open_v > 0 and close_v > 0:
                body = abs(close_v - open_v)
                upper_shadow = max(0.0, high_v - max(open_v, close_v))
                if upper_shadow >= max(body * 0.5, 0.01):
                    long_upper_shadow_hits += 1
                if _safe_float(rr.get("pct_chg", 0.0)) > 4.0 and _safe_float(rr.get("close", 0.0)) < _safe_float(rr.get("open", 0.0)):
                    intraday_dump_hits += 1
    return long_upper_shadow_hits, intraday_dump_hits


# ==================== 【V30.14 2026-11-05】防一日游 ====================
# 用户诉求：前期跌得多、现在开始涨的票要能进池，但必须防「一日游」。
#
# 设计原则（按形态而不是按涨幅拦）：
#   · 强势但**收在高位**的票（阳线、收盘贴近当日最高、连续走高）→ 放行
#   · 拉高后**回落**（收在当日下三分之一）、放量滞涨、高位长上影 → 一日游嫌疑
# 只对「拉高回落」这类当日形态做强否决，其余一律降分，
# 避免把「跌深后第一根放量反转阳线」误杀（那恰恰是要抓的启动点）。
def _p1_one_day_trip_check(
    df,
    curr=None,
    pct_chg_today: float = 0.0,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """返回 {"veto": str|None, "penalty": float, "labels": [..]}。

    任何异常都返回空结论（fail-open），绝不阻断 P1 主链路。
    """
    c = cfg if isinstance(cfg, dict) else {}
    res: Dict[str, Any] = {"veto": None, "penalty": 0.0, "labels": []}
    if not bool(c.get("enabled", True)):
        return res
    try:
        if not isinstance(df, pd.DataFrame) or len(df) < 3:
            return res
        row = curr if curr is not None else df.iloc[-1]
        o = _safe_float(row.get("open", 0.0))
        h = _safe_float(row.get("high", 0.0))
        low = _safe_float(row.get("low", 0.0))
        cl = _safe_float(row.get("close", 0.0))
        if min(o, h, low, cl) <= 0 or h < low:
            return res
        pct = _safe_float(pct_chg_today, _safe_float(row.get("pct_chg", 0.0)))

        rng = h - low
        close_pos = ((cl - low) / rng) if rng > 1e-9 else 1.0   # 收盘在当日区间的相对位置 0~1
        upper_shadow_ratio = ((h - max(o, cl)) / rng) if rng > 1e-9 else 0.0
        pullback_from_high = (h - cl) / max(cl, 1e-9) * 100.0

        vol_t = _safe_float(row.get("vol", 0.0))
        vol_y = _safe_float(df.iloc[-2].get("vol", 0.0)) if len(df) >= 2 else 0.0
        vol_mult = (vol_t / vol_y) if vol_y > 0 else 0.0

        ma20 = _safe_float(row.get("ma20", 0.0))
        if ma20 <= 0 and "ma20" in df.columns:
            ma20 = _safe_float(df["ma20"].iloc[-1], 0.0)

        _gain_3d = _safe_float(df["pct_chg"].tail(3).sum(), 0.0) if "pct_chg" in df.columns else 0.0
        _gain_5d = _safe_float(df["pct_chg"].tail(5).sum(), 0.0) if "pct_chg" in df.columns else 0.0
        _prev_5d = _safe_float(df["pct_chg"].tail(6).head(5).sum(), 0.0) if "pct_chg" in df.columns and len(df) >= 6 else 0.0

        # ---- 强否决 1：拉高回落 ----
        pump_min = float(c.get("pump_pct_min", 5.0))
        pos_max = float(c.get("close_position_max", 0.32))
        pb_min = float(c.get("pullback_from_high_min_pct", 3.0))
        if pct >= pump_min and close_pos <= pos_max and pullback_from_high >= pb_min:
            res["veto"] = (
                f"🎢一日游嫌疑(冲高回落:涨{pct:.1f}%但收盘回落{pullback_from_high:.1f}%,"
                f"收在当日{close_pos*100:.0f}%位置)"
            )
            res["labels"].append("冲高回落")
            return res

        # ---- 强否决 2：放量闷杀（大涨次日放巨量收阴）----
        dump_vr = float(c.get("dump_vol_mult_min", 2.5))
        dump_pct = float(c.get("dump_pct_max", -3.0))
        if vol_mult >= dump_vr and pct <= dump_pct and cl < o:
            res["veto"] = (
                f"🎢一日游嫌疑(放量杀跌:量能{vol_mult:.1f}倍但收{pct:.1f}%)"
            )
            res["labels"].append("放量闷杀")
            return res

        # ---- 降分 1：高位长上影（3 日已大涨后出现长上影）----
        hs_min = float(c.get("high_shadow_ratio_min", 0.50))
        g3_min = float(c.get("high_shadow_3d_gain_min", 15.0))
        if upper_shadow_ratio >= hs_min and _gain_3d >= g3_min:
            res["penalty"] += float(c.get("high_shadow_penalty", 4.0))
            res["labels"].append(f"高位长上影(上影{upper_shadow_ratio*100:.0f}%,3日涨{_gain_3d:.1f}%)")

        # ---- 降分 2：暴跌后单日暴涨但没站上 MA20（反弹一日游）----
        plung_min = float(c.get("plunge_5d_max", -8.0))
        spike_min = float(c.get("spike_pct_min", 7.0))
        if _prev_5d <= plung_min and pct >= spike_min and (ma20 <= 0 or cl < ma20):
            res["penalty"] += float(c.get("plunge_spike_penalty", 3.0))
            res["labels"].append(f"跌深单日暴涨未站MA20(前5日{_prev_5d:.1f}%,当日{pct:.1f}%)")

        # ---- 降分 3：5 日累计涨幅过热且当日收盘位置偏低 ----
        hot_5d = float(c.get("overheat_5d_pct", 30.0))
        if _gain_5d >= hot_5d and close_pos <= float(c.get("overheat_close_pos_max", 0.45)):
            res["penalty"] += float(c.get("overheat_penalty", 3.0))
            res["labels"].append(f"5日过热且收盘偏弱(5日{_gain_5d:.1f}%)")

        return res
    except Exception as e:  # pragma: no cover - 防御性
        logging.debug("[one-day-trip] 判定异常(忽略): %s", e)
        return res


# ==================== 【V30.14 2026-11-05】千亿蓝筹加分 ====================
# 用户诉求：1000 亿以上的蓝筹票「只要分数可以」就该能进池，打分要为这类票优化。
# 背景：2026-11-05 实测 1041 只候选只入池 30 只，且清一色低波动防守票；
# 阳光电源（流通市值 1288 亿）连涨 3 日因打分不够被挡在池外。
#
# 设计：不无条件抬分（否则所有千亿票都能进池），只奖励「大票该有的确定性」：
#   ① 千亿中军基础分（流动性/容量优势）  ② 中期均线多头排列且上行
#   ③ 主力连续净流入                     ④ 温和放量上涨（不是巨量拉高）
# 四项合计有上限，防止垃圾千亿票被抬进池。
def _p1_large_cap_bonus(
    circ_mv_yi: float,
    curr,
    df=None,
    vol_ratio: float = 1.0,
    pct_chg_today: float = 0.0,
    flow_positive_days_5: int = 0,
    cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """返回 {"bonus": float, "labels": [..], "turnover_relax": float}。异常时零加分。"""
    c = cfg if isinstance(cfg, dict) else {}
    out: Dict[str, Any] = {"bonus": 0.0, "labels": [], "turnover_relax": 1.0}
    if not bool(c.get("enabled", True)):
        return out
    try:
        mv_min = float(c.get("min_circ_mv_yi", 1000.0))
        mv = _safe_float(circ_mv_yi, 0.0)
        if mv < mv_min:
            return out

        # 千亿票天然低换手：换手底线按系数放宽（只影响股性呆滞闸，不放宽市值闸）
        out["turnover_relax"] = float(c.get("turnover_relax_factor", 0.75))

        bonus = float(c.get("base_bonus", 1.5))
        labels = [f"千亿中军({mv:.0f}亿)+{bonus:.1f}"]

        ma20 = _safe_float(curr.get("ma20", 0.0))
        ma60 = _safe_float(curr.get("ma60", 0.0))
        ma120 = _safe_float(curr.get("ma120", 0.0))
        slope5 = _safe_float(curr.get("ma20_slope_5", 0.0))
        if ma20 > 0 and ma60 > 0 and ma120 > 0 and ma20 >= ma60 >= ma120 and slope5 >= 0:
            b = float(c.get("trend_bonus", 1.5))
            bonus += b
            labels.append(f"均线多头+{b:.1f}")

        if int(flow_positive_days_5 or 0) >= int(c.get("inflow_days_min", 3)):
            b = float(c.get("inflow_bonus", 1.0))
            bonus += b
            labels.append(f"主力连续流入({int(flow_positive_days_5)}日)+{b:.1f}")

        _vr = _safe_float(vol_ratio, 1.0)
        _lo = float(c.get("mild_volume_ratio_min", 1.1))
        _hi = float(c.get("mild_volume_ratio_max", 3.0))
        _pmin = float(c.get("mild_pct_min", 1.0))
        _pmax = float(c.get("mild_pct_max", 7.0))
        if _lo <= _vr <= _hi and _pmin <= _safe_float(pct_chg_today, 0.0) <= _pmax:
            b = float(c.get("mild_volume_bonus", 1.0))
            bonus += b
            labels.append(f"温和放量({_vr:.2f}倍)+{b:.1f}")

        cap = float(c.get("max_bonus", 5.0))
        out["bonus"] = float(max(0.0, min(bonus, cap)))
        out["labels"] = labels
        return out
    except Exception as e:  # pragma: no cover - 防御性
        logging.debug("[large-cap-bonus] 判定异常(忽略): %s", e)
        return {"bonus": 0.0, "labels": [], "turnover_relax": 1.0}


__all__ = [
    "P1_TREND_MA120_MIN_RATIO", "P1_TREND_SLOPE_FASTPASS",
    "P1_NEAR_MA20_MIN_RATIO", "P1_MACD_BAR_KILL",
    "P1_MA20_PREGATE_LOOKBACK", "P1_MA20_PREGATE_ABOVE_DAYS",
    "P1_VOL_DIVERGENCE_RATIO", "P1_PASS_LINE",
    "P1_MV_SUPER_YI", "P1_AMNESTY_DYNAMIC_INFLOW_CAP_YUAN",
    "p1_capital_resonance_hard_gate", "p1_capital_resonance_scan_secondary_gate",
    "_p1_final_sort_key", "_build_sector_rank_map",
    "_get_regime_thresholds", "get_p1_threshold_profile_label",
    "_p1_regime_profile_key", "get_p1_threshold_summary",
    "_p1_min_circ_mv_yi_pool", "_p1_mv_yi_for_thr",
    "_load_p1_min_score", "_p1_mv_fail_reason_for",
    "infer_turnover_rate_f_pct", "compute_market_contraction_context",
    "adaptive_turnover_kill_threshold_relaxed",
    "turnover_kill_threshold_pct",
    "get_all_basic_industry", "get_latest_sector_ranking",
    "get_latest_daily_data_trade_date_yyyymmdd",
    "_safe_float", "_finite_clip01",
    "_compute_p1_compensation_tier", "_effective_p1_thresholds",
    "_p1_is_high_cooldown", "_p1_is_two_day_below_ma20",
    "_p1_is_single_peak_dense", "_p1_behavior_penalty_hits",
    "_p1_one_day_trip_check", "_p1_large_cap_bonus",
]
