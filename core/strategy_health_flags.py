# -*- coding: utf-8 -*-
"""core/strategy_health_flags.py
========================================================================
战法健康度红绿灯（Tier 2 · 2.6）
========================================================================

【为什么需要】
`strategy_weight_optimizer.optimize_strategy_weights()` 的守卫是
`n_signals >= 200` 才真改权重 —— 这个设计本身是对的（防过拟合）。
但它带来一个副作用：**样本量还很小、但已经明显负向的战法，会一直挂在主榜上**。

本模块只做一件事：**给战法打一个「健康度标签」，供 UI 变灰/变红**。
它【不参与扫描、不改权重、不改阈值、不删档】，纯展示层。

【判定规则（全部可解释）· 2026-11 修订：统计优先，硬编码列表降级为「提示」】
1. 已评估样本 >= 30 → **以统计为准**（胜率 + 平均收益），历史名单不再覆盖统计结论。
2. 已评估样本 < 30 且命中 `NEGATIVE_KNOWN` → 「历史负向·待复核」（橙）：
   只作为线索提示，不直接判负 —— 避免「硬编码名单压在统计之上」的循环论证。
3. 已评估样本 < 30（未命中名单）→ 样本不足（灰）
4. 胜率 < 40% 且 平均收益 < 0 → 负向（红）
5. 胜率 < 45% 且 平均收益 < 0 → 偏弱（黄）
6. 其余 → 正常（绿）

【红线】
· 只读，不写库
· 任何异常回退到「未知」标签，绝不抛错
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 已实测负向的战法（来源：docs/EXTERNAL_REPO_BENCHMARK_REPORT_V2_20261102.md 与
# strategy_effectiveness 快照 2026-10）。key 用战法 ID 前缀匹配。
# 【V35 编号重整】旧号→新号：05A→P5-07 趋势结构 / 05B→P5-08 趋势质量 /
# 05C→P5-09 趋势确认 / 03A→P5-04 资金方向（快照数据本身未变，仅编号迁移）。
NEGATIVE_KNOWN: Dict[str, str] = {
    "P5-07": "2026-10 快照（旧05A）：胜率 28.6% / 平均 -5.6%",
    "P5-08": "2026-10 快照（旧05B）：胜率 21.7%",
    "P5-09": "2026-10 快照（旧05C）：胜率 22.7%",
    "P5-04": "2026-10 快照（旧03A）：胜率 16.7% / 平均 -7.1%",
}

MIN_SAMPLE_N = 30

FLAG_LABEL = {
    "negative": "🔴 负向观察",
    "weak": "🟡 偏弱",
    "watch": "⚪ 样本不足",
    "hint": "🟠 历史负向·待复核",
    "ok": "🟢 正常",
    "unknown": "— 未知",
}


def _match_known(strategy: str) -> Optional[str]:
    s = (strategy or "").upper()
    for k, why in NEGATIVE_KNOWN.items():
        if k.upper() in s:
            return why
    return None


def classify_health(
    strategy: str,
    win_rate: Optional[float] = None,
    avg_return: Optional[float] = None,
    n_evaluated: int = 0,
) -> Dict[str, Any]:
    """返回 {flag, label, reason}。异常一律降级为 unknown。

    2026-11 修订：统计优先 —— 样本量够时，硬编码历史名单不再覆盖统计结论；
    样本量不足时，名单命中也只给「待复核提示」（hint），不直接判负。
    """
    try:
        n = int(n_evaluated or 0)
        why = _match_known(strategy)
        if n >= MIN_SAMPLE_N:
            wr = None if win_rate is None else float(win_rate)
            ar = None if avg_return is None else float(avg_return)
            if wr is not None and ar is not None:
                if wr < 40.0 and ar < 0:
                    return {"flag": "negative", "label": FLAG_LABEL["negative"],
                            "reason": f"胜率 {wr:.1f}% 且平均 {ar:+.2f}%（n={n}）"}
                if wr < 45.0 and ar < 0:
                    return {"flag": "weak", "label": FLAG_LABEL["weak"],
                            "reason": f"胜率 {wr:.1f}% 且平均 {ar:+.2f}%（n={n}）"}
            _r = f"历史名单曾标负向，现按统计复核为中性（n={n}）" if why else ""
            return {"flag": "ok", "label": FLAG_LABEL["ok"], "reason": _r}
        # 样本不足（< MIN_SAMPLE_N）
        if why:
            return {"flag": "hint", "label": FLAG_LABEL["hint"],
                    "reason": f"{why}；已评估 {n} < {MIN_SAMPLE_N} 条，仅作待复核提示，不判负"}
        return {
            "flag": "watch",
            "label": FLAG_LABEL["watch"],
            "reason": f"已评估 {n} < {MIN_SAMPLE_N} 条，暂不下结论",
        }
    except Exception as e:                                    # pragma: no cover
        logger.debug("[strategy_health_flags] 判定失败 %s: %s", strategy, e)
        return {"flag": "unknown", "label": FLAG_LABEL["unknown"], "reason": ""}


def is_negative(strategy: str) -> bool:
    """快捷判断：是否已亮红灯（供推送/主榜过滤参考，不做强制拦截）。"""
    return _match_known(strategy) is not None
