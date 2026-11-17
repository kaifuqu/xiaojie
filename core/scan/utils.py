# -*- coding: utf-8 -*-
"""
core/scan/utils.py
原 core/scan_engine.py L1-L530（V26.88 拆分）

【职责】
- 全局常量：BJ_TZ / PROJECT_ROOT / DANGER_BUY_VWAP_MIN_DAY_PCT / DEFAULT_RISK_CONFIG 等
- 项目根目录与运行时数据路径：get_project_root / ensure_runtime_data_layout / path_*_json
- 模块级 import 容错（带 fallback）：
  - compute_p1_multi_dim_smooth_score（来自 score_calibration）
  - strict_golden_burst_ok（来自 strat_base）
  - precompute_indicators / merge_daily_with_realtime（来自 indicator_calc）
  - percentile_ranks / compress_surge_bonus / dampen_burst_by_extremes / burst_soft_cap / build_rank_lookup / neutral_ranks / personality_liquidity_blend
  - effective_turnover_rate_f / infer_turnover_rate_f_pct / series_effective_turnover_f_daily / compute_market_contraction_context / adaptive_relaxed_golden_gate_ok
- 实用工具：_safe_float / _a_share_elapsed_minutes / _safe_notify_system_alert
- 数据预处理：_sanitize_rt_vol_ratio / _intraday_ma5_vol_baseline_hand / _extract_scan_surface_metrics
- 扫描前自愈：_try_auto_sync_recent_days_for_scan / _expected_latest_daily_anchor_for_intraday / _prev_calendar_bizday_yyyymmdd
- 战法引擎实例化（DummyEngine + P2/P3/P4/P5 引擎）：p2_engine / p3_engine / p4_engine / p5_engine / golden_engine
- 金共振入口：_golden_evaluate
- 结果路由：_route_scored_pool_row
- P1 初筛最小流通市值：_p1_scan_min_circ_mv_yi

【公开 API】（25 个）
- get_realtime_sector_ranking（实际在 _detectors_market.py，此处仅 re-export）
- precompute_indicators / merge_daily_with_realtime（占位）
- compute_p1_multi_dim_smooth_score（占位）
- percentile_ranks / compress_surge_bonus / dampen_burst_by_extremes / burst_soft_cap / build_rank_lookup / neutral_ranks / personality_liquidity_blend（占位）
- effective_turnover_rate_f / infer_turnover_rate_f_pct / series_effective_turnover_f_daily / compute_market_contraction_context / adaptive_relaxed_golden_gate_ok（占位）
- get_project_root / path_blacklist_json / path_intraday_snapshots_json / path_wash_metrics_json / ensure_runtime_data_layout
- _golden_evaluate / golden_engine

【跨文件依赖】（顶层模块级 import 解决）
- core.strategies.score_calibration
- core.strategies.strat_base
- core.strategies.strat_p2_auction / p3_intraday / p4_tail / p5_postmarket
- core.strategies.risk_control_engine (evaluate_3layer_risk, DEFAULT_RISK_CONFIG)
- core.strategies.fund_mv_utils
- core.indicator_calc
- core.danger_signal_utils (would_trigger_danger_sell)
- core.stock_name_utils (normalize_stock_display_name)
- core.runtime_data_paths
- core.config_manager (get_strategy_min_circ_mv_wan)
- core.p5_morning_validation (sse_prev_open_trade_date_before)
- data.db_core
- data.api_fetcher (fetch_realtime_batch)
- data.data_fetcher（运行时 lazy import, sync_recent_days）
"""
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import constants

logging.basicConfig(level=logging.INFO, format='%(asctime)s [扫描引擎] %(message)s')


# ==================== 模块级容错 import（战法引擎 + 算法） ====================
def _p1_scan_min_circ_mv_yi() -> float:
    """【V26.88+ 9-26 双闸】读 STRATEGY_MIN_CIRC_MV_WAN（流通下限，默认 120 亿）→ constants 兜底。"""
    try:
        from core.config_manager import get_strategy_min_circ_mv_wan
        return float(get_strategy_min_circ_mv_wan()) / 10000.0
    except Exception:
        return float(getattr(constants, "STRATEGY_MIN_CIRC_MV_WAN", 1_200_000)) / 10000.0


