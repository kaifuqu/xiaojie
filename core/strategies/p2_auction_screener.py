# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 — P2 竞价选股池「物理胸甲」核心筛选模块
================================================================================
本模块严格对齐 data_fetcher.ALL_55_COLS 字段契约，实现：

  【一】全局风控一票否决（先于四大策略执行）
  【二】四大独立竞价策略（满足任一即入池，并打策略标签）

----------------------------------------------------------------------
增量更新约定（务必遵守）
----------------------------------------------------------------------
1. 调用方只应传入「截至上一交易日 T-1 已收盘」的历史日线 df（按 ts_code 升序、trade_date 升序），
   再加上「当日 T」竞价/快照 rt（open、pre_close、vol_ratio、amount、turnover_rate_f 等）。
2. 本模块内部 **不会** 发起网络请求、**不会** 合并全市场历史、**不会** 触发 DuckDB 全表扫描；
   单日全市场筛选时，由上层按代码列表循环，每只股票只加载各自需要的 df 尾部窗口即可。
3. 批量接口 screen_p2_universe() 仅对内存中的 (df, rt) 列表做向量化/循环筛选，适合「只算今天」的日批任务。

----------------------------------------------------------------------
量纲说明（与 TuShare / 本项目落库一致）
----------------------------------------------------------------------
- circ_mv、total_mv：万元；100 亿 = 1,000,000 万元。
- net_elg_amount、net_main_amount、inst_net_buy 等资金字段：元（本项目 moneyflow 已 *10000）。
- strth：涨停/连板强度类字段，本模块按「万元」口径与策略四文案对齐（若数据源为其它量纲，请通过 P2ScreenerConfig 调整）。
- daily.amount：TuShare 日线成交额多为「千元」；竞价 rt['amount'] 依行情源可能为「元」或其它。
  「防飞刀·额萎缩」在无法确认量纲或缺失时 **不强行否决**，避免误杀（见 _auction_amount_shrinked）。

================================================================================
交钥匙部署指南（摘要 — 详见项目 README 或团队 Wiki 时可复制本节）
================================================================================
1) 文件位置：core/strategies/p2_auction_screener.py（本文件）
2) 引擎接入：core/strategies/strat_p2_auction.py 中 P2Auction.run_all 已调用
   evaluate_p2_screener()；无需再改数据管道。
3) 扫描集成：core/scan_engine.py 对 P2 增加 hit_res['p2_core_screener_pass']，
   在通过四大策略之一时放宽 strict_golden_burst_ok 的硬性门槛（否则策略三 0.5% 高开会被误杀）。
4) 调用示例（单股、增量一日）：
       from core.strategies.p2_auction_screener import evaluate_p2_screener
       r = evaluate_p2_screener(df_hist_tail, rt_snapshot)
       if r["strategies"]:
           print(r["strategies"], r["detail"])
5) 批量（多股、仍是一日增量）：
       from core.strategies.p2_auction_screener import screen_p2_universe
       df_report = screen_p2_universe(list_of_tuples)  # 每项 (ts_code, df, rt)
6) 运行注意：
   - df 至少 21 行（high_20、均线交叉等更稳）；不足时部分策略自动不满足，不抛异常。
   - 竞价阶段 vol_ratio、amount 若尚未落地，防飞刀与量能相关条件可能放宽跳过。
   - 调参只改 P2ScreenerConfig 实例，勿改 55 维字段名。
================================================================================
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

try:
    from core.strategies.fund_mv_utils import (
        circ_mv_wan_from,
        dynamic_inst_single_threshold_yuan,
        dynamic_net_main_threshold_yuan,
        dynamic_strth_threshold_wan,
        effective_turnover_rate_f,
    )
except ImportError:
    from strategies.fund_mv_utils import (  # type: ignore
        circ_mv_wan_from,
        dynamic_inst_single_threshold_yuan,
        dynamic_net_main_threshold_yuan,
        dynamic_strth_threshold_wan,
        effective_turnover_rate_f,
    )

logger = logging.getLogger(__name__)

try:
    import constants as _C
    _P1_MIN_CIRC_MV_WAN = float(getattr(_C, "P1_SELECT_MIN_CIRC_MV_WAN", 1_000_000))
except Exception:
    _P1_MIN_CIRC_MV_WAN = 1_000_000.0


