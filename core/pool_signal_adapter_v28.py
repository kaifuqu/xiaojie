# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V28 — 信号采集真实接入（5 个池）
================================================================================
【V28 关键修复】把 safe_log_pool_signal 真接入到 5 个池的 run_all 末尾。

策略：用一个统一"推送前钩子"封装，每池在 return res 之前调用一次：
    from core.pool_signal_adapter import append_signal_log_safe
    res = append_signal_log_safe(res, "p3", db_conn, rt=rt)

若 res['is_pass']/['p3_core_screener_pass'] 为 True 且 score ≥ 推送阈值，
则把 (ts_code, name, score, strategies, price) 写入 signal_log。

任何异常吞掉（不影响主流程）。
================================================================================
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def append_signal_log_safe(
    res: Dict[str, Any],
    pool: str,
    db_conn=None,
    *,
    push_min_score: Optional[float] = None,
    rt: Optional[Dict[str, Any]] = None,
    trade_date: Optional[str] = None,
) -> Dict[str, Any]:
    """
    统一入口：把 res 中的票尝试写入 signal_log。
    - 不阻塞主流程（任何异常都吞掉并 logger.debug）
    - 仅当 res 通过且分数达到 push_min_score 时才记（None=记全部通过）

    【V30.10 关键修复 2026-10-21】三处断链：
      1) db_conn 恒为 None —— 调用方传的 `globals().get("_GLOBAL_DB_CONN")`
         是全项目从未赋值的死变量，五个池的落库链路事实上全断。
         现在 db_conn 为空时自动用 resolve_write_conn() 解析一个可用连接。
      2) ts_code 裸代码 —— 写入前统一归一成带交易所后缀（000001.SZ / 600000.SH），
         使 signal_log 与 daily_data / bars_daily 的 join 天然对齐。
      3) pushed 语义错位 —— 这里的 pushed=True 表示"这条进了推送候选账本"，
         并不等于"用户真的收到了企微"。真实推送由
         core/notification/gateway.py 在推送占位成功后回写 signal_log.pushed，
         两者语义分离、互不覆盖（见 core/signal_ledger_bridge.py）。
    """
    _opened_here = False
    try:
        if not isinstance(res, dict):
            return res
        # === 修复 1：连接自动解析（原来 db_conn=None 直接早退，信号从不落库） ===
        # 【Tier1 · 1.1 修复 2026-11-02】原实现 `_opened_here = db_conn is not None`，
        # 于是 finally 里会把 resolve_write_conn() 返回的**进程级写单例**关掉——
        # 而 resolve_write_conn() 的契约明确写着「返回的单例连接由调用方不得关闭」。
        # 后果：同一进程里【第一次】append_signal_log_safe 成功写库后单例被 close，
        # 后续每一次写库都静默失败（异常只进 debug 日志）→ signal_log 长期缺行。
        # 现在区分「单例（不关）」与「独占短连接（自己关）」两种来源。
        if db_conn is None:
            try:
                from data.db_core import get_read_conn_singleton

                db_conn = get_read_conn_singleton(max_wait_sec=5.0)
                _opened_here = False          # 进程单例：绝不关闭
                if db_conn is None:
                    from data.db_core import _duckdb_connect_write

                    db_conn = _duckdb_connect_write()
                    _opened_here = True       # 独占短连接：由本函数负责关闭
            except Exception as _e_conn:
                logger.debug("[append_signal_log_safe] 连接解析失败: %s", _e_conn)
        if db_conn is None:
            return res
        # 通过判定
        is_pass = (
            res.get("is_pass", False)
            or res.get(f"{pool}_core_screener_pass", False)
            or res.get("burst_score", 0) >= 50.0
        )
        if not is_pass:
            return res

        score = float(
            res.get("burst_score")
            or res.get("score")
            or res.get("p1_score")
            or 0.0
        )
        if push_min_score is not None and score < float(push_min_score):
            return res

        ts_code = str(res.get("ts_code") or res.get("code") or "").strip()
        if not ts_code:
            return res
        # === 修复 2：ts_code 统一带交易所后缀（与 daily_data 对齐） ===
        try:
            from core.signal_ledger_bridge import normalize_ts_code

            ts_code = normalize_ts_code(ts_code)
        except Exception as _e_nt:
            logger.debug("[append_signal_log_safe] ts_code 归一失败: %s", _e_nt)

        # 推送价：优先 rt['price'] / rt['now_price'] / res 内价格
        price = 0.0
        if isinstance(rt, dict):
            price = float(rt.get("price") or rt.get("now_price") or 0.0)
        if price <= 0:
            price = float(res.get("price") or res.get("push_price") or 0.0)
        if price <= 0:
            # 没价格不写入（避免脏数据）
            return res

        strategies = res.get("strategies") or []
        if isinstance(strategies, str):
            strategies = [s.strip() for s in strategies.replace("|", ",").split(",") if s.strip()]

        # === V28 E 项：注入出场建议 ===
        try:
            from core.exit_strategy import attach_exit_plan_to_res
            attach_exit_plan_to_res(res, push_price=price)
        except Exception as _e_ep:
            logger.debug("[append_signal_log_safe] 出场建议注入失败: %s", _e_ep)

        # === V28 G 项：推送去重（仅在 res 没自带 dedup_decision 时兜底） ===
        try:
            from core.push_dedup import pool_apply_push_dedup
            # pool_apply_push_dedup 自动判 dedup_decision 是否已注入；
            # 已注入则保留上游决定，未注入则走 check_dedup + record_push
            pool_apply_push_dedup(res, pool, record=("dedup_decision" not in res))
            if res.get("dedup_decision", {}).get("should_dedup"):
                logger.debug(
                    "[push_dedup] %s %s 跳过: %s",
                    ts_code, pool, res["dedup_decision"].get("reason", ""),
                )
                return res
        except Exception as _e_dd:
            logger.debug("[append_signal_log_safe] 去重异常: %s", _e_dd)

        # === V28 H 项：动量/反转推送模板 + 时机分流 ===
        try:
            from core.push_template_momentum_reversal import apply_push_template_to_res
            apply_push_template_to_res(res)
        except Exception as _e_pt:
            logger.debug("[append_signal_log_safe] 推送模板异常: %s", _e_pt)

        item = {
            "ts_code": ts_code,
            "name": str(res.get("name") or ""),
            "score": score,
            "burst_score": score,
            "strategies": strategies,
            "price": price,
            "pre_close": float(res.get("pre_close") or (rt or {}).get("pre_close") or 0.0),
            "rank": int(res.get("rank") or res.get("rank_in_pool") or 0),
            # 【V30.10 语义修正】此处的 pushed 表示"进入了推送候选账本"，
            # 不是"用户真的收到了企微"。真实推送由 notification/gateway.py
            # 在推送占位成功后回写 signal_log.pushed = TRUE。
            # 这里写 False，避免与真实推送状态混淆；
            # 真实推送回写会覆盖本值。
            "pushed": False,
            "observation": bool(res.get("is_observation") or res.get("observation_only") or False),
            "remark": str(res.get("wechat_hint") or "")[:120],
        }

        # === V29：数据质量标记 + 风控点位进入信号账本 ===
        # 只采集与打标，不参与任何过滤/打分；失败一律降级，不阻塞推送。
        try:
            from data.data_quality_flags import build_batch_quality_flags

            _flags_map = build_batch_quality_flags(db_conn, [item], trade_date="")
            if _flags_map.get(ts_code):
                item["data_quality_flags"] = _flags_map[ts_code]
        except Exception as _e_q:
            logger.debug("[append_signal_log_safe] 质量标记异常: %s", _e_q)
        try:
            _ep = res.get("exit_plan") if isinstance(res.get("exit_plan"), dict) else None
            if _ep:
                item["exit_plan"] = _ep
            item["suggested_position"] = str(
                res.get("suggested_position") or res.get("建议仓位") or ""
            )
            item["entry_basis"] = str(res.get("entry_basis") or f"push_price:{pool}")
            item["regime"] = str(res.get("regime") or "")
        except Exception as _e_r:
            logger.debug("[append_signal_log_safe] 风控字段采集异常: %s", _e_r)

        # === Tier1-1.3/1.4/1.6 Shadow：只记录不拦截（不参与过滤/打分/推送） ===
        # 记录到 data/runtime/shadow/shadow_YYYYMMDD.jsonl，用 tools/shadow_report.py 出对照表。
        # 失败一律降级，绝不打断落库与推送主链路。
        try:
            from core.shadow_filters import annotate_shadow

            annotate_shadow(pool, [item], db_conn=db_conn)
        except Exception as _e_sh:
            logger.debug("[append_signal_log_safe] shadow 标注异常: %s", _e_sh)

        from core.pool_signal_adapter import safe_log_pool_signal
        safe_log_pool_signal(pool, [item], db_conn, trade_date=trade_date)
    except Exception as e:
        logger.debug("[append_signal_log_safe] %s 异常: %s", pool, e)
    finally:
        # 只关闭我们自己开的独占连接；复用单例的绝不能关
        if _opened_here and db_conn is not None:
            try:
                db_conn.close()
            except Exception:
                pass
    return res
