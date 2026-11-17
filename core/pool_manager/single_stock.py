# -*- coding: utf-8 -*-
"""
core/pool_manager/single_stock.py
原 core/pool_manager.py L1033-L1859（_evaluate_p1_single_stock + _process_single_stock_for_p1）

【职责】
- _evaluate_p1_single_stock：委托 score_calibration.compute_p1_multi_dim_smooth_score
- _process_single_stock_for_p1：单票 P1 安检 800+ 行主流程
  - 涨停炸板拦截
  - 前置硬过滤（市值/黑名单/解禁/PE/流动性）
  - 特殊事件拦截 + 业绩首亏拦截
  - 市值档位判定 + 缩量期撤销呆滞
  - PE / 乖离 / 筹码 / 深海线 / 早龙释放 / 强攻击阳线
  - 大盘温度调节 + A 股特色增强 + 动量/反转 + 机构加速度 + 业绩剪刀差 + 跨池接力
  - 题材生命周期 + 龙虎榜 + exit_plan 注入
  - 渣男股性分级惩罚
  - 返回标准化结果 dict

【依赖】
- core.pool_manager.thresholds（_safe_float / _evaluate_p1_single_stock / 容错补偿 + 行为判定）
"""
import logging
import os
from datetime import datetime
from typing import Any, Dict, Set

import numpy as np
import pandas as pd

from core.pool_manager.thresholds import (
    P1_AMNESTY_DYNAMIC_INFLOW_CAP_YUAN,
    P1_MACD_BAR_KILL,
    P1_MV_SUPER_YI,
    P1_NEAR_MA20_MIN_RATIO,
    P1_PASS_LINE,
    P1_TREND_MA120_MIN_RATIO,
    P1_TREND_SLOPE_FASTPASS,
    P1_VOL_DIVERGENCE_RATIO,
    _compute_p1_compensation_tier,
    _effective_p1_thresholds,
    _macd_hist_1x,
    _p1_behavior_penalty_hits,
    _p1_is_high_cooldown,
    _p1_is_single_peak_dense,
    _p1_is_two_day_below_ma20,
    _p1_large_cap_bonus,
    _p1_one_day_trip_check,
    _p1_mv_yi_for_thr,
    _p1_mv_fail_reason_for,
    _safe_float,
    adaptive_turnover_kill_threshold_relaxed,
    infer_turnover_rate_f_pct,
    turnover_kill_threshold_pct,
)

import constants

logger = logging.getLogger(__name__)


# ======================================================================
# [V31-B1] MACD gate shadow diff recorder
# ----------------------------------------------------------------------
# The MACD gate changed from an absolute price-unit threshold to a
# relative percentage-of-price threshold. This recorder keeps the
# disagreement between the old and the new gate as an auditable trail.
#
# Iron rules:
#   1. Read-only with respect to the DB: it only writes a local JSONL
#      file under data/runtime/.
#   2. It never changes scores, weights, thresholds or history data.
#   3. Any failure is swallowed: shadow logging must never break P1.
# ======================================================================
_MACD_SHADOW_LOGGER_NAME = "p1_macd_gate_shadow"
_MACD_SHADOW_MAX_BYTES = 5 * 1024 * 1024
_MACD_SHADOW_MAX_LINES = 20000


def _macd_shadow_log_path() -> str:
    try:
        from core.runtime_data_paths import RUNTIME_DIR

        os.makedirs(RUNTIME_DIR, exist_ok=True)
        return os.path.join(RUNTIME_DIR, "p1_macd_gate_shadow.jsonl")
    except Exception:
        import tempfile

        return os.path.join(tempfile.gettempdir(), "p1_macd_gate_shadow.jsonl")


def _trim_macd_shadow_log(path: str) -> None:
    """Keep the shadow log bounded: rotate when it grows past the byte cap."""
    try:
        if not os.path.isfile(path) or os.path.getsize(path) <= _MACD_SHADOW_MAX_BYTES:
            return
        keep = path + ".1"
        if os.path.isfile(keep):
            os.remove(keep)
        os.replace(path, keep)
    except Exception:
        pass