# ---------------------------------------------------------------------------
# 可调参数集中配置（实盘可按回测结果微调，避免魔法数散落在分支里）
# ---------------------------------------------------------------------------
@dataclass
class P2ScreenerConfig:
    """P2 竞价筛选阈值配置（全部带默认值，可直接实例化后改字段）。"""

    # --- 全局风控 ---
    # 与 P1/scan_engine 一致：默认 100 亿（1_000_000 万元）；勿再写死其他值导致与 P1 池打架。
    circ_mv_min_wan: float = _P1_MIN_CIRC_MV_WAN
    # 超大盘加权阈值（万元）：高于此值在结果 detail 中记 sort_weight_bonus，供排序使用
    circ_mv_prefer_wan: float = 5_000_000.0
    # 开盘乖离：用开盘价相对昨收 ma20 计算，超过则否决
    open_bias20_max_pct: float = 7.5
    # 竞价高开幅度上限（相对昨收 %）
    auction_pct_chg_max: float = 5.5
    # 昨日换手率下限（%）
    # 真实换手 turnover_rate_f 通常高于总股本换手，阈值略高于旧版 1%
    prev_turnover_min_pct: float = 2.5

    # --- 策略一：主升浪确认 ---
    s1_prev_pct_low: float = 4.0
    s1_prev_pct_high: float = 6.0
    s1_prev_vol_ratio_min: float = 1.8
    s1_winner_min: float = 80.0   # V26.88+9-22 修复：s1 主升浪昨日 winner_rate 下限（漏定义）
    s1_pct_low: float = 1.0       # V26.88+9-22 补全：策略一竞价涨幅下限
    s1_pct_high: float = 3.0       # V26.88+9-22 补全：策略一竞价涨幅上限
    s1_auction_pct_low: float = 1.0
    s1_auction_pct_high: float = 3.0
    s1_atr_pct_max: float = 8.0   # V26.88+9-22 补全：策略一 ATR 上限
    s1_vol_ratio_min: float = 1.8  # V26.88+9-22 补全：策略一量比下限
    s1_auction_amount_vs_prev_ratio_min: float = 0.05  # V26.88+9-22 补全：竞价额/昨量比下限

    # --- 策略二：机构连贯发力 ---
    s2_prev_pct_low: float = 4.0   # V26.88+9-22 补全：昨日涨幅下限
    s2_prev_pct_high: float = 6.0  # V26.88+9-22 补全：昨日涨幅上限
    s2_prev_vol_ratio_min: float = 1.8  # V26.88+9-22 补全：昨日量比下限
    s2_pct_low: float = 0.5       # V26.88+9-22 补全：竞价涨幅下限（alias s2_auction_pct_low）
    s2_pct_high: float = 2.5      # V26.88+9-22 补全：竞价涨幅上限（alias s2_auction_pct_high）
    s2_auction_pct_low: float = 0.5
    s2_auction_pct_high: float = 2.5
    s2_auction_turnover_min_pct: float = 1.2
    s2_upper_shadow_max_pct: float = 2.5
    s2_inst_net_buy_ratio_of_float_mv: float = 0.00006

    # --- 策略三：无尽苍穹 ---
    s3_winner_min: float = 90.0
    s3_pct_low: float = 1.0
    s3_pct_high: float = 4.0
    s3_vol_ratio_min: float = 1.5
    s3_atr_pct_max: float = 8.0
    s3_auction_turnover_min_pct: float = 1.2  # V26.88+9-22 补全：策略三竞价换手率下限
    s3_inst_net_buy_ratio_of_float_mv: float = 0.00006  # V26.88+9-22 补全：策略三机构净买比例
    s3_upper_shadow_max_pct: float = 2.5  # V26.88+9-22 补全：策略三长上影上限
    # 竞价额相对昨日全天成交额比例低于此值视为「异常萎缩」（量纲一致时生效）
    s3_auction_amount_vs_prev_ratio_min: float = 0.05

    # --- 策略四：底仓重金点火（流通市值比例 + 阶梯地板，见 fund_mv_utils）---
    s4_circ_mv_min_wan: float = 5_000_000.0
    s4_net_main_ratio_of_float_mv: float = 0.00018
    s4_pct_low: float = 2.0
    s4_pct_high: float = 4.0
    s4_strth_ratio_of_circ_mv_wan: float = 0.0004

    # --- P2 新主线增强：板块 / 分时生死线 / 次日预期 ---
    regime_strict_boost: float = 1.12
    regime_relaxed_boost: float = 0.92
    board_beta_core_min: float = 1.15
    board_beta_hot_min: float = 1.05
    # 【V26.88+10-1 P2-3 优化】从 0.90 → 0.85（更严的冷板块拦截；冷板块定义为弱于行业 15% 以上）
    board_beta_cold_max: float = 0.85
    open_confirm_minute_low: int = 573
    open_confirm_minute_high: int = 575
    open_confirm_vwap_eps: float = 0.002
    open_confirm_penalty_miss: float = 18.0
    open_confirm_bonus_hit: float = 8.0
    vwap_death_gap_min_pct: float = 0.6
    vwap_death_penalty: float = 25.0
    vwap_death_hard_pct: float = 1.8
    # 【V26.88+ 9-30】朋友1建议：min_samples 从 6 → 2，并加小样本衰减
    # 5300 只股票中大部分新股/小票永远凑不齐 6 次 T+1 历史样本，原值导致该功能空转
    t1_memory_weight: float = 0.20
    t1_memory_min_samples: int = 2  # 由 6 → 2
    t1_memory_sample_decay: float = 0.4  # 小样本时 boost 衰减系数
    t1_memory_boost_max: float = 10.0
    t1_memory_penalty_max: float = 8.0

    # --- V26.88.1 A 股独有风险拦截（接盘侠规避）---
    # 【V26.88+ 9-30】朋友2建议：one_word_board_pct_min=9.5 不能区分板块
    # 改用板块动态阈值：主板 9.5% / 创业板 19.5% / 科创板 19.5% / 北交所 29.5% / ST 4.5%
    # 默认值保持 9.5%（主板），其他板块由 get_limit_up_pct(ts_code, name) 动态计算
    one_word_board_pct_min: float = 9.5
    # ② 跌停危险阈值：使用动态 -板块涨停阈值，配置这里只给主板兜底
    prev_limit_down_pct_max: float = -9.5
    # ③ 一字板二次确认：竞价涨幅 ≥ 板块涨停阈值时，竞价成交量必须 < 5日均量×0.3 才视为"无量封板"
    one_word_volume_ratio_max: float = 0.3
    # ④ 高位跌停反弹诱多：当前价相对 20 日高点的回撤不超过 3%，但昨日跌停 → 疑似诱多
    prev_limit_down_rebound_bias_pct: float = 3.0

    def __post_init__(self):
        """【V26.88+ 9-30 第9项落地】P2 t1_memory 小样本对齐到 constants.T1_MEMORY_*。

        关键对齐项：
          - t1_memory_min_samples: 6 → constants.T1_MEMORY_MIN_SAMPLES（2）
          - t1_memory_sample_decay: 0.4 → constants.T1_MEMORY_SAMPLE_DECAY
        """
        try:
            import constants as _C
            if hasattr(_C, "T1_MEMORY_MIN_SAMPLES"):
                self.t1_memory_min_samples = int(_C.T1_MEMORY_MIN_SAMPLES)
            if hasattr(_C, "T1_MEMORY_SAMPLE_DECAY"):
                self.t1_memory_sample_decay = float(_C.T1_MEMORY_SAMPLE_DECAY)
        except Exception:
            pass


def _resolve_p2_cfg(cfg: Optional[P2ScreenerConfig]) -> P2ScreenerConfig:
    if cfg is not None:
        return cfg
    from core.config_manager import get_p2_screener_config

    return get_p2_screener_config()


def _regime_bucket(rt: Dict[str, Any]) -> str:
    s = str(rt.get("_regime_state", rt.get("regime", "")) or "")
    if any(k in s for k in ["主升", "趋势"]):
        return "strict"
    if any(k in s for k in ["退潮", "空头", "主跌"]):
        return "relaxed"
    return "neutral"


def _board_beta(rt: Dict[str, Any]) -> float:
    for k in ("sector_beta", "industry_beta", "sector_mult"):
        v = _safe_float(rt.get(k), 0.0)
        if v > 0:
            return max(0.7, min(1.5, v))
    return 1.0


