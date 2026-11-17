# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 — P5 盘后选股池「物理胸甲」向量化筛选模块（防镰刀增强版）
================================================================================
【V26.88】防镰刀优化：尾盘无量拉升、T+1收割、全天回落尾拉跟风检测
【V26.88】资金条件：向量化 max(流通市值×比例, 阶梯地板)；换手 100% 使用 _turnover_f_eff（仅 turnover_rate_f 或手数反算）。
禁止 net_* / inst 单一绝对万元门槛；禁止 turnover_rate 降级。
================================================================================
【V35 战法编号重整 2026-11】A/B/C 档位字母与聚合别名全部取消，26 个原子战法重排为 P5-01~P5-25
（P5-00·◆VWAP分时防伪 为防伪降权 mask，不是战法）。旧→新对照（供历史 signal_log 复盘）：
  旧02A→P5-01 量价趋势   旧02B→P5-02 量价放量    旧02C→P5-03 量价确认
  旧03A→P5-04 资金方向   旧03B→P5-05 资金质量    旧03C→P5-06 资金确认
  旧05A→P5-07 趋势结构   旧05B→P5-08 趋势质量    旧05C→P5-09 趋势确认
  旧01 →P5-10 核心四因子共振  旧04→P5-11 单峰密集突破  旧06→P5-12 机构龙虎缩量回踩
  旧08 →P5-13 超级中军点火    旧20→P5-14 欧奈尔趋势模板
  旧12A→P5-15 箱体突破   旧12B→P5-16 箱体回踩
  旧13A→P5-17 均线粘合   旧13B→P5-18 均线发散起点   旧13C→P5-19 均线发散确认
  旧14 →P5-20 缩量分歧低吸    旧15→P5-21 高位旗形蓄势
  旧16 →P5-22 双均线金叉      旧17→P5-23 布林上轨突破   旧18→P5-24 量价新高突破
  旧19 →P5-25 MACD金叉趋势
  旧02/03/05/12/13 聚合别名（与档位 mask 重复）删除；旧07 外资连续建仓（北向数据源下线、
  hk_vol 恒 0，自上线起命中恒 0）删除。
================================================================================
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

from core.strategies.fund_mv_utils import (
    infer_turnover_rate_f_pct,
    vector_dynamic_inst_sum3_threshold,
    vector_dynamic_net_main_threshold,
    vector_dynamic_strth_threshold_wan,
    vector_effective_turnover_rate_f,
    vector_nm_fly_negative_threshold,
)

logger = logging.getLogger(__name__)

try:
    import constants as _C_P5
    _P1_MIN_CIRC_MV_WAN = float(getattr(_C_P5, "P1_SELECT_MIN_CIRC_MV_WAN", 1_000_000))
except Exception:
    _P1_MIN_CIRC_MV_WAN = 1_000_000.0


# 【V34 P0】非战法 mask：只用于降权/防伪，绝不能算「命中战法」。
# 单票路径与面板路径必须共用这份名单——原实现面板路径显式剔除，单票路径却把它加进 hits，
# 导致「只有 VWAP 偏离、零战法」的票也能 p5_core_screener_pass=True（已实测复现）。
_P5_NON_STRATEGY_MASKS = ("P5-00·◆VWAP分时防伪",)


@dataclass
class P5PostmarketConfig:
    """P5 阈值：比例为占流通市值(元)的比例；strth 比例为占 circ_mv(万) 的比例。"""

    # 数据完整度门
    strict_data_quality: bool = True

    # 市场状态阈值
    risk_off_contraction_score: float = 0.95
    risk_off_limit_down_count: int = 60
    risk_off_breadth_pct_max: float = 0.35

    # 反接飞刀阈值（与策略层共享同一道闸门）
    anti_fly_prev_limit_up_pct: float = 9.5
    anti_fly_today_chase_pct: float = 3.5
    anti_fly_today_max_pct: float = 8.0
    anti_fly_upper_shadow_pct: float = 0.04
    anti_fly_vwap_dev_pct: float = 0.6
    anti_fly_overheat_bias_pct: float = 5.0
    anti_fly_volume_ratio_max: float = 1.2
    anti_fly_dump_turnover_pct: float = 8.0
    anti_fly_dump_pct_max: float = 1.0
    anti_fly_sector_beta_min: float = 0.9

    global_bias_high: float = 10.0
    global_bias_low: float = -15.0

    s1_winner_min: float = 85.0
    s1_vr_min: float = 1.6
    s1_pct_low: float = 4.0
    s1_pct_high: float = 6.0
    s1_upper_shadow_ratio_fly_gt: float = 0.04

    s2_winner_min: float = 80.0
    s2_vol_over_vma5_mult: float = 2.0
    s2_cost95_to_close_ratio_fly_lt: float = 1.05

    s3_net_main_ratio_of_float_mv: float = 0.0004
    s3_vol_over_vma5_fly_mult: float = 3.0

    s4_winner_min: float = 90.0
    s4_ma20_slope_min: float = 1.0
    s4_vol_over_vma5_max_mult: float = 1.5

    # ---------- 策略五：绝对趋势雷达（★修复 high_20 含当日死条件后新增的「接近新高」容差）----------
    # 收盘价距 20 日高点不超过该百分比即视为「接近新高」。实测 2% 口径 5/10 日超额 +0.12%/+0.09%，
    # 优于真突破（-0.83%/-1.47%）。0 表示严格站上 20 日高点（含当日），可用于回退对比。
    s5_near_high_gap_pct: float = 2.0

    s5_circ_mv_fly_lt_wan: float = 500000.0

    s6_inst_sum3_ratio_of_float_mv: float = 0.00012
    # ---------- 策略六：机构龙虎缩量回踩 ----------
    # 【上线前修复 2026-10-18】修正默认值倒挂。
    # 原默认 s6_pct_low=2.5 > s6_pct_high=0.0 ⇒ pct >= 2.5 & pct <= 0.0 恒为 False，
    # 只要 config.yaml 缺这两个键，P5-12（机构龙虎缩量回踩）就静默恒假（生产配置正好是 -3.0/0.0）。
    # 默认值必须与生产口径一致：回踩 = 当日小跌或平盘，故 low<high。
    s6_pct_low: float = -3.0
    s6_pct_high: float = 0.0
    s6_vol_under_vma5_ratio: float = 0.8
    s6_net_main_fly_ratio_of_float_mv: float = 0.00035

    s7_pct_low: float = -2.0
    s7_pct_high: float = 2.5
    s7_pe_max: float = 30.0
    s7_circ_mv_min_wan: float = _P1_MIN_CIRC_MV_WAN

    s8_circ_mv_min_wan: float = _P1_MIN_CIRC_MV_WAN
    s8_circ_mv_max_wan: float = 5_000_000.0
    s8_net_main_ratio_of_float_mv: float = 0.00028
    s8_turnover_f_low: float = 3.6
    s8_turnover_f_high: float = 14.0

    # ---------- 策略十二：箱体突破回踩（★右侧优化：涨幅下限收紧至3.0%，必须是右侧突破）----------
    s12_break_min_pct: float = 0.45
    s12_ma20_touch_max_mult: float = 1.018
    s12_pct_low: float = 3.0  # 【优化】原-1.2→3.0，必须是右侧上攻
    s12_pct_high: float = 4.8
    s12_box_range_min: float = 0.055
    s12_box_range_max: float = 0.36
    s12_winner_min: float = 76.0
    s12_vr_min: float = 1.05

    # ---------- 策略十三：均线粘合发散（三日前短均线粘合 + 当日发散向上 + MACD 红柱）（★右侧优化：涨幅下限收紧至0.5%）----------
    s13_cohesion_max_pct: float = 2.70
    s13_spread_min_pct: float = 2.15
    s13_ma20_slope_min: float = 0.55
    s13_pct_low: float = 0.5  # 【优化】原-0.8→0.5，确保是右侧上攻
    s13_pct_high: float = 6.2
    s13_winner_min: float = 74.0
    s13_vr_min: float = 1.05

    # ---------- 策略十五：高位旗形蓄势（HighTightFlag，源自 Sequoia-X）----------
    # 定位：40 日强动量 + 近 10 日极度收敛 + 高位抗跌 + 缩量锁定，捕捉「突破前蓄势态」。
    # 与 P5-15 箱体突破的区别：P5-15 要求已突破且当日涨幅 3~4.8%；本策略（P5-21）要求尚未突破、当日缩量。
    # 参数经 94 日底池内 6808 样本扫描确定：
    #   1.3 / 1.25 / 缩量 0.7 → 5日 +0.92%、10日 +1.29%（超额），日均约 5 只
    #   缩量阈值越严格超额越高但样本越少；0.7 为收益/覆盖率平衡点。
    s15_momentum: float = 1.30        # 40 日 high/low 动量下限
    s15_consolidation: float = 1.25   # 10 日 high/low 收敛上限
    s15_high_level_ratio: float = 0.80  # 近 10 日最低价 >= 40 日最高价 × 0.8（高位抗跌）
    s15_shrink_vol_mult: float = 0.70   # 今日量 < 前 20 日均量 × 0.7（缩量，不含当日）
    s15_min_bars: int = 40             # 需要 40 根 K 线，新股掩码据此排除

    # ---------- 【V34+ 新增】策略十六~二十（现编号 P5-22 ~ P5-25 / P5-14）：源自 _ext_repos 外部策略移植（配套性已逐条审计）----------
    # P5-22 双均线金叉（ai_quant_trade DualMovingAverage 5/20：MA5 上穿 MA20 当日成立）
    s16_pct_low: float = 0.5          # 金叉日涨幅下限（太弱视为假金叉）
    s16_pct_high: float = 6.0         # 涨幅上限（不追极端拉高）
    s16_vr_min: float = 1.0           # 量比确认（缩量金叉不追）
    s16_winner_min: float = 74.0      # winner_rate 地板（缺失不误杀）
    # P5-23 布林上轨突破（ai_quant_trade BollingerBreakout：20 日 ±2σ 固定口径，与源一致）
    s17_pct_low: float = 0.5
    s17_pct_high: float = 6.0
    s17_vol_ratio_min: float = 1.0
    s17_winner_min: float = 72.0
    # P5-24 量价新高突破（ai_quant_trade MomentumVolumeBreakout：收盘 > 前20日最高 + 量能确认）
    s18_vol_mult: float = 1.2         # 今日量 ≥ 前 20 日均量 × 1.2
    s18_pct_high: float = 7.0         # 涨幅上限（防涨停追高；涨停另走 P5 封板逻辑）
    # P5-25 MACD 金叉趋势（ai_quant_trade MACDTrend 12/26/9：DIF 上穿 DEA 当日）
    s19_pct_low: float = -1.0
    s19_pct_high: float = 5.5
    s19_winner_min: float = 70.0
    # 仅认零轴上方金叉（源策略不限位置；实测平静市微金叉产生噪音，系统要求右侧）
    s19_require_above_zero: bool = True
    # P5-14 欧奈尔趋势模板（ai_quant_trade 规则选股 CANSLIM 趋势模板；RPS 条件因向量化引擎
    # 无横截面 RPS 列而省略，其余 7 项全部落地；52 周高低点用窗口内 250 日近似，
    # 窗口不足 120 根时 fail-closed 不出信号）
    s20_above_low_min_pct: float = 30.0   # 现价 ≥ 52 周低点 × 1.3
    s20_from_high_max_pct: float = 25.0   # 现价距 52 周高点 ≤ 25%
    s20_pct_low: float = -2.0
    s20_pct_high: float = 6.5

    # ---------- P5 爆发分专用：均线动能补偿（仅 strat_p5_postmarket.run_all 使用；不改 P1、不做硬否决）----------
    # 健康乖离带 [3%,12%] 仅作文档/话术参考，代码中恒为乘子 1.0，避免与斜率奖励重复加权。
    enable_ma_compensation: bool = True
    ma_bias_overheat_pct: float = 18.0
    circ_mv_wan_large_min: float = 3_000_000.0
    circ_mv_wan_mid_min: float = _P1_MIN_CIRC_MV_WAN
    ma_overheat_mult_large: float = 0.85
    ma_overheat_mult_mid: float = 0.95
    ma_slope_reward_threshold_pct: float = 0.8
    ma_slope_reward_mult_min: float = 1.25
    ma_slope_reward_mult_max: float = 1.4
    ma_slope_reward_interp_high_pct: float = 2.0

    # ---------- 分时结构防伪：VWAP 惩罚（盘后静态日线的最后一道防线）----------
    enable_vwap_penalty: bool = True
    vwap_dev_soft_pct: float = 1.8
    vwap_dev_hard_pct: float = 3.5
    vwap_tail_spike_pct: float = 1.2
    vwap_tail_spike_mult: float = 0.72
    vwap_hard_mult: float = 0.55
    vwap_tail_minutes: int = 15

    # ---------- 【V26.88 P5 优化】2026 慢牛格局专项 ----------
    # 战略赛道列表：硬科技（电子/计算机/通信/机械/军工/电力/汽车/医药）等机构主导赛道，
    # 命名与 pool_manager.STRATEGIC_INDUSTRIES 同步；None 时不启用战略赛道识别。
    strategic_industries: Optional[List[str]] = None
    # 【上线前修复 2026-10-18】北向资金 3 日评分配置已退役（hk_vol 全库恒为 0）。
    # 保留占位仅为兼容历史 config.yaml 的多余键，运行时不再被任何策略读取。
    # 战略赛道加分：核心维度命中的 burst_score 额外加分（硬科技溢价）
    strategic_burst_bonus: float = 2.0
    strategic_cross_bonus: float = 4.0
    # winner_rate 机构地板：策略命中后，若 winner_rate < 此值则在 burst 输出上做软扣
    winner_floor: float = 80.0
    winner_floor_penalty: float = 5.0
    # 【V34 新增】burst 上下界：原实现 8 处硬编码 np.clip(..., 55, 100)，
    # 等于「任何进池票 burst 至少 55」，跨池排序天然占优，且 template_label 的 <40 分支永不触发。
    # 默认 0 表示回到诚实刻度；设回 55 可恢复旧行为。
    p5_burst_floor: float = 0.0
    p5_burst_ceil: float = 100.0
    # 【V34 新增】仓位阶梯参数（盘后信息最全，可略高于盘中池；但观察档必须给 0）
    p5_base_position_ratio: float = 0.10
    p5_max_position_ratio: float = 0.20
    p5_min_position_ratio: float = 0.04
    p5_score_scale_floor: float = 45.0
    p5_score_scale_ceiling: float = 85.0
    # 【V34 新增】盈利质量阈值（原 _fin_quality_score_p4 把阈值硬编码，配置键全部是死键）
    fin_score_qoq_yoy_bonus_threshold: float = 25.0
    fin_score_roe_strong: float = 12.0
    fin_score_gross_margin_strong: float = 25.0
    fin_score_peg_attractive: float = 0.6
    fin_score_qoq_yoy_penalty_threshold: float = -30.0
    # 战略赛道宽容开关
    strategic_pct_loosen: bool = True
    strategic_pct_high_loose: float = 6.5
    strategic_bias_loosen: bool = True
    strategic_bias_high_loose: float = 14.0
    strategic_trend_loosen: bool = True


