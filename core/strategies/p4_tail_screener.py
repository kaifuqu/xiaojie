# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 — P4 尾盘选股池「物理胸甲」硬阈值筛选模块（建议运行窗口 14:30–15:00）
================================================================================
【V26.88 P4 优化要点（2026 慢牛格局）】
① 战略赛道容错：硬科技/军工等战略赛道在强势放量（量比≥2.0、涨幅≤5.5%）时，
   允许短期 bias 略微突破常规上限，slope<=0 时也宽容。
② 【已清理】北向资金 3 日评分与 hk_vol 门槛全部移除（hk_hold 数据源下线后全库 hk_vol 恒为 0，
   依赖它们的旧 P4-03「机构尾盘潜伏」已随退役战法从代码层彻底删除）。
③ 涨幅上限收紧：机构主导市场中尾盘涨幅>5.5%（非战略赛道）更可能是游资接力，
   收紧全局否决阈值。
④ winner_rate 机构地板：任一策略通过后，若 winner_rate < 80% 仍降权否决，
   过滤散户高波动标的。
【V26.88 资金口径】主力/特大单/机构净买门槛 = max(流通市值(元)×比例, 市值阶梯地板)，禁止单一绝对金额一刀切。
【V26.88 换手口径】100% 使用真实自由换手 turnover_rate_f；缺失时用 vol(手)×close/circ_mv(万) 反算，禁止 turnover_rate。

对齐 data_fetcher.ALL_55_COLS；全局一票否决 + 8 个客观战法（满足任一即打标签）。
【V35 战法编号重整 2026-11】原子战法重排为 P4-01~P4-08 连续编号，旧→新对照（供历史 signal_log 复盘）：
  旧01→P4-01 光头阳线抢筹   旧02→P4-02 筹码锁死创新高   旧04→P4-03 均线缩量低吸
  旧05→P4-04 强势洗盘承接   旧06→P4-05 动能突破共振     旧07→P4-06 底仓趋势延续
  旧12→P4-07 连板缩量整理   旧13→P4-08 双均线金叉确认
已从代码层彻底删除的旧编号：03 机构尾盘潜伏（北向下线后恒假）/ 08·09·10·11（函数存在但
从未进注册表的死实现，V26.88 清理时连同 cfg 字段与专属辅助函数一并删除）。
================================================================================
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from core.strategies.fund_mv_utils import (
    circ_mv_wan_from,
    dynamic_inst_single_threshold_yuan,
    dynamic_net_main_threshold_yuan,
    effective_turnover_rate_f,
)

logger = logging.getLogger(__name__)

try:
    import constants as _C
    _P1_MIN_CIRC_MV_WAN = float(getattr(_C, "P1_SELECT_MIN_CIRC_MV_WAN", 1_000_000))
except Exception:
    _P1_MIN_CIRC_MV_WAN = 1_000_000.0