def _safe_notify_system_alert(title: str, detail: str, category: str, dedup_key: str) -> None:
    """扫描链统一运维告警入口，避免各处散落重复 import / 重复格式化。"""
    try:
        from core.notification_gateway import notify_wechat_system_alert
        notify_wechat_system_alert(
            title=title, detail=detail, category=category, dedup_key=dedup_key,
        )
    except Exception as e:
        logging.debug("_safe_notify_system_alert 跳过: %s", e)


try:
    from core.strategies.score_calibration import compute_p1_multi_dim_smooth_score
except ImportError:

    def compute_p1_multi_dim_smooth_score(*args, **kwargs):
        return 0.0, False, "平滑得分不达标", {}


try:
    from core.strategies.strat_base import strict_golden_burst_ok
except ImportError as e:
    logging.warning(f"scan_engine 无法导入 strat_base 门禁: {e}")

    def strict_golden_burst_ok(df, rt, pool_key=None):
        return True


try:
    from core.indicator_calc import precompute_indicators, merge_daily_with_realtime
except ImportError as e:
    logging.warning(f"scan_engine 无法导入 indicator_calc: {e}")

    def precompute_indicators(df):
        return df

    def merge_daily_with_realtime(df, rt):
        return df, False


try:
    from core.strategies.score_calibration import (
        percentile_ranks,
        compress_surge_bonus,
        dampen_burst_by_extremes,
        burst_soft_cap,
        build_rank_lookup,
        neutral_ranks,
        personality_liquidity_blend,
    )
except ImportError:

    def percentile_ranks(df, cols):
        out = df.copy()
        for c in cols:
            if c in out.columns:
                out[c + "_r"] = 0.5
        return out

    def compress_surge_bonus(surge, linear_cap=10.0, asymptote=16.0):
        return float(surge or 0.0)

    def dampen_burst_by_extremes(burst, p1_gene, vr_r, pct_r, trf_r, main_r):
        return float(burst or 0.0)

    def burst_soft_cap(burst, soft=94.0, hard=102.0):
        return float(burst or 0.0)

    def build_rank_lookup(cs_df):
        return {}

    def neutral_ranks():
        return {"pct_r": 0.5, "vol_ratio_r": 0.5, "turnover_f_r": 0.5, "main_ratio_r": 0.5}

    def personality_liquidity_blend(trn_multi, vr_rank):
        t = float(np.clip(trn_multi, 0.85, 1.15))
        vr = float(np.clip(vr_rank, 0.0, 1.0))
        v = float(np.interp(vr, [0.05, 0.35, 0.55, 0.75, 0.95], [0.90, 0.97, 1.0, 1.06, 1.12]))
        return float(np.clip(np.sqrt(max(t, 1e-9) * max(v, 1e-9)), 0.85, 1.15))


from core.danger_signal_utils import would_trigger_danger_sell
from core.stock_name_utils import normalize_stock_display_name

try:
    from core.strategies.fund_mv_utils import (
        effective_turnover_rate_f,
        infer_turnover_rate_f_pct,
        series_effective_turnover_f_daily,
        compute_market_contraction_context,
        adaptive_relaxed_golden_gate_ok,
    )
except ImportError:

    def infer_turnover_rate_f_pct(vol_hand, close, circ_mv_wan):
        if vol_hand <= 0 or close <= 0 or circ_mv_wan <= 0:
            return 0.0
        return vol_hand * close / circ_mv_wan

    def effective_turnover_rate_f(rt, y, close_live):
        r = rt or {}
        t = _safe_float(r.get("turnover_rate_f"), 0.0)
        if t > 0:
            return t
        if hasattr(y, "get"):
            t2 = _safe_float(y.get("turnover_rate_f"), 0.0)
            if t2 > 0:
                return t2
        vh = _safe_float(r.get("volume"), 0.0) / 100.0
        if vh <= 0 and hasattr(y, "get"):
            vh = _safe_float(y.get("vol"), 0.0)
        cm = _safe_float(r.get("circ_mv"), 0.0)
        if cm <= 0 and hasattr(y, "get"):
            cm = _safe_float(y.get("circ_mv"), 0.0)
        if cm <= 0 and hasattr(y, "get"):
            cm = _safe_float(y.get("total_mv"), 0.0) * 0.6
        return infer_turnover_rate_f_pct(vh, _safe_float(close_live, 0.0), cm)

    def series_effective_turnover_f_daily(df):
        if df is None or df.empty:
            return pd.Series(dtype=float)
        if "turnover_rate_f" in df.columns:
            return pd.to_numeric(df["turnover_rate_f"], errors="coerce").fillna(0.0)
        return pd.Series(0.0, index=df.index)

    def compute_market_contraction_context(base_items, rt_map=None):
        return {"score": 0.0, "adaptive_reason": "fund_mv_utils 不可用"}

    def adaptive_relaxed_golden_gate_ok(pool_key, df, rt, market_contraction_score):
        return False