def _record_macd_gate_shadow_diff(rec: dict) -> None:
    """Record one MACD gate disagreement. Best-effort, never raises."""
    try:
        import json as _json
        import os as _os

        path = _macd_shadow_log_path()
        _trim_macd_shadow_log(path)
        rec = dict(rec or {})
        rec["ts"] = datetime.now().isoformat(timespec="seconds")
        with open(path, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("[p1-macd-shadow] 写入失败（忽略）")



# ================= ⚖️ 第五层：极致平滑打分系统 (满分 100) =================
def _evaluate_p1_single_stock(df, rt, circ_mv_yi, ind_rank, pe, ind_stats, ind, dynamic_industries, avg_trn, pass_line):
    """委托 score_calibration.compute_p1_multi_dim_smooth_score：多维分项平滑 + 行业/市值附加；
    fund_memory_score 是否凸入最终分由 config/constants 的 fund_memory_weight_p1 决定；不读 capital_resonance_score。
    """
    try:
        from core.strategies.score_calibration import compute_p1_multi_dim_smooth_score

        return compute_p1_multi_dim_smooth_score(
            df, rt, circ_mv_yi, ind_rank, pe, ind_stats, ind,
            dynamic_industries, avg_trn, pass_line,
        )
    except Exception as e:
        logger.debug(f"P1 单股打分底层异常: {e}")
        return 0.0, False, f"打分系统异常: {e}", {}


# ================= 🛡️ 四大物理防线区 =================
def _process_single_stock_for_p1(item, industry_pe_stats, global_stats, industry_map, sector_rank_map, dynamic_industries, unlock_blacklist, punish_blacklist, thresholds):
    """P1 单票处理主流程（800+ 行）。详细审计见 docstring 顶部模块说明。"""
    inflow_ratio = 0.0
    inflow_10d_ratio = 0.0
    used_fast_track = False
    p1_profile = str(thresholds.get("_p1_profile_key", "neutral")).strip()
    p1_mv_bar = _p1_mv_yi_for_thr(thresholds)
    try:
        ts_code = item.get('ts_code', item.get('code', ''))
        df = item.get('df', pd.DataFrame())
        rt = item.get('hist', item.get('rt', {}))

        # === V27-缺口修复：涨停炸板前置拦截 ===
        try:
            from core.p1_limit_break_filter import prefilter_yesterday_limit_break
            item = prefilter_yesterday_limit_break([item], penalty_score=30.0, drop_to_observation=False)[0] or item
        except Exception as _e_lb:
            logger.debug(f"[limit-break-filter] {ts_code}: {_e_lb}")

        # 全局前置硬过滤：流通市值不足 P1 下限一律在最前面拦截（先于黑名单/解禁/PE/流动性）
        circ_mv_raw_early = rt.get('circ_mv')
        if circ_mv_raw_early is None or pd.isna(circ_mv_raw_early):
            circ_mv_raw_early = rt.get('total_mv', 0) * 0.6
        if (circ_mv_raw_early is None or pd.isna(circ_mv_raw_early)) and isinstance(df, pd.DataFrame) and (not df.empty) and ('circ_mv' in df.columns):
            circ_mv_raw_early = _safe_float(df['circ_mv'].iloc[-1], 0.0)
        circ_mv_yi_early = _safe_float(circ_mv_raw_early) / max(10000.0, 1e-9)
        # 【V26.88+10-3 修复-1 P0-①】用户硬约束：全行业统一 120 亿市值闸，移除 T0 战略行业 80 亿放宽
        _mv_bar_eff = p1_mv_bar
        if circ_mv_yi_early < _mv_bar_eff:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": _p1_mv_fail_reason_for(_mv_bar_eff), "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        if ts_code in punish_blacklist:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": "🛑黑名单冷却期 (防报复性交易)", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        # 【F0-1 修复】拆分 df.empty / len(df)<100 / not rt 三种失败原因文案
        # 之前统一返回 "上市不足或无数据" 导致 1109 只票全军覆没时无法定位是日线缺失还是实时快照为空。
        # 【F2-5/F2-7】长假期宽容：当 thresholds["_p1_is_holiday_window"]=True 时，len(df)<100 闸门降级为 len(df)<60，
        # 这样国庆/中秋/清明前两天（anchor 与 today gap=1~2）不再全军覆没。
        _holiday_window = bool(thresholds.get("_p1_is_holiday_window", False))
        _min_bars = 60 if _holiday_window else 100
        if df is None or df.empty:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": "❌日线 df 为空(数据库未取到 K 线)", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}
        if len(df) < _min_bars:
            _holiday_tag = " (假期宽容<60)" if _holiday_window else ""
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": f"⚠️日线仅 {len(df)} 天 < {_min_bars}{_holiday_tag}(刚上市/复权取错/IPO 早期)", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}
        if not rt:
            # 【F0-3 修复】单票 rt 缺失时不直接出局，合成最小 rt 后继续走打分。
            # 适用场景：正常交易日但 fetch_realtime_batch 限流/单只票接口超时/数据源临时挂。
            # 日线健康(df 长度>=100)即说明基本面可用，rt 维度会因缺失而被评分端当作"无资金信号"扣分，
            # 这比 0 分出局更接近真实质地。
            try:
                _last = df.iloc[-1].to_dict()
                rt = {
                    "price": float(_last.get("close") or 0.0),
                    "open": float(_last.get("open") or 0.0),
                    "high": float(_last.get("high") or 0.0),
                    "low": float(_last.get("low") or 0.0),
                    "pre_close": float(_last.get("pre_close") or 0.0),
                    "vol": float(_last.get("vol") or 0.0),
                    "amount": float(_last.get("amount") or 0.0),
                    "turnover_rate_f": float(_last.get("turnover_rate_f") or 0.0),
                    "pe_ttm": float(_last.get("pe_ttm") or 0.0),
                    "pb": float(_last.get("pb") or 0.0),
                    "ps_ttm": float(_last.get("ps_ttm") or 0.0),
                    "total_mv": float(_last.get("total_mv") or 0.0),
                    "circ_mv": float(_last.get("circ_mv") or 0.0),
                    "name": "",
                    "_rt_fallback": True,
                }
                logger.debug(f"[rt-fallback] {ts_code}: 单票 rt 缺失，用 df 末行合成最小 rt 继续打分")
            except Exception as _e_rf:
                return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": f"📡实时快照空 + 合成失败({_e_rf})", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        # 【修复】现价来源：优先日线末行收盘价（固定），仅在日线价格为0时回退实时价格，保证收盘后结果稳定。
        _df_close = _safe_float(df['close'].iloc[-1]) if not df.empty else 0.0
        _rt_price = _safe_float(rt.get('price', 0.0))
        now_price_initial = _df_close if _df_close > 0 else _rt_price
        _p1_inst_min_yuan = float(constants.P1_INSTITUTION_MIN_PRICE_YUAN)
        if now_price_initial < _p1_inst_min_yuan:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": f"💀机构审美排斥(现价{now_price_initial:.2f}元<{_p1_inst_min_yuan:g}元红线)", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        # 【V26.88+10-3 P0-② MA20 之上前闸】用户硬约束：只在 20 日线之上的票入池
        # 选择 B：近 N 日有 M 日收盘 >= MA20 即视为 MA20 之上（兼顾刚突破的"刚启动"票）
        # 【F3-1 改造】原为硬编码「近 3 日有 2 日 >= MA20」。2026-10-03 实测该闸砍掉 64.7% 候选
        # （718/1109），与「每日 ≥ 30 只」硬约束直接冲突，且窗口太窄、1 天噪声就能翻转结果。
        # 现改为读 config.yaml profiles.<档>.ma20_pregate_lookback / ma20_pregate_above_days，
        # 默认值 3/2 与改造前完全一致（缺配置时零行为变化）。
        # 【F3-2 澄清】两道 MA20 闸语义互补，不是重复切：
        #   - 本前闸：窗口内「计数」规则——近 N 日有 M 日收在 MA20 之上（看的是一段时间的站上广度）
        #   - 深海防线：近端「连续」规则——最近连续 2 日收在 MA20 之下（看的是当下是否正在失守）
        # 构造反例可见二者互不包含：closes=[12, 11.5, 9, 8.8, 9.2] / MA20=10 时，
        # 前闸放行（窗口内 2 日站上）而深海拦截（近端连续 2 日跌破），后者正是「回落后失守」。
        # 因此两道闸都保留：合起来表达「近期站上过 MA20，且当下没在跌破 MA20」。
        try:
            _ma20_lookback = int(thresholds.get("ma20_pregate_lookback", 3) or 3)
            _ma20_need_days = int(thresholds.get("ma20_pregate_above_days", 2) or 2)
            _ma20_pre = _safe_float(df['ma20'].iloc[-1]) if 'ma20' in df.columns else 0.0
            if _ma20_pre > 0:
                _lk = max(1, min(_ma20_lookback, len(df)))
                _close_win = [
                    _safe_float(df['close'].iloc[-1 - _k]) for _k in range(_lk)
                ] if 'close' in df.columns else []
                _above_days = sum([c >= _ma20_pre for c in _close_win])
                if _above_days < _ma20_need_days:
                    return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": f"📉 MA20下方(近{_lk}日仅{_above_days}日站上MA20,要求≥{_ma20_need_days}日)", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}
        except Exception as _e_ma20:
            logger.debug(f"[ma20-pregate][{ts_code}] {type(_e_ma20).__name__}: {_e_ma20}")
            # 容错：MA20 数据异常 → 不拦截（避免和 df 缺失混淆）

        if ts_code in unlock_blacklist:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": "💀未来30天内有天量解禁", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        # === V28 B 项：特殊事件拦截 ===
        try:
            from core.event_filter import compute_event_penalty
            _list_date = (rt.get("list_date") or rt.get("ipo_date") or "") if isinstance(rt, dict) else ""
            if not _list_date and isinstance(df, pd.DataFrame) and "list_date" in df.columns and not df.empty:
                # 【V30-修复】rt 来自实时快照、本轮已由 api_fetcher 注入 list_date；
                # 这里再兜一层日线列，覆盖 rt 合成路径（rt-fallback）与 UI 直通车。
                try:
                    _ld_daily = str(df["list_date"].iloc[-1] or "").strip()
                    if _ld_daily:
                        _list_date = _ld_daily
                except Exception:
                    pass
            if isinstance(rt, dict) and "list_days" not in rt:
                if isinstance(df, pd.DataFrame) and "list_days" in df.columns and not df.empty:
                    try:
                        _lv = df["list_days"].iloc[-1]
                        if pd.notna(_lv):
                            rt["list_days"] = int(float(_lv))
                    except Exception:
                        pass
            _ev_penalty, _ev_label, _ev_veto = compute_event_penalty(
                df,
                rt if isinstance(rt, dict) else {},
                list_date=_list_date,
                ts_code=ts_code,
                name=(rt.get("name") if isinstance(rt, dict) else ""),
            )
            if _ev_veto:
                return {
                    "ts_code": ts_code, "score": 0.0, "is_pass": False,
                    "reason": f"🚨特殊事件拦截[{_ev_label}]",
                    "item": item, "df": df, "inflow_ratio": inflow_ratio,
                    "inflow_10d_ratio": inflow_10d_ratio, "score_details": {},
                    "used_fast_track": used_fast_track,
                }
        except Exception as _e_ef:
            logger.debug(f"[event-filter] {ts_code}: {_e_ef}")

        forecast_type = _safe_float(rt.get('forecast_type', 0))
        if forecast_type == -2:
            # 【F2-4】周期行业（钢铁/煤炭/化工/有色/航运/养殖）的业绩首亏多为周期反转信号；
            # 改为降级为 forecast_type=-1（预减），由评分端扣分，不直接 0 分出局。
            _cycle_inds = ("钢铁", "煤炭", "化工", "有色金属", "航运", "养殖", "石油石化", "化纤", "造纸")
            if ind in _cycle_inds and circ_mv_yi_early >= 200.0:
                logger.debug(f"[F2-4 周期反转] {ts_code}({ind}) 业绩首亏但属周期行业且≥200亿，降至 -1 走打分")
                forecast_type = -1
            else:
                return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": "💣业绩首亏/续亏爆雷", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        circ_mv_raw = rt.get('circ_mv')
        if circ_mv_raw is None or pd.isna(circ_mv_raw):
            circ_mv_raw = rt.get('total_mv', 0) * 0.6
        circ_mv_yi = _safe_float(circ_mv_raw) / max(10000.0, 1e-9)
        # 流通下限已在前置过滤，这里仅用于档位标签与后续打分口径
        if circ_mv_yi >= 2000.0:
            size_emoji, mv_tier = "🦍", "2000亿+巨无霸"
        elif circ_mv_yi >= 1000.0:
            size_emoji, mv_tier = "🐘+", "1000-2000亿千亿中军"
        elif circ_mv_yi >= 500.0:
            size_emoji, mv_tier = "🐘", "500-1000亿超级中军"
        else:
            size_emoji, mv_tier = "🐎", f"{int(p1_mv_bar)}-500亿核心中盘"

        if circ_mv_yi < p1_mv_bar:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": _p1_mv_fail_reason_for(p1_mv_bar), "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        # 超级大盘：放宽「贴 MA20 / MACD 柱斩杀」等短线项（不再依赖资金共振截面分）。
        relaxed_short_ma = circ_mv_yi >= P1_MV_SUPER_YI

        circ_mv_base = max(circ_mv_yi * 100000000.0, 1.0)
        net_inflow_3d = 0.0
        net_inflow_5d_early = 0.0
        net_inflow_10d = 0.0
        for col in ('net_main_amount',):
            if col in df.columns:
                net_inflow_3d += _safe_float(df.tail(3)[col].sum())
                net_inflow_5d_early += _safe_float(df.tail(5)[col].sum())
                net_inflow_10d += _safe_float(df.tail(10)[col].sum())

        _cm_denom = max(circ_mv_base, 1e-9)
        inflow_ratio = net_inflow_3d / _cm_denom
        inflow_10d_ratio = net_inflow_10d / _cm_denom

        df_tail5 = df.tail(5)
        avg_amount = df_tail5['amount'].mean() if 'amount' in df_tail5.columns else 999999
        _tr5 = []
        for _, rr in df_tail5.iterrows():
            v = _safe_float(rr.get("vol"), 0.0)
            c = _safe_float(rr.get("close"), 0.0)
            cm = _safe_float(rr.get("circ_mv"), 0.0)
            t0 = _safe_float(rr.get("turnover_rate_f"), 0.0)
            _tr5.append(t0 if t0 > 0 else infer_turnover_rate_f_pct(v, c, cm))
        avg_trn = float(np.mean(_tr5)) if _tr5 else 0.0

        tr10 = []
        flow_positive_days_10 = 0
        for _, rr in df.tail(10).iterrows():
            v = _safe_float(rr.get("vol"), 0.0)
            c = _safe_float(rr.get("close"), 0.0)
            cm = _safe_float(rr.get("circ_mv"), 0.0)
            t0 = _safe_float(rr.get("turnover_rate_f"), 0.0)
            tr10.append(t0 if t0 > 0 else infer_turnover_rate_f_pct(v, c, cm))
            if _safe_float(rr.get("net_main_amount", 0.0)) > 0:
                flow_positive_days_10 += 1
        avg_trn_10 = float(np.mean(tr10)) if tr10 else 0.0
        recent_10 = df.tail(10)
        if not recent_10.empty and "close" in recent_10.columns:
            close_max_10 = _safe_float(recent_10["close"].max())
            close_min_10 = _safe_float(recent_10["close"].min())
            range_10_pct = (close_max_10 - close_min_10) / max(close_min_10, 1e-9) * 100.0 if close_min_10 > 0 else 0.0
        else:
            range_10_pct = 0.0
        ttl_quiet = (flow_positive_days_10 < 2) and (avg_trn_10 < 1.0) and (range_10_pct < 6.0)

        if avg_amount < 100000.0:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": f"🧊流动性枯竭(近5日均成交额仅{avg_amount/10000:.2f}亿)", "item": item, "price": avg_amount, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        ind = industry_map.get(ts_code, '未知')
        ind_rank = int(sector_rank_map.get(ind, 999))

        _df_vr = 0.0
        if not df.empty and "vol_ratio" in df.columns:
            _df_vr = _safe_float(df["vol_ratio"].iloc[-1], 0.0)
        _rt_vr = _safe_float(rt.get('vol_ratio', 1.0))
        vr = _df_vr if _df_vr > 0 else _rt_vr
        if vr <= 0:
            vr = 1.0
        _open_today_for_attack = _safe_float(rt.get("open"), 0.0)
        if _open_today_for_attack <= 0 and not df.empty and "open" in df.columns:
            _open_today_for_attack = _safe_float(df["open"].iloc[-1], 0.0)
        is_strong_attack = bool(
            vr >= 1.4 and _open_today_for_attack > 0 and now_price_initial > _open_today_for_attack
        )
        # 动态流入特赦门槛：先按市值 3.5% 比例与 4 亿地板取 max，再 min 硬性封顶（15 亿），避免千亿市值算出天文数字使特赦失效
        dynamic_inflow_threshold = min(
            P1_AMNESTY_DYNAMIC_INFLOW_CAP_YUAN,
            max(400000000.0, circ_mv_yi * 100000000.0 * 0.035),
        )
        # 提前定义 curr = df.iloc[-1]，避免后续引用时 NameError
        curr = df.iloc[-1]
        cost_50th = _safe_float(curr.get('cost_50th', 0.0))
        if cost_50th <= 0:
            cost_50th = _safe_float(rt.get('cost_50th', 0.0))
        price_insurance = (cost_50th > 0 and now_price_initial <= cost_50th * 1.30) or (cost_50th <= 0)

        flow_positive_days_5 = sum(1 for _, rr in df.tail(5).iterrows() if _safe_float(rr.get("net_main_amount", 0.0)) > 0)
        is_continuous_inflow = (flow_positive_days_5 >= 3) or (net_inflow_3d > 0 and net_inflow_5d_early > 0 and avg_trn_10 >= avg_trn)
        if not is_continuous_inflow and net_inflow_3d <= 0:
            pass
        is_amnesty = (circ_mv_yi >= 100.0) and is_continuous_inflow and (net_inflow_3d > dynamic_inflow_threshold) and (vr > 2.5) and (ind_rank <= 5) and price_insurance
        amnesty_reason = ""

        is_turnover_killed = False
        kill_reason = ""

        # 【V31 批次 B-2】换手底线改为市值连续函数（smoothstep 窄带），
        # 消除 1000 亿 / 2000 亿两处阶跃：同一只票不再因市值微幅波动换一套标准。
        # 死因文案保留「股性呆滞」关键词，兼容 core/p1_death_cause_registry 的 L2 登记。
        # 【V30.14 2026-11-05】千亿蓝筹：这里先算一次加分配置，换手底线按其系数放宽
        # （千亿票天然低换手，不该套用中小盘的换手门槛；市值闸不受影响）。
        try:
            from core.config_manager import get_p1_large_cap_bonus_config

            _lc_cfg = get_p1_large_cap_bonus_config()
        except Exception:
            _lc_cfg = {}
        _lc_info = _p1_large_cap_bonus(
            circ_mv_yi,
            curr,
            df,
            vol_ratio=vr,
            pct_chg_today=_safe_float(rt.get("pct_chg", curr.get("pct_chg", 0.0))),
            flow_positive_days_5=flow_positive_days_5,
            cfg=_lc_cfg,
        )
        _lc_turnover_relax = float(_lc_info.get("turnover_relax", 1.0) or 1.0)

        _turnover_kill_line = turnover_kill_threshold_pct(circ_mv_yi) * _lc_turnover_relax
        if avg_trn < _turnover_kill_line:
            is_turnover_killed = True
            kill_reason = (
                f"🧊股性呆滞(近5日均换手{avg_trn:.2f}%<底线{_turnover_kill_line:.2f}%"
                f",市值{circ_mv_yi:.0f}亿)"
            )

        # 【自适应优化】极度缩量期：若近5日均换手仍不低于放宽后地板，撤销股性呆滞拦截
        _mcs_p1 = float(thresholds.get("_market_contraction_score", 0.0) or 0.0)
        if is_turnover_killed and _mcs_p1 >= 0.7:
            _nm_rt = _safe_float(rt.get("net_main_amount", 0.0))
            _rel_tr = adaptive_turnover_kill_threshold_relaxed(
                circ_mv_yi,
                net_main_amount=_nm_rt,
            )
            if avg_trn >= _rel_tr:
                is_turnover_killed = False
                kill_reason = ""

        if is_turnover_killed:
            if is_amnesty:
                amnesty_reason = f"特赦激活(极低换手被底部巨量资金捞回,VR:{vr:.1f})"
            else:
                is_shrink_lock_amnesty = False
                if p1_mv_bar <= circ_mv_yi < 2000.0 and flow_positive_days_5 >= 3:
                    if len(df) >= 22:
                        closes = df['close'].values
                        ma1 = closes[-20:].mean()
                        ma2 = closes[-21:-1].mean()
                        ma3 = closes[-22:-2].mean()
                        if closes[-1] >= ma1 and closes[-2] >= ma2 and closes[-3] >= ma3:
                            is_shrink_lock_amnesty = True

                if is_shrink_lock_amnesty:
                    amnesty_reason = f"缩量锁仓特赦(换手{avg_trn:.2f}%,近5日正流入{flow_positive_days_5}天,近3日稳站MA20)"
                else:
                    return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": kill_reason, "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        pe_raw = rt.get('pe_ttm') if rt.get('pe_ttm') is not None else rt.get('pe', 0)
        pe = _safe_float(pe_raw)

        if pe <= 0 or pe > 300:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": f"PE极值异常 (≤0或>300)", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        ind_stats = industry_pe_stats.get(ind, global_stats)
        ind_q75 = ind_stats['q75']

        # 【V26.88+10-3 P1-④】用户硬约束：PE 软硬卡统一改为 ×2.5 / ×1.7，不再做 T0 差异化
        # 硬卡 ×2.5 (PE 极高，直接出局)；软卡 ×1.7 (PE 偏高，可通过特赦/单峰缓刑)
        _pe_hard_cap = 2.5
        _pe_soft_cap = 1.7

        if pe > ind_q75 * _pe_hard_cap:
            # 【V26.88+10-3 P1-④】统一硬卡文案，不再区分 T0
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": f"PE极度超标 (PE:{pe:.1f} > 75分位{ind_q75:.1f}*{_pe_hard_cap:.1f})", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        if pe > ind_q75 * _pe_soft_cap:
            if is_amnesty:
                if amnesty_reason:
                    amnesty_reason += f" | PE特赦(流入{net_inflow_3d/100000000:.1f}亿)"
                else:
                    amnesty_reason = f"特赦激活(高估值被底部巨量资金强捞,VR:{vr:.1f})"
            elif _p1_is_single_peak_dense(curr, rt, now_price_initial) and is_continuous_inflow:
                amnesty_reason = amnesty_reason + " | 筹码单峰密集PE缓刑" if amnesty_reason else "筹码单峰密集PE缓刑"
            else:
                # 【V26.88+10-3 P1-④】统一 PE ×1.7 软卡：超出 → 拒绝（保留原有的特赦/单峰缓刑通道）
                return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": f"PE相对超标 (PE:{pe:.1f} > 75分位{ind_q75:.1f}*{_pe_soft_cap:.1f})", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        curr = df.iloc[-1]
        now_price = _safe_float(rt.get('price', curr.get('close', 0.0)))

        tail_vol_ratio = _safe_float(rt.get('tail_vol_ratio', 0.0))
        bias20 = _safe_float(curr.get('bias_20', (now_price - curr.get('ma20', now_price)) / max(curr.get('ma20', 1), 1) * 100.0))
        _ma60_for_bias = _safe_float(curr.get("ma60", 0.0))
        if _ma60_for_bias > 0:
            bias60 = _safe_float(
                curr.get(
                    "bias_60",
                    (now_price - _ma60_for_bias) / max(_ma60_for_bias, 1e-9) * 100.0,
                )
            )
        else:
            bias60 = 0.0
        if len(df) >= 60 and "high" in df.columns:
            highest_60d = _safe_float(df["high"].tail(60).max(), _safe_float(now_price))
        else:
            highest_60d = _safe_float(now_price)
        if highest_60d <= 0:
            highest_60d = max(_safe_float(now_price), 1e-9)

        # 趋势市(strict)：高位判定不交静态 BIAS20，交给后续熔断/乖离风控
        if p1_profile == "strict":
            is_high_position = now_price >= highest_60d * 0.88
        else:
            is_high_position = (bias20 > 8.0) or (now_price >= highest_60d * 0.88)

        relaxed_bear_bounce = p1_profile == "relaxed" and bias60 < -15.0 and net_inflow_3d > 0

        amplitude = 0.0
        high_val = _safe_float(curr.get('high', 0.0))
        low_val = _safe_float(curr.get('low', 0.0))
        # 与 scan/P3 一致：缺 pre_close 时用昨收链式回退，禁止默认 1 元导致振幅假爆表
        pre_close_val = _safe_float(curr.get("pre_close"), 0.0)
        if pre_close_val <= 0:
            pre_close_val = _safe_float(curr.get("close"), 0.0)

        if high_val > 0 and low_val > 0 and pre_close_val > 0:
            amplitude = (high_val - low_val) / pre_close_val * 100.0
        else:
            rt_price = _safe_float(rt.get('price', now_price))
            rt_open = _safe_float(rt.get('open', rt_price))
            rt_pre_close = _safe_float(rt.get('pre_close', pre_close_val))
            rt_high = _safe_float(rt.get('high', max(rt_price, rt_open)))
            rt_low = _safe_float(rt.get('low', min(rt_price, rt_open)))
            if rt_pre_close > 0:
                amplitude = (rt_high - rt_low) / rt_pre_close * 100.0
        amplitude = float(np.nan_to_num(amplitude, nan=0.0, posinf=0.0, neginf=0.0))

        # P1 去战术化：剔除尾盘偷袭/日内振幅惩罚，保留中期结构和筹码质量判断
        if p1_profile != "strict":
            if p1_mv_bar <= circ_mv_yi < 500.0 and bias20 >= 22.0:
                return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": f"乖离率极危 (BIAS20: {bias20:.1f}% >= 22%)", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}
            if circ_mv_yi >= 500.0 and bias20 >= 22.0:
                return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": f"乖离率极危 (超级中军 BIAS20: {bias20:.1f}% >= 22%)", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        if cost_50th > 0 and now_price > cost_50th * 1.40:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": f"偏离主峰过远 (>40%)", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        ma20 = _safe_float(curr.get('ma20', 0.0))
        ma60 = _safe_float(curr.get('ma60', 0.0))
        ma120 = _safe_float(curr.get('ma120', 0.0))
        ma20_slope_5 = _safe_float(curr.get('ma20_slope_5', 0.0))
        close_today = _safe_float(curr.get('close', now_price_initial))
        close_yesterday = _safe_float(df.iloc[-2].get('close', close_today)) if len(df) >= 2 else close_today
        ma20_today = ma20 if ma20 > 0 else _safe_float(curr.get('ma20', close_today))
        ma20_yesterday = _safe_float(df.iloc[-2].get('ma20', ma20_today)) if len(df) >= 2 else ma20_today
        below_ma20_2d = (ma20_today > 0) and (close_today < ma20_today) and (close_yesterday < ma20_yesterday)
        bias20_cool = _safe_float(curr.get('bias_20', (close_today - ma20_today) / max(ma20_today, 1e-9) * 100.0))
        high_cooldown = (15.0 <= bias20_cool <= 25.0) and (avg_trn <= 2.2)

        comp_tier, comp_note = _compute_p1_compensation_tier(
            curr, df, rt, now_price_initial,
            net_inflow_3d, net_inflow_5d_early,
            vr, ind_rank, dynamic_inflow_threshold, is_amnesty,
            ma20, ma60, ma120, ma20_slope_5,
            circ_mv_yi=circ_mv_yi_early,  # 【V26.88+10-3 P1-⑤】用于市值占比型 Tier 2 判定
        )

        # V26.88+9-22 新增：涨停板溢价信号（HiThink 涨停池）
        _limit_up = item.get("_pre_limit_up") or {}
        if _limit_up:
            try:
                _continue_cnt = int(_limit_up.get("continue_day_cnt", 1) or 1)
            except (TypeError, ValueError):
                _continue_cnt = 1
            try:
                _seal_money_yi = float(_limit_up.get("seal_money", 0) or 0) / 1e8  # 元 → 亿
            except (TypeError, ValueError):
                _seal_money_yi = 0.0
            if _continue_cnt >= 3 and _seal_money_yi >= 1.0:
                comp_tier = min(comp_tier + 1, 2)
                comp_note = (comp_note + " | 涨停龙头溢价(连板{})".format(_continue_cnt)) if comp_note else "涨停龙头溢价(连板{})".format(_continue_cnt)
            elif _continue_cnt >= 2 or _seal_money_yi >= 5.0:
                comp_tier = min(comp_tier + 1, 2)
                seal_label = "封单{:.1f}亿".format(_seal_money_yi) if _seal_money_yi > 0 else "连板{}".format(_continue_cnt)
                comp_note = (comp_note + " | 涨停强势({})".format(seal_label)) if comp_note else "涨停强势({})".format(seal_label)
            used_fast_track = True

        # === 【V26.88+10-8 新增】倍量阳线机构信号：直接入 P1 顶部 ===
        # 用户硬要求：倍量(今日量≥昨日量×3) + 真阳线(close>open) + 流通市值≥120亿
        # → 100 分插队 P1 顶部，但仍走 P1 既有闸门（左侧否决/业绩/黑名单/PE 极值等）
        # 复用现成快车道 used_fast_track 通道；上层 pool_builder.py 会把
        # 标注"倍量阳线"的快车道分抬到 100（涨停快车道仍 85，互不干扰）。
        # 倍数硬约束 3：用户明确"VOL >= REF(VOL,1) * 3"，不接受 2 倍（与 vol_ratio 的 2.0 不同口径）。
        try:
            _vs_close = _safe_float(curr.get('close', close_today))
            _vs_open  = _safe_float(curr.get('open', 0.0))
            _vs_vol_t = _safe_float(curr.get('vol', 0.0))
            _vs_vol_y = _safe_float(df.iloc[-2].get('vol', 0.0)) if len(df) >= 2 else 0.0
            _vs_vr    = _safe_float(curr.get('vol_ratio', 0.0))
            _vs_circ  = float(circ_mv_yi_early) if circ_mv_yi_early is not None else 0.0
            if (
                _vs_open > 0 and _vs_close > _vs_open          # 真阳线（K 线）
                and _vs_vol_t > 0 and _vs_vol_y > 0
                and _vs_vol_t >= _vs_vol_y * 3.0                # 倍量（今日量≥昨日×3，用户硬要求）
                and _vs_vr >= 2.0                                # 量比同步确认（冗余保险，不同口径不调 3）
                and _vs_circ >= 120.0                            # 流通市值≥120亿
            ):
                used_fast_track = True
                score_details["倍量阳线机构信号"] = (
                    f"今日量{_vs_vol_t:.0f}≥昨日{_vs_vol_y:.0f}×3；"
                    f"阳线{_vs_close:.2f}>{_vs_open:.2f}；"
                    f"量比{_vs_vr:.2f}；流通市值{_vs_circ:.0f}亿"
                )
        except Exception as _e_vs:
            logger.debug("[p1-vol-sun-signal] 触发判定异常(忽略): %s", _e_vs)

        # 高空冷却与深海防线
        if _p1_is_two_day_below_ma20(close_today, ma20_today, close_yesterday, ma20_yesterday):
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": "深海防线: 连续2日收盘低于MA20", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}
        if _p1_is_high_cooldown(bias20_cool, avg_trn) and not relaxed_bear_bounce:
            comp_tier = min(comp_tier, 0)
            comp_note = comp_note or "高位冷却压制"
        # 右侧地基：现价须站上 MA60；退潮左侧底仓豁免
        if ma60 > 0 and now_price < ma60 * 0.995 and not relaxed_bear_bounce:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": "右侧地基: 现价未站上MA60", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        # 筹码上沿重压
        wr_p1 = _safe_float(rt.get("winner_rate"), _safe_float(curr.get("winner_rate"), 0.0))
        chip_single_peak_dense = _p1_is_single_peak_dense(curr, rt, now_price)
        if _safe_float(curr.get("cost_95th"), _safe_float(rt.get("cost_95th"), 0.0)) > 0 and not chip_single_peak_dense:
            c95_p1 = _safe_float(curr.get("cost_95th"), _safe_float(rt.get("cost_95th"), 0.0))
            if now_price < c95_p1 * 0.995 and wr_p1 < 72.0 and not is_strong_attack:
                return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": "筹码重压: 未突破成本上沿且获利盘<72%", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        is_trend_pass = False
        trend_ma120_min_ratio = float(thresholds.get("trend_ma120_min_ratio", P1_TREND_MA120_MIN_RATIO))
        trend_slope_fastpass = float(thresholds.get("trend_slope_fastpass", P1_TREND_SLOPE_FASTPASS))
        near_ma20_min_ratio = float(thresholds.get("near_ma20_min_ratio", P1_NEAR_MA20_MIN_RATIO))
        macd_bar_kill = float(thresholds.get("macd_bar_kill", P1_MACD_BAR_KILL))
        # [V31-B1] MACD pct thresholds (relative to price), 3 tiers by compensation tier
        _macd_pct_list = thresholds.get("macd_bar_kill_pct")
        macd_bar_kill_pct = None
        if isinstance(_macd_pct_list, (list, tuple)) and len(_macd_pct_list) >= 3:
            try:
                macd_bar_kill_pct = [float(v) for v in _macd_pct_list[:3]]
            except (TypeError, ValueError):
                macd_bar_kill_pct = None
        vol_divergence_ratio = float(thresholds.get("vol_divergence_ratio", P1_VOL_DIVERGENCE_RATIO))
        pass_line = float(thresholds.get("pass_line", P1_PASS_LINE))

        _t120_eff, macd_kill_eff, vol_div_eff, near_ma20_eff = _effective_p1_thresholds(
            comp_tier,
            trend_ma120_min_ratio,
            macd_bar_kill,
            vol_divergence_ratio,
            near_ma20_min_ratio,
        )

        if relaxed_bear_bounce:
            is_trend_pass = True
            am_str = "退潮左侧底仓特赦(超跌 bias60<-15% 且近3日主力净流入>0，豁免MA20>MA60)"
            amnesty_reason = amnesty_reason + " | " + am_str if amnesty_reason else am_str
        elif ma20 > ma60 and ma20_slope_5 >= trend_slope_fastpass:
            is_trend_pass = True
        elif ma20 > ma60:
            # 【V30.10 统一 MACD 口径 2026-10-21】原取 macd_bar（2× 口径），
            # 现统一取 1× 归一口径（与 daily_data.macd_hist、标定脚本一致）。
            macd_bar_curr = _macd_hist_1x(curr)
            macd_bar_prev = _macd_hist_1x(df.iloc[-2]) if len(df) >= 2 else 0.0
            if net_inflow_5d_early > 0 or (macd_bar_curr > 0 and macd_bar_curr > macd_bar_prev):
                is_trend_pass = True
                am_str = "早龙释放(MA20>MA60且资金/动能支持)"
                amnesty_reason = amnesty_reason + " | " + am_str if amnesty_reason else am_str
            else:
                is_trend_pass = True

        if not is_trend_pass:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": "均线未呈多头且不满足早龙特征", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        tail_3 = df.tail(3)
        if not relaxed_short_ma and 'ma20' in tail_3.columns:
            days_above_ma20 = sum(tail_3['close'] >= tail_3['ma20'])
            latest_close = _safe_float(curr.get('close', now_price))
            latest_ma20 = _safe_float(curr.get('ma20', ma20))
            latest_ma20_safe = max(latest_ma20, 1e-9)
            near_ma20 = latest_ma20 > 0 and latest_close >= latest_ma20_safe * near_ma20_eff
            has_repair_inflow = (net_inflow_3d > 0) and (net_inflow_5d_early > 0)
            if (
                not relaxed_bear_bounce
                and days_above_ma20 == 0
                and not near_ma20
                and not has_repair_inflow
            ):
                return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": "破位洗盘过深", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        # 【V30.10 统一 MACD 口径 2026-10-21】
        # 标定口径（tools/probe_macd_bar_kill_scale.py）用的是 daily_data.macd_hist（【1× 口径】），
        # 而此处原代码取 macd_bar 优先（【2× 口径】(diff-dea)*2，indicator_calc 口径）、
        # macd_hist 兜底 —— 【标定口径与执行口径不一致】。
        # 目前只是因为生产 daily_data 恰好没有 macd_bar 列才"侥幸正确"，
        # 一旦数据源带上 macd_bar，百分比阈值会整整差一倍 → 判定全面偏移。
        # 现统一取 1× 归一口径，与标定脚本、与 daily_data 完全一致。
        macd_bar = _macd_hist_1x(curr)
        _macd_close = _safe_float(curr.get("close", now_price))
        macd_bar_pct = (macd_bar / _macd_close * 100.0) if _macd_close > 0 else None
        macd_kill_pct_eff = macd_bar_kill_pct[comp_tier] if macd_bar_kill_pct else None
        _old_macd_kill = bool(macd_bar <= macd_kill_eff)
        if macd_kill_pct_eff is not None and macd_bar_pct is not None:
            macd_killed = bool(macd_bar_pct <= macd_kill_pct_eff)
            macd_kill_desc = (
                f"柱{macd_bar:.4f}≈{macd_bar_pct:.2f}%股价≤底线{macd_kill_pct_eff:.2f}%"
                f",容错档{comp_tier}"
            )
        else:
            macd_killed = _old_macd_kill
            macd_kill_desc = f"柱{macd_bar:.4f}≤放宽底线{macd_kill_eff:.4f},容错档{comp_tier}"
        if (not relaxed_short_ma) and macd_killed and not is_strong_attack:
            return {
                "ts_code": ts_code,
                "score": 0.0,
                "is_pass": False,
                "reason": f"MACD过弱({macd_kill_desc})",
                "item": item,
                "df": df,
                "inflow_ratio": inflow_ratio,
                "inflow_10d_ratio": inflow_10d_ratio,
                "score_details": {},
                "used_fast_track": used_fast_track,
            }

        # [V31-B1] shadow diff log: new vs old MACD gate disagree -> audit only, never auto-apply
        if _old_macd_kill != macd_killed:
            _record_macd_gate_shadow_diff({
                "ts_code": ts_code,
                "profile": p1_profile,
                "tier": comp_tier,
                "close": round(_macd_close, 4),
                "macd_bar": round(macd_bar, 6),
                "macd_bar_pct": round(macd_bar_pct, 4) if macd_bar_pct is not None else None,
                "old_kill": _old_macd_kill,
                "new_kill": macd_killed,
            })

        # 量价背离（近20日）
        df_20 = df.tail(20).copy()
        _vc_div = "vol" if "vol" in df_20.columns else ("volume" if "volume" in df_20.columns else None)
        if _vc_div is None:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": "量价背离检查缺少成交量列", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}
        up_days = df_20[(df_20["close"] > df_20["open"]) | (df_20["pct_chg"] > 0)]
        down_days = df_20[(df_20["close"] < df_20["open"]) | (df_20["pct_chg"] < 0)]
        avg_up_vol = up_days[_vc_div].mean() if not up_days.empty else 0
        avg_down_vol = down_days[_vc_div].mean() if not down_days.empty else 1
        avg_up_vol = float(np.nan_to_num(_safe_float(avg_up_vol), nan=0.0, posinf=0.0, neginf=0.0))
        avg_down_vol = float(np.nan_to_num(_safe_float(avg_down_vol), nan=0.0, posinf=0.0, neginf=0.0))
        avg_down_ref = avg_down_vol if avg_down_vol > 1e-9 else 1.0

        if avg_up_vol < avg_down_ref * vol_div_eff and not is_strong_attack:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": "量价背离", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        # 深幅回撤（60日）
        df_60 = df.tail(60).copy()
        _vc60 = "vol" if "vol" in df_60.columns else ("volume" if "volume" in df_60.columns else None)
        if _vc60 is None:
            return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": "回撤检查缺少成交量列", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}
        max_high_60 = _safe_float(df_60['high'].max())
        idx_max = df_60['high'].idxmax()
        min_after_max = _safe_float(df.loc[idx_max:]['low'].min() if idx_max in df.index else df_60['low'].min())
        max_drawdown_60d = (max_high_60 - min_after_max) / max_high_60 * 100.0 if max_high_60 > 0 else 0

        if max_drawdown_60d >= 20.0:
            lowest_60d = _safe_float(df_60['low'].min())
            mid_point = (max_high_60 + lowest_60d) / 2.0

            df_10 = df.tail(10)
            down_10 = df_10[(df_10['close'] < df_10['open']) | (df_10['pct_chg'] < 0)]
            up_10 = df_10[(df_10['close'] > df_10['open']) | (df_10['pct_chg'] > 0)]
            avg_down_vol_10 = down_10[_vc60].mean() if not down_10.empty else 0
            avg_up_vol_10 = up_10[_vc60].mean() if not up_10.empty else 1

            _up10 = max(float(avg_up_vol_10), 1e-9)
            is_drawdown_amnesty = (now_price >= mid_point) and (avg_down_vol_10 <= _up10 * 1.5)

            if is_drawdown_amnesty:
                am_str = f"回撤修复特赦(最大回撤{max_drawdown_60d:.1f}%,已收复中轴)"
                amnesty_reason = amnesty_reason + " | " + am_str if amnesty_reason else am_str
            else:
                return {"ts_code": ts_code, "score": 0.0, "is_pass": False, "reason": f"回撤超标 ({max_drawdown_60d:.1f}% >= 20%) 且未修复", "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": {}, "used_fast_track": used_fast_track}

        score, is_pass, reason, score_details = _evaluate_p1_single_stock(
            df, rt, circ_mv_yi, ind_rank, pe, ind_stats, ind, dynamic_industries, avg_trn, pass_line
        )

        # 【V30.14 2026-11-05】防一日游：先判形态，再谈加分。
        # 放在打分之后是因为它要读当日 K 线形态（开高低收/量能/3日5日累计涨幅），
        # 但**否决优先于加分**：一日游形态的票不该因为市值大而被抬进池。
        try:
            from core.config_manager import get_p1_one_day_trip_config

            _odt_cfg = get_p1_one_day_trip_config()
        except Exception:
            _odt_cfg = {}
        _odt = _p1_one_day_trip_check(
            df,
            curr,
            pct_chg_today=_safe_float(rt.get("pct_chg", curr.get("pct_chg", 0.0))),
            cfg=_odt_cfg,
        )
        if _odt.get("veto"):
            return {
                "ts_code": ts_code,
                "score": 0.0,
                "is_pass": False,
                "reason": str(_odt["veto"]),
                "item": item,
                "df": df,
                "inflow_ratio": inflow_ratio,
                "inflow_10d_ratio": inflow_10d_ratio,
                "score_details": {"一日游拦截": str(_odt["veto"])},
                "used_fast_track": used_fast_track,
            }
        _odt_penalty = float(_odt.get("penalty", 0.0) or 0.0)
        if _odt_penalty > 0:
            score = max(0.0, float(score) - _odt_penalty)
            score_details["防一日游降分"] = (
                f"{'、'.join(_odt.get('labels') or []) or '形态嫌疑'} → 扣{_odt_penalty:.1f}分"
            )
            reason = (reason + " | " if reason else "") + "一日游嫌疑降分"
            if float(score) < float(pass_line):
                is_pass = False

        # 【V30.14 2026-11-05】千亿蓝筹加分：奖励「大票该有的确定性」，
        # 上限 5 分，只在形态过关后生效（一日游已在上面否决/降分）。
        _lc_bonus = float(_lc_info.get("bonus", 0.0) or 0.0)
        if _lc_bonus > 0:
            score = float(np.clip(float(score) + _lc_bonus, 0.0, 100.0))
            score_details["千亿蓝筹加分"] = (
                f"{'、'.join(_lc_info.get('labels') or [])} → +{_lc_bonus:.1f}分"
            )
            if (not is_pass) and float(score) >= float(pass_line) and not _odt_penalty:
                # 加分后重新过线：允许入池，但保留原始死因说明，便于复盘
                is_pass = True
                reason = (reason + " | " if reason else "") + "千亿蓝筹加分后过线"

        # 渣男股性分级惩罚
        from core.config_manager import get_p1_stock_behavior_penalty_config

        bh_cfg = get_p1_stock_behavior_penalty_config()
        long_upper_shadow_hits, intraday_dump_hits = _p1_behavior_penalty_hits(df, int(bh_cfg.get("lookback_days", 20)))

        if bool(bh_cfg.get("enabled", True)) and is_pass and (
            long_upper_shadow_hits >= int(bh_cfg.get("long_upper_shadow_hits_min", 3))
            or intraday_dump_hits >= int(bh_cfg.get("intraday_dump_hits_min", 3))
        ):
            long_hits = int(long_upper_shadow_hits or 0)
            dump_hits = int(intraday_dump_hits or 0)
            weighted_hits = long_hits * 1 + dump_hits * 2
            if weighted_hits >= 8:
                penalty = float(bh_cfg.get("score_penalty_severe", 6.0))
                penalty_tier = "重度"
            elif weighted_hits >= 5:
                penalty = float(bh_cfg.get("score_penalty_moderate", 3.0))
                penalty_tier = "中度"
            else:
                penalty = float(bh_cfg.get("score_penalty_light", 1.5))
                penalty_tier = "轻度"
            score = max(0.0, score - penalty)
            score_details["渣男股性惩罚"] = (
                f"近{int(bh_cfg.get('lookback_days', 20))}日"
                f"长上影{long_hits}次/冲高回落{dump_hits}次"
                f"→ {penalty_tier}渣男(扣{penalty:.1f}分)"
            )
            reason = (reason + " | " if reason else "") + f"渣男股性命中({penalty_tier})"
            if score < float(pass_line):
                is_pass = False
                reason = f"渣男股性扣分后未达标({penalty_tier})"

        if is_pass and comp_tier > 0 and comp_note:
            _cn = f"P1容错补偿(档{comp_tier})：{comp_note}"
            amnesty_reason = amnesty_reason + " | " + _cn if amnesty_reason else _cn

        if amnesty_reason and is_pass:
            reason = amnesty_reason + " | " + reason

        if high_cooldown and is_pass:
            score = min(score, 55.0)
            score_details["高位冷却压制"] = "55分封顶"
        score_details["市值档位"] = f"{size_emoji}{mv_tier}"

        # 大盘温度调节（V26.88+9-23 / V26.88+9-24 P2）
        try:
            _overheat = item.get("_pre_market_overheat") or {}
            _layer_scores = _overheat.get("layer_scores") or {}
            if circ_mv_yi >= 500.0:
                _layer_name = "large"
            elif circ_mv_yi >= 100.0:
                _layer_name = "mid"
            else:
                _layer_name = "growth"
            _oh_score = int(_layer_scores.get(_layer_name, _overheat.get("market_overheat_score", 0)))
            if _oh_score != 0 and is_pass:
                _delta = float(_oh_score) * 0.67  # [-2.0, +2.0]
                score = float(np.clip(score + _delta, 0.0, 100.0))
                _oh_label = {-3: "大盘过热", -2: "大盘偏热", -1: "大盘微热",
                             1: "大盘微冷", 2: "大盘偏冷", 3: "大盘超冷"}.get(_oh_score, "")
                score_details["大盘温度调节"] = (
                    f"[{_layer_name}层]{_oh_label}({_oh_score:+d})  {_delta:+.1f}分"
                )
                if _oh_score <= -2 and is_pass and score >= float(pass_line):
                    score = max(score - 1.0, 0.0)
                if _oh_score >= 2 and is_pass:
                    score = min(score + 0.5, 100.0)
            _regime = _overheat.get("regime")
            if _regime and _regime in ("bear_extreme", "bull_extreme"):
                _regime_label = {
                    "bear_extreme": "⚠️大盘极寒(系统性机会)",
                    "bull_extreme": "⚠️大盘狂热(系统性风险)",
                }.get(_regime, "")
                if _regime_label and _regime_label not in (reason or ""):
                    reason = (reason + " | " if reason else "") + _regime_label
        except Exception as _e_oh:
            logger.debug("[overheat-adjust] %s", _e_oh)

        # V26.80 A 股特色增强层（10 大维度）
        try:
            from core.a_share_integration import apply_a_share_enhancement

            _industry_for_a = industry_map.get(ts_code, "") if isinstance(industry_map, dict) else ""
            _a_share_result = apply_a_share_enhancement(
                ts_code=str(ts_code),
                df=df,
                rt=rt,
                sector_history_df=None,
                hk_holding_df=None,
                margin_history=None,
                industry=_industry_for_a,
            )
            if _a_share_result.get("enabled"):
                _w_boost = float(_a_share_result.get("weighted_enhancement", 0.0))
                if abs(_w_boost) > 1e-6:
                    score = float(np.clip(score + _w_boost, 0.0, 100.0))
                    score_details["A股权重加成"] = round(_w_boost, 2)
                    _a_tags = _a_share_result.get("tags") or []
                    if _a_tags:
                        score_details["A股特色标签"] = "、".join(_a_tags[:5])
        except Exception as _e_a_share:
            logger.debug("[a-share-enhance] %s", _e_a_share)

        # P1-A 动量/反转双轨打分
        try:
            from core.p1_momentum_reversal_classifier import (
                classify_momentum_vs_reversal as _cls_mr,
                compute_momentum_track_score as _sc_mom,
                compute_reversal_track_score as _sc_rev,
            )
            _mr_row = {
                "ma5": _safe_float(df["ma5"].iloc[-1]) if "ma5" in df.columns else 0.0,
                "ma10": _safe_float(df["ma10"].iloc[-1]) if "ma10" in df.columns else 0.0,
                "ma20": _safe_float(df["ma20"].iloc[-1]) if "ma20" in df.columns else 0.0,
                "ma60": _safe_float(df["ma60"].iloc[-1]) if "ma60" in df.columns else 0.0,
                "pct_chg_5d": _safe_float(df["pct_chg"].tail(5).sum()) if "pct_chg" in df.columns else 0.0,
                "pct_chg_20d": _safe_float(df["pct_chg"].tail(20).sum()) if "pct_chg" in df.columns else 0.0,
                "vol_ratio": vr,
                "bias_20": _safe_float(df["bias_20"].iloc[-1]) if "bias_20" in df.columns else 0.0,
                "vol_ratio_3d_slope": 0.0,
                "ma20_slope_5": _safe_float(df["ma20_slope_5"].iloc[-1]) if "ma20_slope_5" in df.columns else 0.0,
                "ma20_slope_20": _safe_float(df["ma20_slope_5"].tail(20).mean()) if "ma20_slope_5" in df.columns else 0.0,
                "ma5_ma60_dist_pct": 0.0,
                "pct_chg": _safe_float(rt.get("pct_chg", df["pct_chg"].iloc[-1] if "pct_chg" in df.columns else 0.0)),
                "max_drawdown_3d": _safe_float(df["close"].tail(3).min()) / max(_safe_float(df["close"].tail(3).max()), 1e-9) * 100.0 - 100.0 if "close" in df.columns and len(df) >= 3 else 0.0,
                "winner_rate": _safe_float(rt.get("winner_rate", df["winner_rate"].iloc[-1] if "winner_rate" in df.columns else 0.0)),
                "cyq_concentration": _safe_float(rt.get("cyq_concentration", df["cyq_concentration"].iloc[-1] if "cyq_concentration" in df.columns else 0.0)),
                "net_main_3d_sum": _safe_float(df["net_main_amount"].tail(3).sum()) if "net_main_amount" in df.columns else 0.0,
                # 【V26.88+10-3 P1-⑥】传递 df 给反转识别器，用于刚启动形态识别（5 日下影线、5 日最低点、3 日量能）
                "_df": df,
            }
            if "ma5" in df.columns and "ma60" in df.columns and _safe_float(df["ma60"].iloc[-1]) > 0:
                _mr_row["ma5_ma60_dist_pct"] = (_mr_row["ma5"] / _mr_row["ma60"] - 1.0) * 100.0
            _track = _cls_mr(_mr_row)
            _mom = _sc_mom(score, _mr_row)
            _rev = _sc_rev(score, _mr_row)
            _best = max(_mom, _rev)
            if _best > score:
                score = float(np.clip(_best, 0.0, 100.0))
            score_details["P1动量/反转分类"] = _track
            score_details["动量线分"] = round(_mom, 2)
            score_details["反转线分"] = round(_rev, 2)
        except Exception as _e_mr:
            logger.debug("[mr-classifier] %s", _e_mr)

        # P1-C 机构加速度
        try:
            from core.p1_inst_velocity import compute_inst_velocity_bonus as _ivb
            _today_inst = _safe_float(rt.get("inst_net_buy", 0.0))
            _avg5_inst = _safe_float(df["inst_net_buy"].tail(5).mean()) if "inst_net_buy" in df.columns else 0.0
            _denom = max(abs(_avg5_inst), 1e4)
            _iv = _today_inst / _denom
            _bias20 = _safe_float(df["bias_20"].iloc[-1]) if "bias_20" in df.columns else 0.0
            _bonus, _label = _ivb(_iv, _bias20)
            score = float(np.clip(score + _bonus, 0.0, 100.0))
            score_details["机构资金加速度"] = _label
        except Exception as _e_iv:
            logger.debug("[inst-velocity] %s", _e_iv)

        # P1-D 业绩预告-预期剪刀差
        try:
            from core.p1_forecast_consensus import batch_compute_forecast_consensus_score as _bfc
            _fc = _bfc([{"code": ts_code, "hist": rt if isinstance(rt, dict) else {}, "rt": rt if isinstance(rt, dict) else {}, "df": df}])
            _fc_score = float(_fc.get(ts_code, {}).get("forecast_consensus_score", 0.0))
            if _fc_score > 0.0:
                score = float(np.clip(score + _fc_score, 0.0, 100.0))
            score_details["业绩预告-预期剪刀差"] = round(_fc_score, 2)
        except Exception as _e_fc:
            logger.debug("[forecast-consensus] %s", _e_fc)

        # A1 跨池接力
        try:
            from core.p1_pool_relay import compute_pool_relay_score as _relay
            try:
                from data.db_core import get_read_conn_singleton as _get_conn
                _db_conn = _get_conn()
            except Exception:
                _db_conn = None
            _bonus, _hits, _label = _relay(_db_conn, ts_code)
            if _bonus > 0.0:
                score = float(np.clip(score + _bonus, 0.0, 100.0))
            score_details["跨池接力"] = _label if _label else "无接力"
        except Exception as _e_relay:
            logger.debug("[pool-relay] %s", _e_relay)

        # P1-B 涨停炸板拦截
        try:
            _lb = bool(item.get("_p1_limit_break_yesterday", False))
            if _lb:
                score_details["涨停炸板拦截"] = "昨日炸板 已降权 30"
                score = float(np.clip(score - 30.0, 0.0, 100.0))
                # 【P1-修复】原代码 is_pass_strict 算出来后只在两个分支里 pass，
                # 完全没有联动 is_pass —— 炸板票被扣 30 分后仍然以 is_pass=True 返回，
                # 照样进入主池。这里显式落败并标记降档到观察池。
                if score < float(pass_line):
                    is_pass = False
                    reason = (reason + " | " if reason else "") + "昨日炸板降权后未达标"
                if not item.get("_p1_drop_to_observation"):
                    item["_p1_drop_to_observation"] = bool(
                        item.get("_p1_drop_to_observation", False) or _lb
                    )
        except Exception:
            pass

        # V28 A 题材生命周期加分
        try:
            from core.theme_lifecycle import get_industry_lifecycle
            _db_conn_lc = globals().get("_GLOBAL_DB_CONN")
            _ind = str(industry_map.get(ts_code, "") if isinstance(industry_map, dict) else "")
            if _ind and _ind not in ("未知", "", "None"):
                _lc = get_industry_lifecycle(_db_conn_lc, _ind, lookback_days=30)
                _lc_bonus = float(_lc.get("score_bonus", 0.0))
                score = float(np.clip(score + _lc_bonus, 0.0, 100.0))
                score_details["题材生命周期"] = f"{_lc.get('label','未知')} ({_ind})"
            else:
                score_details["题材生命周期"] = "无行业"
        except Exception as _e_lc:
            logger.debug("[lifecycle] %s", _e_lc)

        # V28 C 龙虎榜席位结构加分
        try:
            from core.dragon_tiger_seat import compute_dragon_tiger_with_fallback
            _db_conn_dt = globals().get("_GLOBAL_DB_CONN")
            _trade_date = str(rt.get("trade_date") or "") if isinstance(rt, dict) else ""
            _dt_bonus, _dt_label, _dt_detail = compute_dragon_tiger_with_fallback(
                rt if isinstance(rt, dict) else {},
                db_conn=_db_conn_dt,
                trade_date=_trade_date,
            )
            if _dt_bonus != 0.0 and _dt_label:
                score = float(np.clip(score + _dt_bonus, 0.0, 100.0))
                score_details["龙虎榜席位"] = _dt_label
        except Exception as _e_dt:
            logger.debug("[dragon-tiger] %s", _e_dt)

        # V28-1 exit_strategy 4 档出场模板注入
        try:
            res_local = {
                "pool": "p1",
                "burst_score": float(score),
                "score": float(score),
                "strategies": item.get("strategies") or [],
                "score_details": score_details,
                "ts_code": ts_code,
                "price": float(_safe_float(item.get("hist", item.get("rt", {})).get("price", 0.0))),
            }
            from core.exit_strategy import attach_exit_plan_to_res
            res_local = attach_exit_plan_to_res(
                res_local,
                push_price=res_local["price"] if res_local["price"] > 0 else 0.0,
            )
            if isinstance(res_local.get("exit_plan"), dict):
                score_details["exit_plan"] = res_local["exit_plan"]
                score_details["exit_template"] = res_local.get("exit_template", "medium")
        except Exception as _e_ep:
            logger.debug("[exit-plan] %s", _e_ep)

        # 【P1-修复】最终分数闸：is_pass 只在 _evaluate_p1_single_stock 里按当时分数定过一次，
        # 其后还叠加了机构加速度 / 业绩预告 / 跨池接力 / 炸板降权 / 题材生命周期 / 龙虎榜 /
        # 大盘温度 等十余项加减分，但返回的 is_pass 从未复核。结果是：某票原本过线，
        # 后续被炸板 -30 或高位冷却压到 pass_line 以下，仍以 is_pass=True 进入主池。
        # 这里按最终分数做一次兜底复核，保证「进池票一定 >= pass_line」。
        if is_pass and float(score) < float(pass_line):
            is_pass = False
            reason = (reason + " | " if reason else "") + f"终分{float(score):.1f}低于准入线{float(pass_line):.1f}"
            score_details["终分复核"] = f"✂ {float(score):.1f} < 准入线 {float(pass_line):.1f} → 淘汰"
        elif is_pass:
            score_details["终分复核"] = f"✓ {float(score):.1f} ≥ 准入线 {float(pass_line):.1f}"

        return {"ts_code": ts_code, "score": score, "is_pass": is_pass, "reason": reason, "item": item, "df": df, "inflow_ratio": inflow_ratio, "inflow_10d_ratio": inflow_10d_ratio, "score_details": score_details, "used_fast_track": used_fast_track}

    except Exception as e:
        return {"ts_code": item.get('code', 'Unknown'), "score": 0.0, "is_pass": False, "reason": "安检致命异常: 数据残缺", "item": item, "df": pd.DataFrame(), "inflow_ratio": 0.0, "inflow_10d_ratio": 0.0, "score_details": {}, "used_fast_track": False}


__all__ = [
    "_evaluate_p1_single_stock",
    "_process_single_stock_for_p1",
]