@dataclass
class P4TailScreenerConfig:
    """
    P4 尾盘阈值脚手架。
    资金类：*_ratio_of_float_mv 表示占流通市值(元)的比例，与阶梯地板取 max。
    换手类：阈值针对 turnover_rate_f（真实换手，通常高于总股本换手）。
    """

    # ---------- 全局否决（p4_global_risk_veto）----------
    circ_mv_min_wan: float = _P1_MIN_CIRC_MV_WAN
    global_fund_reflow_pct_max: float = 0.8
    global_upper_shadow_ratio_max: float = 0.028
    # 长上影+「过度真实换手」诱多：真实换手常高于总换手，阈值略高于旧版
    global_upper_turnover_f_min: float = 14.0

    # ---------- 【V26.88 P4 优化】2026慢牛格局专项 ----------
    strategic_industries: list = None  # 战略赛道列表（与 pool_manager 同步）
    strategic_bias_loosen_pct: float = 2.0
    strategic_trend_loosen: bool = True
    # 【上线前修复 2026-10-18】北向资金相关配置已退役（hk_vol 全库恒为 0）。
    # 保留占位仅为兼容历史 config.yaml 的多余键，运行时不再被任何策略读取。
    # 【V26.88.1 收紧】5.5 → 4.5：尾盘涨幅过大多为游资接力，机构主升涨幅≤4.5%
    pct_high_institution_cap: float = 4.5
    winner_floor: float = 82.0
    fatal_penalty_threshold: float = 80.0
    tail_snapshot_max_age_min: int = 45

    # ---------- 【V34 新增】盈利质量阈值（原 _fin_quality_score_p4 全部硬编码，配置键是死键）----------
    # 该函数 P4/P5 共用；P5 的 P5PostmarketConfig 也有同名字段，两池可各自独立调。
    fin_score_qoq_yoy_bonus_threshold: float = 25.0
    fin_score_roe_strong: float = 12.0
    fin_score_gross_margin_strong: float = 25.0
    fin_score_peg_attractive: float = 0.6
    fin_score_qoq_yoy_penalty_threshold: float = -30.0

    # ---------- 【V33 P4 全局优化】尾盘执行时钟（见 core/strategies/tail_clock.py）----------
    # 14:30=870 / 14:35=875 / 14:45=885 / 14:55=895 / 15:00=900
    # 语义：14:30-14:35 观察期半仓 → 14:35-14:45 黄金窗主建仓 →
    #       14:45-14:55 尾窗减半且不追价 → 14:55 后冻结，禁止新开仓。
    tail_market_open_min: int = 570
    tail_morning_close_min: int = 690
    tail_afternoon_open_min: int = 780
    tail_observe_start_min: int = 870
    tail_golden_start_min: int = 875
    tail_late_start_min: int = 885
    tail_freeze_start_min: int = 895
    tail_market_close_min: int = 900

    # ---------- 尾盘惩罚分组封顶（防「千刀万剐」式悬崖否决）----------
    # 原实现每命中一个尾盘坏形态就 penalty += 15~30，且互相高度相关
    # （无量拉升 / 跟风 / 板块偏弱 / 全天弱于VWAP 常常同时成立），
    # 合计轻易越过 fatal_penalty_threshold=80 → 战法被整票清空，
    # 真正的问题是「误杀」而不是「风控」。
    # 新口径：按经济含义分 5 组，组内累加后封顶，再求和。
    tail_penalty_cap_trap: float = 35.0        # 诱多/派发形态组
    tail_penalty_cap_flow: float = 100.0       # 资金背离组（放量净流出仍保留一票否决力度）
    tail_penalty_cap_board: float = 20.0       # 板块联动组
    tail_penalty_cap_liquidity: float = 30.0   # 量能/流动性组
    tail_penalty_cap_stretch: float = 25.0     # 极端涨幅/位置组

    # ---------- 尾盘正向因子（把「健康尾盘」真正变成加分，而不是只记一个标签）----------
    # 原代码 _check_tail_30min_behavior 给出的 bonus 只写进了 risk_tags，从未进分数；
    # 等于「良性尾盘」这条最重要的 T+1 胜率来源被静默丢弃。
    tail_edge_bonus_max: float = 10.0
    tail_edge_close_pos_strong: float = 0.90   # 收盘位于日内振幅上沿比例（仅诊断）
    tail_edge_close_pos_mid: float = 0.75
    # 【V33 标定结论】「收盘贴日高」在 T+1 维度是负向特征（详见 tools/p4_tail_calibration.py
    # --proxy 全市场近 250 天实测），故该项加分权重默认 0；仅在有人用新样本推翻时才调回。
    tail_edge_close_pos_bonus: float = 0.0
    tail_edge_close_pos_flag_high: float = 0.90  # 超过该比例且涨幅>=3% → 视为尾盘拉高诱多
    tail_edge_vwap_hold_eps: float = 0.001
    # 【V33.1 审计修复】尾盘正向因子的「合并上限」。
    # 原实现把合并封顶写死成 min(20.0, ...)，而 tail_edge_bonus_max=10 只作用于
    # _tail_edge_score 内部那四项；再加上「尾盘30分良性行为」与「长下影承接」后
    # 实际上限是 20，配置名与实际不符。现在合并封顶独立成这一项。
    tail_edge_total_bonus_max: float = 20.0

    # ---------- 仓位阶梯（替代写死的 max_position_ratio=0.15）----------
    p4_base_position_ratio: float = 0.10       # 基准仓位（1 成）
    p4_max_position_ratio: float = 0.20        # 单票仓位上限（2 成）
    p4_min_position_ratio: float = 0.04        # 有信号但质量偏弱时的最小可执行仓位

    # ---------- 【V33.1 审计修复】仓位阶梯的分数刻度 ----------
    # 根因：burst_score 是「量比/资金/筹码/尾盘异动」四项加权分（0.25/0.30/0.20/0.25），
    # 与 pipeline 展示的综合分（raw_score × regime × sector × decay × safety）不是同一刻度。
    # 原实现把 88/78/68/60 四档与「<60 不建仓」直接套在 burst_score 上，
    # 而实测 signal_log 的真实 P4 信号 burst_score 只有 36–58 → 几乎恒定为 0 仓位，
    # 推送文案变成「分53 | 仓0% | 不建仓」（选了却不让买）。
    # 改为在 burst_score 自己的刻度上线性归一，端点可调，不再有 60 分悬崖。
    p4_score_scale_floor: float = 45.0        # 该分及以下 → 分数系数 0.30
    p4_score_scale_ceiling: float = 85.0      # 该分及以上 → 分数系数 1.00
    p4_min_score_for_entry: float = 50.0      # 低于该分直接不建仓（原写死 60）

    # ---------- 策略一：光头阳线抢筹 ----------
    s1_close_to_high_min: float = 0.99
    s1_pct_low: float = 3.0
    s1_pct_high: float = 6.0
    s1_vol_ratio_min: float = 1.5
    s1_winner_min: float = 85.0
    s1_ma20_slope_min: float = 0.5

    # ---------- 策略二：筹码锁死创新高（使用 turnover_rate_f 真换手区间）----------
    s2_winner_min: float = 90.0
    s2_turnover_f_low: float = 1.5
    s2_turnover_f_high: float = 10.0
    # ---------- 策略三：机构尾盘潜伏（比例 + 阶梯）----------
    s3_net_main_ratio_of_float_mv: float = 0.0004
    s3_inst_net_buy_ratio_of_float_mv: float = 0.00008
    s3_pct_low: float = 0.5
    s3_pct_high: float = 3.0

    # ---------- 策略四：均线缩量低吸 ----------
    s4_vol_shrink_ratio: float = 0.8
    s4_pct_low: float = -1.0
    s4_pct_high: float = 2.0
    s4_touch_ma_eps: float = 1.01
    s4_winner_fly_lt: float = 80.0

    # ---------- 策略七：强势洗盘承接 ----------
    s7_net_main_ratio_of_float_mv: float = 0.00025
    s7_ma5_touch_eps: float = 1.005
    s7_pct_low: float = 1.0
    s7_pct_high: float = 4.0
    s7_vol_ratio_min: float = 1.2
    s7_turnover_f_fly_gt: float = 12.0  # 【优化】从18%降至12%，过滤高波动出货嫌疑

    # ---------- 策略八：动能突破共振 ----------
    s8_atr_pct_fly_gt: float = 6.0
    s8_bias_fly_gt: float = 12.0
    s8_net_main_ratio_of_float_mv: float = 0.00022
    s8_min_vol_vs_vma5: float = 0.45

    # ---------- 策略十一（P4-06）：P1底仓趋势延续 ----------
    # 【2026-10-15 重写】旧名「底仓不破均线」。旧门禁（p1>=76 / vwap_hold>=0.78 /
    #   ma5_hold>=0.72 / ma20_hold>=0.65 / reclaim<=1 / pb<=1.4 / dd<=2.2 / shd<=2.0 /
    #   close_vwap_dev∈[0,2] / vr∈[1.0,2.6]）在真实 P4 候选域（P1初筛通过 ∩
    #   p4_global_risk_veto 通过 ∩ winner_rate>=82，1294 条 / 426 只 / 63 个评分日）
    #   实测：p1>=76 后仅剩 98 条，再叠加 vwap_hold>=0.78 后 0/1294 ⇒ 死策略。
    # 新语义：「P1 底仓票在尾盘仍处于温和上行趋势、偏离不大、量能与上影健康」。
    # 新门禁由 p407_final_calib / p407_final_verify / p407_final_refine 在真实候选域
    #   上三折（按评分日 3 等分）标定：T+5 超额三折全正、T+10 超额三折全正、
    #   T+5 胜率 +5.4pp、日均命中 5.25 条、331 条命中。
    s11_p1_score_min: float = 50.0
    s11_ma20_slope_min: float = 0.3
    s11_bias_ma20_max_pct: float = 5.0
    s11_vol_ratio_max: float = 2.5
    s11_upper_shadow_max_pct: float = 2.5

    # ---- 以下为旧「底仓不破均线」门禁，仅保留字段名以兼容历史 config / 备份脚本，
    #      P4-06 新实现不再读取（继续读取只会把策略重新变成死条件）。----
    s11_vwap_hold_ratio_min: float = 0.78      # 弃用：不可达
    s11_ma5_hold_ratio_min: float = 0.72       # 弃用：不可达
    s11_ma20_hold_ratio_min: float = 0.65      # 弃用：不可达
    s11_reclaim_max: int = 1                   # 弃用
    s11_pullback_depth_max_pct: float = 1.4    # 弃用
    s11_intraday_dd_max_pct: float = 2.2       # 弃用
    s11_vol_ratio_low: float = 1.0             # 弃用
    s11_vol_ratio_high: float = 2.6            # 弃用
    s11_sector_strength_min: float = 1.0       # 弃用
    s11_mainline_score_min: float = 0.0        # 弃用
    s11_tail_vwap_dev_min: float = 0.0         # 弃用
    s11_tail_vwap_dev_max: float = 2.0         # 弃用
    s11_hold_bars_min_ratio: float = 0.62      # 弃用

    # ---------- 【V26.88 新增】策略十二：连板后缩量整理（捕捉连板后回调确认的二次买点）----------
    s12_limit_up_lookback_days: int = 3  # 回顾近N日内是否有涨停
    s12_consolidation_days_min: int = 1  # 整理天数最少
    s12_consolidation_days_max: int = 3  # 整理天数最多（超过3天视为弱势）
    s12_pct_low: float = 0.5  # 整理期涨幅下限
    s12_pct_high: float = 3.5  # 整理期涨幅上限
    s12_vol_ratio_max: float = 1.1  # 缩量：量比不超过1.1
    s12_bias_ma5_max: float = 5.0  # bias_ma5不能太高
    s12_vr_confirm: float = 1.3  # 重新放量确认的量比门槛

    # ---------- 【V34+ 新增】策略十三：双均线金叉确认（源自 ai_quant_trade DualMovingAverage 5/20）----------
    # 定位：近 2 个交易日内 MA5 上穿 MA20（金叉），今日尾盘价格站在两线之上确认，多头趋势成立。
    # 与 P4-01/P4-05 的区别：那两条按当日涨幅/资金动能选股，本策略按「均线结构转折」选股，
    # 允许涨幅温和（1%~4.5%）——尾盘确权金叉、次日沿均线走趋势。
    s13_cross_window_days: int = 2   # 金叉发生在近 N 个交易日内（1=昨日K线内，2=最多回溯2日）
    s13_pct_low: float = 1.0         # 金叉确认日涨幅下限（太弱说明金叉无效）
    s13_pct_high: float = 4.5        # 与 pct_high_institution_cap 对齐（尾盘不追高）
    s13_vr_min: float = 1.1          # 量比确认（缩量金叉不追）
    s13_winner_min: float = 78.0     # winner_rate 地板（与 P5-06 口径一致）
    s13_require_macd_positive: bool = True  # MACD 柱为正（与均线多头共振，防诱多金叉）

    def __post_init__(self):
        """【V26.88+ 9-30 第9项落地】P4 放量阈值从 constants 统一读取。

        关键对齐项：
          - s1_vol_ratio_min（光头阳线抢筹量比下限）→ constants.P4_VOL_RATIO_MIN
        """
        try:
            from core.strategies.volume_threshold_utils import get_p4_vol_ratio_min
            self.s1_vol_ratio_min = float(get_p4_vol_ratio_min())
        except Exception:
            # 兜底：保留 dataclass 默认值
            pass


def _resolve_p4_cfg(cfg: Optional[P4TailScreenerConfig]) -> P4TailScreenerConfig:
    if cfg is not None:
        return cfg
    from core.config_manager import get_p4_tail_screener_config

    return get_p4_tail_screener_config()


DEFAULT_P4_CONFIG = P4TailScreenerConfig()


