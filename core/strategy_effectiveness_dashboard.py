# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V29.2 — 战法胜率面板 · 只读数据适配层
================================================================================
【设计前提（硬约束）】
  1. Windows 下 DuckDB 单写文件锁：daemon 运行期间 Streamlit 无法安全连库。
     因此本模块 **只读 JSON 快照**，绝不 import data.db_core、绝不连 DuckDB。
  2. 面板为「纯只读反馈」：不写库、不触发回填、不改实盘权重、不自动应用。
  3. 统计口径唯一来源 = 优化器产出的 primary_n_days 主口径；
     严禁回退到 t5_win_rate（V29.1 已确认该字段在 T+5 未成熟时全为 NULL）。

【数据来源】
  - 主快照：data/strategy_effectiveness_ui_snapshot.json   （daemon 18:30 生成，首选）
  - 兜底快照：data/strategy_weight_recommendation.json      （优化器产出，字段同构）

【历史趋势】
  history 由生成端从 strategy_effectiveness 历史行聚合；兜底快照通常没有 history，
  此时面板显示「暂无趋势」而非报错。
================================================================================
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BJ_TZ = timezone(timedelta(hours=8))

SCHEMA_VERSION = "1.0"

# 快照文件候选顺序：UI 快照优先，其次优化器推荐报表
SNAPSHOT_CANDIDATES = (
    "data/strategy_effectiveness_ui_snapshot.json",
    "data/strategy_weight_recommendation.json",
)

# 面板绝不允许读取的字段（旧 T+5 口径，V29.1 已确认未成熟时为 NULL / 0）
FORBIDDEN_FIELDS = (
    "t5_win_rate",
    "t10_win_rate",
    "avg_t5_return",
    "avg_excess_t5",
)

# 【V30 新增】面板允许展示的混合基准字段（均为纯新增列，不改旧口径）
BLEND_FIELDS = (
    "avg_excess_sh",
    "avg_excess_sz",
    "avg_excess_cyb",
    "avg_excess_blend",
    "beat_blend_rate",
)

# 【Tier2.5 新增】净口径（扣费后）字段（纯新增列，与旧口径并列展示）
NET_FIELDS = (
    "avg_net_return",
    "avg_net_excess",
    "avg_net_excess_blend",
    "net_win_rate",
)

# 样本量分档：决定「成熟度」文案，避免样本不足时给出确定性结论
_SAMPLE_TIERS = (
    (30, "样本充足"),
    (10, "样本中等"),
    (1, "样本偏少"),
)

_FRESH_WARN_HOURS = 24.0
_FRESH_BAD_HOURS = 72.0


def _project_root() -> str:
    """返回项目根目录（纯路径推导，不依赖 data.db_core，避免引入 DuckDB 依赖）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def resolve_snapshot_path(path: Optional[str] = None) -> Optional[str]:
    """返回第一个存在的快照文件绝对路径；都不存在返回 None。"""
    if path:
        return path if os.path.exists(path) else None
    root = _project_root()
    for rel in SNAPSHOT_CANDIDATES:
        cand = os.path.join(root, rel.replace("/", os.sep))
        if os.path.exists(cand):
            return cand
    return None


def _safe_float(v: Any) -> Optional[float]:
    """NULL 安全转 float：None / '' / 'null' / NaN / Inf / 非法值统一返回 None。"""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, str):
        s = v.strip()
        if not s or s.lower() in ("null", "none", "nan", "inf", "-inf", "-", "--", "—"):
            return None
        try:
            f = float(s)
        except ValueError:
            return None
    else:
        try:
            f = float(v)
        except (TypeError, ValueError):
            return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def _safe_int(v: Any, default: int = 0) -> int:
    f = _safe_float(v)
    if f is None:
        return default
    try:
        return int(round(f))
    except (TypeError, ValueError, OverflowError):
        return default


def _norm_pool(v: Any) -> str:
    return str(v or "").strip().lower()


def _norm_n_days_list(v: Any) -> List[int]:
    """把 [2, 1] / "2,1" / "[2, 1]" / 2 统一成排序去重后的 int 列表。"""
    if v is None or isinstance(v, bool):
        return []
    if isinstance(v, (int, float)):
        f = _safe_float(v)
        return [int(f)] if f is not None else []
    out: List[int] = []
    for token in str(v).replace(";", ",").split(","):
        f = _safe_float(token.strip().strip("[]() "))
        if f is not None:
            out.append(int(f))
    return sorted(set(out))


def _norm_pool_rows(v: Any) -> List[Dict[str, Any]]:
    """规范化 pool_rows；防御脏数据（None / 非 dict / 缺字段）。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(v, (list, tuple)):
        return out
    for r in v:
        if not isinstance(r, dict):
            continue
        out.append({
            "pool": _norm_pool(r.get("pool")),
            "n_days": _safe_int(r.get("n_days"), 0),
            "n_signals": _safe_int(r.get("n_signals"), 0),
            "win_rate": _safe_float(r.get("win_rate")),
        })
    return out


