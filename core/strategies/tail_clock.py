# -*- coding: utf-8 -*-
"""
尾盘执行时钟（P4 专用 · 单一事实来源）
================================================================================
【为什么需要这个模块】
P4 的全部语义是「14:30 之后用临近收盘的信息，做次日 T+1 的先手底仓」。
但原实现里，时间只被当成一个隐形开关（curr_min >= 870 才开某些检测），
没有任何地方回答下面这三个真正决定盈亏的问题：

  1) 现在是尾盘的哪个阶段？这个阶段「还能不能开新仓」？
  2) 这个阶段买多少？（14:30 买满 vs 14:52 还买满，是完全两种风险）
  3) 这个阶段下单怎么下？（限价/追价/分笔，滑点差 0.3% 就吃掉一半利润）

原实现的后果：
  · 14:55 之后仍在给出「可轻仓试仓」的买入指引 → 用户实际是在收盘竞价里追单，
    次日低开直接套住；这是 P4 最典型的亏损来源，而不是选股选错了。
  · 14:30–14:35 尾盘还没走出来就满仓，尾盘一变脸就是最高价接货。
  · 仓位是写死的 max_position_ratio=0.15，60 分和 95 分拿一样的钱。

本模块把尾盘切成 5 个阶段，每个阶段给出四个契约字段：
  allow_new_entry / position_mult / score_bar_delta / window_text + order_style

【阶段划分（北京时间分钟数）】
  870 = 14:30   875 = 14:35   885 = 14:45   895 = 14:55   900 = 15:00

  pre_tail  < 870          14:30 前      不可开新仓（P4 只观察）
  observe   870 ≈ 875      14:30–14:35   观察期：尾盘尚未成形，半仓试水
  golden    875 ≈ 885      14:35–14:45   黄金窗：尾盘量价已确认，主建仓
  late      885 ≈ 895      14:45–14:55   尾窗：只剩十分钟，仓减半，不追价
  freeze    895 ≈ 900      14:55–15:00   冻结期：禁止新开仓（收盘竞价不可控）
  closed    >= 900 或 < 570  已收盘/未开盘  仅供复盘，禁止新开仓

【口径红线】
  · 纯函数，不读 DB、不读 config.yaml，所有阈值由调用方以 p4 cfg 传入（便于策略实验室扫参）。
  · 任何异常 → 回退到 pre_tail 且 allow_new_entry=False（宁可不出手，不可乱出手）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

BJ_TZ = timezone(timedelta(hours=8))

PHASE_PRE_TAIL = "pre_tail"
PHASE_OBSERVE = "observe"
PHASE_GOLDEN = "golden"
PHASE_LATE = "late"
PHASE_FREEZE = "freeze"
PHASE_CLOSED = "closed"


@dataclass(frozen=True)
class TailPhase:
    """尾盘阶段契约。"""

    key: str
    label: str
    allow_new_entry: bool
    position_mult: float          # 仓位系数（0~1），最终仓位 = 基础仓位 × 各系数
    score_bar_delta: float        # 入场分数门槛增量（正数 = 该阶段要求更高）
    window_text: str              # 可读的「买入时间窗」文案
    order_style: str              # 下单方式建议（限价/分笔/放弃）
    block_reason: str             # 不可开新仓时的原因
    real_tradable: bool           # 是否处于可下单的连续竞价时段


_PHASE_ATTRS = {
    PHASE_PRE_TAIL: dict(
        label="盘前/盘中（14:30前）",
        allow_new_entry=False,
        position_mult=0.0,
        score_bar_delta=0.0,
        window_text="未到尾盘窗口",
        order_style="不下单，仅观察",
        block_reason="尚未进入 14:30 尾盘确认时段，P4 信号仅供观察",
        real_tradable=False,
    ),
    PHASE_OBSERVE: dict(
        label="尾盘观察期 14:30–14:35",
        allow_new_entry=True,
        position_mult=0.60,
        score_bar_delta=2.5,
        window_text="14:30–14:35（观察期，半仓试水）",
        order_style="限价挂现价+0.2%，分一笔；成交不足不追价",
        block_reason="",
        real_tradable=True,
    ),
    PHASE_GOLDEN: dict(
        label="尾盘黄金窗 14:35–14:45",
        allow_new_entry=True,
        position_mult=1.00,
        score_bar_delta=0.0,
        window_text="14:35–14:45（黄金窗，主建仓）",
        order_style="分两笔：第一笔现价+0.3%限价，第二笔回踩14:40均价/VWAP挂",
        block_reason="",
        real_tradable=True,
    ),
    PHASE_LATE: dict(
        label="尾盘窗口 14:45–14:55",
        allow_new_entry=True,
        position_mult=0.70,
        score_bar_delta=2.0,
        window_text="14:45–14:55（尾窗，减半仓）",
        order_style="只挂现价不追高；14:55 前未成交直接放弃",
        block_reason="",
        real_tradable=True,
    ),
    PHASE_FREEZE: dict(
        label="收盘竞价冻结期 14:55–15:00",
        allow_new_entry=False,
        position_mult=0.0,
        score_bar_delta=0.0,
        window_text="14:55–15:00（冻结期）",
        order_style="禁止新开仓；已持仓者只做止损复核",
        block_reason="14:55 后进入收盘竞价，成交价不可控，禁止尾盘追单买新仓",
        real_tradable=False,
    ),
    PHASE_CLOSED: dict(
        label="非交易时段",
        allow_new_entry=False,
        position_mult=0.0,
        score_bar_delta=0.0,
        window_text="非交易时段",
        order_style="禁止下单，仅供复盘",
        block_reason="已收盘或未开盘，P4 结果仅供复盘，不作为买入依据",
        real_tradable=False,
    ),
}


def resolve_curr_min(curr_min: Any = None) -> int:
    """把 curr_min 归一为北京时间的「当日分钟数」；缺失时读系统时钟。"""
    try:
        if isinstance(curr_min, (int, float)) and curr_min == curr_min:  # 排除 NaN
            return int(curr_min)
    except (TypeError, ValueError):
        pass
    now = datetime.now(BJ_TZ)
    return int(now.hour) * 60 + int(now.minute)


def _cfg_int(cfg: Any, name: str, default: int) -> int:
    try:
        val = getattr(cfg, name, default)
        return int(val)
    except (TypeError, ValueError):
        return int(default)


def resolve_tail_phase(
    curr_min: Any = None,
    cfg: Any = None,
    *,
    is_backtest: bool = False,
) -> TailPhase:
    """按分钟数解析当前尾盘阶段。

    参数：
        curr_min    : 当日分钟数（14:30 = 870）；None → 读系统时钟
        cfg         : P4TailScreenerConfig（可含 tail_* 阶段边界字段）；None → 用内置默认
        is_backtest : 回测/日线代理模式 → 视为黄金窗（不做实时时钟门禁）
    """
    try:
        if is_backtest:
            return TailPhase(key=PHASE_GOLDEN, **_PHASE_ATTRS[PHASE_GOLDEN])

        m = resolve_curr_min(curr_min)

        open_min = _cfg_int(cfg, "tail_market_open_min", 570)      # 09:30
        mid_break = _cfg_int(cfg, "tail_morning_close_min", 690)   # 11:30
        pm_open = _cfg_int(cfg, "tail_afternoon_open_min", 780)    # 13:00
        observe_min = _cfg_int(cfg, "tail_observe_start_min", 870)  # 14:30
        golden_min = _cfg_int(cfg, "tail_golden_start_min", 875)   # 14:35
        late_min = _cfg_int(cfg, "tail_late_start_min", 885)       # 14:45
        freeze_min = _cfg_int(cfg, "tail_freeze_start_min", 895)   # 14:55
        close_min = _cfg_int(cfg, "tail_market_close_min", 900)    # 15:00

        # 午休 / 非交易时段 → 不参与尾盘
        if m < open_min or m > close_min or mid_break < m < pm_open:
            return TailPhase(key=PHASE_CLOSED, **_PHASE_ATTRS[PHASE_CLOSED])

        if m >= freeze_min:
            return TailPhase(key=PHASE_FREEZE, **_PHASE_ATTRS[PHASE_FREEZE])
        if m >= late_min:
            return TailPhase(key=PHASE_LATE, **_PHASE_ATTRS[PHASE_LATE])
        if m >= golden_min:
            return TailPhase(key=PHASE_GOLDEN, **_PHASE_ATTRS[PHASE_GOLDEN])
        if m >= observe_min:
            return TailPhase(key=PHASE_OBSERVE, **_PHASE_ATTRS[PHASE_OBSERVE])
        return TailPhase(key=PHASE_PRE_TAIL, **_PHASE_ATTRS[PHASE_PRE_TAIL])
    except Exception:
        return TailPhase(key=PHASE_PRE_TAIL, **_PHASE_ATTRS[PHASE_PRE_TAIL])


def phase_to_dict(phase: Optional[TailPhase]) -> dict:
    """给 detail / UI 用的序列化。"""
    if phase is None:
        return {}
    return {
        "key": phase.key,
        "label": phase.label,
        "allow_new_entry": bool(phase.allow_new_entry),
        "position_mult": round(float(phase.position_mult), 3),
        "score_bar_delta": round(float(phase.score_bar_delta), 2),
        "window_text": phase.window_text,
        "order_style": phase.order_style,
        "block_reason": phase.block_reason,
        "real_tradable": bool(phase.real_tradable),
    }


__all__ = [
    "BJ_TZ",
    "PHASE_PRE_TAIL", "PHASE_OBSERVE", "PHASE_GOLDEN",
    "PHASE_LATE", "PHASE_FREEZE", "PHASE_CLOSED",
    "TailPhase",
    "resolve_curr_min",
    "resolve_tail_phase",
    "phase_to_dict",
]