def _fin_quality_score_p4(rt: Dict[str, Any], cfg: "P4TailScreenerConfig") -> Tuple[float, str]:
    """【V26.88+9-24 新增】P4 尾盘盈利质量打分（精简版）。

    数据源：fact_financial_reports（已通过 _inject_financial_quality_into_rt 注入到 rt）。
    加分项（每项 +0.5，最多 +2.0）：
      - 单季净利同比 ≥ 25%        → +0.5
      - ROE ≥ 12%                 → +0.5
      - 毛利率 ≥ 25%              → +0.5
      - PEG ≤ 0.6                 → +0.5
    减分项（最多 -2.5）：
      - 单季净利同比 ≤ -30%        → -1.5（塌方）
      - 单季营收同比 ≤ -15%        → -0.5
      - ROE < 0                  → -0.5
    """
    score = 0.0
    tags: List[str] = []

    def _th(name: str, default: float) -> float:
        """【V34 P1-4】阈值改为读 cfg：原实现全部硬编码，P4/P5 配置里的 fin_score_* 是死键。
        缺属性时回退默认值（保持 P3 等其它传入方行为不变）。"""
        try:
            v = getattr(cfg, name, default)
            return float(v) if v is not None else float(default)
        except Exception:
            return float(default)

    _thr_qoq = _th("fin_score_qoq_yoy_bonus_threshold", 25.0)
    _thr_roe = _th("fin_score_roe_strong", 12.0)
    _thr_gross = _th("fin_score_gross_margin_strong", 25.0)
    _thr_peg = _th("fin_score_peg_attractive", 0.6)
    _thr_qoq_pen = _th("fin_score_qoq_yoy_penalty_threshold", -30.0)

    qoq_yoy = _safe_float(rt.get("qoq_profit_yoy"))
    if np.isfinite(qoq_yoy) and qoq_yoy >= _thr_qoq:
        score += 0.5
        tags.append(f"单季同比+{qoq_yoy:.0f}%")

    roe = _safe_float(rt.get("roe_pct"))
    if np.isfinite(roe) and roe >= _thr_roe:
        score += 0.5
        tags.append(f"ROE {roe:.1f}%")

    gross = _safe_float(rt.get("gross_margin"))
    if np.isfinite(gross) and gross >= _thr_gross:
        score += 0.5
        tags.append(f"毛利率{gross:.0f}%")

    pe = _safe_float(rt.get("pe_ttm"))
    if np.isfinite(pe) and np.isfinite(qoq_yoy) and qoq_yoy > 0:
        peg = pe / max(qoq_yoy, 0.1)
        if peg <= _thr_peg:
            score += 0.5
            tags.append(f"PEG {peg:.2f}")

    if np.isfinite(qoq_yoy) and qoq_yoy <= _thr_qoq_pen:
        score -= 1.5
        tags.append(f"单季同比{qoq_yoy:.0f}%(塌方)")
    qoq_rev = _safe_float(rt.get("qoq_revenue_yoy"))
    if np.isfinite(qoq_rev) and qoq_rev <= -15.0:
        score -= 0.5
        tags.append(f"单季营收{qoq_rev:.0f}%")
    if np.isfinite(roe) and roe < 0:
        score -= 0.5
        tags.append("ROE负")

    if not tags:
        return 0.0, "盈利质量(中性)"
    s = "+".join(tags) if score >= 0 else ",".join(tags)
    return float(np.clip(score, -2.5, 2.0)), f"盈利质量:{s}"


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


def _macd_hist_row(row: pd.Series) -> float:
    """
    MACD 柱（1× 统一口径）。

    【V30.10 统一 MACD 口径 2026-10-21】原实现有个隐蔽 bug：
        v = _safe_float(row.get("macd_hist"), 0.0)
        if v == 0.0 and "macd_bar" in row.index:
            v = _safe_float(row.get("macd_bar"), 0.0)
    —— 当 macd_hist 【合法地等于 0.0】时，会回退到 macd_bar，
    而 macd_bar 是【2× 口径】(indicator_calc: (diff-dea)*2)，
    于是同一个策略在不同数据源上拿到的柱体量级差一倍，
    与 macd_bar_kill 这类【绝对值阈值】比较时宽严整整差一倍。

    现统一走 core.strategies.macd_unified.macd_hist_normalized：
      macd_hist(1×) → macd_bar/2(2×折算) → (macd - macd_signal) 现算 → 缺失返回 0.0
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
          因此恒有 high_20 >= high >= close ⇒ `close_live > high_20` 永不成立。
    实测：daily_data 全量 218499 行中 close>high_20 命中 0 次；
          换成不含当日的 20 日高点后命中 12578 次（5.76%）。
    影响：P4-02 筹码锁死创新高、P4-05 动能突破共振 两条战法恒不命中。
    先例：P5 已于 p5_postmarket_screener.py:349 用 high_20_prev 修过同一 BUG，P4 未同步。
    改法：优先用 high_20_prev 列（若上游已产出），否则用 high.shift(1).rolling(20) 现算。
          不改任何阈值 / 权重 / 过滤规则，只修正「突破基准」这一事实性错误。
    """
    for col in ("high_20_prev",):
        if col in df.columns:
            v = _safe_float(y.get(col), 0.0)
            if v > 0:
                return v
    if "high" not in df.columns:
        return 0.0
    hi = pd.to_numeric(df["high"], errors="coerce").dropna()
    # 若末行已是实时追加行，则排除末行；否则取前 20 根
    if len(hi) >= 21:
        prev = hi.iloc[-21:-1] if len(hi) >= 21 else hi.iloc[:-1]
    else:
        prev = hi.iloc[:-1] if len(hi) >= 1 else hi
    if prev.empty:
        return _safe_float(y.get("high_20"), 0.0)
    return float(prev.tail(20).max())


def _field_rt_or_y(rt: Dict[str, Any], y: pd.Series, key: str, default: float = 0.0) -> float:
    raw = rt.get(key)
    if raw is not None and not (isinstance(raw, float) and pd.isna(raw)):
        return _safe_float(raw, default)
    return _safe_float(y.get(key), default)


def _today_vol_hand(rt: Dict[str, Any], y: pd.Series) -> float:
    """日内成交量（手）；仅用于量能/换手反算，不是换手率。"""
    v = _safe_float(rt.get("volume"), 0.0)
    if v <= 0:
        return float("nan")
    return float(v / 100.0)


def _trf(rt: Dict[str, Any], y: pd.Series, close_live: float) -> float:
    """统一真实换手(%)：只 turnover_rate_f 或反算。"""
    return effective_turnover_rate_f(rt, y, close_live)


