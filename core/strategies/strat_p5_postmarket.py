# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 - P5 盘后真龙池引擎（防镰刀增强版 + 2026慢牛优化）
【说明】
1. 与 P4 完全解耦：硬逻辑在 p5_postmarket_screener.py 中向量化实现；本类负责单股窗口拼接与打分输出。
2. run_all(df, rt) 契约与 scan_engine 一致，供盘后/收盘扫描调用。
3. 批量跑批请直接使用 p5_postmarket_screener.screen_p5_panel_for_date（15:30 定时任务）。
4. 十三策略含「均线粘合发散」等全日确认项；与 P4 尾盘十条互补。

【V26.88 防镰刀优化】
① 尾盘无量拉升检测：收盘涨但全天量能不足
② 昨日涨停T+1风险：涨停次日冲高收割风险
③ 全天回落尾盘拉升检测：典型收割模式
④ 跟风/板块同步检测强化：个股涨幅与板块不同步惩罚
⑤ 尾盘尾盘异动检测：尾盘脉冲后次日兑现风险
⑥ 全天弱于VWAP惩罚：诱多嫌疑
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.strategies.risk_control_engine import (
    DEFAULT_RISK_CONFIG,
    RiskControlConfig,
    evaluate_3layer_risk,
    evaluate_layer2_right_side_only,
    hits_indicate_right_side_attack,
    merge_risk_into_engine_result,
)
from core.config_manager import get_p5_postmarket_config, get_risk_control_config
from core.strategies.p5_postmarket_screener import (
    P5PostmarketConfig,
    _derive_p5_action_summary,
    build_single_stock_window_from_rt,
    evaluate_p5_single_prepared_row,
    evaluate_vwap_penalty_p5,
    p5_hits_are_actionable,
    p5_limit_burst_veto,
    prepare_p5_lags,
)

# 【V26.88+9-23 新增】复用 P3/P4 的财务注入器（fact_financial_reports → rt）
try:
    from core.strategies.strat_p3_intraday import _inject_financial_quality_into_rt
except ImportError:
    _inject_financial_quality_into_rt = None  # 退化时不影响主流程

logger = logging.getLogger(__name__)


def _p5_safe_float(val: Any, default: float = 0.0) -> float:
    """标量转 float，非有限数回退 default。"""
    try:
        x = float(pd.to_numeric(val, errors="coerce"))
        if not np.isfinite(x):
            return float(default)
        return float(x)
    except Exception:
        return float(default)


def _p5_rt_get(rt: Any, key: str, default: Any = None) -> Any:
    if isinstance(rt, dict):
        return rt.get(key, default)
    return default


def _p5_row_get(row: Any, key: str, default: Any = None) -> Any:
    try:
        return row.get(key, default)
    except Exception:
        return default


def _p5_trade_date(rt: Any) -> Any:
    return _p5_rt_get(rt, "trade_date", None)


def _bias_ma5_ma20_pct(ma5: float, ma20: float) -> float:
    """
    均线间乖离（%）：(ma5 - ma20) / ma20 * 100。
    仅在 ma20>0 且语义有效时由调用方使用；否则返回 nan。
    """
    if ma20 <= 0 or not np.isfinite(ma20):
        return float("nan")
    return (ma5 - ma20) / ma20 * 100.0


def _ma_overheat_penalty_mult(
    circ_mv_wan: float,
    bias_ma5_ma20: float,
    ma5: float,
    ma20: float,
    cfg: P5PostmarketConfig,
) -> float:
    """
    虚火分级降权（软约束，不否决）：
    - 须多头排列 ma5 > ma20，且乖离 > 阈值（默认 18%）才触发；
    - circ_mv >= 300 亿（万）：乘子 ma_overheat_mult_large（默认 0.85）；
    - P1 流通下限（默认 100 亿）<= circ_mv < 300 亿：乘子 ma_overheat_mult_mid（默认 0.95）；
    - circ_mv < P1 下限：不罚（主升浪极端乖离在小票尚可容忍）；
    - [3%,12%] 健康带内不罚（实际上乖离 <=18% 时已不触发本分支）。
    """
    if ma5 <= ma20 or ma20 <= 0:
        return 1.0
    if not np.isfinite(bias_ma5_ma20) or bias_ma5_ma20 <= float(cfg.ma_bias_overheat_pct):
        return 1.0
    if circ_mv_wan < float(cfg.circ_mv_wan_mid_min):
        return 1.0
    if not np.isfinite(circ_mv_wan):
        return 1.0
    large = float(cfg.circ_mv_wan_large_min)
    mid = float(cfg.circ_mv_wan_mid_min)
    if circ_mv_wan + 1e-9 >= large:
        return float(cfg.ma_overheat_mult_large)
    if circ_mv_wan + 1e-9 >= mid:
        return float(cfg.ma_overheat_mult_mid)
    return 1.0


def _ma_slope_reward_mult(
    slope_today: float,
    slope_yesterday: float,
    ma5: float,
    ma20: float,
    cfg: P5PostmarketConfig,
) -> float:
    """
    MA20 五日斜率奖励（indicator_calc 与全库一致：ma20_slope_5 已为百分比变化）：
    - 须 ma5 > ma20；
    - 今日斜率 > 阈值（默认 0.8%）且 今日斜率 > 昨日斜率（加速向上）；
    - 在 [threshold, interp_high] 上线性映射到 [mult_min, mult_max]，超过 interp_high 封顶 mult_max。
    """
    if ma5 <= ma20 or ma20 <= 0:
        return 1.0
    th = float(cfg.ma_slope_reward_threshold_pct)
    if not (np.isfinite(slope_today) and slope_today > th):
        return 1.0
    if not (np.isfinite(slope_yesterday) and slope_today > slope_yesterday):
        return 1.0
    lo = th
    hi = float(cfg.ma_slope_reward_interp_high_pct)
    mmin = float(cfg.ma_slope_reward_mult_min)
    mmax = float(cfg.ma_slope_reward_mult_max)
    if hi <= lo:
        return mmax
    if slope_today >= hi:
        return mmax
    t = (slope_today - lo) / (hi - lo)
    return float(mmin + t * (mmax - mmin))


def _apply_p5_ma_kinetic_to_burst(
    burst_base: float,
    prep: pd.DataFrame,
    row: Any,
    cfg: P5PostmarketConfig,
) -> Tuple[float, Dict[str, Any]]:
    """
    基准爆发分 × 虚火惩罚 × 斜率奖励，再截断到 [55, 99]。
    返回 (burst_after, detail_patch)；关闭 enable_ma_compensation 时原样返回 burst_base 与空 dict。
    """
    if not bool(cfg.enable_ma_compensation):
        return float(burst_base), {}

    ma5 = _p5_safe_float(row.get("ma5"), 0.0)
    ma20 = _p5_safe_float(row.get("ma20"), 0.0)
    circ_mv_wan = _p5_safe_float(row.get("circ_mv"), 0.0)
    bias_b = _bias_ma5_ma20_pct(ma5, ma20)

    slope_today = _p5_safe_float(row.get("ma20_slope_5"), 0.0)
    slope_yesterday = 0.0
    if prep is not None and isinstance(prep, pd.DataFrame) and len(prep) >= 2:
        slope_yesterday = _p5_safe_float(prep.iloc[-2].get("ma20_slope_5"), 0.0)

    m_pen = _ma_overheat_penalty_mult(circ_mv_wan, bias_b, ma5, ma20, cfg)
    m_rw = _ma_slope_reward_mult(slope_today, slope_yesterday, ma5, ma20, cfg)
    out = float(burst_base) * m_pen * m_rw
    # 【V34 P0】地板/封顶改为可配（原硬编码 55 ⇒ 任何进池票 burst 至少 55，跨池排序天然占优）
    _lo = _p5_safe_float(getattr(cfg, "p5_burst_floor", 0.0), 0.0)
    _hi = _p5_safe_float(getattr(cfg, "p5_burst_ceil", 100.0), 100.0)
    if _hi <= _lo:
        _hi = _lo + 1.0
    out = float(np.clip(out, _lo, _hi))

    patch: Dict[str, Any] = {
        "MA动能_bias_ma5_ma20_pct": round(bias_b, 4) if np.isfinite(bias_b) else None,
        "MA动能_虚火惩罚乘子": m_pen,
        "MA动能_斜率奖励乘子": m_rw,
        "MA动能_ma20_slope5_今日": round(slope_today, 4),
        "MA动能_ma20_slope5_昨日": round(slope_yesterday, 4),
        "MA动能_circ_mv_万元": round(circ_mv_wan, 2),
    }
    return out, patch


