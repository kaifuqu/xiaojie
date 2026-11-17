# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 — P3 盘中选股池「物理胸甲」硬阈值筛选模块
================================================================================
对齐 data_fetcher.ALL_55_COLS；全局一票否决 + 11 个客观战法（满足任一即命中）。
【V35 战法编号重整 2026-11】原子战法重排为 P3-01~P3-11 连续编号，旧→新对照（供历史 signal_log 复盘）：
  旧01→P3-01 右侧起爆    旧02→P3-02 均线回踩低吸    旧03→P3-03 单峰跃迁
  旧04→P3-04 巨头连贯发力 旧05→P3-05 平台/二次确认   旧07→P3-06 资金逆势托底
  旧08→P3-07 倍量启动延续 旧09→P3-08 质量趋势底仓（重新启用）  旧10→P3-09 缩量分歧低吸
  旧15→P3-10 均线粘合突破（B1 辅助子策略）   旧18→P3-11 RSI超跌反转（观察档）
  已从代码层彻底删除的旧编号：06 水上金叉 / 11 缩量加速 / 12·13·14 早盘三战法（依赖不存在的
  rt['intraday_5min']）/ 16·17 B4 两战法（与 05·08 重复且存在计算错误）。

【V26.88 防镰刀优化】
① 拥挤过热阈值收紧（ordinary 2.8 / strong 3.8 / leader 4.5）
② 板块超额强度因子（龙头/跟风/phantom 分类）
③ 强化跟风/独自点火惩罚（strat_p3_intraday.py 中处理）
④ 早盘冲高回落检测（strat_p3_intraday.py 中处理）
⑤ 尾盘异动识别（strat_p3_intraday.py 中处理）
⑥ 战略赛道加成（电子/计算机/通信/军工/机械/电力/汽车/医药 +2.5 分）
⑦ 战略赛道容错（bias +2% / 允许 slope<=0 通过）

【增量 / 流式约定】
1. 单票单次调用复杂度 O(1)：仅访问 df 尾部固定窗口（如 tail(5)），不扫描全表、不聚合全市场。
2. 每次盘中快照只需传入「当前 rt」+「已缓存的昨日及以前日线 df」；禁止在模块内做 DuckDB 全表扫描。
3. 预估全日成交量：用 rt['volume']（股）换算为「手」后按已交易分钟数线性外推；若 rt 提供
   elapsed_mins / projected_vol_hand 则优先使用（流式任务可写入，避免重复计算时钟）。
4. 资金、筹码字段：优先读 rt（实时/快照），缺失时回退到 df 最后一根日线（昨收）上的同名列。

【部署摘要】（完整步骤见模块末尾注释与项目集成说明）
- 定时任务 / 行情推送：每只股票在回调里调用 evaluate_p3_intraday_screener(df, rt, cfg)。
- 批量：screen_p3_universe(rows) 仅对内存列表循环，适合「当前这一批快照」增量过滤。

================================================================================
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

try:
    from core.strategies.fund_mv_utils import (
        circ_mv_wan_from,
        dynamic_inst_single_threshold_yuan,
        dynamic_net_main_threshold_yuan,
        effective_turnover_rate_f,
        series_effective_turnover_f_daily,
    )
except ImportError:
    from strategies.fund_mv_utils import (  # type: ignore
        circ_mv_wan_from,
        dynamic_inst_single_threshold_yuan,
        dynamic_net_main_threshold_yuan,
        effective_turnover_rate_f,
        series_effective_turnover_f_daily,
    )

logger = logging.getLogger(__name__)

try:
    import constants as _C
    _P1_MIN_CIRC_MV_WAN = float(getattr(_C, "P1_SELECT_MIN_CIRC_MV_WAN", 1_000_000))
except Exception:
    _P1_MIN_CIRC_MV_WAN = 1_000_000.0

BJ_TZ = timezone(timedelta(hours=8))


@dataclass
class P3IntradayScreenerConfig:
    """P3 硬阈值：资金类为流通市值比例+阶梯地板；换手一律真实换手 turnover_rate_f（可反算）。"""

    # ---------- 全局 ----------
    # 与 P1 扫描下限对齐；策略四仍可用更高的 s4_circ_mv_min_wan 单独卡大票。
    circ_mv_min_wan: float = _P1_MIN_CIRC_MV_WAN
    # ---------- 【V29 涨幅硬闸门】用户硬约束：盘中涨幅越界一律不参与本池 ----------
    pct_hard_cap_pct: float = 5.0      # 涨幅 > +5%  -> 一票否决（盘中不追高）
    pct_hard_floor_pct: float = -1.5   # 涨幅 < -1.5% -> 一票否决（盘中已走坏）
    bias_20_max_pct: float = 6.5
    strong_stock_bias_20_max_pct: float = 12.0
    leader_bias_20_max_pct: float = 16.0
    strong_stock_circ_mv_mult: float = 1.4
    leader_sector_strength_min: float = 1.12
    leader_sector_rank_max: int = 2
    leader_sector_beta_min: float = 1.08
    leader_crowding_vr_floor: float = 1.55
    # ---------- 【V26.88 P3 优化①】拥挤过热阈值收紧（A股量化横行，原阈值过宽） ----------
    # 高位警戒：vr 快速拉升但价格无法跟上 → 量化出货前兆
    crowding_vr_high_warning: float = 2.2   # 普通/强势股 vr 超过此值且 bias>6% 发出预警（非否决）
    crowding_leader_vr_high: float = 3.0   # 龙头 vr 超过此值且 bias>8% 发出预警
    # 滞涨预警：涨速过快但量能不足 → 缩量赶顶
    stagnation_vr_max: float = 1.3         # bias>5% 时若 vr<此值 → 滞涨预警
    stagnation_bias_min: float = 5.0       # 滞涨判断的 bias 门槛（%）
    # ---------- 【V26.88 P3 优化③】T+1 涨停风险（抱团次日高开低走最常见） ----------
    limit_up_pct_thr: float = 9.4         # 昨日涨幅超过此值 → 触发 T+1 风险检查
    t1_vr_confirm_min: float = 2.0        # T+1 日 vr 需超过此值才能确认为真启动（否则量化抢跑）
    t1_winner_rate_min: float = 85.0      # T+1 日 winner_rate 最低门槛
    # ---------- 【V26.88 P3 优化④】量化盘口速度异常 ----------
    quant_speed_vol_ratio: float = 3.5      # 当日已成交量 / 预估同期均值 > 此值 → 量化介入预警
    quant_speed_big_cap_ratio: float = 4.5 # 大盘股（circ_mv>500亿）阈值更宽松

    # ---------- 【V26.88 P3 优化】2026慢牛格局专项 ----------
    # 战略赛道配置（与 pool_manager.STRATEGIC_INDUSTRIES 保持同步）
    strategic_industries: list = None
    # 【V26.88+9-23 新增】盈利质量 + 单季同比加分参数
    fin_score_qoq_yoy_bonus_threshold: float = 20.0   # 单季净利同比 ≥20% 加分
    fin_score_qoq_yoy_penalty_threshold: float = -30.0  # 单季净利同比 ≤-30% 减分
    fin_score_roe_strong: float = 15.0               # ROE ≥15% 加分
    fin_score_gross_margin_strong: float = 30.0      # 毛利率 ≥30% 加分
    fin_score_peg_attractive: float = 0.5           # PEG ≤0.5 加分（PE/growth）
    # 【V34 新增】仓位阶梯参数：P3 盘中仓位按「信号强度 / 分档共振 / 惩罚 / 时段 / 温度」折价，
    # 上限低于尾盘池（盘中持仓窗口短、无隔夜缓冲）。可在 config.yaml strategies.p3 下覆盖。
    p3_base_position_ratio: float = 0.10
    p3_max_position_ratio: float = 0.18
    p3_min_position_ratio: float = 0.03
    p3_score_scale_floor: float = 45.0
    p3_score_scale_ceiling: float = 85.0
    # 战略赛道容错参数
    strategic_bias_loosen_pct: float = 2.5
    strategic_trend_loosen: bool = True
    # 拥挤过热收紧（2026量化横行，原阈值过宽）
    # 【V26.88+ 9-30 第9项落地】3 档拥挤度阈值统一从 constants.CROWDING_VR_* 读取
    crowding_ordinary_vr: float = 2.5
    crowding_strong_vr: float = 3.5
    crowding_leader_vr: float = 4.2

    def __post_init__(self):
        """【V26.88+ 9-30 第9项落地】从 constants 同步放量阈值，避免散落硬编码。
        仅当 cfg.yaml 显式覆盖时才使用 dataclass 默认值，否则读 constants 全局基准。
        """
        try:
            # 拥挤度 3 档对齐（统一收口到 volume_threshold_utils）
            from core.strategies.volume_threshold_utils import (
                get_crowding_vr_thresholds,
                get_p3_vol_ma5_mult_s1,
                get_p3_vol_ma5_mult_s8,
            )
            ord_v, str_v, lead_v = get_crowding_vr_thresholds()
            self.crowding_ordinary_vr = float(ord_v)
            self.crowding_strong_vr = float(str_v)
            self.crowding_leader_vr = float(lead_v)
            # vol_ma5_mult 池内统一对齐
            self.s1_vol_ma5_mult = float(get_p3_vol_ma5_mult_s1())
            self.s8_vol_vs_vma5_mult = float(get_p3_vol_ma5_mult_s8())
        except Exception:
            # 兜底：保留 dataclass 默认值（与历史版本一致）
            pass
    # 机构主导市场 winner_rate 门槛（所有策略统一基准）
    winner_institution_floor: float = 82.0

    # ---------- 策略一 ----------
    s1_pct_low: float = 3.0
    s1_pct_high: float = 6.0
    s1_vol_ma5_mult: float = 1.5
    s1_winner_min: float = 85.0

    # ---------- 策略二 ----------
    s2_pct_high: float = 3.0
    s2_ma20_touch_ratio: float = 1.01
    s2_winner_min: float = 80.0
    s2_turnover_fly_max: float = 20.0
    s2_pb_fly_max: float = 10.0
    s2_pullback_bonus_bias_min: float = -2.5
    s2_pullback_bonus_bias_max: float = 2.5
    s2_pullback_close_to_ma20_pct: float = 1.5

    # ---------- 策略三 ----------
    s3_winner_min: float = 90.0
    s3_pct_low: float = 2.0
    s3_pct_high: float = 5.0
    s3_vol_ratio_min: float = 1.5
    s3_rsi_fly: float = 80.0

    # ---------- 策略四 ----------
    s4_circ_mv_min_wan: float = 3_000_000.0
    s4_net_main_ratio_of_float_mv: float = 0.00035
    s4_inst_net_buy_ratio_of_float_mv: float = 0.00006

    # ---------- 策略五 ----------
    s5_vol_ratio_min: float = 1.8
    s5_cost50_mult: float = 1.05
    s5_atr_fly_max: float = 6.0

    # ---------- 策略七：资金逆势托底（★右侧优化：涨幅下限收紧至2%）----------
    s7_pct_low: float = 2.0  # 【优化】原1.0→2.0，确保是上升趋势中的资金确认而非低位抄底
    s7_pct_high: float = 3.0
    s7_net_main_ratio_of_float_mv: float = 0.00025
    s7_pre_close_low_mult: float = 0.99
    s7_vol_ratio_low: float = 1.2
    s7_vol_ratio_high: float = 2.5
    # 【V26.88 P3 优化】换手率阈值从30%降低到20%，避免纳入高风险股票
    s7_turnover_fly: float = 20.0
    # ---------- 策略八：倍量启动延续（昨倍量阳 + 沿 5/10 多头）----------
    s8_vol_vs_vma5_mult: float = 1.85
    s8_pct_low: float = 0.8
    s8_pct_high: float = 5.5
    s8_vr_min: float = 1.15
    s8_winner_min: float = 78.0
    s8_ma20_slope_min: float = 0.15
    s8_yang_body_min: float = 0.998
    # ---------- 策略九：质量趋势底仓（★右侧优化：涨幅下限收紧至1.5%，确保是上升趋势）----------
    s9_pe_max: float = 20.0
    s9_turnover_f_max_lt: float = 4.0
    s9_pct_low: float = 1.5  # 【优化】原-1.0→1.5，避免低位抄底，要求必须是上升趋势
    s9_pct_high: float = 2.5  # 【优化】原1.5→2.5，收紧涨幅上限确保是右侧上攻
    s9_ma60_downtrend_rel: float = 0.98
    s9_cost50_mult: float = 1.05
    s9_winner_min: float = 78.0
    s9_enabled: bool = True
    # ---------- 策略十：缩量分歧低吸（★右侧优化：收紧bias容忍，避免深度回调）----------
    s10_touch_bias_ma20_low: float = -3.0  # 【优化】原-8.0→-3.0，避免深度回调抄底
    s10_touch_bias_ma20_high: float = 5.0
    s10_touch_bias_ma5_abs: float = 8.0
    s10_tail_window_min: int = 30
    s10_need_market_contraction: float = 0.7
    # ---------- 策略十八：RSI 超跌反转（源自 ai_quant_trade RSI 均值回归示例）----------
    # 定位：连续下跌后 RSI 进入超卖区，当日缩量企稳、盘中已脱离最低点 → 观察档低吸候选。
    # 只登记「观察档」（obs），不能单独否决/放行黄金门禁，命中后仍需 core/aux 共振才进池。
    # 编号为 P3-11（V35 连续编号重整，不再保留历史空洞）。
    s18_rsi_window: int = 14
    s18_rsi_oversold: float = 30.0
    s18_pct_low: float = -1.5   # 与全局 pct_hard_floor 对齐（盘中已跌超 1.5% 不入池）
    s18_pct_high: float = 2.0   # 超跌反转不追涨
    s18_vr_max: float = 1.2     # 缩量企稳：放量下跌是第二段杀跌，不接
    s18_close_off_low_pct: float = 1.0  # 现价须已脱离日内最低点 ≥1%（左侧底确认）
    s18_bias20_min: float = -12.0       # 防飞刀：偏离 20 日线过深（<-12%）视为下跌中继
    s18_winner_min: float = 0.0         # >0 时才校验 winner_rate（0=不启用，避免数据缺失误杀）
    # ---------- 环境与记忆增强 ----------
    regime_breaker_veto_score: float = 0.35
    regime_breaker_safety_mult: float = 0.82
    regime_boost_score: float = 1.08
    vwap_support_eps: float = 0.004
    t1_memory_weight: float = 0.22
    t1_memory_min_samples: int = 6
    t1_memory_score_boost: float = 8.0
    # ---------- 盘中成交量预测 ----------
    intraday_profile_lookback_days: int = 20
    intraday_anomaly_open_gap_pct: float = 2.2
    intraday_anomaly_limit_move_pct: float = 9.4
    intraday_anomaly_spike_ratio: float = 2.2
    intraday_profile_min_weight: float = 0.35
    intraday_profile_max_weight: float = 1.35
    intraday_profile_floor_ratio: float = 0.20
    intraday_profile_ceiling_ratio: float = 0.95
    # ---------- 突破类共性（防量价背离 / 假突破）----------
    breakout_yang_vol_ratio_min: float = 1.05  # 近5日阳量均值 ≥ 阴量×该系数
    breakout_vwap_eps: float = 0.004  # 现价不得低于分时 VWAP 超过该比例（有额量时生效）
    giant_min_vol_ratio: float = 1.2  # 巨头连贯策略最低量比