def p4_global_risk_veto(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    cfg: Optional[P4TailScreenerConfig] = None,
) -> Tuple[bool, str]:
    cfg = _resolve_p4_cfg(cfg)
    if df is None or df.empty or len(df) < 2:
        return False, "历史K线不足"

    y = df.iloc[-1]
    y_prev = df.iloc[-2]

    close_live = _safe_float(rt.get("price"), 0.0)
    if close_live <= 0:
        close_live = _safe_float(rt.get("close"), 0.0)
    if close_live <= 0:
        return False, "缺少尾盘有效收盘价(现价)"

    ma20 = _safe_float(y.get("ma20"), 0.0)
    if ma20 > 0 and close_live < ma20:
        return False, "短期破位: 收盘 < ma20"

    ma60 = _safe_float(y.get("ma60"), 0.0)
    if ma60 > 0 and close_live < ma60:
        return False, "中期破位: 收盘 < ma60(非右侧趋势)"

    pct = _pct_chg_today(rt, y, close_live)
    vol_hand = _today_vol_hand(rt, y)
    vma5 = _safe_float(y.get("vol_ma5"), 0.0)
    # 尾盘无量偷袭：若累计成交量远低于 5 日均量，多为情绪拉尾而非全日承接
    if not np.isnan(vol_hand) and vol_hand > 0 and vma5 > 0 and vol_hand < vma5 * 0.38:
        return False, "尾盘偷袭: 全日量不足5日均量38%"

    sig = _safe_float(y.get("macd_signal"), 0.0)
    m = _safe_float(y.get("macd"), 0.0)
    m_prev = _safe_float(y_prev.get("macd"), 0.0)
    if sig < 0 and m < m_prev:
        return False, "动能死叉预警: macd_signal<0 且 macd 向下发散"

    nm_rt = _safe_float(rt.get("net_main_amount"), float("nan"))
    if not pd.isna(nm_rt):
        if nm_rt < 0 and pct <= cfg.global_fund_reflow_pct_max:
            return False, f"资金暗流: 主力为负且尾盘涨幅未超 cfg.global_fund_reflow_pct_max({cfg.global_fund_reflow_pct_max}%)"

    high_d = _safe_float(rt.get("high"), close_live)
    turnover_f = _trf(rt, y, close_live)
    if high_d > 0 and close_live > 0:
        upper_ratio = (high_d - close_live) / close_live
        if upper_ratio > cfg.global_upper_shadow_ratio_max and turnover_f > cfg.global_upper_turnover_f_min:
            return False, "诱多长上影: 上影/收盘与真实换手超全局阈值"

    cm = _field_rt_or_y(rt, y, "circ_mv")
    if cm < cfg.circ_mv_min_wan:
        return False, f"规模过小 circ_mv={cm:.0f}万 < {cfg.circ_mv_min_wan:.0f}万"

    # ===== 【V26.88 P4 优化①】战略赛道容错 =====
    # 在机构主导市场中，硬科技等战略赛道偶有尾盘急速拉升突破短期 bias 上限，
    # 只要当日成交量配合（量比≥2.0）、涨幅属于健康主升（≤5.5%），不触发 bias 否决。
    ind = str(rt.get("industry", "") or y.get("industry", ""))
    strat_inds = cfg.strategic_industries or []
    is_strategic = ind in strat_inds
    _vr_v = _field_rt_or_y(rt, y, "vol_ratio")
    _cap_exempt = (
        is_strategic
        and _vr_v >= 2.0
        and 0 < pct <= cfg.pct_high_institution_cap
    )

    # ===== 【V26.88 P4 优化②】涨幅上限收紧（机构主导：>5.5% 多为游资接力）=====
    if pct > cfg.pct_high_institution_cap and not is_strategic:
        return False, f"涨幅过速: pct={pct:.2f}% > {cfg.pct_high_institution_cap}%（非战略赛道）"

    # ===== 【V26.88 P4 优化③】战略赛道 slope 容错 =====
    slope = _safe_float(y.get("ma20_slope_5"), 0.0)
    if is_strategic and cfg.strategic_trend_loosen and slope <= 0 and _cap_exempt:
        pass  # 战略赛道强势放量时宽容 slope<=0
    elif slope <= 0:
        return False, "趋势破败: ma20_slope_5 <= 0 且非战略强势赛道"

    # === V26.88.1 P4 全局否决新增（接盘侠规避） ===

    # 6) ST 股拦截：名称含 *ST/SST/ST+中文 → 直接否决（P4 尾盘不碰 ST）
    _stock_name = str(
        rt.get("name", rt.get("stock_name", rt.get("名称", ""))) or ""
    ).upper().strip()
    if _stock_name.startswith("*ST") or _stock_name.startswith("SST"):
        return False, f"ST拦截: *ST/SST 股 {_stock_name[:8]} (尾盘不碰ST)"
    if _stock_name.startswith("ST") and len(_stock_name) > 2:
        _next_char = _stock_name[2:3]
        if _next_char and not (_next_char.isalnum() and ord(_next_char) < 128):
            return False, f"ST拦截: ST 股 {_stock_name[:8]} (尾盘不碰ST)"

    # 7) 一字板拦截：收盘涨幅 ≥ 板块涨停阈值×0.95 + 尾盘无量 → 尾盘追板 T+1 高开必砸
    # 【V26.88+ 9-30】朋友2建议：动态板块阈值（创业板/科创板19.5%、北交所29.5%、主板9.5%）
    try:
        from core.strategies.board_limit_utils import get_limit_up_pct_fast
        _dyn_limit_up_p4 = float(get_limit_up_pct_fast(rt.get("ts_code", ""), rt.get("name", "")))
    except Exception:
        _dyn_limit_up_p4 = 9.4
    if pct >= _dyn_limit_up_p4 * 0.95:
        tail_vol_ratio_veto = _safe_float(rt.get("tail_vol_ratio"), 0.0)
        if tail_vol_ratio_veto > 0 and tail_vol_ratio_veto < 0.3:
            return False, (
                f"一字板拦截: 收盘涨{pct:.2f}%但尾盘量比{tail_vol_ratio_veto:.2f}<0.3"
                f"(无量封板，尾盘追板风险，板块阈值{_dyn_limit_up_p4:.1f}%)"
            )
        # 若无 tail_vol_ratio 数据，用全天量作为兜底（仅当与全天均量比 < 0.3）
        vol_veto = _safe_float(rt.get("volume"), 0.0)
        vol_ma5_veto = _safe_float(y.get("vol_ma5"), 0.0)
        if tail_vol_ratio_veto <= 0 and vol_ma5_veto > 0 and vol_veto > 0:
            vol_ratio_veto = vol_veto / (vol_ma5_veto * 100.0)
            if vol_ratio_veto < 0.3:
                return False, (
                    f"一字板拦截: 收盘涨{pct:.2f}%但全天量比{vol_ratio_veto:.2f}×ma5(<0.3)"
                    f"(无量封板，尾盘追板风险)"
                )

    # 8) T+1 涨停次日风险硬否决（尾盘 14:30 后才有意义）
    # 昨日涨幅 ≥ 板块涨停阈值×0.95 且今日尾盘涨幅 ≥ 2% 且 vr < 2.5 → 抱团股次日低开陷阱
    prev_pct_veto = _safe_float(y_prev.get("pct_chg"), 0.0)
    if prev_pct_veto >= _dyn_limit_up_p4 * 0.95:
        vr_veto = _safe_float(rt.get("vol_ratio"), _safe_float(y.get("vol_ratio"), 0.0))
        if vr_veto < 2.5 and pct >= 2.0:
            return False, (
                f"T+1涨停否决: 昨涨{prev_pct_veto:.1f}%+今收涨{pct:.2f}%但vr={vr_veto:.2f}<2.5"
                f"(抱团股次日低开陷阱，板块阈值{_dyn_limit_up_p4:.1f}%)"
            )

    # 9) 昨日跌停拦截：前日跌幅 ≤ -板块涨停阈值×0.95 → 今日仍可能恐慌
    # 【V26.88+ 9-30】动态板块阈值（创业板/科创板19.5%、北交所29.5%、主板9.5%）
    if prev_pct_veto <= -_dyn_limit_up_p4 * 0.95:
        return False, f"跌停拦截: 昨日跌停({prev_pct_veto:.2f}%, 板块阈值{_dyn_limit_up_p4:.1f}%)，今日尾盘仍可能恐慌"

    # 10) 【V26.88+9-22 优化15】HiThink 炸板池硬否决（A 股实战核心：尾盘买炸板股 = 90% 次日低开）
    _ht_burst = rt.get("_hithink_limit_burst") or {}
    if _ht_burst:
        _open_times = int(_safe_float(_ht_burst.get("open_times"), 0.0) or 0)
        # 任意一次炸板即否决（尾盘买炸板股几乎必亏）；3次以上再加标签
        return False, (
            f"炸板拦截: HiThink 炸板池命中(open_times={_open_times}, 尾盘买入=次日高风险)"
        )

    # 11) 【V26.88+9-22 优化23】假突破识别（主力诱多：A 股 30-40% 假突破尾盘拉升）
    # 关键信号：1)开盘 1 小时内冲高 ≥2%；2)但当前已回落到开盘价下方；
    #           3)且量比放大；4)尾盘拉红——典型「拉高出货形态」。
    _open_px = _safe_float(rt.get("open"), 0.0)
    _high_px = _safe_float(rt.get("high"), 0.0)
    _now_px = _safe_float(rt.get("price"), _safe_float(rt.get("close"), 0.0))
    _vol_ratio = _safe_float(rt.get("vol_ratio"), 0.0)
    _intraday_amp = _safe_float(rt.get("intraday_amplitude_pct"), 0.0)
    # 模式 A：现价 < 开盘价 且 振幅 > 3% 且 量比 ≥ 1.5
    _is_fake_breakout_a = (
        _open_px > 0 and _now_px > 0 and _now_px < _open_px * 0.99
        and _vol_ratio >= 1.5
        and _high_px > _open_px * 1.02  # 早盘冲到 ≥ +2%
    )
    # 模式 B：日内振幅 > 4%（高位剧烈震荡）+ 收盘接近 VWAP 下方 → 主力派发嫌疑
    _is_fake_breakout_b = (
        _intraday_amp > 4.0
        and _now_px > 0
        and _vol_ratio >= 2.0
        and pct > 0  # 收红但形态恶劣
    )
    if _is_fake_breakout_a or _is_fake_breakout_b:
        _pattern = "拉高砸盘回落" if _is_fake_breakout_a else "高位剧烈震荡派发"
        return False, (
            f"假突破拦截[{_pattern}]: 高位冲高{_high_px:.2f}→回落{_now_px:.2f}"
            f"(振幅{_intraday_amp:.1f}%, vr={_vol_ratio:.1f}, 主力诱多嫌疑)"
        )

    return True, ""


