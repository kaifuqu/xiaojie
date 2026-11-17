# -*- coding: utf-8 -*-
"""
core/p1_explain.py - 【V26.88+10-3 P2-⑨】P1 选股决策白盒接口

【背景】
P1 打分链内部 11 项基础 + 5 项增强 + 周线 3 项 + V28 附加，外部难以理解。
新增 explain_p1_decision(ts_code) 接口，返回：
  1. 每条硬闸是否触发 (passed/failed + 文案)
  2. 每条加分项最终分数
  3. combo 乘数触发条件
  4. 是否走特赦通道
  5. base × combo × memory 三阶段贡献
  6. 是否进入 P1 主池 / 观察池 / 被拒

【使用】
from core.p1_explain import explain_p1_decision
result = explain_p1_decision("600519.SH")  # 或 "sh600519"
result = explain_p1_decision("600519", s_code=True)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 简单 in-memory 缓存：ts_code -> (timestamp, payload)
_EXPLAIN_CACHE: Dict[str, tuple] = {}
_EXPLAIN_CACHE_TTL = 300  # 5 分钟


def _norm_ts_code(ts_code: str, s_code: bool = False) -> str:
    """统一为 ts_code 格式 (600519.SH)。"""
    code = str(ts_code or "").strip()
    if not code:
        return ""
    if s_code:
        # 6 位代码 → 推断交易所
        if code.startswith(("60", "68", "90")):
            return f"{code}.SH"
        if code.startswith(("00", "30", "20")):
            return f"{code}.SZ"
        if code.startswith(("8", "4")):
            return f"{code}.BJ"
        return f"{code}.SH"  # 默认上交所
    return code


def explain_p1_decision(
    ts_code: str,
    *,
    s_code: bool = False,
    refresh: bool = False,
) -> Dict[str, Any]:
    """
    解释某只票的 P1 决策全过程。

    Args:
        ts_code: ts_code 格式 "600519.SH"，或 s_code "600519"（需 s_code=True）
        s_code: 是否传入 6 位 s_code（自动推断交易所）
        refresh: 是否强制刷新缓存

    Returns:
        {
            "ts_code": "600519.SH",
            "passed": True/False,
            "pool_tier": "main"/"observation"/"rejected"/"unknown",
            "final_score": 73.5,
            "hard_gates": [{"gate": "市值", "passed": True, "reason": "..."}, ...],
            "score_breakdown": {"筹码真空": 12.3, ...},
            "combo_multiplier": 1.12,
            "combo_tags": ["顶级底仓共振"],
            "amnesty_paths": ["资金特赦档"],
            "behavior_penalty": -1.5,
            "phases": {
                      "raw_base": 65.2,
                      "amplified": 73.0,  # = raw_base × combo
                      "memory_blended": 73.5,  # = (1-w)*amplified + w*fm100
                  },
            "timestamp": "2026-10-03 22:30:00"
        }
    """
    ts = _norm_ts_code(ts_code, s_code=s_code)
    if not ts:
        return {"error": "empty ts_code"}

    # 缓存检查
    if not refresh and ts in _EXPLAIN_CACHE:
        ts_cached, payload = _EXPLAIN_CACHE[ts]
        if time.time() - ts_cached < _EXPLAIN_CACHE_TTL:
            return dict(payload)

    result: Dict[str, Any] = {
        "ts_code": ts,
        "passed": None,
        "pool_tier": "unknown",
        "final_score": 0.0,
        "hard_gates": [],
        "score_breakdown": {},
        "combo_multiplier": 1.0,
        "combo_tags": [],
        "amnesty_paths": [],
        "behavior_penalty": 0.0,
        # 【批次 A】死因层级 / 处置建议（展示层字段，不参与任何判定）
        "death_cause_level": None,
        "death_cause_level_cn": "",
        "death_cause_advice": "",
        "phases": {"raw_base": 0.0, "amplified": 0.0, "memory_blended": 0.0},
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    try:
        # 1) 找最近一次 P1 缓存 JSON (p1_cache_YYYYMMDD.json)
        import glob as _glob

        from core.runtime_data_paths import POOL_CACHE_DIR

        candidates = _glob.glob(os.path.join(str(POOL_CACHE_DIR), "p1_cache_*.json"))
        # 取最新（按文件名排序）
        latest_path = sorted(candidates)[-1] if candidates else None

        if not latest_path or not os.path.isfile(str(latest_path)):
            result["error"] = "未找到 P1 缓存 JSON，请先跑一次 P1 build"
            return result

        with open(str(latest_path), "r", encoding="utf-8") as f:
            payload = json.load(f)

        items = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            items = payload if isinstance(payload, list) else []

        target = None
        for it in items:
            if not isinstance(it, dict):
                continue
            code = str(it.get("code") or it.get("ts_code") or "").strip()
            if code == ts or code.split(".")[0] == ts.split(".")[0]:
                target = it
                break

        if target is None:
            result["error"] = f"该票不在最近 P1 缓存（可能未进入主池/已被拒）"
            # 仍然尝试从 rejected 池中找
            rej = payload.get("rejected") if isinstance(payload, dict) else None
            if isinstance(rej, list):
                for it in rej:
                    if str(it.get("代码") or "") == ts.split(".")[0]:
                        _reason = it.get("淘汰死因")
                        result["rejected_reason"] = _reason
                        result["pool_tier"] = "rejected"
                        # 【批次 A】附带死因层级与处置建议
                        try:
                            from core.p1_death_cause_registry import (
                                classify_death_cause as _cdc,
                            )

                            _ci = _cdc(_reason)
                            result["death_cause_level"] = _ci.get("level")
                            result["death_cause_level_cn"] = _ci.get("level_cn") or ""
                            result["death_cause_advice"] = _ci.get("advice") or ""
                        except Exception:
                            pass
                        break
            return result

        # 2) 提取详情
        result["passed"] = bool(target.get("is_pass", target.get("p1_score", 0) > 0))
        result["final_score"] = float(target.get("p1_score", 0.0) or 0.0)
        result["pool_tier"] = str(target.get("pool_tier", "main"))
        result["score_breakdown"] = dict(target.get("score_details") or {})
        result["combo_multiplier"] = float(result["score_breakdown"].get("组合特征乘数", 1.0) or 1.0)
        result["combo_tags"] = [
            t for t in str(result["score_breakdown"].get("组合特征标签", "")).split("、") if t
        ]
        result["amnesty_paths"] = [
            t for t in str(target.get("reason", "")).split(" | ") if t
        ]
        result["behavior_penalty"] = float(result["score_breakdown"].get("渣男股性惩罚", 0.0) or 0.0)
        result["phases"] = {
            "raw_base": float(result["score_breakdown"].get("融合前分项合计", 0.0) or 0.0),
            "amplified": float(result["score_breakdown"].get("乘数后基底分", 0.0) or 0.0),
            "memory_blended": float(target.get("p1_score", 0.0) or 0.0),
        }
        # 硬闸：列出当前是否触发了 MA20 之下、市值、PE 等前置闸
        result["hard_gates"] = _infer_hard_gates(target, result["score_breakdown"])

    except Exception as e:
        logger.debug(f"[explain_p1_decision][{ts}] {type(e).__name__}: {e}")
        result["error"] = str(e)

    _EXPLAIN_CACHE[ts] = (time.time(), result)
    return result


def _infer_hard_gates(target: Dict[str, Any], details: Dict[str, Any]) -> list:
    """从 score_details / reason 反推硬闸触发情况。"""
    gates = []
    sd_lower = json.dumps(details, ensure_ascii=False).lower() if details else ""
    reason = str(target.get("reason", "") or "")

    gate_defs = [
        ("市值≥120亿", "流通市值"),
        ("MA20之上(近3日≥2日)", "MA20下方"),
        ("PE硬卡×2.5", "PE极度超标"),
        ("PE软卡×1.7", "PE相对超标"),
        ("深海防线", "深海防线"),
        ("乖离率≤22%", "乖离率极危"),
        ("业绩首亏", "业绩首亏"),
        ("解禁黑名单", "解禁"),
        ("股性呆滞", "股性呆滞"),
        ("流动性枯竭", "流动性枯竭"),
        ("均线多头", "均线未呈多头"),
        ("MACD柱", "MACD过弱"),
        ("量价背离", "量价背离"),
        ("回撤超标", "回撤超标"),
        ("破位洗盘", "破位洗盘"),
        ("偏离主峰", "偏离主峰过远"),
        ("筹码重压", "筹码重压"),
    ]
    # 【批次 A】死因分级：层级来自 core.p1_death_cause_registry
    #（静态映射表，不读库、不参与判定）。
    # 未触发的闸门一律给中性层级，避免把「未触发」误标成「高风险」。
    # [V31-A fix] 原因：此处对整条 reason 做首命中分类，复合死因文案会把层级归给错误的闸门。
    # 修复：改用 classify_gate_death_cause(resecret, keyword)，按本次确认触发的闸门关键词取层级。
    try:
        from core.p1_death_cause_registry import classify_gate_death_cause as _cdc2
    except Exception:
        _cdc2 = None

    for label, keyword in gate_defs:
        passed = keyword not in reason
        _lvl = _lvl_cn = _adv = None
        if not passed and _cdc2 is not None:
            try:
                _ci2 = _cdc2(reason, keyword)
                _lvl = _ci2.get("level")
                _lvl_cn = _ci2.get("level_cn")
                _adv = _ci2.get("advice")
            except Exception:
                _lvl = _lvl_cn = _adv = None
        gates.append({
            "gate": label,
            "passed": passed,
            "reason": reason if not passed else "",
            "level": _lvl,
            "level_cn": _lvl_cn,
            "advice": _adv or "",
        })
    return gates


__all__ = ["explain_p1_decision"]