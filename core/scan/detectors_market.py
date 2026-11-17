# -*- coding: utf-8 -*-
"""
core/scan/detectors_market.py
原 core/scan_engine.py L619-L681（性格评估）+ L680-L1154（核心探测器 + 实时板块）+

【职责】
- 个股性格评估器：_evaluate_personality（vr_rank-aware 与 fallback 双路径）
- 安全因子：_calc_safety_factor（连板梯队 + 乖离豁免 + 短期超买 + 筹码脱离 + 趋势市加权）
- ATR 衰减：_calc_decay_factor_atr
- 仓位：_calc_position（退潮封顶 / 主升 / 游击 / 压舱）
- 防守线：_calc_defense_line
- 拥挤度惩罚：_calc_crowding_penalty（mega-cap 豁免）
- P3 右侧防追高：_p3_right_side_guard
- 执行分层：_execution_tier_for_pool（A/B/C + 门禁放宽自动降档）
- 门禁诊断文案：_infer_golden_gate_reason（P2/P3/P4/P5 分池统一口径）
- 优先标签：_build_priority_tags
- 实时板块排名：get_realtime_sector_ranking（落库回退 + 集合竞价前回退）

【公开 API】
- get_realtime_sector_ranking（唯一公开函数）
"""
import logging
from datetime import datetime

import numpy as np
import pandas as pd