class P5Postmarket:
    def __init__(self, cfg: P5PostmarketConfig = None, risk_cfg: Optional[RiskControlConfig] = None):
        self.name = "P5盘后真龙引擎"
        self.version = "V26.88_P5_PostmarketAntiScam_2026Bull"
        self._cfg_lock_external = cfg is not None
        self._cfg = cfg if cfg is not None else get_p5_postmarket_config()
        self._risk_cfg_lock_external = risk_cfg is not None
        if risk_cfg is not None:
            self._risk_cfg = risk_cfg
        else:
            try:
                self._risk_cfg = get_risk_control_config()
            except Exception as ex:
                logger.debug("风控配置从 config.yaml 加载失败，使用 DEFAULT_RISK_CONFIG: %s", ex)
                self._risk_cfg = DEFAULT_RISK_CONFIG

    def _map_vr(self, vr: float, winner_rate: float, bias20: float) -> float:
        """筹码-量比位置化映射：低位放量 / 中位温和放量 / 高位爆量派发 / 高位缩量锁仓。"""
        vr = float(vr) if np.isfinite(vr) else 0.0
        wr = float(winner_rate) if np.isfinite(winner_rate) else 0.0
        b20 = float(bias20) if np.isfinite(bias20) else 0.0
        if wr < 60.0 or abs(b20) < 3.0:
            xp = [0.0, 0.8, 1.2, 2.0, 3.0, 4.5]
            yp = [0.0, 20.0, 45.0, 80.0, 95.0, 100.0]
        elif wr <= 85.0:
            xp = [0.0, 0.8, 1.0, 1.5, 2.5, 3.5, 4.5]
            yp = [0.0, 18.0, 35.0, 70.0, 88.0, 76.0, 60.0]
        else:
            if vr < 1.0:
                xp = [0.0, 0.6, 0.8, 1.0, 1.5, 2.5, 4.0]
                yp = [0.0, 25.0, 55.0, 92.0, 100.0, 96.0, 84.0]
            else:
                xp = [0.0, 0.8, 1.2, 2.0, 2.5, 3.5, 4.5]
                yp = [0.0, 20.0, 50.0, 78.0, 72.0, 48.0, 28.0]
        return float(np.interp(vr, xp, yp))

    def _burst_bounds(self) -> Tuple[float, float]:
        """【V34 P0】burst 上下界（可配）。

        原实现 8 处硬编码 `np.clip(..., 55, 100)`：任何进池票 burst 至少 55，
        跨池排序天然占优（P3 无地板、P4 是 0–100），且 template_label 的 `<40 → 不追高` 分支永不触发。
        默认 p5_burst_floor=0 回到诚实刻度；设回 55 即恢复旧行为。
        """
        lo = _p5_safe_float(getattr(self._cfg, "p5_burst_floor", 0.0), 0.0)
        hi = _p5_safe_float(getattr(self._cfg, "p5_burst_ceil", 100.0), 100.0)
        if hi <= lo:
            hi = lo + 1.0
        return lo, hi

    def _p5_position_ladder(
        self, *, score: float, penalty: float, dim_count: int, cross_bonus: float,
        action: str, trade_grade: str, layer_overheat: int, template_label: str,
        suggested_min: float,
    ) -> Tuple[float, Dict[str, Any]]:
        """【V34 P1】P5 仓位阶梯：此前 P5 完全没有 max_position_ratio，下游「最大仓位比例」恒空，
        仓位纪律只靠一句文字标签（📉建议单笔≤10%）。盘后池信息最全、可等次日确认，故上限略高于盘中池。

        刻度：burst 已是 0–100 诚实刻度（p5_burst_floor 默认 0），这里仍用 45–85 线性归一，
        避免 60/80 硬分档把「分 53」一刀切成「仓 0%」。
        """
        cfg = self._cfg
        base = _p5_safe_float(getattr(cfg, "p5_base_position_ratio", 0.10), 0.10)
        cap = _p5_safe_float(getattr(cfg, "p5_max_position_ratio", 0.20), 0.20)
        low = _p5_safe_float(getattr(cfg, "p5_min_position_ratio", 0.04), 0.04)

        ladder: Dict[str, Any] = {
            "base": round(base, 3), "score_mult": 1.0, "action_mult": 1.0,
            "penalty_mult": 1.0, "ev_mult": 1.0, "heat_mult": 1.0, "dim_mult": 1.0,
        }
        # 观察档/不追高：只出信息，不给仓位（与 p5_core_screener_pass 同口径）
        _act = str(action or "观察")
        if _act not in ("追涨", "低吸", "核心", "追涨/低吸", "辅助"):
            ladder.update({"ratio": 0.0, "reason": f"动作档={_act}，仅观察不建仓"})
            return 0.0, ladder
        if str(template_label or "") == "不追高":
            ladder.update({"ratio": 0.0, "reason": "模板标签=不追高，禁止建仓"})
            return 0.0, ladder

        s_floor = _p5_safe_float(getattr(cfg, "p5_score_scale_floor", 45.0), 45.0)
        s_ceil = _p5_safe_float(getattr(cfg, "p5_score_scale_ceiling", 85.0), 85.0)
        if s_ceil <= s_floor:
            s_ceil = s_floor + 1.0
        _frac = (float(score) - s_floor) / (s_ceil - s_floor)
        ladder["score_mult"] = round(min(1.0, max(0.30, 0.30 + 0.70 * _frac)), 3)
        ladder["score_input"] = round(float(score), 2)

        ladder["action_mult"] = {
            "核心": 1.0, "追涨": 0.95, "低吸": 0.90, "追涨/低吸": 0.95, "辅助": 0.80,
        }.get(_act, 0.75)

        _dc = int(dim_count or 0)
        ladder["dim_mult"] = {3: 1.0, 2: 0.88, 1: 0.72}.get(_dc, 0.70)
        if float(cross_bonus or 0.0) >= 12.0:
            ladder["dim_mult"] = min(1.0, float(ladder["dim_mult"]) + 0.08)
        ladder["dim_count"] = _dc

        p = _p5_safe_float(penalty, 0.0)
        if p <= 10.0:
            ladder["penalty_mult"] = 1.0
        elif p <= 30.0:
            ladder["penalty_mult"] = 0.85
        elif p <= 50.0:
            ladder["penalty_mult"] = 0.60
        else:
            ladder["penalty_mult"] = 0.30

        ladder["ev_mult"] = {"A": 1.0, "B": 0.90, "B+": 0.95, "C": 0.75, "D": 0.45}.get(
            str(trade_grade or "").strip().upper(), 0.85
        )

        oh = int(layer_overheat or 0)
        if oh <= -3:
            ladder["heat_mult"] = 0.70
        elif oh <= -2:
            ladder["heat_mult"] = 0.85

        _smin = _p5_safe_float(suggested_min, 0.0)
        if _smin > 0 and float(score) < _smin:
            ladder.update({"ratio": 0.0, "reason": f"综合分{float(score):.0f}<建议最低{_smin:.0f}"})
            return 0.0, ladder

        ratio = base
        for k in ("score_mult", "action_mult", "dim_mult", "penalty_mult", "ev_mult", "heat_mult"):
            ratio *= float(ladder[k])
        if ratio <= 0:
            ladder["ratio"] = 0.0
            return 0.0, ladder
        ratio = min(cap, max(low, ratio))
        ladder["ratio"] = round(ratio, 3)
        return float(ratio), ladder

    def _p5_wechat_hint(self, rt: Dict[str, Any], res: Dict[str, Any]) -> str:
        """【V34 P1】企微推送要短：长文案留在 buy_hint，这里给「池|名称|分|仓|动作|止损」短码（与 P3/P4 同口径）。"""
        name = str((rt or {}).get("name", "") or "").strip() or "--"
        score = float(res.get("burst_score", 0.0) or 0.0)
        pos = float(res.get("max_position_ratio", 0.0) or 0.0)
        action = str(res.get("primary_action", "") or "观察")
        stop = ""
        _sl = res.get("auto_stop_loss")
        try:
            if isinstance(_sl, dict):
                _px = float(_sl.get("stop_loss_price", 0.0) or 0.0)
                stop = f"{_px:.2f}" if _px > 0 else ""
            elif _sl:
                stop = str(_sl)
        except Exception:
            stop = ""
        parts = [f"P5盘后|{name}", f"分{score:.0f}", f"仓{pos * 100:.0f}%", action]
        if stop:
            parts.append(f"止损{stop}")
        return "|".join(parts)

    def run_all(self, df, rt):
        """
        单股入口：df 为历史日线窗口；rt 为当日收盘/盘后快照（可与最后一根同日对齐或带 trade_date 追加）。

        返回 dict：burst_score、strategies、penalty、detail、p5_core_screener_pass、p5_veto_reason
        """
        if not self._cfg_lock_external:
            self._cfg = get_p5_postmarket_config()
        if not self._risk_cfg_lock_external:
            try:
                self._risk_cfg = get_risk_control_config()
            except Exception:
                pass
        res = {
            "burst_score": 0.0,
            "surge_bonus": 0.0,
            "penalty": 0.0,
            "detail": {},
            "strategies": [],
            "primary_action": "",
            "secondary_actions": [],
            "market_status": "",
            "risk_level": "",
            "p5_core_screener_pass": False,
            "p5_veto_reason": "",
            "risk_tags": [],
            "suggested_min_entry_score": 0.0,
            "risk_control": {},
            "buy_hint": "",
            "wechat_hint": "",
        }
        if df is None or df.empty or len(df) < 3:
            return res

        try:
            code = str(_p5_rt_get(rt, "ts_code", "") or "")
            if not code and "ts_code" in df.columns:
                code = str(df["ts_code"].iloc[-1])
            if not code:
                code = "_P5_SINGLE_"

            work = build_single_stock_window_from_rt(df, rt, code)
            if work is None or len(work) < 3:
                return res

            prep = prepare_p5_lags(work)
            if prep is None or prep.empty:
                return res
            row = prep.iloc[-1]

            rt_risk = dict(rt)
            rt_risk["_pool_key"] = "p5"
            risk_pre = evaluate_3layer_risk(work, rt_risk, self._risk_cfg, is_right_side_strategy=False, pool_key="p5")
            if not risk_pre.get("pass_layer1", False):
                res["p5_veto_reason"] = str(risk_pre.get("veto_reason", "") or "")
                res["risk_control"] = {
                    "pass_layer1": False,
                    "pass_layer2": True,
                    "veto_reason": res["p5_veto_reason"],
                    "penalty": float(risk_pre.get("penalty", 0.0) or 0.0),
                    "risk_tags": list(risk_pre.get("risk_tags", []) or []),
                    "suggested_min_entry_score": float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0),
                    "ui_warnings": list(risk_pre.get("ui_warnings", []) or []),
                    "debug": {
                        "stage": "layer1_veto",
                        "ts_code": code,
                        "trade_date": _p5_trade_date(rt),
                    },
                }
                res["risk_tags"] = list(risk_pre.get("risk_tags", []) or [])
                res["suggested_min_entry_score"] = float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0)
                return res

            try:
                mcs = float(_p5_rt_get(rt, "_market_contraction_score", 0.0) or 0.0)
            except (TypeError, ValueError):
                mcs = 0.0

            # 【V26.88+9-24 P5 修复 + 重构】盈利质量注入（修复 ts_code→code，挪到风险否决前）
            # 这样即使被 3 层风控否决，detail 里也能记录财务质量分数
            try:
                if _inject_financial_quality_into_rt is not None and code:
                    _inject_financial_quality_into_rt(rt, str(code))
            except Exception as _e_fin:
                logger.debug("P5 财务注入失败（不影响主流程）: %s", _e_fin)

            # 【V26.88+9-24 P5 新增】大盘分层温度注入（按流通市值对应 large/mid/growth 层）
            try:
                from core.p1_precompute import compute_market_overheat_score
                _oh = compute_market_overheat_score()
                rt["_pre_market_overheat"] = _oh
                _circ_mv_p5 = _p5_safe_float(rt.get("circ_mv"), _p5_safe_float(row.get("circ_mv"), 0.0))
                _cm_yi_p5 = _circ_mv_p5 / 10000.0
                if _cm_yi_p5 >= 500.0:
                    _layer_p5 = "large"
                elif _cm_yi_p5 >= 100.0:
                    _layer_p5 = "mid"
                else:
                    _layer_p5 = "growth"
                rt["_market_layer"] = _layer_p5
                rt["_layer_overheat_score"] = int(
                    (_oh.get("layer_scores") or {}).get(_layer_p5, _oh.get("market_overheat_score", 0))
                )
                rt["_market_overheat_score"] = int(_oh.get("market_overheat_score", 0))
                rt["_market_regime"] = str(_oh.get("regime", "neutral"))
            except Exception as _e_oh_p5:
                logger.debug("P5 大盘温度注入失败: %s", _e_oh_p5)

            # 【V26.88+9-24 P5 新增】HiThink 炸板股拦截（盘后买入炸板股 = 隔日大概率低开）
            try:
                # 【V34 P2-3】炸板否决走唯一实现（带交易日作用域），不再各处各判一遍
                _veto_brk, _brk_open, _brk_turnover = p5_limit_burst_veto(rt, _p5_trade_date(rt))
                if _veto_brk:
                    res["p5_veto_reason"] = (
                        f"盘后拦截：HiThink炸板池(回开{_brk_open}次 换手{_brk_turnover:.1f}%)"
                    )
                    res["risk_control"] = {
                        "pass_layer1": False,
                        "pass_layer2": True,
                        "veto_reason": res["p5_veto_reason"],
                        "penalty": 100.0,
                        "risk_tags": [f"🚨[盘后炸板]HiThink炸板池(回开{_brk_open}次 换手{_brk_turnover:.1f}%)"],
                        "ui_warnings": ["盘后买入炸板股次日大概率低开"],
                    }
                    res["risk_tags"].append(f"🚨[盘后炸板]HiThink炸板池(回开{_brk_open}次)")
                    res["detail"]["p5_break_pool_reject"] = True
                    return res
            except Exception as _e_p5_brk:
                logger.debug("P5 炸板拦截异常: %s", _e_p5_brk)

            # 【V26.88+9-24 P5 新增】财务质量打分（在 evaluate 之前完成，便于在 burst_score 合成时使用）
            try:
                from core.strategies.p4_tail_screener import _fin_quality_score_p4
                _fin_p5_score, _fin_p5_reason = _fin_quality_score_p4(rt, self._cfg)
                res["detail"]["fin_quality_score"] = round(float(_fin_p5_score), 2)
                res["detail"]["fin_quality_reason"] = str(_fin_p5_reason)
            except Exception as _e_fin_p5:
                logger.debug("P5 财务打分失败: %s", _e_fin_p5)

            # 【缩量分歧低吸】传入全日 prep + 收缩度，由 screener 单票函数追加 mask（仅 mcs≥0.7 生效）
            gp_ok, hits, veto, extra_detail = evaluate_p5_single_prepared_row(
                row,
                self._cfg,
                stock_prep_df=prep,
                rt=rt,
                market_contraction_score=mcs,
            )
            hits = list(hits or [])
            res["p5_veto_reason"] = veto or ""

            if not gp_ok:
                return res
            if not hits:
                return res

            hits = list(hits)
            if hits_indicate_right_side_attack(hits):
                l2 = evaluate_layer2_right_side_only(work, rt_risk, self._risk_cfg)
                for uw in l2.get("ui_warnings") or []:
                    res["risk_tags"].append("⚠️[二层预警]" + str(uw))
                if not l2.get("pass_layer2", True):
                    res["p5_veto_reason"] = str(l2.get("veto_reason", "") or "")
                    res["risk_control"] = {
                        "pass_layer1": True,
                        "pass_layer2": False,
                        "veto_reason": res["p5_veto_reason"],
                        "penalty": float(risk_pre.get("penalty", 0.0) or 0.0),
                        "risk_tags": list(risk_pre.get("risk_tags", []) or []),
                        "suggested_min_entry_score": float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0),
                        "ui_warnings": list(risk_pre.get("ui_warnings", []) or []),
                        "debug": {
                            "stage": "layer2_veto",
                            "ts_code": code,
                            "trade_date": _p5_trade_date(rt),
                        },
                    }
                    res["risk_tags"] = list(risk_pre.get("risk_tags", []) or [])
                    res["suggested_min_entry_score"] = float(risk_pre.get("suggested_min_entry_score", 0.0) or 0.0)
                    return res

            res["strategies"] = hits

            # 【V34 P2-2】动作分类不再内联复制 24 项清单——与 screener 的 _derive_p5_action_summary
            # 双份清单必然分叉（此前已出现 P5-03C/03A 与 P5-03 聚合名两套口径）。
            # 同时删掉 `any("VWAP" in risk_tags)`：此处 vwap_warn 尚未写入，恒判「正常/中」。
            primary_action, secondary_actions, market_status, risk_level = _derive_p5_action_summary(hits)
            res["primary_action"] = primary_action
            res["secondary_actions"] = secondary_actions
            res["market_status"] = market_status
            res["risk_level"] = risk_level

            wr = _p5_safe_float(_p5_row_get(row, "winner_rate"), 0.0)
            bias20 = _p5_safe_float(_p5_row_get(row, "bias_20"), 0.0)
            vr = _p5_safe_float(_p5_row_get(row, "vol_ratio"), 0.0)
            s_vol = self._map_vr(vr, wr, bias20)
            # 【V35 编号重整】三向共振维度清单同步为新编号（旧号→新号见 p5_postmarket_screener 模块头）：
            #   A 资金底座 = 资金方向/质量/确认（P5-04/05/06）+ 超级中军点火（P5-13；旧08，外资旧07 已删）
            #   B 结构买点 = 箱体/均线/缩量低吸/旗形/布林/新高（P5-15~P5-21、P5-23、P5-24）
            #   C 趋势量价 = 量价/趋势/四因子/单峰/双均线/MACD/欧奈尔（P5-01~P5-03、P5-07~P5-11、P5-14、P5-22、P5-25）
            dim_map = {
                "A资金底座": any(any(k in h for k in ["P5-04", "P5-05", "P5-06", "P5-13"]) for h in hits),
                "B结构买点": any(any(k in h for k in ["P5-15", "P5-16", "P5-17", "P5-18", "P5-19", "P5-20", "P5-21", "P5-23", "P5-24"]) for h in hits),
                "C趋势量价": any(any(k in h for k in ["P5-01", "P5-02", "P5-03", "P5-07", "P5-08", "P5-09", "P5-10", "P5-11", "P5-22", "P5-25", "P5-14"]) for h in hits),
            }
            sector_beta = 1.0
            if isinstance(rt.get("sector_beta"), (int, float)) and not pd.isna(rt.get("sector_beta")):
                sector_beta = float(rt.get("sector_beta", 1.0) or 1.0)
            elif isinstance(rt.get("industry_beta"), (int, float)) and not pd.isna(rt.get("industry_beta")):
                sector_beta = float(rt.get("industry_beta", 1.0) or 1.0)
            elif isinstance(rt.get("sector_mult"), (int, float)) and not pd.isna(rt.get("sector_mult")):
                sector_beta = float(rt.get("sector_mult", 1.0) or 1.0)
            sector_beta = max(0.7, min(1.5, sector_beta))
            board_bonus = 0.0
            if sector_beta >= 1.20:
                board_bonus = 6.0
                res["risk_tags"].append("🌋[热板块加权]")
            elif sector_beta >= 1.10:
                board_bonus = 3.0
                res["risk_tags"].append("🔥[偏热板块]")
            elif sector_beta < 0.9:
                board_bonus = -3.0
                res["risk_tags"].append("🧊[冷板块折价]")
            dim_score = 0.0
            dim_score += 10.0 if dim_map["A资金底座"] else 0.0
            dim_score += 10.0 if dim_map["B结构买点"] else 0.0
            dim_score += 10.0 if dim_map["C趋势量价"] else 0.0
            cross_bonus = 0.0
            if dim_map["A资金底座"] and dim_map["C趋势量价"]:
                cross_bonus += 8.0
            if dim_map["A资金底座"] and dim_map["B结构买点"]:
                cross_bonus += 6.0
            if dim_map["B结构买点"] and dim_map["C趋势量价"]:
                cross_bonus += 6.0
            if all(dim_map.values()):
                cross_bonus += 12.0
            raw = 48.0 + s_vol * 0.18 + dim_score + cross_bonus + board_bonus
            # 基准爆发分（改为异构共振 + 位置化量能，不再线性叠加 hit 数）
            burst_base = float(min(raw, 100.0))
            burst_adj, ma_detail = _apply_p5_ma_kinetic_to_burst(burst_base, prep, row, self._cfg)
            vwap_mult, vwap_detail, vwap_warn = evaluate_vwap_penalty_p5(prep, rt, self._cfg)
            burst_adj = float(np.clip(burst_adj * vwap_mult, *self._burst_bounds()))
            # 【V26.88 P5 优化⑤】战略赛道硬科技溢价加分
            is_strat = bool((extra_detail or {}).get("is_strategic_industry", False))
            if is_strat:
                burst_adj = float(np.clip(
                    burst_adj + float(self._cfg.strategic_burst_bonus), *self._burst_bounds()
                ))
                cross_bonus += float(self._cfg.strategic_cross_bonus)
                res["risk_tags"].append("🌟[战略赛道认定]")

            # 【V26.88+9-22 优化19】次日开盘溢价加分（涨停龙头 + 大盘实时锚）
            try:
                _ht_lu = rt.get("_hithink_limit_up") or {}
                _ht_strength = float(rt.get("_hithink_index_strength", 1.0) or 1.0)
                _next_day_bonus = 0.0
                _next_day_tags = []
                # 涨停龙头加分（最高 +12）
                if _ht_lu:
                    _ht_cnt = int(_p5_safe_float(_ht_lu.get("continue_day_cnt"), 1) or 1)
                    _ht_seal_yi = _p5_safe_float(_ht_lu.get("seal_money"), 0.0) / 1e8
                    if _ht_cnt >= 3 and _ht_seal_yi >= 1.0:
                        _next_day_bonus += 12.0
                        _next_day_tags.append(f"💎[次日龙头]{_ht_cnt}板封板{_ht_seal_yi:.1f}亿")
                    elif _ht_cnt >= 3:
                        _next_day_bonus += 8.0
                        _next_day_tags.append(f"💎[次日连板]{_ht_cnt}板")
                    elif _ht_cnt >= 2:
                        _next_day_bonus += 5.0
                        _next_day_tags.append("💎[次日2板]")
                    else:
                        _next_day_bonus += 2.0
                        _next_day_tags.append("💎[次日首板]")
                # 大盘极强加分（+6）
                if _ht_strength >= 1.5:
                    _next_day_bonus += 6.0
                    _next_day_tags.append(f"🌋[大盘极强]次日跟涨概率高")
                # 【V26.88+9-22 优化26】板块同步性识别（HiThink 涨停池总数代理「板块热度」）
                # HiThink 涨停池 ≥ 50 只 = 市场情绪高亢；10-50 = 中等；< 10 = 弱势
                # 实战：涨停池大时封板龙头「板块共振」溢价更确定
                try:
                    _total_lu = len((_ht_lu or {}).get("limit_up_map", {}))
                    if _total_lu >= 50:
                        _next_day_bonus += 10.0
                        _next_day_tags.append(f"[市场大热]涨停池{_total_lu}只")
                    elif _total_lu >= 25:
                        _next_day_bonus += 6.0
                        _next_day_tags.append(f"[市场偏热]涨停池{_total_lu}只")
                    elif _total_lu >= 10:
                        _next_day_bonus += 2.0
                        _next_day_tags.append(f"[市场中性]涨停池{_total_lu}只")
                    elif _total_lu < 10 and _ht_lu:
                        # 弱势涨停：孤狼涨停嫌疑，扣分
                        _next_day_bonus -= 5.0
                        _next_day_tags.append(f"[市场冷清]涨停池仅{_total_lu}只，冲高回落风险")
                except Exception as _e_pi:
                    logger.debug("[P5] 板块同步性识别跳过: %s", _e_pi)
                # 应用加分
                if _next_day_bonus > 0:
                    burst_adj = float(np.clip(burst_adj + _next_day_bonus, *self._burst_bounds()))
                    res["risk_tags"].extend(_next_day_tags)
                    res["detail"]["次日开盘溢价"] = round(_next_day_bonus, 2)
                    res["detail"]["次日开盘标签"] = "|".join(_next_day_tags)
            except Exception as _e_nd:
                logger.debug("[P5] 次日开盘溢价计算异常（已降级）: %s", _e_nd)
            # 【V26.88 P5 优化④】winner_rate 机构地板软扣
            wr = _p5_safe_float(_p5_row_get(row, "winner_rate"), 0.0)
            if wr > 0 and wr < float(self._cfg.winner_floor):
                burst_adj = float(np.clip(burst_adj - float(self._cfg.winner_floor_penalty), *self._burst_bounds()))
                res["risk_tags"].append(f"⚠️【winner地板】wr={wr:.1f}%<{self._cfg.winner_floor:.0f}%")

            # 【V26.88+9-24 P5 新增】合并财务质量打分（消费已注入的财务字段，最大幅度 ±2.0）
            _fin_p5_score = float(res.get("detail", {}).get("fin_quality_score", 0.0) or 0.0)
            if _fin_p5_score != 0:
                burst_adj = float(np.clip(burst_adj + _fin_p5_score, *self._burst_bounds()))
                _fin_label = res.get("detail", {}).get("fin_quality_reason", "")
                if _fin_p5_score > 0:
                    res["risk_tags"].append(f"💎[盈利质量]{_fin_label}")
                else:
                    res["risk_tags"].append(f"⚠️[盈利质量]{_fin_label}")

            # 【V26.88+9-24 P5 新增】大盘分层温度加权（盘后决策：影响更大 ±2.5）
            try:
                _layer_oh_p5 = int(rt.get("_layer_overheat_score", 0))
                if _layer_oh_p5 != 0:
                    _oh_delta_p5 = float(_layer_oh_p5) * 0.83  # 盘后：±2.5 分
                    burst_adj = float(np.clip(burst_adj + _oh_delta_p5, *self._burst_bounds()))
                    _layer_name_p5 = str(rt.get("_market_layer", "?"))
                    _oh_label_p5 = {-3: "过热", -2: "偏热", -1: "微热",
                                    1: "微冷", 2: "偏冷", 3: "超冷"}.get(_layer_oh_p5, "")
                    res["risk_tags"].append(
                        f"🌡️[大盘温度][{_layer_name_p5}层]{_oh_label_p5}({_layer_oh_p5:+d})  {_oh_delta_p5:+.1f}分"
                    )
                    res["detail"]["market_overheat_delta"] = round(_oh_delta_p5, 2)
            except Exception as _e_oh_p5b:
                logger.debug("P5 大盘温度加权异常: %s", _e_oh_p5b)

            # 【V26.80 统一 A 股特色评分】P5 盘后场景：涨停板状态 + 炸板修复 + 次新/ST
            try:
                from core.p2_p5_a_share_enhance import unified_a_share_score

                _a_share = unified_a_share_score(prep, rt)
                if _a_share.get("is_p2_p5_veto"):
                    res["p5_veto_reason"] = _a_share.get("veto_reason", "A 股特色高风险")
                    res["risk_tags"].append(f"🚨[统一风控]{_a_share.get('veto_reason')}")
                    res["burst_score"] = 0.0
                    return res

                _a_share_tags = _a_share.get("all_tags", [])[:5]
                for tag in _a_share_tags:
                    if tag and tag not in res["risk_tags"]:
                        res["risk_tags"].append(tag)

                # A 股特色加权（盘后权重 0.2，盘后信息最全）
                _a_boost = float(_a_share.get("total_boost", 0.0)) * 0.2
                burst_adj = float(np.clip(burst_adj + _a_boost, *self._burst_bounds()))
                res["a_share_p5_boost"] = round(_a_boost, 2)
                res["a_share_p5_tags"] = _a_share_tags
            except Exception as _e_a5:
                logging.debug("V26.80 P5 统一 A 股特色评分跳过: %s", _e_a5)

            res["burst_score"] = round(burst_adj, 2)
            res["surge_bonus"] = 0.0
            res["penalty"] = 0.0
            p5_why = []
            if dim_map["A资金底座"]:
                p5_why.append("资金")
            if dim_map["B结构买点"]:
                p5_why.append("结构")
            if dim_map["C趋势量价"]:
                p5_why.append("趋势")
            if cross_bonus >= 12.0:
                p5_why.append("共振")
            if any("高位缩量锁仓" in t for t in res["risk_tags"]):
                p5_why.append("锁仓")
            # [BugFix] 早期诊断字段（fin_quality_score / 次日开盘溢价 / market_overheat_delta 等）
            # 在此前分支已写入 res["detail"]，此处整体重写会静默清空。
            # 根因：res["detail"] 被直接赋值覆盖 -> 修复：快照后 update 合并。
            _early_detail_p5 = dict(res.get("detail") or {})
            _early_detail_p5.update({
                "风险等级": risk_level,
                "市场状态": market_status,
                "主动作": primary_action,
                "次级动作": "|".join(secondary_actions) if secondary_actions else "",
                "买入原因": "/".join(p5_why) if p5_why else "分数高",
                "VWAP惩罚乘子": round(vwap_mult, 4),
                "P5爆发基准分_均线前": round(burst_base, 2),
                "量比位置化分": round(s_vol, 2),
                "板块加成": round(board_bonus, 2),
                "sector_beta": round(sector_beta, 3),
                "命中维度数": int(sum(1 for v in dim_map.values() if v)),
                "ts_code": code,
            })
            res["detail"] = _early_detail_p5
            res["detail"]["维度A_资金底座"] = bool(dim_map["A资金底座"])
            res["detail"]["维度B_结构买点"] = bool(dim_map["B结构买点"])
            res["detail"]["维度C_趋势量价"] = bool(dim_map["C趋势量价"])
            res["detail"]["跨界共振加分"] = round(cross_bonus, 2)
            # 【V26.88 P5 优化】把 extra_detail（is_strategic_industry）写入主 detail
            if isinstance(extra_detail, dict) and extra_detail:
                for k, v in extra_detail.items():
                    res["detail"][k] = v
            if ma_detail:
                res["detail"].update(ma_detail)
            if vwap_detail:
                res["detail"].update(vwap_detail)
            for w in vwap_warn:
                res["risk_tags"].append(str(w))
            # 【V26.88 P5 防镰刀检测字段】（必须先取值再使用）
            pct_close = _p5_safe_float(_p5_row_get(row, "pct_chg"), 0.0)
            close_px = _p5_safe_float(_p5_row_get(row, "close"), 0.0)
            open_px = _p5_safe_float(_p5_row_get(row, "open"), close_px)
            prev_pct = 0.0
            if len(prep) >= 2:
                prev_pct = _p5_safe_float(prep.iloc[-2].get("pct_chg"), 0.0)
            day_direction = 0.0
            if open_px > 0 and close_px > 0:
                day_direction = (close_px - open_px) / open_px * 100.0
            res["detail"]["尾盘无量拉升"] = bool(vr < 1.2 and pct_close > 2.0 and close_px > open_px)
            res["detail"]["T1收割风险"] = bool(prev_pct >= 9.5 and pct_close > 2.5)
            res["detail"]["全天回落尾拉"] = bool(day_direction < -1.5 and pct_close > 1.0)
            res["detail"]["跟风拉升嫌疑"] = bool(sector_beta < 1.0 and pct_close > 3.0 and vr < 1.8)

            # 【V26.88 P5 防镰刀优化①】尾盘无量拉升检测
            # 收盘涨但全天量比不足，可能是做收盘价而非真实拉升
            if vr < 1.2 and pct_close > 2.0 and close_px > open_px:
                res["risk_tags"].append("⚠️【尾盘无量拉升】全天量比不足，收盘价可能虚高")
                res["penalty"] = float(res["penalty"] + 15.0)

            # 【V26.88 P5 防镰刀优化②】昨日涨停T+1风险检测
            # 昨日涨停今日冲高 → 可能是T+1收割
            if prev_pct >= 9.5 and pct_close > 2.5:
                res["risk_tags"].append(f"⚠️【T+1收割】昨涨停({prev_pct:.1f}%)今冲高({pct_close:.1f}%)，次日易低开")
                res["penalty"] = float(res["penalty"] + 20.0)

            # 【V26.88 P5 防镰刀优化③】全天回落尾盘拉升检测
            # 全天在跌，收盘却拉升 → 典型诱多模式
            if day_direction < -1.5 and pct_close > 1.0:
                res["risk_tags"].append("⚠️【全天回落尾盘拉升】典型诱多，次日低开概率大")
                res["penalty"] = float(res["penalty"] + 25.0)

            # 【V26.88 P5 防镰刀优化④】跟风拉升检测
            # 个股涨幅远超板块涨幅但量比不足 → 跟风嫌疑
            if sector_beta < 1.0 and pct_close > 3.0 and vr < 1.8:
                res["risk_tags"].append("⚠️【跟风拉升】板块偏弱但个股涨，次日易被砸")
                res["penalty"] = float(res["penalty"] + 15.0)

            # 【V26.88 P5 防镰刀优化⑤】尾盘脉冲后兑现风险
            # 尾盘VWAP偏离过大 + 收盘高于VWAP → 尾盘拉升兑现嫌疑
            vwap_dev = _p5_safe_float(_p5_row_get(row, "vwap"), 0.0)
            if vwap_dev > 0:
                vwap_dev_pct = (close_px - vwap_dev) / vwap_dev * 100.0 if vwap_dev > 0 else 0.0
                if vwap_dev_pct > 2.0 and pct_close > 3.0:
                    res["risk_tags"].append("⚠️【尾盘脉冲兑现】收盘远高于VWAP，次日兑现风险大")
                    res["penalty"] = float(res["penalty"] + 18.0)

            # 【V26.88 P5 防镰刀优化⑥】全天弱于VWAP惩罚
            # 收盘在VWAP下方运行 → 主力控盘但可能是诱多陷阱
            if vwap_dev > 0:
                vwap_dev_pct = (close_px - vwap_dev) / vwap_dev * 100.0 if vwap_dev > 0 else 0.0
                if vwap_dev_pct < -1.0 and pct_close > 1.5:
                    res["risk_tags"].append("⚠️【弱于VWAP】全天在均价下方，收盘拉升疑似诱多")
                    res["penalty"] = float(res["penalty"] + 15.0)
            if dim_map["C趋势量价"] and wr > 85.0 and abs(bias20) > 8.0 and vr > 2.5:
                res["risk_tags"].append("⚠️【高位爆量派发】筹码高位松动，需防次日兑现")
                res["penalty"] = float(res["penalty"] + 8.0)
            if dim_map["C趋势量价"] and wr > 85.0 and vr < 1.0 and pct_close > 0:
                res["risk_tags"].append("✅【高位缩量锁仓】高位创新高但量能收缩，疑似锁仓加速")
                res["burst_score"] = round(min(res["burst_score"] + 6.0, 100.0), 2)
                res["detail"]["高位缩量锁仓加分"] = 6.0
            # === V27 A4 炸板回封强度分级 ===
            try:
                from core.strategies.limit_break_quality import classify_limit_break_quality as _lbq
                _lb = _lbq(rt if isinstance(rt, dict) else {})
                if _lb[0] == "dragon":
                    res["strategies"] = list(res.get("strategies") or []) + [f"P5-炸板回封龙头:{_lb[2]}"]
                    res["burst_score"] = round(min(res["burst_score"] + 8.0, 100.0), 2)
                    res["detail"]["炸板回封龙头加分"] = 8.0
                elif _lb[0] == "fake":
                    res["strategies"] = list(res.get("strategies") or []) + [f"P5-假回封诱多:{_lb[2]}"]
                    res["burst_score"] = round(max(res["burst_score"] - 8.0, 0.0), 2)
                    res["risk_tags"].append("⚠️【假回封诱多】炸板回封失败，强烈建议跳过")
                    res["detail"]["假回封诱多减分"] = 8.0
            except Exception as _ex:
                logging.debug("P5 炸板回封分级异常: %s", _ex)

            # === V28 D 项：封板质量三维（封板时间/开板/封单/联袂） ===
            try:
                from core.limit_quality_3d import compute_limit_quality_3d
                _lq_bonus, _lq_label = compute_limit_quality_3d(rt if isinstance(rt, dict) else {})
                if _lq_bonus != 0.0:
                    res["burst_score"] = round(
                        float(np.clip(res.get("burst_score", 0.0) + _lq_bonus, 0.0, 100.0)),
                        2,
                    )
                    res["detail"]["封板质量三维"] = _lq_label
                    if _lq_bonus >= 4.0:
                        res["strategies"] = list(res.get("strategies") or []) + [f"P5-封板质量强:{_lq_label}"]
                    elif _lq_bonus <= -6.0:
                        res["strategies"] = list(res.get("strategies") or []) + [f"P5-封板质量差:{_lq_label}"]
                        res["risk_tags"].append("⚠️【封板质量差】开板≥3 或孤板 跳过")
            except Exception as _e_lq:
                logging.debug("P5 封板质量三维异常: %s", _e_lq)
            # 早盘量比底线用于解释全天收盘强弱，不作为前置条件引用
            vmf = _p5_safe_float(_p5_rt_get(rt, "vr_morning_floor", -1.0), -1.0)
            if vmf >= 0.0 and vmf < 0.75 and pct_close >= 5.0 and vr >= 1.2:
                res["detail"]["早盘定调vs全日收盘"] = (
                    f"floor_vr={vmf:.2f} 收盘涨幅={pct_close:.1f}% 全日量比={vr:.2f}"
                )
            # 【缩量分歧低吸】低权重附加：略抬 burst 上限内加分，并打仓位≤10%纪律标签
            if any("缩量分歧低吸" in h for h in hits):
                res["burst_score"] = round(min(res["burst_score"] + 12.0, 100.0), 2)
                res["risk_tags"].append("📉【缩量分歧低吸】建议单笔≤10%仓位")
                res["detail"]["缩量分歧低吸加分"] = 12.0
            # 重新归纳一次动作，确保新增细分标签被识别
            primary_action, secondary_actions, market_status, risk_level = _derive_p5_action_summary(
                hits,
                vwap_mult=vwap_mult,
                risk_tags=res["risk_tags"],
            )
            res["primary_action"] = primary_action
            res["secondary_actions"] = secondary_actions
            res["market_status"] = market_status
            res["risk_level"] = risk_level
            # 【V34 P1】能否进池由「命中档位」决定：观察档（P5-17 均线粘合、P5-21 高位旗形蓄势）
            # 只出信息，不再绕过金门禁。原实现只要 hits 非空就置 True ⇒
            # 叠加 VWAP 防伪 mask 时「零战法也进池」（已实测复现）。
            # 判定委托给 screener 的 p5_hits_are_actionable：对清单外战法名一律放行，
            # 避免「新增策略忘了登记动作清单 → 静默不进池」。
            res["p5_core_screener_pass"] = bool(p5_hits_are_actionable(hits))
            res["detail"]["主动作"] = primary_action
            res["detail"]["次级动作"] = "|".join(secondary_actions) if secondary_actions else ""
            res["detail"]["市场状态"] = market_status
            res["detail"]["风险等级"] = risk_level
            entry_reason = "主线前排，次日延续更稳"
            if any("P5-15" in h or "P5-16" in h for h in hits):
                entry_reason = "箱体回踩确认，延续更稳"
            elif any("P5-17" in h or "P5-18" in h or "P5-19" in h for h in hits):
                entry_reason = "均线发散确认，趋势更顺"
            elif any("P5-07" in h or "P5-08" in h or "P5-09" in h for h in hits):
                entry_reason = "趋势确认较强，延续更顺"
            elif any("P5-20" in h for h in hits):
                entry_reason = "缩量分歧低吸，等次日确认"
            elif any("P5-21" in h for h in hits):
                entry_reason = "高位旗形缩量蓄势，等放量突破确认"
            elif any("P5-22" in h for h in hits):
                entry_reason = "5日线上穿20日线，趋势转折确认"
            elif any("P5-23" in h for h in hits):
                entry_reason = "突破布林上轨，动能转强"
            elif any("P5-24" in h for h in hits):
                entry_reason = "放量创20日新高，突破有效"
            elif any("P5-25" in h for h in hits):
                entry_reason = "MACD金叉上攻，动能确认"
            elif any("P5-14" in h for h in hits):
                entry_reason = "欧奈尔趋势模板成立，趋势股配置"
            elif res.get("burst_score", 0.0) >= 85.0 and "假强" not in "|".join(res["risk_tags"]):
                entry_reason = "主线前排，次日延续更稳"
            elif any("尾盘偷袭" in tag or "派发" in tag or "假强" in tag for tag in res["risk_tags"]):
                entry_reason = "尾盘有虚火，防兑现"
            elif primary_action in ("追涨", "核心"):
                entry_reason = "主线偏强，次日看强"
            res["detail"]["入池理由"] = entry_reason
            buy_hint = "次日先看能否站稳关键位，确认后再考虑加仓。"
            if any("P5-15" in h or "P5-16" in h for h in hits):
                buy_hint = "箱体回踩确认后再看，稳住箱体上沿更好。"
            elif any("P5-17" in h or "P5-18" in h or "P5-19" in h for h in hits):
                buy_hint = "均线发散确认后再跟，先看承接是否持续。"
            elif any("P5-20" in h for h in hits):
                buy_hint = "缩量分歧低吸型，先等企稳不破关键位后再试仓。"
            elif any("P5-21" in h for h in hits):
                buy_hint = "高位旗形蓄势型，尚未突破；等放量站上前高再跟，勿提前埋伏。"
            elif any("P5-22" in h for h in hits):
                buy_hint = "双均线金叉确认型，次日沿MA5持有；跌破MA5收盘0.5%止损。"
            elif any("P5-23" in h for h in hits):
                buy_hint = "布林上轨突破型，次日回踩上轨不破可加仓；跌回中轨减仓。"
            elif any("P5-24" in h for h in hits):
                buy_hint = "量价新高突破型，次日站稳突破位持有；跌回20日高点半仓观察。"
            elif any("P5-25" in h for h in hits):
                buy_hint = "MACD金叉趋势型，次日站稳MA20持有；死叉或跌破MA20离场。"
            elif any("P5-14" in h for h in hits):
                buy_hint = "欧奈尔趋势模板型，沿MA60趋势持有；跌破MA60收盘1%止损。"
            elif primary_action in ("追涨", "核心") and "高位爆量派发" not in "|".join(res["risk_tags"]):
                buy_hint = "主线真龙可次日轻仓跟随，回踩VWAP或5日线确认后再加。"
            if any("尾盘偷袭" in tag or "派发" in tag or "假强" in tag for tag in res["risk_tags"]):
                buy_hint = "尾盘偏强但需防兑现，建议次日确认站稳再介入。"
            if any("假强" in h for h in hits):
                buy_hint = "疑似假强，先观望，等次日确认延续再考虑。"
            res["buy_hint"] = buy_hint
            res["template_label"] = "主推" if res.get("burst_score", 0.0) >= 85.0 and "假强" not in "|".join(res["risk_tags"]) else ("观察" if res.get("burst_score", 0.0) >= 40.0 else "不追高")

            # 【V26.88+9-22 优化28+21+27】P5 盘后入场质量评估 + 自动止损 + EV 评分
            try:
                from core.stop_loss_utils import (
                    evaluate_entry_quality, compute_expected_value,
                )
                strat_type = "leader" if any("龙头" in t for t in res["risk_tags"]) else "right_side"
                _eq5 = evaluate_entry_quality(work, rt, risk_reward_min=2.0, strategy_type=strat_type)
                _ev5 = compute_expected_value(
                    risk_reward=_eq5["risk_reward_ratio"],
                    signal_strength=float(res.get("burst_score", 60.0) or 60.0),
                    penalty=float(res.get("penalty", 0.0) or 0.0),
                )
                res["entry_quality"] = _eq5
                res["expected_value"] = _ev5
                res["auto_stop_loss"] = _eq5["stop_loss"]
                res["auto_take_profit"] = _eq5["take_profit"]
                res["trailing_stop_rules"] = _eq5["trailing_stop_rules"]
                res["trade_grade"] = _ev5["trade_grade"]
                res["detail"]["P5入场质量分"] = _eq5["entry_quality_score"]
                # 质量低时附加标签
                if not _eq5["pass_2_1"] and _eq5["entry_quality_score"] < 50:
                    res["risk_tags"].append(
                        f"📉[赔率低]RR={_eq5['risk_reward_ratio']:.1f} 评级{_ev5['trade_grade']}"
                    )
            except Exception as _e_aq5:
                logger.debug("[P5] 入场质量评估跳过: %s", _e_aq5)

            # 【V34 修复 · P0】merge 返回新字典，原式丢弃返回值 → 风控扣分/标签全部落空。
            # deduct_burst_score=False：pipeline 已按 res["penalty"] 扣一次，避免双重扣分。
            res = merge_risk_into_engine_result(
                res, risk_pre, penalty_key="penalty", deduct_burst_score=False,
            )

            # 【V34 P1】P5 仓位口径：必须在 merge 之后——阶梯要看到合并后的 penalty 与建议最低分。
            try:
                _pos5, _pos5_ladder = self._p5_position_ladder(
                    score=float(res.get("burst_score", 0.0) or 0.0),
                    penalty=float(res.get("penalty", 0.0) or 0.0),
                    dim_count=int(sum(1 for v in dim_map.values() if v)),
                    cross_bonus=float(cross_bonus or 0.0),
                    action=str(res.get("primary_action", "") or ""),
                    trade_grade=str(res.get("trade_grade", "") or ""),
                    layer_overheat=int(_p5_safe_float(rt.get("_layer_overheat_score", 0), 0.0)),
                    template_label=str(res.get("template_label", "") or ""),
                    suggested_min=_p5_safe_float(res.get("suggested_min_entry_score", 0.0), 0.0),
                )
                res["max_position_ratio"] = round(float(_pos5), 3)
                res["position_ladder"] = _pos5_ladder
                res["detail"]["max_position_ratio"] = round(float(_pos5), 3)
                res["detail"]["position_reason"] = str(
                    _pos5_ladder.get("reason", "") or _pos5_ladder.get("dim_count", "")
                )
            except Exception as _e_pos5:
                logger.debug("[P5] 仓位阶梯跳过: %s", _e_pos5)
            try:
                res["wechat_hint"] = self._p5_wechat_hint(rt, res)
            except Exception:
                res["wechat_hint"] = f"P5盘后|分{float(res.get('burst_score', 0.0) or 0.0):.0f}"

        except Exception as e:
            logger.exception(
                "P5 run_all 异常，ts_code=%s, trade_date=%s, detail_keys=%s",
                code if 'code' in locals() else "",
                _p5_trade_date(rt),
                sorted(list(res.get("detail", {}).keys())) if isinstance(res.get("detail"), dict) else [],
            )
            res["p5_veto_reason"] = f"run_all异常:{type(e).__name__}"
            return res

        # === V28 真实接入 signal_log ===
        try:
            from core.pool_signal_adapter_v28 import append_signal_log_safe
            # 【V30.10 修复 2026-10-21】_GLOBAL_DB_CONN 是全项目从未赋值的死变量，
            # 恒为 None → P5 信号从不落库。改为 None 传入，append_signal_log_safe
            # 内部会用 resolve_write_conn() 自动解析可用连接。
            db_conn = None
            # V28-4 修复：先注入 push_template（含 should_push_now）再注入 exit_plan
            try:
                from core.push_template_momentum_reversal import apply_push_template_to_res
                apply_push_template_to_res(res)
            except Exception:
                pass
            # V28-4 修复：exit_plan 显式注入
            try:
                from core.exit_strategy import attach_exit_plan_to_res
                rt_for_price = rt if isinstance(rt, dict) else {}
                push_price = float(rt_for_price.get("price") or rt_for_price.get("now_price") or 0.0)
                if push_price > 0:
                    res_with_pool = dict(res)
                    res_with_pool.setdefault("pool", "p5")
                    attach_exit_plan_to_res(res_with_pool, push_price=push_price)
                    if isinstance(res_with_pool.get("exit_plan"), dict):
                        res["exit_plan"] = res_with_pool["exit_plan"]
                        res["exit_template"] = res_with_pool.get("exit_template", "medium")
            except Exception:
                pass
            # V28-4 修复：推送去重（票级冷却 + 跨池冷却）
            try:
                from core.push_dedup import pool_apply_push_dedup
                pool_apply_push_dedup(res, "p5")
            except Exception:
                pass
            # 【V30.10 阈值统一 2026-10-21】push_min_score 改为从 config.yaml 动态解析
            # （core/push_threshold.resolve_push_min_score），不再硬编码。
            # 【2026-11 修复】补 rt=rt：append_signal_log_safe 取价顺序为
            # rt.price → rt.now_price → res.price → res.push_price → 0，price<=0 直接早退。
            # 不传 rt 时只有 res 自带价格才能落库，与 P2/P4 同根因（B1），此处补齐。
            from core.push_threshold import resolve_push_min_score
            append_signal_log_safe(res, "p5", db_conn,
                                    push_min_score=resolve_push_min_score("p5", for_log=True),
                                    rt=rt if isinstance(rt, dict) else None)
        except Exception:
            pass

        return res
