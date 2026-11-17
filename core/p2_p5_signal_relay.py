# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V26.80 — P1→P5 跨池信号接力

【背景】
A 股实战中，一只票常在多个池中"接力"出现：
  - P1 底仓入选 → 第二天 P2 竞价 → P3 盘中 → P4 尾盘 → P5 盘后
  - 同一只票在不同池中的信号应当**累积加分**

【本模块】
  1. 跨池信号记录（每次扫描后写入 session_state）
  2. 跨池信号接力评分：
     - 同一票连续 2 个池命中 +5 分
     - 连续 3 个池命中 +10 分
     - 连续 4 个池命中 +15 分
     - 连续 5 个池命中 +20 分
  3. 反向信号：曾被 P5 拒绝的票，P2 应当谨慎

【使用】
  from core.p2_p5_signal_relay import (
      record_pool_hit,
      calc_relay_bonus,
      was_rejected_in_pool,
  )
"""
from __future__ import annotations

import logging
import os
import json
import time
from datetime import datetime, timezone, timedelta
from threading import RLock
from typing import Dict, Any, List, Optional
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


# ==================== 持久化 ====================

_RELAY_FILE = "_p2_p5_relay_history.json"
_LOCK = RLock()


def _today_bj_yyyymmdd() -> str:
    """北京时间业务日（YYYYMMDD），跨日自动切换，接力记录不跨交易日污染。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")


def _get_relay_path() -> str:
    try:
        from core.runtime_data_paths import path_blacklist_json
        base = os.path.dirname(path_blacklist_json())
    except Exception:
        base = os.path.join(os.getcwd(), "data")
    return os.path.join(base, _RELAY_FILE)


def _load_relay_history() -> Dict[str, Any]:
    """读取跨池历史（带 TTL）。"""
    p = _get_relay_path()
    if not os.path.exists(p):
        return {"date": "", "records": {}}

    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        # [BugFix] 统一走北京时间，避免部署机时区非 Asia/Shanghai 时跨日污染跨池历史
        # 根因：正常读取路径仍用本地时区，写入路径已用 _today_bj_yyyymmdd()
        today = _today_bj_yyyymmdd()
        if data.get("date") != today:
            return {"date": today, "records": {}}
        return data
    except Exception as e:
        logger.debug(f"读取跨池历史失败: {e}")
        return {"date": _today_bj_yyyymmdd(), "records": {}}


def _save_relay_history(data: Dict[str, Any]) -> None:
    p = _get_relay_path()
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.debug(f"保存跨池历史失败: {e}")


# ==================== 信号记录 ====================

def record_pool_hit(
    ts_code: str,
    pool_key: str,
    burst_score: float,
    is_rejected: bool = False,
    reject_reason: str = "",
) -> None:
    """
    记录某只股票在某池的命中/拒绝记录。

    Args:
        ts_code: 股票代码
        pool_key: p1/p2/p3/p4/p5
        burst_score: 当前分数
        is_rejected: 是否被拒绝
        reject_reason: 拒绝原因
    """
    with _LOCK:
        history = _load_relay_history()
        today = _today_bj_yyyymmdd()
        if history.get("date") != today:
            history = {"date": today, "records": {}}

        records = history.setdefault("records", {})
        rec = records.setdefault(ts_code, {"hits": [], "rejects": []})

        if is_rejected:
            rec["rejects"].append({
                "pool": pool_key,
                "reason": reject_reason[:80],
                "ts": time.time(),
            })
            # 最多保留 5 次拒绝
            rec["rejects"] = rec["rejects"][-5:]
        else:
            rec["hits"].append({
                "pool": pool_key,
                "score": burst_score,
                "ts": time.time(),
            })
            # 最多保留 5 次命中
            rec["hits"] = rec["hits"][-5:]

        history["records"] = records
        history["date"] = today
        _save_relay_history(history)


# ==================== 信号接力评分 ====================

# 池子顺序
_POOL_ORDER = ["p1", "p2", "p3", "p4", "p5"]


