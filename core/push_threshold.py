# -*- coding: utf-8 -*-
"""
core/push_threshold.py
========================================================================
推送分数阈值 · 唯一解析入口
========================================================================

【背景 · 为什么需要统一】
阈值这件事在系统里有【三套】互相独立、且不一致的实现：

  1) 硬编码在四个 strat 的调用点（core/strategies/strat_p2/p3/p4/p5_*.py）
        append_signal_log_safe(res, "p3", db_conn, push_min_score=<阈值>, ...)
        —— 决定「这条信号是否写入 signal_log（成为统计样本）」
  2) config.yaml → novice_guard.push_min_score
        —— 决定「这条信号是否真的推给用户」
  3) core/push_threshold_calibrator.CURRENT_THRESHOLDS
        —— 标定器的对比基线

三处数值必须手工保持同步，改一处忘另一处就会
「落库了但没推送」或「推送了但没进统计样本」。

【本模块的职责】
  · resolve_push_min_score(pool) —— 唯一取阈值的函数
  · 优先级：config.yaml（novice_guard.push_min_score / push_min_score 顶层）
             > 硬编码默认值（与历史行为一致）
  · 提供 resolve_all() 一次性取出五档阈值 + 来源标记
  · 提供 refresh_from_calibration()：把标定器算出的推荐值
    写回 config.yaml（显式调用，默认 dry_run）

【口径说明】
  · 「落库阈值」与「推送阈值」共用同一套数值（本来就是同一个人定的），
    但【用途不同】：落库阈值低一点能积累更多统计样本；
    推送阈值高一点能减少打扰。统一取值后，若要分开调，
    只需在 config.yaml 里给 pool 加 _log 后缀键即可（见下）。

【config.yaml 键位】
  push_min_score:            # 顶层：全局默认（可选）
    p2: <阈值>
  novice_guard:
    push_min_score:         # 推送 + 落库共用（主用）
      p2: <阈值>
      p2_log: <阈值>          # 可选：只影响落库（积累样本），不影响推送

【红线】
  · 只读 config.yaml（除 refresh_from_calibration 显式写入）
  · 任何异常都回退到内置默认值，绝不抛错
"""
from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")

# 内置默认值（与历史生产行为完全一致，作为最终兜底）
DEFAULT_PUSH_MIN_SCORE: Dict[str, float] = {
    "p1_high_pool": 60.0,
    "p2": 50.0,
    "p3": 50.0,
    "p4": 55.0,
    "p5": 70.0,
}

# 落库阈值的后缀键（用于「多积累统计样本」的场景）
LOG_SUFFIX = "_log"

_POOLS = ("p1_high_pool", "p2", "p3", "p4", "p5")

_cfg_cache: Dict[str, Any] = {}
_cfg_cache_mtime: Optional[float] = None
_cfg_lock = None


def _load_config() -> Dict[str, Any]:
    """读 config.yaml（带 mtime 缓存，改配置后自动重读）。"""
    global _cfg_cache, _cfg_cache_mtime
    try:
        mtime = os.path.getmtime(CONFIG_PATH)
    except OSError:
        return _cfg_cache or {}
    if _cfg_cache_mtime == mtime and _cfg_cache:
        return _cfg_cache
    try:
        import yaml

        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if isinstance(data, dict):
            _cfg_cache = data
            _cfg_cache_mtime = mtime
    except Exception as e:
        logger.debug("[push_threshold] 读 config.yaml 失败: %s", e)
    return _cfg_cache or {}