def _resolve_p3_cfg(cfg: Optional[P3IntradayScreenerConfig]) -> P3IntradayScreenerConfig:
    if cfg is not None:
        return cfg
    from core.config_manager import get_p3_intraday_screener_config

    return get_p3_intraday_screener_config()


DEFAULT_P3_CONFIG = P3IntradayScreenerConfig()


def _safe_float(val: Any, default: float = 0.0) -> float:
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


def _macd_hist_series(row: pd.Series) -> float:
    """
    MACD 柱（1× 统一口径）。

    【V30.10 统一 MACD 口径 2026-10-21】原实现的「macd_hist 为 0 时回退 macd_bar」
    会把 1× 与 2× 两种尺度混在一起，与绝对值阈值比较时宽严差一倍。
    现统一走 core.strategies.macd_unified。
    """
    try:
        from core.strategies.macd_unified import macd_hist_normalized

        v = macd_hist_normalized(row)
        return float(v) if v is not None else 0.0
    except Exception:
        v = _safe_float(row.get("macd_hist"), 0.0)
        if v == 0.0 and "macd_bar" in row.index:
            v = _safe_float(row.get("macd_bar"), 0.0)
        return v


def _high_20_prev(df: pd.DataFrame, y: pd.Series) -> float:
    """
    【2026-10-15 审计修复 · high_20 含当日导致突破条件恒假】
    根因：daily_data.high_20 = high.rolling(20).max()，**包含当日**，
          因此恒有 high_20 >= high >= close ⇒ `now_px > high_20` 永不成立。
    证据：daily_data 全量 218499 行中 close>high_20 命中 0 次；
          换成不含当日的 20 日高点后命中 12578 次（5.76%）。
          盘中实测（末行为当日实时行）：high_20 = 1400.0 = 当日 high，
          `close(1400) > high_20(1400)` 仍为 False，同为死条件。
    影响：P3-08 单峰跃迁、P3-05 平台二次确认 两条战法突破判定恒假。
    先例：P5 已于 p5_postmarket_screener.py:349 用 high_20_prev 修过同一 BUG，
          P3/P4 此前均未同步（本次已在 P4 同步修复）。
    改法：优先用 high_20_prev 列（若上游已产出），否则用 high 排除末行后取 20 日最大值。
          不改任何阈值 / 权重 / 过滤规则，只修正「突破基准」这一事实性错误。
    """
    if "high_20_prev" in df.columns:
        v = _safe_float(y.get("high_20_prev"), 0.0)
        if v > 0:
            return v
    if "high" not in df.columns:
        return 0.0
    hi = pd.to_numeric(df["high"], errors="coerce").dropna()
    if hi.empty:
        return 0.0
    # 排除末行（可能是当日实时行），取前 20 根最高价
    prev = hi.iloc[:-1] if len(hi) >= 2 else hi
    if prev.empty:
        return _safe_float(y.get("high_20"), 0.0)
    return float(prev.tail(20).max())


def _recent_yang_yin_vol_balance(df: pd.DataFrame, n: int = 5, ratio_min: float = 1.05) -> bool:
    """
    近 n 根已收盘 K：上涨日成交量均值 vs 下跌日成交量均值（右侧放量须阳量占优）。
    分母为 0 时返回 False，避免除零。
    """
    if df is None or len(df) < max(3, n):
        return False
    tail = df.tail(n)
    vc = "vol" if "vol" in tail.columns else ("volume" if "volume" in tail.columns else None)
    if vc is None:
        return False
    if "pct_chg" in tail.columns:
        pch = pd.to_numeric(tail["pct_chg"], errors="coerce").fillna(0.0)
    else:
        pch = pd.Series(0.0, index=tail.index)
    # 【V29 修复】按 pct_chg 严格二分涨跌；一字星/十字星(pct==0)既不计涨也不计跌，
    # 不再因「两边都没量」而被误判为结构失败（横盘整理是平台突破的合法前置形态）。
    vols = pd.to_numeric(tail[vc], errors="coerce").fillna(0.0)
    up = vols[pch > 0]
    dn = vols[pch < 0]
    av_u = float(up.mean()) if not up.empty else 0.0
    av_d = float(dn.mean()) if not dn.empty else 0.0
    if vc == "volume":
        av_u /= 100.0
        av_d /= 100.0
    if av_d <= 0:
        # 无有效下跌日：只要区间内确有成交（阳线有量，或纯横盘缩量整理）即视为结构健康
        return av_u > 0 or float(vols.sum()) > 0
    return av_u >= av_d * ratio_min


def _fin_quality_score(rt: Dict[str, Any], cfg: P3IntradayScreenerConfig) -> Tuple[float, str]:
    """
    【V26.88+9-23 新增】盈利质量 + 单季同比综合打分

    加分维度（每项 +1，最多 +4）:
      1. 单季净利同比 ≥ 20%        → +1
      2. ROE ≥ 15%                 → +1
      3. 毛利率 ≥ 30%              → +1
      4. PEG ≤ 0.5 (PE/单季同比增速) → +1

    减分维度（最多 -3）:
      1. 单季净利同比 ≤ -30%        → -2（已塌方）
      2. 单季营收同比 ≤ -15%        → -1
      3. ROE < 0                  → -1（亏损）

    数据来源：Tushare fina_indicator 已落库到 fact_financial_reports（V26.88+9-23 增强）。
    调用方：analyze_stock_financial_quality(rt) → 把最新财季指标注入 rt。
    """
    score = 0.0
    tags: List[str] = []

    # === 加分 ===
    qoq_yoy = _safe_float(rt.get("qoq_profit_yoy"))
    if np.isfinite(qoq_yoy) and qoq_yoy >= float(getattr(cfg, "fin_score_qoq_yoy_bonus_threshold", 20.0)):
        score += 1.0
        tags.append(f"单季同比+{qoq_yoy:.0f}%")

    roe = _safe_float(rt.get("roe_pct"))
    if np.isfinite(roe) and roe >= float(getattr(cfg, "fin_score_roe_strong", 15.0)):
        score += 1.0
        tags.append(f"ROE {roe:.1f}%")

    gross = _safe_float(rt.get("gross_margin"))
    if np.isfinite(gross) and gross >= float(getattr(cfg, "fin_score_gross_margin_strong", 30.0)):
        score += 1.0
        tags.append(f"毛利率{gross:.0f}%")

    # PEG = PE_TTM / max(单季净利同比, 0.1)
    pe = _safe_float(rt.get("pe_ttm"))
    if np.isfinite(pe) and np.isfinite(qoq_yoy) and qoq_yoy > 0:
        growth = max(qoq_yoy, 0.1)
        peg = pe / growth
        if peg <= float(getattr(cfg, "fin_score_peg_attractive", 0.5)):
            score += 1.0
            tags.append(f"PEG {peg:.2f}")

    # === 减分 ===
    if np.isfinite(qoq_yoy) and qoq_yoy <= float(getattr(cfg, "fin_score_qoq_yoy_penalty_threshold", -30.0)):
        score -= 2.0
        tags.append(f"单季同比{qoq_yoy:.0f}%(塌方)")

    qoq_rev = _safe_float(rt.get("qoq_revenue_yoy"))
    if np.isfinite(qoq_rev) and qoq_rev <= -15.0:
        score -= 1.0
        tags.append(f"单季营收{qoq_rev:.0f}%")

    if np.isfinite(roe) and roe < 0:
        score -= 1.0
        tags.append("ROE负")

    if not tags:
        return 0.0, "盈利质量(中性)"
    return float(np.clip(score, -3.0, 4.0)), "盈利质量:" + "+".join(tags) if score >= 0 else "盈利质量:" + ",".join(tags)


def _estimate_vwap_from_rt(rt: Dict[str, Any], ref_price: float) -> float:
    """与 strat_p3 一致：amount(元) / volume(股)；异常量纲时尝试按手折算。

    [V26.88+10-9 修复] amount 或 volume 缺失时, 静默返 0 → 调用方 _price_on_vwap_ok
    会直接 return True (等同关闭 VWAP 检查). 加 debug 级别日志便于断流排查.
    """
    amt = _safe_float(rt.get("amount"), 0.0)
    vol = _safe_float(rt.get("volume"), 0.0)
    if amt <= 0 or vol <= 0:
        ts_code = str(rt.get("ts_code") or rt.get("代码") or "?")
        logger.debug("P3 VWAP 缺数据: ts_code=%s amount=%s volume=%s (检查将静默通过)",
                    ts_code, amt, vol)
        return 0.0
    tentative = amt / max(vol, 1e-9)
    if tentative > ref_price * 20.0:
        return amt / max(vol * 100.0, 1e-9)
    return tentative


