# -*- coding: utf-8 -*-
"""
core/signal_ledger_bridge.py
========================================================================
信号账本桥接层：把「入池候选」与「实际推送」两套账本打通
========================================================================

【背景 · 必须先说清的根因】
signal_log 与 push_history.json 是两套独立账本：
  · signal_log      —— 记录"进了池子并算出分数"的候选信号
  · push_history.json —— 记录"真正推送给用户"的信号（core/push_dedup.py 维护）

设计者本意是用 signal_log.pushed 字段把两者关联起来：
  · core/scan/intraday_snapshots.py:177  读 item.get('_pushed', False)
  · core/pool_signal_adapter_v28.py:119   写 item["pushed"] = True
但 _pushed 这个 key 在生产链路里【从未被写入】——全项目只有读取点，没有写入点。
后果（实测）：
  · signal_log.pushed 100% 为 False / NULL
  · signal_strategy_ledger.pushed 100% 为 False
  · 无法回答"这 120 条候选里，真正推给用户的是哪几条"
  · → T+N 回看把所有候选都当推送样本，样本量虚高但归因失真
  · → push_min_score 调参失去参照系

【本模块职责】
  1. normalize_ts_code()  —— 统一 ts_code 口径（裸 6 位 <-> 带后缀）
  2. mark_pushed()        —— 推送成功后回写 signal_log.pushed = TRUE
  3. reconcile_push_history() —— 用 push_history.json 回填历史 pushed 标记
  4. build_bridge_report() —— 输出一致性体检报告

【红线】
  · 只写 signal_log.pushed 这一个语义字段，不改任何策略/打分/过滤逻辑
  · 所有写操作幂等；异常一律吞掉，绝不阻塞推送主链路
  · 不删数据、不改 trade_date / score / strategy 等任何其它列
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BJ_TZ = timezone(timedelta(hours=8))

PUSH_HISTORY_FILE = "data/push_history.json"


# ============================ 1. ts_code 归一 ============================

def with_exchange_suffix(code: str) -> str:
    """
    补交易所后缀，使 ts_code 能与 daily_data.ts_code 对齐。

    【口径来源】实测生产库 daily_data / bars_daily / vw_daily_data_compat
    的 ts_code 后缀分布（2026-10-21 实测）：
        6xxxxx -> .SH   (1100 只)
        0/1/2/3 -> .SZ (1022 只)
        9xxxxx -> .BJ   (12 只，北交所 920xxx 新代码段)
    裸代码 4xxxxx / 8xxxxx 同样属北交所 -> .BJ

    【重要修正】早期实现把 9 开头判成 .SH（部分历史代码），
    而生产库实测 9 开头一律是 .BJ —— 旧口径会让北交所 12 只票
    在 T+N 回看 / 战法有效性统计中全部 join miss。
    本函数口径与 daily_data 实测完全对齐。
    """
    s = str(code or "").strip().upper()
    if not s:
        return ""
    if "." in s:
        return s
    if not (len(s) == 6 and s.isdigit()):
        return s
    if s.startswith("6"):
        return f"{s}.SH"
    if s.startswith(("4", "8", "9")):
        return f"{s}.BJ"
    return f"{s}.SZ"


def normalize_ts_code(code: str) -> str:
    """信号账本通用归一化：补后缀 + 大写 + 去空白。"""
    return with_exchange_suffix(code)


def bare_code(code: str) -> str:
    """去掉交易所后缀，只留 6 位裸代码。"""
    s = str(code or "").strip().upper()
    return s.split(".")[0] if "." in s else s


def _same_stock(a: str, b: str) -> bool:
    """两个 ts_code 是否指向同一只票（容忍后缀差异）。"""
    return bare_code(a) == bare_code(b) and bare_code(a) != ""


# ============================ 2. pushed 回写 ============================

# 异步回写线程池：与推送线程池分离，避免推送主链路被 DB IO 拖慢
_PUSHED_MARK_EXECUTOR = None
_PUSHED_MARK_LOCK = threading.Lock()
_PUSHED_MARK_PENDING: Dict[Tuple[str, str], float] = {}   # (pool, bare_code) -> ts
_PUSHED_MARK_MAX_PENDING = 2000


def _get_pushed_executor():
    """惰性创建后台回写线程池（守护线程，daemon=True 允许随进程退出）。"""
    global _PUSHED_MARK_EXECUTOR
    if _PUSHED_MARK_EXECUTOR is not None:
        return _PUSHED_MARK_EXECUTOR
    with _PUSHED_MARK_LOCK:
        if _PUSHED_MARK_EXECUTOR is None:
            from concurrent.futures import ThreadPoolExecutor

            _PUSHED_MARK_EXECUTOR = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="sig-ledger-mark"
            )
    return _PUSHED_MARK_EXECUTOR


def _flush_pushed_marks() -> int:
    """
    把待回写的 (pool, code) 批量写进 signal_log.pushed。

    复用进程内写连接单例（get_read_conn_singleton 在有写连接时也返回它），
    避免在推送线程开独占短连接与主扫描抢 DuckDB 文件锁。
    """
    with _PUSHED_MARK_LOCK:
        if not _PUSHED_MARK_PENDING:
            return 0
        items = list(_PUSHED_MARK_PENDING.items())
        _PUSHED_MARK_PENDING.clear()

    con = None
    try:
        from data.db_core import get_read_conn_singleton

        con = get_read_conn_singleton(max_wait_sec=8.0)
    except Exception as e:
        logger.debug("[ledger_bridge] 取连接失败: %s", e)
        con = None

    if con is None:
        # 取不到连接 → 放回队列，下轮重试
        with _PUSHED_MARK_LOCK:
            for k, v in items:
                _PUSHED_MARK_PENDING.setdefault(k, v)
        return 0

    n = 0
    try:
        for (pool, bare, td), _ts in items:
            try:
                if td:
                    # 精确到交易日：同一天同票可能被多个池命中，不限定会误标
                    con.execute(
                        "UPDATE signal_log SET pushed = TRUE "
                        "WHERE pool = ? "
                        "AND REPLACE(COALESCE(trade_date, ''), '-', '') = ? "
                        "AND (ts_code = ? OR ts_code = ? OR ts_code LIKE ?)",
                        [pool, td, bare, with_exchange_suffix(bare), bare + ".%"],
                    )
                else:
                    # 无交易日：退化为该池该票最新一条候选
                    con.execute(
                        "UPDATE signal_log SET pushed = TRUE "
                        "WHERE pool = ? AND (ts_code = ? OR ts_code = ? OR ts_code LIKE ?)",
                        [pool, bare, with_exchange_suffix(bare), bare + ".%"],
                    )
                n += 1
            except Exception as e:
                logger.debug("[ledger_bridge] 回写 %s/%s 失败: %s", pool, bare, e)
    except Exception as e:
        logger.debug("[ledger_bridge] 批量回写异常: %s", e)
    return n


def submit_pushed_mark(ts_code: str, pool: str, trade_date: str = "") -> None:
    """
    提交一条「已推送」标记（异步、幂等、可重试）。

    调用方：core/notification/gateway.py push_stock_if_allowed
    在真正占用推送额度之后调用 —— 即"这条信号确实推给用户了"。
    """
    try:
        bare = bare_code(ts_code)
        p = str(pool or "").strip().lower()
        td = str(trade_date or "").replace("-", "").strip()[:8]
        if not bare or not p:
            return
        with _PUSHED_MARK_LOCK:
            if len(_PUSHED_MARK_PENDING) > _PUSHED_MARK_MAX_PENDING:
                # 队列爆了：丢掉最老的一半，避免无界增长
                keys = sorted(_PUSHED_MARK_PENDING.items(), key=lambda kv: kv[1])
                for k, _v in keys[: len(keys) // 2]:
                    _PUSHED_MARK_PENDING.pop(k, None)
            _PUSHED_MARK_PENDING[(p, bare, td)] = time.time()
        _get_pushed_executor().submit(_safe_flush)
    except Exception as e:
        logger.debug("[ledger_bridge] submit_pushed_mark 异常（忽略）: %s", e)


def _safe_flush() -> None:
    """线程池任务包装：吞掉所有异常，绝不让后台线程因未捕获异常而静默死亡。"""
    try:
        _flush_pushed_marks()
    except Exception as e:
        logger.debug("[ledger_bridge] flush 异常（忽略）: %s", e)


def mark_pushed(db_conn, ts_code: str, pool: str, trade_date: str = "",
                score: Optional[float] = None) -> int:
    """
    把某条信号标记为「已推送」。

    匹配口径：优先按 (trade_date, pool, ts_code) 精确匹配；
    trade_date 为空时按 (pool, ts_code) + 最新一条匹配。
    ts_code 容忍后缀差异。

    返回：更新的行数（0 = 没匹配到，不代表失败）。
    """
    if db_conn is None or not ts_code:
        return 0
    try:
        pool = str(pool or "").strip().lower()
        code = str(ts_code or "").strip()
        if not pool or not code:
            return 0
        td = str(trade_date or "").replace("-", "").strip()[:8]

        where = "pool = ?"
        params: List[Any] = [pool]
        if td:
            where += " AND REPLACE(COALESCE(trade_date, ''), '-', '') = ?"
            params.append(td)

        # ts_code 允许带/不带后缀两种写法
        bare = bare_code(code)
        suff = with_exchange_suffix(code)
        ts_clause = "(ts_code = ? OR ts_code = ? OR ts_code LIKE ?)"
        ts_params = [code, suff, bare + ".%"]

        sql = (
            "UPDATE signal_log SET pushed = TRUE "
            "WHERE " + where + " AND " + ts_clause
        )
        all_params = params + ts_params
        n = int(db_conn.execute(sql, all_params).fetchone()[0] or 0)

        if n:
            logger.info("[ledger_bridge] 标记已推送 %s 条 pool=%s code=%s td=%s",
                        n, pool, code, td or "(latest)")
        return n
    except Exception as e:
        logger.debug("[ledger_bridge] mark_pushed 异常（忽略）: %s", e)
        return 0


# ============================ 3. 用 push_history 回填 ============================

def _load_push_history(path: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    p = path or PUSH_HISTORY_FILE
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.debug("[ledger_bridge] 读 push_history 失败: %s", e)
        return {}


def _history_index(history: Dict[str, List[Dict[str, Any]]]) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    """
    push_history -> {(date, pool, bare_code): record}
    date 统一 YYYYMMDD（push_history 用的是 'YYYY-MM-DD'，两种都兜住）。

    【V30.10 修复 · 池口径错位】
    生产实测：push_history 里记的 pool 主要是 "p1"
    （P1 底仓重建时写入，core/pool_manager/pool_builder.py），
    而 signal_log 里记的是 "p3"/"p5"（信号所属池）。
    按 (date, pool, code) 严格匹配 → 72 条【已真实推送】的信号全部匹配不上，
    导致回溯把大量真推送样本误判为"未推送"，战法有效性统计严重失真。

    修复：额外建一张【池无关】索引 {(date, bare_code): record}，
    供 reconcile 在严格匹配失败时兜底（跨池冷却本身就会导致同票同日
    只记在最先推送的那个池上，所以"同票同日被推过"是唯一可靠事实）。
    """
    idx: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    any_pool: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for code, arr in (history or {}).items():
        bare = bare_code(code)
        if not bare:
            continue
        for it in arr or []:
            if not isinstance(it, dict):
                continue
            raw_d = str(it.get("date", "") or "")[:10]
            d8 = raw_d.replace("-", "")[:8]
            if len(d8) != 8 or not d8.isdigit():
                continue
            pool = str(it.get("pool", "") or "").strip().lower()
            try:
                sc = float(it.get("score", 0.0) or 0.0)
            except (TypeError, ValueError):
                sc = 0.0
            key = (d8, pool, bare)
            # 同键保留分数更高的一条（更接近真实推送的那次）
            prev = idx.get(key)
            if prev is None or sc > float(prev.get("score", 0.0) or 0.0):
                idx[key] = it
            # 池无关索引：同键同样保留分数更高的
            akey = (d8, bare)
            aprev = any_pool.get(akey)
            if aprev is None or sc > float(aprev.get("score", 0.0) or 0.0):
                any_pool[akey] = it
    idx["__any_pool__"] = any_pool  # type: ignore[index]
    return idx


def reconcile_push_history(db_conn, path: Optional[str] = None,
                            apply: bool = False) -> Dict[str, Any]:
    """
    用 push_history.json 回填 signal_log.pushed。

    apply=False（默认）→ 只做体检，返回差异报告，不写库。
    apply=True          → 执行 UPDATE 回填。

    匹配键：(date, pool, bare_code)
    """
    report: Dict[str, Any] = {
        "history_file": path or PUSH_HISTORY_FILE,
        "history_exists": False,
        "history_entries": 0,
        "history_dates": [],
        "log_total": 0,
        "log_pushed_true": 0,
        "log_pushed_false": 0,
        "log_pushed_null": 0,
        "matched": 0,
        "unmatched_history": 0,
        "log_pushed_but_no_history": 0,
        "applied": False,
        "updated_rows": 0,
        "examples_unmatched": [],
    }
    if db_conn is None:
        return report

    history = _load_push_history(path)
    report["history_exists"] = bool(history)
    any_pool_idx: Dict[Tuple[str, str], Dict[str, Any]] = {}
    if history:
        hidx = _history_index(history)
        any_pool_idx = hidx.pop("__any_pool__", {})  # type: ignore[assignment]
        report["history_entries"] = len(hidx)
        report["history_dates"] = sorted({k[0] for k in hidx})

    try:
        total = int(db_conn.execute("SELECT COUNT(*) FROM signal_log").fetchone()[0] or 0)
        report["log_total"] = total
        t = db_conn.execute(
            "SELECT "
            "SUM(CASE WHEN pushed = TRUE THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN pushed = FALSE THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN pushed IS NULL THEN 1 ELSE 0 END) "
            "FROM signal_log"
        ).fetchone()
        report["log_pushed_true"] = int(t[0] or 0)
        report["log_pushed_false"] = int(t[1] or 0)
        report["log_pushed_null"] = int(t[2] or 0)
    except Exception as e:
        logger.debug("[ledger_bridge] signal_log 统计失败: %s", e)
        return report

    if not history:
        return report

    try:
        rows = db_conn.execute(
            "SELECT trade_date, pool, ts_code FROM signal_log"
        ).fetchall()
    except Exception as e:
        logger.debug("[ledger_bridge] 读取 signal_log 失败: %s", e)
        return report

    matched = 0
    matched_cross_pool = 0
    unmatched_hist = 0
    unmatched_examples: List[str] = []
    updates: List[Tuple[str, str, str]] = []
    updates_cross: List[Tuple[str, str]] = []

    for td, pool, code in rows:
        d8 = str(td or "").replace("-", "")[:8]
        p = str(pool or "").strip().lower()
        b = bare_code(code)
        if not d8 or not p or not b:
            continue
        hit = hidx.get((d8, p, b))
        if hit is not None:
            matched += 1
            updates.append((d8, p, b))
            continue
        # 【V30.10】严格匹配失败 → 池无关兜底：同票同日被推过就算已推送
        # （跨池冷却会把同票同日的记录只留在最先推送的那个池上）
        if any_pool_idx.get((d8, b)) is not None:
            matched_cross_pool += 1
            updates_cross.append((d8, b))
            continue
        unmatched_hist += 1
        if len(unmatched_examples) < 10:
            unmatched_examples.append("%s/%s/%s" % (d8, p, code))

    report["matched"] = matched
    report["matched_cross_pool"] = matched_cross_pool
    report["unmatched_history"] = unmatched_hist
    report["examples_unmatched"] = unmatched_examples

    # 反向：history 里有但 signal_log 没有的
    log_keys = set()
    for td, pool, code in rows:
        log_keys.add((str(td or "").replace("-", "")[:8],
                      str(pool or "").strip().lower(), bare_code(code)))
    only_hist = [k for k in hidx if k not in log_keys]
    report["history_not_in_log"] = len(only_hist)

    if apply and (updates or updates_cross):
        try:
            for d8, p, b in updates:
                db_conn.execute(
                    "UPDATE signal_log SET pushed = TRUE "
                    "WHERE REPLACE(COALESCE(trade_date, ''), '-', '') = ? "
                    "AND pool = ? AND (ts_code = ? OR ts_code = ? OR ts_code LIKE ?)",
                    [d8, p, b, with_exchange_suffix(b), b + ".%"],
                )
            # 池无关兜底：不限定 pool，只按 (交易日, 代码) 标记
            for d8, b in updates_cross:
                db_conn.execute(
                    "UPDATE signal_log SET pushed = TRUE "
                    "WHERE REPLACE(COALESCE(trade_date, ''), '-', '') = ? "
                    "AND (ts_code = ? OR ts_code = ? OR ts_code LIKE ?)",
                    [d8, b, with_exchange_suffix(b), b + ".%"],
                )
            report["updated_rows"] = len(updates) + len(updates_cross)
            report["applied"] = True
            logger.info(
                "[ledger_bridge] 已回填 pushed=TRUE %d 行（严格 %d + 跨池 %d）",
                len(updates) + len(updates_cross), len(updates), len(updates_cross),
            )
        except Exception as e:
            logger.warning("[ledger_bridge] 回填 pushed 失败: %s", e)

    return report


# ============================ 4. 连接解析 ============================
#
# 【为什么需要这个】
# 历史代码用 `globals().get("_GLOBAL_DB_CONN")` 取写连接，
# 但该全局变量在全项目【从未被赋值】（只读不写），恒为 None。
# 后果：P1 的 safe_log_pool_signal 被 `if _db_conn is not None` 直接跳过，
#        P2/P3/P4/P5 的 append_signal_log_safe 因 db_conn=None 早退，
#        —— 五个池的「信号落库」链路事实上全部断裂。
#
# 本函数按优先级解析一个可写连接：
#   1) 进程内写连接单例（get_read_conn_singleton 在写连接存在时也返回它）
#      —— 复用主扫描的连接，零额外锁开销
#   2) 独占短连接（get_write_conn）—— 单例不可用时的兜底
#
def resolve_write_conn():
    """
    解析一个可用的写连接。返回连接对象；不可用时返回 None。

    注意：返回的单例连接由调用方【不得关闭】（进程级共享）。
    """
    # 1) 进程内单例
    try:
        from data.db_core import get_read_conn_singleton

        con = get_read_conn_singleton(max_wait_sec=5.0)
        if con is not None:
            return con
    except Exception as e:
        logger.debug("[ledger_bridge] 取单例连接失败: %s", e)

    # 2) 独占短连接（调用方负责 close）
    try:
        from data.db_core import _duckdb_connect_write

        return _duckdb_connect_write()
    except Exception as e:
        logger.debug("[ledger_bridge] 开独占连接失败: %s", e)
    return None


# ============================ 5. 一致性体检报告 ============================

def build_bridge_report(db_conn, path: Optional[str] = None) -> Dict[str, Any]:
    """
    输出一份「入池候选 vs 实际推送」的一致性体检报告。

    回答的问题：
      · signal_log 里有多少候选真正被推送了？
      · push_history 里有多少推送没进 signal_log？
      · 哪些战法的「推送后样本」最充足（可作为调参依据）？
    """
    rep = reconcile_push_history(db_conn, path, apply=False)
    rep["by_pool"] = {}
    rep["by_strategy_pushed"] = {}

    if db_conn is None:
        return rep

    try:
        rows = db_conn.execute(
            "SELECT pool, pushed, COUNT(*) FROM signal_log GROUP BY 1,2"
        ).fetchall()
        for pool, pushed, n in rows:
            node = rep["by_pool"].setdefault(
                str(pool or ""), {"total": 0, "pushed_true": 0, "pushed_false": 0, "pushed_null": 0}
            )
            node["total"] += int(n or 0)
            if pushed is True:
                node["pushed_true"] += int(n or 0)
            elif pushed is False:
                node["pushed_false"] += int(n or 0)
            else:
                node["pushed_null"] += int(n or 0)
    except Exception as e:
        logger.debug("[ledger_bridge] by_pool 统计失败: %s", e)

    # 已推送战法分布（这是真正可用于调参的样本）
    try:
        rows = db_conn.execute(
            "SELECT strategy_id, COUNT(*) FROM signal_strategy_ledger "
            "WHERE pushed = TRUE GROUP BY 1 ORDER BY 2 DESC"
        ).fetchall()
        rep["by_strategy_pushed"] = {str(r[0]): int(r[1]) for r in rows}
    except Exception as e:
        logger.debug("[ledger_bridge] by_strategy_pushed 统计失败: %s", e)

    return rep


def summarize(rep: Dict[str, Any]) -> str:
    """把体检报告渲染成一段可读文本。"""
    L: List[str] = []
    A = L.append
    A("=" * 78)
    A("信号账本一致性体检")
    A("=" * 78)
    A("push_history 存在      : %s（%d 条记录，%d 个日期）" % (
        rep.get("history_exists"), rep.get("history_entries", 0),
        len(rep.get("history_dates") or [])))
    A("signal_log 总行数       : %s" % rep.get("log_total"))
    A("  pushed = TRUE         : %s" % rep.get("log_pushed_true"))
    A("  pushed = FALSE        : %s" % rep.get("log_pushed_false"))
    A("  pushed = NULL         : %s" % rep.get("log_pushed_null"))
    A("可匹配到推送历史        : %s" % rep.get("matched"))
    A("推送历史有但账本无      : %s" % rep.get("history_not_in_log"))
    A("-" * 78)
    for pool, node in (rep.get("by_pool") or {}).items():
        A("池 %-4s  候选=%-5s 已推送=%-5s 未推送=%-5s NULL=%s" % (
            pool, node.get("total"), node.get("pushed_true"),
            node.get("pushed_false"), node.get("pushed_null")))
    A("-" * 78)
    sp = rep.get("by_strategy_pushed") or {}
    if sp:
        A("已推送战法 TOP：")
        for k, v in list(sp.items())[:12]:
            A("   %-12s %s 条" % (k, v))
    else:
        A("已推送战法：无（pushed 未打通，无法做战法级推送归因）")
    A("=" * 78)
    return "\n".join(L)


__all__ = [
    "with_exchange_suffix",
    "normalize_ts_code",
    "bare_code",
    "mark_pushed",
    "submit_pushed_mark",
    "reconcile_push_history",
    "build_bridge_report",
    "summarize",
]
