# -*- coding: utf-8 -*-
"""
core/paper_trading/strategy_labels.py — 战法编号 → 通俗中文说明

【职责】
把推送里的 "P3-08" 这类战法编号翻译成 20 字以内的中文白话说明，
让不熟悉内部术语的人也能看懂「为什么买这只票」。

【数据来源】
战法中文名取自各战法模块内的命中标签（形如 "P3-08·★倍量启动延续"），
说明文本在此集中维护，便于校对与统一口径。

【设计原则】
  · 说明 ≤ 20 个中文字符，超长自动截断
  · 未登记的编号安全回落（显示原编号，不报错、不中断推送）
  · 纯函数、无副作用、无外部依赖
"""
from __future__ import annotations

from typing import Dict, List, Optional

# 战法编号 → 20字内通俗说明
# 【V35 编号重整】全部战法重排为连续编号；旧编号对照见各 screener 模块头映射表。
STRATEGY_LABELS: Dict[str, str] = {
    # ===== P3 盘中(实时) =====
    "P3-01": "右侧放量起爆，追强势突破",
    "P3-02": "回踩20日均线不破，低吸",
    "P3-03": "单日放量跃迁，股价跳升",
    "P3-04": "主力连续放量发力，上攻",
    "P3-05": "横盘平台整理后二次突破",
    "P3-06": "主力资金逆势买入托底",
    "P3-07": "昨日倍量阳线，今日延续涨",
    "P3-08": "低估值低换手，趋势底仓",
    "P3-09": "缩量分歧回调，逢低介入",
    "P3-10": "均线粘合后放量突破",
    "P3-11": "RSI超跌后企稳，观察低吸",
    # ===== P4 尾盘 =====
    "P4-01": "光头阳线收盘，尾盘抢筹",
    "P4-02": "筹码锁定，股价创新高",
    "P4-03": "缩量回踩均线，低吸机会",
    "P4-04": "强势洗盘后获得承接支撑",
    "P4-05": "动能与突破共振，趋势强",
    "P4-06": "底仓趋势延续，持股待涨",
    "P4-07": "连板后缩量整理，随时反包",
    "P4-08": "5日线上穿20日线，趋势转多",
    # ===== P5 盘后 =====
    "P5-01": "量价趋势方向向上",
    "P5-02": "成交量明显放大",
    "P5-03": "量价信号确认有效",
    "P5-04": "主力资金流向正确",
    "P5-05": "资金质量良好",
    "P5-06": "资金信号确认",
    "P5-07": "趋势结构完整向上",
    "P5-08": "趋势质量优良",
    "P5-09": "趋势信号确认",
    "P5-10": "核心四项指标同时共振",
    "P5-11": "筹码单峰密集，向上突破",
    "P5-12": "机构龙虎榜后缩量回踩",
    "P5-13": "中军股放量点火带节奏",
    "P5-14": "欧奈尔趋势模板全满足",
    "P5-15": "股价突破箱体上沿",
    "P5-16": "箱体突破后缩量回踩",
    "P5-17": "均线粘合横盘待变",
    "P5-18": "均线开始发散，起点",
    "P5-19": "均线发散后确认向上",
    "P5-20": "缩量分歧后回调低吸",
    "P5-21": "高位旗形整理，蓄势待涨",
    "P5-22": "5日线上穿20日线金叉",
    "P5-23": "突破布林上轨，动能转强",
    "P5-24": "放量创20日新高",
    "P5-25": "MACD金叉确认上攻",
    # ===== 兜底：未在上表登记的编号 =====
    "_default": "策略信号触发，具体逻辑待补充",
}


# 退役/已删除战法集合
# 【V35】编号重整后所有战法编号均为连续合法编号（旧退役编号已全部重新分配），
# 退役集合清空；若未来再退役战法，在此登记。
RETIRED_STRATEGIES: set = set()

MAX_LABEL_LEN = 20


def _truncate(text: str) -> str:
    """限制说明长度在 20 字以内（含标点）。"""
    s = str(text or "").strip()
    if len(s) <= MAX_LABEL_LEN:
        return s
    return s[:MAX_LABEL_LEN]


def strategy_label(code: str) -> str:
    """单个战法编号 → 通俗说明。

    未登记编号回落为通用话术（而非空串），保证推送里不会出现光秃秃的编号，
    让人无从判断这是什么问题；已退役战法单独标记。
    """
    key = str(code or "").strip().upper()
    if not key:
        return ""
    # 去掉可能附带的 ★ 或前后缀说明，只取编号
    if "·" in key:
        key = key.split("·", 1)[0].strip().upper()
    hit = STRATEGY_LABELS.get(key)
    if hit:
        return _truncate(hit)
    if key in RETIRED_STRATEGIES:
        return "历史战法(已退役)"
    return _truncate(STRATEGY_LABELS.get("_default", "策略信号触发"))


def strategy_labels(codes: List[str]) -> List[str]:
    """批量翻译，返回与输入等长的说明列表（保持顺序对应）。"""
    out: List[str] = []
    for c in (codes or []):
        out.append(strategy_label(c))
    return out


def format_strategy_line(codes: List[str], max_items: int = 3) -> str:
    """生成推送里「战法」那一行文本。

    格式： P3-07 倍量启动延续昨日阳线今延续 / P5-19 均线发散确认向上
    · 最多展示 max_items 个，超出用「等N项」收尾
    · 最多 3 条，每条说明 ≤20 字，整行控制在可读长度内
    """
    codes = [str(c or "").strip() for c in (codes or []) if str(c or "").strip()]
    if not codes:
        return ""
    parts: List[str] = []
    for c in codes[:max_items]:
        lab = strategy_label(c)
        if lab:
            parts.append(f"{c} {lab}")
    if not parts:
        return ""
    extra = len(codes) - len(parts)
    line = " / ".join(parts)
    if extra > 0:
        line += f" 等{len(codes)}项"
    return line


__all__ = [
    "STRATEGY_LABELS",
    "RETIRED_STRATEGIES",
    "MAX_LABEL_LEN",
    "strategy_label",
    "strategy_labels",
    "format_strategy_line",
]