def _price_on_vwap_ok(rt: Dict[str, Any], now_px: float, eps: float) -> bool:
    """有分时额量时要求现价站在 VWAP 之上（或贴邻），否则不判定（缺数据不硬杀）。"""
    vw = _estimate_vwap_from_rt(rt, now_px)
    if vw <= 0 or now_px <= 0:
        return True
    return now_px >= vw * (1.0 - eps)


def _field_rt_or_bar(rt: Dict[str, Any], y: pd.Series, key: str, default: float = 0.0) -> float:
    """优先实时 rt，其次昨日日线 y。"""
    raw = rt.get(key)
    if raw is not None and not (isinstance(raw, float) and pd.isna(raw)):
        return _safe_float(raw, default)
    return _safe_float(y.get(key), default)


def _rsi_sma(close: pd.Series, window: int = 14) -> float:
    """SMA 口径 RSI（与 ai_quant_trade RSI 示例同口径：简单平均涨跌幅）。

    返回最后值；数据不足或退化时返回 NaN，由调用方 fail-closed。
    """
    try:
        s = pd.to_numeric(close, errors="coerce").dropna()
    except Exception:
        return float("nan")
    if len(s) <= window:
        return float("nan")
    changes = s.diff().dropna()
    if len(changes) < window:
        return float("nan")
    recent = changes.tail(window)
    gains = float(recent.clip(lower=0.0).mean())
    losses = float((-recent.clip(upper=0.0)).mean())
    if losses == 0.0:
        return 100.0
    rs = gains / losses
    return 100.0 - 100.0 / (1.0 + rs)


def _p3_mainline_score(
    rt: Dict[str, Any], y: pd.Series, cfg: Optional[P3IntradayScreenerConfig] = None
) -> Tuple[float, str, Dict[str, Any]]:
    """
    主线识别 + 【V26.88 P3 优化②/⑥】板块超额强度因子 + 战略赛道加成。

    在「板块绝对强度」基础上叠加「个股相对板块的超额涨幅」，
    区分三种拥挤类型：
      crowd_leader:  板块强 + 个股更强 → 机构真建仓龙头
      crowd_follower: 板块强 + 个股偏弱 → 量化跟风（溢价买入）
      crowd_phantom:  板块弱 + 个股独自拉升 → 主力独自点火（高风险）

    【V26.88 P3 优化⑥】战略赛道加成：属于 STRATEGIC_INDUSTRIES 的个股，
    在板块维度额外获得「机构主线认定」加成，鼓励底仓配置型资金入场。

    返回: (score, reason, extra)
      extra: {excess_strength, crowd_type, is_crowd_leader, is_crowd_follower, is_crowd_phantom,
              is_strategic_industry, strategic_bonus, hithink_market_bias}
    """
    # V26.88+9-22 优化9：HiThink 大盘指数实时锚（与 P2 共用注入结果）
    hithink_strength = _safe_float(rt.get("_hithink_index_strength"), 1.0)
    if hithink_strength < 0.5:    hithink_bias = -1.5
    elif hithink_strength < 1.0:  hithink_bias = -0.5
    elif hithink_strength >= 1.5: hithink_bias = 1.5
    elif hithink_strength >= 1.2: hithink_bias = 0.8
    else:                          hithink_bias = 0.0  # 1.0 <= hithink_strength < 1.2 → 中性

    sector_strength = _safe_float(
        rt.get("sector_strength",
               rt.get("industry_strength",
                      rt.get("sector_beta", 1.0))), 1.0)
    # [V26.88+10-9 修复] 区分"中性 1.0" vs "数据缺失": 三个 alias 全 None 时打 debug 日志
    if not any(k in rt for k in ("sector_strength", "industry_strength", "sector_beta")):
        ts_code = str(rt.get("ts_code") or rt.get("代码") or "?")
        logger.debug("P3 板块强度数据缺失: ts_code=%s (3个 alias 全空, 按中性1.0处理)", ts_code)
    sector_rank = _safe_float(rt.get("sector_rank"), 999.0)
    sector_total = _safe_float(rt.get("sector_total"), 0.0)
    board_beta = _safe_float(
        rt.get("sector_beta",
               rt.get("industry_beta",
                      rt.get("sector_mult", 1.0))), 1.0)
    vr = _safe_float(rt.get("vol_ratio"), _safe_float(y.get("vol_ratio"), 0.0))
    now_px = _safe_float(rt.get("price"), 0.0)
    vwap = _safe_float(rt.get("vwap"), now_px)
    pre_close = _safe_float(rt.get("pre_close"), _safe_float(y.get("close"), 0.0))
    pct_chg = (now_px - pre_close) / pre_close * 100.0 if now_px > 0 and pre_close > 0 else 0.0

    score = 0.0
    reasons = []

    # ---- 原有板块维度 ----
    if sector_strength >= 1.12:
        score += 3.0; reasons.append("强板块")
    elif sector_strength >= 1.05:
        score += 1.8; reasons.append("中强板块")
    elif sector_strength <= 0.95:
        score -= 2.2; reasons.append("板块偏弱")

    if board_beta >= 1.10:
        score += 1.0; reasons.append("板块扩散")
    elif board_beta <= 0.92:
        score -= 1.0; reasons.append("板块退潮")

    if sector_total >= 3 and sector_rank > 0:
        if sector_rank <= 2:
            score += 2.0; reasons.append("板块前排")
        elif sector_rank <= 5:
            score += 1.0; reasons.append("板块前列")
        elif sector_rank > sector_total - 3:
            score -= 2.0; reasons.append("板块后排")

    if now_px > 0 and vwap > 0:
        if now_px >= vwap:
            score += 1.0; reasons.append("站稳VWAP")
        else:
            score -= 1.5; reasons.append("VWAP下方")

    if pct_chg >= 2.5 and vr < 1.2:
        score -= 1.5; reasons.append("冲高无量")
    if pct_chg >= 3.0 and now_px < vwap * 1.002:
        score -= 2.0; reasons.append("假突破回落")
    if pct_chg >= 4.0 and sector_strength < 1.05:
        score -= 1.2; reasons.append("强度不匹配")

    if vr >= 1.2:
        score += 0.8
    if pct_chg >= 1.0:
        score += 0.8
    elif pct_chg < 0:
        score -= 0.8

    # ---- 【V26.88 P3 优化②】板块超额强度因子 ----
    # 核心：个股涨幅 pct_chg 与板块强度 sector_strength 是否匹配
    # pct_chg > 3% 且 sector_strength < 1.05 → 跟风拉升（量化介入概率高）
    # pct_chg > 5% 且 sector_strength < 1.05 → 独自点火（主力控盘但高风险）
    excess_strength = 0.0   # 正=超额，负=落后
    crowd_type = "normal"

    # 用板块强度与个股涨幅的偏差来估算超额
    # 经验阈值：sector_strength=1.0 时板块平均涨幅约 1%
    sector_expected_pct = (sector_strength - 1.0) * 20.0  # 粗估板块应涨 %
    excess_pct = pct_chg - sector_expected_pct

    if sector_strength >= 1.08:  # 强板块内
        if pct_chg >= 3.0 and excess_pct >= 1.5:
            # 个股涨幅明显跑赢板块 → 可能是龙头
            excess_strength = 2.0
            crowd_type = "crowd_leader"
            reasons.append("龙头超额")
        elif pct_chg >= 1.0 and excess_pct >= -0.5:
            # 个股随板块，差距不大 → 正常跟随
            excess_strength = 0.0
            crowd_type = "normal"
        elif pct_chg >= 2.0 and sector_strength >= 1.05 and excess_pct < -1.0:
            # 强板块中个股涨幅低于预期 → 量化跟风（被动溢价买入）
            excess_strength = -1.5
            crowd_type = "crowd_follower"
            reasons.append("跟风拉升")
    elif sector_strength < 1.0:  # 弱/冷板块
        if pct_chg >= 3.0 and excess_pct >= 2.0:
            # 弱板块独自拉升 → 主力点火（高风险）
            excess_strength = -2.0
            crowd_type = "crowd_phantom"
            reasons.append("独自点火高风险")
        elif pct_chg >= 2.0:
            excess_strength = -1.0
            crowd_type = "crowd_follower"

    # ---- 【V26.88 P3 优化⑥】战略赛道加成：机构主导市场的「主线认定」----
    strategic_bonus = 0.0
    is_strategic_industry = False
    if cfg is not None:
        strat_inds = getattr(cfg, "strategic_industries", None)
        if strat_inds:
            ind_name = str(rt.get("industry", rt.get("sector", ""))).strip()
            if ind_name in strat_inds:
                is_strategic_industry = True
                # 战略赛道 + 强板块 + 机构认可 → 加分
                if sector_strength >= 1.05 and board_beta >= 1.05:
                    strategic_bonus = 2.5
                    reasons.append("战略主线认定")
                elif sector_strength >= 1.05:
                    strategic_bonus = 1.5
                    reasons.append("战略赛道")
                elif sector_strength >= 1.0:
                    strategic_bonus = 0.8
                    reasons.append("战略行业")
                score += strategic_bonus

    # V26.88+9-22 优化9：HiThink 大盘锚注入主线打分（大盘极强 → 全市场系统性做多加分）
    score += hithink_bias
    if hithink_bias != 0.0:
        reasons.append(f"A股大盘{'极强' if hithink_bias > 0 else '极弱'}(ht={hithink_strength:.1f})")

    extra = {
        "excess_strength": round(excess_strength, 2),
        "crowd_type": crowd_type,
        "is_crowd_leader": crowd_type == "crowd_leader",
        "is_crowd_follower": crowd_type == "crowd_follower",
        "is_crowd_phantom": crowd_type == "crowd_phantom",
        "is_strategic_industry": is_strategic_industry,
        "strategic_bonus": round(float(strategic_bonus), 2),
        "hithink_market_bias": round(hithink_bias, 2),   # V26.88+9-22 优化9
    }
    return float(score), "/".join(reasons) if reasons else "中性", extra


def _fund_flow_signal(rt: Dict[str, Any], y: pd.Series, now_px: float) -> Tuple[float, str, Dict[str, float]]:
    """资金流评分：不做硬门禁，仅提供加分、背离和风险提示。"""
    cm = _safe_float(rt.get("circ_mv"), _safe_float(y.get("circ_mv"), 0.0))
    if cm <= 0:
        cm = _safe_float(y.get("circ_mv"), 0.0)
    nm_rt = _safe_float(rt.get("net_main_amount"), np.nan)
    nm_y = _safe_float(y.get("net_main_amount"), 0.0)
    nm = nm_rt if not pd.isna(nm_rt) else nm_y
    inst = _safe_float(rt.get("inst_net_buy"), _safe_float(y.get("inst_net_buy"), 0.0))
    vr = _safe_float(rt.get("vol_ratio"), _safe_float(y.get("vol_ratio"), 0.0))
    pre_close = _safe_float(rt.get("pre_close"), _safe_float(y.get("close"), 0.0))
    pct = (now_px - pre_close) / pre_close * 100.0 if now_px > 0 and pre_close > 0 else 0.0
    ma20 = _safe_float(y.get("ma20"), 0.0)
    bias20 = (now_px - ma20) / ma20 * 100.0 if now_px > 0 and ma20 > 0 else 0.0

    score = 0.0
    notes = []
    metrics = {"net_main_amount": nm, "inst_net_buy": inst, "fund_bias_20": bias20}

    if cm > 0:
        nm_ratio = nm / cm
        inst_ratio = inst / cm
        if nm > 0:
            score += 1.2
            notes.append("主力净流入")
            if nm_ratio >= 0.00025:
                score += 0.8
                notes.append("主力强度足")
        else:
            score -= 0.4
            notes.append("主力净流出")
            if pct > 0.8 and vr >= 1.2 and bias20 >= -1.5:
                score += 0.6
                notes.append("价强量配合")
            if pct > 2.0 and vr < 1.2:
                score -= 0.8
                notes.append("冲高无量背离")

        if inst > 0:
            score += 0.6
            notes.append("机构净买入")
            if inst_ratio >= 0.00006:
                score += 0.4
                notes.append("机构强度足")
        else:
            score -= 0.2
            notes.append("机构净卖出")

        if nm > 0 and inst > 0:
            score += 0.4
            notes.append("资金同向")
        elif nm < 0 and inst < 0:
            score -= 0.4
            notes.append("资金同向流出")

    if pct >= 2.0 and nm > 0:
        score += 0.4
    if pct <= 0 and nm > 0:
        notes.append("资金领先但价格未证实")
        score -= 0.2

    return float(score), "/".join(notes) if notes else "资金中性", metrics