def _cfg_push_map(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """抽取两处 push_min_score 段并合并（novice_guard 优先于顶层）。"""
    out: Dict[str, Any] = {}
    top = cfg.get("push_min_score")
    if isinstance(top, dict):
        out.update(top)
    ng = cfg.get("novice_guard")
    if isinstance(ng, dict):
        pm = ng.get("push_min_score")
        if isinstance(pm, dict):
            out.update(pm)
    return out


def resolve_push_min_score(pool: str, *, for_log: bool = False) -> float:
    """
    取某个池的分数阈值。

    参数：
        pool    : 'p1_high_pool' / 'p2' / 'p3' / 'p4' / 'p5'
        for_log : True → 优先取「落库专用」阈值（<pool>_log 键），没有则用常规值

    返回：阈值分数（float）。配置缺失时返回内置默认值。
    """
    key = str(pool or "").strip().lower()
    pm = _cfg_push_map(_load_config())

    if for_log:
        log_key = key + LOG_SUFFIX
        if log_key in pm:
            try:
                return float(pm[log_key])
            except (TypeError, ValueError):
                pass

    if key in pm:
        try:
            return float(pm[key])
        except (TypeError, ValueError):
            pass

    return float(DEFAULT_PUSH_MIN_SCORE.get(key, 50.0))


def resolve_all() -> Dict[str, Dict[str, Any]]:
    """
    一次性取出五档的「推送阈值」与「落库阈值」+ 来源标记。

    返回：
        {pool: {"push": float, "log": float, "source": str, "log_source": str}}
    """
    pm = _cfg_push_map(_load_config())
    out: Dict[str, Dict[str, Any]] = {}
    for pool in _POOLS:
        push = resolve_push_min_score(pool, for_log=False)
        log = resolve_push_min_score(pool, for_log=True)
        in_cfg = pool in pm
        log_in_cfg = (pool + LOG_SUFFIX) in pm
        out[pool] = {
            "push": push,
            "log": log,
            "source": "config" if in_cfg else "builtin_default",
            "log_source": ("config:%s_log" % pool) if log_in_cfg
                          else ("config" if in_cfg else "builtin_default"),
        }
    return out


def describe() -> str:
    """渲染成可读文本（供 UI / 日志）。"""
    L: List[str] = []
    A = L.append
    A("推送分数阈值现状")
    A("-" * 62)
    A("%-14s %-9s %-9s %-18s %s" % ("池", "推送阈值", "落库阈值", "来源", "落库来源"))
    for pool, node in resolve_all().items():
        A("%-14s %-9s %-9s %-18s %s" % (
            pool, node["push"], node["log"], node["source"], node["log_source"]))
    A("-" * 62)
    return "\n".join(L)


def refresh_from_calibration(
    recommendation: Dict[str, Any],
    *,
    dry_run: bool = True,
    write_log_threshold: bool = False,
) -> Dict[str, Any]:
    """
    把标定器算出的推荐阈值写回 config.yaml 的 novice_guard.push_min_score。

    【默认 dry_run=True】—— 阈值变更属于策略行为变更，必须显式落地。

    参数：
        recommendation        : core.push_threshold_calibrator.calc_recommended_thresholds 的输出
        dry_run               : True → 只返回将要做的变更，不写盘
        write_log_threshold   : True → 同时写 <pool>_log（落库阈值）；
                                默认 False（只改推送阈值，避免影响统计样本量）

    返回：{"applied": bool, "changes": [...], "error": str|None}
    """
    recs = (recommendation or {}).get("recommendations") or {}
    changes: List[Dict[str, Any]] = []
    for pool, node in recs.items():
        thr = node.get("threshold")
        if thr is None:
            continue
        cur = node.get("current")
        if cur is None or abs(float(thr) - float(cur)) < 1e-6:
            continue
        changes.append({
            "pool": pool, "from": cur, "to": float(thr),
            "reason": node.get("reason", ""),
        })

    if dry_run:
        return {"applied": False, "dry_run": True, "changes": changes, "error": None}
    if not changes:
        return {"applied": False, "dry_run": False, "changes": [], "error": None}

    try:
        import yaml

        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        ng = cfg.setdefault("novice_guard", {})
        pm = ng.setdefault("push_min_score", {})
        for ch in changes:
            pm[ch["pool"]] = float(ch["to"])
            if write_log_threshold:
                pm[ch["pool"] + LOG_SUFFIX] = float(ch["to"])

        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, CONFIG_PATH)

        # 让缓存立即失效
        global _cfg_cache, _cfg_cache_mtime
        _cfg_cache = {}
        _cfg_cache_mtime = None

        return {"applied": True, "dry_run": False, "changes": changes, "error": None}
    except Exception as e:
        logger.warning("[push_threshold] 落地失败: %s", e)
        return {"applied": False, "dry_run": False, "changes": changes,
                "error": f"{type(e).__name__}: {e}"}


__all__ = [
    "DEFAULT_PUSH_MIN_SCORE",
    "LOG_SUFFIX",
    "resolve_push_min_score",
    "resolve_all",
    "describe",
    "refresh_from_calibration",
]