def _resolve_p5_cfg(cfg: Optional[P5PostmarketConfig]) -> P5PostmarketConfig:
    if cfg is not None:
        return cfg
    from core.config_manager import get_p5_postmarket_config

    return get_p5_postmarket_config()


DEFAULT_P5_CONFIG = P5PostmarketConfig()


# ====================== 【V26.88 P5 优化】辅助函数 ======================

def _is_strategic_industry(
    rt: Optional[Dict[str, Any]] = None,
    row: Optional[pd.Series] = None,
    cfg: Optional["P5PostmarketConfig"] = None,
) -> bool:
    """
    【V26.88 P5 优化①】判断当前股票是否属于战略赛道（电子/计算机/通信/机械/军工等）。
    来源：rt["industry"] 或 row["industry"]，大小写不敏感匹配 cfg.strategic_industries 列表。
    """
    cfg = _resolve_p5_cfg(cfg)
    items = cfg.strategic_industries or []
    if not items:
        return False
    industry = ""
    if isinstance(rt, dict):
        industry = str(rt.get("industry") or rt.get("industry_name") or "")
    if not industry and row is not None:
        try:
            industry = str(row.get("industry") or "")
        except Exception:
            industry = ""
    if not industry:
        return False
    industry_norm = industry.strip()
    return industry_norm in {str(x).strip() for x in items}


# ======================================================================


def _ensure_numeric(df: pd.DataFrame, cols: List[str]) -> None:
    for c in cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")


def _macd_hist_col(df: pd.DataFrame) -> pd.Series:
    """
    MACD 柱（1× 统一口径，批量）。

    【V30.10 统一 MACD 口径 2026-10-21】原实现优先 macd_hist（1×）、
    缺失时 fillna(macd_bar)（2×），把两种尺度混进同一列，
    与绝对值阈值比较时宽严差一倍。
    现统一走 core.strategies.macd_unified.macd_hist_series：
      macd_hist(1×) → macd_bar/2(2×折算) → (macd - macd_signal) 现算
    """
    try:
        from core.strategies.macd_unified import macd_hist_series

        s = macd_hist_series(df)
        if s is not None:
            return s
    except Exception:
        pass
    if "macd_hist" in df.columns:
        h = pd.to_numeric(df["macd_hist"], errors="coerce")
    else:
        h = pd.Series(np.nan, index=df.index, dtype=float)
    if "macd_bar" in df.columns:
        b = pd.to_numeric(df["macd_bar"], errors="coerce")
        h = h.fillna(b)
    return h


def _bias_20_series(df: pd.DataFrame) -> pd.Series:
    if "bias_20" in df.columns:
        return pd.to_numeric(df["bias_20"], errors="coerce")
    c = pd.to_numeric(df["close"], errors="coerce")
    m20 = pd.to_numeric(df["ma20"], errors="coerce")
    return np.where(m20 > 0, (c - m20) / m20 * 100.0, np.nan)