def _mainline_sector_score(rt: Dict[str, Any], board_beta: float) -> Tuple[float, str]:
    """
    主线板块识别：偏向强板块中最强、最稳的那一类。

    V26.88+9-22 增强：
    - 新增 HiThink 大盘指数实时锚（_hithink_index_strength 0~2）：
      大盘极强(+2) → 全市场系统性做多，分项分 +1.5；
      大盘极弱(≤0) → 全市场系统性回避，分项分 -1.5；
      中性(1.0) → 不影响原有板块分。
    - 该锚由 auto_sniper_daemon 在扫描前注入 rt['rt']，盘中运行时实时生效，
      解决「regime_analyzer 日线数据滞后导致 _regime_bucket 总是 neutral」的历史痛点。
    """
    sector_strength = _safe_float(rt.get("sector_strength", rt.get("industry_strength", board_beta)), board_beta)
    sector_rank = _safe_float(rt.get("sector_rank"), 999.0)
    sector_total = _safe_float(rt.get("sector_total"), 0.0)
    sector_advance = _safe_float(rt.get("sector_advance_ratio"), 0.0)
    sector_vwap_gap = _safe_float(rt.get("sector_vwap_gap_pct"), 0.0)

    score = 0.0
    reasons = []

    # V26.88+9-22 新增：HiThink 大盘指数实时锚
    # 市场强度 0.0~2.0 → 映射到 -1.5 ~ +1.5 的分项加权
    # 大盘极弱(0.0)：全市场系统性风险，分项降 -1.5
    # 大盘极强(2.0)：全市场系统性做多，分项升 +1.5
    hithink_strength = _safe_float(rt.get("_hithink_index_strength"), 1.0)
    market_bias = 0.0  # 默认中性（V26.88+9-22 新增 HiThink 大盘指数实时锚）
    if hithink_strength < 0.5:
        market_bias = -1.5
        reasons.append(f"A股大盘极弱(market_strength={hithink_strength:.1f})")
    elif hithink_strength < 1.0:
        market_bias = -0.5
        reasons.append(f"A股大盘偏弱(market_strength={hithink_strength:.1f})")
    elif hithink_strength >= 1.5:
        market_bias = 1.5
        reasons.append(f"A股大盘极强(market_strength={hithink_strength:.1f})")
    elif hithink_strength >= 1.2:
        market_bias = 0.8
        reasons.append(f"A股大盘偏强(market_strength={hithink_strength:.1f})")
    # else 1.0 ≤ hithink_strength < 1.2 → 中性，market_bias = 0.0（已默认）

    if sector_strength >= 1.15:
        score += 3.0
        reasons.append("强度>1.15")
    elif sector_strength >= 1.05:
        score += 1.8
        reasons.append("强度>1.05")
    elif sector_strength <= 0.95:
        score -= 2.5
        reasons.append("强度偏弱")

    if sector_advance >= 0.55:
        score += 1.5
        reasons.append("上涨家数占优")
    elif sector_advance > 0 and sector_advance < 0.35:
        score -= 1.2
        reasons.append("上涨家数不足")

    if sector_vwap_gap > 0.8:
        score += 1.0
        reasons.append("板块VWAP上方")
    elif sector_vwap_gap < -0.6:
        score -= 1.0
        reasons.append("板块VWAP下方")

    if sector_total >= 3 and sector_rank > 0:
        if sector_rank <= 2:
            score += 2.0
            reasons.append("板块前排")
        elif sector_rank <= 5:
            score += 1.0
            reasons.append("板块前列")
        elif sector_rank > sector_total - 3:
            score -= 2.0
            reasons.append("板块后排")

    score += market_bias
    return float(score), "/".join(reasons) if reasons else "中性"


def _open_confirm_window(rt: Dict[str, Any], cfg: P2ScreenerConfig) -> bool:
    m = rt.get("curr_min")
    if m is None:
        return False
    try:
        mi = int(float(m))
    except Exception:
        return False
    return int(cfg.open_confirm_minute_low) <= mi <= int(cfg.open_confirm_minute_high)


def _estimate_vwap_from_rt(rt: Dict[str, Any], ref_price: float) -> float:
    amt = _safe_float(rt.get("amount"), 0.0)
    vol = _safe_float(rt.get("volume"), 0.0)
    if amt <= 0 or vol <= 0:
        return 0.0
    tentative = amt / max(vol, 1e-9)
    if tentative > ref_price * 20.0:
        return amt / max(vol * 100.0, 1e-9)
    return tentative


def _open_vwap_support_ok(rt: Dict[str, Any], now_px: float, cfg: P2ScreenerConfig) -> bool:
    vw = _estimate_vwap_from_rt(rt, now_px)
    if vw <= 0 or now_px <= 0:
        return True
    return now_px >= vw * (1.0 - cfg.open_confirm_vwap_eps)


def _p2_collect_t1_memory(rt: Dict[str, Any]) -> Tuple[float, float, float]:
    mem = rt.get("_t1_memory") if isinstance(rt.get("_t1_memory"), dict) else {}
    avg_ret = _safe_float(mem.get("avg_ret_t1_pct", 0.0), 0.0)
    win_rate = _safe_float(mem.get("win_rate_t1_pct", 0.0), 0.0)
    sample_n = _safe_float(mem.get("sample_n", 0.0), 0.0)
    return avg_ret, win_rate, sample_n


# 【V26.88+ 9-30】板块涨停阈值解析（迁移至 board_limit_utils 统一模块）
def _BOARD_LIMIT_UP_CACHE(ts_code: str, name: str = "") -> float:
    try:
        from core.strategies.board_limit_utils import get_limit_up_pct_fast
        return float(get_limit_up_pct_fast(ts_code, name))
    except Exception:
        # 兜底：复刻简单逻辑（避免引入 import 循环）
        name_upper = str(name or "").upper()
        if "ST" in name_upper or "*ST" in name_upper:
            return 4.5
        code = str(ts_code or "").upper()
        if code.endswith(".BJ"):
            return 29.5
        if code.endswith(".SZ") and code[:6].startswith("30"):
            return 19.5
        if code.endswith(".SH") and code[:6].startswith("688"):
            return 19.5
        return 9.5


