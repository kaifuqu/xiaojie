# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.88+9-22 — HiThink 大盘指数实时快照模块

【功能】
一次性拉取 A 股核心指数（上证/沪深300/中证500）实时快照，
计算「大盘强度评分 0~2」，供 P2~P5 各池的 regime 判断实时锚定。

【设计意图】
- 传统 regime_analyzer 用历史日线数据（收盘后才有），盘中运行时大盘强度靠估算，
  导致 P2/P3 的 _regime_bucket 几乎总是 "neutral"，无法自适应切换严格/宽松档位。
- HiThink index_prices_snapshot 是实时接口（秒级），直接拉指数快照，比个股估算更准。

【大盘强度评分规则】
  0.0 = 大盘极弱（沪深300 跌幅 > -1% 或 上证 < 3100）
  0.5 = 大盘偏弱（沪深300 跌幅 -1%~0% 或 震荡）
  1.0 = 大盘正常（中性基准）
  1.5 = 大盘偏强（沪深300 涨幅 0%~1%）
  2.0 = 大盘极强（沪深300 涨幅 > 1%）

【调用方】
- auto_sniper_daemon：在调用 run_scan_engine 前注入 rt['_hithink_index_strength']
- P2/P3/P4/P5 引擎：通过 rt.get('_hithink_index_strength') 读取，不改变各引擎内部逻辑
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 核心指数列表（格式：thscode）
_INDEX_CODES = ["000001.SH", "000300.SH", "000905.SH"]

# 大盘强度阈值（A 股经验值）
_HS300_WEAK = -1.0    # 沪深300 跌幅超过 -1% → 极弱
_HS300_SOFT = 0.0     # 沪深300 在 0% 附近 → 偏弱
_HS300_STRONG = 1.0   # 沪深300 涨幅超过 +1% → 极强
_SZSE_WEAK = 3100     # 上证指数 < 3100 → 极弱兜底