def _intraday_elapsed_minutes(rt: Dict[str, Any]) -> int:
    """
    已交易分钟数（与 scan_engine 主会话 9:30~15:00 对齐，中间非连续简化为 240 分钟满额日）。
    流式场景可在 rt['elapsed_mins'] 注入，避免多股重复调用系统时钟。
    """
    em = rt.get("elapsed_mins")
    if em is not None:
        return max(1, int(_safe_float(em, 1.0)))
    try:
        now = datetime.now(BJ_TZ)
        cm = now.hour * 60 + now.minute
        if cm < 565 or cm > 900:
            return 240
        if cm < 570:
            return 1
        if cm <= 690:            # 09:30-11:30 上午连续交易
            return max(1, cm - 570)
        if cm < 780:             # 11:30-13:00 午休：停在上午满额 120 分钟
            return 120
        # 【V29 修复】13:00 后重新计数，避免「已交易分钟」虚高 60 分钟
        # （旧实现 13:00 记为 210 分钟 → 分时占比桶提前跳到 close，全日量被系统性低估）
        return 120 + (cm - 780)   # 13:00 精确起点 = 120 分，不再被 max(1,…) 多算 1 分
    except Exception as _e:
        # 【全局审计修复】维度2：交易分钟推算失败时回退 120，打 debug 与风控引擎口径对齐
        logging.getLogger(__name__).debug("_intraday_elapsed_minutes 异常回退 120: %s", _e, exc_info=True)
        return 120


def _intraday_time_ratio(rt: Dict[str, Any]) -> float:
    """按交易时段粗略估计当日进度（9:30~15:00 记为 1.0，午间不连续时段按满额修正）。"""
    em = max(1, _intraday_elapsed_minutes(rt))
    return float(min(1.0, max(0.01, em / 240.0)))


def _intraday_profile_bucket(rt: Dict[str, Any]) -> str:
    em = max(1, _intraday_elapsed_minutes(rt))
    if em <= 30:
        return "open"
    if em <= 90:
        return "morning"
    if em <= 160:
        return "midday"
    if em <= 210:
        return "afternoon"
    return "close"


def _intraday_profile_base_share(bucket: str) -> float:
    profile = {
        "open": 0.28,
        "morning": 0.50,
        "midday": 0.70,
        "afternoon": 0.84,
        "close": 0.96,
    }
    return float(profile.get(bucket, 0.70))


def _intraday_anomaly_weight(rt: Dict[str, Any], y: pd.Series) -> float:
    """异常日权重：开盘跳空、涨停/跌停、极端放量时，降低历史模板权重。

    【V26.88+ 9-30】朋友2指出 9.4 硬阈值不能区分板块；
    改用 board_limit_utils.get_limit_up_pct_fast(ts_code, name) 动态计算涨停阈值。
    """
    now_px = _safe_float(rt.get("price"), 0.0)
    pre_close = _safe_float(rt.get("pre_close"), _safe_float(y.get("close"), 0.0))
    open_px = _safe_float(rt.get("open"), now_px)
    high_px = _safe_float(rt.get("high"), now_px)
    low_px = _safe_float(rt.get("low"), now_px)
    pct = (now_px - pre_close) / pre_close * 100.0 if now_px > 0 and pre_close > 0 else 0.0
    gap_pct = (open_px - pre_close) / pre_close * 100.0 if open_px > 0 and pre_close > 0 else 0.0
    vr = _safe_float(rt.get("vol_ratio"), _safe_float(y.get("vol_ratio"), 0.0))

    # 板块动态涨停阈值（创业板/科创板 19.5%、北交所 29.5%、主板 9.5%、ST 4.5%）
    _ts = str(rt.get("ts_code", ""))
    _name = str(rt.get("name", rt.get("stock_name", "")))
    _dynamic_limit_up = 9.5  # 兜底
    try:
        from core.strategies.board_limit_utils import get_limit_up_pct_fast
        _dynamic_limit_up = float(get_limit_up_pct_fast(_ts, _name))
    except Exception:
        pass

    weight = 1.0
    if abs(gap_pct) >= 2.2:
        weight *= 0.78
    # 触达板块涨停阈值（或已高过 95% 涨幅）→ 视为异常日
    if pct >= _dynamic_limit_up * 0.95 or high_px >= pre_close * (1.0 + _dynamic_limit_up / 100.0 * 0.95):
        weight *= 0.62
    if pct <= -_dynamic_limit_up * 0.95 or low_px <= pre_close * (1.0 - _dynamic_limit_up / 100.0 * 0.95):
        weight *= 0.62
    if vr >= 2.2:
        weight *= 0.82
    return float(max(0.35, min(1.0, weight)))


def _hist_intraday_volume_share(df: pd.DataFrame, bucket: str, lookback_days: int = 20) -> float:
    """用历史日线的量能特征估算当前时点已完成全天量的比例，缺少字段时退回时段基准。"""
    if df is None or df.empty:
        return _intraday_profile_base_share(bucket)
    hist = df.tail(max(5, lookback_days))
    cols = [c for c in ["vol_ratio", "turnover_rate_f", "turnover_f", "volume", "vol"] if c in hist.columns]
    if not cols:
        return _intraday_profile_base_share(bucket)
    base = _intraday_profile_base_share(bucket)
    metric = pd.to_numeric(hist[cols[0]], errors="coerce").replace([np.inf, -np.inf], np.nan).dropna()
    if metric.empty:
        return base
    # 历史越强，通常同一时点的量能完成度略高；越弱则略低
    med = float(metric.median())
    last = float(metric.iloc[-1])
    delta = 0.0 if med <= 0 else (last - med) / max(abs(med), 1e-9)
    return float(np.clip(base + delta * 0.04, 0.18, 0.98))


def projected_vol_hand_today(rt: Dict[str, Any], y: pd.Series) -> float:
    """
    预估当日总成交量（手）：采用历史分时占比模板 + 异常权重的非线性估算。
    若 rt['projected_vol_hand'] 已存在则直接使用（上游流式只算一次）。
    """
    pv = rt.get("projected_vol_hand")
    if pv is not None and _safe_float(pv, 0.0) > 0:
        return _safe_float(pv, 0.0)
    vol_shares = _safe_float(rt.get("volume"), 0.0)
    if vol_shares <= 0:
        return 0.0
    vol_hand = vol_shares / 100.0
    bucket = _intraday_profile_bucket(rt)
    elapsed_ratio = _intraday_time_ratio(rt)
    hist_share = _hist_intraday_volume_share(y.to_frame().T if isinstance(y, pd.Series) else pd.DataFrame(), bucket, 20)
    anomaly_w = _intraday_anomaly_weight(rt, y)
    effective_share = max(0.12, min(0.98, hist_share * anomaly_w))
    if elapsed_ratio > 0:
        effective_share = min(effective_share, max(0.12, elapsed_ratio))
    projected = vol_hand / max(effective_share, 1e-6)
    projected *= float(np.clip((0.72 + 0.28 * anomaly_w), 0.7, 1.05))
    return float(max(vol_hand, projected))


def _resolve_risk_mode(rt: Dict[str, Any], y: pd.Series, cfg: P3IntradayScreenerConfig) -> str:
    """普通股 / 强势股 / 主线龙头 三层风控分级。"""
    sector_strength, _, _ = _p3_mainline_score(rt, y, cfg)
    sector_rank = _safe_float(rt.get("sector_rank"), 999.0)
    sector_beta = _safe_float(rt.get("sector_beta", rt.get("industry_beta", 1.0)), 1.0)
    cm = _safe_float(rt.get("circ_mv"), _safe_float(y.get("circ_mv"), 0.0))
    base_cmw = max(cfg.circ_mv_min_wan, 1.0)

    is_leader = (
        sector_strength >= 2.8
        and sector_rank > 0
        and sector_rank <= cfg.leader_sector_rank_max
        and sector_beta >= cfg.leader_sector_beta_min
    )
    if is_leader:
        return "leader"

    is_strong = (
        sector_strength >= 1.4
        or cm >= base_cmw * cfg.strong_stock_circ_mv_mult
        or sector_beta >= 1.03
    )
    if is_strong:
        return "strong"

    return "ordinary"


def _p3_populate_quant_warnings(
    rt: Dict[str, Any],
    df: pd.DataFrame,
    y: pd.Series,
    mode: str,
    bias_live: float,
    vr: float,
    cfg: P3IntradayScreenerConfig,
) -> None:
    """
    【V26.88 P3 优化①/③/④】
    将量化横行相关预警写入 rt 字典（副作用），由 evaluate_p3_intraday_screener 透传到 out["detail"]。
    预警键：
      _quant_warnings: list of str（预警文本列表）
      _is_stagnation: bool
      _is_t1_risk: bool
      _is_quant_speed: bool
    """
    warnings: List[str] = []
    rt["_quant_warnings"] = warnings
    rt["_is_stagnation"] = False
    rt["_is_t1_risk"] = False
    rt["_is_quant_speed"] = False

    # ----- ① 滞涨预警（量化出货前兆：涨速过快但量能跟不上） -----
    if bias_live >= cfg.stagnation_bias_min and vr < cfg.stagnation_vr_max:
        rt["_is_stagnation"] = True
        tag = f"滞涨⚠️涨{abs(bias_live):.1f}%但量比{vr:.2f}（量化赶顶嫌疑）"
        warnings.append(tag)
    elif mode in ("strong", "leader") and vr >= cfg.crowding_leader_vr_high and bias_live > 8.0:
        rt["_is_stagnation"] = True
        tag = f"拥挤⚠️{mode}vr={vr:.2f}偏热且bias={bias_live:.1f}%"
        warnings.append(tag)

    # ----- ③ T+1 涨停风险（抱团次日高开低走最常见） -----
    if df is not None and len(df) >= 2:
        prev = df.iloc[-2]
        prev_pct = _safe_float(prev.get("pct_chg"), 0.0)
        if prev_pct >= cfg.limit_up_pct_thr:
            now_px = _safe_float(rt.get("price"), 0.0)
            pre_close = _safe_float(rt.get("pre_close"), _safe_float(y.get("close"), 0.0))
            pct_today = (now_px - pre_close) / pre_close * 100.0 if now_px > 0 and pre_close > 0 else 0.0
            if vr < cfg.t1_vr_confirm_min:
                rt["_is_t1_risk"] = True
                warnings.append(f"T+1预警⚠️昨涨幅{prev_pct:.1f}%但今日vr={vr:.2f}不足（抢跑风险）")
            else:
                curr_wr = _field_rt_or_bar(rt, y, "winner_rate", default=0.0)
                prev_wr = _safe_float(prev.get("winner_rate"), 0.0)
                if curr_wr > 0 and curr_wr < cfg.t1_winner_rate_min:
                    rt["_is_t1_risk"] = True
                    warnings.append(f"T+1预警⚠️昨冲高{prev_pct:.1f}%但winner={curr_wr:.0f}%偏低")
                elif prev_wr > 0 and curr_wr > 0 and curr_wr < prev_wr * 0.75:
                    rt["_is_t1_risk"] = True
                    warnings.append(f"T+1预警⚠️winner从昨{prev_wr:.0f}%降至今日{curr_wr:.0f}%")

    # ----- ④ 量化盘口速度异常 -----
    # 原理：当日已成交量远超标称同期均值 → 高频量化介入
    elapsed_mins = _intraday_elapsed_minutes(rt)
    circ_mv = _safe_float(rt.get("circ_mv"), _safe_float(y.get("circ_mv"), 0.0))
    circ_mv_yi = circ_mv / 10000.0 if circ_mv > 0 else 0.0
    if elapsed_mins >= 30:
        speed_ratio_thr = (
            cfg.quant_speed_big_cap_ratio
            if circ_mv_yi >= 500.0
            else cfg.quant_speed_vol_ratio
        )
        # 当 vr 超过阈值 65% 时发出预警
        if vr >= speed_ratio_thr * 0.65 and elapsed_mins <= 120:
            rt["_is_quant_speed"] = True
            warnings.append(f"量化介入⚠️vr={vr:.2f}偏快（阈值{speed_ratio_thr}，疑似高频）")