def _p2_t1_memory_score(avg_ret: float, win_rate: float, sample_n: float, cfg: P2ScreenerConfig) -> float:
    """
    【V26.88+ 9-30】朋友1建议重写：
    - min_samples 从 6 → 2，让新股/小票 T+1 信号有意义
    - 小样本时按 sample_n/min_samples 比例衰减 boost/penalty 强度，避免 1 个样本就飙分
    """
    if sample_n < cfg.t1_memory_min_samples:
        return 0.0
    # 小样本衰减：sample_n=2 时 boost/penalty 仅 0.4× 强度
    decay_factor = min(1.0, (sample_n / max(cfg.t1_memory_min_samples * 3.0, 1.0)) * cfg.t1_memory_sample_decay + (1.0 - cfg.t1_memory_sample_decay))
    # 上限 sample_n >= 3*min_samples 时不再衰减
    if sample_n >= cfg.t1_memory_min_samples * 3:
        decay_factor = 1.0
    score = 0.0
    if avg_ret > 0:
        score += min(cfg.t1_memory_boost_max, avg_ret * cfg.t1_memory_weight) * decay_factor
    else:
        score -= min(cfg.t1_memory_penalty_max, abs(avg_ret) * cfg.t1_memory_weight) * decay_factor
    if win_rate >= 55.0:
        score += min(cfg.t1_memory_boost_max * 0.6, (win_rate - 50.0) * 0.10) * decay_factor
    elif win_rate < 45.0:
        score -= min(cfg.t1_memory_penalty_max * 0.6, (45.0 - win_rate) * 0.12) * decay_factor
    return float(score)


# 兼容旧 import；运行时应使用 _resolve_p2_cfg(None) 或 ConfigManager
DEFAULT_P2_CONFIG = P2ScreenerConfig()


def _safe_float(val: Any, default: float = 0.0) -> float:
    """安全浮点转换：与 scan_engine / strat_base 行为保持一致。"""
    if val is None:
        return default
    try:
        if isinstance(val, (float, np.floating)) and pd.isna(val):
            return default
        s = str(val).strip()
        if s in ("", "-", "nan", "None"):
            return default
        return float(val)
    except (TypeError, ValueError):
        return default


def _macd_hist_row(row: pd.Series) -> float:
    """
    MACD 柱（1× 统一口径）。

    【V30.10 统一 MACD 口径 2026-10-21】原实现「macd_hist 为 0 时回退 macd_bar」
    会把 1× / 2× 两种尺度混用；现统一走 core.strategies.macd_unified。
    """
    try:
        from core.strategies.macd_unified import macd_hist_normalized

        v = macd_hist_normalized(row)
        return float(v) if v is not None else 0.0
    except Exception:
        v = row.get("macd_hist", np.nan)
        if pd.isna(v) or _safe_float(v, 0.0) == 0.0:
            v = row.get("macd_bar", 0.0)
        return _safe_float(v, 0.0)


def _open_bias20_pct(open_px: float, ma20: float) -> float:
    """以开盘价估算的 20 日乖离率（分母为昨日均线 ma20）。"""
    if ma20 <= 0 or open_px <= 0:
        return 0.0
    return (open_px - ma20) / ma20 * 100.0


def _auction_pct_vs_preclose(open_px: float, pre_close: float) -> float:
    """竞价涨幅 %：相对昨收（或 pre_close）。"""
    if pre_close <= 0 or open_px <= 0:
        return 0.0
    return (open_px - pre_close) / pre_close * 100.0


def _vol_ma_cross_upward(df: pd.DataFrame) -> bool:
    """
    量能均线「上穿」判定（在 T-1 收盘时刻下可计算的部分）。

    业务含义：用最近两根已收盘 K 上的 vol_ma5 / vol_ma20，判断是否发生由下转上的金叉。
    竞价时刻无法预知当日全日量，因此不臆造「预估成交量」，只使用已落地日线量能均线。
    """
    if df is None or len(df) < 2:
        return False
    if "vol_ma5" not in df.columns or "vol_ma20" not in df.columns:
        return False
    a = df.iloc[-2]
    b = df.iloc[-1]
    v5a, v20a = _safe_float(a.get("vol_ma5")), _safe_float(a.get("vol_ma20"))
    v5b, v20b = _safe_float(b.get("vol_ma5")), _safe_float(b.get("vol_ma20"))
    if v5a <= 0 or v20a <= 0 or v5b <= 0 or v20b <= 0:
        return False
    return (v5a <= v20a) and (v5b > v20b)


def _auction_amount_shrinked(rt: Dict[str, Any], prev_row: pd.Series, cfg: P2ScreenerConfig) -> bool:
    """
    策略一防飞刀：竞价成交额相对昨日全天额是否「异常萎缩」。

    注意：若 rt['amount'] 或 prev_row['amount'] 缺失/为 0，或两者量纲明显不可比，
    则返回 False（不触发飞刀剔除），避免数据不齐时全池误杀。
    """
    rt_amt = _safe_float(rt.get("amount"), 0.0)
    prev_amt = _safe_float(prev_row.get("amount"), 0.0)
    if rt_amt <= 0 or prev_amt <= 0:
        return False
    ratio = rt_amt / prev_amt
    thr = float(cfg.s1_auction_amount_vs_prev_ratio_min)
    # 若竞价额已经大于昨日全天（通常量纲不一致），视为不可比，不杀
    if ratio >= 1.0:
        return False
    return ratio < thr