def hithink_index_snapshot(
    codes: Optional[list] = None,
    hithink_client=None,
) -> Dict[str, Any]:
    """
    拉取 A 股核心指数实时快照，计算大盘强度评分。

    Args:
        codes: 指数代码列表，默认 ["600000.SH", "000300.SH", "000905.SH"]
        hithink_client: 已实例化的 HiThink 客户端（避免重复初始化开销）

    Returns:
        {
            "enabled": bool,           # HiThink 是否可用
            "scores": {
                "sh000001": {pct_chg, price, prev_close},
                "hs300":    {pct_chg, price, prev_close},
                "zz500":    {pct_chg, price, prev_close},
            },
            "market_strength": float,   # 大盘强度评分 0.0~2.0
            "market_label": str,        # "极弱"/"偏弱"/"正常"/"偏强"/"极强"
            "best_interpretation": str, # 中文解释，供日志/UI 使用
            "ts": float,               # 抓取时间戳
        }
    """
    import time
    ts = time.time()
    out: Dict[str, Any] = {
        "enabled": False,
        "scores": {},
        "market_strength": 1.0,   # 默认中性
        "market_label": "正常",
        # 【2026-11-14】文案修正：此默认值在「未启用 / 网络失败 / 快照为空」多条路径
        # 都会输出，原「HiThink 未启用」在已启用但网络失败时误导排查方向。
        "best_interpretation": "大盘正常（HiThink 快照不可用，使用默认中性值）",
        "ts": ts,
    }

    if hithink_client is None:
        try:
            from data.hithink_client import HithinkClient
            hithink_client = HithinkClient()
        except Exception as e:
            logger.debug("[HiThink 大盘] 客户端初始化失败: %s", e)
            return out

    if not hithink_client.is_enabled:
        logger.debug("[HiThink 大盘] HiThink 未启用")
        return out

    targets = codes if codes else _INDEX_CODES
    try:
        df = hithink_client.index_prices_snapshot(targets)
        if df is None or df.empty:
            logger.debug("[HiThink 大盘] 指数快照为空")
            return out
    except Exception as e:
        logger.debug("[HiThink 大盘] 拉取失败: %s", e)
        return out

    # 解析结果
    scores: Dict[str, Dict[str, float]] = {}
    for _, row in df.iterrows():
        code = str(row.get("thscode", "")).strip()
        try:
            pct = float(row.get("price_change_ratio_pct", 0.0) or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        try:
            price = float(row.get("last_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            price = 0.0
        try:
            prev = float(row.get("prev_price", 0.0) or 0.0)
        except (TypeError, ValueError):
            prev = 0.0

        # 【2026-11-14 修复】上证指数 thscode 是 000001.SH（600000.SH 是浦发银行，
        # HiThink 指数快照接口对其直接不返回 → 上证涨幅/点位长期恒 0）。
        if code in ("000001.SH", "600000.SH"):
            scores["sh000001"] = {"pct_chg": pct, "price": price, "prev_close": prev}
        elif code == "000300.SH":
            scores["hs300"] = {"pct_chg": pct, "price": price, "prev_close": prev}
        elif code == "000905.SH":
            scores["zz500"] = {"pct_chg": pct, "price": price, "prev_close": prev}
        # 其他指数（如果有扩展）

    if not scores:
        logger.debug("[HiThink 大盘] 未能匹配到任何已知指数")
        return out

    # 计算大盘强度评分（以沪深300 为主，上证兜底）
    hs300_pct = scores.get("hs300", {}).get("pct_chg", 0.0)
    sh_pct = scores.get("sh000001", {}).get("pct_chg", 0.0)
    zz_pct = scores.get("zz500", {}).get("pct_chg", 0.0)
    sh_price = scores.get("sh000001", {}).get("price", 0.0)

    # 核心规则：沪深300 为锚，辅以上证绝对价格兜底
    if hs300_pct <= _HS300_WEAK or (sh_price > 0 and sh_price < _SZSE_WEAK):
        strength = 0.0
        label = "极弱"
    elif hs300_pct <= _HS300_SOFT:
        strength = 0.5
        label = "偏弱"
    elif hs300_pct >= _HS300_STRONG:
        strength = 2.0
        label = "极强"
    elif hs300_pct >= _HS300_STRONG * 0.5:   # 0.5%~1%
        strength = 1.5
        label = "偏强"
    else:   # -0.5%~0.5%
        strength = 1.0
        label = "正常"

    # 交叉验证：若中小盘（中证500）方向与沪深300 相反，略微修正
    if strength >= 1.0 and zz_pct < -0.5:
        strength = max(0.5, strength - 0.5)  # 大盘强但小盘弱 → 降一档
        label = "偏弱"
    elif strength <= 1.0 and zz_pct > 0.5:
        strength = min(1.5, strength + 0.5)  # 大盘弱但小盘强 → 升一档
        label = "偏强"

    interpretation = (
        f"A股大盘{'强' if strength >= 1.5 else '偏强' if strength >= 1.0 else '偏弱' if strength >= 0.5 else '极弱'}"
        f"（沪深300 {hs300_pct:+.2f}% / 上证 {sh_pct:+.2f}% / 中证500 {zz_pct:+.2f}%）"
    )

    out = {
        "enabled": True,
        "scores": scores,
        "market_strength": round(strength, 2),
        "market_label": label,
        "best_interpretation": interpretation,
        "ts": ts,
        # 原始涨跌供各池直接引用（避免再算一次）
        "hs300_pct": round(hs300_pct, 4),
        "sh_pct": round(sh_pct, 4),
        "zz_pct": round(zz_pct, 4),
    }
    logger.info(
        "[HiThink 大盘] 强度 %.1f (%s) | 沪深300 %+.3f%% 上证 %+.3f%% 中证500 %+.3f%%",
        strength, label, hs300_pct, sh_pct, zz_pct,
    )
    return out


def get_market_strength_for_rt() -> float:
    """
    便捷封装：直接返回大盘强度评分（0.0~2.0），供 scan_engine / auto_sniper_daemon 调用。
    失败时返回 1.0（中性），不阻塞主流程。
    """
    result = hithink_index_snapshot()
    return float(result.get("market_strength", 1.0))
