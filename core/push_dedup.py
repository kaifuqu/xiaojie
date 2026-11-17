# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V28 — 推送去重（同票冷却 + 跨池去重）
================================================================================
【V28 新增·G 项】
  - 票级冷却：单只股票推过后 N 个交易日内不重复推（除非分数显著高于上次）
  - 跨池冷却：同一票当日已被 P3/P4 推过，P5 不再推
  - 提示文案：去重后给用户提示"该股 N 天前已推送，关注中"

数据：data/push_history.json（本地缓存，格式：{ts_code: [{date, pool, score}, ...]}）

集成：
  - core/pool_signal_adapter_v28.append_signal_log_safe 末尾调用
  - 命中冷却则 is_pass=False 返回
================================================================================
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

BJ_TZ = timezone(timedelta(hours=8))
HISTORY_FILE = "data/push_history.json"
COOLDOWN_DAYS = 3          # 票级冷却期（交易日）
COOLDOWN_DAYS_CROSS = 1    # 跨池冷却（1 日内同票不重推）
SCORE_DELTA_THRESHOLD = 10.0  # 分数差 > 此值允许重推


def _qa_mode() -> bool:
    """True = 当前处于「测试 / 演练」环境，不允许写生产推送账本。

    【2026-11-03 修复 · 根因消除】
    事故：tools/test_signal_ledger_write.py、tools/test_signal_log_wiring.py 等
    回归脚本会走 core/pool_signal_adapter_v28.append_signal_log_safe →
    pool_apply_push_dedup(record=True) → record_push()，
    而 HISTORY_FILE 是**硬编码相对路径** `data/push_history.json`，
    既不受 XJ_NO_SIGNAL_PERSIST 保护，也不随 DuckDB 的 QA 副本切换而隔离。
    后果：测试票（贵州茅台 / 中国平安 / 五粮液 …）被写进生产推送账本，
    COOLDOWN_DAYS=3 的票级冷却把**真实票**静默拦掉最多 3 天。

    三重判定（任一命中即视为 QA 环境）：
      1) 环境变量 XJ_NO_SIGNAL_PERSIST / XJ_NO_PUSH / XJ_NO_DEEPSEEK 任一为真
         （core/qa_switches.py 的统一开关，生产启动脚本一律不设置）
      2) 当前 DuckDB 主库路径不在项目根的 data/ 下（即指向 data/_qa 副本）
      3) 显式环境变量 XJ_QA_PUSH_HISTORY=1

    【红线】判定失败一律 fail-open（当作生产环境），绝不允许因为读取失败
    而让生产漏记推送历史（漏记会导致重复推送，比多记严重得多）。
    """
    try:
        from core.qa_switches import env_flag

        if env_flag("XJ_NO_SIGNAL_PERSIST") or env_flag("XJ_NO_PUSH") or env_flag("XJ_NO_DEEPSEEK"):
            return True
        if env_flag("XJ_QA_PUSH_HISTORY"):
            return True
    except Exception:
        pass
    try:
        import data.db_core._connection as _dbc

        _p = str(getattr(_dbc, "db_path", "") or "").replace("\\", "/").rstrip("/").lower()
        if not _p:
            return False
        _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        _prod_dir = os.path.join(_root, "data").replace("\\", "/").rstrip("/").lower()
        # 生产布局：db 文件直接放在 <root>/data/ 下
        # QA 布局：<root>/data/_qa/quant_data.duckdb（父目录多了一层）
        _parent = os.path.dirname(_p)
        if _parent != _prod_dir:
            return True
        # 兜底：路径里带 _qa / _qa_db 之类标记
        if "_qa" in os.path.basename(_parent) or "/_qa" in _p:
            return True
    except Exception:
        pass
    return False


def _now_iso() -> str:
    return datetime.now(BJ_TZ).strftime("%Y-%m-%d")


