# -*- coding: utf-8 -*-
"""core/sentiment_shadow.py — H1 情绪温度计 Shadow 记录（只记录，不拦截）

背景：`core/market_sentiment.py` 的 8 状态 × 5 池系数表（SENTIMENT_POOL_MULTIPLIER，
退潮期 P2/P3 打 0.6 折）此前全仓 0 个消费者 —— 情绪温度计只是"报告型"。
按既定红线（不改选股逻辑 / 不改阈值），这里先做 **Shadow 模式**：

  · 每次扫描结束后，计算「今天若把情绪系数接进 pass_line，会是什么状态」；
  · 把状态、系数、各池通过数写入 JSONL（data/runtime/observability/sentiment_shadow.jsonl）；
  · **不拦截任何信号、不影响任何分数**；
  · 攒满 ≥20 个交易日后，人工评估"系数方向与后续信号质量"的吻合率，
    再决定是否接线（与 Tier1-1.3/1.4 的 Shadow 纪律一致）。

输入口径（尽量从已有数据取，不新增外部调用）：
  · limit_up_count / max_consecutive_limit ← hithink_special_limit_up_pool（当日，缺失回退最近一日）
  · limit_down_count                        ← hithink_special_limit_down_pool
  · limit_burst_rate                        ← hithink_special_limit_break_pool / (涨停+炸板)
  · up_ratio                                ← 扫描时已拉取的 rt_map（pct_chg > 0 占比）

红线：只读数据 + 追加 JSONL；任何异常静默降级，绝不影响扫描主流程。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_OUT_DIR = os.path.join(_ROOT, "data", "runtime", "observability")
_OUT_JSONL = os.path.join(_OUT_DIR, "sentiment_shadow.jsonl")
_MAX_BYTES = 5 * 1024 * 1024          # 超 5MB 轮转为 .1（只保留一份历史）
_LAST_RECORD_KEY = "_h1_sentiment_shadow_last"


def _today_bj_ymd() -> str:
    try:
        from datetime import timedelta, timezone

        return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _up_ratio_from_rt(rt_map: Optional[Dict[str, Any]]) -> Optional[float]:
    """从扫描用的实时行情映射里算上涨家数占比；数据不足返回 None。"""
    if not isinstance(rt_map, dict) or not rt_map:
        return None
    n = 0
    up = 0
    for _code, rt in rt_map.items():
        if not isinstance(rt, dict):
            continue
        try:
            pct = rt.get("pct_chg")
            if pct is None:
                pct = rt.get("change_pct")
            if pct is None:
                price = float(rt.get("price") or rt.get("now_price") or 0)
                pre = float(rt.get("pre_close") or 0)
                if price > 0 and pre > 0:
                    pct = (price / pre - 1.0) * 100.0
            if pct is None:
                continue
            n += 1
            if float(pct) > 0:
                up += 1
        except Exception:
            continue
    if n < 50:
        return None
    return up / n


def _counts_from_db(db_conn=None) -> Dict[str, Any]:
    """从 HiThink 池表读涨停/跌停/炸板计数（只 SELECT）。失败返回空 dict。"""
    out: Dict[str, Any] = {}
    con = db_conn
    _opened = False
    try:
        if con is None:
            try:
                from data.db_core import get_read_conn_singleton

                con = get_read_conn_singleton(max_wait_sec=3.0)
            except Exception:
                con = None
            if con is None:
                import duckdb

                db_path = os.path.join(_ROOT, "data", "quant_data.duckdb")
                con = duckdb.connect(db_path, read_only=True)
                _opened = True
        today = _today_bj_ymd()

        def _q(sql: str, params=None):
            try:
                return con.execute(sql, params or []).fetchall()
            except Exception:
                return []

        # 涨停池：当日优先，缺失回退表内最近一日
        rows = _q(
            "SELECT count(*), max(continuous_limit_days) "
            "FROM hithink_special_limit_up_pool WHERE CAST(trade_date AS VARCHAR) = ?",
            [today],
        )
        if not rows or not rows[0] or int(rows[0][0] or 0) == 0:
            rows = _q(
                "SELECT count(*), max(continuous_limit_days) "
                "FROM hithink_special_limit_up_pool "
                "WHERE trade_date = (SELECT max(trade_date) FROM hithink_special_limit_up_pool)"
            )
            out["limit_up_source"] = "latest"
        else:
            out["limit_up_source"] = "today"
        if rows and rows[0] is not None:
            out["limit_up_count"] = int(rows[0][0] or 0)
            try:
                out["max_consecutive_limit"] = int(rows[0][1] or 0)
            except Exception:
                out["max_consecutive_limit"] = 0

        # 跌停池
        rows = _q(
            "SELECT count(*) FROM hithink_special_limit_down_pool "
            "WHERE CAST(trade_date AS VARCHAR) = ?",
            [today],
        )
        if not rows or not rows[0] or int(rows[0][0] or 0) == 0:
            rows = _q(
                "SELECT count(*) FROM hithink_special_limit_down_pool "
                "WHERE trade_date = (SELECT max(trade_date) FROM hithink_special_limit_down_pool)"
            )
            out["limit_down_source"] = "latest"
        else:
            out["limit_down_source"] = "today"
        if rows and rows[0] is not None:
            out["limit_down_count"] = int(rows[0][0] or 0)

        # 炸板池
        rows = _q(
            "SELECT count(*) FROM hithink_special_limit_break_pool "
            "WHERE CAST(trade_date AS VARCHAR) = ?",
            [today],
        )
        if not rows or not rows[0] or int(rows[0][0] or 0) == 0:
            rows = _q(
                "SELECT count(*) FROM hithink_special_limit_break_pool "
                "WHERE trade_date = (SELECT max(trade_date) FROM hithink_special_limit_break_pool)"
            )
            out["limit_break_source"] = "latest"
        else:
            out["limit_break_source"] = "today"
        if rows and rows[0] is not None:
            out["limit_break_count"] = int(rows[0][0] or 0)
    except Exception as e:
        out["db_error"] = f"{type(e).__name__}: {e}"
    finally:
        if _opened and con is not None:
            try:
                con.close()
            except Exception:
                pass
    return out


def compute_shadow_sentiment(
    rt_map: Optional[Dict[str, Any]] = None,
    db_conn=None,
) -> Optional[Dict[str, Any]]:
    """计算「若接线」的情绪状态与各池系数（纯计算，不产生任何副作用）。"""
    try:
        from core.market_sentiment import classify_market_sentiment

        db = _counts_from_db(db_conn)
        n_up = int(db.get("limit_up_count", 0) or 0)
        n_down = int(db.get("limit_down_count", 0) or 0)
        n_break = int(db.get("limit_break_count", 0) or 0)
        if n_up == 0 and n_down == 0:
            # 两张池表都没有数据 → 情绪无法判定，不记录（避免写 "未知" 噪声）
            return None
        max_lb = int(db.get("max_consecutive_limit", 0) or 0)
        burst = (n_break / (n_up + n_break)) if (n_up + n_break) > 0 else 0.0
        up_ratio = _up_ratio_from_rt(rt_map)
        _up_defaulted = up_ratio is None
        if up_ratio is None:
            up_ratio = 0.5
        s = classify_market_sentiment(
            limit_up_count=n_up,
            limit_down_count=n_down,
            max_consecutive_limit=max_lb,
            limit_burst_rate=burst,
            up_ratio=up_ratio,
        )
        return {
            "state": s.get("state", "未知"),
            "score": s.get("score", 0),
            "multiplier": s.get("multiplier") or {},
            "inputs": {
                "limit_up_count": n_up,
                "limit_down_count": n_down,
                "limit_break_count": n_break,
                "max_consecutive_limit": max_lb,
                "limit_burst_rate": round(burst, 4),
                "up_ratio": round(float(up_ratio), 4),
                "up_ratio_defaulted": _up_defaulted,
                "limit_up_source": db.get("limit_up_source", ""),
                "limit_down_source": db.get("limit_down_source", ""),
            },
        }
    except Exception as e:
        logger.debug("[sentiment_shadow] 计算失败: %s", e)
        return None


def record_sentiment_shadow(
    res: Any,
    pools,
    rt_map: Optional[Dict[str, Any]] = None,
    db_conn=None,
) -> None:
    """扫描结束后调用：追加一行 Shadow 记录。任何异常静默。"""
    try:
        snap = compute_shadow_sentiment(rt_map=rt_map, db_conn=db_conn)
        if not snap:
            return
        kept = {}
        try:
            for k in pools or []:
                kept[str(k)] = len((res or {}).get(k) or [])
        except Exception:
            pass
        rec = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "business_date": _today_bj_ymd(),
            **snap,
            "kept_n": kept,
            "shadow_only": True,          # 明确标注：本记录未参与任何拦截
        }
        os.makedirs(_OUT_DIR, exist_ok=True)
        try:
            if os.path.exists(_OUT_JSONL) and os.path.getsize(_OUT_JSONL) > _MAX_BYTES:
                os.replace(_OUT_JSONL, _OUT_JSONL + ".1")
        except Exception:
            pass
        with open(_OUT_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        logger.info(
            "[H1-Shadow] 情绪=%s(score=%s) | P2×%.2f P3×%.2f P4×%.2f P5×%.2f | 通过数 %s（仅记录，不拦截）",
            rec["state"], rec["score"],
            float(rec["multiplier"].get("p2", 1.0) or 1.0),
            float(rec["multiplier"].get("p3", 1.0) or 1.0),
            float(rec["multiplier"].get("p4", 1.0) or 1.0),
            float(rec["multiplier"].get("p5", 1.0) or 1.0),
            kept,
        )
    except Exception as e:
        logger.debug("[sentiment_shadow] 记录跳过: %s", e)