def _pct_chg_today(rt: Dict[str, Any], y: pd.Series, close_live: float) -> float:
    pre = _safe_float(rt.get("pre_close"), _safe_float(y.get("close"), 0.0))
    if pre <= 0:
        return 0.0
    return (close_live - pre) / pre * 100.0


def _strategy_bald_bull(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    cfg: P4TailScreenerConfig,
    close_live: float,
    pct: float,
) -> Tuple[bool, str]:
    high_d = _safe_float(rt.get("high"), close_live)
    if high_d <= 0:
        return False, "最高价缺失"
    if close_live < high_d * cfg.s1_close_to_high_min:
        return False, "未收在近高点(低于 cfg.s1_close_to_high_min*high)"

    vr = _field_rt_or_y(rt, y, "vol_ratio")
    if not (cfg.s1_pct_low <= pct <= cfg.s1_pct_high):
        return False, "涨幅不在 cfg 设定区间"
    if vr < cfg.s1_vol_ratio_min:
        return False, "量比低于 cfg.s1_vol_ratio_min"

    wr = _field_rt_or_y(rt, y, "winner_rate")
    if wr <= cfg.s1_winner_min:
        return False, "winner_rate 低于 cfg.s1_winner_min"

    slope = _safe_float(y.get("ma20_slope_5"), 0.0)
    if slope <= cfg.s1_ma20_slope_min:
        return False, "防飞刀: ma20_slope_5 未高于 cfg.s1_ma20_slope_min"

    return True, "OK"


def _strategy_chip_lock_high(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    close_live: float,
    vol_hand: float,
    cfg: P4TailScreenerConfig,
) -> Tuple[bool, str]:
    wr = _field_rt_or_y(rt, y, "winner_rate")
    if wr <= cfg.s2_winner_min:
        return False, "winner_rate 不足"

    c95 = _field_rt_or_y(rt, y, "cost_95th")
    h20 = _high_20_prev(df, y)   # [2026-10-15 修复] 突破基准改为不含当日的 20 日高点
    if c95 <= 0 or close_live <= c95:
        return False, "未突破 cost_95th"
    if h20 <= 0 or close_live <= h20:
        return False, "未突破前20日高点(high_20_prev)"

    vma5 = _safe_float(y.get("vol_ma5"), 0.0)
    if np.isnan(vol_hand) or vma5 <= 0:
        return False, "成交量或 vol_ma5 缺失"
    if vol_hand > vma5:
        return False, "未满足缩量(vol<=vol_ma5)"

    turnover_f = _trf(rt, y, close_live)
    if not (cfg.s2_turnover_f_low <= turnover_f <= cfg.s2_turnover_f_high):
        return False, "真实换手不在 cfg.s2_turnover_f 区间"

    return True, "OK"




def _strategy_ma_shrink_dip(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    close_live: float,
    low_d: float,
    pct: float,
    vol_hand: float,
    cfg: P4TailScreenerConfig,
) -> Tuple[bool, str]:
    ma5 = _safe_float(y.get("ma5"), 0.0)
    ma20 = _safe_float(y.get("ma20"), 0.0)
    ma60 = _safe_float(y.get("ma60"), 0.0)
    if not (ma5 > ma20 > ma60):
        return False, "非多头 ma5>ma20>ma60"

    if not (cfg.s4_pct_low <= pct <= cfg.s4_pct_high):
        return False, "涨幅不在策略四 cfg 区间"

    touch_m20 = ma20 > 0 and low_d <= ma20 * cfg.s4_touch_ma_eps
    touch_m5 = ma5 > 0 and low_d <= ma5 * cfg.s4_touch_ma_eps
    if not (touch_m20 or touch_m5):
        return False, "盘中未触及 ma20 或 ma5 回踩带"
    if not (close_live > ma20):
        return False, "尾盘未站稳 ma20 之上"

    vma5 = _safe_float(y.get("vol_ma5"), 0.0)
    if np.isnan(vol_hand) or vma5 <= 0:
        return False, "量数据缺失"
    if vol_hand >= vma5 * cfg.s4_vol_shrink_ratio:
        return False, "未极致缩量(vol < vol_ma5*cfg)"

    if _macd_hist_row(y) <= 0:
        return False, "macd_hist 未为正"

    wr = _field_rt_or_y(rt, y, "winner_rate")
    if wr < cfg.s4_winner_fly_lt:
        return False, "防飞刀: winner_rate 低于 cfg.s4_winner_fly_lt"

    return True, "OK"


def _strategy_wash_rebound(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    close_live: float,
    low_d: float,
    pct: float,
    cfg: P4TailScreenerConfig,
) -> Tuple[bool, str]:
    ma5 = _safe_float(y.get("ma5"), 0.0)
    ma20 = _safe_float(y.get("ma20"), 0.0)
    if not (ma5 > ma20):
        return False, "非多头 ma5>ma20"

    if ma5 <= 0:
        return False, "ma5 缺失"
    if not (low_d <= ma5 * cfg.s7_ma5_touch_eps):
        return False, "盘中未触及 ma5*cfg.s7_ma5_touch_eps 附近"
    if not (close_live > ma5):
        return False, "尾盘未收在 ma5 之上"

    cmw = circ_mv_wan_from(y, rt)
    nm = _field_rt_or_y(rt, y, "net_main_amount")
    thr_nm = dynamic_net_main_threshold_yuan(cmw, cfg.s7_net_main_ratio_of_float_mv)
    if nm <= thr_nm:
        return False, "主力净额未达市值动态门槛"

    if not (cfg.s7_pct_low <= pct <= cfg.s7_pct_high):
        return False, "涨幅不在策略七 cfg 区间"

    vr = _field_rt_or_y(rt, y, "vol_ratio")
    if vr < cfg.s7_vol_ratio_min:
        return False, "量比低于 cfg.s7_vol_ratio_min"

    turnover_f = _trf(rt, y, close_live)
    if turnover_f > cfg.s7_turnover_f_fly_gt:
        return False, "防飞刀: 真实换手过高"

    sig = _safe_float(y.get("macd_signal"), 0.0)
    sig_p = _safe_float(df.iloc[-2].get("macd_signal"), 0.0) if len(df) >= 2 else sig
    m = _safe_float(y.get("macd"), 0.0)
    if sig < sig_p and m < sig:
        return False, "防飞刀: macd_signal 拐头且弱于快线(死叉前兆)"

    return True, "OK"


def _strategy_momentum_break(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    close_live: float,
    pct: float,
    cfg: P4TailScreenerConfig,
) -> Tuple[bool, str]:
    ma5 = _safe_float(y.get("ma5"), 0.0)
    ma20 = _safe_float(y.get("ma20"), 0.0)
    ma60 = _safe_float(y.get("ma60"), 0.0)
    if not (ma5 > ma20 > ma60):
        return False, "非绝对多头 ma5>ma20>ma60"

    h20 = _high_20_prev(df, y)   # [2026-10-15 修复] 突破基准改为不含当日的 20 日高点
    if h20 <= 0 or close_live <= h20:
        return False, "收盘未站上前20日高点(high_20_prev)"

    cmw = circ_mv_wan_from(y, rt)
    nm = _field_rt_or_y(rt, y, "net_main_amount")
    thr_nm = dynamic_net_main_threshold_yuan(cmw, cfg.s8_net_main_ratio_of_float_mv)
    if nm <= thr_nm:
        return False, "主力净额未达动态门槛(动能突破)"

    vol_hand = _today_vol_hand(rt, y)
    vma5 = _safe_float(y.get("vol_ma5"), 0.0)
    if not np.isnan(vol_hand) and vma5 > 0 and vol_hand < vma5 * cfg.s8_min_vol_vs_vma5:
        return False, "全日量能不足: 累计量未达5日均量45%"

    c95 = _field_rt_or_y(rt, y, "cost_95th")
    wr = _field_rt_or_y(rt, y, "winner_rate")
    if c95 > 0 and close_live < c95 * 0.995 and wr < 78.0:
        return False, "筹码重压: 未有效突破成本上沿且获利盘不足"

    atrp = _safe_float(y.get("atr_pct"), 0.0)
    if atrp > cfg.s8_atr_pct_fly_gt:
        return False, "防飞刀: atr_pct 高于 cfg.s8_atr_pct_fly_gt"

    ma20v = _safe_float(y.get("ma20"), 0.0)
    if ma20v > 0:
        bias = (close_live - ma20v) / ma20v * 100.0
        if bias > cfg.s8_bias_fly_gt:
            return False, "防飞刀: bias 高于 cfg.s8_bias_fly_gt"

    return True, "OK"