def _calc_volatility_adjusted_bias_limit(
    df: Optional[pd.DataFrame],
    mode: str,
    cfg: P3IntradayScreenerConfig,
) -> float:
    """
    【V26.88 新增】波动率自适应bias阈值。
    - 高波动股票（涨停股、强势股）天然会有更高bias，固定阈值会误杀
    - 低波动股票需要更严格的bias控制

    返回：根据波动率调整后的bias阈值
    """
    try:
        if df is None or len(df) < 20 or "close" not in df.columns:
            return cfg.leader_bias_20_max_pct if mode == "leader" else (cfg.strong_stock_bias_20_max_pct if mode == "strong" else cfg.bias_20_max_pct)

        # 计算近20日收益率标准差作为波动率代理
        recent_prices = df["close"].iloc[-20:].values
        returns = np.diff(recent_prices) / recent_prices[:-1] * 100.0
        volatility_std = float(np.nanstd(returns))

        # 根据波动率调整阈值
        if volatility_std > 4.0:
            adjustment = 1.2  # 高波动：阈值放宽20%
        elif volatility_std > 3.0:
            adjustment = 1.1  # 中高波动：阈值放宽10%
        elif volatility_std < 1.5:
            adjustment = 0.85  # 低波动：阈值收紧15%
        elif volatility_std < 2.0:
            adjustment = 0.95  # 中低波动：阈值收紧5%
        else:
            adjustment = 1.0  # 正常波动：不变

        if mode == "leader":
            base = cfg.leader_bias_20_max_pct
        elif mode == "strong":
            base = cfg.strong_stock_bias_20_max_pct
        else:
            base = cfg.bias_20_max_pct

        return base * adjustment
    except Exception:
        return cfg.leader_bias_20_max_pct if mode == "leader" else (cfg.strong_stock_bias_20_max_pct if mode == "strong" else cfg.bias_20_max_pct)


def p3_global_risk_veto(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    cfg: Optional[P3IntradayScreenerConfig] = None,
) -> Tuple[bool, str]:
    """
    P3 全局风控（一票否决）。

    使用「实时价」= rt['price']（缺失则无法用乖离/破位逻辑，直接否决）。
    资金流不再作为硬门禁，仅作为后续评分与背离提示。
    """
    cfg = _resolve_p3_cfg(cfg)
    if df is None or df.empty or len(df) < 2:
        return False, "历史K线不足"

    y = df.iloc[-1]
    now_px = _safe_float(rt.get("price"), 0.0)
    if now_px <= 0:
        return False, "缺少有效现价"

    # ===== 【V29 涨幅硬闸门】最优先执行：涨幅越界的票一律不进入 P3 =====
    # 用户硬约束：P3 是「当日启动初期」池，涨幅已超过 5% 说明追高风险大于赔率；
    # 盘中已翻绿超过阈值说明走势走坏。放在所有形态判断之前，避免为必被否决的票做无谓计算。
    _pre_close_gate = _safe_float(rt.get("pre_close"), _safe_float(y.get("close"), 0.0))
    if _pre_close_gate > 0:
        _pct_gate = (now_px - _pre_close_gate) / _pre_close_gate * 100.0
        _cap = float(getattr(cfg, "pct_hard_cap_pct", 5.0))
        _floor = float(getattr(cfg, "pct_hard_floor_pct", -1.5))
        # 涨幅由 (now-pre)/pre 反推，浮点运算会产生 ~1e-13 误差，
        # 导致「恰好 5.00%」被误判为 5.0000001% 而错杀。因此先四舍五入再比较。
        _pct_cmp = round(_pct_gate, 4)
        if _cap > 0 and _pct_cmp > round(_cap, 4):
            return False, f"涨幅越界: {_pct_gate:.2f}% > {_cap:.1f}%(盘中不追高)"
        if _floor < 0 and _pct_cmp < round(_floor, 4):
            return False, f"涨幅越界: {_pct_gate:.2f}% < {_floor:.1f}%(盘中已走坏)"

    mode = _resolve_risk_mode(rt, y, cfg)
    ma60 = _safe_float(y.get("ma60"), 0.0)
    ma20 = _safe_float(y.get("ma20"), 0.0)
    slope = _safe_float(y.get("ma20_slope_5"), 0.0)
    bias_live = ((now_px - ma20) / ma20 * 100.0) if ma20 > 0 else 0.0

    # ---- 【V26.88.1 P3 优化①】拥挤过热收紧阈值 ----
    crowding_ord_vr = float(getattr(cfg, "crowding_ordinary_vr", 2.5))
    crowding_str_vr = float(getattr(cfg, "crowding_strong_vr", 3.5))
    crowding_lead_vr = float(getattr(cfg, "crowding_leader_vr", 4.2))
    # 战略赛道容错：允许适度放宽 bias 限制
    strat_bias_loosen = float(getattr(cfg, "strategic_bias_loosen_pct", 2.5))
    strat_trend_loosen = bool(getattr(cfg, "strategic_trend_loosen", True))
    ind_name = str(rt.get("industry", rt.get("sector", ""))).strip()
    strat_inds = getattr(cfg, "strategic_industries", None)
    is_strategic = bool(strat_inds and ind_name in strat_inds)

    # 1) 趋势向下规避：普通股与强势股都不能逆势，龙头可保留更宽松的均线回踩窗口
    # 战略赛道允许 slope<=0 时通过（机构主导市场有底仓配置资金托底）
    if ma60 > 0 and now_px <= ma60 and mode != "leader":
        return False, "趋势规避: 现价 <= ma60"
    if slope <= 0 and mode != "leader":
        if not (is_strategic and strat_trend_loosen):
            return False, f"趋势规避: ma20_slope_5={slope:.4f} <= 0"
        # 战略赛道容错通过
    if slope <= 0 and mode == "leader" and not (is_strategic and strat_trend_loosen):
        return False, f"趋势规避: ma20_slope_5={slope:.4f} <= 0"

    # 2) 破位飞刀：普通股严格，强势股适度放宽，龙头允许回踩但不允许深度失守
    if ma20 > 0:
        if mode == "ordinary" and now_px < ma20:
            return False, "破位飞刀: 现价 < ma20"
        if mode == "strong" and now_px < ma20 * 0.985:
            return False, "强势股回踩失守: 现价 < ma20*0.985"
        if mode == "leader" and now_px < ma20 * 0.975:
            return False, "龙头回踩失守: 现价 < ma20*0.975"

    # 3) 盘子过小：circ_mv 万元
    cm = _safe_float(rt.get("circ_mv"), _safe_float(y.get("circ_mv"), 0.0))
    if cm < cfg.circ_mv_min_wan:
        return False, f"流通市值过小 circ_mv={cm:.0f}万 < {cfg.circ_mv_min_wan:.0f}万"

    # 4) 【V26.88 新增】过度乖离：使用波动率自适应阈值
    # - 高波动股票（涨停股、强势股）天然会有更高bias，固定阈值会误杀
    # - 低波动股票需要更严格的bias控制
    bias_limit = _calc_volatility_adjusted_bias_limit(df, mode, cfg)
    if is_strategic:
        bias_limit += strat_bias_loosen  # 战略赛道容错：允许适度更高乖离

    if bias_live > bias_limit:
        return False, f"过度乖离: bias_20={bias_live:.2f}% > {bias_limit:.2f}%({mode})"

    # 5) 拥挤过热收紧（2026量化横行，原阈值过宽）
    vr = _safe_float(rt.get("vol_ratio"), _safe_float(y.get("vol_ratio"), 0.0))
    if mode == "ordinary" and vr >= crowding_ord_vr and bias_live >= 6.0:
        return False, f"普通股拥挤过热(vr>{crowding_ord_vr})"
    if mode == "strong" and vr >= crowding_str_vr and bias_live >= 8.0:
        return False, f"强势股拥挤过热(vr>{crowding_str_vr})"
    if mode == "leader" and vr >= crowding_lead_vr and bias_live >= 10.0:
        return False, f"龙头极端拥挤过热(vr>{crowding_lead_vr})"
    if mode == "leader" and vr < cfg.leader_crowding_vr_floor and bias_live > 8.0:
        return False, "龙头高位无承接"

    # === V26.88.1 P3 全局否决新增（接盘侠规避） ===

    # 6) ST 股拦截：名称含 *ST/SST/ST+中文 → 直接否决（P3 盘中不玩 ST）
    _stock_name = str(
        rt.get("name", rt.get("stock_name", rt.get("名称", ""))) or ""
    ).upper().strip()
    if _stock_name.startswith("*ST") or _stock_name.startswith("SST"):
        return False, f"ST拦截: *ST/SST 股 {_stock_name[:8]} (盘中不碰ST)"
    if _stock_name.startswith("ST") and len(_stock_name) > 2:
        _next_char = _stock_name[2:3]
        if _next_char and not (_next_char.isalnum() and ord(_next_char) < 128):
            return False, f"ST拦截: ST 股 {_stock_name[:8]} (盘中不碰ST)"

    # 7) 一字板拦截：今日涨幅 ≥ 板块涨停阈值×0.95 且已交易量极少 → 追板风险极高
    # 【V26.88+ 9-30】朋友2建议：动态板块阈值（创业板/科创板19.5%、北交所29.5%、主板9.5%）
    now_px_veto = _safe_float(rt.get("price"), 0.0)
    pre_close_veto = _safe_float(rt.get("pre_close"), _safe_float(y.get("close"), 0.0))
    if pre_close_veto > 0:
        pct_today_veto = (now_px_veto - pre_close_veto) / pre_close_veto * 100.0
        try:
            from core.strategies.board_limit_utils import get_limit_up_pct_fast
            _dyn_limit_up = float(get_limit_up_pct_fast(rt.get("ts_code", ""), rt.get("name", "")))
        except Exception:
            _dyn_limit_up = 9.4
        if pct_today_veto >= _dyn_limit_up * 0.95:
            vol_veto = _safe_float(rt.get("volume"), 0.0)
            vol_ma5_veto = _safe_float(y.get("vol_ma5"), 0.0)
            if vol_ma5_veto > 0 and vol_veto > 0:
                vol_ratio_veto = vol_veto / (vol_ma5_veto * 100.0)
                if vol_ratio_veto < 0.3:
                    return False, (
                        f"一字板拦截: 涨停{pct_today_veto:.2f}%但竞价量稀少"
                        f"(量比{vol_ratio_veto:.2f}×ma5, T+1高开必砸)"
                    )

    # 8) T+1 涨停风险强化：昨日涨幅 ≥ 板块涨停阈值×0.95，今日 vr < 2.5 → 直接否决
    # 【V26.88.1 强化】原预警→升级为否决（vr<2.5 意味着量化在抢跑，次日必低开）
    if df is not None and len(df) >= 2:
        prev_veto = df.iloc[-2]
        prev_pct_veto = _safe_float(prev_veto.get("pct_chg"), 0.0)
        try:
            from core.strategies.board_limit_utils import get_limit_up_pct_fast
            _dyn_limit_up_v = float(get_limit_up_pct_fast(rt.get("ts_code", ""), rt.get("name", "")))
        except Exception:
            _dyn_limit_up_v = 9.4
        if prev_pct_veto >= _dyn_limit_up_v * 0.95:
            if vr < 2.5:
                return False, (
                    f"T+1涨停否决: 昨涨幅{prev_pct_veto:.1f}%但今日vr={vr:.2f}<2.5"
                    f"(量化抢跑，次日低开)"
                )

    # 9) 昨日跌停拦截：前日跌幅 ≤ -板块涨停阈值×0.95 → 今日继续恐慌概率高
    y_pct_veto = _safe_float(y.get("pct_chg"), 0.0)
    try:
        from core.strategies.board_limit_utils import get_limit_up_pct_fast
        _dyn_limit_down = float(get_limit_up_pct_fast(rt.get("ts_code", ""), rt.get("name", "")))
    except Exception:
        _dyn_limit_down = 9.5
    if y_pct_veto <= -_dyn_limit_down * 0.95:
        return False, f"跌停拦截: 昨日跌停({y_pct_veto:.2f}%, 板块阈值{_dyn_limit_down:.1f}%)，今日仍可能恐慌"

    # ---------- 【V26.88 P3 优化①/③/④】量化横行预警（通过 rt 副作用传递，调用方从 detail 消费） ----------
    _p3_populate_quant_warnings(rt, df, y, mode, bias_live, vr, cfg)

    return True, ""