# ==================== 0. 全局物理底座寻址雷达 ====================
def get_project_root():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    # core/scan/utils.py -> core/scan/ -> core/ -> workspace
    for _ in range(5):
        if os.path.exists(os.path.join(current_dir, "config.yaml")):
            return current_dir
        current_dir = os.path.dirname(current_dir)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


PROJECT_ROOT = get_project_root()

try:
    from core.runtime_data_paths import (
        ensure_runtime_data_layout,
        path_blacklist_json,
        path_intraday_snapshots_json,
        path_wash_metrics_json,
    )
except ImportError:
    def ensure_runtime_data_layout():
        os.makedirs(os.path.join(PROJECT_ROOT, "data"), exist_ok=True)

    def path_blacklist_json():
        return os.path.join(PROJECT_ROOT, "data", "blacklist.json")

    def path_intraday_snapshots_json():
        return os.path.join(PROJECT_ROOT, "data", "intraday_snapshots.json")

    def path_wash_metrics_json():
        return os.path.join(PROJECT_ROOT, "data", "wash_metrics_history.json")


# ==================== 1. 跨部门资源调配 (任督二脉贯通) ====================
import constants
from data.db_core import (
    save_df_to_sql,
    get_latest_sector_ranking,
    get_stock_industry,
    get_all_basic_industry,
    load_p1_cache,
    get_latest_daily_data_trade_date_yyyymmdd,
)
from data.api_fetcher import fetch_realtime_batch

# [V26.88+10-8 P5 扫描修复] 文档里长期承诺 re-export get_realtime_sector_ranking, 但代码未真正
# import, 导致 pipeline.py:322 的 NameError. 此处补齐 (带 try/except 兜底, 避免循环导入).
try:
    from core.scan.detectors_market import get_realtime_sector_ranking  # noqa: F401
except ImportError:
    def get_realtime_sector_ranking():  # type: ignore[no-redef]
        """兜底: 检测器不可用时返回空 dict, 避免扫描崩."""
        return {}

BJ_TZ = timezone(timedelta(hours=8))


def _prev_calendar_bizday_yyyymmdd(today_yyyymmdd: str) -> str:
    """无交易所日历时的兜底：按工作日回退到上一个自然工作日。"""
    s = str(today_yyyymmdd or "").strip().replace("-", "")[:8]
    if len(s) != 8 or (not s.isdigit()):
        return ""
    try:
        dt = datetime.strptime(s, "%Y%m%d")
    except ValueError:
        return ""
    dt = dt - timedelta(days=1)
    while dt.weekday() >= 5:  # 5=Sat, 6=Sun
        dt = dt - timedelta(days=1)
    return dt.strftime("%Y%m%d")


def _expected_latest_daily_anchor_for_intraday(today_yyyymmdd: str) -> str:
    """盘中/盘后扫描的最低日线新鲜度。"""
    try:
        from core.p5_morning_validation import sse_prev_open_trade_date_before
        prev_td = str(sse_prev_open_trade_date_before(today_yyyymmdd) or "").strip()
        if len(prev_td) == 8 and prev_td.isdigit():
            return prev_td
    except Exception:
        pass
    return _prev_calendar_bizday_yyyymmdd(today_yyyymmdd)


def _normalize_td_yyyymmdd(value) -> str:
    """把 trade_date 的各种写法统一成 8 位 YYYYMMDD 字符串；无法解析返回空串。"""
    if value is None:
        return ""
    s = str(value).strip().replace("-", "").replace("/", "")[:8]
    if len(s) != 8 or not s.isdigit():
        return ""
    try:
        datetime.strptime(s, "%Y%m%d")
    except ValueError:
        return ""
    return s


