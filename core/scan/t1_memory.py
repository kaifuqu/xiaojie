# -*- coding: utf-8 -*-
"""
core/scan/t1_memory.py
原 core/scan_engine.py L1665-L2159（T+1 资金记忆与归因闭环）

【职责】
- 多层级池子（wash_metrics 共用元数据）：_TIER_META_KEY + _tier_wash_path + _load_prev_scan_empty_streak + _merge_tier_meta_scan
- T+1 记忆 schema：T1_MEMORY_SCHEMA_VERSION + _default_t1_memory_payload + _normalize_t1_pool_payload
- 历史归因聚合：_collect_t1_memory_for_pools + _load_t1_memory_for_scan
- 协议编码：_encode_t1_memory_protocol
- scan_attribution 落盘（含 T+1 自动结算）：_persist_scan_attribution_snapshot
- 拥挤度解析：_parse_crowding_score
- 入表行契约：_ensure_pool_table_row_contract
- 漏斗回滚：_rollback_scan_funnel_for_skipped_stock
- P1 行业 PE 分布估计：_scan_build_industry_pe_stats_for_p1

【公开 API】
无（纯内部工具）

【跨包依赖】
- core.scan.utils（BJ_TZ, _safe_float, ensure_runtime_data_layout, get_all_basic_industry, path_wash_metrics_json）
- core.file_utils（atomic_json_update）
- data.db_core（get_read_conn_singleton, table_exists）
"""
import json
import logging
import os
from datetime import datetime

import numpy as np
import pandas as pd