def p2_global_risk_veto(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    cfg: Optional[P2ScreenerConfig] = None,
) -> Tuple[bool, str]:
    """
    P2 全局风控（一票否决）。

    返回:
        (True, "")  — 通过，可进入四大策略
        (False, 原因文案) — 不通过

    字段来源:
        - 体量、估值、换手、均线斜率：优先取 df 最后一根（T-1 收盘）
        - 开盘价、竞价涨幅、乖离：用 rt['open'] 与昨收 ma20
    """
    cfg = _resolve_p2_cfg(cfg)
    if df is None or df.empty:
        return False, "历史K线为空"

    y = df.iloc[-1]

    circ_mv = _safe_float(rt.get("circ_mv"), _safe_float(y.get("circ_mv"), 0.0))
    if circ_mv < cfg.circ_mv_min_wan:
        return False, f"流通市值过小 circ_mv={circ_mv:.0f}万 < {cfg.circ_mv_min_wan:.0f}万"

    ma60 = _safe_float(y.get("ma60"), 0.0)
    y_close = _safe_float(y.get("close"), 0.0)
    open_px = _safe_float(rt.get("open"), 0.0)
    price_ref = open_px if open_px > 0 else _safe_float(rt.get("price"), y_close)
    if ma60 > 0 and y_close > 0 and y_close <= ma60:
        return False, "左侧/下行: 昨收 close <= ma60"

    ma20_slope_5 = _safe_float(y.get("ma20_slope_5"), 0.0)
    if ma20_slope_5 <= 0:
        return False, f"20日均线斜率 ma20_slope_5={ma20_slope_5:.4f} <= 0"

    ma20 = _safe_float(y.get("ma20"), 0.0)
    bias_open = _open_bias20_pct(open_px, ma20)
    if bias_open > cfg.open_bias20_max_pct:
        return False, f"极端乖离开局 bias20(open)={bias_open:.2f}% > {cfg.open_bias20_max_pct}%"

    pre_close = _safe_float(rt.get("pre_close"), y_close)
    auc_pct = _auction_pct_vs_preclose(open_px, pre_close)
    if auc_pct > cfg.auction_pct_chg_max:
        return False, f"假突破陷阱 竞价高开 pct={auc_pct:.2f}% > {cfg.auction_pct_chg_max}%"

    wr_y = _safe_float(y.get("winner_rate"), 0.0)
    if auc_pct >= 3.0 and wr_y < 82.0:
        return False, "高开陷阱: 竞价涨幅≥3%但昨获利盘不足82%(易遭抛压)"

    if bias_open > 6.0 and auc_pct >= 2.5:
        px_a = open_px if open_px > 0 else _safe_float(rt.get("price"), y_close)
        est_to = effective_turnover_rate_f(rt, y, px_a if px_a > 0 else y_close)
        if est_to > 22.0:
            return False, "高开陷阱: 乖离过大且竞价换手畸高(疑似出货)"

    pe_ttm = _safe_float(y.get("pe_ttm"), 0.0)
    if pe_ttm < 0:
        return False, "市盈率 pe_ttm < 0"

    prev_to = effective_turnover_rate_f({}, y, y_close)
    if prev_to < cfg.prev_turnover_min_pct:
        return False, f"昨日真实换手过低 turnover_rate_f={prev_to:.2f}% < {cfg.prev_turnover_min_pct}%"

    # === V26.88.1 A 股独有风险拦截（接盘侠规避）===
    # ⑥ ST 股拦截：股票名称含 ST / *ST / SST → 直接否决（A 股 ST 股每天±5%，但流动性差、容易被误判）
    # 严格检测：① 名称以 *ST/SST 开头；② 名称以 ST 开头且后接中文字符（避免误杀 STORE/STEVEN）
    _stock_name = str(
        rt.get("name", rt.get("stock_name", rt.get("名称", ""))) or ""
    ).upper().strip()
    if _stock_name.startswith("*ST") or _stock_name.startswith("SST"):
        return False, f"ST拦截: *ST/SST 股 {(_stock_name[:8])} (流动性差/易被误判)"
    if _stock_name.startswith("ST") and len(_stock_name) > 2:
        # 检查 ST 后面第一个字符：
        # ① 中文字符（CJK U+4E00-U+9FFF）→ 真 ST 股，否决
        # ② 空格/符号 → 也应视为真实 ST 股（如 "ST 国安" 中间有空格），否决
        # ③ 纯 ASCII 字母数字 → STORE / STEVEN / ST01 等，正常股票，放过
        _next_char = _stock_name[2:3]
        if _next_char:
            _is_ascii_alnum = _next_char.isalnum() and ord(_next_char) < 128
            if not _is_ascii_alnum:
                return False, f"ST拦截: ST 股 {(_stock_name[:8])} (流动性差/易被误判)"

    # ⑦ 一字板 / 秒板拦截：竞价涨幅 ≥ 板块涨停阈值 且竞价成交量稀少 → 高位接盘危险，T+1 极易低开
    # 【V26.88+ 9-30】朋友2建议：动态板块阈值（创业板/科创板19.5%、北交所29.5%、主板9.5%、ST 4.5%）
    _ts_code_for_board = str(rt.get("ts_code") or (df.attrs.get("ts_code") if hasattr(df, "attrs") else "") or "")
    _dynamic_limit_up_pct = _BOARD_LIMIT_UP_CACHE(_ts_code_for_board, _stock_name)
    if auc_pct >= _dynamic_limit_up_pct:
        # 估算竞价成交量：rt['volume'] 是当前已成交股数
        auction_vol = _safe_float(rt.get("volume"), 0.0)
        # 取 T-1 的 5 日均量（手数）
        vol_ma5 = _safe_float(y.get("vol_ma5"), 0.0)
        if vol_ma5 > 0 and auction_vol > 0:
            vol_ratio_auction = auction_vol / (vol_ma5 * 100.0)  # 手数转股数
        else:
            vol_ratio_auction = 1.0  # 数据缺失视为正常
        if vol_ratio_auction < cfg.one_word_volume_ratio_max:
            return False, (
                f"一字板拦截: 竞价{auc_pct:.2f}%且量能{vol_ratio_auction:.2f}×ma5"
                f"(<{cfg.one_word_volume_ratio_max})，无量封板 T+1 必砸"
            )
        else:
            # 即便不是一字板，竞价 ≥ 板块涨停阈值也太极端
            return False, f"假突破陷阱: 竞价{auc_pct:.2f}%≥{_dynamic_limit_up_pct:.1f}% (一字/秒板边界，板块{('主板' if _dynamic_limit_up_pct<=10 else ('创业板/科创板' if _dynamic_limit_up_pct<=20 else '北交所'))})，T+1 风险极高"

    # ⑧ 昨日跌停拦截：前日跌幅 ≤ -板块涨停阈值 → 次日仍可能继续恐慌（特别是高位跌停）
    y_pct_chg = _safe_float(y.get("pct_chg"), 0.0)
    _dynamic_limit_down_pct = -_dynamic_limit_up_pct
    if y_pct_chg <= _dynamic_limit_down_pct:
        # 若同时靠近 20 日高点（bias 较小），属于"高位跌停诱多反弹"，更危险
        high_20 = _safe_float(y.get("high_20"), 0.0)
        rebound_bias = ((open_px - high_20) / high_20 * 100.0) if high_20 > 0 and open_px > 0 else -100.0
        if rebound_bias >= -cfg.prev_limit_down_rebound_bias_pct:
            return False, (
                f"跌停拦截: 昨日跌停({y_pct_chg:.2f}%, 板块阈值{_dynamic_limit_down_pct:.1f}%)且价近20日高点"
                f"({rebound_bias:.1f}%)，高位跌停诱多反弹风险"
            )
        return False, f"跌停拦截: 昨日跌停({y_pct_chg:.2f}%, 板块阈值{_dynamic_limit_down_pct:.1f}%)，次日仍可能继续恐慌"

    return True, ""