def p3_legacy_hard_veto(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    cfg: Optional[P3IntradayScreenerConfig] = None,
) -> Tuple[bool, str]:
    """
    专项回测 legacy 模式：恢复近似旧版「CCI 动能下限、九转见顶」硬否决。

    仅在 is_backtest_legacy_mode() 为 True 时生效；实盘默认关闭，不改变物理胸甲剥离版行为。
    """
    try:
        from core.backtest_context import is_backtest_legacy_mode
    except ImportError:
        return True, ""
    if not is_backtest_legacy_mode():
        return True, ""
    if df is None or df.empty:
        return False, "历史K线不足"
    y = df.iloc[-1]
    cci = _safe_float(y.get("cci"), np.nan)
    if np.isfinite(cci) and cci < 100.0:
        return False, "legacy: CCI<100 动能不足"
    nt = _safe_float(y.get("nineturn_signal"), 0.0)
    if nt >= 9.0:
        return False, "legacy: 九转见顶(nineturn>=9)"
    return True, ""


def _strategy_right_side_ignite(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    prev: pd.Series,
    cfg: P3IntradayScreenerConfig,
    pct: float,
    now_px: float,
    proj_vol_hand: float,
) -> Tuple[bool, str]:
    """策略一：右侧起爆（保留为辅助确认，不再作为唯一强进攻入口）。"""
    if not (cfg.s1_pct_low <= pct <= cfg.s1_pct_high):
        return False, "涨幅不在 3%~6%"

    mh_now = _macd_hist_series(y)
    mh_prev = _macd_hist_series(prev)
    if not (mh_now > 0 and mh_prev <= 0):
        return False, "MACD 柱未呈现刚翻红"

    vma5 = _safe_float(y.get("vol_ma5"), 0.0)
    vma20 = _safe_float(y.get("vol_ma20"), 0.0)
    if vma5 <= 0 or vma20 <= 0:
        return False, "量能均线缺失"
    if not (proj_vol_hand > vma5 * cfg.s1_vol_ma5_mult):
        return False, "预估全日量未达 vol_ma5*1.5"
    if not (vma5 > vma20):
        return False, "vol_ma5 未大于 vol_ma20"

    wr = _field_rt_or_bar(rt, y, "winner_rate")
    if wr <= cfg.s1_winner_min:
        return False, "winner_rate 不足"

    boll_u = _safe_float(y.get("boll_upper"), 0.0)
    if boll_u > 0 and now_px > boll_u:
        return False, "防飞刀: 突破布林上轨"

    if not _recent_yang_yin_vol_balance(df, 5, cfg.breakout_yang_vol_ratio_min):
        return False, "量价结构: 近5日阳量未压过阴量"
    if not _price_on_vwap_ok(rt, now_px, cfg.breakout_vwap_eps):
        return False, "分时均价: 现价未站稳VWAP"

    return True, "OK"


def _strategy_ma20_pullback(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    cfg: P3IntradayScreenerConfig,
    pct: float,
    now_px: float,
    low_px: float,
    proj_vol_hand: float,
) -> Tuple[bool, str]:
    """策略二：均线回踩低吸（优先趋势内回踩，兼容突破确认）。"""
    if not (-1.0 <= pct <= cfg.s2_pct_high):
        return False, "涨幅不在回踩区间"

    ma20_pb = _safe_float(y.get("ma20"), 0.0)
    ma5 = _safe_float(y.get("ma5"), 0.0)
    if ma20_pb <= 0 or ma5 <= 0:
        return False, "ma20 或 ma5 缺失"

    touch_ma20 = low_px <= ma20_pb * cfg.s2_ma20_touch_ratio
    near_ma20 = abs(now_px - ma20_pb) / ma20_pb * 100.0 <= cfg.s2_pullback_close_to_ma20_pct if ma20_pb > 0 else False
    above_ma20 = now_px >= ma20_pb
    pullback_bias = (now_px - ma20_pb) / ma20_pb * 100.0 if ma20_pb > 0 else 0.0

    if not (touch_ma20 or near_ma20):
        return False, "未形成 ma20 回踩/贴线"
    if not above_ma20:
        return False, "现价未站回 ma20 上方"
    if ma5 < ma20_pb:
        return False, "ma5 未强于 ma20"

    vma5 = _safe_float(y.get("vol_ma5"), 0.0)
    if vma5 <= 0:
        return False, "vol_ma5 缺失"
    if not (proj_vol_hand <= vma5 * 1.05):
        return False, "预估全日量未体现缩量"

    if _macd_hist_series(y) < 0:
        return False, "macd_hist 未转强"
    wr = _field_rt_or_bar(rt, y, "winner_rate")
    if wr <= cfg.s2_winner_min:
        return False, "winner_rate 不足"

    turnover_f = effective_turnover_rate_f(rt, y, now_px if now_px > 0 else _safe_float(y.get("close"), 0.0))
    if turnover_f > cfg.s2_turnover_fly_max:
        return False, "防飞刀: 真实换手过高"
    pb = _safe_float(y.get("pb"), 0.0)
    if pb > cfg.s2_pb_fly_max:
        return False, "防飞刀: pb>10"

    if pullback_bias > cfg.s2_pullback_bonus_bias_max:
        return False, "回踩过浅"

    return True, "OK"


def _strategy_single_peak_break(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    cfg: P3IntradayScreenerConfig,
    pct: float,
    now_px: float,
) -> Tuple[bool, str]:
    """策略三：单峰跃迁（筹码密集区突破）。"""
    wr = _field_rt_or_bar(rt, y, "winner_rate")
    if wr <= cfg.s3_winner_min:
        return False, "winner_rate 不足"

    c95 = _field_rt_or_bar(rt, y, "cost_95th", default=-1.0)
    if c95 <= 0 or now_px <= c95:
        return False, "未突破 cost_95th"

    if not (cfg.s3_pct_low <= pct <= cfg.s3_pct_high):
        return False, "涨幅不在 2%~5%"

    vr = _safe_float(rt.get("vol_ratio"), _safe_float(y.get("vol_ratio"), 0.0))
    if vr < cfg.s3_vol_ratio_min:
        return False, "量比不足"

    h20 = _high_20_prev(df, y)   # [2026-10-15 修复] 突破基准改为不含当日的 20 日高点
    if h20 > 0 and now_px <= h20:
        return False, "防飞刀: 未突破前20日高点(high_20_prev,假突破)"

    rsi = _safe_float(y.get("rsi_14"), 0.0)
    if rsi > cfg.s3_rsi_fly:
        return False, "防飞刀: RSI14 超买"

    # 高位乖离 + 缩量：易为假突破/骗线，收紧过滤
    b20 = _safe_float(y.get("bias_20"), 0.0)
    if b20 > 16.0 and pct > 2.0 and vr < 1.8:
        return False, "防飞刀: 高乖离缩量上攻"

    if not _recent_yang_yin_vol_balance(df, 5, cfg.breakout_yang_vol_ratio_min):
        return False, "量价结构: 阳量未压过阴量"
    if not _price_on_vwap_ok(rt, now_px, cfg.breakout_vwap_eps):
        return False, "分时均价: 未站稳VWAP"

    return True, "OK"


def _strategy_giant_continuous(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    prev: pd.Series,
    cfg: P3IntradayScreenerConfig,
    pct: float,
    now_px: float,
) -> Tuple[bool, str]:
    """策略四：巨头连贯发力。"""
    cmw = circ_mv_wan_from(y, rt)
    if cmw <= cfg.s4_circ_mv_min_wan:
        return False, "流通市值未达 300 亿"

    nm_gc = _safe_float(rt.get("net_main_amount"), np.nan)
    nm_gc = nm_gc if not pd.isna(nm_gc) else _safe_float(y.get("net_main_amount"), 0.0)
    thr_yuan = dynamic_net_main_threshold_yuan(cmw, cfg.s4_net_main_ratio_of_float_mv)
    if nm_gc <= thr_yuan:
        return False, "主力净额不足(动态门槛)"

    inst_y = _safe_float(y.get("inst_net_buy"), 0.0)
    inst_thr = dynamic_inst_single_threshold_yuan(cmw, cfg.s4_inst_net_buy_ratio_of_float_mv)
    if inst_y <= inst_thr:
        return False, "昨日 inst_net_buy 未达动态门槛"

    ma5 = _safe_float(y.get("ma5"), 0.0)
    ma20 = _safe_float(y.get("ma20"), 0.0)
    ma60 = _safe_float(y.get("ma60"), 0.0)
    if not (ma5 > ma20 > ma60):
        return False, "均线未呈 ma5>ma20>ma60"

    if not (1.5 <= pct <= 4.5):
        return False, "涨幅不在 1.5%~4.5%"

    vr_gc = _safe_float(rt.get("vol_ratio"), _safe_float(y.get("vol_ratio"), 0.0))
    if vr_gc < cfg.giant_min_vol_ratio:
        return False, "量比不足(巨头连贯)"

    return True, "OK"


def _strategy_platform_reattack(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    cfg: P3IntradayScreenerConfig,
    pct: float,
    now_px: float,
) -> Tuple[bool, str]:
    """策略五：平台二次确认。"""
    h20 = _high_20_prev(df, y)   # [2026-10-15 修复] 突破基准改为不含当日的 20 日高点
    if h20 <= 0 or now_px <= h20:
        return False, "未站上前20日高点(high_20_prev)"

    slope = _safe_float(y.get("ma20_slope_5"), 0.0)
    if slope <= 1.0:
        return False, "ma20_slope_5 未 > 1.0"

    vr = _safe_float(rt.get("vol_ratio"), _safe_float(y.get("vol_ratio"), 0.0))
    if vr < cfg.s5_vol_ratio_min:
        return False, "量比不足"

    c50 = _field_rt_or_bar(rt, y, "cost_50th")
    if c50 <= 0 or now_px <= c50 * cfg.s5_cost50_mult:
        return False, "未显著高于 cost_50th"

    nm_pf = _safe_float(rt.get("net_main_amount"), _safe_float(y.get("net_main_amount"), 0.0))
    if nm_pf < 0:
        return False, "防飞刀: 主力净额为负"

    atrp = _safe_float(y.get("atr_pct"), 0.0)
    if atrp > cfg.s5_atr_fly_max:
        return False, "防飞刀: atr_pct 过高"

    if _macd_hist_series(y) <= 0:
        return False, "macd_hist 未为正(平台突破)"
    if not _recent_yang_yin_vol_balance(df, 5, cfg.breakout_yang_vol_ratio_min):
        return False, "量价结构: 阳量未压过阴量"
    if not _price_on_vwap_ok(rt, now_px, cfg.breakout_vwap_eps):
        return False, "分时均价: 未站稳VWAP"

    return True, "OK"