def _p1_daily_vwap_series(df: pd.DataFrame) -> pd.Series:
    """
    【2026-10-15 新增】由 daily_data 的 amount / vol 推导逐日成交量加权均价 VWAP。

    口径依据（已用真实数据校验，见 _pyenv/logs/audit_vwap_derive.py）：
        TuShare daily: vol = 手(×100股), amount = 千元
        => vwap = amount * 1000 / (vol * 100) = amount * 10 / vol
    校验结论：推导值 100% 落在当日 [low, high] 区间内（40 只 × 130 日零越界）。

    用途：P1 底仓类统计（P4-06 的旧口径「底仓不破均线」）需要逐日
          「收盘是否站在当日 VWAP 上方」的持有率；P4-06 现实现「底仓趋势延续」
          已不读取该口径，仅保留给盘面标签与历史脚本使用。
          daily_data 原本没有 vwap 列，导致该指标恒为 0。
    兼容：
      - 若上游已有 vwap 列（P5 走 rt 注入）则直接复用；
      - vol/amount 缺失或为 0 时该行返回 NaN，由调用方 dropna 跳过，不做任何猜测。
    """
    idx = df.index
    if "vwap" in df.columns:
        return pd.to_numeric(df["vwap"], errors="coerce")
    if "amount" not in df.columns or "vol" not in df.columns:
        return pd.Series(np.nan, index=idx, dtype=float)
    amount = pd.to_numeric(df["amount"], errors="coerce")
    vol = pd.to_numeric(df["vol"], errors="coerce")
    # amount(千元)*1000 / (vol(手)*100)  ==  amount*10/vol
    out = amount * 10.0 / vol.replace(0.0, np.nan)
    # 成交额与成交量不可能把均价推到当日最高/最低之外；越界视为脏数据 -> NaN
    if "low" in df.columns and "high" in df.columns:
        low_s = pd.to_numeric(df["low"], errors="coerce")
        high_s = pd.to_numeric(df["high"], errors="coerce")
        bad = (out < low_s * 0.999) | (out > high_s * 1.001)
        out = out.mask(bad)
    return out.replace([np.inf, -np.inf], np.nan)


def _p1_history_window(df: pd.DataFrame, rt: Dict[str, Any]) -> pd.DataFrame:
    """
    【2026-10-15 新增】剥掉「当日未收盘的实时追加行」，只留已收盘历史。

    背景：core.indicator_calc.merge_daily_with_realtime 在盘中扫描时会把当日
          一行 OHLCV 追加/覆盖到 df 末行（已用 audit_p4_realrow.py 证明：
          60 行 -> 61 行，末行 trade_date = 今天）。P4 是尾盘策略，这个末行
          尚未收盘，不能参与「历史持有率」类统计，否则盘中瞬时波动会污染口径。

    判定：末行 trade_date == 北京时间当天 且 df 原本不来自当日已收盘落库
          （用 rt 的 pre_close 与末行 pre_close 交叉校验）-> 视为实时行，剥离。
    兜底：无法判定时保守保留（宁可多算一天，也不要把真实收盘日丢掉）。
    """
    if df is None or df.empty:
        return df
    n = len(df)
    if n < 2:
        return df
    last_td = None
    if "trade_date" in df.columns:
        td = pd.to_datetime(df["trade_date"], errors="coerce").iloc[-1]
        if not pd.isna(td):
            last_td = td.date()
    if last_td is None:
        return df
    try:
        from datetime import datetime, timedelta, timezone as _tz
        today_bj = datetime.now(_tz(timedelta(hours=8))).date()
    except Exception:
        return df
    if last_td != today_bj:
        # 末行就是最近一个已收盘交易日 -> 全部都是历史
        return df
    rt_pc = _safe_float(rt.get("pre_close"), 0.0)
    row_pc = _safe_float(df.iloc[-1].get("pre_close"), 0.0)
    if rt_pc > 0 and row_pc > 0 and abs(rt_pc - row_pc) > 1e-6:
        # df 末行的 pre_close 与 rt 不一致 -> 末行是昨天，df 里根本没有当日行
        return df
    return df.iloc[:-1]


def _p1_bottom_hold_stats(df: pd.DataFrame, rt: Dict[str, Any]) -> Dict[str, Any]:
    """统计 P1 底仓票的盘中运行特征（P4-06 的旧口径「底仓不破均线」）。"""
    out = {
        "vwap_hold_ratio": 0.0,
        "ma5_hold_ratio": 0.0,
        "ma20_hold_ratio": 0.0,
        "reclaim_count": 0,
        "pullback_depth_pct": 0.0,
        "intraday_dd_pct": 0.0,
        "upper_shadow_pct": 0.0,
        "close_vwap_dev_pct": 0.0,
        "hold_bars": 0,
    }
    if df is None or df.empty:
        return out
    last = df.iloc[-1]
    price = _safe_float(rt.get("price"), _safe_float(last.get("close"), 0.0))
    if price <= 0:
        return out
    vwap_s = _p1_daily_vwap_series(df)
    vwap = _safe_float(rt.get("vwap"), 0.0)
    if vwap <= 0 and len(vwap_s) > 0:
        tail_vwap = vwap_s.iloc[-1]
        vwap = _safe_float(tail_vwap, 0.0) if pd.notna(tail_vwap) else 0.0
    ma5 = _safe_float(last.get("ma5"), 0.0)
    ma20 = _safe_float(last.get("ma20"), 0.0)
    high = _safe_float(rt.get("high"), price)
    low = _safe_float(rt.get("low"), price)
    pre_close = _safe_float(rt.get("pre_close"), _safe_float(last.get("pre_close"), 0.0))
    if pre_close > 0:
        out["pullback_depth_pct"] = max(0.0, (high - low) / pre_close * 100.0)
        out["intraday_dd_pct"] = max(0.0, (high - price) / pre_close * 100.0)
        out["upper_shadow_pct"] = max(0.0, (high - max(price, _safe_float(rt.get("open"), price))) / pre_close * 100.0)
        out["close_vwap_dev_pct"] = ((price - vwap) / vwap * 100.0) if vwap > 0 else 0.0
    # 【2026-10-15 审计修复 P0】P4-06 的三个事实性错误（均不涉及阈值语义）
    #   错误A：取数源缺失 → vwap 恒为 0
    #     daily_data 无 vwap 列，pipeline 的 rt 也不含 vwap 键，
    #     而原代码在 `if vwap > 0` 分支内才统计，
    #     ⇒ vwap_hold_ratio 恒 0 ⇒ 「VWAP 上方运行不足」恒成立
    #     ⇒ P4-06 自上线起第一道门就死（实测 600 只 0 命中）。
    #     改法：新增 _p1_daily_vwap_series() 由 amount/vol 推导取当日标量。
    #     实测：40 只 × 5165 行推导值全部落在当日 [low, high] 区间，0 个越界。
    #
    #   错误B：分母混入当日未收盘行
    #     merge_daily_with_realtime（core/scan/pipeline.py:566-582）会把当日盘中行追加/覆写到 df 末行，
    #     P4 是尾盘策略，末行尚未收盘，不该计入「历史持有率」。
    #     改法：新增 _p1_history_window() 剥离末行。
    #     实测：真实构造 60 → 61 行，剥离后恢复为 60 行。
    #
    #   错误C：reclaim_count 比的是「当日 VWAP」而非「逐日 VWAP」
    #     reclaim 语义 = 从 VWAP 下方回收到 VWAP 上方的次数，必须同一个水平。
    #     修复前用了逐日 VWAP。本次统一为当日 VWAP 标量。
    hist = _p1_history_window(df, rt)
    if "close" in hist.columns and len(hist) > 0 and vwap > 0:
        # 【口径纠正】hold_ratio 必须与 ma5_hold_ratio / ma20_hold_ratio 同口径：
        #   「历史收盘与当日 VWAP 水平比较」。原目的 s11_vwap_hold_ratio_min=0.78
        #   与 s11_ma5_hold_ratio_min=0.72 是同一个标定口径，若改成「逐日 VWAP」
        #   则 p50 降至 0.48、max 仅 0.63 → 0.78 永远不可达，策略成为死条件。
        #   因此保留「标量 VWAP」语义，仅用 amount/vol 补全缺失的 VWAP 取值。
        close_h = pd.to_numeric(hist["close"], errors="coerce").dropna()
        if len(close_h) > 0:
            out["vwap_hold_ratio"] = float((close_h > vwap).mean())
            out["hold_bars"] = int((close_h > vwap).sum())
            if len(close_h) >= 2:
                prev_c = close_h.shift(1)
                out["reclaim_count"] = int(((prev_c < vwap) & (close_h >= vwap)).sum())
    # ma5 / ma20 持有率：保留原口径（与末行均线值比较），只把分母换成已收盘历史。
    #   说明：改为「逐行均线」属于口径变更而非缺陷修复 —— 现有阈值
    #   s11_ma5_hold_ratio_min / s11_ma20_hold_ratio_min 是按原口径标定的，
    #   改口径会使 P4-06 命中率塌到 0，属于策略语义变更，故不动。
    if "close" in hist.columns and len(hist) > 0:
        close_h = pd.to_numeric(hist["close"], errors="coerce")
        if ma5 > 0:
            out["ma5_hold_ratio"] = float((close_h > ma5).mean())
        if ma20 > 0:
            out["ma20_hold_ratio"] = float((close_h > ma20).mean())
    return out