def _load_history() -> Dict[str, List[Dict[str, Any]]]:
    try:
        if not os.path.exists(HISTORY_FILE):
            return {}
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _save_history(h: Dict[str, List[Dict[str, Any]]]) -> None:
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE) or ".", exist_ok=True)
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(h, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.debug("[push_dedup] 保存失败: %s", e)


def _is_within_days(date_str: str, days: int) -> bool:
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d")
    except Exception:
        return False
    return (datetime.now(BJ_TZ).date() - d.date()).days <= int(days)


def check_dedup(
    ts_code: str,
    pool: str,
    score: float,
    *,
    cooldown_days: int = COOLDOWN_DAYS,
    cross_pool_days: int = COOLDOWN_DAYS_CROSS,
) -> Dict[str, Any]:
    """
    检查是否应去重。

    返回：
        {
          "should_dedup": bool,
          "reason": str,            # 提示用户的原因
          "last_push": dict or None,
          "score_delta": float
        }

    【V26.88+ 9-30 朋友1建议】当日跨池多次触发同票时，保留最高分池推送，其余标 dedup。
    原逻辑：当日任意池推过 → 后续池全被 3 日冷却拦截（reason 是"3日冷却"）。
    新逻辑：
      1. 若当前池分数比历史最高分高 ≥SCORE_DELTA_THRESHOLD=10，仍允许（已存在）
      2. 否则在 cooldown_days=3 内一律 dedup（已存在）
      3. 新增：当超过 3 日冷却窗口但同日内其他池已推过 → 跨池冷却命中（同日仅一次）
    """
    if not ts_code:
        return {"should_dedup": False, "reason": "", "last_push": None, "score_delta": 0.0}

    h = _load_history()
    history = h.get(ts_code, []) or []
    if not history:
        return {"should_dedup": False, "reason": "", "last_push": None, "score_delta": 0.0}

    # 找最近的推送
    history_sorted = sorted(history, key=lambda x: x.get("date", ""), reverse=True)
    last = history_sorted[0]
    last_pool = str(last.get("pool", ""))
    last_date = str(last.get("date", ""))
    last_score = float(last.get("score", 0.0))

    score_delta = float(score) - last_score

    # 同票级冷却（跨任意池，cooldown_days 天内）
    if _is_within_days(last_date, cooldown_days):
        if abs(score_delta) >= SCORE_DELTA_THRESHOLD:
            return {
                "should_dedup": False,
                "reason": f"该股 {last_date} 已推送但当前分数 +{score_delta:.1f} 显著高于上次",
                "last_push": last,
                "score_delta": score_delta,
            }
        return {
            "should_dedup": True,
            "reason": f"该股 {last_date}（{last_pool}）已推送，冷却 {cooldown_days} 日",
            "last_push": last,
            "score_delta": score_delta,
        }

    # 【V26.88+ 9-30】当日跨池冷却：在 3 日冷却窗口之外，但同日内已有其他池推过
    # 场景：上次推是 4 天前的 P5（过了 3 日冷却），但今天 P2 已经推过 → 仍 dedup
    if last_pool != pool and _is_within_days(last_date, cross_pool_days):
        return {
            "should_dedup": True,
            "reason": f"该股今日已被 {last_pool} 推送（{last_date}），跨池冷却",
            "last_push": last,
            "score_delta": score_delta,
        }

    return {"should_dedup": False, "reason": "", "last_push": last, "score_delta": score_delta}


def record_push(
    ts_code: str,
    pool: str,
    score: float,
    *,
    max_history_per_stock: int = 20,
    trade_date: str = "",
) -> None:
    """
    把推送记录写入本地缓存。

    【V30.10 修复 · 历史自我挤爆 bug】
    原实现无条件 append，然后用 arr[-max_history_per_stock:] 截断。
    后果（生产实测 2026-10-21）：
      · P1 底仓重建每次运行都会对同一批票调 record_push
        （core/pool_manager/pool_builder.py:1029）
      · 600577.SH 因此积累了 20 条【同日同池重复】记录
      · arr[-20:] 把 10-08 / 10-09 / 10-12 的真实推送记录【挤没了】
      · 结果：72 条"已推送"信号在回溯时全部匹配不上，
        T+N 归因把大量真实推送样本当成"未推送"，胜率统计失真

    修复：
      1) 同 (date, pool) 已存在 → 【原地更新分数】，不再追加
         （同一天同一池推多次是正常现象，保留分数最高/最新的那次即可）
      2) 上限从"条数"改为"条数+天数"双约束，且同一条目不再消耗额外名额
      3) 支持传入真实 trade_date（信号所属交易日），
         避免"跨日补跑时把今天当成交割日"

    参数：
        ts_code / pool / score : 同原语义
        max_history_per_stock  : 保留条数上限（默认 20，约合 4 周交易史）
        trade_date             : 交易日期 YYYYMMDD 或 YYYY-MM-DD，空 = 用今天

    【2026-11-03 根因消除 · QA 模式不写生产账本】
    QA / 演练环境下（_qa_mode() 为 True）**直接 return，不写任何文件**。
    这样回归脚本再也污染不了生产推送账本，真实票不会再被
    COOLDOWN_DAYS=3 的票级冷却静默拦掉。
    生产环境（_qa_mode() 为 False）行为**完全不变**。
    """
    if not ts_code:
        return
    try:
        if _qa_mode():
            logger.debug(
                "[push_dedup] QA 模式（_qa_mode=True），跳过写入推送历史账本: %s/%s",
                ts_code, pool,
            )
            return
    except Exception:
        pass
    try:
        h = _load_history()
        arr = h.get(ts_code, []) or []

        # 交易日期：优先用信号自身的交易日，退化为今天
        d = str(trade_date or "").replace("-", "").strip()[:8]
        if len(d) != 8 or not d.isdigit():
            d = _now_iso().replace("-", "")
        pool_s = str(pool or "").strip().lower()

        # 同日同池 → 原地更新（取分数较高者，避免被低分覆盖）
        replaced = False
        for it in arr:
            if not isinstance(it, dict):
                continue
            it_d = str(it.get("date", "")).replace("-", "")[:8]
            if it_d == d and str(it.get("pool", "")).strip().lower() == pool_s:
                try:
                    old = float(it.get("score", 0.0) or 0.0)
                except (TypeError, ValueError):
                    old = 0.0
                if float(score) >= old:
                    it["score"] = float(score)
                replaced = True
                break

        if not replaced:
            arr.append({
                "date": d,
                "pool": pool_s,
                "score": float(score),
            })

        # 保留最近 N 条（此时同一条目不再重复占位）
        h[ts_code] = arr[-int(max_history_per_stock):]
        _save_history(h)
    except Exception as e:
        logger.debug("[push_dedup] record_push 异常（忽略）: %s", e)


def apply_dedup_to_res(res: Dict[str, Any], pool: str) -> Dict[str, Any]:
    """
    便捷封装：给 res 注入 dedup_decision，并把不通过的去重票标 is_pass=False。
    """
    try:
        if not isinstance(res, dict):
            return res
        ts_code = str(res.get("ts_code") or "").strip()
        score = float(res.get("burst_score") or res.get("score") or 0.0)
        decision = check_dedup(ts_code, pool, score)
        res["dedup_decision"] = decision
        if decision.get("should_dedup"):
            res["is_pass"] = False
            res.setdefault("risk_tags", []).append(f"🔁推送去重:{decision.get('reason','')}")
    except Exception as e:
        logger.debug("[push_dedup] 应用失败: %s", e)
    return res


def pool_apply_push_dedup(res: Dict[str, Any], pool: str, record: bool = True) -> Dict[str, Any]:
    """
    【V26.88+ 9-30 第12项落地】统一推送去重入口。

    各池（p1/p2/p3/p4/p5）的"准备推送"步骤统一调用本函数：
    1. 走 check_dedup 完整去重（3 日冷却 + 跨池冷却 + 分数覆盖三段逻辑）
    2. 命中 dedup → res["is_pass"] = False，加 risk_tags
    3. 未命中 dedup → 若 record=True 则 record_push 写历史
    4. 把 dedup_decision 注入 res["dedup_decision"]，便于上层消费

    替代各 strats 文件中的 5 段重复 try/except 块（P1→pool_manager.py，P2/P3/P4/P5→4 个 strat 文件）。
    """
    try:
        if not isinstance(res, dict):
            return res
        ts_code = str(res.get("ts_code") or "").strip()
        if not ts_code:
            return res
        score = float(res.get("burst_score") or res.get("score") or 0.0)
        decision = check_dedup(ts_code, pool, score)
        res["dedup_decision"] = decision
        if decision.get("should_dedup"):
            res["is_pass"] = False
            res.setdefault("risk_tags", []).append(f"🔁推送去重:{decision.get('reason','')}")
        else:
            if record:
                record_push(ts_code, pool, score)
    except Exception as e:
        logger.debug("[push_dedup] pool_apply_push_dedup 异常: %s", e)
    return res


def is_same_day_cross_pool_duplicate(ts_code: str, pool: str, score: float, score_gap: float = 5.0) -> bool:
    """
    【V26.88+ 9-30 朋友1建议】当日跨池重复判定。

    场景：P2 在 09:26 推了 600519.SH 评分 85 分；P3 在 09:35 又命中，评分 87；
          若分差 < score_gap（默认 5.0），视为"实质性重复" → 建议 dedup。

    与 check_dedup 的区别：
    - check_dedup 是"是否应该 dedup"的最终决定（会被3日冷却拦截）
    - is_same_day_cross_pool_duplicate 是"是否同一日内被多个池触发"的语义查询
      主要用于：
        a) 当 P4 14:36 拿到更高分时，决定"覆盖"还是"沉默"
        b) UI 提示"该票今日已被 P2 推送过"

    返回 True 表示"已经被同日内其他池推送过"，调用方可按 score_gap 决定是否覆盖。
    """
    if not ts_code:
        return False
    try:
        h = _load_history()
        history = h.get(ts_code, []) or []
        if not history:
            return False
        # 取今天（BJ_TZ）的所有推送
        today_iso = _now_iso()
        for item in history:
            if str(item.get("date", ""))[:10] != today_iso:
                continue
            if str(item.get("pool", "")) != pool:
                last_score = float(item.get("score", 0.0))
                if abs(score - last_score) < float(score_gap):
                    return True
        return False
    except Exception as _e:
        logger.debug("[push_dedup] is_same_day_cross_pool_duplicate 异常: %s", _e)
        return False