def _strategy_counter_trend_bid(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    cfg: P3IntradayScreenerConfig,
    pct: float,
    now_px: float,
    low_px: float,
) -> Tuple[bool, str]:
    """策略七：资金逆势托底。"""
    if not (cfg.s7_pct_low <= pct <= cfg.s7_pct_high):
        return False, "涨幅不在 1%~3%"

    cmw = circ_mv_wan_from(y, rt)
    nm = _safe_float(rt.get("net_main_amount"), _safe_float(y.get("net_main_amount"), 0.0))
    thr_m = dynamic_net_main_threshold_yuan(cmw, cfg.s7_net_main_ratio_of_float_mv)
    if nm <= thr_m:
        return False, "主力净额不足(动态门槛)"

    pre_close = _safe_float(rt.get("pre_close"), _safe_float(y.get("close"), 0.0))
    if pre_close <= 0:
        return False, "昨收缺失"
    if low_px < pre_close * cfg.s7_pre_close_low_mult:
        return False, "盘中出现过深回踩"

    vr = _safe_float(rt.get("vol_ratio"), _safe_float(y.get("vol_ratio"), 0.0))
    if not (cfg.s7_vol_ratio_low <= vr <= cfg.s7_vol_ratio_high):
        return False, "量比不在 1.2~2.5"

    turnover_f = effective_turnover_rate_f(rt, y, now_px if now_px > 0 else pre_close)
    if turnover_f > cfg.s7_turnover_fly:
        return False, "防飞刀: 真实换手过高"

    return True, "OK"


def _strategy_shrink_divergence_dip(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    cfg: P3IntradayScreenerConfig,
) -> Tuple[bool, str]:
    """
    # 【缩量分歧低吸】辅助 mask：仅当 rt['_market_contraction_score']≥0.7（与第1条自适应联动）时参与判定；
    # 不修改原七大策略公式，仅追加命中项与后续引擎中的低权重加分/仓位提示。
    """
    _ = cfg
    mcs = _safe_float(rt.get("_market_contraction_score", 0.0), 0.0)
    if mcs < 0.7:
        return False, "缩量分歧低吸未激活(mcs<0.7)"

    if df is None or len(df) < 30:
        return False, "历史不足(换手分位需窗口)"

    y = df.iloc[-1]
    now_px = _safe_float(rt.get("price"), 0.0)
    if now_px <= 0:
        now_px = _safe_float(y.get("close"), 0.0)
    if now_px <= 0:
        return False, "无效现价"

    ma5 = _safe_float(y.get("ma5"), 0.0)
    ma20 = _safe_float(y.get("ma20"), 0.0)
    if min(ma5, ma20) <= 0:
        return False, "均线缺失"

    # 【缩量分歧低吸】贴近攻击线与生命线（★右侧优化：收紧bias容忍）
    b5 = (now_px - ma5) / ma5 * 100.0
    b20 = (now_px - ma20) / ma20 * 100.0
    if not (cfg.s10_touch_bias_ma20_low <= b20 <= cfg.s10_touch_bias_ma20_high):
        return False, f"bias_ma20不在[{cfg.s10_touch_bias_ma20_low},{cfg.s10_touch_bias_ma20_high}]%: {b20:.2f}"
    if abs(b5) > cfg.s10_touch_bias_ma5_abs:
        return False, f"未贴近ma5(|bias|>{cfg.s10_touch_bias_ma5_abs}%)"

    hist = df.tail(60)
    s_tr = series_effective_turnover_f_daily(hist)
    s_tr = pd.to_numeric(s_tr, errors="coerce").replace(0.0, np.nan).dropna()
    if len(s_tr) < 20:
        return False, "有效换手序列过短"
    p20 = float(s_tr.quantile(0.20))
    p60 = float(s_tr.quantile(0.60))
    if p60 <= p20 or p20 <= 0:
        return False, "换手分位退化"

    # 【缩量分歧低吸】近8根日K（落在5~10日量级）真实换手持续处于历史偏低但非地量：>(20%分位)且<(60%分位)
    tail_n = min(10, max(5, len(df) - 1))
    recent_tr = series_effective_turnover_f_daily(df.tail(tail_n))
    recent_tr = pd.to_numeric(recent_tr, errors="coerce")
    ok_band = 0
    for v in recent_tr.values:
        fv = float(v) if pd.notna(v) else 0.0
        if fv <= 0:
            continue
        if p20 < fv < p60:
            ok_band += 1
    need = max(5, tail_n - 2)
    if ok_band < need:
        return False, f"近{tail_n}日换手未持续处于20~60%分位带({ok_band}/{tail_n})"

    wr = _field_rt_or_bar(rt, y, "winner_rate")
    wr_hist = pd.to_numeric(hist.get("winner_rate"), errors="coerce").dropna()
    if wr_hist.empty:
        return False, "winner_rate历史缺失"
    wr_med = float(wr_hist.median())
    if wr <= wr_med:
        return False, f"winner_rate未高于60日中位({wr:.1f}<={wr_med:.1f})"

    vr = _safe_float(rt.get("vol_ratio"), _safe_float(y.get("vol_ratio"), 0.0))
    if not (1.0 <= vr <= 1.8):
        return False, f"量比不在1.0~1.8: {vr:.2f}"

    # 【缩量分歧低吸】筹码未松动：与 P3 二次上攻 cyq 口径一致（cyq 有效且较低表示相对锁定；缺失不硬杀）
    cyq = _field_rt_or_bar(rt, y, "cyq_concentration", default=999.0)
    if cyq < 900.0 and cyq >= 28.0:
        return False, f"筹码偏松动(cyq={cyq:.1f}≥28)"

    return True, "OK"


def _strategy_volume_breakout_follow(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    prev: pd.Series,
    cfg: P3IntradayScreenerConfig,
    pct: float,
    now_px: float,
) -> Tuple[bool, str]:
    """策略八：昨日倍量阳线启动，今日沿 5/10 多头延续（A 股量价惯性）。"""
    if len(df) < 3:
        return False, "历史不足"

    vol_prev = _safe_float(prev.get("vol"), 0.0)
    vma5_prev = _safe_float(prev.get("vol_ma5"), 0.0)
    if vma5_prev <= 0 or vol_prev <= 0:
        return False, "缺少昨日成交量"
    if vol_prev < vma5_prev * cfg.s8_vol_vs_vma5_mult:
        return False, "昨日未达倍量(vs vol_ma5)"

    o_prev = _safe_float(prev.get("open"), 0.0)
    c_prev = _safe_float(prev.get("close"), 0.0)
    if o_prev <= 0 or c_prev < o_prev * cfg.s8_yang_body_min:
        return False, "昨日非阳线启动"

    ma5 = _safe_float(y.get("ma5"), 0.0)
    ma10 = _safe_float(y.get("ma10"), 0.0)
    ma20 = _safe_float(y.get("ma20"), 0.0)
    if min(ma5, ma10, ma20) <= 0:
        return False, "均线缺失"
    if not (ma5 > ma10 > ma20):
        return False, "非多头 ma5>ma10>ma20"

    slope = _safe_float(y.get("ma20_slope_5"), 0.0)
    if slope < cfg.s8_ma20_slope_min:
        return False, "ma20_slope_5 不足"

    if not (now_px > ma5 and now_px > ma10):
        return False, "现价未沿 5/10 上方"

    if not (cfg.s8_pct_low <= pct <= cfg.s8_pct_high):
        return False, "涨幅不在策略八区间"

    vr = _safe_float(rt.get("vol_ratio"), _safe_float(y.get("vol_ratio"), 0.0))
    if vr < cfg.s8_vr_min:
        return False, "量比不足"

    wr = _field_rt_or_bar(rt, y, "winner_rate")
    if wr < cfg.s8_winner_min:
        return False, "winner_rate 不足"

    return True, "OK"


def _strategy_quality_trend_base(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    cfg: P3IntradayScreenerConfig,
    pct: float,
    now_px: float,
) -> Tuple[bool, str]:
    """策略八：质量趋势底仓（V35 重新启用；历史编号为旧 P3-09）。

    定位：低估值（PE≤20）+ 低换手（<4%）的价值底仓型标的，在右侧温和上行中
    （涨幅 1.5~2.5%）确认筹码健康后入场。条件（全部满足才命中）：
      1. 当日涨幅落在 [s9_pct_low, s9_pct_high]（右侧上攻，不抄底、不追高）；
      2. pe_ttm ≤ s9_pe_max（低估值；缺失/亏损视为不合格）；
      3. 真实换手 turnover_rate_f < s9_turnover_f_max_lt（低换手，符合底仓特征）；
      4. 现价站上 ma60，且 ma60 未处下行趋势（今日 ≥ 5 日前 × s9_ma60_downtrend_rel）；
      5. 现价显著高于 cost_50th（筹码平均成本 × s9_cost50_mult，防接盘套牢区）；
      6. winner_rate ≥ s9_winner_min（获利盘健康，筹码未松动）。
    """
    if not bool(getattr(cfg, "s9_enabled", True)):
        return False, "s9_disabled"

    if not (cfg.s9_pct_low <= pct <= cfg.s9_pct_high):
        return False, f"涨幅({pct:.2f}%)不在[{cfg.s9_pct_low},{cfg.s9_pct_high}]%"

    pe = _safe_float(rt.get("pe_ttm"), _safe_float(y.get("pe_ttm"), 0.0))
    if pe <= 0:
        return False, "pe_ttm 缺失或为负"
    if pe > cfg.s9_pe_max:
        return False, f"pe_ttm({pe:.1f})> {cfg.s9_pe_max}"

    turnover_f = effective_turnover_rate_f(rt, y, now_px if now_px > 0 else _safe_float(y.get("close"), 0.0))
    if turnover_f <= 0:
        return False, "真实换手缺失"
    if turnover_f >= cfg.s9_turnover_f_max_lt:
        return False, f"turnover_f({turnover_f:.2f}%)≥{cfg.s9_turnover_f_max_lt}%"

    ma60 = _safe_float(y.get("ma60"), 0.0)
    if ma60 <= 0:
        return False, "ma60 缺失"
    if now_px <= ma60:
        return False, "现价未站上 ma60"
    # ma60 下行趋势判定：5 日前的 ma60 不可显著高于今日（rel=0.98 允许 2% 滞后）
    if df is not None and len(df) >= 6 and "ma60" in df.columns:
        prev60 = _safe_float(df["ma60"].iloc[-6], 0.0)
        if prev60 > 0 and ma60 < prev60 * cfg.s9_ma60_downtrend_rel:
            return False, f"ma60 仍在下行({ma60:.2f}<{prev60 * cfg.s9_ma60_downtrend_rel:.2f})"

    c50 = _field_rt_or_bar(rt, y, "cost_50th")
    if c50 <= 0 or now_px <= c50 * cfg.s9_cost50_mult:
        return False, "未显著高于 cost_50th"

    wr = _field_rt_or_bar(rt, y, "winner_rate")
    if wr < cfg.s9_winner_min:
        return False, f"winner_rate({wr:.1f})<{cfg.s9_winner_min}"

    return True, "OK"