def _strategy_main_wave_confirm(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    cfg: P2ScreenerConfig,
) -> Tuple[bool, str]:
    """
    策略一：主升浪确认（量价引擎共振接力）

    昨日:
        pct_chg 4%~6%, macd_hist > 0, vol_ratio >= 1.8
    今日竞价:
        open > ma5, pct 1%~3%
    均线:
        vol_ma5 上穿 vol_ma20（在昨收可判定意义下）
    防飞刀:
        昨日 net_main_amount < 0 则剔除
    """
    open_px = _safe_float(rt.get("open"), 0.0)
    pre_close = _safe_float(rt.get("pre_close"), _safe_float(y.get("close"), 0.0))
    pct = _auction_pct_vs_preclose(open_px, pre_close)
    vr = _safe_float(rt.get("vol_ratio"), 0.0)

    wr = _safe_float(y.get("winner_rate"), _safe_float(rt.get("winner_rate"), 0.0))
    c95 = _safe_float(y.get("cost_95th"), _safe_float(rt.get("cost_95th"), 0.0))
    h20 = _safe_float(y.get("high_20"), 0.0)

    if not (wr > cfg.s1_winner_min):
        return False, "winner_rate 不足"
    if c95 <= 0 or open_px <= c95:
        return False, "开盘未过 cost_95th"
    if h20 <= 0 or open_px <= h20:
        return False, "开盘未过 high_20"
    if not (cfg.s1_pct_low <= pct <= cfg.s1_pct_high):
        return False, "竞价涨幅不在 1%~4%"
    if vr < cfg.s1_vol_ratio_min:
        return False, "量比不足"

    atrp = _safe_float(y.get("atr_pct"), 0.0)
    if atrp > cfg.s1_atr_pct_max:
        return False, f"防飞刀 atr_pct={atrp:.2f}% > {cfg.s1_atr_pct_max}%"

    if _auction_amount_shrinked(rt, y, cfg):
        return False, "防飞刀 竞价额相对昨日萎缩"

    return True, "OK"


def _strategy_institution_streak(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    cfg: P2ScreenerConfig,
) -> Tuple[bool, str]:
    """
    策略二：机构连贯发力（长线资金护航高开）

    资金:
        inst_net_buy 连续 2 日 > 0（取昨日可见的最新筹码数据）
    趋势:
        ma5 > ma20 > ma60（昨日收盘均线多头）
    今日竞价:
        pct 0.5%~2.5%, 预估开盘 turnover_rate_f > 0.5%
    防飞刀:
        昨日长上影 (high-close)/close > 3%
    """
    y_pct = _safe_float(y.get("pct_chg"), 0.0)
    y_vr = _safe_float(y.get("vol_ratio"), 0.0)
    mh = _macd_hist_row(y)
    if not (cfg.s2_prev_pct_low <= y_pct <= cfg.s2_prev_pct_high):
        return False, "昨日涨幅不在 4%~6%"
    if mh <= 0:
        return False, "昨日 macd_hist 未为正"
    if y_vr < cfg.s2_prev_vol_ratio_min:
        return False, "昨日量比不足 1.8"

    open_px = _safe_float(rt.get("open"), 0.0)
    pre_close = _safe_float(rt.get("pre_close"), _safe_float(y.get("close"), 0.0))
    ma5 = _safe_float(y.get("ma5"), 0.0)
    pct = _auction_pct_vs_preclose(open_px, pre_close)
    if ma5 <= 0 or open_px <= ma5:
        return False, "开盘未站上 ma5"
    if not (cfg.s2_auction_pct_low <= pct <= cfg.s2_auction_pct_high):
        return False, "竞价涨幅不在 1%~3%"

    if not _vol_ma_cross_upward(df):
        return False, "vol_ma5 未上穿 vol_ma20"

    nm = _safe_float(y.get("net_main_amount"), 0.0)
    if nm < 0:
        return False, "防飞刀 昨日主力净额 < 0"

    return True, "OK"


def _strategy_endless_sky(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    cfg: P2ScreenerConfig,
) -> Tuple[bool, str]:
    """
    策略三：无尽苍穹（筹码彻底解放型高开）

    条件同时满足:
        - 昨日 winner_rate > 90%
        - 今日 open > 昨日 cost_95th
        - 今日 open > 近20日 high_20（取昨日行上的 rolling 值）
        - 今日 pct_chg 1%~4%
        - 今日 vol_ratio >= 1.5
    防飞刀:
        - 竞价额异常萎缩 或 昨日 atr_pct > 8%
    """
    if len(df) < 2:
        return False, "历史不足 2 日"

    y_prev = df.iloc[-2]
    cmw = circ_mv_wan_from(y, rt)
    inst_thr = dynamic_inst_single_threshold_yuan(cmw, cfg.s3_inst_net_buy_ratio_of_float_mv)
    inst1 = _safe_float(y.get("inst_net_buy"), 0.0)
    inst0 = _safe_float(y_prev.get("inst_net_buy"), 0.0)
    if not (inst1 > inst_thr and inst0 > inst_thr):
        return False, "inst_net_buy 未连续两日达动态机构门槛"

    ma5 = _safe_float(y.get("ma5"), 0.0)
    ma20 = _safe_float(y.get("ma20"), 0.0)
    ma60 = _safe_float(y.get("ma60"), 0.0)
    if not (ma5 > ma20 > ma60):
        return False, "均线未呈 ma5>ma20>ma60"

    open_px = _safe_float(rt.get("open"), 0.0)
    pre_close = _safe_float(rt.get("pre_close"), _safe_float(y.get("close"), 0.0))
    pct = _auction_pct_vs_preclose(open_px, pre_close)
    if not (cfg.s3_pct_low <= pct <= cfg.s3_pct_high):
        return False, "竞价涨幅不在 0.5%~2.5%"

    px_auction = open_px if open_px > 0 else _safe_float(rt.get("price"), pre_close)
    est_to = effective_turnover_rate_f(rt, y, px_auction if px_auction > 0 else pre_close)
    if est_to <= cfg.s3_auction_turnover_min_pct:
        return False, f"预估开盘换手 {est_to:.3f}% <= {cfg.s3_auction_turnover_min_pct}%"

    y_high = _safe_float(y.get("high"), 0.0)
    y_close = _safe_float(y.get("close"), 0.0)
    if y_close > 0:
        upper = (y_high - y_close) / y_close * 100.0
        if upper > cfg.s3_upper_shadow_max_pct:
            return False, f"防飞刀 长上影 {upper:.2f}% > {cfg.s3_upper_shadow_max_pct}%"

    return True, "OK"