def classify_sample(n_evaluated: int) -> str:
    """样本量分档文案（只描述样本厚度，不给出「有效/无效」结论）。"""
    n = _safe_int(n_evaluated, 0)
    for threshold, text in _SAMPLE_TIERS:
        if n >= threshold:
            return text
    return "零样本"


def normalize_stats(raw: Any) -> List[Dict[str, Any]]:
    """
    把快照里的 stats（dict[str, dict] 或 list[dict]）规范化成 list[dict]。

    · 脏行（缺 strategy / 非 dict）直接丢弃，不抛异常
    · 主动剔除 FORBIDDEN_FIELDS（旧 T+5 口径），杜绝误用
    · 每行附带 sample_tier 文案
    """
    rows: List[Dict[str, Any]] = []
    items: List[Tuple[Any, Any]] = []
    if isinstance(raw, dict):
        items = list(raw.items())
    elif isinstance(raw, (list, tuple)):
        items = list(enumerate(raw))
    else:
        return rows

    for key, info in items:
        if not isinstance(info, dict):
            continue
        # 完全空的条目（如 {"P5-XX": {}}）视为脏行：除键名外没有任何指标，
        # 展示出来只会是一行全 0 的假数据。真正的「零样本」战法仍会保留
        # （它们至少有 n_signals 等字段，由 classify_sample 标注）。
        if not info:
            continue
        strategy = str(info.get("strategy") or key or "").strip()
        if not strategy:
            continue
        pool_rows = _norm_pool_rows(info.get("pool_rows"))
        pools = [_norm_pool(p) for p in (info.get("pools") or [])]
        pools = [p for p in pools if p]
        if not pools:
            pools = sorted({r["pool"] for r in pool_rows if r["pool"]})

        n_eval = _safe_int(info.get("n_evaluated"), 0)
        row: Dict[str, Any] = {
            "strategy": strategy,
            "strategy_label": str(info.get("strategy_label") or "").strip(),
            "n_signals": _safe_int(info.get("n_signals"), 0),
            "n_evaluated": n_eval,
            "n_win": _safe_int(info.get("n_win"), 0),
            "win_rate": _safe_float(info.get("win_rate")),
            "primary_n_days": _safe_int(info.get("primary_n_days"), 0) or None,
            "available_n_days": _norm_n_days_list(info.get("available_n_days")),
            "missing_n_days": _norm_n_days_list(info.get("missing_n_days")),
            "avg_return": _safe_float(info.get("avg_return")),
            "avg_excess": _safe_float(info.get("avg_excess")),
            "avg_max_drawdown": _safe_float(info.get("avg_max_drawdown")),
            "avg_max_profit": _safe_float(info.get("avg_max_profit")),
            "avg_excess_sh": _safe_float(info.get("avg_excess_sh")),
            "avg_excess_sz": _safe_float(info.get("avg_excess_sz")),
            "avg_excess_cyb": _safe_float(info.get("avg_excess_cyb")),
            "avg_excess_blend": _safe_float(info.get("avg_excess_blend")),
            "beat_blend_rate": _safe_float(info.get("beat_blend_rate")),
            # ---- Tier2.5 净口径（缺数据 → None，绝不 0）----
            "avg_net_return": _safe_float(info.get("avg_net_return")),
            "avg_net_excess": _safe_float(info.get("avg_net_excess")),
            "avg_net_excess_blend": _safe_float(info.get("avg_net_excess_blend")),
            "net_win_rate": _safe_float(info.get("net_win_rate")),
            "pools": sorted(set(pools)),
            "pool_rows": pool_rows,
            "sample_tier": classify_sample(n_eval),
        }
        # Tier 2 · 2.6 战法健康度红绿灯（纯展示，不参与打分/过滤）
        try:
            from core.strategy_health_flags import classify_health

            _h = classify_health(
                strategy,
                win_rate=row.get("win_rate"),
                avg_return=row.get("avg_return"),
                n_evaluated=n_eval,
            )
            row["health_flag"] = _h["flag"]
            row["health_label"] = _h["label"]
            row["health_reason"] = _h["reason"]
        except Exception:
            row["health_flag"] = "unknown"
            row["health_label"] = "— 未知"
            row["health_reason"] = ""
        for f in FORBIDDEN_FIELDS:
            row.pop(f, None)
        rows.append(row)

    rows.sort(key=lambda r: (-r["n_evaluated"], r["strategy"]))
    return rows