def calc_relay_bonus(ts_code: str, current_pool: str) -> Dict[str, Any]:
    """
    计算跨池信号接力加分。

    Args:
        ts_code: 股票代码
        current_pool: 当前池（p2/p3/p4/p5）

    Returns:
        {
            "relay_bonus": 0~20,
            "consecutive_pools": ["p2", "p3", "p4"],
            "tags": ["🔥[三连命中]"]
        }
    """
    with _LOCK:
        history = _load_relay_history()
        rec = history.get("records", {}).get(ts_code, {})
        hits = rec.get("hits", [])

        if not hits:
            return {"relay_bonus": 0.0, "consecutive_pools": [], "tags": []}

        # 提取所有命中过的池子（去重）
        hit_pools = []
        for h in hits:
            p = h.get("pool", "")
            if p and p not in hit_pools:
                hit_pools.append(p)

        # 按 _POOL_ORDER 排序
        sorted_pools = sorted(hit_pools, key=lambda x: _POOL_ORDER.index(x) if x in _POOL_ORDER else 99)

        # 计算连续池数（从当前池向前数）
        if current_pool not in _POOL_ORDER:
            return {"relay_bonus": 0.0, "consecutive_pools": [], "tags": []}

        cur_idx = _POOL_ORDER.index(current_pool)
        consecutive = []
        for p in sorted_pools:
            if p in _POOL_ORDER:
                p_idx = _POOL_ORDER.index(p)
                if p_idx <= cur_idx:
                    consecutive.append(p)
        consecutive = sorted(consecutive, key=lambda x: _POOL_ORDER.index(x))

        # 加分
        n = len(consecutive)
        bonus = 0.0
        tags = []
        if n >= 5:
            bonus = 20.0
            tags.append("🌟[P1-P5全链命中]极强信号")
        elif n >= 4:
            bonus = 15.0
            tags.append("🔥[四连命中]强势接力")
        elif n >= 3:
            bonus = 10.0
            tags.append("🚀[三连命中]信号一致")
        elif n >= 2:
            bonus = 5.0
            tags.append("📈[双池命中]值得关注")

        return {
            "relay_bonus": bonus,
            "consecutive_pools": consecutive,
            "tags": tags,
        }


def was_rejected_in_pool(
    ts_code: str,
    pool_key: str,
    lookback_hours: int = 4,
) -> Optional[Dict[str, Any]]:
    """
    检查某股票在某池中最近是否被拒绝过。

    Args:
        ts_code: 股票代码
        pool_key: 池子
        lookback_hours: 回看小时数

    Returns:
        {"rejected": True, "reason": "...", "ts": ...} or None
    """
    with _LOCK:
        history = _load_relay_history()
        rec = history.get("records", {}).get(ts_code, {})
        rejects = rec.get("rejects", [])

        now = time.time()
        for r in rejects:
            if r.get("pool") == pool_key and (now - r.get("ts", 0)) < lookback_hours * 3600:
                return r

        return None


# ==================== P2/P3/P4/P5 注入辅助 ====================

def apply_relay_bonus_to_pool_res(
    res: Dict[str, Any],
    ts_code: str,
    pool_key: str,
    apply_to_burst: bool = True,
) -> Dict[str, Any]:
    """
    将跨池接力加分注入到打分结果中。

    Args:
        res: 策略返回的字典
        ts_code: 股票代码
        pool_key: 池子
        apply_to_burst: 是否直接加到 burst_score

    Returns:
        更新后的 res
    """
    try:
        relay = calc_relay_bonus(ts_code, pool_key)
        if relay["relay_bonus"] > 0:
            # 写入 risk_tags / detail
            res.setdefault("risk_tags", [])
            for tag in relay["tags"]:
                if tag not in res["risk_tags"]:
                    res["risk_tags"].append(tag)

            res.setdefault("detail", {})
            res["detail"]["relay_bonus"] = relay["relay_bonus"]
            res["detail"]["relay_consecutive_pools"] = relay["consecutive_pools"]

            # 加到 burst_score
            if apply_to_burst:
                old_score = float(res.get("burst_score", 0.0))
                res["burst_score"] = round(min(100.0, old_score + relay["relay_bonus"]), 2)

        # 检查是否有最近的拒绝记录
        rejected = was_rejected_in_pool(ts_code, pool_key)
        if rejected and pool_key != "p5":  # P5 盘后通常不复用
            res.setdefault("risk_tags", [])
            reject_tag = f"⚠️[曾被{rejected['pool'].upper()}拒绝]{rejected.get('reason', '')[:30]}"
            if reject_tag not in res["risk_tags"]:
                res["risk_tags"].append(reject_tag)

    except Exception as e:
        logger.debug(f"跨池信号接力注入失败: {e}")

    return res