def prepare_p5_lags(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return df
    out = df.copy()
    if "ts_code" not in out.columns:
        raise ValueError("prepare_p5_lags: 缺少 ts_code 列")
    if "trade_date" not in out.columns:
        raise ValueError("prepare_p5_lags: 缺少 trade_date 列")

    out["trade_date"] = pd.to_datetime(out["trade_date"])
    out = out.sort_values(["ts_code", "trade_date"]).reset_index(drop=True)

    num_cols = [
        "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "vol_ratio", "turnover_rate_f",
        "pe_ttm", "pb", "ps_ttm", "dv_ratio", "circ_mv", "ma5", "ma10", "ma20", "ma60", "ma120", "ma250",
        "vol_ma5", "vol_ma10", "vol_ma20", "ma20_slope_5", "high_20", "low_60", "macd", "macd_signal",
        "atr_pct", "bias_20", "net_main_amount", "inst_net_buy",
        "cost_50th", "cost_95th", "winner_rate", "limit_times", "strth",
    ]
    _ensure_numeric(out, [c for c in num_cols if c in out.columns])

    tf_src = out["turnover_rate_f"] if "turnover_rate_f" in out.columns else pd.Series(np.nan, index=out.index)
    out["_turnover_f_eff"] = vector_effective_turnover_rate_f(
        out["vol"], out["close"], out["circ_mv"], tf_src
    )

    out["_mh"] = _macd_hist_col(out)

    g = out.groupby("ts_code", sort=False)
    out["nm_lag1"] = g["net_main_amount"].shift(1)
    out["nm_lag2"] = g["net_main_amount"].shift(2)
    out["macd_hist_prev"] = g["_mh"].shift(1)
    out["vol_ma5_prev"] = g["vol_ma5"].shift(1)
    out["vol_ma20_prev"] = g["vol_ma20"].shift(1)
    out["inst_net_buy_sum3"] = g["inst_net_buy"].transform(lambda s: s.rolling(3, min_periods=3).sum())
    out["ma60_lag5"] = g["ma60"].shift(5)
    if "high_20" in out.columns:
        out["close_lag1"] = g["close"].shift(1)
        out["high_20_lag2"] = g["high_20"].shift(2)
    # ---------------------------------------------------------------------
    # 【V26.88+10-5 修复 P0 BUG】high_20 含当日 → close > high_20 恒为 False
    #
    # 落库 high_20 = rolling(20).max()，**包含当日**，因此恒有
    #     high_20 >= high >= close   ⇒   close > high_20 永不成立
    # 这使趋势结构 / 趋势质量 / 趋势确认（旧05A/05B/05C，现 P5-07/08/09）自上线起命中数恒为 0（已实测验证）。
    #
    # 新增两个语义明确的列，禁止再用 high_20 做「突破」判断：
    #   high_20_prev : 前 20 个交易日（不含当日）最高价 —— 真突破基准
    #   high_20_rel  : high_20 / close - 1，>0 表示收盘价在 20 日高点之下多少
    #                  （P5-07 改用此列做「接近新高」判定，见 s5_near_high_gap_pct）
    if "high" in out.columns:
        out["high_20_prev"] = g["high"].shift(1).rolling(20, min_periods=20).max()
        out["low_20_prev"] = g["low"].shift(1).rolling(20, min_periods=20).min()
    if "high_20" in out.columns:
        _h20 = pd.to_numeric(out["high_20"], errors="coerce")
        out["high_20_rel"] = _h20 / pd.to_numeric(out["close"], errors="coerce").replace(0, np.nan) - 1.0

    # ---------------------------------------------------------------------
    # 【V26.88+10-5 新增】P5-21 高位旗形（HighTightFlag）所需滚动列
    # 全部严格 min_periods=n（预热不足即 NaN→False），并提供 *_prev 口径避免当日自比较。
    # ---------------------------------------------------------------------
    if {"high", "low", "vol"}.issubset(out.columns):
        _g = out.groupby("ts_code", sort=False)
        out["high_40"] = _g["high"].transform(lambda s: s.rolling(40, min_periods=40).max())
        out["low_40"] = _g["low"].transform(lambda s: s.rolling(40, min_periods=40).min())
        out["high_10"] = _g["high"].transform(lambda s: s.rolling(10, min_periods=10).max())
        out["low_10"] = _g["low"].transform(lambda s: s.rolling(10, min_periods=10).min())
        out["vol_ma20_prev"] = _g["vol_ma20"].shift(1) if "vol_ma20" in out.columns else np.nan
    if all(c in out.columns for c in ("ma5", "ma10", "ma20")):
        out["ma5_lag3"] = g["ma5"].shift(3)
        out["ma10_lag3"] = g["ma10"].shift(3)
        out["ma20_lag3"] = g["ma20"].shift(3)

    # ---------------------------------------------------------------------
    # 【V34+ 新增】外部策略移植（现 P5-22 ~ P5-25 / P5-14）所需滚动列
    #   ma5_prev_c / ma20_prev_c     : 双均线金叉的「前一日」基准（P5-22）
    #   boll_mid / boll_up           : 布林带 20 日 / 2σ 上轨（P5-23，口径与源策略一致）
    #   high_20_prev 已有            : 量价新高突破基准（P5-24）
    #   macd_prev / macd_signal_prev : MACD 金叉的前一日基准（P5-25）
    #   high_250w / low_250w / ma250_lag20 : 欧奈尔趋势模板（P5-14）。
    #     生产底仓窗口约 130 根，250 日滚动以 min_periods=120 近似「52 周」，
    #     窗口不足 120 根时为 NaN → fail-closed（不出信号），不拿短窗口冒充 52 周。
    # ---------------------------------------------------------------------
    if "ma5" in out.columns:
        out["ma5_prev_c"] = g["ma5"].shift(1)
    if "ma20" in out.columns:
        out["ma20_prev_c"] = g["ma20"].shift(1)
    if "macd" in out.columns:
        out["macd_prev"] = g["macd"].shift(1)
    if "macd_signal" in out.columns:
        out["macd_signal_prev"] = g["macd_signal"].shift(1)
    if {"high", "low"}.issubset(out.columns):
        _g250 = out.groupby("ts_code", sort=False)
        out["high_250w"] = _g250["high"].transform(lambda s: s.rolling(250, min_periods=120).max())
        out["low_250w"] = _g250["low"].transform(lambda s: s.rolling(250, min_periods=120).min())
    if "ma250" in out.columns:
        out["ma250_lag20"] = g["ma250"].shift(20)
    if "close" in out.columns:
        _gc = out.groupby("ts_code", sort=False)["close"]
        _bm = _gc.transform(lambda s: s.rolling(20, min_periods=20).mean())
        _bs = _gc.transform(lambda s: s.rolling(20, min_periods=20).std(ddof=0))
        out["boll_mid"] = _bm
        out["boll_up"] = _bm + 2.0 * _bs

    out["_bias_live"] = _bias_20_series(out)

    if "vwap" not in out.columns:
        out["vwap"] = np.nan
    if "tail_close" not in out.columns:
        out["tail_close"] = np.nan
    if "tail_vwap" not in out.columns:
        out["tail_vwap"] = np.nan
    if "tail_volume" not in out.columns:
        out["tail_volume"] = np.nan

    return out


def p5_limit_burst_veto(rt: Optional[Dict[str, Any]], trade_date: Any = None) -> Tuple[bool, int, float]:
    """【V34 P2-3】HiThink 炸板池硬否决：全链路唯一实现，且带交易日作用域。

    原实现引擎与单票函数各查一遍、且只判 `_hithink_limit_burst` 非空 ——
    payload 未按交易日清理时会持续全池否决。这里优先用 payload 自带交易日与扫描日比对；
    payload 缺日期时按「当前有效」处理（向后兼容），但判断只剩这一处，不会再分叉。
    返回 (是否否决, 回开次数, 换手%)。
    """
    payload = (rt or {}).get("_hithink_limit_burst") or {}
    if not payload:
        return False, 0, 0.0

    def _norm_date(x: Any) -> str:
        try:
            s = str(x or "").strip()
            digits = "".join(ch for ch in s if ch.isdigit())
            return digits[:8]
        except Exception:
            return ""

    _p_date = _norm_date(
        payload.get("trade_date") or payload.get("date") or payload.get("d") or payload.get("dt")
    )
    _s_date = _norm_date(trade_date)
    if _p_date and _s_date and _p_date != _s_date:
        return False, 0, 0.0

    _open = int(_to_int_safe(payload.get("open_times"), 0) or 0)
    _to = _to_float_safe(payload.get("turnover_ratio_pct"), 0.0)
    return True, _open, float(_to if np.isfinite(_to) else 0.0)


def p5_global_pass_mask(df: pd.DataFrame, cfg: Optional[P5PostmarketConfig] = None) -> pd.Series:
    cfg = _resolve_p5_cfg(cfg)
    if df is None or df.empty:
        return pd.Series(dtype=bool)
    required_cols = ["close", "ma60", "ma20_slope_5"]
    if any(c not in df.columns for c in required_cols):
        return pd.Series(False, index=df.index)
    close = pd.to_numeric(df["close"], errors="coerce")
    ma60 = pd.to_numeric(df["ma60"], errors="coerce")
    slope = pd.to_numeric(df["ma20_slope_5"], errors="coerce")
    v1 = (close > ma60) & (slope > 0)
    v1 = v1.fillna(False)

    bias = df["_bias_live"] if "_bias_live" in df.columns else _bias_20_series(df)
    v2 = (bias <= cfg.global_bias_high) & (bias >= cfg.global_bias_low)
    v2 = v2.fillna(False)

    # 【V34 P2-6】pe 缺失不再默认放行：原式 ~(pe < 0) 对 NaN 取 True，
    # 亏损票只要缺字段反而通过闸门。现要求 pe 有值且 >= 0。
    pe = pd.to_numeric(df["pe_ttm"], errors="coerce")
    v3 = pe.notna() & (pe >= 0)

    nm0 = pd.to_numeric(df["net_main_amount"], errors="coerce")
    nm1 = pd.to_numeric(df["nm_lag1"], errors="coerce")
    nm2 = pd.to_numeric(df["nm_lag2"], errors="coerce")
    three_neg = nm0.notna() & nm1.notna() & nm2.notna() & (nm0 < 0) & (nm1 < 0) & (nm2 < 0)
    v4 = ~three_neg

    mask = v1 & v2 & v3 & v4
    # 【V34 P2-5】回测/实盘闸门统一：原式在 is_backtest_legacy_mode() 下额外叠 cci>=100 与
    # nineturn<9 ⇒ 回测样本与实盘走的不是同一道闸门，回测胜率不可外推。现两条路径共用同一 mask。
    return mask


def _base_ok(df: pd.DataFrame) -> pd.Series:
    c = pd.to_numeric(df["close"], errors="coerce")
    return c.notna() & (c > 0)


def _to_float_safe(val: Any, default: float = np.nan) -> float:
    try:
        x = float(pd.to_numeric(val, errors="coerce"))
        return x if np.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _to_int_safe(val: Any, default: int = 0) -> int:
    try:
        x = int(float(pd.to_numeric(val, errors="coerce")))
        return x
    except Exception:
        return int(default)


def _is_new_stock_mask_p5(df: pd.DataFrame, lookback_days: int = 60) -> pd.Series:
    """
    【V26.88+10-1 P1-2 新增】新股白名单掩码。

    返回 True 表示该票属于"上市 < lookback_days 日"的新股。
    调用方在需要历史形态的策略（如箱体 P5-15/16、均线 P5-17~19）上取反（~new_stock_mask）排除新股。

    数据来源（按优先级）：
    1) df["list_date"] 列（StockBasic 落库后派生；格式 YYYYMMDD 或 YYYY-MM-DD）
    2) df["list_days"] 列（直接给出的上市天数）
    3) 缺失列时全 False（视为全部非新股，让后续 NaN 检查处理）
    """
    if df is None or df.empty:
        return pd.Series(dtype=bool)

    if "list_days" in df.columns:
        ld = pd.to_numeric(df["list_days"], errors="coerce")
        mask = (ld.notna()) & (ld < float(lookback_days))
        return mask.fillna(False)

    if "list_date" in df.columns and "trade_date" in df.columns:
        ld_raw = pd.to_numeric(
            df["list_date"].astype(str).str.replace(r"[^0-9]", "", regex=True).str[:8],
            errors="coerce",
        )
        td_raw = pd.to_numeric(
            df["trade_date"].astype(str).str.replace(r"[^0-9]", "", regex=True).str[:8],
            errors="coerce",
        )
        days = (td_raw - ld_raw) / 10000.0  # YYYYMMDD - YYYYMMDD ≈ 天数
        mask = (days.notna()) & (days < float(lookback_days))
        return mask.fillna(False)

    # 缺列时不过滤（保持原行为）
    return pd.Series(False, index=df.index)


def _p5_required_quality_mask(df: pd.DataFrame, cfg: P5PostmarketConfig) -> pd.Series:
    """数据完整度门：缺少关键字段就视为不可执行候选，不允许降级为通过。"""
    required = [
        "close",
        "pre_close",
        "open",
        "high",
        "low",
        "vol",
        "vol_ratio",
        "ma5",
        "ma20",
        "ma60",
        "ma20_slope_5",
    ]
    if not bool(cfg.strict_data_quality):
        return pd.Series(True, index=df.index)
    if df is None or df.empty:
        return pd.Series(dtype=bool)
    return df.reindex(columns=required).notna().all(axis=1)


# ======================================================================
# 【Tier1 · 1.2 2026-11-02】P5「反接飞刀 / 市场状态」这条支线【未启用】
# ----------------------------------------------------------------------
# 现状（已实测）：下面三个函数在 P5 主链路上【没有任何调用点】——
#   _p5_market_regime()          : 判定 risk_off / repair / trend / rotation
#   _p5_anti_fly_mask()          : 接飞刀形态拒绝 + 关键字段缺失拒绝
#   _p5_required_quality_mask()  : 只被 _p5_anti_fly_mask 调用（连带未启用）
# 对应 config 里 10 个 anti_fly_* 键因此也全部未生效。
#
# 处理方式：**保持不接线**（红线：不改策略逻辑/阈值），但在这里显式声明，
# 避免再出现「能调、但不生效」的静默状态。任何 UI / 报表引用本清单即可提示用户。
# ======================================================================
P5_DISABLED_FEATURES = (
    "_p5_market_regime",
    "_p5_anti_fly_mask",
    "_p5_required_quality_mask",
    "cfg.anti_fly_* (10 个键)",
)


def _p5_market_regime(rt: Optional[Dict[str, Any]], cfg: P5PostmarketConfig) -> str:
    """【未启用 · Tier1-1.2】根据收盘后的盘面因子判定市场状态，用于反接飞刀/主推门槛控制。

    注意：本函数当前【不在 P5 主链路上】，改动它不会影响选股结果。
    """
    rt = rt or {}
    contraction = _to_float_safe(
        rt.get("_market_contraction_score", rt.get("market_contraction_score", 0.0)), 0.0
    )
    limit_down = _to_float_safe(
        rt.get("limit_down_count", rt.get("跌停数", 0.0)), 0.0
    )
    # breadth_pct / market_trend_score 目前无生产写入方：缺失时按「未知」处理，
    # 不能用 1.0 假装「全市场都上涨」，否则 risk_off 的宽度闸门永远不触发。
    breadth_raw = rt.get("breadth_pct", rt.get("market_breadth"))
    breadth = _to_float_safe(breadth_raw, np.nan) if breadth_raw is not None else np.nan
    breadth_risk = bool(not pd.isna(breadth) and breadth <= cfg.risk_off_breadth_pct_max)
    if (
        contraction >= cfg.risk_off_contraction_score
        or limit_down >= cfg.risk_off_limit_down_count
        or breadth_risk
    ):
        return "risk_off"
    if contraction >= 0.7:
        return "repair"
    trend_raw = rt.get("market_trend_score")
    if trend_raw is not None and _to_float_safe(trend_raw, 0.0) >= 0.65:
        return "trend"
    return "rotation"


def _p5_anti_fly_mask(
    df: pd.DataFrame,
    cfg: P5PostmarketConfig,
    rt: Optional[Dict[str, Any]] = None,
) -> Tuple[pd.Series, Dict[str, List[str]]]:
    """【未启用 · Tier1-1.2】对常见的接飞刀形态打拒绝标签，缺失关键字段也被拒绝；rt 提供弱板块等可选因子。

    注意：本函数当前【不在 P5 主链路上】，只被 _p5_anti_fly_mask 的调用方使用，而它没有调用方。
    """
    rt = rt or {}
    if df is None or df.empty:
        return pd.Series(dtype=bool), {}

    idx = df.index
    reasons: Dict[str, List[str]] = {}

    def add(mask: pd.Series, reason: str) -> None:
        for i in idx[mask.fillna(False)]:
            reasons.setdefault(i, []).append(reason)

    def series(name: str, fallback: Optional[pd.Series] = None) -> pd.Series:
        if name in df.columns:
            return pd.to_numeric(df[name], errors="coerce")
        if fallback is not None:
            return pd.to_numeric(fallback, errors="coerce")
        return pd.Series(np.nan, index=idx, dtype=float)

    close = series("close")
    pre_close = series("pre_close")
    pct = series("pct_chg")
    prev_pct = series("prev_pct_chg")
    opn = series("open")
    high = series("high")
    low = series("low")
    vwap = series("vwap")
    vol_ratio = series("vol_ratio")
    turnover = series("turnover_rate_f")
    upper_shadow = (high - np.maximum(close, opn)) / pre_close * 100.0
    day_direction = (close - opn) / opn * 100.0
    vwap_dev = (close - vwap) / vwap * 100.0

    bias = df["_bias_live"] if "_bias_live" in df.columns and "ma20" in df.columns \
        else pd.Series(np.nan, index=idx, dtype=float)
    bias = pd.to_numeric(bias, errors="coerce")

    add(prev_pct >= cfg.anti_fly_prev_limit_up_pct, "昨日涨停T+1")
    add(
        (pct >= cfg.anti_fly_today_chase_pct)
        & (pct <= cfg.anti_fly_today_max_pct)
        & (upper_shadow >= cfg.anti_fly_upper_shadow_pct),
        "长上影追高",
    )
    add((day_direction <= -1.5) & (pct >= 1.0), "全天回落尾拉")
    add(
        (vwap_dev >= cfg.anti_fly_vwap_dev_pct)
        & (pct >= cfg.anti_fly_today_chase_pct),
        "收盘偏离VWAP",
    )
    add(
        (bias >= cfg.anti_fly_overheat_bias_pct)
        & (vol_ratio <= cfg.anti_fly_volume_ratio_max)
        & (pct > 0),
        "高位缩量透支",
    )
    add(
        (turnover >= cfg.anti_fly_dump_turnover_pct)
        & (pct <= cfg.anti_fly_dump_pct_max)
        & (upper_shadow >= 0.03),
        "放量滞涨派发",
    )

    sector_beta = _to_float_safe(rt.get("sector_beta", rt.get("industry_beta", 1.0)), 1.0)
    if sector_beta < cfg.anti_fly_sector_beta_min:
        add(
            (pct >= cfg.anti_fly_today_chase_pct) & (vol_ratio < 1.8),
            "弱板块跟风",
        )

    quality = _p5_required_quality_mask(df, cfg)
    add(~quality, "关键数据缺失")

    rejected = pd.Series(False, index=idx)
    for i in reasons:
        rejected.loc[i] = True
    return rejected, reasons


def evaluate_vwap_penalty_p5(
    prep_one_stock: pd.DataFrame,
    rt: Optional[Dict[str, Any]] = None,
    cfg: Optional[P5PostmarketConfig] = None,
) -> Tuple[float, Dict[str, Any], List[str]]:
    cfg = _resolve_p5_cfg(cfg)
    rt = rt or {}
    if not bool(cfg.enable_vwap_penalty):
        return 1.0, {}, []

    warnings: List[str] = []
    patch: Dict[str, Any] = {}
    if prep_one_stock is None or prep_one_stock.empty:
        return 1.0, patch, warnings

    last = prep_one_stock.iloc[-1]
    close = _to_float_safe(last.get("close"), np.nan)
    if not np.isfinite(close) or close <= 0:
        return 1.0, patch, warnings

    vwap = _to_float_safe(last.get("vwap"), np.nan)
    tail_close = _to_float_safe(last.get("tail_close"), np.nan)
    tail_vwap = _to_float_safe(last.get("tail_vwap"), np.nan)
    tail_minutes = _to_int_safe(cfg.vwap_tail_minutes, 15)

    vwap_missing = not (np.isfinite(vwap) and vwap > 0)
    tail_vwap_missing = not (np.isfinite(tail_vwap) and tail_vwap > 0)
    if vwap_missing and tail_vwap_missing:
        patch["VWAP_数据缺失"] = True
        patch["VWAP_尾盘分钟"] = tail_minutes
        return 1.0, patch, warnings

    if vwap_missing:
        vwap = tail_vwap
        patch["VWAP_数据缺失"] = True
        patch["VWAP_尾盘分钟"] = tail_minutes
        # 只有缺少全天 VWAP、但存在尾盘 VWAP 时，记录提示但不报警，不做降权，避免全池误报。
        return 1.0, patch, warnings

    dev_pct = (close - vwap) / vwap * 100.0
    patch["VWAP_收盘偏离_pct"] = round(dev_pct, 4)
    patch["VWAP_收盘价"] = round(close, 4)
    patch["VWAP_均价"] = round(vwap, 4)

    if np.isfinite(tail_close) and np.isfinite(tail_vwap) and tail_vwap > 0:
        tail_dev_pct = (tail_close - tail_vwap) / tail_vwap * 100.0
    else:
        tail_dev_pct = np.nan
    if np.isfinite(tail_dev_pct):
        patch["VWAP_尾盘偏离_pct"] = round(tail_dev_pct, 4)
    patch["VWAP_尾盘分钟"] = tail_minutes

    m = 1.0
    hard = float(cfg.vwap_dev_hard_pct)
    soft = float(cfg.vwap_dev_soft_pct)
    tail_spike = float(cfg.vwap_tail_spike_pct)
    if np.isfinite(dev_pct) and abs(dev_pct) >= hard:
        m = float(cfg.vwap_hard_mult)
        warnings.append("📡【VWAP重锤】收盘严重偏离全天均价线，疑似尾盘画线")
    elif np.isfinite(dev_pct) and abs(dev_pct) >= soft:
        m = min(m, 0.82)
        warnings.append("📡【VWAP警惕】收盘偏离全天均价线偏大")

    if np.isfinite(tail_dev_pct) and tail_dev_pct >= tail_spike:
        m = min(m, float(cfg.vwap_tail_spike_mult))
        warnings.append("📡【尾盘脉冲】尾盘存在偏强脉冲拉升，需防次日兑现")

    if np.isfinite(dev_pct) and abs(dev_pct) >= hard and np.isfinite(tail_dev_pct) and tail_dev_pct >= tail_spike:
        m = min(m, float(cfg.vwap_hard_mult))

    return float(max(0.4, min(1.0, m))), patch, warnings


# 【V35 编号重整】全链路唯一动作清单（唯一来源，禁止任何地方再抄一份）。
# 24 项标准战法按「结构买点 → 核心 → 辅助 → 趋势族」优先级排列；
# A/B/C 档位与聚合别名已取消（与档位 mask 完全重复），每项即一个独立原子战法。
_P5_ACTION_PRIORITY: Tuple[Tuple[str, str], ...] = (
        ("P5-16·★箱体回踩", "低吸"),
        ("P5-15·★箱体突破", "追涨"),
        ("P5-19·★均线发散确认", "追涨"),
        ("P5-18·★均线发散起点", "追涨"),
        ("P5-17·★均线粘合", "观察"),
        ("P5-21·★高位旗形蓄势", "观察"),
        ("P5-20·★缩量分歧低吸", "低吸"),
        # 【V35+ 新增】外部策略移植：结构性突破/趋势确认均为追涨档，与源策略「右侧买入」语义一致
        ("P5-22·★双均线金叉", "追涨"),
        ("P5-23·★布林上轨突破", "追涨"),
        ("P5-24·★量价新高突破", "追涨"),
        ("P5-25·★MACD金叉趋势", "追涨"),
        ("P5-14·★欧奈尔趋势模板", "辅助"),
        ("P5-06·★资金确认", "核心"),
        ("P5-05·★资金质量", "核心"),
        ("P5-04·★资金方向", "核心"),
        ("P5-03·★量价确认", "核心"),
        ("P5-02·★量价放量", "核心"),
        ("P5-01·★量价趋势", "核心"),
        ("P5-12·★机构龙虎缩量回踩", "辅助"),
        ("P5-13·★超级中军点火", "辅助"),
        ("P5-10·★核心四因子共振", "辅助"),
        ("P5-11·★单峰密集突破", "辅助"),
        ("P5-09·★趋势确认", "核心"),
        ("P5-07·★趋势结构", "核心"),
        ("P5-08·★趋势质量", "核心"),
)

# 「只观察、不建仓」的档位。由上面唯一清单派生，禁止另抄。
_P5_OBSERVE_ONLY_LABEL = "观察"


def p5_hits_are_actionable(strategy_names: List[str]) -> bool:
    """【V34 P1】命中是否足以进池（= 至少一条命中属于「非观察」档）。

    【为什么不能只看 primary_action】
    最初把门禁写成 `primary_action in {追涨,低吸,核心,...}`，但 primary_action 是
    「清单里第一个命中的档位」，清单外的战法名（新增策略、或调用方自定义名）
    一律落不到任何档位 ⇒ primary_action 停在默认值「观察」⇒ 被静默挡在门外。
    实测代价：既有回归 tools/_test_release_regressions6.py U1c 直接红掉
    （桩数据返回 ['P5-03·主力回流','P5-01·趋势加速','P5-12·箱体回踩']，
    这三个名字都不在清单里，于是「有明确战法命中却不进池」）。

    现口径改为【unknown = 放行】：只显式排除清单里标注为「观察」的档，
    清单外的名字按可交易处理。宁可多放一个未知策略进池，
    也不让新增策略因为「忘了登记清单」而静默失效。
    """
    names = list(strategy_names or [])
    if not names:
        return False
    for h in names:
        matched = [lab for k, lab in _P5_ACTION_PRIORITY if k in str(h)]
        if not matched:
            # 清单外的战法名：保守放行（见 docstring）
            return True
        if any(lab != _P5_OBSERVE_ONLY_LABEL for lab in matched):
            return True
    # 所有命中都只落在观察档
    return False


def _derive_p5_action_summary(strategy_names: List[str], vwap_mult: float = 1.0, risk_tags: Optional[List[str]] = None) -> Tuple[str, List[str], str, str]:
    risk_tags = list(risk_tags or [])
    primary_action = "观察"
    secondary_actions: List[str] = []
    for key, label in _P5_ACTION_PRIORITY:
        if any(key in h for h in strategy_names):
            if primary_action == "观察":
                primary_action = label
            elif label not in secondary_actions and label != primary_action:
                secondary_actions.append(label)
    if any("VWAP" in h for h in risk_tags) or vwap_mult < 0.9:
        market_status = "分时偏弱"
        risk_level = "高"
    elif vwap_mult < 0.98:
        market_status = "分时警惕"
        risk_level = "中高"
    else:
        market_status = "正常"
        risk_level = "中"
    if any("派发" in h or "假强" in h for h in risk_tags):
        market_status = "防兑现"
        risk_level = "中高"
    return primary_action, secondary_actions, market_status, risk_level


def evaluate_shrink_divergence_mask_p5(
    prep_one_stock: pd.DataFrame,
    rt: Optional[Dict[str, Any]] = None,
    market_contraction_score: float = 0.0,
) -> bool:
    """
    【缩量分歧低吸】四步递进硬门槛：
    1) 大盘/板块处于缩量或震荡；
    2) 个股真实换手位于自身近期分位带；
    3) 获利盘/筹码未明显松动；
    4) 收盘站稳 MA20 且未破坏短均线结构。
    """
    rt = rt or {}
    if market_contraction_score < 0.7:
        return False
    if prep_one_stock is None or prep_one_stock.empty or len(prep_one_stock) < 30:
        return False
    if "_turnover_f_eff" not in prep_one_stock.columns:
        return False

    last = prep_one_stock.iloc[-1]
    close = float(pd.to_numeric(last.get("close"), errors="coerce") or 0.0)
    ma5 = float(pd.to_numeric(last.get("ma5"), errors="coerce") or 0.0)
    ma20 = float(pd.to_numeric(last.get("ma20"), errors="coerce") or 0.0)
    if min(ma5, ma20, close) <= 0:
        return False

    b20 = (close - ma20) / ma20 * 100.0
    b5 = (close - ma5) / ma5 * 100.0
    if not (-7.0 <= b20 <= 3.5):
        return False
    if not (-5.0 <= b5 <= 4.0):
        return False

    hist = prep_one_stock.tail(60)
    s_tr = pd.to_numeric(hist["_turnover_f_eff"], errors="coerce").replace(0.0, np.nan).dropna()
    if len(s_tr) < 20:
        return False
    p20 = float(s_tr.quantile(0.20))
    p60 = float(s_tr.quantile(0.60))
    if p60 <= p20 or p20 <= 0:
        return False

    recent = pd.to_numeric(prep_one_stock["_turnover_f_eff"].tail(8), errors="coerce")
    band_hit = 0
    for v in recent.values:
        if pd.isna(v) or float(v) <= 0:
            continue
        fv = float(v)
        if p20 < fv < p60:
            band_hit += 1
    if band_hit < 6:
        return False

    wr = float(pd.to_numeric(last.get("winner_rate"), errors="coerce") or 0.0)
    wr_hist = pd.to_numeric(hist.get("winner_rate"), errors="coerce").dropna()
    if wr_hist.empty:
        return False
    wr_med = float(wr_hist.median())
    if wr <= wr_med:
        return False

    vr = float(pd.to_numeric(last.get("vol_ratio"), errors="coerce") or 0.0)
    if not (1.0 <= vr <= 1.6):
        return False

    cyq_raw = last["cyq_concentration"] if "cyq_concentration" in last.index else None
    cyq = float(pd.to_numeric(cyq_raw, errors="coerce") or np.nan)
    if np.isnan(cyq):
        cyq = float(pd.to_numeric(rt.get("cyq_concentration"), errors="coerce") or 999.0)
    if cyq < 900.0 and cyq >= 26.0:
        return False

    return True


def strategy_masks_p5(
    df: pd.DataFrame,
    cfg: Optional[P5PostmarketConfig] = None,
    market_contraction_score: float = 0.0,
) -> Dict[str, pd.Series]:
    cfg = _resolve_p5_cfg(cfg)
    if df is None or df.empty:
        return {}
    if "_turnover_f_eff" not in df.columns:
        raise ValueError("strategy_masks_p5: 请先 prepare_p5_lags（含 _turnover_f_eff）")

    mh = df["_mh"]
    mh_prev = pd.to_numeric(df["macd_hist_prev"], errors="coerce")
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    vol = pd.to_numeric(df["vol"], errors="coerce")
    pct = pd.to_numeric(df["pct_chg"], errors="coerce")
    vr = pd.to_numeric(df["vol_ratio"], errors="coerce")
    wr = pd.to_numeric(df["winner_rate"], errors="coerce")
    vma5 = pd.to_numeric(df["vol_ma5"], errors="coerce")
    vma20 = pd.to_numeric(df["vol_ma20"], errors="coerce")
    ma5 = pd.to_numeric(df["ma5"], errors="coerce")
    ma10 = pd.to_numeric(df["ma10"], errors="coerce")
    ma20 = pd.to_numeric(df["ma20"], errors="coerce")
    ma60 = pd.to_numeric(df["ma60"], errors="coerce")
    slope20 = pd.to_numeric(df["ma20_slope_5"], errors="coerce")
    h20 = pd.to_numeric(df["high_20"], errors="coerce")
    macd = pd.to_numeric(df["macd"], errors="coerce")
    sig = pd.to_numeric(df["macd_signal"], errors="coerce")
    nm = pd.to_numeric(df["net_main_amount"], errors="coerce")
    inst = pd.to_numeric(df["inst_net_buy"], errors="coerce")
    c95 = pd.to_numeric(df["cost_95th"], errors="coerce")
    c50 = pd.to_numeric(df["cost_50th"], errors="coerce")
    cm = pd.to_numeric(df["circ_mv"], errors="coerce")
    to_eff = pd.to_numeric(df["_turnover_f_eff"], errors="coerce")
    pe = pd.to_numeric(df["pe_ttm"], errors="coerce")
    ps = pd.to_numeric(df["ps_ttm"], errors="coerce")
    atrp = pd.to_numeric(df["atr_pct"], errors="coerce")
    lim = pd.to_numeric(df["limit_times"], errors="coerce").fillna(0).astype(int)

    v5p = df["vol_ma5_prev"]
    v20p = df["vol_ma20_prev"]
    inst3 = pd.to_numeric(df["inst_net_buy_sum3"], errors="coerce")

    thr_nm_s3 = vector_dynamic_net_main_threshold(cm, cfg.s3_net_main_ratio_of_float_mv)
    thr_inst3_s6 = vector_dynamic_inst_sum3_threshold(cm, cfg.s6_inst_sum3_ratio_of_float_mv)
    thr_nm_s8 = vector_dynamic_net_main_threshold(cm, cfg.s8_net_main_ratio_of_float_mv)
    nm_fly_s6 = vector_nm_fly_negative_threshold(cm, cfg.s6_net_main_fly_ratio_of_float_mv)

    masks: Dict[str, pd.Series] = {}

    s1_core = (
        (wr > cfg.s1_winner_min)
        & (vr >= cfg.s1_vr_min)
        & (mh > 0)
        & (mh > mh_prev)
        & (pct >= cfg.s1_pct_low)
        & (pct <= cfg.s1_pct_high)
    )
    s1_fly = (high - close) / close.replace(0, np.nan) > cfg.s1_upper_shadow_ratio_fly_gt
    masks["P5-10·★核心四因子共振"] = s1_core & (~s1_fly.fillna(False)) & _base_ok(df)

    s2_trend = (vma5 > vma20) & (v5p <= v20p) & (ma5 > ma20)
    s2_volume = vol > vma5 * cfg.s2_vol_over_vma5_mult
    s2_quality = wr > cfg.s2_winner_min
    s2_core = s2_trend & s2_volume & s2_quality
    s2_fly = (c95 / close.replace(0, np.nan)) < cfg.s2_cost95_to_close_ratio_fly_lt
    masks["P5-01·★量价趋势"] = s2_trend & (~s2_fly.fillna(False)) & _base_ok(df)
    masks["P5-02·★量价放量"] = s2_volume & (~s2_fly.fillna(False)) & _base_ok(df)
    masks["P5-03·★量价确认"] = s2_core & (~s2_fly.fillna(False)) & _base_ok(df)

    s3_money = (nm > thr_nm_s3) & (close > ma60)
    # 【上线前修复 2026-10-18】移除 hk_vol 硬门槛。
    # 根因：hk_hold 数据源下线后全库 hk_vol 恒为 0（daily_data 222834 行非 0 行 0），
    # hk > 0 恒为 False，使资金质量/资金确认（现 P5-05 / P5-06）自上线起命中数恒为 0（死策略）。
    # 现改为：P5-05 只保留机构净买入（inst > 0），P5-06 为机构净买入 + 主力净流入（nm > thr_nm_s3）。
    s3_quality = inst > 0
    s3_core = s3_money & s3_quality
    s3_fly = vol > vma5 * cfg.s3_vol_over_vma5_fly_mult
    masks["P5-04·★资金方向"] = s3_money & (~s3_fly.fillna(False)) & _base_ok(df)
    masks["P5-05·★资金质量"] = s3_quality & (~s3_fly.fillna(False)) & _base_ok(df)
    masks["P5-06·★资金确认"] = s3_core & (~s3_fly.fillna(False)) & _base_ok(df)

    s4_core = (
        (wr > cfg.s4_winner_min)
        & (close > c95)
        & (slope20 > cfg.s4_ma20_slope_min)
        & (vol <= vma5 * cfg.s4_vol_over_vma5_max_mult)
    )
    s4_fly = (mh < mh_prev) & mh_prev.notna() & mh.notna()
    masks["P5-11·★单峰密集突破"] = s4_core & (~s4_fly.fillna(False)) & _base_ok(df)

    # ---------------------------------------------------------------------
    # 【V26.88+10-5 修复 P0 BUG】原 s5_trend 含 (close > h20)。
    # 落库 high_20 = rolling(20).max() 含当日 ⇒ high_20 >= high >= close
    # ⇒ 该条件恒为 False ⇒ 趋势结构/趋势确认（现 P5-07 / P5-09）自上线起命中数恒为 0。
    #
    # 修复口径（经 94 日 · 底池内 6808 样本前瞻收益实测对比）：
    #   真突破 close > high_20_prev        : 5日 -2.25%(ex -0.83%) / 10日 -3.55%(ex -1.47%)  ← 追高，负期望
    #   接近新高 close >= high_20*(1-2%)   : 5日 -1.30%(ex +0.12%) / 10日 -2.00%(ex +0.09%)  ← 采纳
    # 故改为「收盘价距 20 日高点 2% 以内」，用 high_20_rel 列表达（>0 即未站上高点）。
    # ---------------------------------------------------------------------
    h20_rel = (
        pd.to_numeric(df["high_20_rel"], errors="coerce")
        if "high_20_rel" in df.columns
        else (h20 / close.replace(0, np.nan) - 1.0)
    )
    s5_near_high = h20_rel.notna() & (h20_rel >= -cfg.s5_near_high_gap_pct / 100.0)
    s5_trend = (ma5 > ma20) & (ma20 > ma60) & s5_near_high
    # 【V30.10 统一 MACD 口径 2026-10-21】
    # 原 s5_quality 用 (macd > sig)、s12/s13/s15 的 fly 用 (macd < sig)、
    # s13 又额外用 (mh > 0) —— 同一份数据被两套写法判定。
    # 实测（生产 daily_data 227158 行）三者【完全等价】（dif>dea ⟺ hist>0，0 行不一致），
    # 但写法分散 = 未来数据源一变就分叉。
    # 现统一以【1× 归一柱体 mh】为唯一判据：
    #   多头  mh > 0     空头  mh < 0
    s5_quality = (mh > 0) & (cm >= cfg.s5_circ_mv_fly_lt_wan)
    s5_fly = cm < cfg.s5_circ_mv_fly_lt_wan
    s5_chase = (pct > 4.5) & (vr > 2.2) & (wr < 78.0)
    vwap = pd.to_numeric(df["vwap"], errors="coerce") if "vwap" in df.columns else pd.Series(np.nan, index=df.index)
    s5_fake_strong = s5_chase & ((close - vwap).abs() / vwap.replace(0, np.nan) > 0.03)
    masks["P5-07·★趋势结构"] = s5_trend & (~s5_fly.fillna(False)) & (~s5_fake_strong.fillna(False)) & _base_ok(df)
    masks["P5-08·★趋势质量"] = s5_quality & (~s5_fly.fillna(False)) & (~s5_fake_strong.fillna(False)) & _base_ok(df)
    masks["P5-09·★趋势确认"] = s5_trend & s5_quality & (~s5_fly.fillna(False)) & (~s5_fake_strong.fillna(False)) & _base_ok(df)

    s6_core = (
        (inst3 > thr_inst3_s6)
        & (pct >= cfg.s6_pct_low)
        & (pct <= cfg.s6_pct_high)
        & (vol < vma5 * cfg.s6_vol_under_vma5_ratio)
        & (close > ma20)
    )
    s6_fly = nm < nm_fly_s6
    masks["P5-12·★机构龙虎缩量回踩"] = s6_core & (~s6_fly.fillna(False)) & _base_ok(df)

    s8_core = (
        (cm >= cfg.s8_circ_mv_min_wan)
        & (cm <= cfg.s8_circ_mv_max_wan)
        & (nm > thr_nm_s8)
        & (to_eff >= cfg.s8_turnover_f_low)
        & (to_eff <= cfg.s8_turnover_f_high)
        & (close > ma60)
    )
    masks["P5-13·★超级中军点火"] = s8_core & _base_ok(df)

    # ---------- 【V34+ 新增】外部策略移植（现编号 P5-22 ~ P5-25 / P5-14） ----------
    # P5-22 双均线金叉（MA5 上穿 MA20，当日成立；量比/涨幅/winner_rate 三重确认）
    if "ma5_prev_c" in df.columns and "ma20_prev_c" in df.columns:
        ma5_prev_c = pd.to_numeric(df["ma5_prev_c"], errors="coerce")
        ma20_prev_c = pd.to_numeric(df["ma20_prev_c"], errors="coerce")
        s16_cross = (ma5 > ma20) & (ma5_prev_c <= ma20_prev_c) & ma5_prev_c.notna() & ma20_prev_c.notna()
        s16_core = (
            s16_cross
            & (pct >= cfg.s16_pct_low)
            & (pct <= cfg.s16_pct_high)
            & (vr >= cfg.s16_vr_min)
            & (close > ma20)
        )
        s16_fly = (wr > 0) & (wr < cfg.s16_winner_min)
        masks["P5-22·★双均线金叉"] = s16_core & (~s16_fly.fillna(False)) & _base_ok(df)
    else:
        masks["P5-22·★双均线金叉"] = pd.Series(False, index=df.index)

    # P5-23 布林上轨突破（收盘 > 20 日 2σ 上轨；源策略无涨幅上限，这里加 pct 区间防追极端）
    if "boll_up" in df.columns:
        boll_up = pd.to_numeric(df["boll_up"], errors="coerce")
        s17_core = (
            (close > boll_up)
            & boll_up.notna()
            & (pct >= cfg.s17_pct_low)
            & (pct <= cfg.s17_pct_high)
            & (vr >= cfg.s17_vol_ratio_min)
        )
        s17_fly = (wr > 0) & (wr < cfg.s17_winner_min)
        masks["P5-23·★布林上轨突破"] = s17_core & (~s17_fly.fillna(False)) & _base_ok(df)
    else:
        masks["P5-23·★布林上轨突破"] = pd.Series(False, index=df.index)

    # P5-24 量价新高突破（收盘站上前 20 日最高 + 量能 ≥ 前 20 日均量 ×1.2）
    if "high_20_prev" in df.columns:
        h20_prev_s18 = pd.to_numeric(df["high_20_prev"], errors="coerce")
        v20p_s18 = v20p if isinstance(v20p, pd.Series) else pd.Series(np.nan, index=df.index)
        s18_core = (
            (close > h20_prev_s18)
            & h20_prev_s18.notna()
            & (v20p_s18.notna())
            & (vol >= v20p_s18 * cfg.s18_vol_mult)
            & (pct > 0)
            & (pct <= cfg.s18_pct_high)
        )
        masks["P5-24·★量价新高突破"] = s18_core & _base_ok(df)
    else:
        masks["P5-24·★量价新高突破"] = pd.Series(False, index=df.index)

    # P5-25 MACD 金叉趋势（DIF 上穿 DEA 当日 + 收盘站上 MA20）
    if "macd_prev" in df.columns and "macd_signal_prev" in df.columns:
        macd_prev = pd.to_numeric(df["macd_prev"], errors="coerce")
        sig_prev = pd.to_numeric(df["macd_signal_prev"], errors="coerce")
        s19_cross = (macd > sig) & (macd_prev <= sig_prev) & macd_prev.notna() & sig_prev.notna()
        # 【V34+ 移植加固】源策略对金叉位置不设限，实测平静横盘微幅震荡也会产生
        # 「零轴下方微金叉」噪音信号；系统口径要求右侧——默认只认零轴上方金叉。
        if bool(getattr(cfg, "s19_require_above_zero", True)):
            s19_cross = s19_cross & (macd > 0)
        s19_core = (
            s19_cross
            & (close > ma20)
            & (pct >= cfg.s19_pct_low)
            & (pct <= cfg.s19_pct_high)
        )
        s19_fly = (wr > 0) & (wr < cfg.s19_winner_min)
        masks["P5-25·★MACD金叉趋势"] = s19_core & (~s19_fly.fillna(False)) & _base_ok(df)
    else:
        masks["P5-25·★MACD金叉趋势"] = pd.Series(False, index=df.index)

    # P5-14 欧奈尔趋势模板（价格>MA60>MA120>MA250、MA250 上升≥20 日、52周低点上方30%+、
    #                       距52周高点25%以内；RPS 条件省略——向量化引擎无横截面 RPS 列）
    if all(c in df.columns for c in ("ma120", "ma250", "ma250_lag20", "high_250w", "low_250w")):
        ma120_c = pd.to_numeric(df["ma120"], errors="coerce")
        ma250_c = pd.to_numeric(df["ma250"], errors="coerce")
        ma250_l20 = pd.to_numeric(df["ma250_lag20"], errors="coerce")
        h250w = pd.to_numeric(df["high_250w"], errors="coerce")
        l250w = pd.to_numeric(df["low_250w"], errors="coerce")
        s20_core = (
            (ma60 > 0) & (ma120_c > 0) & (ma250_c > 0)
            & (close > ma60) & (close > ma120_c) & (close > ma250_c)
            # 【V34+ 修复】底仓窗口仅约 130 行时 ma120 与 ma250 可能相等（同窗口均值），
            # 严格 > 会让本策略在「原始 prep 路径」与「引擎重算路径」间命中分叉；改用 >=。
            & (ma60 > ma120_c) & (ma120_c >= ma250_c)
            & (ma250_l20.notna()) & (ma250_c > ma250_l20)
            & h250w.notna() & l250w.notna()
            & (close >= l250w * (1.0 + cfg.s20_above_low_min_pct / 100.0))
            & (close >= h250w * (1.0 - cfg.s20_from_high_max_pct / 100.0))
            & (pct >= cfg.s20_pct_low)
            & (pct <= cfg.s20_pct_high)
        )
        masks["P5-14·★欧奈尔趋势模板"] = s20_core & _base_ok(df)
    else:
        masks["P5-14·★欧奈尔趋势模板"] = pd.Series(False, index=df.index)


    if (
        "close_lag1" in df.columns
        and "high_20_lag2" in df.columns
        and "low_60" in df.columns
        and "high_20" in df.columns
    ):
        close_lag1 = pd.to_numeric(df["close_lag1"], errors="coerce")
        h20_lag2 = pd.to_numeric(df["high_20_lag2"], errors="coerce")
        l60 = pd.to_numeric(df["low_60"], errors="coerce")
        box_rng = (h20 - l60) / close.replace(0, np.nan)
        brk = (close_lag1 > h20_lag2 * (1.0 + cfg.s12_break_min_pct / 100.0)) & close_lag1.notna() & h20_lag2.notna()
        s12_pb = (
            (close > ma20)
            & (low <= ma20 * cfg.s12_ma20_touch_max_mult)
            & (pct >= cfg.s12_pct_low)
            & (pct <= cfg.s12_pct_high)
        )
        s12_rng = (box_rng > cfg.s12_box_range_min) & (box_rng < cfg.s12_box_range_max)
        s12_breakout = brk & s12_rng & (ma5 > ma10) & (vr >= cfg.s12_vr_min)
        s12_pullback = s12_pb & s12_rng & (ma5 > ma10) & (wr > cfg.s12_winner_min) & (vr >= cfg.s12_vr_min)
        # 【V30.10 统一 MACD 口径 2026-10-21】原 (macd < sig) → 统一为 (mh < 0)（1× 归一口径）
        s12_fly = (mh < 0) & mh.notna()
        # 【V26.88+10-1 P1-2 修复】箱体策略需要 60 日历史，新股（<60 日）box_rng=NaN 全部 False。
        # 这里把"上市 < 60 日"的票从箱体策略中排除（量价/资金/趋势/四因子/单峰等单日策略不受影响）。
        new_stock_mask = _is_new_stock_mask_p5(df, lookback_days=60)
        masks["P5-15·★箱体突破"] = s12_breakout & (~s12_fly.fillna(False)) & (~new_stock_mask) & _base_ok(df)
        masks["P5-16·★箱体回踩"] = s12_pullback & (~s12_fly.fillna(False)) & (~new_stock_mask) & _base_ok(df)
    else:
        masks["P5-15·★箱体突破"] = pd.Series(False, index=df.index)
        masks["P5-16·★箱体回踩"] = pd.Series(False, index=df.index)

    if (
        "ma5_lag3" in df.columns
        and "ma10_lag3" in df.columns
        and "ma20_lag3" in df.columns
    ):
        m5l3 = pd.to_numeric(df["ma5_lag3"], errors="coerce")
        m10l3 = pd.to_numeric(df["ma10_lag3"], errors="coerce")
        m20l3 = pd.to_numeric(df["ma20_lag3"], errors="coerce")
        spr3 = np.maximum(np.abs(m5l3 - m10l3), np.abs(m10l3 - m20l3)) / m20l3.replace(0, np.nan)
        cohesion = spr3 < (cfg.s13_cohesion_max_pct / 100.0)
        spr_now = (ma5 - ma20).abs() / ma20.replace(0, np.nan)
        widen = spr_now > (cfg.s13_spread_min_pct / 100.0)

        s13_stage1 = cohesion.fillna(False) & (ma5 > ma10) & (ma10 > ma20)
        s13_stage2 = s13_stage1 & widen.fillna(False) & (close > ma20)
        s13_stage3 = (
            s13_stage2
            & (pct >= cfg.s13_pct_low)
            & (pct <= cfg.s13_pct_high)
            & (slope20 > cfg.s13_ma20_slope_min)
            & (wr > cfg.s13_winner_min)
            & (vr >= cfg.s13_vr_min)
            & (mh > 0)
        )
        # 【V30.10 统一 MACD 口径 2026-10-21】原 (macd < sig) → 统一为 (mh < 0)（1× 归一口径）
        s13_fly = (mh < 0) & mh.notna()
        # 【V26.88+10-1 P1-2 修复】均线粘合发散需要 ma5/10/20 lag3 历史，新股 (<3 日) NaN 全 False。
        # 同样排除上市 <3 日的新股（这些策略不适用）
        new_stock_mask_13 = _is_new_stock_mask_p5(df, lookback_days=3)
        masks["P5-17·★均线粘合"] = s13_stage1 & (~new_stock_mask_13) & _base_ok(df)
        masks["P5-18·★均线发散起点"] = s13_stage2 & (~s13_fly.fillna(False)) & (~new_stock_mask_13) & _base_ok(df)
        masks["P5-19·★均线发散确认"] = s13_stage3 & (~s13_fly.fillna(False)) & (~new_stock_mask_13) & _base_ok(df)
    else:
        masks["P5-17·★均线粘合"] = pd.Series(False, index=df.index)
        masks["P5-18·★均线发散起点"] = pd.Series(False, index=df.index)
        masks["P5-19·★均线发散确认"] = pd.Series(False, index=df.index)

    vwap_pen_mask = pd.Series(1.0, index=df.index, dtype=float)
    vwap_warning_col: List[str] = []
    if "ts_code" in df.columns:
        for _, g in df.groupby("ts_code", sort=False):
            if g is None or len(g) < 3:
                continue
            last_i = g.index[-1]
            try:
                mult, patch, warnings = evaluate_vwap_penalty_p5(g, {}, cfg)
                vwap_pen_mask.loc[last_i] = float(mult)
                if warnings:
                    vwap_warning_col.append(f"{g.iloc[-1].get('ts_code', '')}:{'|'.join(warnings)}")
                for k, v in patch.items():
                    try:
                        df.loc[last_i, k] = v
                    except Exception:
                        pass
            except Exception as _e:
                logger.debug("VWAP惩罚 分组计算跳过 ts_code 末行: %s", _e, exc_info=True)
                continue
    masks["P5-00·◆VWAP分时防伪"] = vwap_pen_mask < 0.999

    # 【缩量分歧低吸】向量化面板：按 ts_code 分组用全日窗口计算，仅缩量期写入 True（末行代表当日）
    if market_contraction_score < 0.7 or "_turnover_f_eff" not in df.columns or "ts_code" not in df.columns:
        masks["P5-20·★缩量分歧低吸"] = pd.Series(False, index=df.index)
    else:
        shrink = pd.Series(False, index=df.index)
        for _, g in df.groupby("ts_code", sort=False):
            if g is None or len(g) < 30:
                continue
            last_i = g.index[-1]
            try:
                # 先做更严格的前置：必须至少满足一个“缩量承接”形态与一个“筹码锁定”条件，再进完整判断
                g_last = g.iloc[-1]
                g_close = float(pd.to_numeric(g_last.get("close"), errors="coerce") or 0.0)
                g_ma20 = float(pd.to_numeric(g_last.get("ma20"), errors="coerce") or 0.0)
                g_ma5 = float(pd.to_numeric(g_last.get("ma5"), errors="coerce") or 0.0)
                g_vr = float(pd.to_numeric(g_last.get("vol_ratio"), errors="coerce") or 0.0)
                if min(g_close, g_ma20, g_ma5) <= 0:
                    continue
                if not (g_ma5 >= g_ma20 * 0.985 and g_close >= g_ma20 * 0.992):
                    continue
                if not (0.8 <= g_vr <= 1.8):
                    continue
                if evaluate_shrink_divergence_mask_p5(g, {}, market_contraction_score):
                    shrink.loc[last_i] = True
            except Exception as _e:
                # 【全局审计修复】维度2：单股缩量分歧面板异常跳过该 ts_code，禁止无日志静默
                logger.debug("★缩量分歧低吸 分组计算跳过 ts_code 末行: %s", _e, exc_info=True)
                continue
        masks["P5-20·★缩量分歧低吸"] = shrink & _base_ok(df)

    # ---------------------------------------------------------------------
    # 【V26.88+10-5 新增】策略十五：高位旗形蓄势（HighTightFlag）
    #
    # 【设计取舍 · 与 Sequoia-X 原版的差异，勿轻易改回】
    # Sequoia-X 原版是「突破确认式」：旗形 + close > 前 20 日高点 + 放量。
    # 本实现在 94 个交易日（底池 6808 样本）上前瞻收益实测结果：
    #     旗形 + 真突破        5日 -2.66% (ex -1.23%) / 10日 -3.88% (ex -1.80%)  ← 负期望
    #     旗形 + 缩量(0.7)    5日 -0.50% (ex +0.92%) / 10日 -0.79% (ex +1.29%)  ← 采纳
    # 结论：本市场（样本期等权指数 -29.6%）里「缩量抗跌」有效、「放量突破」是追高接盘。
    # 因此保留 Sequoia-X 的旗形形态与缩量精神，去掉突破与放量。
    #
    # 缩量基准用 vol_ma20_prev（不含当日），避免「今日量 vs 含今日的均量」自我比较。
    # 40 日窗口 → 必须用 _is_new_stock_mask_p5(lookback_days=40) 排除新股。
    # ---------------------------------------------------------------------
    _s15_cols = ("high_40", "low_40", "high_10", "low_10", "vol_ma20_prev")
    if all(c in df.columns for c in _s15_cols):
        _h40 = pd.to_numeric(df["high_40"], errors="coerce")
        _l40 = pd.to_numeric(df["low_40"], errors="coerce")
        _h10 = pd.to_numeric(df["high_10"], errors="coerce")
        _l10 = pd.to_numeric(df["low_10"], errors="coerce")
        _v20p = pd.to_numeric(df["vol_ma20_prev"], errors="coerce")
        _l40_safe = _l40.replace(0, np.nan)
        _l10_safe = _l10.replace(0, np.nan)

        s15_momentum = (_h40 / _l40_safe) > cfg.s15_momentum
        s15_consolidation = (_h10 / _l10_safe) < cfg.s15_consolidation
        s15_high_level = _l10 >= (_h40 * cfg.s15_high_level_ratio)
        s15_shrink = _v20p.notna() & _v20p.gt(0) & (vol < _v20p * cfg.s15_shrink_vol_mult)
        # 趋势未破：多头排列且站上 ma20（旗形必须处于上升趋势的整理阶段）
        s15_trend = (ma5 > ma10) & (ma10 > ma20) & (close > ma20)

        s15_core = (
            s15_momentum
            & s15_consolidation
            & s15_high_level
            & s15_shrink
            & s15_trend
        ).fillna(False)
        # MACD 死叉过滤（与箱体/均线类 P5-15~P5-19 同款防飞刀口径）
        # 【V30.10 统一 MACD 口径 2026-10-21】原 (macd < sig) → 统一为 (mh < 0)（1× 归一口径）
        s15_fly = (mh < 0) & mh.notna()
        new_stock_mask_15 = _is_new_stock_mask_p5(df, lookback_days=int(cfg.s15_min_bars))
        masks["P5-21·★高位旗形蓄势"] = (
            s15_core & (~s15_fly.fillna(False)) & (~new_stock_mask_15) & _base_ok(df)
        )
    else:
        masks["P5-21·★高位旗形蓄势"] = pd.Series(False, index=df.index)

    return masks


def screen_p5_panel_for_date(
    df: pd.DataFrame,
    trade_date: Union[str, pd.Timestamp],
    cfg: Optional[P5PostmarketConfig] = None,
    market_contraction_score: float = 0.0,
) -> pd.DataFrame:
    cfg = _resolve_p5_cfg(cfg)
    prep = prepare_p5_lags(df)
    td = pd.to_datetime(trade_date)
    sub = prep[prep["trade_date"] == td].copy()
    if sub.empty:
        return pd.DataFrame()

    gpass = p5_global_pass_mask(sub, cfg)
    sub["_global_pass"] = gpass

    # 【缩量分歧低吸】批量面板可显式传入大盘收缩度；默认 0 保持历史兼容，与 scan 层单票 rt 路径一致。
    masks = strategy_masks_p5(sub, cfg, market_contraction_score=market_contraction_score)
    hit_mat = pd.DataFrame({k: v.astype(bool) for k, v in masks.items()}, index=sub.index)

    # 【V34 P0】命中统计同样剔除防伪/降权 mask：原来 strategy_tags 剔了、hit_any 没剔，
    # 结果「只有 VWAP 偏离」的票 tags 为空却 hit_any=True，照样进池。
    _strategy_cols = [c for c in hit_mat.columns if c not in _P5_NON_STRATEGY_MASKS]
    if _strategy_cols:
        any_hit = hit_mat[_strategy_cols].any(axis=1)
    else:
        any_hit = pd.Series(False, index=sub.index)
    tags: List[str] = []
    for idx in sub.index:
        names = [c for c in _strategy_cols if bool(hit_mat.at[idx, c])]
        # 【V35】别名去重已随编号重整删除（不再存在聚合别名标签），两入口天然一致
        tags.append("|".join(names))
    sub["strategy_tags"] = tags
    sub["hit_any"] = any_hit.values & gpass.values
    sub["global_pass"] = gpass.values

    sub["vwap_penalty_mult"] = np.where(
        hit_mat.get("P5-00·◆VWAP分时防伪", pd.Series(False, index=sub.index)).values,
        0.999,
        1.0,
    )

    out_cols = [
        "ts_code",
        "trade_date",
        "global_pass",
        "hit_any",
        "strategy_tags",
        "vwap_penalty_mult",
    ]
    out = sub[out_cols].copy()
    ordered_strategy_cols = [
        "P5-01·★量价趋势",
        "P5-02·★量价放量",
        "P5-03·★量价确认",
        "P5-04·★资金方向",
        "P5-05·★资金质量",
        "P5-06·★资金确认",
        "P5-07·★趋势结构",
        "P5-08·★趋势质量",
        "P5-09·★趋势确认",
        "P5-10·★核心四因子共振",
        "P5-11·★单峰密集突破",
        "P5-12·★机构龙虎缩量回踩",
        "P5-13·★超级中军点火",
        "P5-14·★欧奈尔趋势模板",
        "P5-15·★箱体突破",
        "P5-16·★箱体回踩",
        "P5-17·★均线粘合",
        "P5-18·★均线发散起点",
        "P5-19·★均线发散确认",
        "P5-20·★缩量分歧低吸",
        "P5-21·★高位旗形蓄势",
        "P5-22·★双均线金叉",
        "P5-23·★布林上轨突破",
        "P5-24·★量价新高突破",
        "P5-25·★MACD金叉趋势",
        "P5-00·◆VWAP分时防伪",
    ]
    for c in ordered_strategy_cols:
        if c in hit_mat.columns:
            out[c] = hit_mat[c].values
    for c in hit_mat.columns:
        if c not in out.columns:
            out[c] = hit_mat[c].values
    return out


def evaluate_p5_single_prepared_row(
    prep_row: pd.Series,
    cfg: Optional[P5PostmarketConfig] = None,
    *,
    stock_prep_df: Optional[pd.DataFrame] = None,
    rt: Optional[Dict[str, Any]] = None,
    market_contraction_score: float = 0.0,
) -> Tuple[bool, List[str], str, Dict[str, Any]]:
    cfg = _resolve_p5_cfg(cfg)
    one = prep_row.to_frame().T

    # 【V26.88+9-22 优化20】HiThink 炸板池硬否决（盘后买入炸板股 = 100% 次日低开）
    # 【V34 P2-3】改为走唯一实现 p5_limit_burst_veto（带交易日作用域），避免与引擎各判一遍、口径分叉
    _veto_burst, _open_times, _burst_to = p5_limit_burst_veto(rt, (rt or {}).get("trade_date"))
    if _veto_burst:
        return False, [], (
            f"炸板拦截: HiThink 炸板池命中(open_times={_open_times}, 换手{_burst_to:.1f}%, "
            f"盘后买炸板股=次日100%低开)"
        ), {}

    gp = p5_global_pass_mask(one, cfg)
    if not bool(gp.iloc[0]):
        reasons = []
        close = float(pd.to_numeric(one["close"], errors="coerce").iloc[0] or 0)
        ma60 = float(pd.to_numeric(one["ma60"], errors="coerce").iloc[0] or 0)
        slope = float(pd.to_numeric(one["ma20_slope_5"], errors="coerce").iloc[0] or 0)
        if close <= ma60 or slope <= 0:
            reasons.append("中期破位或斜率")
        bias = float(one["_bias_live"].iloc[0]) if "_bias_live" in one.columns else np.nan
        if not np.isnan(bias) and (bias > cfg.global_bias_high or bias < cfg.global_bias_low):
            reasons.append("乖离极端")
        pe = float(pd.to_numeric(one["pe_ttm"], errors="coerce").iloc[0] or 0)
        if pe < 0:
            reasons.append("亏损 pe_ttm<0")
        nm0 = float(pd.to_numeric(one["net_main_amount"], errors="coerce").iloc[0] or np.nan)
        nm1 = float(pd.to_numeric(one["nm_lag1"], errors="coerce").iloc[0] or np.nan)
        nm2 = float(pd.to_numeric(one["nm_lag2"], errors="coerce").iloc[0] or np.nan)
        if np.all(pd.notna([nm0, nm1, nm2])) and (nm0 < 0) and (nm1 < 0) and (nm2 < 0):
            reasons.append("主力三连负")
        return False, [], ";".join(reasons) if reasons else "全局否决", {}

    # 单票路径优先用完整历史窗口跑 masks，再只读取末行结果；缺少窗口时保持旧的单行回退逻辑。
    mask_input = one
    mask_idx = one.index[0]
    if stock_prep_df is not None and not stock_prep_df.empty:
        mask_input = stock_prep_df
        mask_idx = stock_prep_df.index[-1]
    masks = strategy_masks_p5(mask_input, cfg, market_contraction_score=market_contraction_score)
    # 【V35 编号重整】展示优先顺序：结构买点在前的同一优先级链（别名项已删，一次输出即可）
    hit_order = [
        "P5-15·★箱体突破",
        "P5-16·★箱体回踩",
        "P5-19·★均线发散确认",
        "P5-18·★均线发散起点",
        "P5-17·★均线粘合",
        "P5-09·★趋势确认",
        "P5-08·★趋势质量",
        "P5-07·★趋势结构",
    ]
    hits: List[str] = []
    seen_hits = set()
    for name in hit_order:
        s = masks.get(name)
        if s is None:
            continue
        try:
            hit = bool(s.loc[mask_idx])
        except Exception:
            hit = bool(s.iloc[-1]) if len(s) else False
        if hit and name not in seen_hits:
            hits.append(name)
            seen_hits.add(name)

    for name, s in masks.items():
        if name in seen_hits or name == "P5-20·★缩量分歧低吸":
            continue
        if name in _P5_NON_STRATEGY_MASKS:
            # 【V34 P0】防伪/降权 mask 不是战法，不进 hits（面板路径一直是这么做的）
            continue
        try:
            hit = bool(s.loc[mask_idx])
        except Exception:
            hit = bool(s.iloc[-1]) if len(s) else False
        if hit:
            hits.append(name)
            seen_hits.add(name)

    if market_contraction_score >= 0.7 and stock_prep_df is not None:
        if evaluate_shrink_divergence_mask_p5(stock_prep_df, rt, market_contraction_score) and "P5-20·★缩量分歧低吸" not in seen_hits:
            hits.append("P5-20·★缩量分歧低吸")

    # 【V35】别名去重已随编号重整删除，hits 即最终展示名

    # 【上线前修复 2026-10-18】detail：只写战略赛道标识。
    # 原先写入的 hk_3d_score / hk_3d_has_data 来自已退役的北向资金链路，
    # hk_vol 全库恒为 0，继续输出只会让 UI/日志出现「有北向评分但恒为 0」的假信号。
    is_strat = _is_strategic_industry(rt, prep_row, cfg)
    extra_detail: Dict[str, Any] = {
        "is_strategic_industry": bool(is_strat),
    }
    return True, hits, "", extra_detail


def _recompute_p5_tail_indicators(work: pd.DataFrame) -> pd.DataFrame:
    """【V34 P2-1】跨日追加新行后，按「含今日」的窗口重算【机械可推导】指标。

    原实现整行复制上一日、再只覆盖价/量 ⇒ ma5/ma20/ma60/ma20_slope_5/high_20/low_60 全是昨日值，
    close 却是今日值，乖离/斜率/箱体全线错配（补跑、隔日重扫、盘后重算昨日必踩）。

    口径严格对齐 core/indicator_calc.precompute_indicators（即「DB 正常更新后的样子」）：
      · MA / vol_ma / high_20 / low_60 用 min_periods=1（生产如此，新股也会得到值）。
      · ma20_slope_5 = (ma20 - ma20.shift(5)) / ma20.shift(5) * 100。
      · bias_20 = (close - ma20) / ma20 * 100。
      · MACD 用 ewm(12/26/9, adjust=False)，hist 取 1× 口径（= macd - macd_signal）。
        该口径与生产 daily_data 实测一致（max|macd_hist-(macd-macd_signal)| = 0.0）。
        窗口 < 60 行时 EMA 尚未收敛，置 NaN fail-closed —— 宁可不出信号，也不拿旧值冒充。
      · cost_50th / cost_95th 等来自筹码与资金流数据，无法由 OHLCV 推导，
        仍沿用 rt 注入值或上一日值（保持原行为，本函数不伪造）。
    """
    if work is None or work.empty or "close" not in work.columns:
        return work
    last = work.index[-1]
    close = pd.to_numeric(work["close"], errors="coerce")
    high = pd.to_numeric(work["high"], errors="coerce") if "high" in work.columns else close
    low = pd.to_numeric(work["low"], errors="coerce") if "low" in work.columns else close
    vol = pd.to_numeric(work["vol"], errors="coerce") if "vol" in work.columns else None

    def _set(series: pd.Series, col: str) -> None:
        if col not in work.columns:
            return
        try:
            v = float(series.iloc[-1])
        except Exception:
            v = float("nan")
        work.loc[last, col] = v if np.isfinite(v) else np.nan

    for _w, _c in ((5, "ma5"), (10, "ma10"), (20, "ma20"), (60, "ma60"), (120, "ma120"), (250, "ma250")):
        _set(close.rolling(_w, min_periods=1).mean(), _c)
    if vol is not None:
        for _w, _c in ((5, "vol_ma5"), (10, "vol_ma10"), (20, "vol_ma20")):
            _set(vol.rolling(_w, min_periods=1).mean(), _c)
    _set(high.rolling(20, min_periods=1).max(), "high_20")
    _set(low.rolling(60, min_periods=1).min(), "low_60")
    _set(high.rolling(40, min_periods=1).max(), "high_40")
    _set(low.rolling(40, min_periods=1).min(), "low_40")
    _set(high.rolling(10, min_periods=1).max(), "high_10")
    _set(low.rolling(10, min_periods=1).min(), "low_10")

    if "ma20" in work.columns:
        ma20 = pd.to_numeric(work["ma20"], errors="coerce")
        if len(ma20) > 5:
            base = float(ma20.iloc[-6]) if pd.notna(ma20.iloc[-6]) else float("nan")
            if np.isfinite(base) and base > 0 and pd.notna(ma20.iloc[-1]):
                work.loc[last, "ma20_slope_5"] = float((float(ma20.iloc[-1]) / base - 1.0) * 100.0)
        m20 = float(pd.to_numeric(work.loc[last, "ma20"], errors="coerce") or 0.0)
        c = float(pd.to_numeric(work.loc[last, "close"], errors="coerce") or 0.0)
        if m20 > 0 and c > 0:
            work.loc[last, "bias_20"] = float((c - m20) / m20 * 100.0)

    if len(close) >= 60:
        _dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
        _dea = _dif.ewm(span=9, adjust=False).mean()
        _hist = _dif - _dea
        _set(_dif, "macd")
        _set(_dea, "macd_signal")
        _set(_hist, "macd_hist")
        if "macd_bar" in work.columns:
            _set(_hist * 2.0, "macd_bar")
    else:
        for _c in ("macd", "macd_signal", "macd_hist"):
            if _c in work.columns:
                work.loc[last, _c] = np.nan
    return work


def build_single_stock_window_from_rt(
    df_hist: pd.DataFrame,
    rt: Dict[str, Any],
    ts_code: str,
) -> pd.DataFrame:
    if df_hist is None or df_hist.empty:
        return pd.DataFrame()

    work = df_hist.copy()
    if "ts_code" not in work.columns:
        work["ts_code"] = ts_code
    if "trade_date" not in work.columns:
        work["trade_date"] = pd.Timestamp.now().normalize()

    work["trade_date"] = pd.to_datetime(work["trade_date"])
    last_td = work["trade_date"].iloc[-1]
    rt_td_raw = rt.get("trade_date")
    rt_td = pd.to_datetime(rt_td_raw) if rt_td_raw is not None else last_td

    def _vol_to_hand(vol_raw: float) -> float:
        """【V34 P2-4】量纲推断：优先用 amount/price 反推股数做交叉校验。

        原来是 `v >= 100000 → v/100` 的魔数阈值：九万股这种小票会被当成「手」再放大 100 倍，
        vol / 量比 / 换手全错。现按「与反推股数更接近的是真口径」判定，缺 amount/price 才退回旧启发式。
        """
        v = float(vol_raw)
        if v <= 0:
            return v
        try:
            _amt = float(rt.get("amount") or 0.0)
            _px = float(rt.get("price") or rt.get("close") or 0.0)
            if _amt > 0 and _px > 0:
                shares = _amt / _px
                if shares > 0:
                    err_shares = abs(v - shares) / shares
                    err_hands = abs(v * 100.0 - shares) / shares
                    return v if err_hands < err_shares else v / 100.0
        except Exception:
            pass
        return v / 100.0 if v >= 100000.0 else v

    prev_close = float(pd.to_numeric(work["close"].iloc[-1], errors="coerce") or 0.0)

    if rt_td_raw is not None and rt_td > last_td:
        row = work.iloc[-1].copy()
        row["trade_date"] = rt_td
        row["pre_close"] = prev_close
    else:
        row = work.iloc[-1].copy()

    px = rt.get("price")
    if px is None:
        px = rt.get("close")
    if px is not None:
        row["close"] = float(px)

    if rt.get("open") is not None:
        row["open"] = float(rt["open"])
    if rt.get("high") is not None:
        row["high"] = float(rt["high"])
    if rt.get("low") is not None:
        row["low"] = float(rt["low"])
    if rt.get("pre_close") is not None:
        row["pre_close"] = float(rt["pre_close"])

    if rt.get("vol") is not None:
        row["vol"] = float(rt["vol"])
    elif rt.get("volume") is not None:
        row["vol"] = _vol_to_hand(float(rt["volume"]))

    amt = rt.get("amount")
    vol_shares = rt.get("volume")
    if amt is not None and vol_shares is not None:
        amt_f = float(amt)
        vol_f = float(vol_shares)
        if amt_f > 0 and vol_f > 0:
            tentative = amt_f / max(vol_f, 1e-9)
            row["vwap"] = tentative / 100.0 if tentative > float(px or 0.0) * 20.0 else tentative

    if rt.get("tail_close") is not None:
        row["tail_close"] = float(rt["tail_close"])
    if rt.get("tail_vwap") is not None:
        row["tail_vwap"] = float(rt["tail_vwap"])

    if rt.get("pct_chg") is not None:
        row["pct_chg"] = float(rt["pct_chg"])
    elif px is not None and float(row.get("pre_close", prev_close) or 0) > 0:
        pc = float(row.get("pre_close", prev_close))
        row["pct_chg"] = (float(px) - pc) / pc * 100.0

    if rt.get("vol_ratio") is not None:
        row["vol_ratio"] = float(rt["vol_ratio"])

    for k in (
        "net_main_amount", "inst_net_buy",
        "winner_rate", "nineturn_signal", "limit_times", "strth", "circ_mv",
    ):
        if k in rt and rt[k] is not None and not (isinstance(rt[k], float) and pd.isna(rt[k])):
            row[k] = rt[k]

    if rt.get("pre_close") is None:
        if rt_td_raw is not None and rt_td > last_td:
            row["pre_close"] = prev_close
        elif len(work) >= 2:
            row["pre_close"] = float(pd.to_numeric(work.iloc[-2]["close"], errors="coerce") or prev_close)
        else:
            row["pre_close"] = float(pd.to_numeric(row.get("pre_close"), errors="coerce") or prev_close)

    close_f = float(pd.to_numeric(row.get("close"), errors="coerce") or 0.0)
    vol_hand = float(pd.to_numeric(row.get("vol"), errors="coerce") or 0.0)
    cmw = float(pd.to_numeric(row.get("circ_mv"), errors="coerce") or 0.0)
    if cmw <= 0 and "circ_mv" in work.columns:
        cmw = float(pd.to_numeric(work.iloc[-1]["circ_mv"], errors="coerce") or 0.0)
        row["circ_mv"] = cmw

    tr_rt = rt.get("turnover_rate_f")
    if tr_rt is not None and not (isinstance(tr_rt, float) and pd.isna(tr_rt)) and float(tr_rt) > 0:
        row["turnover_rate_f"] = float(tr_rt)
    elif "turnover_rate_f" in work.columns:
        row["turnover_rate_f"] = float(pd.to_numeric(work.iloc[-1]["turnover_rate_f"], errors="coerce") or 0.0)
    else:
        row["turnover_rate_f"] = 0.0
    if float(row.get("turnover_rate_f", 0.0) or 0.0) <= 0 and close_f > 0 and cmw > 0 and vol_hand > 0:
        row["turnover_rate_f"] = infer_turnover_rate_f_pct(vol_hand, close_f, cmw)

    if rt_td_raw is not None and rt_td > last_td:
        work = pd.concat([work, row.to_frame().T], ignore_index=True)
        # 【V34 P2-1】新行的指标必须重算，不能沿用上一日
        work = _recompute_p5_tail_indicators(work)
    else:
        work = pd.concat([work.iloc[:-1], row.to_frame().T], ignore_index=True)

    return work