def build_daily_history(raw: Any) -> List[Dict[str, Any]]:
    """规范化每日趋势历史；脏数据一律丢弃。"""
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, (list, tuple)):
        return out
    for r in raw:
        if not isinstance(r, dict):
            continue
        stat_date = str(r.get("stat_date") or "").strip()[:10]
        if not stat_date:
            continue
        out.append({
            "stat_date": stat_date,
            "pool": _norm_pool(r.get("pool")),
            "strategy": str(r.get("strategy") or "").strip(),
            "stat_n_days": _safe_int(r.get("stat_n_days"), 0) or None,
            "n_signals": _safe_int(r.get("n_signals"), 0),
            "n_evaluated": _safe_int(r.get("n_evaluated"), 0),
            "n_win": _safe_int(r.get("n_win"), 0),
            "win_rate": _safe_float(r.get("win_rate")),
            "avg_return": _safe_float(r.get("avg_return")),
        })
    out.sort(key=lambda r: (r["stat_date"], r["strategy"]))
    return out


def _parse_iso(v: Any) -> Optional[datetime]:
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def judge_freshness(generated_at: Optional[str], now: Optional[datetime] = None) -> Tuple[str, str]:
    """
    快照新鲜度判定。返回 (level, message)，level ∈ {fresh, warn, bad, unknown}。
    """
    ts = _parse_iso(generated_at)
    if ts is None:
        return "unknown", "快照缺少生成时间，无法判断新鲜度"
    now = now or datetime.now(BJ_TZ)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=BJ_TZ)
    age_h = max(0.0, (now - ts).total_seconds() / 3600.0)
    if age_h <= _FRESH_WARN_HOURS:
        return "fresh", "快照生成于 %s（%.1f 小时前）" % (ts.strftime("%Y-%m-%d %H:%M"), age_h)
    if age_h <= _FRESH_BAD_HOURS:
        return "warn", "⚠️ 快照已 %.1f 小时未更新（超过 %.0f 小时），战法表现可能滞后" % (
            age_h, _FRESH_WARN_HOURS)
    return "bad", "🔴 快照已 %.1f 小时未更新（超过 %.0f 小时），请检查 daemon 18:30 回填任务" % (
        age_h, _FRESH_BAD_HOURS)


def weighted_average(rows: List[Dict[str, Any]], field: str,
                     weight_field: str = "n_evaluated") -> Optional[float]:
    """按 n_evaluated 加权平均（禁止算术平均：小样本会拉飞整体）。"""
    num = 0.0
    den = 0.0
    for r in rows:
        v = _safe_float(r.get(field))
        w = _safe_float(r.get(weight_field)) or 0.0
        if v is None or w <= 0:
            continue
        num += v * w
        den += w
    return (num / den) if den > 0 else None


def summarize(rows: List[Dict[str, Any]], recommendation: Dict[str, Any]) -> Dict[str, Any]:
    """顶部指标卡数据（胜率/收益一律按 n_evaluated 加权）。"""
    promote: List[str] = []
    demote: List[str] = []
    insufficient: List[str] = []
    for r in rows:
        strat = r.get("strategy", "")
        rec = recommendation.get(strat)
        rec = rec if isinstance(rec, dict) else {}
        weight = _safe_float(rec.get("weight"))
        reason = str(rec.get("reason") or "")
        if weight is not None and weight < 1.0:
            demote.append(strat)
        elif weight is not None and weight > 1.0:
            promote.append(strat)
        elif reason.startswith("样本不足"):
            insufficient.append(strat)
    return {
        "n_strategies": len(rows),
        "avg_win_rate": weighted_average(rows, "win_rate"),
        "avg_return": weighted_average(rows, "avg_return"),
        "avg_max_drawdown": weighted_average(rows, "avg_max_drawdown"),
        "avg_max_profit": weighted_average(rows, "avg_max_profit"),
        "total_n_signals": sum(_safe_int(r.get("n_signals"), 0) for r in rows),
        "total_n_evaluated": sum(_safe_int(r.get("n_evaluated"), 0) for r in rows),
        "n_promote": len(promote),
        "n_demote": len(demote),
        "n_insufficient": len(insufficient),
        "promote_list": promote,
        "demote_list": demote,
        "insufficient_list": insufficient,
    }