def _strategy_rsi_reversal(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    cfg: P3IntradayScreenerConfig,
    pct: float,
    now_px: float,
    low_px: float,
) -> Tuple[bool, str]:
    """策略十八：RSI 超跌反转（观察档）。

    条件（全部满足才命中）：
      1. 14 日 SMA-RSI ≤ 超卖阈值（默认 30），基于「昨日及以前」日线计算；
      2. 今日涨幅落在 [s18_pct_low, s18_pct_high]（不追涨、不接深跌）；
      3. 量比 ≤ s18_vr_max（缩量企稳，放量下跌不接）；
      4. 现价已脱离日内最低点 ≥ s18_close_off_low_pct（左侧底确认）；
      5. 偏离 20 日线 ≥ s18_bias20_min（防飞刀）；
      6. 可选 winner_rate ≥ s18_winner_min（默认 0 不启用）。
    """
    if df is None or len(df) < max(20, cfg.s18_rsi_window + 2):
        return False, "历史不足(RSI需≥20根)"

    rsi = _rsi_sma(df["close"] if "close" in df.columns else pd.Series(dtype=float), cfg.s18_rsi_window)
    if not np.isfinite(rsi):
        return False, "RSI计算失败"
    if rsi > cfg.s18_rsi_oversold:
        return False, f"RSI({rsi:.1f})未进入超卖区(≤{cfg.s18_rsi_oversold})"

    if not (cfg.s18_pct_low <= pct <= cfg.s18_pct_high):
        return False, f"涨幅({pct:.2f}%)不在[{cfg.s18_pct_low},{cfg.s18_pct_high}]%"

    vr = _safe_float(rt.get("vol_ratio"), _safe_float(y.get("vol_ratio"), 0.0))
    if vr <= 0:
        return False, "量比缺失"
    if vr > cfg.s18_vr_max:
        return False, f"量比({vr:.2f})放量> {cfg.s18_vr_max}，不接第二段杀跌"

    if now_px <= 0 or low_px <= 0:
        return False, "现价/低点无效"
    off_low = (now_px - low_px) / low_px * 100.0
    if off_low < cfg.s18_close_off_low_pct:
        return False, f"未脱离日内低点({off_low:.2f}%<{cfg.s18_close_off_low_pct}%)"

    ma20 = _safe_float(y.get("ma20"), 0.0)
    if ma20 > 0:
        bias20 = (now_px - ma20) / ma20 * 100.0
        if bias20 < cfg.s18_bias20_min:
            return False, f"防飞刀: bias_20({bias20:.1f}%)<{cfg.s18_bias20_min}%"

    if cfg.s18_winner_min > 0:
        wr = _field_rt_or_bar(rt, y, "winner_rate", 0.0)
        if wr < cfg.s18_winner_min:
            return False, f"winner_rate({wr:.1f})<{cfg.s18_winner_min}"

    return True, f"OK(RSI={rsi:.1f}, 脱离低点{off_low:.1f}%)"


def _p3_hit_tier_cn(hit: str) -> str:
    """P3 命中分档（与 strat_p3_intraday._p3_strategy_tier 保持一致）。
    core=主战法 / aux=辅助 / obs=观察项。
    【V35】质量趋势底仓（重新启用）计入 core 主战法档。
    """
    s = str(hit or "")
    if any(k in s for k in ["右侧起爆", "均线回踩低吸", "巨头连贯发力", "倍量启动延续", "质量趋势底仓"]):
        return "core"
    if any(k in s for k in ["单峰跃迁", "平台/二次确认", "资金逆势托底", "均线粘合突破"]):
        return "aux"
    if "缩量分歧低吸" in s or "RSI超跌反转" in s:
        return "obs"
    return "aux"


def evaluate_p3_intraday_screener(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    cfg: Optional[P3IntradayScreenerConfig] = None,
) -> Dict[str, Any]:
    """
    单票单次评估：全局否决 + 11 个客观战法；仅使用 O(1) 尾部字段与 rt 快照。

    返回:
        veto_pass, veto_reason, strategies, strategy_checks,
        p3_core_screener_pass, detail
    """
    cfg = _resolve_p3_cfg(cfg)
    out: Dict[str, Any] = {
        "veto_pass": False,
        "veto_reason": "",
        "strategies": [],
        "strategy_checks": {},
        "p3_core_screener_pass": False,
        "detail": {},
    }

    ok, reason = p3_global_risk_veto(df, rt, cfg)
    out["veto_pass"] = ok
    out["veto_reason"] = reason
    if not ok:
        return out

    ok_l, reason_l = p3_legacy_hard_veto(df, rt, cfg)
    if not ok_l:
        out["veto_pass"] = False
        out["veto_reason"] = reason_l
        return out

    y = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else y
    pre_close = _safe_float(rt.get("pre_close"), _safe_float(y.get("pre_close"), _safe_float(y.get("close"), 0.0)))
    now_px = _safe_float(rt.get("price"), 0.0)
    low_px = _safe_float(rt.get("low"), now_px)
    high_px = _safe_float(rt.get("high"), now_px)

    sector_strength, mainline_reason, msr_extra = _p3_mainline_score(rt, y, cfg)
    pct = (now_px - pre_close) / pre_close * 100.0 if pre_close > 0 else 0.0
    vr = _safe_float(rt.get("vol_ratio"), 0.0)
    fund_score, fund_reason, fund_metrics = _fund_flow_signal(rt, y, now_px)
    # 【V26.88+9-23 新增】盈利质量 + 单季同比打分（fact_financial_reports）
    fin_score, fin_reason = _fin_quality_score(rt, cfg)
    if sector_strength <= -1.0 and vr >= 2.0 and pct >= 3.0:
        out["veto_pass"] = False
        out["veto_reason"] = "假突破过滤: 强拉但板块不配合"
        return out
    proj = projected_vol_hand_today(rt, y)

    # 【V35 编号重整】P3 注册表重排为 P3-01~P3-11 连续编号（旧号→新号对照见模块头 docstring）。
    #   原 V29 删除的 06 水上金叉 / 11 缩量加速 / 12·13·14 早盘三战法 / 16·17 B4 两战法的
    #   实现与配置已从代码层彻底删除；质量趋势底仓（旧09）按用户要求重新启用为 P3-08。
    strats: List[Tuple[str, Any]] = [
        ("P3-01·★右侧起爆", lambda: _strategy_right_side_ignite(df, rt, y, prev, cfg, pct, now_px, proj)),
        ("P3-02·★均线回踩低吸", lambda: _strategy_ma20_pullback(df, rt, y, cfg, pct, now_px, low_px, proj)),
        ("P3-03·★单峰跃迁", lambda: _strategy_single_peak_break(df, rt, y, cfg, pct, now_px)),
        ("P3-04·★巨头连贯发力", lambda: _strategy_giant_continuous(df, rt, y, prev, cfg, pct, now_px)),
        ("P3-05·★平台/二次确认", lambda: _strategy_platform_reattack(df, rt, y, cfg, pct, now_px)),
        ("P3-06·★资金逆势托底", lambda: _strategy_counter_trend_bid(df, rt, y, cfg, pct, now_px, low_px)),
        ("P3-07·★倍量启动延续", lambda: _strategy_volume_breakout_follow(df, rt, y, prev, cfg, pct, now_px)),
        ("P3-08·★质量趋势底仓", lambda: _strategy_quality_trend_base(df, rt, y, cfg, pct, now_px)),
        ("P3-09·★缩量分歧低吸", lambda: _strategy_shrink_divergence_dip(df, rt, cfg)),
        # 【V34+ 新增】源自 ai_quant_trade RSI 均值回归示例 → 观察档低吸，不单独放行金门禁
        ("P3-11·★RSI超跌反转", lambda: _strategy_rsi_reversal(df, rt, y, cfg, pct, now_px, low_px)),
    ]

    # === V27 B1 辅助子策略（B4 两战法已随 V29 精简彻底删除） ===
    try:
        from core.strategies.p3_aux_strategies import strategy_ma_convergence_breakout as _b1
        ok_b1, msg_b1 = _b1(df, cfg.__dict__ if cfg is not None else {}, rt)
    except Exception as ex:
        logger.debug("P3 辅助子策略异常: %s", ex)
        ok_b1 = False
        msg_b1 = f"异常:{ex}"

    hits: List[str] = []
    checks: Dict[str, str] = {}
    for name, fn in strats:
        try:
            passed, msg = fn()
        except Exception as ex:
            logger.debug("P3 策略 %s 异常: %s", name, ex)
            passed, msg = False, f"异常:{ex}"
        checks[name] = msg
        if passed:
            hits.append(name)

    # 【V35】P3-10 均线粘合突破（B1 辅助子策略，已改为用 rt 现价）
    checks["P3-10·★均线粘合突破"] = msg_b1
    if ok_b1:
        hits.append("P3-10·★均线粘合突破")

    out["strategies"] = hits
    out["strategy_checks"] = checks
    # 【V29 Bugfix】原逻辑 len(hits)>0 会让“观察项”（缩量分歧低吸）也绕过 golden gate，
    # 使弱信号直接进池。改为分档门禁：core 命中即通；无 core 时需 ≥2 个 aux 共振。
    tier_map = {"core": 0, "aux": 0, "obs": 0}
    for h in hits:
        tier_map[_p3_hit_tier_cn(h)] += 1
    gate_ok = tier_map["core"] > 0 or tier_map["aux"] >= 2
    out["p3_core_screener_pass"] = bool(gate_ok)
    out["p3_strategy_tier_counts"] = tier_map
    # 【V26.88 P3 优化①/③/④】量化横行预警透传
    quant_warnings = list(rt.get("_quant_warnings", []) or [])
    out["detail"] = {
        "pct_chg_live": round(pct, 3),
        "projected_vol_hand": round(proj, 2),
        "elapsed_mins": _intraday_elapsed_minutes(rt),
        "sector_strength": round(sector_strength, 3),
        "mainline_reason": mainline_reason,
        "fund_flow_score": round(fund_score, 3),
        "fund_flow_reason": fund_reason,
        "fund_flow_metrics": {k: round(v, 3) if isinstance(v, (int, float, np.floating)) else v for k, v in fund_metrics.items()},
        # 【V26.88 P3 优化②/⑥】板块超额强度因子 + 战略赛道
        "excess_strength": msr_extra.get("excess_strength", 0.0),
        "crowd_type": msr_extra.get("crowd_type", "normal"),
        "is_crowd_leader": msr_extra.get("is_crowd_leader", False),
        "is_crowd_follower": msr_extra.get("is_crowd_follower", False),
        "is_crowd_phantom": msr_extra.get("is_crowd_phantom", False),
        "is_strategic_industry": msr_extra.get("is_strategic_industry", False),
        "strategic_bonus": msr_extra.get("strategic_bonus", 0.0),
        # 【V26.88+9-23 新增】盈利质量 + 单季同比
        "fin_quality_score": round(fin_score, 2),
        "fin_quality_reason": fin_reason,
        # 量化横行预警
        "quant_warnings": quant_warnings,
        "is_stagnation": bool(rt.get("_is_stagnation", False)),
        "is_t1_risk": bool(rt.get("_is_t1_risk", False)),
        "is_quant_speed": bool(rt.get("_is_quant_speed", False)),
    }
    return out


def screen_p3_universe(
    rows: List[Tuple[str, pd.DataFrame, Dict[str, Any]]],
    cfg: Optional[P3IntradayScreenerConfig] = None,
) -> pd.DataFrame:
    """批量增量：仅遍历当日快照列表，不做全表计算。"""
    cfg = _resolve_p3_cfg(cfg)
    rec: List[Dict[str, Any]] = []
    for ts_code, df, rt in rows:
        r = evaluate_p3_intraday_screener(df, rt, cfg)
        rec.append(
            {
                "ts_code": ts_code,
                "veto_pass": r["veto_pass"],
                "veto_reason": r["veto_reason"],
                "strategies": "|".join(r["strategies"]),
                "p3_core_screener_pass": r["p3_core_screener_pass"],
            }
        )
    return pd.DataFrame(rec)