def _strategy_heavy_base_ignite(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    cfg: P2ScreenerConfig,
) -> Tuple[bool, str]:
    """
    策略四：底仓重金点火（巨头温和启动）

    体量:
        circ_mv > 5000000 万（500 亿）
    异动:
        近 20 日 limit_times == 1 或 2（取昨日行上的窗口统计值）
    资金:
        昨日 net_main_amount 达 max(流通市值×比例, 市值阶梯地板)（元）
    今日竞价:
        pct 2%~4%, strth(万) > max(circ_mv(万)×比例, 3000 万)
    防飞刀:
        今日 open <= 昨日 cost_50th 则否决
    """
    cmw = circ_mv_wan_from(y, rt)
    if cmw <= cfg.s4_circ_mv_min_wan:
        return False, "流通市值未达 500 亿档"

    lt = int(_safe_float(y.get("limit_times"), 0.0))
    if lt not in (1, 2):
        return False, "limit_times 不为 1 或 2"

    nm = _safe_float(y.get("net_main_amount"), 0.0)
    nm_thr_yuan = dynamic_net_main_threshold_yuan(cmw, cfg.s4_net_main_ratio_of_float_mv)
    if nm <= nm_thr_yuan:
        return False, "昨日主力净额不足(动态门槛)"

    open_px = _safe_float(rt.get("open"), 0.0)
    pre_close = _safe_float(rt.get("pre_close"), _safe_float(y.get("close"), 0.0))
    pct = _auction_pct_vs_preclose(open_px, pre_close)
    if not (cfg.s4_pct_low <= pct <= cfg.s4_pct_high):
        return False, "竞价涨幅不在 2%~4%"

    strth = _safe_float(rt.get("strth"), _safe_float(y.get("strth"), 0.0))
    strth_need = dynamic_strth_threshold_wan(cmw, cfg.s4_strth_ratio_of_circ_mv_wan)
    if strth <= strth_need:
        return False, "strth 不足(动态门槛)"

    c50 = _safe_float(y.get("cost_50th"), _safe_float(rt.get("cost_50th"), 0.0))
    if c50 > 0 and open_px <= c50:
        return False, "防飞刀 开盘未过 cost_50th"

    return True, "OK"


def evaluate_p2_screener(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    cfg: Optional[P2ScreenerConfig] = None,
) -> Dict[str, Any]:
    """
    单股 P2 完整筛选（全局风控 + 四大策略）。

    参数:
        df: 截至 T-1 的日线，需含 ALL_55_COLS 中策略用到的列（缺失列策略自动不满足）
        rt: 当日竞价快照 dict
        cfg: 阈值配置

    返回 dict 键:
        veto_pass: bool
        veto_reason: str
        strategies: List[str]  命中的策略标签（可多条，业务上满足任一即可入池）
        strategy_checks: Dict[str, str] 各策略未通过原因或 OK
        sort_weight_bonus: float  市值超配加权系数（>1 表示更优先排序）
        p2_core_screener_pass: bool  是否命中至少一条策略（供 scan_engine 放宽黄金门禁）
        detail: dict 调试信息
    """
    cfg = _resolve_p2_cfg(cfg)
    out: Dict[str, Any] = {
        "veto_pass": False,
        "veto_reason": "",
        "strategies": [],
        "strategy_checks": {},
        "sort_weight_bonus": 1.0,
        "p2_core_screener_pass": False,
        "detail": {},
    }

    ok, reason = p2_global_risk_veto(df, rt, cfg)
    out["veto_pass"] = ok
    out["veto_reason"] = reason
    if not ok:
        return out

    y = df.iloc[-1]
    circ_mv = _safe_float(rt.get("circ_mv"), _safe_float(y.get("circ_mv"), 0.0))
    if circ_mv >= cfg.circ_mv_prefer_wan:
        out["sort_weight_bonus"] = 1.08

    now_px = _safe_float(rt.get("open"), _safe_float(rt.get("price"), 0.0))
    regime_bucket = _regime_bucket(rt)
    board_beta = _board_beta(rt)
    sector_strength = float(rt.get("sector_strength", rt.get("industry_strength", board_beta)) or board_beta)
    sector_rank = _safe_float(rt.get("sector_rank"), 999.0)
    sector_total = _safe_float(rt.get("sector_total"), 0.0)
    mainline_score, mainline_reason = _mainline_sector_score(rt, board_beta)
    t1_avg, t1_win, t1_n = _p2_collect_t1_memory(rt)
    t1_memory_score = _p2_t1_memory_score(t1_avg, t1_win, t1_n, cfg)
    open_confirm_hit = _open_confirm_window(rt, cfg) and _open_vwap_support_ok(rt, now_px, cfg)
    vwap_px = _estimate_vwap_from_rt(rt, now_px)
    vwap_gap_pct = ((now_px - vwap_px) / vwap_px * 100.0) if vwap_px > 0 and now_px > 0 else 0.0

    # 序号 P2-01～P2-04：与产品文档/配置常量命名对齐，便于检索与复盘
    strat_defs = [
        ("P2-01·★主升浪确认", lambda: _strategy_main_wave_confirm(df, rt, y, cfg)),
        ("P2-02·★机构连贯发力", lambda: _strategy_institution_streak(df, rt, y, cfg)),
        ("P2-03·★无尽苍穹", lambda: _strategy_endless_sky(df, rt, y, cfg)),
        ("P2-04·★底仓重金点火", lambda: _strategy_heavy_base_ignite(df, rt, y, cfg)),
    ]

    if regime_bucket == "strict":
        out["sort_weight_bonus"] *= cfg.regime_strict_boost
    elif regime_bucket == "relaxed":
        out["sort_weight_bonus"] *= cfg.regime_relaxed_boost
    if mainline_score >= 3.0:
        out["sort_weight_bonus"] *= 1.12
    elif mainline_score >= 1.0:
        out["sort_weight_bonus"] *= 1.06
    elif mainline_score <= -2.0:
        out["sort_weight_bonus"] *= 0.84
    if board_beta >= cfg.board_beta_core_min:
        out["sort_weight_bonus"] *= 1.08
    elif board_beta >= cfg.board_beta_hot_min:
        out["sort_weight_bonus"] *= 1.03
    elif board_beta <= cfg.board_beta_cold_max:
        out["sort_weight_bonus"] *= 0.92
    if sector_strength >= 1.10:
        out["sort_weight_bonus"] *= 1.12
    elif sector_strength >= 1.03:
        out["sort_weight_bonus"] *= 1.06
    elif sector_strength <= 0.95:
        out["sort_weight_bonus"] *= 0.88
    if sector_total >= 3 and sector_rank > 0:
        if sector_rank <= 2:
            out["sort_weight_bonus"] *= 1.10
        elif sector_rank <= 5:
            out["sort_weight_bonus"] *= 1.05
        elif sector_rank > sector_total - 3:
            out["sort_weight_bonus"] *= 0.90
    if open_confirm_hit:
        out["sort_weight_bonus"] *= 1.05
    if t1_n >= cfg.t1_memory_min_samples:
        if t1_memory_score > 0:
            out["sort_weight_bonus"] *= min(1.15, 1.0 + t1_memory_score / 100.0)
        elif t1_memory_score < 0:
            out["sort_weight_bonus"] *= max(0.88, 1.0 + t1_memory_score / 120.0)
    if vwap_gap_pct > 1.8:
        out["sort_weight_bonus"] *= 0.90
    elif vwap_gap_pct < -0.8:
        out["sort_weight_bonus"] *= 0.86

    hits: List[str] = []
    checks: Dict[str, str] = {}
    for name, fn in strat_defs:
        try:
            passed, msg = fn()
        except Exception as ex:
            logger.debug("P2 策略 %s 判定异常: %s", name, ex)
            passed, msg = False, f"异常:{ex}"
        checks[name] = msg if passed else msg
        if passed:
            hits.append(name)

    if open_confirm_hit:
        out["sort_weight_bonus"] *= 1.06
    if t1_memory_score != 0.0:
        out["sort_weight_bonus"] *= max(0.9, min(1.12, 1.0 + t1_memory_score / 100.0))

    if sector_total >= 3 and sector_rank > 0 and hits:
        if sector_rank <= 2 and any(tag in hits[0] for tag in ("P2-01", "P2-04", "P2-02")):
            out["sort_weight_bonus"] *= 1.05
        elif sector_rank > sector_total - 3:
            out["sort_weight_bonus"] *= 0.95

    if mainline_score >= 3.0 and hits:
        if any(tag in hits[0] for tag in ("P2-01", "P2-02", "P2-04")):
            out["sort_weight_bonus"] *= 1.04

    if sector_strength >= 1.10 and hits:
        hit_primary = any(tag in hits[0] for tag in ("P2-01", "P2-04", "P2-02"))
        if hit_primary:
            out["sort_weight_bonus"] *= 1.05

    # V26.88+9-22 优化4：HiThink 涨停板龙头溢价 → P2 竞价排序加权
    # P2 是竞价池，核心信号是「昨日涨停股今日竞价表现」。
    # 连板龙头（≥3板）高开本身就是全市场最强信号之一，给 sort_weight_bonus +1.15 乘数。
    # 数据来源：rt 传入的 _pre_limit_up（由 build_p1_pool_and_cache / screen_p2_universe 预加载）
    _limit_up = rt.get("_pre_limit_up") or {}
    if _limit_up and hits:
        try:
            _cnt = int(_limit_up.get("continue_day_cnt", 1) or 1)
        except (TypeError, ValueError):
            _cnt = 1
        try:
            _seal_yi = float(_limit_up.get("seal_money", 0) or 0) / 1e8
        except (TypeError, ValueError):
            _seal_yi = 0.0
        # 龙头连板溢价（连板≥3 且封单≥1亿 → 强溢价；连板=2 → 温和溢价）
        if _cnt >= 3 and _seal_yi >= 1.0:
            out["sort_weight_bonus"] *= 1.15
            _lu_tag = f"[龙头{_cnt}板(封{_seal_yi:.1f}亿)]"
            hits.append(_lu_tag)
        elif _cnt >= 2 or _seal_yi >= 5.0:
            out["sort_weight_bonus"] *= 1.08
            _seal_label = f"封{_seal_yi:.1f}亿" if _seal_yi > 0 else f"{_cnt}板"
            _lu_tag = f"[连板{_cnt}({_seal_label})]"
            hits.append(_lu_tag)

    out["strategies"] = hits
    out["strategy_checks"] = checks
    out["p2_core_screener_pass"] = len(hits) > 0
    out["detail"] = {
        "circ_mv_wan": circ_mv,
        "sort_weight_bonus": out["sort_weight_bonus"],
        "regime_bucket": regime_bucket,
        "board_beta": round(board_beta, 3),
        "sector_strength": round(sector_strength, 3),
        "sector_rank": int(sector_rank if sector_rank != 999 else 999),
        "sector_total": int(sector_total),
        "mainline_score": round(mainline_score, 2),
        "mainline_reason": mainline_reason,
        "t1_avg_ret_pct": round(t1_avg, 3),
        "t1_win_rate_pct": round(t1_win, 1),
        "t1_sample_n": int(t1_n),
        "t1_memory_score": round(t1_memory_score, 2),
        "open_confirm_hit": bool(open_confirm_hit),
        "y_trade_date": str(y.get("trade_date", "")),
    }
    return out