def _per_stock_daily_stale_reason(df, required_td: str) -> str:
    """逐票日线新鲜度闸：返回空串表示新鲜，返回中文原因表示应淘汰。

    【P0-20261018 V30.5】全局闸只看全库 MAX(trade_date)，无法发现「个别票滞后」。
    实测 2026-10-16 全库最新日 = 20261016，但 daily_data 2134 只票里有 703 只
    最后一行早于该日（如 601003.SH 只到 20260508），这批票一旦进入 P2~P5：
      1) 策略拿昨天的 K 线当今天用；
      2) P5 业务日 business_date 取 _last_row.trade_date，会把信号写到历史日期；
      3) T+N 事后回看与模拟盘建仓日整体错位。
    因此在单票入口加一道闸：落后票不进策略、不写 signal_log，只在漏斗里记账。
    """
    if df is None or not hasattr(df, "empty") or df.empty:
        return "无日线数据"
    req = _normalize_td_yyyymmdd(required_td)
    if len(req) != 8:
        # 拿不到基准日时不误杀，放行给原有逻辑处理
        return ""
    if "trade_date" not in df.columns:
        return "日线缺 trade_date 列"
    last_td = _normalize_td_yyyymmdd(df["trade_date"].iloc[-1])
    if len(last_td) != 8:
        return "日线末行 trade_date 无法解析"
    if last_td < req:
        return f"日线截止日落后({last_td}<{req})"
    return ""


def _try_auto_sync_recent_days_for_scan(progress_callback=None) -> bool:
    """扫描前轻量自愈：尝试执行近期增量同步。"""
    try:
        import data.fetcher as data_fetcher  # V26.88 拆分
    except Exception as e:
        logging.warning("扫描前自动增量同步不可用: %s", e)
        return False
    if getattr(data_fetcher, "pro", None) is None:
        logging.warning("扫描前自动增量同步跳过：Tushare pro 未初始化")
        return False
    try:
        if progress_callback:
            try:
                progress_callback("♻️ 检测到日线数据可能未对齐，正在自动增量同步最近交易日数据…")
            except Exception:
                pass
        sync_ret = data_fetcher.sync_recent_days(
            days=3,
            status_callback=lambda m: logging.info("[scan:auto-sync] %s", str(m)),
        )
        if isinstance(sync_ret, pd.DataFrame):
            return not sync_ret.empty
        if isinstance(sync_ret, (list, tuple, set, dict)):
            return len(sync_ret) > 0
        if sync_ret is None:
            return False
        return bool(sync_ret)
    except Exception as e:
        logging.warning("扫描前自动增量同步失败: %s", e)
        return False


# ==================== 战法引擎实例化（Dummy 占位 + 真实战法）====================
class DummyEngine:
    def run_all(self, df, rt): return {}
    def evaluate(self, df, rt): return {}


try:
    from core.strategies.strat_p2_auction import P2Auction
    p2_engine = P2Auction()
except Exception as e:
    logging.warning(f"P2战法加载失败: {e}")
    p2_engine = DummyEngine()

try:
    from core.strategies.strat_p3_intraday import P3Intraday
    p3_engine = P3Intraday()
except Exception as e:
    logging.warning(f"P3战法加载失败: {e}")
    p3_engine = DummyEngine()

try:
    from core.strategies.strat_p4_tail import P4Tail
    p4_engine = P4Tail()
except Exception as e:
    logging.warning(f"P4战法加载失败: {e}")
    p4_engine = DummyEngine()

# 【主干解耦】P5 盘后真龙池独立引擎，禁止与 P4 共用同一实例
try:
    from core.strategies.strat_p5_postmarket import P5Postmarket
    p5_engine = P5Postmarket()
except Exception as e:
    logging.warning(f"P5战法加载失败: {e}")
    p5_engine = DummyEngine()

try:
    from core.strategies.score_calibration import compute_golden_institutional_bonus

    def _golden_evaluate(df, rt, pool_key, ind_rank=999, net_elg=0.0, cyq=999.0):
        """P3-P5 兼容入口：转发到 compute_golden_institutional_bonus"""
        try:
            return compute_golden_institutional_bonus(
                df=df, rt=rt, pool_key=pool_key, ind_rank=ind_rank,
                net_main_amount=net_elg, cyq=cyq,
            )
        except Exception as e:
            logging.debug("compute_golden_institutional_bonus 调用失败(降级): %s", e)
            return [], 0.0, 0.0
    golden_engine = _golden_evaluate
except ImportError as e:
    logging.warning(f"金共振函数加载失败: {e}")

    def _golden_evaluate(df, rt, pool_key, ind_rank=999, net_elg=0.0, cyq=999.0):
        """fallback 占位：导入失败时返回空结果。"""
        return [], 0.0, 0.0
    golden_engine = None

