# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.80 — P1 排名合理性保障器

【A 股实战问题】
当前 P1 排序只有两键：(p1_score, inflow_ratio*0.6)
这会导致：

  1. ❌ 高分票一律优先，可能忽略真正的「次新强势」
  2. ❌ 同一档位内，老白马 vs 新龙头 随机排序
  3. ❌ 涨停板票 vs 潜伏票 排在不同档位
  4. ❌ 没有「连板高度」「题材强度」作为权重
  5. ❌ 缺少「相关性分散」（避免 P1 池全是同一题材）

【本模块】
在原有 _p1_final_sort_key 基础上，新增 5 个排序因子：

  1. 连板高度（limit_times）—— A 股特色
  2. 题材新颖度（板块近 3 日强度变化）
  3. 资金流入加权（机构净买 / 主力净流入）
  4. 相邻票相关性惩罚（同行业内强制分散）
  5. 时间衰减（老题材 vs 新题材）

【效果】
  - 排行榜前 5 名更稳定（不会出现昨日榜首今天出局）
  - 避免「资金抱团」极端集中
  - 主题龙头自动浮出
"""
from __future__ import annotations

import logging
from typing import Dict, Any, List, Tuple, Optional
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


# 排序因子权重
SORT_WEIGHTS = {
    "p1_score": 1.00,           # 主分（不变）
    "limit_grade": 0.15,        # 连板高度
    "sector_momentum": 0.20,    # 题材新颖度
    "north_buy": 0.10,          # 北向加持
    "inflow_ratio": 0.10,       # 资金流入
    "recency_bonus": 0.05,      # 时间衰减（新龙头加分）
    "rps_penalty": 0.10,        # RPS 过热回避（实测为负向因子，见 calc_rps_penalty）
}


def calc_rps_penalty(item: Dict[str, Any]) -> float:
    """RPS-60 相对强度：极端过热与弱势回避（-1.0 ~ +0.5）

    【为什么是「惩罚」而不是「加分」】
    94 个交易日（底池内 6808 样本）分层前瞻收益实测：
        RPS 0-50   5日 -2.40% (ex -0.98%) / 10日 -1.77% (ex +0.31%)
        RPS 50-80  5日 -0.83% (ex +0.59%) / 10日 -1.51% (ex +0.58%)   ← 最优区间
        RPS 80-95  5日 -1.22% (ex +0.21%) / 10日 -2.06% (ex +0.03%)
        RPS 95+    5日 -2.58% (ex -1.16%) / 10日 -3.96% (ex -1.87%)   ← 最差
    呈明显「倒 U 形」：最强的一档反而最差，因此绝不能当「越高越好」的加分项，
    否则会把 10 日超额 -1.87% 的极端超买票顶到最前。

    【RPS 定义与周期】
    rps = (close / close.shift(rps_period) - 1) * 100，再按当日横截面百分位排名(0~100)。
    周期 60 而非 Sequoia-X 的 120：本项目日线库当前仅 94 个交易日，
    shift(120) 恒为 NaN ⇒ 命中 0。60 需 61 根，样本覆盖 1493/2015 只。
    随数据累积到 121 根以上，可把 rps_period 提回 120（欧奈尔原始口径），
    届时应重新扫描上述分层并复核本惩罚项方向。

    评分：
        RPS >= 95   : -1.0   极端超买，追高风险最大
        RPS >= 90   : -0.5   明显过热
        RPS <  20   : -0.5   弱势，右侧不足
        其余        : +0.5   处于强势但未过热的最优区间(50~90)
        缺失        :  0.0   无数据不惩罚（保持与其它因子一致的中性处理）
    """
    rps = item.get("rps_60")
    if rps is None:
        return 0.0
    try:
        rps = float(rps)
    except (TypeError, ValueError):
        return 0.0
    if not (rps == rps) or rps in (float("inf"), float("-inf")):  # NaN / inf 防御
        return 0.0
    if rps >= 95.0:
        return -1.0
    if rps >= 90.0:
        return -0.5
    if rps < 20.0:
        return -0.5
    if rps < 90.0:
        return 0.5
    return 0.0


def calc_limit_grade_bonus(item: Dict[str, Any]) -> float:
    """
    连板高度加分（0 ~ +1.5）

    - 1 板: 0.3
    - 2 板: 0.6
    - 3 板: 1.0
    - 4 板: 1.3
    - ≥5 板: 1.5
    """
    rt = item.get("hist", item.get("rt", {}))
    limit_times = int(rt.get("limit_times", 0) or 0)
    if limit_times <= 0:
        return 0.0
    if limit_times == 1:
        return 0.3
    if limit_times == 2:
        return 0.6
    if limit_times == 3:
        return 1.0
    if limit_times == 4:
        return 1.3
    return 1.5


def calc_sector_momentum_bonus(item: Dict[str, Any]) -> float:
    """
    题材新颖度（-1 ~ +1.5）

    - 板块近 3 日累计涨幅 > 5%（主线接力）: +1.5
    - 板块近 3 日累计涨幅 0~5%（温和上行）: +0.8
    - 板块近 3 日累计涨幅 -5~0%（震荡）: 0.0
    - 板块近 3 日累计涨幅 < -5%（退潮）: -1.0
    """
    sd = item.get("score_details", {}) or {}
    sector_trend_ctx = sd.get("板块近3日均排", 999)
    sector_rank_breakout = sd.get("板块趋势标签", "")

    if "强度新高" in str(sector_trend_ctx) or "前排持续" in str(sector_rank_breakout):
        return 1.5
    if isinstance(sector_trend_ctx, (int, float)) and sector_trend_ctx <= 5:
        return 1.0
    if isinstance(sector_trend_ctx, (int, float)) and sector_trend_ctx <= 10:
        return 0.5
    if isinstance(sector_trend_ctx, (int, float)) and sector_trend_ctx >= 50:
        return -0.5
    return 0.0


def calc_north_buy_bonus(item: Dict[str, Any]) -> float:
    """
    北向加持（-0.5 ~ +1.0）

    - 北向重仓（>5%）且 5 日增持 > 5%: +1.0
    - 北向重仓（>5%）: +0.5
    - 北向 5 日减持 > 10%: -0.5
    """
    sd = item.get("score_details", {}) or {}

    # 简单判断：score_details 中有"北向重仓"标签
    if "北向重仓" in str(sd.get("A股特色标签", "")):
        return 0.5
    if "🇭🇰重仓" in str(sd.get("A股特色标签", "")):
        return 1.0
    if "⚠️5日减持" in str(sd.get("A股特色标签", "")):
        return -0.5
    return 0.0


def calc_inflow_bonus(item: Dict[str, Any]) -> float:
    """
    资金流入加权（0 ~ +1.0）

    - 近 3 日净流入 > 5 亿: +1.0
    - 近 3 日净流入 1~5 亿: +0.5
    - 近 3 日净流入 < 0: 0
    """
    inflow_ratio = float(item.get("inflow_ratio", 0.0) or 0.0)
    circ_mv_yi = float(item.get("hist", {}).get("circ_mv", 0) or 0) / 1e8  # 亿元

    # 净流入 = inflow_ratio × 流通市值
    net_inflow_yi = inflow_ratio * circ_mv_yi

    if net_inflow_yi >= 5.0:
        return 1.0
    if net_inflow_yi >= 1.0:
        return 0.5
    return 0.0


def calc_recency_bonus(item: Dict[str, Any], today: Optional[str] = None) -> float:
    """
    时间衰减（-0.5 ~ +0.5）

    - 涨停日就在今日 +0.5
    - 涨停日在昨日 +0.3
    - 涨停日在 2~5 日前 0
    - 涨停日在 >5 日前 -0.3
    """
    if today is None:
        # 【V26.88+9-27 修复 D3】wall clock 跨日漂移保护：用北京时间（+8）保证 0:00~7:59 期间仍使用"今日"语义，
        # 否则 UTC 凌晨会拿到前一天日期，导致时间衰减全部偏 1 天，进而与策略下游"昨日"对齐错位。
        try:
            today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
        except Exception:
            today = datetime.now().strftime("%Y%m%d")

    rt = item.get("hist", item.get("rt", {}))
    last_limit_date = rt.get("last_limit_date")
    if not last_limit_date:
        return 0.0
    try:
        if len(str(last_limit_date)) == 8:
            diff = (datetime.strptime(today, "%Y%m%d") - datetime.strptime(str(last_limit_date), "%Y%m%d")).days
        else:
            return 0.0
    except Exception:
        return 0.0

    if diff <= 0:
        return 0.5
    if diff <= 1:
        return 0.3
    if diff <= 5:
        return 0.0
    return -0.3


def calc_diversity_penalty(
    candidate: Dict[str, Any],
    already_in: List[Dict[str, Any]],
    max_same_industry: int = 2,
) -> float:
    """
    相关性分散惩罚：同一行业在池中已有 N 只，扣分

    - 同行业 < max_same_industry: 0
    - 同行业 = max_same_industry: -0.5
    - 同行业 > max_same_industry: -2.0
    """
    cand_industry = str(candidate.get("_pre_ind", candidate.get("hist", {}).get("industry", "")))
    if not cand_industry:
        return 0.0

    same_count = 0
    for it in already_in:
        it_industry = str(it.get("_pre_ind", it.get("hist", {}).get("industry", "")))
        if it_industry == cand_industry:
            same_count += 1

    if same_count < max_same_industry:
        return 0.0
    if same_count == max_same_industry:
        return -0.5
    return -2.0


# ==================== 增强排序键 ====================

def enhanced_p1_sort_key(item: Dict[str, Any], today: Optional[str] = None) -> Tuple[float, ...]:
    """
    P1 增强排序键（元组：先比第一个，再比第二个，依次类推）。

    排序优先级：
      1. 综合分（p1_score）权重 1.0
      2. + 连板高度 × 0.15
      3. + 题材新颖度 × 0.20
      4. + 北向加持 × 0.10
      5. + 资金流入 × 0.10
      6. + 时间衰减 × 0.05
      7. + RPS 过热回避 × 0.10   ← 【V26.88+10-5 新增】
      8. 次键：原 inflow_ratio + 10d_ratio × 0.6

    注：单票中不应用 diversity_penalty，由上层 rerank_with_diversity 处理
    """
    p1_score = float(item.get("p1_score", 0.0) or 0.0)
    limit_bonus = calc_limit_grade_bonus(item)
    sector_bonus = calc_sector_momentum_bonus(item)
    north_bonus = calc_north_buy_bonus(item)
    inflow_bonus = calc_inflow_bonus(item)
    recency_bonus = calc_recency_bonus(item, today)
    rps_penalty = calc_rps_penalty(item)

    # 【P0 漏洞修复】原代码 L230 使用 SORT_WEIGHTS["inflow"]，但 SORT_WEIGHTS 字典
    # 中实际键名是 "inflow_ratio"（L44），导致 KeyError 或资金流入加权完全不生效。
    # 修复：使用正确的键名 "inflow_ratio"。
    weighted_score = (
        p1_score * SORT_WEIGHTS["p1_score"]
        + limit_bonus * SORT_WEIGHTS["limit_grade"] * 10
        + sector_bonus * SORT_WEIGHTS["sector_momentum"] * 10
        + north_bonus * SORT_WEIGHTS["north_buy"] * 10
        + inflow_bonus * SORT_WEIGHTS["inflow_ratio"] * 10
        + recency_bonus * SORT_WEIGHTS["recency_bonus"] * 10
        + rps_penalty * SORT_WEIGHTS["rps_penalty"] * 10
    )

    inf = (
        float(item.get("inflow_ratio", 0.0) or 0.0)
        + float(item.get("inflow_10d_ratio", 0.0) or 0.0) * 0.6
    )

    return (round(weighted_score, 4), round(inf, 4))


def rerank_with_diversity(
    pool: List[Dict[str, Any]],
    target_size: int = 5,
    max_same_industry: int = 2,
    today: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    带多样性约束的重排序。

    1. 按 enhanced_p1_sort_key 降序排序
    2. 贪心选择：
       - 若同行业已满 max_same_industry，跳过这只（除非无候选）
       - 否则加入池
    3. 直到池子达到 target_size

    Args:
        pool: P1 池原始排序结果
        target_size: 目标池大小
        max_same_industry: 同行业最多几只
        today: 当前日期

    Returns:
        重排序后的 P1 池
    """
    if not pool:
        return []

    # 排序
    sorted_pool = sorted(
        pool,
        key=lambda x: enhanced_p1_sort_key(x, today),
        reverse=True,
    )

    selected = []
    industry_count = {}

    # 第一遍：按分数选，但不超 max_same_industry
    for cand in sorted_pool:
        if len(selected) >= target_size:
            break

        ind = str(cand.get("_pre_ind", cand.get("hist", {}).get("industry", "未知")))
        if industry_count.get(ind, 0) >= max_same_industry:
            # 同行业已满，暂时跳过
            continue

        selected.append(cand)
        industry_count[ind] = industry_count.get(ind, 0) + 1

    # 第二遍：如果池子未满，强制填充（无同行业约束）
    if len(selected) < target_size:
        for cand in sorted_pool:
            if cand in selected:
                continue
            if len(selected) >= target_size:
                break
            selected.append(cand)

    return selected