def load_snapshot(path: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """
    只读加载快照。
    文件缺失 / JSON 损坏 / 结构非法 / schema 不受支持 → 返回 None（绝不抛异常），
    调用方判空即可安全降级。
    """
    p = resolve_snapshot_path(path)
    if not p:
        logger.debug("[se-dash] 无战法快照可读")
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        logger.warning("[se-dash] 快照读取失败(%s): %s", os.path.basename(p), e)
        return None
    if not isinstance(raw, dict):
        logger.warning("[se-dash] 快照结构非法（顶层非 dict）: %s", os.path.basename(p))
        return None

    ver = str(raw.get("schema_version") or "").strip()
    # schema_version 缺省 = 优化器推荐报表（无版本号）；有值则只接受 1.x
    if ver and not ver.startswith("1."):
        logger.warning("[se-dash] 快照 schema_version=%s 不受支持，忽略", ver)
        return None

    maturity = raw.get("maturity") if isinstance(raw.get("maturity"), dict) else {}
    strat_mat = raw.get("strategy_maturity") if isinstance(raw.get("strategy_maturity"), dict) else {}
    rec = raw.get("recommendation") if isinstance(raw.get("recommendation"), dict) else {}
    # 【Tier2.5 新增】净口径推荐表（与旧 recommendation 并列，缺失时为空 dict）
    net_rec = raw.get("net_recommendation") if isinstance(raw.get("net_recommendation"), dict) else {}
    deprecated = raw.get("deprecated") if isinstance(raw.get("deprecated"), (list, tuple)) else []

    generated_at = str(
        raw.get("generated_at") or maturity.get("updated_at") or raw.get("updated_at") or ""
    ).strip()
    level, message = judge_freshness(generated_at)

    # 【V30 新增】市场水位（指数行情）+ 基准覆盖度；缺失时降级为空，不影响主面板
    raw_quotes = raw.get("index_quotes")
    index_quotes: List[Dict[str, Any]] = []
    if isinstance(raw_quotes, (list, tuple)):
        for q in raw_quotes:
            if not isinstance(q, dict):
                continue
            index_quotes.append({
                "ts_code": str(q.get("ts_code") or ""),
                "name": str(q.get("name") or ""),
                "trade_date": str(q.get("trade_date") or "")[:8],
                "close": _safe_float(q.get("close")),
                "pct_chg": _safe_float(q.get("pct_chg")),
                "chg_5d": _safe_float(q.get("chg_5d")),
                "chg_20d": _safe_float(q.get("chg_20d")),
            })
    raw_cov = raw.get("index_coverage")
    index_coverage = raw_cov if isinstance(raw_cov, dict) else {}

    rows = normalize_stats(raw.get("stats"))
    return {
        "schema_version": ver or SCHEMA_VERSION,
        "source_file": os.path.basename(p),
        "generated_at": generated_at,
        "freshness_level": level,
        "freshness_message": message,
        "stat_date": str(maturity.get("stat_date") or "").strip(),
        "mode": str(raw.get("mode") or "statistics_only").strip(),
        "maturity": maturity,
        "strategy_maturity": strat_mat,
        "recommendation": rec,
        "net_recommendation": net_rec,
        "deprecated": [str(x) for x in deprecated],
        "stats": rows,
        "history": build_daily_history(raw.get("history")),
        "index_quotes": index_quotes,
        "index_coverage": index_coverage,
        "blend_ready": bool(index_coverage.get("primary_ready")) or bool(index_quotes),
        "summary": summarize(rows, rec),
        "duplicates": detect_suspicious_duplicates(rows),
    }


def build_pool_matrix(rows: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """按 pool 归集战法行，供池子筛选器使用；一个战法可出现在多个池。"""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        for pool in (r.get("pools") or ["unknown"]):
            out.setdefault(pool, []).append(r)
    for pool in out:
        out[pool].sort(key=lambda r: (-r.get("n_evaluated", 0), r.get("strategy", "")))
    return out


def history_for_strategy(history: List[Dict[str, Any]], strategy: str) -> List[Dict[str, Any]]:
    return [h for h in history if h.get("strategy") == strategy]


def detect_suspicious_duplicates(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    「疑似重复统计」探测：多个战法的 (n_signals, n_evaluated, win_rate, avg_return)
    完全一致，通常意味着同一次扫描被重复记账。
    面板只提示，不自动修数据（修它会影响 P5 选股结果，超出面板职责）。
    """
    buckets: Dict[Tuple[Any, ...], List[str]] = {}
    for r in rows:
        key = (
            r.get("n_signals"),
            r.get("n_evaluated"),
            None if r.get("win_rate") is None else round(float(r["win_rate"]), 6),
            None if r.get("avg_return") is None else round(float(r["avg_return"]), 6),
        )
        buckets.setdefault(key, []).append(r.get("strategy", ""))
    out: List[Dict[str, Any]] = []
    for key, strats in buckets.items():
        if len(strats) > 1 and (key[0] or 0) > 0:
            out.append({
                "strategies": sorted(strats),
                "n_signals": key[0],
                "n_evaluated": key[1],
                "win_rate": key[2],
                "avg_return": key[3],
            })
    out.sort(key=lambda d: (-d["n_evaluated"], -d["n_signals"]))
    return out