try:
    from core.strategies.risk_control_engine import evaluate_3layer_risk, DEFAULT_RISK_CONFIG
except Exception as _scan_risk_imp:
    logging.debug("scan_engine 未加载 risk_control_engine（观察池二次一层将跳过）: %s", _scan_risk_imp)
    evaluate_3layer_risk = None
    DEFAULT_RISK_CONFIG = None

DANGER_BUY_VWAP_MIN_DAY_PCT = 0.4  # 占位：实际值在 _detectors_pattern.py


# ==================== 实用工具 ====================
def _safe_float(val, default=0.0):
    if val is None: return default
    try:
        if pd.isna(val) or str(val).strip() in ['', '-']: return default
        return float(val)
    except (ValueError, TypeError):
        return default


def _a_share_elapsed_minutes(curr_min) -> int:
    """返回 A 股已交易分钟数；午休不计入，范围固定为 1~240。"""
    cm = int(_safe_float(curr_min, 570.0))
    if cm < 570:
        return 1
    if cm <= 690:
        return max(1, cm - 570)
    if cm < 780:
        return 120
    if cm <= 900:
        return min(240, 120 + cm - 780)
    return 240


def _route_scored_pool_row(res, pool_key, row, *, observation_only=False) -> None:
    """将正式候选和观察候选写入互斥结果区，避免观察态污染主池。"""
    if observation_only:
        res.setdefault("observation", {}).setdefault(pool_key, []).append(row)
    else:
        res.setdefault(pool_key, []).append(row)


# ==================== 量比与基线兜底 ====================
def _sanitize_rt_vol_ratio(rt, df_target, hist, vol_col, tag):
    """P2–P5 与 base_info 共用：库中 vol_ratio=0、实时推算为 0 时，用语义一致的 vol/vol_ma5 兜底。"""
    vr = _safe_float(rt.get("vol_ratio", 0.0), 0.0)
    if vr > 0 and np.isfinite(vr):
        return float(min(vr, 50.0)), tag
    try:
        yb = df_target.iloc[-1]
        vh = _safe_float(yb.get(vol_col), 0.0)
        vma5 = _safe_float(yb.get("vol_ma5"), 0.0)
        if vma5 <= 0:
            vma5 = _safe_float(yb.get("vol_ma10"), 0.0)
        if vma5 > 0 and vh > 0:
            r = vh / vma5
            if np.isfinite(r) and r > 0:
                return float(min(r, 50.0)), "C"
    except Exception:
        pass
    if isinstance(hist, dict):
        raw = hist.get("vol_ratio", hist.get("vr"))
        rh = _safe_float(raw, float("nan"))
        if rh > 0 and np.isfinite(rh):
            return float(min(rh, 50.0)), "H" if ("vol_ratio" in hist or "vr" in hist) else tag
    return 1.0, "~"


def _intraday_ma5_vol_baseline_hand(df_target, vol_col: str) -> float:
    """盘中量比分母：取「最新一根已收盘 K」上的 5 日均量（手/日）。"""
    if df_target is None or df_target.empty:
        return 1.0
    last = df_target.iloc[-1]
    for col in ("vol_ma5", "vma5"):
        if col in df_target.columns:
            v = _safe_float(last[col], 0.0)
            if v > 0 and np.isfinite(v):
                return float(max(v, 1.0))
    vc = vol_col if vol_col and vol_col in df_target.columns else None
    if not vc:
        vc = "vol" if "vol" in df_target.columns else ("volume" if "volume" in df_target.columns else None)
    if vc:
        v = _safe_float(last[vc], 0.0)
        if v > 0 and np.isfinite(v):
            return float(max(v, 1.0))
    return 1.0