def _strategy_p1_trend_continuation(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    close_live: float,
    pct: float,
    cfg: P4TailScreenerConfig,
) -> Tuple[bool, str]:
    """
    【2026-10-15 重写】策略十一 P4-06「底仓趋势延续」（旧名「底仓不破均线」，旧编号 P4-07）。

    重写根因：旧门禁在真实 P4 候选域上恒不成立（死策略）——
      p1_score>=76 后 98/1294 条，再叠加 vwap_hold_ratio>=0.78 后 0/1294。
      旧口径把「历史收盘是否一直站在某一条静态水平（当日 VWAP / 末行 ma5 / 末行 ma20）
      之上」当成趋势，既是水平比较又对分母与末行均线有强依赖，在真实候选域不可达。

    新语义：P1 底仓票在尾盘仍处于**温和上行趋势**且**偏离不大**：
      1) P1 多维平滑分够底仓水位；
      2) 20 日均线斜率为正且不弱（趋势真向上，而非横盘）；
      3) 收盘对 ma20 的偏离被限制在很窄的带内（不追高，仍在成本附近）；
      4) 量比不失控（排除放量出货/爆量冲高）；
      5) 上影不长（排除尾盘冲高回落）。

    标定：p407_final_calib / p407_final_verify / p407_final_refine，
      宇宙 = P1 初筛通过 ∩ p4_global_risk_veto 通过 ∩ winner_rate>=winner_floor，
      1294 条 / 426 只 / 63 个评分日；三折（按评分日 3 等分）T+5 与 T+10 超额全正，
      T+5 胜率相对基准 +5.4pp，日均命中 5.25 条。

    注：本函数不再读取任何 s11_* 旧死门字段（vwap_hold/ma5_hold/ma20_hold/reclaim/
        pullback/intraday_dd/close_vwap_dev/vr 下限），这些字段在 cfg 里保留仅为兼容。
    """
    p1_score = _safe_float(rt.get("p1_score"), _safe_float(y.get("p1_score"), 0.0))
    if p1_score < cfg.s11_p1_score_min:
        return False, "P1分低于底仓门槛"

    ma20 = _safe_float(y.get("ma20"), 0.0)
    if ma20 <= 0:
        return False, "均线缺失"

    slope = _safe_float(y.get("ma20_slope_5"), 0.0)
    if slope < cfg.s11_ma20_slope_min:
        return False, "ma20_slope_5 未达趋势延续下限"

    bias_20 = (close_live / ma20 - 1.0) * 100.0
    if bias_20 > cfg.s11_bias_ma20_max_pct:
        return False, "收盘偏离ma20过大(非温和延续)"

    vr = _field_rt_or_y(rt, y, "vol_ratio")
    if vr > cfg.s11_vol_ratio_max:
        return False, "量比过高(放量出货嫌疑)"

    stats = _p1_bottom_hold_stats(df, rt)
    if stats["upper_shadow_pct"] > cfg.s11_upper_shadow_max_pct:
        return False, "上影过长"

    return True, "OK"


# 【兼容别名】历史脚本 / 报告按旧函数名调用该战法（现编号 P4-06），实现已重写为「底仓趋势延续」。
_strategy_p1_bottom_hold_ma = _strategy_p1_trend_continuation


def _has_limit_up_in_window(df: pd.DataFrame, lookback_days: int) -> Tuple[bool, int]:
    """
    检测近N日内是否有涨停（涨幅>=9.8%），返回 (是否有涨停, 距今天数)。
    0=今天涨停, 1=昨天涨停, -1=无
    """
    if df is None or len(df) < 2:
        return False, -1

    n = min(lookback_days, len(df) - 1)
    for i in range(n):
        row = df.iloc[-(i + 1)]
        close = _safe_float(row.get("close"), 0.0)
        prev_close = _safe_float(df.iloc[-(i + 1) - 1].get("close"), 0.0) if i + 2 <= len(df) else close
        if prev_close > 0:
            pct = (close - prev_close) / prev_close * 100.0
            if pct >= 9.8:
                return True, i
    return False, -1


def _count_consecutive_limit_up(df: pd.DataFrame, max_days: int = 5) -> int:
    """
    计算连续涨停天数（从最近一天往前数）。
    """
    if df is None or len(df) < 2:
        return 0

    consecutive = 0
    n = min(max_days, len(df) - 1)
    for i in range(n):
        row = df.iloc[-(i + 1)]
        prev_row = df.iloc[-(i + 2)] if i + 2 <= len(df) else None
        if prev_row is None:
            break
        close = _safe_float(row.get("close"), 0.0)
        prev_close = _safe_float(prev_row.get("close"), 0.0)
        if prev_close > 0:
            pct = (close - prev_close) / prev_close * 100.0
            if pct >= 9.8:
                consecutive += 1
            else:
                break
        else:
            break
    return consecutive


def _strategy_limit_up_consolidation(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    close_live: float,
    pct: float,
    cfg: P4TailScreenerConfig,
) -> Tuple[bool, str]:
    """
    【V26.88 新增】策略十二：连板后缩量整理。
    捕捉连板后回调确认的二次买点——连板股高位整理缩量后重新放量确认。

    条件：
    1. 近3日内有涨停（limit_up）
    2. 今日涨幅 0.5%~3.5%（整理期）
    3. 量比 < 1.1（缩量整理）
    4. 收盘站稳MA5
    5. bias_ma5 < 5%（不能偏离太多）
    6. 整理天数1-3天
    """
    # 1) 检查近N日内是否有涨停
    has_limit, days_ago = _has_limit_up_in_window(df, cfg.s12_limit_up_lookback_days)
    if not has_limit:
        return False, f"近{cfg.s12_limit_up_lookback_days}日未见涨停"
    if days_ago > cfg.s12_consolidation_days_max:
        return False, f"距涨停已{days_ago}天，超过整理期{cfg.s12_consolidation_days_max}天"

    # 2) 今日涨幅在整理区间
    if not (cfg.s12_pct_low <= pct <= cfg.s12_pct_high):
        return False, f"整理期涨幅({pct:.2f}%)不在{cfg.s12_pct_low}%-{cfg.s12_pct_high}%区间"

    # 3) 量比缩量
    vr = _field_rt_or_y(rt, y, "vol_ratio")
    if vr >= cfg.s12_vol_ratio_max:
        return False, f"量比({vr:.2f})未缩量，需<{cfg.s12_vol_ratio_max}"

    # 4) 收盘站稳MA5
    ma5 = _safe_float(y.get("ma5"), 0.0)
    if ma5 <= 0:
        return False, "ma5缺失"
    if close_live < ma5:
        return False, "未站稳ma5上方"

    # 5) bias_ma5 不能太高
    bias_ma5 = (close_live - ma5) / ma5 * 100.0
    if bias_ma5 > cfg.s12_bias_ma5_max:
        return False, f"bias_ma5({bias_ma5:.2f}%)偏高"

    # 6) MACD 红柱
    mh = _macd_hist_row(y)
    if mh <= 0:
        return False, "macd_hist未为正"

    # 7) 胜率要求
    wr = _field_rt_or_y(rt, y, "winner_rate")
    if wr < 78.0:
        return False, f"winner_rate({wr:.1f}%)不足78%"

    # 8) 均线多头排列（连板股要求5/10多头）
    ma10 = _safe_float(y.get("ma10"), 0.0)
    if ma10 <= 0:
        return False, "ma10缺失"
    if not (ma5 > ma10):
        return False, "非ma5>ma10多头"

    return True, f"OK(涨停{days_ago}天后整理第{ days_ago + 1 }天)"