from core.scan.utils import (
    BJ_TZ,
    _safe_float,
    ensure_runtime_data_layout,
    get_all_basic_industry,
    path_wash_metrics_json,
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s [扫描引擎-t1] %(message)s')

# ==================== 多层级池子元数据 ====================
_TIER_META_KEY = "__tier_pool_meta__"


def _tier_wash_path():
    return path_wash_metrics_json()


def _load_prev_scan_empty_streak():
    """读取上一轮落盘的 P2–P5 主池连续无票 streak，供本趟是否允许写入观察池（乐观门控）。"""
    p = _tier_wash_path()
    if not os.path.exists(p):
        return 0
    try:
        with open(p, "r", encoding="utf-8") as f:
            root = json.load(f)
        if not isinstance(root, dict):
            return 0
        meta = root.get(_TIER_META_KEY)
        if not isinstance(meta, dict):
            return 0
        return int(meta.get("scan_empty_streak", 0) or 0)
    except Exception as e:
        logging.debug("【多层级池子】读取 scan streak 失败: %s", e)
        return 0


def _merge_tier_meta_scan(is_main_empty):
    """本次扫描结束后更新「主池(P2–P5)无票」连续计数；有票清零。同日多次扫描不重复加一。"""
    from core.file_utils import atomic_json_update

    p = _tier_wash_path()
    box = {"streak": 0}

    def _upd(root: dict) -> None:
        meta = root.get(_TIER_META_KEY)
        if not isinstance(meta, dict):
            meta = {}
        streak = int(meta.get("scan_empty_streak", 0) or 0)
        today_str = datetime.now(BJ_TZ).strftime("%Y%m%d")
        last_d = str(meta.get("scan_last_empty_date", "") or "")
        if is_main_empty:
            if last_d != today_str:
                streak += 1
                meta["scan_last_empty_date"] = today_str
        else:
            streak = 0
            meta["scan_last_empty_date"] = ""
        meta["scan_empty_streak"] = streak
        root[_TIER_META_KEY] = meta
        box["streak"] = streak

    try:
        ensure_runtime_data_layout()
        atomic_json_update(p, _upd, timeout=5)
    except Exception as e:
        logging.warning("【多层级池子】atomic 写入 scan streak 失败: %s", e)
    return int(box["streak"])


def _parse_crowding_score(row):
    """从 '68(中拥挤)' 解析数值 68.0；解析失败返回 0。"""
    try:
        s = str((row or {}).get("拥挤度", "") or "").strip()
        if not s:
            return 0.0
        n = str(s).split("(", 1)[0].strip()
        return float(_safe_float(n, 0.0))
    except Exception:
        return 0.0


# ==================== T+1 记忆 schema ====================
T1_MEMORY_SCHEMA_VERSION = 1


def _default_t1_memory_payload():
    return {
        "schema_version": T1_MEMORY_SCHEMA_VERSION,
        "source": "scan_attribution.t1_settlement",
        "pools": {},
    }


def _normalize_t1_pool_payload(raw):
    payload = {
        "sample_n": 0,
        "avg_ret_t1_pct": 0.0,
        "win_rate_t1_pct": 0.0,
        "by_tier": {},
    }
    if not isinstance(raw, dict):
        return payload
    payload["sample_n"] = int(_safe_float(raw.get("sample_n", 0), 0))
    payload["avg_ret_t1_pct"] = round(float(_safe_float(raw.get("avg_ret_t1_pct", 0.0), 0.0)), 3)
    payload["win_rate_t1_pct"] = round(float(_safe_float(raw.get("win_rate_t1_pct", 0.0), 0.0)), 1)
    by_tier = raw.get("by_tier") if isinstance(raw.get("by_tier"), dict) else {}
    norm_bt = {}
    for tk, tv in by_tier.items():
        if not isinstance(tv, dict):
            continue
        norm_bt[str(tk)] = {
            "n": int(_safe_float(tv.get("n", 0), 0)),
            "avg_ret_t1_pct": round(float(_safe_float(tv.get("avg_ret_t1_pct", 0.0), 0.0)), 3),
            "win_rate_t1_pct": round(float(_safe_float(tv.get("win_rate_t1_pct", 0.0), 0.0)), 1),
        }
    payload["by_tier"] = norm_bt
    return payload


def _collect_t1_memory_for_pools(root: dict, target_pools):
    """从历史 scan_attribution 里汇总 P3/P4/P5 的 T+1 记忆，供实时打分使用。"""
    out = {}
    if not isinstance(root, dict):
        return out
    for pk in target_pools:
        if pk not in out:
            out[pk] = _normalize_t1_pool_payload(None)
    for d8, dnode in root.items():
        if not (isinstance(d8, str) and len(d8) == 8 and d8.isdigit()):
            continue
        sat = dnode.get("scan_attribution") if isinstance(dnode, dict) else None
        if not isinstance(sat, dict):
            continue
        t1 = sat.get("t1_settlement") if isinstance(sat.get("t1_settlement"), dict) else None
        if not isinstance(t1, dict):
            continue
        pools = t1.get("pools") if isinstance(t1.get("pools"), dict) else {}
        for pk in target_pools:
            pdata = pools.get(pk)
            if not isinstance(pdata, dict):
                continue
            n = int(_safe_float(pdata.get("sample_n", 0), 0))
            if n <= 0:
                continue
            cur = out.setdefault(pk, _normalize_t1_pool_payload(None))
            cur_n = int(cur.get("sample_n", 0) or 0)
            cur_avg = float(cur.get("avg_ret_t1_pct", 0.0) or 0.0)
            cur_win = float(cur.get("win_rate_t1_pct", 0.0) or 0.0)
            total_n = cur_n + n
            if total_n > 0:
                cur["avg_ret_t1_pct"] = round(((cur_avg * cur_n) + (float(pdata.get("avg_ret_t1_pct", 0.0) or 0.0) * n)) / total_n, 3)
                cur["win_rate_t1_pct"] = round(((cur_win * cur_n) + (float(pdata.get("win_rate_t1_pct", 0.0) or 0.0) * n)) / total_n, 1)
                cur["sample_n"] = total_n
            by_tier = pdata.get("by_tier") if isinstance(pdata.get("by_tier"), dict) else {}
            cur_bt = cur.setdefault("by_tier", {})
            for tk, tv in by_tier.items():
                if not isinstance(tv, dict):
                    continue
                tn = int(_safe_float(tv.get("n", 0), 0))
                if tn <= 0:
                    continue
                prev = cur_bt.get(tk) if isinstance(cur_bt.get(tk), dict) else {"n": 0, "avg_ret_t1_pct": 0.0, "win_rate_t1_pct": 0.0}
                p_n = int(prev.get("n", 0) or 0)
                t_n = p_n + tn
                if t_n > 0:
                    prev["avg_ret_t1_pct"] = round(((float(prev.get("avg_ret_t1_pct", 0.0)) * p_n) + (float(tv.get("avg_ret_t1_pct", 0.0) or 0.0) * tn)) / t_n, 3)
                    prev["win_rate_t1_pct"] = round(((float(prev.get("win_rate_t1_pct", 0.0)) * p_n) + (float(tv.get("win_rate_t1_pct", 0.0) or 0.0) * tn)) / t_n, 1)
                    prev["n"] = t_n
                cur_bt[tk] = prev
    return out


def _load_t1_memory_for_scan(target_pools, *, fallback=None):
    """安全加载扫描用 T+1 历史记忆；支持版本化协议与旧格式兼容。"""
    pools = list(target_pools or [])
    empty = {pk: _normalize_t1_pool_payload(None) for pk in pools}
    if not pools:
        return {}
    p = _tier_wash_path()
    if not os.path.exists(p):
        return empty
    try:
        with open(p, "r", encoding="utf-8") as f:
            root = json.load(f)
        if not isinstance(root, dict):
            return fallback if isinstance(fallback, dict) else empty

        mem_root = None
        meta = root.get("__t1_memory_protocol__")
        if isinstance(meta, dict):
            mem_root = meta.get("payload")
        if isinstance(mem_root, dict):
            schema_version = int(_safe_float(meta.get("schema_version", 0), 0))
            pools_node = mem_root.get("pools") if isinstance(mem_root.get("pools"), dict) else {}
            if schema_version >= T1_MEMORY_SCHEMA_VERSION and pools_node:
                out = {}
                for pk in pools:
                    out[pk] = _normalize_t1_pool_payload(pools_node.get(pk))
                return out

        mem = _collect_t1_memory_for_pools(root, pools)
        for pk in pools:
            mem.setdefault(pk, _normalize_t1_pool_payload(None))
        return mem
    except Exception as e:
        logging.debug("读取 t1_memory 失败，回退为空: %s", e)
        return fallback if isinstance(fallback, dict) else empty


def _encode_t1_memory_protocol(target_pools, t1_memory):
    pools = list(target_pools or [])
    payload = _default_t1_memory_payload()
    payload["pools"] = {pk: _normalize_t1_pool_payload((t1_memory or {}).get(pk)) for pk in pools}
    return {
        "schema_version": T1_MEMORY_SCHEMA_VERSION,
        "updated_at": datetime.now(BJ_TZ).isoformat(),
        "payload": payload,
    }


# ==================== scan_attribution 落盘（含 T+1 自动结算）====================
def _persist_scan_attribution_snapshot(res, target_pools, regime):
    """第3步：收益归因闭环（第一阶段）。"""
    from core.file_utils import atomic_json_update

    p = _tier_wash_path()
    today = datetime.now(BJ_TZ).strftime("%Y%m%d")

    def _apply_attribution(root: dict) -> None:
        day = root.get(today)
        if not isinstance(day, dict):
            day = {}

        attrib = {
            "updated_at": datetime.now(BJ_TZ).isoformat(),
            "regime": str(regime or ""),
            "pools": {},
            "note": "scan结构快照（非收益结算）；用于第3步参数归因与执行复盘。",
        }

        t1_memory = _load_t1_memory_for_scan(target_pools)

        for pk in target_pools:
            rows = res.get(pk) or []
            if not isinstance(rows, list):
                rows = []
            n = len(rows)
            scores = [float(_safe_float((r or {}).get("综合分", 0.0), 0.0)) for r in rows if isinstance(r, dict)]
            crows = [_parse_crowding_score(r) for r in rows if isinstance(r, dict)]
            tier_cnt = {"A": 0, "B": 0, "C": 0, "--": 0}
            for r in rows:
                if not isinstance(r, dict):
                    continue
                t = str(r.get("执行层级", "--") or "--").strip().upper()
                if t not in tier_cnt:
                    t = "--"
                tier_cnt[t] += 1
            attrib["pools"][pk] = {
                "count_main": n,
                "avg_score": round(float(np.mean(scores)) if scores else 0.0, 2),
                "avg_crowding": round(float(np.mean(crows)) if crows else 0.0, 1),
                "tier_counts": tier_cnt,
                "t1_memory": t1_memory.get(pk, _normalize_t1_pool_payload(None)),
                "t1_memory_protocol": {
                    "schema_version": T1_MEMORY_SCHEMA_VERSION,
                    "updated_at": datetime.now(BJ_TZ).isoformat(),
                },
                "picks": [
                    {
                        "code": str((r or {}).get("代码", "") or "").strip(),
                        "score": round(float(_safe_float((r or {}).get("综合分", 0.0), 0.0)), 2),
                        "tier": str((r or {}).get("执行层级", "--") or "--").strip().upper(),
                        "signal_px": float(_safe_float((r or {}).get("现价", 0.0), 0.0)),
                        "crowding": round(_parse_crowding_score(r), 1),
                    }
                    for r in rows[:80]
                    if isinstance(r, dict) and str((r or {}).get("代码", "") or "").strip()
                ],
            }

        obs_root = res.get("observation") if isinstance(res, dict) else {}
        if not isinstance(obs_root, dict):
            obs_root = {}
        obs_counts = {}
        for pk in target_pools:
            obs_rows = obs_root.get(pk) or []
            obs_counts[pk] = len(obs_rows) if isinstance(obs_rows, list) else 0
        attrib["observation_counts"] = obs_counts

        day["scan_attribution"] = attrib
        root[today] = day
        root["__t1_memory_protocol__"] = _encode_t1_memory_protocol(target_pools, t1_memory)

        # 第二阶段：T+1 收益结算归因
        try:
            from data.db_core import get_read_conn_singleton, table_exists

            if table_exists("daily_data"):
                con = get_read_conn_singleton()
                if con is not None:
                    day_keys = sorted([k for k in root.keys() if str(k).isdigit() and len(str(k)) == 8])[-12:]
                    for d8 in day_keys:
                        dnode = root.get(d8) if isinstance(root.get(d8), dict) else {}
                        sat = dnode.get("scan_attribution") if isinstance(dnode.get("scan_attribution"), dict) else None
                        if not isinstance(sat, dict):
                            continue
                        if sat.get("t1_settlement_done"):
                            continue

                        row_next = con.execute(
                            """
                            SELECT REPLACE(CAST(trade_date AS VARCHAR), '-', '') AS d8
                            FROM daily_data
                            WHERE REPLACE(CAST(trade_date AS VARCHAR), '-', '') > ?
                            GROUP BY 1
                            ORDER BY 1 ASC
                            LIMIT 1
                            """,
                            [str(d8)],
                        ).fetchone()
                        if not row_next or not row_next[0]:
                            continue
                        next_d8 = str(row_next[0])

                        pools = sat.get("pools") if isinstance(sat.get("pools"), dict) else {}
                        t1 = {"next_trade_date": next_d8, "pools": {}}
                        for pk, pdata in pools.items():
                            if not isinstance(pdata, dict):
                                continue
                            picks = pdata.get("picks") if isinstance(pdata.get("picks"), list) else []
                            rets = []
                            tier_bucket = {"A": [], "B": [], "C": [], "--": []}
                            for it in picks:
                                if not isinstance(it, dict):
                                    continue
                                code6 = str(it.get("code", "") or "").strip()[:6]
                                if len(code6) != 6:
                                    continue
                                r0 = con.execute(
                                    """
                                    SELECT close
                                    FROM daily_data
                                    WHERE SUBSTR(CAST(ts_code AS VARCHAR), 1, 6)=?
                                      AND REPLACE(CAST(trade_date AS VARCHAR), '-', '')=?
                                    ORDER BY trade_date DESC
                                    LIMIT 1
                                    """,
                                    [code6, str(d8)],
                                ).fetchone()
                                r1 = con.execute(
                                    """
                                    SELECT close
                                    FROM daily_data
                                    WHERE SUBSTR(CAST(ts_code AS VARCHAR), 1, 6)=?
                                      AND REPLACE(CAST(trade_date AS VARCHAR), '-', '')=?
                                    ORDER BY trade_date DESC
                                    LIMIT 1
                                    """,
                                    [code6, next_d8],
                                ).fetchone()
                                c0 = _safe_float(r0[0], 0.0) if r0 else 0.0
                                c1 = _safe_float(r1[0], 0.0) if r1 else 0.0
                                if c0 <= 0 or c1 <= 0:
                                    continue
                                ret = (c1 - c0) / c0 * 100.0
                                rets.append(ret)
                                tier = str(it.get("tier", "--") or "--").strip().upper()
                                if tier not in tier_bucket:
                                    tier = "--"
                                tier_bucket[tier].append(ret)
                            t1["pools"][pk] = {
                                "sample_n": len(rets),
                                "avg_ret_t1_pct": round(float(np.mean(rets)) if rets else 0.0, 3),
                                "win_rate_t1_pct": round(
                                    100.0 * float(np.mean([1.0 if x > 0 else 0.0 for x in rets])) if rets else 0.0,
                                    1,
                                ),
                                "by_tier": {
                                    tk: {
                                        "n": len(tv),
                                        "avg_ret_t1_pct": round(float(np.mean(tv)) if tv else 0.0, 3),
                                        "win_rate_t1_pct": round(
                                            100.0 * float(np.mean([1.0 if x > 0 else 0.0 for x in tv])) if tv else 0.0,
                                            1,
                                        ),
                                    }
                                    for tk, tv in tier_bucket.items()
                                },
                            }
                        sat["t1_settlement"] = t1
                        sat["t1_settlement_done"] = True
                        sat["t1_settlement_at"] = datetime.now(BJ_TZ).isoformat()
                        dnode["scan_attribution"] = sat
                        root[d8] = dnode
        except Exception as e:
            logging.debug("scan attribution T+1 settlement skipped: %s", e)

    try:
        ensure_runtime_data_layout()
        atomic_json_update(p, _apply_attribution, timeout=5)
    except Exception as e:
        logging.warning("写入 scan attribution 快照失败: %s", e)


# ==================== 入表行契约 ====================
def _ensure_pool_table_row_contract(row, hk_column_key=None):
    """主池 / 观察池入表行契约：仅对缺失键补默认值。"""
    defaults = {
        "代码": "",
        "名称": "",
        "行业": "",
        "股性": "",
        "综合分": 0.0,
        "涨幅": "--",
        "量比": "--",
        "建议仓位": "",
        "纪律防线": "",
        "真换手": "--",
        "集中度": "--",
        "现价": "--",
        "战法": "",
        "连板高度": "",
        "机构预测": "",
        "风险标签": "--",
        "建议最低分": "--",
        "操盘提示": "--",
        "pool_tier": "main",
    }
    out = {**defaults, **(row or {})}
    # 【上线前修复 2026-10-18】hk_vol 依赖已整体退役，不再补「外资(万)」列。
    # 传入 hk_column_key 时仍兼容旧调用方（显式指定时才补该列）。
    _hk = str(hk_column_key or "").strip()
    if _hk and _hk not in out:
        out[_hk] = "--"
    return out


# ==================== 漏斗回滚 ====================
def _rollback_scan_funnel_for_skipped_stock(res, target_pools):
    """个股在计入漏斗后因市值或 P1 分未达标而 continue 时，回滚诊断计数。"""
    if not isinstance(res, dict) or "funnel" not in res:
        return
    fu = res["funnel"]
    for k in target_pools:
        if k not in fu:
            continue
        fk = fu[k]
        fk["total_candidates"] = max(0, int(fk.get("total_candidates", 0) or 0) - 1)
        fk["enter_strategy_check"] = max(0, int(fk.get("enter_strategy_check", 0) or 0) - 1)


# ==================== P1 行业 PE 分布估计 ====================
def _scan_build_industry_pe_stats_for_p1(items):
    """用当前扫描批次的 base_items 估计行业 PE 分布（q75 等），供多维分项中「动态 PE」维使用。"""
    industry_map = get_all_basic_industry()
    pe_records = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ts_code = item.get("code", "")
        ind = industry_map.get(ts_code, "未知")
        hist = item.get("hist", {}) or {}
        if not isinstance(hist, dict):
            hist = {}
        pe_raw = hist.get("pe_ttm")
        if pe_raw is None or pd.isna(pe_raw) or str(pe_raw).strip() in ("", "-"):
            pe_raw = hist.get("pe", 0)
        pe = _safe_float(pe_raw)
        if pe <= 0 or pe > 500:
            continue
        pe_records.append({"ind": ind, "pe": pe})
    df_pe = pd.DataFrame(pe_records)
    global_stats = {
        "median": df_pe["pe"].median() if not df_pe.empty else 20.0,
        "q30": df_pe["pe"].quantile(0.30) if not df_pe.empty else 15.0,
        "q50": df_pe["pe"].quantile(0.50) if not df_pe.empty else 20.0,
        "q75": df_pe["pe"].quantile(0.75) if not df_pe.empty else 30.0,
        "q80": df_pe["pe"].quantile(0.80) if not df_pe.empty else 35.0,
    }
    industry_pe_stats = {}
    if not df_pe.empty:
        for ind, group in df_pe.groupby("ind"):
            if len(group) >= 10:
                industry_pe_stats[ind] = {
                    "median": group["pe"].median(),
                    "q30": group["pe"].quantile(0.30),
                    "q50": group["pe"].quantile(0.50),
                    "q75": group["pe"].quantile(0.75),
                    "q80": group["pe"].quantile(0.80),
                }
            else:
                industry_pe_stats[ind] = global_stats
    return industry_map, industry_pe_stats, global_stats