def _extract_scan_surface_metrics(item, rt_map, curr_min, is_after_hours, today_str, snapshots):
    """轻量截面采样：与主循环量价口径尽量一致。"""
    _ = today_str
    try:
        full_code = item.get("code", "")
        s_code = str(full_code).split(".")[0][:6]
        if s_code not in rt_map:
            fallback_hist = item.get("hist", {})
            if isinstance(fallback_hist, dict) and fallback_hist:
                _fb_pc = _safe_float(fallback_hist.get("pre_close"), 0.0)
                if _fb_pc <= 0:
                    _fb_pc = _safe_float(fallback_hist.get("close"), 0.0)
                rt_map[s_code] = {
                    "price": fallback_hist.get("close", 0),
                    "pre_close": _fb_pc,
                    "open": fallback_hist.get("open", 0),
                    "volume": 0,
                    "high": fallback_hist.get("high", 0),
                    "low": fallback_hist.get("low", 0),
                    "name": normalize_stock_display_name(fallback_hist.get("name", s_code)),
                }
            else:
                return None
        rt = dict(rt_map.get(s_code, {}) or {})
        if not isinstance(rt, dict):
            return None
        from core.scan.intraday_snapshots import _apply_intraday_snapshots_to_rt
        if isinstance(snapshots, dict) and s_code in snapshots:
            _apply_intraday_snapshots_to_rt(rt, snapshots[s_code])

        df = item.get("df")
        if df is None or df.empty:
            return None
        vol_col = "vol" if "vol" in df.columns else ("volume" if "volume" in df.columns else None)
        if vol_col is None:
            return None

        circ_mv_wan = _safe_float(df["circ_mv"].iloc[-1]) if "circ_mv" in df.columns else 0.0
        if circ_mv_wan <= 0:
            circ_mv_raw = rt.get("circ_mv")
            if circ_mv_raw is None or pd.isna(circ_mv_raw):
                circ_mv_raw = rt.get("total_mv", 10000000) * 0.6
            circ_mv_wan = _safe_float(circ_mv_raw, default=10000000)
        circ_mv_yi = circ_mv_wan / 10000.0
        if circ_mv_yi < _p1_scan_min_circ_mv_yi():
            return None

        hist = item.get("hist", {}) or {}
        now_price = _safe_float(rt.get("price"))
        if now_price <= 0:
            now_price = _safe_float(hist.get("close", 0.0))
        pre_price = _safe_float(rt.get("pre_close"), 0.0)
        if pre_price <= 0:
            pre_price = _safe_float(hist.get("pre_close", 0.0))
        if pre_price <= 0:
            pre_price = _safe_float(hist.get("close", 0.0))
        if pre_price <= 0:
            return None
        pct = (now_price - pre_price) / pre_price * 100.0

        vol_shares = _safe_float(rt.get("volume", 0.0))
        ma5_vol_hand = _intraday_ma5_vol_baseline_hand(df, vol_col)

        if vol_shares <= 0 or curr_min < 565:
            rt_local_vr = _safe_float(hist.get("vol_ratio", hist.get("vr", 1.0)), default=1.0)
            calc_price = now_price if now_price > 0 else _safe_float(hist.get("close", 10), default=10.0)
            _vh = _safe_float(hist.get("vol", df[vol_col].iloc[-1] if vol_col else 0), 0.0)
            _trf = infer_turnover_rate_f_pct(_vh, calc_price, circ_mv_wan)
        else:
            vol_hand = vol_shares / 100.0
            if is_after_hours:
                rt_local_vr = vol_hand / ma5_vol_hand
            else:
                elapsed_mins = _a_share_elapsed_minutes(curr_min)
                rt_local_vr = min((vol_hand / elapsed_mins) / (ma5_vol_hand / 240), 50.0)
            calc_price = now_price if now_price > 0 else _safe_float(hist.get("close", 10), default=10.0)
            _trf = min(infer_turnover_rate_f_pct(vol_hand, calc_price, circ_mv_wan), 100.0)

        rt_for_san = {"vol_ratio": rt_local_vr}
        _vr_fix, _ = _sanitize_rt_vol_ratio(rt_for_san, df, hist, vol_col, "S")
        vol_ratio = float(min(_vr_fix, 50.0))

        net3 = 0.0
        for col in ("net_main_amount",):
            if col in df.columns:
                net3 += _safe_float(df.tail(3)[col].sum())
        circ_base = max(circ_mv_yi * 100000000.0, 1e-9)
        main_ratio = float(net3 / circ_base)

        return {
            "s_code": s_code,
            "pct": float(np.nan_to_num(pct, nan=0.0, posinf=50.0, neginf=-50.0)),
            "vol_ratio": float(np.nan_to_num(vol_ratio, nan=1.0, posinf=50.0, neginf=0.05)),
            "turnover_f": float(np.nan_to_num(_trf, nan=0.0, posinf=100.0, neginf=0.0)),
            "main_ratio": float(np.nan_to_num(main_ratio, nan=0.0, posinf=10.0, neginf=-10.0)),
        }
    except Exception:
        return None
