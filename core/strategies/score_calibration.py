# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88 — 综合分稳定化工具（截面 Rank + 对数饱和 + 爆发压制）
+ P1 底仓多维分项平滑插值（2026慢牛优化版，不读写 DuckDB 表结构）

【V26.88 优化版本】
1. 资金维度解耦：净流入强度（9分）与换手活跃度（6分）独立计分，消除量价同源隐性加权
2. 高位熔断渐进式惩罚：bias20 8~10% 扣4分，>10% 才扣12分；高位+缩量才算真正泡沫
3. 5日衰减因子优化：保留主升浪，5日涨幅<5%无衰减，5~12%温和衰减，>12%保持0.90
4. 组合乘数 elif 链重构：消除重复计算，顶级底仓共振优先级最高
5. 小市值插值点微调：<300亿市值门槛从2%降为1.5%，满分区间收窄
6. 量价背离窗口动态化：根据历史波动率调整检测窗口（7~15天）和阈值

【设计目的】
1. 缓解「换手 × 量比 × 战法爆发」在链条上的隐性多重共线性：同一日的流动性信息不应在性格乘子与战法分上被重复放大。
2. 对连续型极端值（涨幅、量比、真换手、主力推力）做截面分位与对数饱和，避免单日妖股靠一项指标把综合分拉到离谱，掩盖 P1 基因或其它维度短板。
3. 本模块只做数学变换与乘子，不读写 UI；输出由 scan_engine 在既有公式末尾乘上稳定因子，不改变 DataFrame 列名与展示口径。

【使用约定】
- 所有 Rank 均在「当次扫描候选集」内计算；样本过少（<5）时退化为 0.5 中性分位，避免噪声。
- 返回值保证有限、有界，不向外抛 NaN/inf。

【V26.88 新增 2026 慢牛优化】
+ 大盘多头容错：极致放量进攻（量比≥2.5 + 涨幅≥4%）时允许短期均线破坏，提升强势机构主导股捕获率
+ 硬科技共振加成：T0 战略赛道（电子/计算机/通信）资金+动能共振时组合乘数上限 ×1.05
+ 共振容错收紧：-4 优惠门槛从「资金+动能」升级为「资金+动能+战略核心行业加分≥12」
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def _finite01(x: float) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.5
    if not math.isfinite(v):
        return 0.5
    return float(np.clip(v, 0.0, 1.0))


def percentile_ranks(df: pd.DataFrame, cols: list) -> pd.DataFrame:
    """
    对指定列做截面百分比秩（average 法），结果落在 (0,1) 近似均匀。
    行数 < 5 时各列填 0.5。
    """
    out = df.copy()
    n = len(out)
    if n < 5:
        for c in cols:
            if c in out.columns:
                out[c + "_r"] = 0.5
        return out
    for c in cols:
        if c not in out.columns:
            out[c + "_r"] = 0.5
            continue
        s = pd.to_numeric(out[c], errors="coerce")
        s = s.replace([np.inf, -np.inf], np.nan).fillna(s.median() if s.notna().any() else 0.0)
        rk = s.rank(pct=True, method="average")
        out[c + "_r"] = rk.fillna(0.5).clip(0.001, 0.999)
    return out


def log_saturation(x: float, knee: float, ceiling: float) -> float:
    """
    标量对数饱和：x<=knee 近似线性；超过 knee 后增量递减，渐近于 ceiling。
    用于战法 burst 或加分项上沿，防止单日脉冲把总分顶穿。
    """
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(v):
        return 0.0
    v = max(v, 0.0)
    if v <= knee:
        return v
    span = max(ceiling - knee, 1e-9)
    excess = v - knee
    return float(knee + span * (1.0 - math.exp(-excess / max(span * 0.6, 1e-9))))


def compress_surge_bonus(surge: float, linear_cap: float = 10.0, asymptote: float = 16.0) -> float:
    """
    金共振/十绝等 surge_bonus：前 linear_cap 点保持原值，之后对数压缩，渐近于 asymptote。
    """
    s = max(0.0, float(np.nan_to_num(surge, nan=0.0, posinf=0.0, neginf=0.0)))
    return log_saturation(s, knee=linear_cap, ceiling=asymptote)


def dampen_burst_by_extremes(
    burst: float,
    p1_gene: float,
    vr_r: float,
    pct_r: float,
    trf_r: float,
    main_r: float,
) -> float:
    """
    在保持 burst 为「主信息源」的前提下，当 P1 基因偏弱而量价换手同时处于截面极端高分位、
    且主力推力分位不高时，对 burst 做温和下调（乘子约 0.55~1.0），减轻一日游妖票刷分。

    参数均为标量；main_r 为「主力推力」截面秩（越高越强）。
    """
    b = float(np.nan_to_num(burst, nan=0.0, posinf=0.0, neginf=0.0))
    if b <= 1e-9:
        return b
    vr_r, pct_r, trf_r, main_r = _finite01(vr_r), _finite01(pct_r), _finite01(trf_r), _finite01(main_r)
    pg = float(np.nan_to_num(p1_gene, nan=70.0, posinf=100.0, neginf=0.0))

    # 基因越低于 72 越「弱」，只在该区间发力
    gene_weak = max(0.0, min(1.0, (72.0 - pg) / 28.0))

    # 流动性双高：量比秩与换手秩同时偏高
    liq_hot = max(0.0, min(1.0, (vr_r + trf_r) * 0.5 - 0.78))

    # 涨幅截面也极端高
    price_hot = max(0.0, min(1.0, pct_r - 0.82))

    # 主力推力在截面并不强（秩偏低），却打出高 burst —— 典型「虚火」
    fund_cold = max(0.0, min(1.0, 0.72 - main_r))

    # 合成抑制强度（0~1）
    stress = gene_weak * liq_hot * price_hot * (0.35 + 0.65 * fund_cold)
    mult = 1.0 - 0.42 * stress
    mult = float(np.clip(mult, 0.55, 1.0))
    return float(b * mult)


def burst_soft_cap(burst: float, soft: float = 94.0, hard: float = 102.0) -> float:
    """
    战法 raw burst 上沿软封顶：soft 以下不变，超过部分对数压向 hard，避免配置里 110 分战法名实不符地线性顶格。
    """
    b = float(np.nan_to_num(burst, nan=0.0, posinf=hard, neginf=0.0))
    if b <= soft:
        return b
    return log_saturation(b, knee=soft, ceiling=hard)


def personality_liquidity_blend(trn_multi: float, vr_rank: float) -> float:
    """
    性格乘子：日内相对历史的换手倍数(trn_multi) 与 截面量比秩(vr_rank) 组合。
    用几何平均降低「换手与量比同源」的线性叠乘效应。
    """
    t = float(np.nan_to_num(trn_multi, nan=1.0, posinf=1.15, neginf=0.85))
    t = float(np.clip(t, 0.85, 1.15))
    vr = _finite01(vr_rank)
    # 将秩映射到与旧 vol_multi 可比的轻微摆动
    vol_from_rank = float(np.interp(vr, [0.05, 0.35, 0.55, 0.75, 0.95], [0.90, 0.97, 1.0, 1.06, 1.12]))
    vol_from_rank = float(np.clip(vol_from_rank, 0.85, 1.15))
    geo = math.sqrt(max(t, 1e-9) * max(vol_from_rank, 1e-9))
    return float(np.clip(geo, 0.85, 1.15))


def build_rank_lookup(cs_df: pd.DataFrame) -> Dict[str, Dict[str, float]]:
    """由带 *_r 列的截面表构建 s_code -> 分数字典。"""
    if cs_df is None or cs_df.empty or "s_code" not in cs_df.columns:
        return {}
    out: Dict[str, Dict[str, float]] = {}
    for _, row in cs_df.iterrows():
        sc = str(row.get("s_code", "")).strip()
        if not sc:
            continue
        out[sc] = {
            "pct_r": float(row.get("pct_r", 0.5) or 0.5),
            "vol_ratio_r": float(row.get("vol_ratio_r", 0.5) or 0.5),
            "turnover_f_r": float(row.get("turnover_f_r", 0.5) or 0.5),
            "main_ratio_r": float(row.get("main_ratio_r", 0.5) or 0.5),
        }
    return out


def neutral_ranks() -> Dict[str, float]:
    return {"pct_r": 0.5, "vol_ratio_r": 0.5, "turnover_f_r": 0.5, "main_ratio_r": 0.5}


# =============================================================================
# P1 底仓：多维分项平滑插值 + 行业/市值附加分（仅运算衍生分，不改 DuckDB DDL）
# =============================================================================
# 说明：已废弃「资金共振截面分」参与 P1 一票否决与排序；本函数为 P1 平滑百分制主来源；
# 可选按 config/constants 将 fund_memory_score 凸入最终分（与系统文档 FUND_MEMORY_WEIGHT_P1 一致）。


def _p1_scalar_finite(x: Any, default: float = 0.0) -> float:
    """与 pool_manager._safe_float 语义一致：None/NaN/空串回退 default，避免插值链污染。"""
    if x is None:
        return default
    try:
        if pd.isna(x) or str(x).strip() in ("", "-"):
            return default
        return float(x)
    except (ValueError, TypeError):
        return default