def screen_p2_universe(
    rows: List[Tuple[str, pd.DataFrame, Dict[str, Any]]],
    cfg: Optional[P2ScreenerConfig] = None,
    limit_up_map: Optional[Dict[str, Dict[str, Any]]] = None,
) -> pd.DataFrame:
    """
    批量增量筛选（一日、全市场）。

    参数:
        rows: [(ts_code, df_hist, rt), ...]
            每只股票仅携带自身历史与当日 rt，不依赖全表。
        limit_up_map: V26.88+9-22 可选：HiThink 涨停池信号 dict，
            格式同 p1_precompute.batch_precompute_limit_up_signals 的返回值。
            若不传，screen_p2_universe 内部尝试从 HiThink 加载（HiThink 不可用时静默跳过）。

    返回:
        pandas.DataFrame，便于导出 CSV / 写库；列含 ts_code、是否否决、命中策略等。
    """
    # V26.88+9-22 优化4：未传 limit_up_map 时自动从 HiThink 加载
    if limit_up_map is None:
        try:
            from core.p1_precompute import batch_precompute_limit_up_signals
            _fake_items = [{"code": code} for code, _, _ in rows]
            limit_up_map = batch_precompute_limit_up_signals(_fake_items)
        except Exception:
            limit_up_map = {}

    cfg = _resolve_p2_cfg(cfg)
    rec: List[Dict[str, Any]] = []
    for ts_code, df, rt in rows:
        # 注入涨停信号（供 evaluate_p2_screener 读取 rt["_pre_limit_up"]）
        if limit_up_map and ts_code in limit_up_map:
            rt["_pre_limit_up"] = limit_up_map[ts_code]
        r = evaluate_p2_screener(df, rt, cfg)
        rec.append(
            {
                "ts_code": ts_code,
                "veto_pass": r["veto_pass"],
                "veto_reason": r["veto_reason"],
                "strategies": "|".join(r["strategies"]),
                "p2_core_screener_pass": r["p2_core_screener_pass"],
                "sort_weight_bonus": r["sort_weight_bonus"],
            }
        )
    return pd.DataFrame(rec)