from core.scan.utils import (
    BJ_TZ,
    _safe_float,
    get_latest_sector_ranking,
    get_all_basic_industry,
    fetch_realtime_batch,
    series_effective_turnover_f_daily,
    effective_turnover_rate_f,
    infer_turnover_rate_f_pct,
    personality_liquidity_blend,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [扫描引擎-market] %(message)s')


# ==================== 🧬 个股性格评估器 (Personality Evaluator) ====================
def _evaluate_personality(df_target, rt, pool_key, vr_rank=None):
    """
    vr_rank: 当次扫描截面量比百分位秩（0~1）。传入时用「历史换手倍数 × 截面量比秩」几何融合；
    未传入时回退旧版「分位换手 + 日内量历史百分位」线性混合。
    """
    if pool_key == 'p2':
        return 1.0

    if df_target is None or len(df_target) < 20:
        return 1.0

    df_hist = df_target.tail(60).copy()

    try:
        hist_series = series_effective_turnover_f_daily(df_hist)
        hist_trn_median = float(hist_series.median()) if len(hist_series) else 0.0
        y_last = df_hist.iloc[-1]
        now_px = _safe_float(rt.get("price", 0.0))
        if now_px <= 0:
            now_px = _safe_float(y_last.get("close"), 0.0)
        today_trn = effective_turnover_rate_f(rt, y_last, now_px)
        if hist_trn_median > 0.01 and today_trn > 0:
            trn_ratio = today_trn / max(hist_trn_median, 0.5)
            trn_multi = np.interp(trn_ratio, [0.5, 1.0, 2.5, 4.0, 6.0], [0.85, 1.0, 1.05, 1.10, 1.15])
        else:
            trn_multi = 1.0

        if vr_rank is not None and np.isfinite(float(vr_rank)):
            multiplier = personality_liquidity_blend(float(trn_multi), float(vr_rank))
        else:
            today_vol = _safe_float(rt.get('volume', 0))
            _pvc = 'vol' if 'vol' in df_hist.columns else ('volume' if 'volume' in df_hist.columns else None)
            if _pvc and today_vol > 0:
                vol_percentile = (df_hist[_pvc] < today_vol).mean() * 100.0
                vol_multi = np.interp(vol_percentile, [50, 70, 85, 95, 99], [0.90, 1.0, 1.05, 1.10, 1.15])
            else:
                vol_multi = 1.0
            multiplier = trn_multi * 0.5 + vol_multi * 0.5
            multiplier = max(0.85, min(1.15, float(multiplier)))

        # 禁止用默认 1.0 冒充昨收：缺 rt 时回退 T-1 日线 pre_close/close
        pre_close = _safe_float(rt.get("pre_close"), 0.0)
        if pre_close <= 0:
            pre_close = _safe_float(y_last.get("pre_close"), 0.0)
        if pre_close <= 0:
            pre_close = _safe_float(y_last.get("close"), 0.0)
        if pre_close <= 0:
            return round(multiplier, 3)
        today_pct = (_safe_float(rt.get("price", 0.0)) - pre_close) / pre_close * 100.0

        if today_pct < 0 and 'atr_pct' in df_hist.columns:
            hist_atr_median = df_hist['atr_pct'].median()
            if hist_atr_median > 0 and abs(today_pct) > hist_atr_median * 2.0:
                multiplier = 0.5

    except Exception as e:
        logging.debug(f"性格评估器计算异常: {e}")
        return 1.0

    return round(multiplier, 3)


# ==================== 2. 核心探测器 ====================
def _calc_safety_factor(df, rt, regime, industry, sorted_sectors, limit_times, is_empty_board):
    """综合安全因子：连板梯队、乖离、短期涨幅、筹码距离。"""
    if df is None or len(df) < 6:
        return 1.0, []
    try:
        curr = df.iloc[-1]
        now_price = _safe_float(rt.get('price', 0))
        ma20 = _safe_float(curr.get('ma20', now_price))
        if ma20 <= 0:
            return 1.0, []
        safety = 1.0
        tags = []
        is_core_sector = industry in sorted_sectors[:3]

        bias20 = (now_price - ma20) / ma20 * 100.0
        # 【审计修复】维度2-pct_5d 分母避免 sub-1 元股价时被 max(close,1) 压扁导致乖离误判
        _c5 = _safe_float(df.iloc[-6]['close'])
        pct_5d = (now_price - _c5) / max(_c5, 1e-9) * 100.0
        cost_50th = _safe_float(rt.get('cost_50th', now_price))

        if is_empty_board and limit_times >= 2:
            tags.append("💣[中空假龙]")
            safety *= 0.70
        else:
            if 2 <= limit_times <= 4 and is_core_sector:
                tags.append("👑[2-4板免死]")
            else:
                if limit_times >= 5:
                    safety *= 0.50
                    tags.append("💀[≥5板断头台]")

        exempt_high_bias = (
            2 <= limit_times <= 4
            and is_core_sector
            and (not is_empty_board)
        )
        if (not exempt_high_bias) and bias20 > 15.0:
            safety *= 0.60
            tags.append("⚠️[高位乖离]")

        if pct_5d > 20.0 and not (2 <= limit_times <= 4 and is_core_sector and not is_empty_board):
            safety *= 0.80
            tags.append("🔥[短期超买]")

        if cost_50th > 0 and now_price > cost_50th * 1.3 and not (2 <= limit_times <= 4 and is_core_sector and not is_empty_board):
            safety *= 0.85
            tags.append("☁️[脱离筹码]")

        if regime in ["趋势市", "主升浪", "趋势主升"]:
            safety = min(1.0, safety * 1.15)

        return safety, tags
    except Exception as e:
        logging.debug(f"_calc_safety_factor 异常: {e}")
        return 1.0, []


def _calc_decay_factor_atr(df, rt):
    try:
        if df is None or len(df) < 5: return 1.0
        now_vr = _safe_float(rt.get('vol_ratio', 0))
        now_price = _safe_float(rt.get('price', 0))

        df_tail_60 = df.tail(60).copy()
        recent_60_high = _safe_float(df_tail_60['high'].max())
        atr_pct = _safe_float(df.iloc[-1].get('atr_pct', 3.0), default=3.0)

        if atr_pct > 4.5: half_life = 2.0
        elif atr_pct < 2.5: half_life = 10.0
        else: half_life = 5.0

        if now_vr > 1.2 or now_price >= recent_60_high: return 1.0

        _vcol = 'vol' if 'vol' in df_tail_60.columns else ('volume' if 'volume' in df_tail_60.columns else None)
        _ma5_col = None
        for _c in ("vol_ma5", "vma5"):
            if _c in df_tail_60.columns:
                _ma5_col = _c
                break
        if _ma5_col and _vcol:
            cond_spike = df_tail_60[_vcol] > 1.2 * df_tail_60[_ma5_col]
        else:
            cond_spike = pd.Series(False, index=df_tail_60.index)

        cond_high = df_tail_60['high'] >= df_tail_60['high'].rolling(60, min_periods=1).max()
        combined_cond = (cond_spike | cond_high).values
        true_indices = np.where(combined_cond)[0]

        if len(true_indices) > 0: days_since = len(combined_cond) - 1 - true_indices[-1]
        else: days_since = 60

        if days_since <= 1: return 1.0
        return max(0.5, 0.5 ** (days_since / half_life))
    except Exception as e:
        logging.debug(f"_calc_decay_factor_atr 异常: {e}")
        return 1.0


def _calc_position(size_emoji, safety_factor, regime, is_precise_pullback):
    if regime in ["情绪退潮市", "主跌浪", "退潮防守"]:
        return "⚠️空仓期建议 · 最高10% (试错)"
    if regime in ["趋势市", "主升浪", "趋势主升"]:
        if size_emoji == "🐎": return "🔥主攻: 30%-40%"
        elif size_emoji in ["🦍", "🐘"]: return "🛡️压舱: 15%-20%"
    else:
        if size_emoji == "🐎": return "⚔️游击: 15%-20%"
        elif size_emoji in ["🦍", "🐘"]: return "🛡️均衡: 20%-30%"
    return "🛡️常规: 15%-20%"


def _calc_defense_line(df, rt, size_emoji):
    try:
        if df is None or len(df) < 20: return "--"
        now_price = _safe_float(rt.get('price', 0))
        if size_emoji in ["🦍", "🐘"]:
            ma20 = _safe_float(df.iloc[-1].get('ma20', 0))
            if ma20 > 0: return f"破20日线: {ma20:.2f}"
            return "破20日线止损"
        else:
            return "3日未强化则无条件退出"
    except Exception as e:
        logging.debug(f"_calc_defense_line 异常: {e}")
        return "--"


def _calc_crowding_penalty(pool_key, pct, vol_ratio, turnover_f, upper_shadow_pct, circ_mv_yi=0.0):
    """
    拥挤度惩罚（收益导向）：不做硬剔除，仅做分档降权。
    流通市值 circ_mv_yi≥500 亿（超级中军）：大幅抬高量比/换手/集中度过热阈值。
    """
    pk = str(pool_key or "").lower()
    p = float(_safe_float(pct, 0.0))
    vr = float(_safe_float(vol_ratio, 1.0))
    trf = float(_safe_float(turnover_f, 0.0))
    us = float(_safe_float(upper_shadow_pct, 0.0))
    circ_yi = float(_safe_float(circ_mv_yi, 0.0))
    megacap_relief = circ_yi >= 500.0

    if pk in ("p2", "p3"):
        vr_hot, trf_hot, pct_hot = 2.8, 18.0, 6.0
    elif pk == "p4":
        vr_hot, trf_hot, pct_hot = 2.4, 14.0, 5.0
    else:
        vr_hot, trf_hot, pct_hot = 2.2, 12.0, 4.5

    if megacap_relief:
        vr_hot += 2.65
        trf_hot += 26.0
        pct_hot += 3.0

    score = 0.0
    if vr > vr_hot:
        score += min(35.0, (vr - vr_hot) * 14.0)
    if trf > trf_hot:
        score += min(30.0, (trf - trf_hot) * 2.8)
    if p > pct_hot:
        score += min(25.0, (p - pct_hot) * 4.5)
    if us > 2.2:
        score += min(20.0, (us - 2.2) * 6.0)
    score = float(max(0.0, min(100.0, score)))

    if pk in ("p2", "p3"):
        if score >= 80.0:
            mult, label = 0.90, "重拥挤"
        elif score >= 65.0:
            mult, label = 0.95, "中拥挤"
        elif score >= 50.0:
            mult, label = 0.98, "轻拥挤"
        else:
            mult, label = 1.0, "正常"
    elif pk == "p4":
        if score >= 80.0:
            mult, label = 0.86, "重拥挤"
        elif score >= 65.0:
            mult, label = 0.92, "中拥挤"
        elif score >= 50.0:
            mult, label = 0.97, "轻拥挤"
        else:
            mult, label = 1.0, "正常"
    else:
        if score >= 80.0:
            mult, label = 0.82, "重拥挤"
        elif score >= 65.0:
            mult, label = 0.90, "中拥挤"
        elif score >= 50.0:
            mult, label = 0.96, "轻拥挤"
        else:
            mult, label = 1.0, "正常"

    if megacap_relief and mult < 1.0:
        mult = 1.0
        label = f"{label}豁免"
    return score, mult, label


def _p3_right_side_guard(
    df_target,
    rt,
    *,
    hit_res=None,
    market_contraction_score: float = 0.0,
    crowd_score: float = 0.0,
) -> tuple:
    """
    P3 防追高判定：硬过滤 + 降权。
    返回 (hard_filter, multiplier, label, tags)。
    """
    tags = []
    hard = False
    mult = 1.0
    label = "正常"
    try:
        if df_target is None or len(df_target) < 6:
            return False, 1.0, "数据不足", ["数据不足"]
        last = df_target.iloc[-1]
        price = _safe_float(rt.get("price", last.get("close", 0.0)), 0.0)
        pre_close = _safe_float(rt.get("pre_close", last.get("pre_close", 0.0)), 0.0)
        open_px = _safe_float(rt.get("open", last.get("open", price)), 0.0)
        high_px = _safe_float(rt.get("high", last.get("high", price)), 0.0)
        ma5 = _safe_float(last.get("ma5", 0.0), 0.0)
        ma20 = _safe_float(last.get("ma20", 0.0), 0.0)
        if price <= 0 or pre_close <= 0:
            return False, 1.0, "价格不足", ["价格不足"]
        # 从 _detectors_pattern 拿 VWAP 估算（避免循环）
        try:
            from core.scan.detectors_pattern import _danger_buy_estimate_vwap_from_rt
            vwap = _danger_buy_estimate_vwap_from_rt(rt, price) if _danger_buy_estimate_vwap_from_rt else 0.0
        except Exception:
            vwap = 0.0
        vwap_dev_pct = (price - vwap) / vwap * 100.0 if vwap and vwap > 0 else 0.0
        pct = (price - pre_close) / pre_close * 100.0
        vol_ratio = _safe_float(rt.get("vol_ratio", 0.0), 0.0)
        turnover_f = _safe_float(rt.get("turnover_rate_f", 0.0), 0.0)
        upper_shadow_pct = 0.0
        if pre_close > 0:
            upper_shadow_pct = (max(high_px, price) - max(price, open_px)) / pre_close * 100.0
        cost_50th = _safe_float(last.get("cost_50th", price), 0.0)
        if cost_50th > 0:
            cost_dev_pct = (price - cost_50th) / cost_50th * 100.0
        else:
            cost_dev_pct = 0.0

        sector_beta = 1.0
        stock_memory_score = 0.0
        if isinstance(hit_res, dict):
            detail = hit_res.get("detail") or {}
            if isinstance(detail, dict):
                sector_beta = _safe_float(detail.get("sector_beta", 1.0), 1.0)
                stock_memory_score = _safe_float(detail.get("stock_memory_score", 0.0), 0.0)

        if vwap_dev_pct > 4.0:
            hard = True
            tags.append("VWAP严重偏离(急拉过热)")
        if pct >= 6.0 and vwap_dev_pct > 2.0:
            mult *= 0.82
            tags.append("盘中涨幅偏热(脉冲风险)")
        if ma5 > 0 and price > ma5 * 1.08:
            mult *= 0.85
            tags.append("5日线乖离过热")
        if ma20 > 0 and price > ma20 * 1.15:
            mult *= 0.90
            tags.append("20日线偏离偏高")
        if vol_ratio >= 6.0:
            mult *= 0.82
            tags.append("量比尖刺(诱多风险)")
        # P3 引擎已经对高换手、上影线和 VWAP 下方运行计入结构风险
        if turnover_f >= 15.0 and price < vwap:
            hard = True
            tags.append("均价线下高换手(承接差)")
        if sector_beta <= 0.95:
            mult *= 0.92
            tags.append("板块跟随不足(孤强)")
        if sector_beta >= 1.15:
            mult *= 1.03
            tags.append("板块共振(主线同步)")
        if stock_memory_score >= 60.0:
            mult *= 1.03
            tags.append("筹码锁定稳")
        if cost_dev_pct >= 8.0:
            mult *= 0.88
            tags.append("筹码乖离偏高")
        if market_contraction_score >= 0.7:
            mult *= 1.03
            tags.append("收缩环境(容错更高)")
        if vwap_dev_pct <= 1.5 and 1.5 <= vol_ratio <= 3.0 and 5.0 <= turnover_f <= 12.0:
            mult *= 1.06
            tags.append("贴线温和放量(稳健)")
        if price >= vwap and price >= ma5 and sector_beta >= 1.0 and stock_memory_score >= 45.0:
            mult *= 1.05
            tags.append("右侧确认(真核)")
        if hard:
            label = "硬过滤"
        elif mult >= 1.08:
            label = "强确认"
        elif mult >= 1.02:
            label = "适度加分"
        elif mult < 1.0:
            label = "降权"
        else:
            label = "正常"
        mult = float(max(0.55, min(1.12, mult)))
        if hard:
            return True, mult, label, tags
        return False, mult, label, tags
    except Exception as e:
        logging.debug("_p3_right_side_guard 异常: %s", e)
        return False, 1.0, "异常回退", ["异常回退"]


def _execution_tier_for_pool(pool_key, final_score, crowding_score, relaxed_gate_applied=False):
    """P4/P5 执行分层（A/B/C）。"""
    pk = str(pool_key or "").lower()
    fs = float(_safe_float(final_score, 0.0))
    cs = float(_safe_float(crowding_score, 0.0))

    if pk == "p4":
        if fs >= 90.0 and cs <= 55.0:
            tier = "A"
        elif fs >= 80.0 and cs <= 72.0:
            tier = "B"
        else:
            tier = "C"
    elif pk == "p5":
        if fs >= 88.0 and cs <= 60.0:
            tier = "A"
        elif fs >= 78.0 and cs <= 75.0:
            tier = "B"
        else:
            tier = "C"
    else:
        return "--", ""

    if relaxed_gate_applied and tier in ("A", "B"):
        tier = "B" if tier == "A" else "C"

    if tier == "A":
        return tier, "主仓候选（按纪律分批）"
    if tier == "B":
        return tier, "跟踪试错（轻仓）"
    return tier, "观察优先（谨慎）"


def _infer_golden_gate_reason(pool_key, df, rt):
    """门禁未通过时给出可读原因（用于漏斗诊断，不参与策略计算）。"""
    try:
        curr = df.iloc[-1] if isinstance(df, pd.DataFrame) and not df.empty else {}
        price = _safe_float(rt.get('price', curr.get('close', 0.0)))
        pre_close = _safe_float(rt.get('pre_close', curr.get('pre_close', 0.0)))
        open_price = _safe_float(rt.get('open', curr.get('open', 0.0)))
        high_price = _safe_float(rt.get('high', curr.get('high', 0.0)))
        vr = _safe_float(rt.get('vol_ratio', 0.0))
        if pre_close <= 0 or price <= 0 or vr <= 0:
            return "基础行情字段缺失"

        pct = (price - pre_close) / pre_close * 100.0
        pk = str(pool_key or '').lower()
        if pk == 'p2':
            open_pct = (open_price - pre_close) / pre_close * 100.0 if pre_close > 0 else 0.0
            winner = _safe_float(rt.get('winner_rate', curr.get('winner_rate', 0.0)))
            cost_50th = _safe_float(rt.get('cost_50th', curr.get('cost_50th', 0.0)))
            if open_pct <= 1.0:
                return "竞价涨幅不足(open_pct<=1)"
            if vr < 1.2:
                return "量比不足(vr<1.2)"
            if winner > 0 and cost_50th > 0 and not (winner > 85.0 and price > cost_50th):
                return "胜率/成本峰不达标"
            return "P2门禁其他条件未满足"

        if pk == 'p3':
            if pct <= 2.0:
                return "盘中涨幅不足(pct<=2)"
            if vr < 1.5:
                return "量比不足(vr<1.5)"
            macd_bar = _safe_float(curr.get('macd_bar', curr.get('macd_hist', 0.0)))
            amount = _safe_float(rt.get('amount', 0.0))
            volume = _safe_float(rt.get('volume', 0.0))
            if amount <= 0 or volume <= 0:
                return "量额缺失且MACD未转正" if macd_bar <= 0 else "量额缺失"
            _vol_d = max(volume, 1e-9)
            tentative = amount / _vol_d
            vwap = amount / max(volume * 100.0, 1e-9) if tentative > price * 20 else tentative
            if vwap <= 0:
                return "VWAP不可用"
            if price <= vwap:
                return "未站上VWAP"
            if macd_bar <= 0:
                return "MACD未转正"
            return "P3门禁其他条件未满足"

        if pk == "p4":
            tail_vol_ratio = _safe_float(rt.get("tail_vol_ratio", 0.0))
            net_main_gate = _safe_float(rt.get("net_main_amount", curr.get("net_main_amount", 0.0)))
            if not (2.0 < pct < 8.0):
                return "涨幅不在P4尾盘窗口(2~8%)"
            if vr < 1.1:
                return "量比不足(vr<1.1)"
            tail_ok = (tail_vol_ratio > 1.5) if tail_vol_ratio > 0 else True
            if not tail_ok:
                return "尾盘增量不足(tail_vol_ratio<=1.5)"
            if net_main_gate <= 0:
                return "主力净流入<=0"
            hi_shadow = _safe_float(curr.get("high", 0.0))
            if hi_shadow <= 0:
                hi_shadow = _safe_float(rt.get("high", 0.0))
            if hi_shadow <= 0:
                hi_shadow = max(price, open_price)
            upper_shadow_pct = (hi_shadow - max(price, open_price)) / pre_close * 100.0 if pre_close > 0 else 0.0
            if upper_shadow_pct >= 2.8:
                return "上影过长(>=2.8%)"
            return "P4门禁其他条件未满足"

        if pk == "p5":
            try:
                from core.config_manager import get_golden_config
                g = get_golden_config()
                p5_vr = float(g.get("p5_golden_vr_min", 1.2))
                p5_lo = float(g.get("p5_golden_pct_low", 2.0))
                p5_hi = float(g.get("p5_golden_pct_high", 7.0))
            except Exception:
                p5_vr, p5_lo, p5_hi = 1.2, 2.0, 7.0
            if vr < p5_vr:
                return f"量比不足(P5全日<{p5_vr})"
            if not (p5_lo < pct < p5_hi):
                return f"涨幅不在P5全日窗口({p5_lo}~{p5_hi})"
            net_main = _safe_float(rt.get("net_main_amount", curr.get("net_main_amount", 0.0)))
            if net_main <= 0:
                return "主力净流入非正(P5)"
            ma5_line = _safe_float(curr.get("ma5", 0.0))
            ma20_line = _safe_float(curr.get("ma20", 0.0))
            if ma20_line <= 0 or not (price > ma20_line and price > ma5_line):
                return "未同时站稳ma5与ma20(P5)"
            return "P5门禁其他条件未满足"

        return "门禁未通过(未分类)"
    except Exception:
        return "门禁未通过(诊断异常)"


def _build_priority_tags(p1_gene, burst_score, surge_bonus, regime_mult, decay_factor, is_stop_loss, penalty, is_core_sector, pool_key, safety_tags, personality_mult):
    tags = safety_tags.copy()
    if is_core_sector: tags.append("👑[主线保送]")
    if is_stop_loss: tags.append("🚨[危-破位]")
    elif decay_factor <= 0.85: tags.append("⚠️[衰减严重]")
    if penalty > 0 and not is_core_sector: tags.append(f"🩹[瑕疵-{penalty:.0f}]")

    if personality_mult == 0.5: tags.append("🩸[熔断拦截]")
    elif personality_mult >= 1.10: tags.append("🌟[异动觉醒]")

    if p1_gene >= 90.0: tags.append("🛡️[底仓王]")
    if surge_bonus >= 2.0: tags.append("🚀[黄金十绝]")
    if burst_score >= 80.0: tags.append("⚡[强爆发]")
    if regime_mult > 1.0: tags.append("🌊[顺风增压]")
    return " ".join(tags[:5])


# ==================== 实时板块排名 ====================
def get_realtime_sector_ranking():
    ind_map = get_all_basic_industry()
    if not ind_map:
        return get_latest_sector_ranking()

    now_time = datetime.now(BJ_TZ)
    curr_min = now_time.hour * 60 + now_time.minute
    # 非交易日或开盘前：固定回退到最新落库板块排名
    if now_time.weekday() >= 5 or curr_min < 565:
        return get_latest_sector_ranking()

    rt_map = fetch_realtime_batch(list(ind_map.keys()))
    if not rt_map:
        return get_latest_sector_ranking()
    sector_data = {}
    for ts_code, ind in ind_map.items():
        s_code = ts_code.split('.')[0][:6]
        if s_code in rt_map:
            rt = rt_map[s_code]
            if not isinstance(rt, dict):
                continue
            now_p = float(rt.get('price', 0))
            pre_p = float(rt.get('pre_close', 0.0))
            if pre_p > 0 and now_p > 0:
                if ind not in sector_data:
                    sector_data[ind] = []
                sector_data[ind].append((now_p - pre_p) / pre_p * 100.0)
    ranking = {ind: sum(pcts)/len(pcts) for ind, pcts in sector_data.items() if len(pcts) >= 5}
    return dict(sorted(ranking.items(), key=lambda item: item[1], reverse=True))