def _strategy_ma_cross_confirmed(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    y: pd.Series,
    close_live: float,
    pct: float,
    cfg: P4TailScreenerConfig,
) -> Tuple[bool, str]:
    """策略十三：双均线金叉确认（MA5 上穿 MA20，尾盘确权）。

    条件：
      1. 近 s13_cross_window_days 个日线内出现 MA5 上穿 MA20（用 y / y_prev 的均线值判定）；
      2. 当前价（尾盘现价）站上 MA5 且 MA5 > MA20（金叉结构当日仍成立）；
      3. 今日涨幅 [s13_pct_low, s13_pct_high]；
      4. 量比 ≥ s13_vr_min；
      5. winner_rate ≥ s13_winner_min（winner_rate 缺失按 0 处理时放行，避免误杀）；
      6. MACD 柱为正（可配置关闭）——均线金叉 + 动能柱翻红才算有效金叉。
    """
    if df is None or len(df) < 3:
        return False, "历史K线不足"

    ma5 = _safe_float(y.get("ma5"), 0.0)
    ma20 = _safe_float(y.get("ma20"), 0.0)
    if ma5 <= 0 or ma20 <= 0:
        return False, "均线缺失"
    if not (ma5 > ma20):
        return False, "MA5未在MA20上方(非多头)"

    # 金叉检测：从 y 往前逐个比较 (mi> mj & 前日 mi<=mj)
    cross_days_ago = None
    win = max(1, int(cfg.s13_cross_window_days))
    for k in range(win):
        i_cur = -(1 + k)      # 当日/昨日的均线行
        i_prev = -(2 + k)
        if abs(i_prev) > len(df):
            break
        cur_row = df.iloc[i_cur]
        prev_row = df.iloc[i_prev]
        c5 = _safe_float(cur_row.get("ma5"), 0.0)
        c20 = _safe_float(cur_row.get("ma20"), 0.0)
        p5 = _safe_float(prev_row.get("ma5"), 0.0)
        p20 = _safe_float(prev_row.get("ma20"), 0.0)
        if min(c5, c20, p5, p20) <= 0:
            continue
        if c5 > c20 and p5 <= p20:
            cross_days_ago = k + 1
            break
    if cross_days_ago is None:
        return False, f"近{win}日无MA5上穿MA20金叉"

    if close_live <= 0 or close_live < ma5:
        return False, "现价未站稳MA5"

    if not (cfg.s13_pct_low <= pct <= cfg.s13_pct_high):
        return False, f"涨幅({pct:.2f}%)不在[{cfg.s13_pct_low},{cfg.s13_pct_high}]%"

    vr = _field_rt_or_y(rt, y, "vol_ratio")
    if vr < cfg.s13_vr_min:
        return False, f"量比({vr:.2f})<{cfg.s13_vr_min}"

    wr = _field_rt_or_y(rt, y, "winner_rate")
    if wr > 0 and wr < cfg.s13_winner_min:
        return False, f"winner_rate({wr:.1f}%)<{cfg.s13_winner_min}"

    if cfg.s13_require_macd_positive:
        mh = _macd_hist_row(y)
        if not (mh > 0):
            return False, "MACD柱未翻红(金叉缺动能确认)"

    return True, f"OK(金叉于{cross_days_ago}日前, 站稳MA5)"


def evaluate_p4_tail_screener(
    df: pd.DataFrame,
    rt: Dict[str, Any],
    cfg: Optional[P4TailScreenerConfig] = None,
) -> Dict[str, Any]:
    cfg = _resolve_p4_cfg(cfg)
    out: Dict[str, Any] = {
        "veto_pass": False,
        "veto_reason": "",
        "strategies": [],
        "strategy_checks": {},
        "p4_core_screener_pass": False,
        "detail": {},
    }

    ok, reason = p4_global_risk_veto(df, rt, cfg)
    out["veto_pass"] = ok
    out["veto_reason"] = reason
    if not ok:
        return out

    y = df.iloc[-1]
    close_live = _safe_float(rt.get("price"), 0.0)
    if close_live <= 0:
        close_live = _safe_float(rt.get("close"), 0.0)
    low_d = _safe_float(rt.get("low"), close_live)
    pct = _pct_chg_today(rt, y, close_live)
    vol_hand = _today_vol_hand(rt, y)

    # 【V35 编号重整】P4 注册表重排为 P4-01~P4-08 连续编号（旧号→新号对照见模块头 docstring）。
    #   退役战法（旧03 机构尾盘潜伏等）的实现、配置与注释已从代码层全部删除。
    strats = [
        ("P4-01·★光头阳线抢筹", lambda: _strategy_bald_bull(df, rt, y, cfg, close_live, pct)),
        ("P4-02·★筹码锁死创新高", lambda: _strategy_chip_lock_high(df, rt, y, close_live, vol_hand, cfg)),
        ("P4-03·★均线缩量低吸", lambda: _strategy_ma_shrink_dip(df, rt, y, close_live, low_d, pct, vol_hand, cfg)),
        ("P4-04·★强势洗盘承接", lambda: _strategy_wash_rebound(df, rt, y, close_live, low_d, pct, cfg)),
        ("P4-05·★动能突破共振", lambda: _strategy_momentum_break(df, rt, y, close_live, pct, cfg)),
        ("P4-06·★底仓趋势延续", lambda: _strategy_p1_trend_continuation(df, rt, y, close_live, pct, cfg)),
        ("P4-07·★连板缩量整理", lambda: _strategy_limit_up_consolidation(df, rt, y, close_live, pct, cfg)),
        # 【V34+ 新增】源自 ai_quant_trade DualMovingAverage(5/20) → 尾盘确权金叉
        ("P4-08·★双均线金叉确认", lambda: _strategy_ma_cross_confirmed(df, rt, y, close_live, pct, cfg)),
    ]

    hits: List[str] = []
    checks: Dict[str, str] = {}
    for name, fn in strats:
        try:
            passed, msg = fn()
        except Exception as ex:
            logger.debug("P4 策略 %s 异常: %s", name, ex)
            passed, msg = False, f"异常:{ex}"
        checks[name] = msg
        if passed:
            hits.append(name)

    out["strategies"] = hits
    out["strategy_checks"] = checks
    out["p4_core_screener_pass"] = len(hits) > 0
    cmw = circ_mv_wan_from(y, rt)
    # 【上线前修复 2026-10-18】北向资金 3 日评分已移除（hk_vol 全库恒为 0，已退役）。
    ind = str(rt.get("industry", "") or y.get("industry", ""))
    strat_inds = cfg.strategic_industries or []
    is_strategic = ind in strat_inds
    out["detail"] = {
        "close_live": round(close_live, 4),
        "pct_chg_day": round(pct, 3),
        "vol_hand_day": round(vol_hand, 2) if not np.isnan(vol_hand) else None,
        "circ_mv_wan": round(cmw, 2),
        "turnover_f_eff_pct": round(_trf(rt, y, close_live), 3),
        "is_strategic_industry": is_strategic,         # 是否战略赛道
    }
    # 【V26.88 P4 优化】winner_rate 机构地板：若通过任一策略但 winner_rate 偏低，否决。
    # 【V33.1 审计修复 · P1】原实现只把 p4_core_screener_pass 置 False，strategies 仍原样保留、
    # veto_pass 仍为 True → 这只票照样被算作「P4 命中」，唯一区别只是失去「金门禁豁免」。
    # 也就是说「机构地板」其实从未否决过任何票，与 docstring 里的「降权否决」不一致。
    # 这里改成真正的否决；同时用 0 < wr 保护：winner_rate 缺数据时 _field_rt_or_y 返回 0，
    # 若无条件否决，会把「数据缺失」误当成「散户高波动」而清空整池。
    wr = _field_rt_or_y(rt, y, "winner_rate")
    if out["p4_core_screener_pass"] and 0.0 < wr < cfg.winner_floor:
        out["strategies"] = []
        out["p4_core_screener_pass"] = False
        out["veto_pass"] = False
        out["veto_reason"] = f"winner_rate({wr:.1f}%) < 机构地板({cfg.winner_floor}%)"
    return out


def screen_p4_tail_universe(
    rows: List[Tuple[str, pd.DataFrame, Dict[str, Any]]],
    cfg: Optional[P4TailScreenerConfig] = None,
) -> pd.DataFrame:
    cfg = _resolve_p4_cfg(cfg)
    rec: List[Dict[str, Any]] = []
    for ts_code, df, rt in rows:
        r = evaluate_p4_tail_screener(df, rt, cfg)
        rec.append(
            {
                "ts_code": ts_code,
                "veto_pass": r["veto_pass"],
                "veto_reason": r["veto_reason"],
                "strategies": "|".join(r["strategies"]),
                "p4_core_screener_pass": r["p4_core_screener_pass"],
            }
        )
    return pd.DataFrame(rec)