def _p1_finite_clip_scalar(x: Any) -> float:
    """压成有限 float，供 np.interp 与除法分母使用。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not np.isfinite(v):
        return 0.0
    return v


# =============================================================================
# 【V26.88 新增】P1 四大增强因子
# =============================================================================


def _calc_forecast_bonus_score(forecast_type: float) -> float:
    """
    【优化3：业绩预告/快报因子】
    - forecast_type: -2=首亏/续亏, -1=预减/略减, 0=扭亏, 0.5=不确定, 1=预增/略增/续盈
    返回: 业绩加分 (-3 ~ +5)
    """
    if not np.isfinite(forecast_type):
        return 0.0
    ft = float(forecast_type)
    if ft >= 1.0:  # 预增/略增/续盈
        return 3.0
    elif ft == 0.5:  # 扭亏
        return 1.5
    elif ft <= -2.0:  # 首亏/续亏（已在pool_manager一票否决，这里兜底）
        return -3.0
    elif ft <= -1.0:  # 预减/略减
        return -1.5
    return 0.0


def _calc_chip_concentration_bonus(df: pd.DataFrame) -> float:
    """
    【优化4：筹码集中度因子】
    基于股东户数变化趋势打分
    - 近2期股东户数降幅 > 25% → +3分
    - 近2期股东户数降幅 > 15% → +2分
    - 近2期股东户数降幅 > 8%  → +1分
    - 持平或增加 → 0分
    数据来源：日线 DataFrame 中查找 holder_num_prev / holder_num_curr 等字段
    """
    try:
        if df is None or df.empty or len(df) < 5:
            return 0.0
        
        # 尝试从日线 DataFrame 查找股东户数字段（由 data_fetcher 同步）
        holder_prev_col = None
        holder_curr_col = None
        for col in df.columns:
            col_lower = str(col).lower()
            if "holder_prev" in col_lower or "holder_num_prev" in col_lower:
                holder_prev_col = col
            if "holder_curr" in col_lower or "holder_num_curr" in col_lower or col_lower == "holder_num":
                holder_curr_col = col
        
        # 也检查实时字典
        holder_num_keys = ["holder_num", "holder_count", "shareholders", "holder_num_curr", "holder_num_prev"]
        
        if holder_curr_col is None:
            # 尝试从实时字段获取
            curr_row = df.iloc[-1]
            holder_curr = None
            for key in holder_num_keys:
                if key in curr_row.index:
                    holder_curr = _p1_finite_clip_scalar(curr_row[key])
                    if holder_curr > 0:
                        break
            
            if holder_curr is None or holder_curr <= 0:
                return 0.0
            
            # 尝试获取上一期
            holder_prev = None
            if len(df) >= 2:
                prev_row = df.iloc[-2]
                for key in holder_num_keys:
                    if key in prev_row.index:
                        holder_prev = _p1_finite_clip_scalar(prev_row[key])
                        if holder_prev > 0:
                            break
            
            if holder_prev is None or holder_prev <= 0 or holder_curr >= holder_prev:
                return 0.0
            
            # 计算降幅
            drop_pct = (holder_prev - holder_curr) / max(holder_prev, 1.0) * 100.0
            if drop_pct >= 25.0:
                return 3.0
            elif drop_pct >= 15.0:
                return 2.0
            elif drop_pct >= 8.0:
                return 1.0
            return 0.0
    except Exception:
        pass
    return 0.0


def _calc_sector_momentum_bonus(
    ind: str,
    df: pd.DataFrame,
    sector_rank_map: Dict[str, Any],
    dynamic_industries: Dict[str, Any],
) -> float:
    """
    【优化8：行业轮动动态权重】
    - 近期涨幅前3行业指数 → 该行业个股额外 +2分
    - 近期跌幅前3行业指数 → 该行业个股额外 -2分
    - 基于申万一级行业指数近20日涨幅计算相对强弱

    【V26.88 新增】增加3日短期行业动量权重（短线交易更关注短期板块轮动）
    
    返回: 行业动量加分 (-3 ~ +3)
    """
    try:
        if df is None or df.empty or len(df) < 25 or not ind:
            return 0.0
        
        # 计算该股票所属行业近20日涨幅
        c0 = _p1_finite_clip_scalar(df.iloc[-1].get("close", 0.0))
        c20 = _p1_finite_clip_scalar(df.iloc[-21].get("close", 0.0)) if len(df) >= 21 else None
        
        # 如果没有行业指数数据，使用个股20日涨幅作为行业代理
        if c20 is None or c20 <= 0 or c0 <= 0:
            return 0.0
        
        stock_20d_pct = (c0 / c20 - 1.0) * 100.0
        
        # 【V26.88 新增】计算3日短期行业动量
        c3 = _p1_finite_clip_scalar(df.iloc[-1].get("close", 0.0))
        c3_start = _p1_finite_clip_scalar(df.iloc[-4].get("close", 0.0)) if len(df) >= 4 else None
        stock_3d_pct = 0.0
        if c3_start is not None and c3_start > 0 and c3 > 0:
            stock_3d_pct = (c3 / c3_start - 1.0) * 100.0
        
        # 从 dynamic_industries 中获取板块排名
        bonus = 0.0
        if isinstance(dynamic_industries, dict):
            ctx = dynamic_industries.get("__sector_trend_context__", {})
            if isinstance(ctx, dict) and ind in ctx:
                ind_ctx = ctx[ind]
                recent_rank_mean = float(ind_ctx.get("recent_rank_mean_3", 999.0))
                
                # 排名前3 = 强势行业
                if recent_rank_mean <= 3.0 and stock_20d_pct > 0:
                    bonus = 2.0
                # 排名后3 = 弱势行业
                elif recent_rank_mean >= len(sector_rank_map) - 2 and stock_20d_pct < 0:
                    bonus = -2.0
        
        # 【V26.88 新增】3日短期动量加分（独立于20日，短线更敏感）
        if stock_3d_pct >= 5.0:
            bonus += 1.0  # 近3日涨幅>=5%，额外+1
        elif stock_3d_pct >= 3.0:
            bonus += 0.5  # 近3日涨幅>=3%，额外+0.5
        elif stock_3d_pct <= -5.0:
            bonus -= 1.0  # 近3日跌幅>=5%，额外-1
        elif stock_3d_pct <= -3.0:
            bonus -= 0.5  # 近3日跌幅>=3%，额外-0.5
        
        return float(np.clip(bonus, -3.0, 3.0))
    except Exception:
        return 0.0


def _calc_price_volume_divergence_penalty(df: pd.DataFrame, lookback_days: int = 10) -> float:
    """
    【优化6：量价背离提前预警】—— 窗口动态化
    - 近5日出现：涨幅>3%但量能<5日均量 → -2分
    - 近10日出现2次以上量价背离 → -3分（累计惩罚）
    - 量价背离是趋势减弱的早期信号
    - 【新增】窗口动态化：根据股票历史波动率调整背离检测敏感度
      - 高波动股（std>5%）：扩大窗口至15天，降低敏感度
      - 低波动股（std<2%）：缩小窗口至7天，提高敏感度
    """
    try:
        if df is None or df.empty or len(df) < 6:
            return 0.0
        
        # ------------------------------------------------------------------
        # 【优化6】窗口动态化：根据历史波动率调整检测窗口
        # ------------------------------------------------------------------
        # 计算近20日收益率标准差作为波动率代理
        lookback_for_volatility = min(20, len(df) - 1)
        if lookback_for_volatility >= 10 and "close" in df.columns:
            recent_prices = df["close"].iloc[-lookback_for_volatility:].values
            returns = np.diff(recent_prices) / recent_prices[:-1] * 100.0
            volatility_std = float(np.nanstd(returns))
        else:
            volatility_std = 3.0  # 默认中等波动
        
        # 根据波动率动态调整窗口
        if volatility_std > 5.0:
            # 高波动股：扩大窗口，降低敏感度
            effective_lookback = 15
            threshold_pct = 4.0  # 涨幅>4%才算背离
        elif volatility_std < 2.0:
            # 低波动股：缩小窗口，提高敏感度
            effective_lookback = 7
            threshold_pct = 2.5  # 涨幅>2.5%就算背离
        else:
            # 中等波动：保持默认
            effective_lookback = 10
            threshold_pct = 3.0
        
        penalty = 0.0
        divergence_count = 0
        recent = df.tail(effective_lookback).copy()
        
        # 计算5日均量（使用调整后的窗口）
        if "vol" in recent.columns:
            avg_vol_5 = recent["vol"].iloc[-6:-1].mean() if len(recent) >= 6 else recent["vol"].mean()
        else:
            avg_vol_5 = 1.0
        
        if avg_vol_5 <= 0:
            avg_vol_5 = 1.0
        
        for i in range(min(5, len(recent) - 1)):
            idx = len(recent) - 2 - i
            if idx < 0:
                break
            
            row = recent.iloc[idx]
            pct = _p1_finite_clip_scalar(row.get("pct_chg", 0.0))
            vol = _p1_finite_clip_scalar(row.get("vol", 0.0))
            
            # 量价背离：涨幅大但量能萎缩（使用动态阈值）
            if pct > threshold_pct and vol < avg_vol_5 * 0.85:
                divergence_count += 1
        
        if divergence_count >= 2:
            penalty = -3.0
        elif divergence_count == 1:
            penalty = -2.0
        
        return penalty
    except Exception:
        return 0.0


def _calc_excess_return_score(df: pd.DataFrame) -> float:
    """
    【V26.88 新增】板块内RS排序：计算个股相对板块的超额收益。

    计算近5日个股涨幅与板块（使用同板块个股平均）涨幅的差值：
    - 超额收益>3%：+2分（明显跑赢板块）
    - 超额收益>1%：+1分（小幅跑赢）
    - 超额收益<-3%：-2分（明显跑输）
    - 超额收益<-1%：-1分（小幅跑输）

    注意：由于没有板块指数数据，使用个股自身作为代理，
    通过近5日涨幅与近20日涨幅的对比来间接判断相对强弱。
    """
    try:
        if df is None or len(df) < 25 or "close" not in df.columns:
            return 0.0

        c0 = _p1_finite_clip_scalar(df.iloc[-1].get("close", 0.0))
        c5 = _p1_finite_clip_scalar(df.iloc[-6].get("close", 0.0)) if len(df) >= 6 else None
        c20 = _p1_finite_clip_scalar(df.iloc[-21].get("close", 0.0)) if len(df) >= 21 else None

        if c0 <= 0 or c5 is None or c20 is None:
            return 0.0

        # 近5日涨幅
        ret_5d = (c0 / c5 - 1.0) * 100.0
        # 近20日涨幅
        ret_20d = (c0 / c20 - 1.0) * 100.0

        # 使用5日/20日涨幅比率作为相对强弱代理
        # 如果5日涨幅占20日涨幅的比例高，说明近期加速，跑赢板块
        if ret_20d > 0:
            ratio = ret_5d / ret_20d if ret_20d != 0 else 0
            # 近期涨幅占20日涨幅的60%以上，说明强势
            if ratio >= 0.8 and ret_5d >= 3.0:
                return 2.0  # 明显跑赢
            elif ratio >= 0.6 and ret_5d >= 1.5:
                return 1.0  # 小幅跑赢
            elif ratio <= 0.3 and ret_5d <= -1.5:
                return -2.0  # 明显跑输
            elif ratio <= 0.5 and ret_5d <= -0.5:
                return -1.0  # 小幅跑输
        elif ret_20d < 0:
            # 20日下跌，但5日上涨，说明逆势走强
            if ret_5d > 1.0:
                return 1.5
            elif ret_5d > 3.0:
                return 2.0
            # 20日下跌，5日加速下跌
            if ret_5d < -2.0:
                return -1.5

        return 0.0
    except Exception:
        return 0.0


# =============================================================================
# 【P1·底仓升级 V2】周线维度辅助函数（互联网实战共识导向）
# 核心目标：识别趋势真伪、过滤高位接盘、识别主力建仓+洗盘。
# 与日线维度正交：从周时间尺度过滤噪音，与日线 MA 一票否决互补。
# =============================================================================

_WEEKLY_ROW_CACHE: Dict[str, Any] = {}
_WEEKLY_ROW_CACHE_TTL_SEC = 300.0


def _fetch_weekly_row(ts_code: str) -> Dict[str, Any]:
    """从 weekly_data 读最近一周的行（用于 P1 打分时实时取最新一周信号）。

    返回字典：ma5_w/ma10_w/ma20_w/ma60_w/close_w + 4 周前 MA + 近 5 周均量 + 上周量数据。
    找不到/无数据时返回空 dict；调用方按"无信号"处理。

    【V33 性能】weekly_data 一周才更新一次，而 P3/P4/P5 每轮扫描对每只票都要读一次，
    等于把同一条 SQL 重复打几千遍。这里按 ts_code 做 5 分钟 TTL 缓存，
    不改变任何打分口径（只减少重复 IO）。
    """
    if not ts_code:
        return {}
    _key = str(ts_code).strip()
    try:
        import time as _t
        _entry = _WEEKLY_ROW_CACHE.get(_key)
        if _entry is not None and (_t.time() - float(_entry[0])) <= _WEEKLY_ROW_CACHE_TTL_SEC:
            return dict(_entry[1]) if isinstance(_entry[1], dict) else {}
    except Exception:
        _entry = None
    _out = _fetch_weekly_row_raw(_key)
    try:
        import time as _t
        if len(_WEEKLY_ROW_CACHE) > 8000:
            _WEEKLY_ROW_CACHE.clear()
        _WEEKLY_ROW_CACHE[_key] = (_t.time(), dict(_out))
    except Exception:
        pass
    return _out


def _fetch_weekly_row_raw(ts_code: str) -> Dict[str, Any]:
    if not ts_code:
        return {}
    try:
        # 【V30.1 2026-10-16 修 DuckDB 连接配置冲突】
        # 旧实现直连 duckdb(..., read_only=True)，同进程存在读写连接时必抛配置冲突，
        # 被 except 吞成 logger.debug，P1 周线打分静默退化为 trend=0/vol=1。
        # 统一走 data.db_core 读连接入口，避免进程内连接配置冲突。
        from data.db_core import get_read_conn, get_duckdb_path

        db_path = get_duckdb_path()
        if not db_path or not os.path.isfile(str(db_path)):
            return {}
        with get_read_conn(read_only=True) as con:
            row = con.execute(
                """
                SELECT ma5_w, ma10_w, ma20_w, ma60_w,
                       ma20_w_4w_ago, ma60_w_4w_ago,
                       close_w, vol_w, vol_avg_5w,
                       vol_w_prev1, vol_w_prev2, is_up_week_prev1,
                       pct_chg_w, is_up_week
                FROM weekly_data
                WHERE ts_code = ?
                ORDER BY week_end DESC
                LIMIT 1
                """,
                [str(ts_code).strip()],
            ).fetchone()
        if not row:
            return {}
        return {
            "ma5_w": float(row[0] or 0.0),
            "ma10_w": float(row[1] or 0.0),
            "ma20_w": float(row[2] or 0.0),
            "ma60_w": float(row[3] or 0.0),
            "ma20_w_4w_ago": float(row[4] or 0.0),
            "ma60_w_4w_ago": float(row[5] or 0.0),
            "close_w": float(row[6] or 0.0),
            "vol_w": float(row[7] or 0.0),
            "vol_avg_5w": float(row[8] or 0.0),
            "vol_w_prev1": float(row[9] or 0.0),
            "vol_w_prev2": float(row[10] or 0.0),
            "is_up_week_prev1": bool(row[11]) if row[11] is not None else False,
            "pct_chg_w": float(row[12] or 0.0),
            "is_up_week": bool(row[13]) if row[13] is not None else False,
        }
    except Exception as _e:
        logger.debug("P1 周线读取异常 [%s]: %s", ts_code, _e)
        return {}


def _calc_weekly_trend_score(wf: Dict[str, Any], cfg: Dict[str, float], safety_cfg: Dict[str, int]) -> float:
    """【V2】周线趋势打分（互联网共识导向，避免"接盘高位"）。

    6 档：
      +6  标准多头：MA5>MA10>MA20 + MA20 4w_ago > 0 且 MA20>MA20_4w_ago + close>MA20
      +4  初期多头：MA5>MA10>MA20（拐头未确认）
      +2  中期向上短期回调：MA10>MA20 且 MA5<=MA10
       0  危险走平：MA20 4w_ago 已存在且近 4 周涨跌幅 ∈ [-0.5%, +0.5%]
      -2  60 周牛熊线下：(close<MA60) 或 (MA60 4 周前存在且 MA60<=MA60_4w_ago)
      -4  空头排列：MA5<MA10<MA20 且 close<MA20

    安全阀：当 MA60 历史周数 < safety.min_history_weeks 时不施加"60周下"惩罚（避免新股误判）。
    """
    if not wf or wf.get("ma20_w", 0.0) <= 0:
        return float(cfg.get("danger_flat", 0.0))

    ma5 = float(wf.get("ma5_w", 0.0))
    ma10 = float(wf.get("ma10_w", 0.0))
    ma20 = float(wf.get("ma20_w", 0.0))
    ma60 = float(wf.get("ma60_w", 0.0))
    ma20_4w = float(wf.get("ma20_w_4w_ago", 0.0))
    ma60_4w = float(wf.get("ma60_w_4w_ago", 0.0))
    close_w = float(wf.get("close_w", 0.0))

    # 1) 空头排列（最严）
    if ma5 > 0 and ma5 < ma10 < ma20 and close_w > 0 and close_w < ma20:
        return float(cfg.get("short_align", -4.0))

    # 2) 60周牛熊线下（互联网共识最大避险信号）；MA60 历史不足时跳过
    enough_history = (ma60_4w > 0.0) and (ma60 > 0.0)
    if enough_history and (close_w > 0 and close_w < ma60):
        return float(cfg.get("below_ma60", -2.0))
    if enough_history and ma60 <= ma60_4w:
        return float(cfg.get("below_ma60", -2.0))

    # 3) 危险走平（MA20 4 周前已存在且近 4 周涨跌 < 0.5%）
    if ma20_4w > 0 and abs((ma20 - ma20_4w) / ma20_4w) < 0.005:
        return float(cfg.get("danger_flat", 0.0))

    # 4) 标准多头（最严）
    if ma5 > ma10 > ma20 and ma20 > ma20_4w and close_w > ma20:
        return float(cfg.get("standard_full", 6.0))

    # 5) 初期多头
    if ma5 > ma10 > ma20:
        return float(cfg.get("initial_full", 4.0))

    # 6) 中期向上短期回调
    if ma10 > ma20:
        return float(cfg.get("ma20_above_shake", 2.0))

    return float(cfg.get("danger_flat", 0.0))


def _calc_weekly_volume_score(wf: Dict[str, Any], cfg: Dict[str, float]) -> float:
    """【V2】周线放量打分（互联网共识，避陷阱为主）。

    6 档：
      +4  温和放量上涨：1.2 ≤ ratio ≤ 1.8 且 周阳线（健康）
      +2  温和放量下跌：1.2 ≤ ratio ≤ 1.8 且 周阴线（健康洗盘）
      +1  平量（0.9 ≤ ratio < 1.2）
      +1  爆量上涨：ratio > 1.8 且 周阳线（透支，仍给正分但不强奖励）
      -2  缩量上涨：ratio < 0.9 且 周阳线（互联网共识"无量突破皆为假"，高危）
      -4  高位放量下跌：ratio > 1.8 且 周阴线（互联网共识"主力出货"双杀）

    ratio = vol_w / vol_avg_5w
    """
    if not wf or wf.get("vol_avg_5w", 0.0) <= 0:
        return float(cfg.get("flat", 1.0))
    ratio = float(wf.get("vol_w", 0.0)) / float(wf["vol_avg_5w"])
    is_up = bool(wf.get("is_up_week", False))

    # 缩量上涨 = 高危陷阱（第一避坑）
    if ratio < 0.9 and is_up:
        return float(cfg.get("shrink_up", -2.0))
    # 高位放量下跌 = 双杀陷阱（第二避坑）
    if ratio > 1.8 and not is_up:
        return float(cfg.get("surge_down", -4.0))
    # 温和放量上涨（健康）：1.05 ≤ ratio ≤ 1.8 且 周阳线
    if 1.05 <= ratio <= 1.8 and is_up:
        return float(cfg.get("warm_up_full", 4.0))
    # 温和放量下跌（健康洗盘）：1.05 ≤ ratio ≤ 1.8 且 周阴线
    if 1.05 <= ratio <= 1.8 and not is_up:
        return float(cfg.get("warm_down", 2.0))
    # 爆量上涨（透支，不强奖励）：ratio > 1.8 且 周阳线
    if ratio > 1.8 and is_up:
        return float(cfg.get("surge_up_tired", 1.0))

    return float(cfg.get("flat", 1.0))


def _calc_weekly_combo_bonus(wf: Dict[str, Any], cfg: Dict[str, float]) -> float:
    """【V2】量价组合加成（互联网共识"放量建仓 + 缩量洗盘"识别）。

    两条规则：
      A. pullback_volume_shrink (默认 +1):
         本周放量涨（ratio>1.2 且周阳线） + 上周缩量（vol_w_prev1 < vol_w_prev2 * 0.9）
         → 经典主力动作：放量建仓后缩量洗盘
      B. three_week_surge (默认 -1):
         本周、本周前 1、本周前 2 连续 3 周 ratio 均 > 1.5
         → 持续爆量透支后续动能
    """
    if not wf or wf.get("vol_avg_5w", 0.0) <= 0:
        return 0.0
    vol = float(wf.get("vol_w", 0.0))
    vol_avg = float(wf["vol_avg_5w"])
    vol_prev1 = float(wf.get("vol_w_prev1", 0.0))
    vol_prev2 = float(wf.get("vol_w_prev2", 0.0))
    is_up = bool(wf.get("is_up_week", False))
    if vol_avg <= 0:
        return 0.0
    cur_ratio = vol / vol_avg
    prev1_ratio = vol_prev1 / vol_avg if vol_avg > 0 and vol_prev1 > 0 else 0.0
    prev2_ratio = vol_prev2 / vol_avg if vol_avg > 0 and vol_prev2 > 0 else 0.0

    bonus = 0.0
    # 规则 A：本周放量涨 + 上周缩量
    if cur_ratio > 1.05 and is_up and vol_prev1 > 0 and vol_prev2 > 0 and vol_prev1 < vol_prev2 * 0.9:
        bonus += float(cfg.get("pullback_volume_shrink", 1.0))
    # 规则 B：连续 3 周放量（透支）
    if cur_ratio > 1.5 and prev1_ratio > 1.5 and prev2_ratio > 1.5:
        bonus += float(cfg.get("three_week_surge", -1.0))
    return float(bonus)


def _compute_weekly_buff(
    ts_code: str,
    weekly_cfg_root: Dict[str, Any],
    single_pts: float = 3.0,
    double_pts: float = 6.0,
    bear_penalty_pts: float = 0.0,
    trend_positive_min: float = 4.5,
    volume_positive_min: float = 2.5,
) -> Dict[str, float]:
    """【V26.88+】周线趋势/放量 buff 计算（P3 / P4 共用）。

    - 周线趋势多头（trend ≥ trend_positive_min）→ 单 buff +single_pts
    - 周线放量（vol   ≥ volume_positive_min）→ 单 buff +single_pts
    - 双强（两项同时成立）                  → +double_pts（不与单项相加）
    - 空头 + 缩量 → 默认 bear_penalty_pts=0（保持中性，不扣分）
    - 阈值默认与 P1 周线双强共振 (resonance.trend_min=4.5 / volume_min=2.5) 完全一致

    返回 dict：{'pts','trend','vol','is_double_strong','is_trend_hit','is_vol_hit'}
    调用方拿到 pts 后叠加到 raw_burst / burst_score；P3 / P4 各自的 burst_score 仍走 min(...,100) 封顶。
    """
    out: Dict[str, float] = {
        "pts": 0.0,
        "trend": 0.0,
        "vol": 0.0,
        "is_double_strong": 0.0,
        "is_trend_hit": 0.0,
        "is_vol_hit": 0.0,
    }
    if not ts_code or not weekly_cfg_root.get("enabled", True):
        return out
    try:
        _wf = _fetch_weekly_row(str(ts_code).strip())
        if not _wf:
            return out
        out["trend"] = round(float(_calc_weekly_trend_score(
            _wf, weekly_cfg_root.get("trend", {}), weekly_cfg_root.get("safety", {})
        )), 2)
        out["vol"] = round(float(_calc_weekly_volume_score(
            _wf, weekly_cfg_root.get("volume", {})
        )), 2)
    except Exception as _e:
        logger.debug("P3/P4 周线 buff 读取异常 [%s]: %s", ts_code, _e)
        return out

    is_trend_hit = out["trend"] >= float(trend_positive_min)
    is_vol_hit = out["vol"] >= float(volume_positive_min)
    out["is_trend_hit"] = 1.0 if is_trend_hit else 0.0
    out["is_vol_hit"] = 1.0 if is_vol_hit else 0.0

    if is_trend_hit and is_vol_hit:
        out["pts"] = float(double_pts)
        out["is_double_strong"] = 1.0
    elif is_trend_hit or is_vol_hit:
        out["pts"] = float(single_pts)
    else:
        out["pts"] = float(bear_penalty_pts)
    return out


def compute_p1_multi_dim_smooth_score(
    df: pd.DataFrame,
    rt: Any,
    circ_mv_yi: float,
    ind_rank: int,
    pe: float,
    ind_stats: Dict[str, Any],
    ind: str,
    dynamic_industries: Dict[str, float],
    avg_trn: float,
    pass_line: float,
) -> Tuple[float, bool, str, Dict[str, Any]]:
    """
    P1 底仓「多维平滑插值」百分制（满分 100，含附加分后仍 cap 在 100）。

    分项结构（非字面「11 个」）：核心插值项含筹码/趋势/均线/资金/势能/波段/黄金/健康/斜率/PE/熔断等，
    明细中另含假突破惩罚、行业/板块/市值附加与市值档位标签。

    执行顺序：①左侧否决 → ②各维写入 score_details →③base_score 与记忆凸组合得 final
    →④据 details 与入参 pass_line 算 effective_pass_line（共振时 base-4）并判定 passed。

    约束：
    - 不读取、不写入、不修改 DuckDB 表结构；仅使用传入的 DataFrame / 行情字典做派生运算。
    - 不使用日线 capital_resonance_score；fund_memory_score（0~200→0~100）仅当 fund_memory_weight_p1>0 时参与
      final_score = (1-w)*base_score + w*fm100；base_score 为正面维度和 + 假突破惩罚 + 高位熔断倒扣。

    返回：(final_score, is_pass, reason, score_details)
    """
    try:
        from core.strategies.fund_mv_utils import effective_turnover_rate_f
        from core.config_manager import (
            get_p1_combo_multiplier_config,
            get_p1_fund_memory_weight,
            get_p1_select_min_circ_mv_wan,
            get_p1_weekly_config,
        )

        try:
            p1_min_yi = float(get_p1_select_min_circ_mv_wan()) / 10000.0
        except Exception:
            try:
                import constants as _constants

                p1_min_yi = float(getattr(_constants, "P1_SELECT_MIN_CIRC_MV_WAN", 1_000_000)) / 10000.0
            except Exception:
                p1_min_yi = 100.0

        rt_d = rt if isinstance(rt, dict) else {}
        curr = df.iloc[-1]

        # ------------------------------------------------------------------
        # 防守端：带放量豁免的左侧一票否决（先于各维度计分）
        # ------------------------------------------------------------------
        _ma20_v = _p1_scalar_finite(curr.get("ma20", 0.0), 0.0)
        _ma60_v = _p1_scalar_finite(curr.get("ma60", 0.0), 0.0)
        _slope5_v = _p1_scalar_finite(curr.get("ma20_slope_5", 0.0), 0.0)
        _bias20_v = _p1_scalar_finite(curr.get("bias_20", 0.0), 0.0)
        _vr_v = _p1_scalar_finite(rt_d.get("vol_ratio", curr.get("vol_ratio", 1.0)), 1.0)
        if not np.isfinite(_vr_v) or _vr_v <= 0:
            _vr_v = 1.0
        _pct_v = _p1_scalar_finite(rt_d.get("pct_chg", curr.get("pct_chg", 0.0)), 0.0)
        if not np.isfinite(_pct_v):
            _pct_v = 0.0
        _close_v = _p1_scalar_finite(curr.get("close", 0.0), 0.0)
        _break_left = (
            _slope5_v < -0.05
            or (_ma60_v > 0 and _ma20_v < _ma60_v * 0.985)
            or _bias20_v < -12.0
        )
        # 2026慢牛容错①：普通放量豁免（原条件）
        _exempt = _vr_v >= 1.4 and _pct_v >= 2.5 and _close_v >= _ma20_v
        # 2026慢牛容错②：强势机构进攻容错——资金大幅抢筹时可以宽容短期均线破坏
        # 在机构主导市场中，极致放量进攻（量比≥2.5 且涨幅≥4%）偶尔会短暂跌破均线，不应一票否决
        _exempt_bull = (
            _vr_v >= 2.5
            and _pct_v >= 4.0
            and _close_v >= _ma20_v * 0.97  # 贴线附近即可，不要求完全站上
        )
        if _break_left and not _exempt and not _exempt_bull:
            return (0.0, False, "左侧极寒未放量，一票否决", {})

        # ------------------------------------------------------------------
        # 现价：优先日线 DataFrame 末行收盘价（固定），仅在日线价格为 0 时回退实时价格，保证收盘后结果稳定。
        # ------------------------------------------------------------------
        _df_close = _p1_scalar_finite(curr.get("close", 0.0), 0.0)
        _rt_price = _p1_scalar_finite(rt_d.get("price", 0.0), 0.0)
        now_price = _df_close if _df_close > 0 else _rt_price

        # ------------------------------------------------------------------
        # ① 筹码真空（满分 14）：按真实换手 turnover_rate_f 做分段线性插值。
        # ------------------------------------------------------------------
        turnover_today = effective_turnover_rate_f(rt_d, curr, now_price)
        if not np.isfinite(turnover_today):
            turnover_today = 0.0
        turnover_today = float(np.clip(turnover_today, 0.0, 100.0))
        turnover_eval = turnover_today if turnover_today > 0 else _p1_scalar_finite(avg_trn, 0.0)
        turnover_eval = float(np.clip(_p1_finite_clip_scalar(turnover_eval), 0.0, 100.0))
        xp_chip = [0.0, 0.8, 1.5, 3.0, 5.0, 8.0]
        yp_chip = np.array([0.0, 2.0, 6.0, 12.0, 16.0, 18.0], dtype=float) * (14.0 / 18.0)
        chip_score = float(np.interp(turnover_eval, xp_chip, list(yp_chip)))
        chip_score = float(np.nan_to_num(chip_score, nan=0.0, posinf=0.0, neginf=0.0))

        # ------------------------------------------------------------------
        # ② 趋势距离（满分 10）：MA20 相对 MA60 的百分比距离，多头拉开时给满，过度发散后降分。
        # ------------------------------------------------------------------
        ma20 = _p1_scalar_finite(curr.get("ma20", 0.0), 0.0)
        ma60 = _p1_scalar_finite(curr.get("ma60", 1.0), 1.0)
        ma60_safe = max(_p1_finite_clip_scalar(ma60), 1e-9)
        dist_pct = ((ma20 - ma60) / ma60_safe * 100.0) if ma60 > 0 else 0.0
        dist_pct = float(np.nan_to_num(dist_pct, nan=0.0, posinf=0.0, neginf=0.0))
        xp_dist = [-5.0, 0.0, 2.0, 6.0, 12.0, 18.0]
        yp_dist = np.array([0.0, 8.0, 16.0, 16.0, 6.0, 0.0], dtype=float) * (10.0 / 16.0)
        trend_dist_score = float(np.interp(dist_pct, xp_dist, list(yp_dist)))
        trend_dist_score = float(np.nan_to_num(trend_dist_score, nan=0.0, posinf=0.0, neginf=0.0))

        # ------------------------------------------------------------------
        # ③ 均线成熟（满分 8）：MA20/MA60 比值贴近 1.02~1.15 视为「成熟多头」平台区。
        # ------------------------------------------------------------------
        ma60_m = _p1_scalar_finite(curr.get("ma60", 1.0), 1.0)
        ma60_ms = max(_p1_finite_clip_scalar(ma60_m), 1e-9)
        maturity = (ma20 / ma60_ms) if ma60_m > 0 else 1.0
        maturity = float(np.nan_to_num(maturity, nan=1.0, posinf=1.0, neginf=0.0))
        xp_mat = [0.95, 1.00, 1.02, 1.15, 1.30, 1.45]
        yp_mat = np.array([0.0, 4.0, 12.0, 12.0, 4.0, 0.0], dtype=float) * (8.0 / 12.0)
        mat_score = float(np.interp(maturity, xp_mat, list(yp_mat)))
        mat_score = float(np.nan_to_num(mat_score, nan=0.0, posinf=0.0, neginf=0.0))

        # ------------------------------------------------------------------
        # ④ 资金攻击（满分 15）：拆解为「净流入强度」+ 「换手活跃度」双因子独立计分，避免量价同源导致的隐性加权。
        # 因子1：净流入强度（满分 9）—— 北向+主力近5日加权净流入 vs 成交额
        # ------------------------------------------------------------------
        df_5d = df.tail(5)
        time_weights = [0.2, 0.4, 0.6, 0.8, 1.0]
        weighted_inflow = 0.0
        for i in range(min(5, len(df_5d))):
            row = df_5d.iloc[i]
            day_inflow = 0.0
            for col in ("net_main_amount",):
                if col in row:
                    day_inflow += _p1_scalar_finite(row[col], 0.0)
            weighted_inflow += day_inflow * time_weights[i]
        amount_sum_5d = _p1_scalar_finite(df_5d["amount"].sum(), 0.0) if "amount" in df_5d.columns else 0.0
        _amt5 = max(_p1_finite_clip_scalar(amount_sum_5d), 1e-9)
        inflow_vs_amount = weighted_inflow / _amt5 if amount_sum_5d > 0 else 0.0
        inflow_vs_amount = float(np.nan_to_num(inflow_vs_amount, nan=0.0, posinf=0.0, neginf=0.0))
        xp_flow = [-0.05, 0.0, 0.02, 0.05, 0.10, 0.20]
        yp_flow = [0.0, 0.0, 3.0, 6.0, 8.0, 10.0]
        flow_score = float(np.interp(inflow_vs_amount, xp_flow, yp_flow))
        flow_score = float(np.nan_to_num(flow_score, nan=0.0, posinf=0.0, neginf=0.0))
        flow_score = min(flow_score, 9.0)  # 因子1封顶9分

        # ------------------------------------------------------------------
        # 因子2：换手活跃度（满分 6）—— 独立于净流入，用真实换手率衡量市场参与热度
        # 【优化1】用 vol_ratio 和 turnover_eval 共同衡量活跃度，避免单一指标偏差
        # ------------------------------------------------------------------
        vol_ratio = _p1_scalar_finite(rt_d.get("vol_ratio", curr.get("vol_ratio", 1.0)), 1.0)
        if not np.isfinite(vol_ratio) or vol_ratio <= 0:
            vol_ratio = 1.0
        # 综合活跃度：量比为主，换手率为辅（用除以2来平衡量纲差异）
        activity_proxy = max(vol_ratio, turnover_eval / 2.0)
        activity_proxy = float(np.nan_to_num(activity_proxy, nan=1.0, posinf=10.0, neginf=0.0))
        xp_act = [0.8, 1.2, 1.8, 2.5, 4.0]
        yp_act = [0.0, 1.0, 2.5, 4.5, 6.0]
        act_score = float(np.interp(float(activity_proxy), xp_act, yp_act))
        act_score = float(np.nan_to_num(act_score, nan=0.0, posinf=0.0, neginf=0.0))
        act_score = min(act_score, 6.0)  # 因子2封顶6分

        fund_score = min(flow_score + act_score, 15.0)

        # ------------------------------------------------------------------
        # ④b 启动势能（10 分）：pct_chg_20d / vol_ratio / 突破 / pct_chg_5d 衰减，全程 np.interp（突破奖励为二值）
        # ------------------------------------------------------------------
        momentum_score = 0.0
        fake_penalty_val = 0.0
        close_px = _p1_scalar_finite(curr.get("close", 0.0), 0.0)
        pct_chg_20d = _p1_scalar_finite(curr.get("pct_chg_20d", np.nan), np.nan)
        if not np.isfinite(pct_chg_20d):
            if len(df) >= 21 and "close" in df.columns:
                c0 = _p1_scalar_finite(curr.get("close", 0.0), 0.0)
                c20 = _p1_scalar_finite(df.iloc[-21]["close"], 0.0)
                pct_chg_20d = (c0 / max(c20, 1e-9) - 1.0) * 100.0 if c20 > 0 else 0.0
            else:
                pct_chg_20d = 0.0
        pct_chg_20d = float(np.nan_to_num(pct_chg_20d, nan=0.0, posinf=0.0, neginf=0.0))

        pct_chg_5d = _p1_scalar_finite(curr.get("pct_chg_5d", np.nan), np.nan)
        if not np.isfinite(pct_chg_5d):
            if len(df) >= 6 and "close" in df.columns:
                c0 = _p1_scalar_finite(curr.get("close", 0.0), 0.0)
                c5 = _p1_scalar_finite(df.iloc[-6]["close"], 0.0)
                pct_chg_5d = (c0 / max(c5, 1e-9) - 1.0) * 100.0 if c5 > 0 else 0.0
            else:
                pct_chg_5d = 0.0
        pct_chg_5d = float(np.nan_to_num(pct_chg_5d, nan=0.0, posinf=0.0, neginf=0.0))

        high_10d = _p1_scalar_finite(curr.get("high_10d", np.nan), np.nan)
        if not np.isfinite(high_10d) or high_10d <= 0:
            if "high" in df.columns and len(df) >= 11:
                high_10d = float(df.iloc[-11:-1]["high"].astype(float).max())
            elif "high" in df.columns and len(df) >= 2:
                high_10d = float(df.iloc[:-1]["high"].astype(float).tail(min(10, len(df) - 1)).max())
            else:
                high_10d = _p1_scalar_finite(curr.get("high", 0.0), 0.0)

        xp_pct = np.array([-10.0, 0.0, 5.0, 12.0, 18.0, 25.0], dtype=float)
        yp_pct = np.array([0.0, 2.0, 6.0, 6.0, 2.0, 0.0], dtype=float)
        score_pct = float(np.interp(float(max(pct_chg_20d, -10.0)), xp_pct, yp_pct))
        score_pct = float(np.nan_to_num(score_pct, nan=0.0, posinf=0.0, neginf=0.0))

        xp_vol_m = np.array([1.0, 1.4, 2.0, 3.5], dtype=float)
        yp_vol_m = np.array([0.0, 2.0, 4.0, 3.0], dtype=float)
        score_vol = float(np.interp(float(vol_ratio), xp_vol_m, yp_vol_m))
        score_vol = float(np.nan_to_num(score_vol, nan=0.0, posinf=0.0, neginf=0.0))

        close_eff = now_price if now_price > 0 else close_px
        is_breakout = bool(high_10d > 0 and close_eff > high_10d * 0.995)
        bonus_breakout = 2.0 if is_breakout else 0.0

        # ------------------------------------------------------------------
        # 【优化3】5日衰减因子优化：改为分段，保留主升浪动能
        # - 5日涨幅 < 5%：无衰减（1.0）
        # - 5日涨幅 5~12%：温和衰减（1.0 → 0.85）
        # - 5日涨幅 > 12%：保留主升浪（0.90），不再继续惩罚
        # ------------------------------------------------------------------
        xp_dec = np.array([0.0, 5.0, 12.0, 18.0], dtype=float)
        yp_dec = np.array([1.0, 1.0, 0.88, 0.90], dtype=float)
        decay_factor = float(np.interp(float(pct_chg_5d), xp_dec, yp_dec))
        decay_factor = float(np.nan_to_num(decay_factor, nan=1.0, posinf=1.0, neginf=0.0))

        momentum_score = min(
            10.0,
            round((score_pct + score_vol + bonus_breakout) * decay_factor, 2),
        )

        if is_breakout and vol_ratio < 1.25:
            penalty = min(9.0, momentum_score * 0.85)
            fake_penalty_val = -round(penalty, 2)

        # ------------------------------------------------------------------
        # ⑤ 波段涨幅（10 分）：max_60d_pct 双峰 np.interp
        # ------------------------------------------------------------------
        max_60d_pct = _p1_scalar_finite(curr.get("max_60d_pct", 0.0), 0.0)
        _mx60 = float(np.nan_to_num(_p1_finite_clip_scalar(max_60d_pct), nan=0.0, posinf=0.0, neginf=0.0))
        xp_gain = np.array([-5.0, 3.0, 6.0, 9.0, 11.0, 14.0, 18.0, 22.0, 25.0, 30.0], dtype=float)
        yp_gain = np.array([0.0, 6.0, 10.0, 8.0, 4.0, 8.0, 10.0, 8.0, 2.0, 0.0], dtype=float)
        gain_score = float(np.interp(_mx60, xp_gain, yp_gain))
        gain_score = float(np.nan_to_num(gain_score, nan=0.0, posinf=0.0, neginf=0.0))
        gain_score = min(10.0, gain_score)

        # ------------------------------------------------------------------
        # ⑥ 黄金起爆（满分 12）：仅保留 circ_mv_yi（亿元）三档 + np.interp(pct_chg/vol_ratio)，无历史 if/elif 阶梯。
        # ------------------------------------------------------------------
        pct_chg = _p1_scalar_finite(rt_d.get("pct_chg", curr.get("pct_chg", 0.0)), 0.0)
        if not np.isfinite(pct_chg):
            pct_chg = 0.0
        if float(circ_mv_yi) > 500.0:
            xp_pg = np.array([1.0, 2.5, 4.5], dtype=float)
            yp_pg = np.array([0.0, 8.0, 4.0], dtype=float)
            xp_vg = np.array([1.0, 1.4, 2.0], dtype=float)
            yp_vg = np.array([0.0, 4.0, 4.0], dtype=float)
        elif float(circ_mv_yi) >= 300.0:
            xp_pg = np.array([1.5, 3.5, 5.5], dtype=float)
            yp_pg = np.array([0.0, 8.0, 4.0], dtype=float)
            xp_vg = np.array([1.2, 1.6, 2.2], dtype=float)
            yp_vg = np.array([0.0, 4.0, 4.0], dtype=float)
        else:
            # 【优化5】小市值（<300亿）插值点微调：降低涨幅门槛，更贴合2026年市场风格
            # 保留适度弹性（最低2%起），但缩短满分区间，避免小票靠一日脉冲拿满分
            xp_pg = np.array([1.5, 3.0, 5.5, 8.0], dtype=float)
            yp_pg = np.array([0.0, 6.0, 8.0, 4.0], dtype=float)
            xp_vg = np.array([1.3, 1.8, 2.5], dtype=float)
            yp_vg = np.array([0.0, 4.0, 4.0], dtype=float)
        g_pct = float(np.interp(float(pct_chg), xp_pg, yp_pg))
        g_vol = float(np.interp(float(vol_ratio), xp_vg, yp_vg))
        g_pct = float(np.nan_to_num(g_pct, nan=0.0, posinf=0.0, neginf=0.0))
        g_vol = float(np.nan_to_num(g_vol, nan=0.0, posinf=0.0, neginf=0.0))
        golden_score = min(12.0, round(g_pct + g_vol, 2))

        # ------------------------------------------------------------------
        # ⑦ 趋势健康（满分 4）：乖离 bias20——贴线运行健康，过度偏离双向降分。
        # ------------------------------------------------------------------
        ma20_bias_denom = max(_p1_finite_clip_scalar(ma20), 1e-9)
        fallback_bias = (now_price - ma20) / ma20_bias_denom * 100.0
        fallback_bias = float(np.nan_to_num(fallback_bias, nan=0.0, posinf=0.0, neginf=0.0))
        bias20 = _p1_scalar_finite(curr.get("bias_20", fallback_bias), fallback_bias)
        bias20 = float(np.nan_to_num(bias20, nan=fallback_bias, posinf=0.0, neginf=0.0))
        xp_health = [-15.0, -5.0, -2.0, 1.0, 4.0, 8.0]
        yp_health = [0.0, 1.3, 3.3, 4.0, 3.3, 1.3]
        trend_health_score = float(np.interp(bias20, xp_health, yp_health))
        trend_health_score = float(np.nan_to_num(trend_health_score, nan=0.0, posinf=0.0, neginf=0.0))

        # ------------------------------------------------------------------
        # ⑧ 主升斜率（满分 9）：ma20_slope_5 五年化斜率百分数，过快或过慢均降分。
        # ------------------------------------------------------------------
        slope_5d = _p1_scalar_finite(curr.get("ma20_slope_5", 0.0), 0.0)
        xp_slope = [0.0, 1.2, 1.8, 2.8, 3.8, 5.5]
        yp_slope = np.array([0.0, 3.0, 6.0, 8.0, 5.0, 0.0], dtype=float) * (9.0 / 8.0)
        slope_score = float(np.interp(_p1_finite_clip_scalar(slope_5d), xp_slope, list(yp_slope)))
        slope_score = float(np.nan_to_num(slope_score, nan=0.0, posinf=0.0, neginf=0.0))

        # ------------------------------------------------------------------
        # ⑨ 动态 PE（满分 3）：相对行业 75 分位 q75 的插值，过高连续降分（不替代硬闸，仅平滑贡献）。
        # ------------------------------------------------------------------
        pe_score = 0.0
        if pe > 0:
            q75 = _p1_scalar_finite(ind_stats.get("q75", 30.0), 30.0)
            q75 = max(q75, 0.01)
            xp_pe = [0.0, q75 * 0.8, q75 * 1.2, q75 * 1.8, q75 * 2.5]
            yp_pe = np.array([4.0, 4.0, 3.0, 1.0, 0.0], dtype=float) * (3.0 / 4.0)
            pe_score = float(np.interp(pe, xp_pe, list(yp_pe)))

        # ------------------------------------------------------------------
        # ⑩ 高位熔断：bias20 过高视为透支；改为渐进式惩罚，避免与「黄金起爆」逻辑相悖。
        # 【优化2】bias20 8~10% 扣 4 分，> 10% 才扣 12 分；高位+缩量才算真正泡沫。
        # ------------------------------------------------------------------
        # 计算量价配合度：高位时需要放量确认，否则视为虚火
        vol_ratio_for_melt = _p1_scalar_finite(rt_d.get("vol_ratio", curr.get("vol_ratio", 1.0)), 1.0)
        if not np.isfinite(vol_ratio_for_melt) or vol_ratio_for_melt <= 0:
            vol_ratio_for_melt = 1.0
        
        # 渐进式惩罚表
        xp_melt = [0.0, 5.0, 8.0, 10.0, 12.0, 15.0]
        yp_melt = [3.0, 3.0, 2.0, 0.0, -4.0, -8.0]
        melt_interp = float(np.interp(max(bias20, 0.0), xp_melt, yp_melt))
        melt_interp = float(np.nan_to_num(melt_interp, nan=0.0, posinf=0.0, neginf=0.0))
        melt_score = round(max(melt_interp, 0.0), 2)  # melt_score 展示用正值
        melt_bubble_penalty = round(min(melt_interp, 0.0), 2)  # 负值才是真正的扣分
        
        # 【增强】高位+缩量 = 真正泡沫，加倍惩罚
        if bias20 > 8.0 and vol_ratio_for_melt < 1.5:
            melt_bubble_penalty = round(melt_bubble_penalty * 1.5, 2)  # 额外50%惩罚
            melt_score = 0.0  # 展示时仍为0

        # ------------------------------------------------------------------
        # ⑪ 市值与行业加分（附加，与历史口径一致写在明细里）
        # ------------------------------------------------------------------
        ind_bonus = float(dynamic_industries.get(ind, 0.0)) if isinstance(dynamic_industries, dict) else 0.0
        rank_bonus = 5.0 if ind_rank <= 3 else (2.0 if ind_rank <= 8 else 0.0)
        sector_trend_bonus_map = dynamic_industries.get("__sector_trend_bonus__", {}) if isinstance(dynamic_industries, dict) else {}
        sector_trend_context_map = dynamic_industries.get("__sector_trend_context__", {}) if isinstance(dynamic_industries, dict) else {}
        sector_trend_bonus = float(sector_trend_bonus_map.get(ind, 0.0)) if isinstance(sector_trend_bonus_map, dict) else 0.0
        sector_trend_ctx = sector_trend_context_map.get(ind, {}) if isinstance(sector_trend_context_map, dict) else {}
        mv_bonus = 0.0
        if 500.0 <= circ_mv_yi < 1000.0:
            mv_bonus = 2.0
        elif 1000.0 <= circ_mv_yi < 2000.0:
            mv_bonus = 3.0
        elif circ_mv_yi >= 2000.0:
            mv_bonus = 4.0

        if circ_mv_yi >= 2000.0:
            mv_tier = "🦍巨无霸(2000亿+)"
        elif circ_mv_yi >= 1000.0:
            mv_tier = "🐘+千亿中军(1000-2000亿)"
        elif circ_mv_yi >= 500.0:
            mv_tier = "🐘500亿+"
        else:
            mv_tier = f"🐎{int(p1_min_yi)}-500亿"

        # 第 2 步：多维分项与附加项全部写入 details（先于 base_score / 记忆融合）
        score_details: Dict[str, Any] = {
            "筹码真空": round(chip_score, 2),
            "趋势距离": round(trend_dist_score, 2),
            "均线成熟": round(mat_score, 2),
            "资金攻击": round(fund_score, 2),
            "启动势能": round(momentum_score, 2),
            "假突破惩罚": float(fake_penalty_val),
            "波段涨幅": round(gain_score, 2),
            "黄金起爆": round(golden_score, 2),
            "趋势健康": round(trend_health_score, 2),
            "主升斜率": round(slope_score, 2),
            "动态PE": round(pe_score, 2),
            "高位熔断": round(melt_score, 2),
            "行业动态加分": round(float(ind_bonus), 2),
            "板块排名加分": rank_bonus,
            "板块趋势加分": round(float(sector_trend_bonus), 2),
            "市值优待": round(mv_bonus, 2),
            "市值等级": mv_tier,
        }

        # =====================================================================
        # 【V26.88 新增】四大增强因子计算
        # =====================================================================

        # 【优化3：业绩预告/快报因子】
        forecast_type_val = _p1_scalar_finite(rt_d.get("forecast_type", curr.get("forecast_type", np.nan)), np.nan)
        forecast_bonus = _calc_forecast_bonus_score(forecast_type_val)
        score_details["业绩预告加分"] = round(forecast_bonus, 2)

        # 【优化4：筹码集中度因子】- 近2期股东户数变化趋势
        chip_concentration_bonus = _calc_chip_concentration_bonus(df)
        score_details["筹码集中加分"] = round(chip_concentration_bonus, 2)

        # 【优化8：行业轮动动态权重】
        # 【V26.88+9-27 修复】sector_rank_map 从 dynamic_industries["__sector_rank_map__"] 获取，不再用 dir() 运行时检测
        _sector_rank = dynamic_industries.get("__sector_rank_map__", {}) if isinstance(dynamic_industries, dict) else {}
        sector_momentum_bonus = _calc_sector_momentum_bonus(ind, df, _sector_rank, dynamic_industries)
        score_details["行业动量加分"] = round(sector_momentum_bonus, 2)

        # 【优化10：量价背离提前预警】
        divergence_penalty = _calc_price_volume_divergence_penalty(df, lookback_days=10)
        score_details["量价背离惩罚"] = round(divergence_penalty, 2)

        # 【V26.88 新增】板块内RS排序：个股相对板块的超额收益
        excess_return_score = _calc_excess_return_score(df)
        score_details["板块内RS排序"] = round(excess_return_score, 2)

        # =====================================================================
        # 【P1·底仓升级 V2】周线维度计算（互联网实战共识）
        # 与日线维度正交：从周时间尺度过滤噪音，识别趋势真伪 + 量价陷阱
        # =====================================================================
        weekly_cfg_root = get_p1_weekly_config()
        weekly_enabled = bool(weekly_cfg_root.get("enabled", True))
        weekly_trend_score: float = 0.0
        weekly_volume_score: float = 0.0
        weekly_combo_bonus: float = 0.0
        weekly_resonance: bool = False
        if weekly_enabled:
            try:
                _ts_code = str(rt_d.get("ts_code", "") if isinstance(rt_d, dict) else "").strip()
                if not _ts_code and df is not None and not df.empty and "ts_code" in df.columns:
                    _ts_code = str(df["ts_code"].iloc[-1] or "").strip()
                if _ts_code:
                    _wf = _fetch_weekly_row(_ts_code)
                    weekly_trend_score = _calc_weekly_trend_score(
                        _wf, weekly_cfg_root.get("trend", {}), weekly_cfg_root.get("safety", {})
                    )
                    weekly_volume_score = _calc_weekly_volume_score(
                        _wf, weekly_cfg_root.get("volume", {})
                    )
                    weekly_combo_bonus = _calc_weekly_combo_bonus(
                        _wf, weekly_cfg_root.get("combo_bonus", {})
                    )
            except Exception as _we:
                logger.debug("P1 周线维度计算异常: %s", _we)
                weekly_trend_score = 0.0
                weekly_volume_score = 0.0
                weekly_combo_bonus = 0.0
        score_details["周线趋势"] = round(weekly_trend_score, 2)
        score_details["周线放量"] = round(weekly_volume_score, 2)
        if weekly_combo_bonus != 0.0:
            score_details["周线量价组合"] = round(weekly_combo_bonus, 2)

        # 第 3 步：base_score 与组合特征乘数、股性记忆凸组合 → final_p1_score
        combo_cfg = get_p1_combo_multiplier_config()
        if not combo_cfg.get("enabled", True):
            combo_cfg = {
                "min": 1.0,
                "max": 1.0,
                "top_tier": 1.0,
                "second_tier": 1.0,
                "third_tier": 1.0,
                "industry_dark_flow": 1.0,
                "fund_shape_wakeup": 1.0,
                "fund_pulse_confirm": 1.0,
                "weak_pattern_discount": 1.0,
                "bubble_discount": 1.0,
                "hot_low_fund_discount": 1.0,
                "chip_core_min": 9e9,
                "trend_dist_min": 9e9,
                "mat_min": 9e9,
                "fund_core_min": 9e9,
                "momentum_core_min": 9e9,
                "golden_core_min": 9e9,
                "healthy_core_min": 9e9,
                "slope_core_min": 9e9,
                "fund_hot_min": 9e9,
                "chip_hot_min": 9e9,
                "trend_health_min": 9e9,
                "industry_bonus_min": 9e9,
                "rank_bonus_min": 9e9,
            }
        if sector_trend_ctx:
            _breakout_flag = bool(sector_trend_ctx.get("rank_breakout_20", False))
            _persist_flag = bool(sector_trend_ctx.get("top8_persist", False) or sector_trend_ctx.get("top12_persist", False))
            _trend_flag = bool(sector_trend_ctx.get("trend_improving", False))
            score_details["板块趋势标签"] = "、".join(
                [
                    x for x, ok in (
                        ("强度新高", _breakout_flag),
                        ("前排持续", _persist_flag),
                        ("趋势改善", _trend_flag),
                    ) if ok
                ]
            ) or "--"
            score_details["板块近3日均排"] = round(float(sector_trend_ctx.get("recent_rank_mean_3", 999.0)), 2)
            score_details["板块近5日均排"] = round(float(sector_trend_ctx.get("recent_rank_mean_5", 999.0)), 2)

        base_score = float(
            round(chip_score, 2) * 1.10
            + round(trend_dist_score, 2) * 1.15
            + round(mat_score, 2) * 1.10
            + round(fund_score, 2) * 1.05
            + float(momentum_score) * 0.85
            + round(gain_score, 2) * 0.70
            + float(golden_score) * 0.80
            + round(trend_health_score, 2) * 1.10
            + round(slope_score, 2) * 1.05
            + round(pe_score, 2) * 0.80
            + float(melt_score)
            + float(melt_bubble_penalty)
            + float(fake_penalty_val)
            + float(ind_bonus) * 1.00
            + float(rank_bonus) * 1.00
            + float(sector_trend_bonus) * 1.00
            + float(mv_bonus) * 1.00
            + float(forecast_bonus) * 1.00        # 【优化3：业绩预告加分】
            + float(chip_concentration_bonus) * 1.00  # 【优化4：筹码集中度加分】
            + float(sector_momentum_bonus) * 1.00    # 【优化8：行业动量加分】
            + float(divergence_penalty) * 1.00       # 【优化10：量价背离惩罚】
            + float(excess_return_score) * 1.00       # 【V26.88新增：板块内RS排序】
            + float(weekly_trend_score) * float(weekly_cfg_root.get("trend", {}).get("agg_weight", 0.6))   # 【P1·升级V2】周线趋势
            + float(weekly_volume_score) * float(weekly_cfg_root.get("volume", {}).get("agg_weight", 0.5))  # 【P1·升级V2】周线放量
            + float(weekly_combo_bonus) * 1.0                                                          # 【P1·升级V2】量价组合加成
        )
        base_score = float(np.nan_to_num(base_score, nan=0.0, posinf=100.0, neginf=0.0))

        # 组合特征乘数：只奖励跨维度共振，不奖励单项极值。
        # 目标是把“横盘蓄势 + 筹码唤醒 + 资金真流入 + 趋势抬头”这种难伪造的底仓结构放大。
        combo_multiplier = 1.0
        combo_tags = []

        # ------------------------------------------------------------------
        # 【优化4】组合乘数判断：重构为互斥 if-elif 链，避免重复计算和条件重叠
        # 优先级从高到低：
        # 1. 顶级底仓共振（最高优先级）
        # 2. 硬科技T0共振
        # 3. 次级筹码唤醒+资金修复
        # 4. 趋势动能共振
        # 5. 行业暗流建仓
        # ------------------------------------------------------------------
        combo_multiplier = 1.0
        combo_tags = []

        chip_core_min = float(combo_cfg.get("chip_core_min", 8.8))
        trend_dist_min = float(combo_cfg.get("trend_dist_min", 7.0))
        mat_min = float(combo_cfg.get("mat_min", 5.0))
        fund_core_min = float(combo_cfg.get("fund_core_min", 8.5))
        momentum_core_min = float(combo_cfg.get("momentum_core_min", 5.8))
        golden_core_min = float(combo_cfg.get("golden_core_min", 7.5))
        healthy_core_min = float(combo_cfg.get("healthy_core_min", 2.6))
        slope_core_min = float(combo_cfg.get("slope_core_min", 4.5))
        fund_hot_min = float(combo_cfg.get("fund_hot_min", 11.5))
        chip_hot_min = float(combo_cfg.get("chip_hot_min", 6.4))
        trend_health_min = float(combo_cfg.get("trend_health_min", 2.2))
        industry_bonus_min = float(combo_cfg.get("industry_bonus_min", 8.0))
        rank_bonus_min = float(combo_cfg.get("rank_bonus_min", 2.0))

        chip_core = chip_score >= chip_core_min
        trend_core = trend_dist_score >= trend_dist_min and mat_score >= mat_min
        fund_core = fund_score >= fund_core_min
        momentum_core = momentum_score >= momentum_core_min or golden_score >= golden_core_min
        healthy_core = trend_health_score >= healthy_core_min and slope_score >= slope_core_min

        # 【F2-3】顶级组合软化。要"四件齐"门槛高，可改为「四件中三件齐且资金形状 ≥ 4」。
        # 实测多数深度满足规则4（趋势+动能+健康）的票被锁在外面无法触顶。
        # 注意：必须保留资金维度（fund_core 或 资金形状），避免纯趋势票混入顶级。
        # 【F3-3 修复 2026-10-04】此处原写成 `float(amnesty_industry_bonus or 0.0)`，但该名字
        # 在本函数作用域内从未被赋值（只是 single_stock 层的特赦文案变量），必然抛
        # NameError → 被外层 except 吞成 "P1多维分项打分异常"，score 回退 0.0，
        # 于是所有活过前置硬闸的票（实测 100/1109）全部 0 分出局 → P1 主池 0 只。
        # 正确取「资金形状」= 因子1 连续净流入分 flow_score（满分 9，见上方 flow_score 封顶）。
        _fund_shape_strong = float(flow_score) >= 4.0
        _top3 = sum(int(x) for x in [chip_core, trend_core, fund_core, healthy_core]) >= 3 and (fund_core or _fund_shape_strong)

        # 2026 慢牛加成：战略核心赛道（电子/计算机/通信T0）
        is_strategic_t0 = ind in ("电子", "计算机", "通信")

        # 1. 顶级底仓组合：筹码密集 + 趋势成熟 + 资金攻击 + 形态健康（最高优先级）
        # 【F2-3】改为「任三项 + 资金维度在场」即可触发顶级乘数（1.20）。
        if _top3:
            combo_multiplier = float(combo_cfg.get("top_tier", 1.20))
            combo_tags.append("顶级底仓共振")
        # 2. 硬科技T0共振：战略核心赛道（电子/计算机/通信T0）+ 资金+动能共振
        elif is_strategic_t0 and fund_core and momentum_core:
            combo_multiplier = max(combo_multiplier, float(combo_cfg.get("top_tier", 1.20)) * 1.05)
            combo_tags.append("硬科技共振")
        # 3. 次一级组合：筹码唤醒 + 资金连续修复 + 启动势能成型
        elif chip_core and fund_core and momentum_core:
            combo_multiplier = float(combo_cfg.get("second_tier", 1.15))
            combo_tags.append("筹码唤醒+资金修复")
        # 4. 右侧加速但不极端：趋势、动能、健康同时成立
        elif trend_core and momentum_core and healthy_core:
            combo_multiplier = float(combo_cfg.get("third_tier", 1.12))
            combo_tags.append("趋势动能共振")
        # 5. 行业暗流：板块热度不是最热，但排名与行业加分同时在场，轻度放大
        elif ind_bonus >= industry_bonus_min and rank_bonus >= rank_bonus_min and fund_core:
            combo_multiplier = float(combo_cfg.get("industry_dark_flow", 1.08))
            combo_tags.append("行业暗流建仓")

        # 资金形状的额外奖励：只在“连续净流入 + 价格推升效率足够”时才加成
        if fund_score >= fund_hot_min and chip_score >= chip_hot_min and trend_health_score >= trend_health_min:
            combo_multiplier = max(combo_multiplier, float(combo_cfg.get("fund_shape_wakeup", 1.18)))
            combo_tags.append("资金形状唤醒")
        elif fund_score >= 9.0 and momentum_score >= 5.0 and chip_score >= 5.0:
            combo_multiplier = max(combo_multiplier, float(combo_cfg.get("fund_pulse_confirm", 1.10)))
            combo_tags.append("资金脉冲确认")

        # 负面股性惩罚：长上影、假突破、虚火过热不奖励，必要时轻微折价。
        anti_pattern_penalty = 1.0
        if fake_penalty_val < 0:
            anti_pattern_penalty *= float(combo_cfg.get("weak_pattern_discount", 0.94))
        if melt_bubble_penalty < 0:
            anti_pattern_penalty *= float(combo_cfg.get("bubble_discount", 0.92))
        if pe_score <= 0.5 and momentum_score >= 7.5 and fund_score < 6.0:
            anti_pattern_penalty *= float(combo_cfg.get("hot_low_fund_discount", 0.95))
            combo_tags.append("高动量低资金折价")

        # 【优化10增强】量价背离惩罚：直接折价乘数
        if divergence_penalty < -1.5:
            anti_pattern_penalty *= 0.94
            combo_tags.append("量价背离预警")

        # 【新增】业绩预增共振奖励：业绩好 + 资金强 + 趋势好 = 底仓黄金组合
        if forecast_bonus >= 2.0 and fund_score >= 10.0 and trend_dist_score >= 8.0:
            combo_multiplier = max(combo_multiplier, 1.10)
            combo_tags.append("业绩预增共振")

        # 【新增】筹码集中共振：筹码集中 + 趋势健康 = 机构控盘信号
        if chip_concentration_bonus >= 2.0 and trend_health_score >= 3.0:
            combo_multiplier = max(combo_multiplier, 1.08)
            combo_tags.append("筹码集中共振")

        # ------------------------------------------------------------------
        # 【P1·底仓升级 V2】周线双强共振：周线趋势 + 周线放量同向强势
        # 仅当 weekly_enabled（默认 True）；阈值由 get_p1_weekly_config().resonance 控制
        # ------------------------------------------------------------------
        if weekly_enabled:
            _res = weekly_cfg_root.get("resonance", {})
            _trend_min = float(_res.get("trend_min", 4.5))
            _volume_min = float(_res.get("volume_min", 2.5))
            if weekly_trend_score >= _trend_min and weekly_volume_score >= _volume_min:
                weekly_resonance = True
                combo_multiplier = max(combo_multiplier, float(_res.get("multiplier", 1.05)))
                combo_tags.append("周线双强共振")

        combo_multiplier = float(np.clip(combo_multiplier * anti_pattern_penalty, float(combo_cfg.get("min", 0.90)), float(combo_cfg.get("max", 1.20))))

        w_fm = float(get_p1_fund_memory_weight())
        fm100 = 0.0
        fm_raw = _p1_scalar_finite(curr.get("fund_memory_score", 0.0), 0.0)
        amplified_base = round(base_score * combo_multiplier, 2)
        if w_fm > 1e-9:
            fm100 = float(np.clip(fm_raw / 200.0 * 100.0, 0.0, 100.0))
            final_p1_score = round((1.0 - w_fm) * amplified_base + w_fm * fm100, 2)
        else:
            final_p1_score = round(amplified_base, 2)

        final_p1_score = float(min(final_p1_score, 100.0))

        if w_fm > 1e-9:
            koujing = f"主线稳票优先+股性记忆凸组合(权重={w_fm:.2f})"
        else:
            koujing = "主线稳票优先(权重=0，未融合股性记忆)"

        score_details["融合前分项合计"] = round(base_score, 2)
        score_details["组合特征乘数"] = round(combo_multiplier, 3)
        if combo_tags:
            score_details["组合特征标签"] = "、".join(combo_tags[:4])
        score_details["乘数后基底分"] = round(amplified_base, 2)
        score_details["评分口径"] = koujing + "；乘数只奖励组合特征，不奖励单项极值"
        if w_fm > 1e-9:
            score_details["股性记忆(0-100)"] = round(fm100, 2)
            score_details["记忆融合权重"] = round(w_fm, 4)

        # 第 4 步（函数末尾）：据写满的 details + YAML pass_line 计算 effective_pass_line，再判定 passed
        try:
            _pl_raw = float(pass_line)
        except (TypeError, ValueError):
            _pl_raw = 50.0
        base_pass_line = _pl_raw if _pl_raw else 50.0
        if not np.isfinite(base_pass_line) or base_pass_line <= 0:
            base_pass_line = 50.0

        # 2026 慢牛共振容错：战略核心赛道（电子/计算机/通信T0）在资金+动能双强时允许 -4
        # 机构主导市场中，硬科技龙头若趋势健康不应被严苛门槛拦截
        score_momentum = _p1_scalar_finite(score_details.get("启动势能", 0.0), 0.0)
        score_capital = _p1_scalar_finite(score_details.get("资金攻击", 0.0), 0.0)
        fake_penalty = _p1_scalar_finite(score_details.get("假突破惩罚", 0.0), 0.0)
        ind_bonus_score = _p1_scalar_finite(score_details.get("行业动态加分", 0.0), 0.0)
        if (
            score_momentum >= 8.0
            and score_capital >= 10.0
            and fake_penalty >= -2.0
            and ma20 >= ma60 * 0.98
            and ind_bonus_score >= 12.0  # 必须是战略核心赛道才触发共振容错
        ):
            effective_pass_line = base_pass_line - 4.0
        else:
            effective_pass_line = base_pass_line

        score_details["基础及格线"] = round(base_pass_line, 2)
        score_details["有效及格线"] = round(effective_pass_line, 2)

        if final_p1_score < effective_pass_line:
            score_details["未达标比对"] = (
                f"{final_p1_score:.2f} < {effective_pass_line:.2f}（基准线 {base_pass_line:.2f}）"
            )
            return (
                final_p1_score,
                False,
                "平滑得分不达标",
                score_details,
            )
        return final_p1_score, True, "入选", score_details

    except Exception as e:
        logger.debug("P1多维分项打分异常: %s", e)
        return (
            0.0,
            False,
            f"P1多维分项打分异常: {e}",
            {},
        )


# 历史函数名别名（外部脚本 / 旧笔记中的 import 仍可用；行为等同于 compute_p1_multi_dim_smooth_score）
compute_p1_eleven_dim_smooth_score = compute_p1_multi_dim_smooth_score


# ========== 【V26.88+9-27 修复O3】金共振机构识别函数 ==========
# 原 strat_golden_10.GoldenTenStrategies 已在 V26.88 标记 DEPRECATED，
# 旧代码引用 compute_golden_institutional_bonus()，本文件原计划实现却遗漏，
# 导致 core/backtest_runner.py 与 core/scan/utils.py 启动期 ImportError warning。
# 现补齐：基于净流入 / 港资 / 胜率 / 市值门槛，返回 (hits, burst_score, bonus_score)。
def compute_golden_institutional_bonus(
    df=None,
    rt=None,
    pool_key: str = "p4",
    ind_rank: int = 999,
    net_main_amount: float = 0.0,
    cyq: float = 999.0,
    # 兼容旧式具名调用（UPGRADE_LOG.md 第 124-132 行的迁移示例）
    circ_mv_yi: float | None = None,
    net_main_wan: float | None = None,
    pct_chg_5d_max: float | None = None,
):
    """
    金共振机构识别（V26.79 迁移版实现，V26.88+9-27 补齐）。

    Parameters
    ----------
    df : pd.DataFrame | None
        K 线子集（含 vol/pct_chg 等列）。None 时仅基于标量参数打分。
    rt : dict | None
        实时快照（含 price/open/high/volume/vol_ratio 等）。None 时视为未知。
    pool_key : str
        池位 (p2/p3/p4/p5)，影响爆发力档位。
    ind_rank : int
        行业排名（1 表示行业最强）；999 = 行业未名。
    net_main_amount / net_main_wan : float
        主力净流入（万元口径），正为流入。
    cyq : float
        筹码集中度（90% 成本占比，0~100），越小越集中。
    circ_mv_yi : float | None
        流通市值（亿元）。None 时用默认 500 亿门槛。
    pct_chg_5d_max : float | None
        5 日最大涨幅（%），用于右侧交易过滤。

    Returns
    -------
    tuple[list[str], float, float]
        (gold_tags, burst_score, bonus_score) — 与旧 GoldenTenStrategies.evaluate
        兼容的三元组。
    """
    try:
        tags: list[str] = []
        burst: float = 0.0
        bonus: float = 0.0

        # 兼容：把 .NET 风格具名参数映射到统一单位（万元）
        net = float(net_main_wan) if net_main_wan is not None else float(net_main_amount)
        circ = float(circ_mv_yi) if circ_mv_yi is not None else 500.0
        pct5 = float(pct_chg_5d_max) if pct_chg_5d_max is not None else 5.0

        # 1) 主力净流入确认（≥3000 万算"主力流入"）
        if net >= 3000.0:
            tags.append("主力净流入")
            bonus += 1.5
        elif net >= 1500.0:
            bonus += 0.8

        # 2) 筹码集中度（≤20% 算高集中）
        if 0 < cyq <= 20.0:
            tags.append("筹码集中")
            bonus += 1.0
        elif 0 < cyq <= 35.0:
            bonus += 0.4

        # 3) 行业地位（rank ≤ 30 算行业前列）
        if 0 < ind_rank <= 30:
            tags.append("行业强度")
            bonus += 0.8

        # 4) 爆发力 burst：按池位与涨幅给出 0~3.5 分
        try:
            pct_now = float((rt or {}).get("pct_chg", 0.0))
        except Exception:
            pct_now = 0.0
        if pool_key in ("p3", "p4"):
            if pct_now >= 7.0:
                burst = 3.5
            elif pct_now >= 5.0:
                burst = 2.8
            elif pct_now >= 3.0:
                burst = 1.8
            else:
                burst = max(0.0, pct_now * 0.3)
        else:  # p2 / p5 / 其它
            burst = max(0.0, pct_now * 0.4)

        # 5) 右侧交易过滤：5 日最大涨幅不能超 9%（防追高）
        if pct5 > 9.0:
            bonus *= 0.5
            tags.append("右侧过滤")

        # 6) 流通市值下限（≥50 亿）：过小盘扣分
        if circ < 50.0:
            bonus *= 0.6

        return tags, float(burst), float(bonus)
    except Exception as e:
        # 任何异常都返回中性结果，绝不传播给上层策略
        logging.getLogger(__name__).debug("compute_golden_institutional_bonus 内部异常: %s", e)
        return [], 0.0, 0.0


# ========== 【V26.88+9-22 已移除】Golden10 核心逻辑 ==========
# 原 compute_golden_institutional_bonus() 及 golden_tier_elg_hk_punish_wan_local()
# 已从系统中移除。原因：
#   - 该函数自 V26.79 引入后从未被任何生产代码调用
#   - scan_engine.py 当前使用旧版 GoldenTenStrategies (strat_golden_10.py)
# 如需重新启用，请参考 UPGRADE_LOG.md 历史记录或联系作者。
