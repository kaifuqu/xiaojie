# -*- coding: utf-8 -*-
"""
core/strategy_effectiveness_snapshot.py
========================================================================
战法有效性「随时快照」基础设施（quick_effectiveness_snapshot）
========================================================================

【为什么需要这个模块】
scripts/strategy_effectiveness_one_shot.py 是一次性脚本：跑一次、出一个 md、
就结束了。系统真正的需求是：

  · 盘中（任意时刻）能在 UI 上看到「各战法样本外表现」，
    而不是等 18:30 的 strategy_effectiveness 批处理；
  · 调参（push_min_score / 权重）时能立刻看到「改完之后效果变好还是变坏」；
  · 一天看多次，用的是同一套口径（不随调用方各写一套而漂移）。

本模块把一次性脚本的计算内核提取为【可 import 的常驻 API】，
口径与脚本【完全一致】（同一套 Wilson 下界 / T+N 交易日定位 / 基准累计），
只是不再强制落盘、可以被任意调用方按需触发。

【口径锚点（与一次性脚本逐字一致）】
- 入场价：signal_log.push_price
- T+N 收盘：ts_code 在 trade_date 之后第 N 个【真实交易日】的 close
- 交易日历：dim_trade_calendar，兜底 daily_data DISTINCT trade_date
- 胜率：return_pct > 0（与 signal_pnl.win 语义一致）
- Wilson 95% 置信下界：小样本下防止裸胜率自欺

【与 signal_backtest / strategy_effectiveness 的关系】
  · signal_backtest.backfill_pnl  → 写入 signal_pnl（需调度、落库）
  · strategy_effectiveness        → 18:30 批处理聚合（落库）
  · 本模块                      → 【不落库】的即时只读视图
三者口径同源，本模块用于「落库前先看一眼」与「交互式调参」。

【红线】
  · 只读 DuckDB，绝不写 signal_log / signal_pnl / strategy_effectiveness
  · 不改任何策略权重（auto_apply 恒为 False）
  · 异常一律降级返回，绝不抛出到调用方
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

BJ_TZ = timezone(timedelta(hours=8))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "data", "quant_data.duckdb")
OUT_JSON = os.path.join(ROOT, "data", "strategy_effectiveness_one_shot.json")
OUT_MD = os.path.join(ROOT, "data", "strategy_effectiveness_one_shot.md")

# ============================================================================
# 【Tier2.7 修复】UI 快照落盘路径（与一次性快照分离）
# ----------------------------------------------------------------------------
# 根因：build_ui_snapshot() 原来把 quick_effectiveness_snapshot() 的输出写到了
#       OUT_JSON（data/strategy_effectiveness_one_shot.json），而且只写 9 个字段。
#       而 UI 面板读的是 data/strategy_effectiveness_ui_snapshot.json，
#       需要 schema_version/stat_date/mode/maturity/recommendation/stats/
#       history/index_quotes/index_coverage 等 15 个字段
#       （契约见 core/FIELD_CONTRACT.md 与 docs/SYSTEM_OVERVIEW.md 8.18.5）。
#       结果：daemon 18:30 跑完，面板数据永远停在最后一次手工生成的那版。
# 修复：两条落盘路径彻底分离，UI 快照按契约重建。
#       一次性快照仍由 quick_effectiveness_snapshot(write_files=True) 与
#       scripts/strategy_effectiveness_one_shot.py 负责，互不覆盖。
# ============================================================================
UI_SNAPSHOT_JSON = os.path.join(ROOT, "data", "strategy_effectiveness_ui_snapshot.json")
RECOMMENDATION_JSON = os.path.join(ROOT, "data", "strategy_weight_recommendation.json")

_SNAPSHOT_NOTICE = "只读快照：供 Streamlit 战法胜率面板展示，禁止用于自动改权重"
_SNAPSHOT_SOURCE = "auto_sniper_daemon._signal_backfill_pipeline"

HOLD_DAYS = (1, 2, 5)
SCORE_BUCKETS = (50.0, 60.0, 70.0, 80.0)

# 快照缓存：同一 (db, days, hold) 在 TTL 内复用，避免 UI 反复点就重算
_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()
_CACHE_TTL_SEC = 300.0


# ============================ 基础工具 ============================

def _wilson_lo(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson 95% 置信下界。小样本时裸胜率严重高估，用它排序。"""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1.0 + (z * z) / n
    centre = p + (z * z) / (2.0 * n)
    spread = z * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * n)) / n)
    return max(0.0, (centre - spread) / denom)


def _r(v: Optional[float], nd: int = 4) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:
        return None
    return round(f, nd)


def _median(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    m = n // 2
    return s[m] if n % 2 else (s[m - 1] + s[m]) / 2.0


def _now_iso() -> str:
    return datetime.now(BJ_TZ).isoformat()


def _bare(code: str) -> str:
    c = str(code).strip()
    return c.split(".")[0] if "." in c else c


def _suffixed(code: str) -> str:
    """
    补齐交易所后缀。

    【V30.10 2026-10-21 口径统一】改用 core.signal_ledger_bridge.with_exchange_suffix，
    与生产 daily_data 实测口径完全一致（6*→.SH / 0~3*→.SZ / 4,8,9*→.BJ）。
    旧实现把 9 开头判成 .SH，导致北交所 12 只票全部 join miss。
    """
    try:
        from core.signal_ledger_bridge import with_exchange_suffix

        return with_exchange_suffix(code)
    except Exception:
        c = str(code).strip().upper()
        if not c:
            return c
        if "." in c:
            return c
        if len(c) == 6 and c.isdigit():
            if c.startswith("6"):
                return c + ".SH"
            if c.startswith(("4", "8", "9")):
                return c + ".BJ"
            return c + ".SZ"
        return c


def _bench_code(code: str) -> str:
    """个股 -> 基准指数。30 创业板、6/9 沪市、0/2 深市、4/8 北交所(无基准，退沪深300)。"""
    c = _bare(code)
    if c.startswith("30"):
        return "399006.SZ"
    if c.startswith("6") or c.startswith("9"):
        return "000300.SH"
    return "399001.SZ"


def _exists(con, tbl: str) -> bool:
    try:
        return con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=? LIMIT 1", [tbl]
        ).fetchone() is not None
    except Exception:
        return False


def _open_ro(path: str):
    import duckdb
    return duckdb.connect(path, read_only=True)


# ============================ 采样累加器 ============================

class Acc:
    """
    单样本累加器。

    【V30.10 2026-10-21】新增基准对比能力：
    一次性脚本的 Acc.out(bench=...) 会给出「跑赢基准」的比例，
    本模块原先只算绝对收益，缺失了最关键的「超额」维度。
    现补齐：每个样本同时记录其基准同期累计涨幅 → 可算超额、跑赢率。
    """
    __slots__ = ("n", "wins", "rets", "dds", "mps", "codes",
                 "exc", "bench_rets", "beat_n")

    def __init__(self) -> None:
        self.n = 0
        self.wins = 0
        self.rets: List[float] = []
        self.dds: List[float] = []
        self.mps: List[float] = []
        self.codes: set = set()
        self.exc: List[float] = []          # 超额收益（%）
        self.bench_rets: List[float] = []   # 基准同期涨幅（%）
        self.beat_n = 0                     # 跑赢基准的样本数

    def add(self, ret: float, dd: Optional[float], mp: Optional[float],
            code: str = "", bench: Optional[float] = None) -> None:
        self.n += 1
        if ret > 0:
            self.wins += 1
        self.rets.append(ret)
        if dd is not None:
            self.dds.append(dd)
        if mp is not None:
            self.mps.append(mp)
        if code:
            self.codes.add(code)
        if bench is not None:
            self.bench_rets.append(bench)
            self.exc.append(ret - bench)
            if ret > bench:
                self.beat_n += 1

    def summary(self) -> Dict[str, Any]:
        n = self.n
        if n <= 0:
            return {"n": 0}
        rets = self.rets
        out: Dict[str, Any] = {
            "n": n,
            "wins": self.wins,
            "win_rate": round(self.wins / n, 4),
            "wilson_lo": round(_wilson_lo(self.wins, n), 4),
            "avg_return": _r(sum(rets) / len(rets)) if rets else None,
            "median_return": _r(_median(rets)),
        }
        if self.dds:
            out["avg_max_drawdown"] = _r(sum(self.dds) / len(self.dds))
        if self.mps:
            out["avg_max_profit"] = _r(sum(self.mps) / len(self.mps))
        out["ge5_rate"] = round(sum(1 for x in rets if x >= 5.0) / n, 4)
        out["le5_rate"] = round(sum(1 for x in rets if x <= -5.0) / n, 4)
        # ---- 基准对比（无基准时全部 None，绝不用 0.0 冒充） ----
        if self.bench_rets:
            out["avg_bench"] = _r(sum(self.bench_rets) / len(self.bench_rets))
            out["avg_excess"] = _r(sum(self.exc) / len(self.exc))
            out["median_excess"] = _r(_median(self.exc))
            out["beat_bench_rate"] = round(self.beat_n / len(self.exc), 4)
        return out


# ============================ 数据装载 ============================

def _calendar(con) -> List[str]:
    if _exists(con, "dim_trade_calendar"):
        rows = con.execute(
            "SELECT DISTINCT trade_date FROM dim_trade_calendar ORDER BY trade_date"
        ).fetchall()
        if rows:
            return [str(r[0])[:10] for r in rows]
    for tbl in ("daily_data", "vw_daily_data_compat", "bars_daily"):
        if _exists(con, tbl):
            rows = con.execute(
                f"SELECT DISTINCT trade_date FROM {tbl} ORDER BY trade_date"
            ).fetchall()
            if rows:
                return [str(r[0])[:10] for r in rows]
    return []


def _pick_price_table(con) -> str:
    """选行情表：优先 daily_data，兜底 vw_daily_data_compat / bars_daily。"""
    for tbl in ("daily_data", "vw_daily_data_compat", "bars_daily"):
        if _exists(con, tbl):
            return tbl
    return "daily_data"


def _load(con, days: int, src: str) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """读信号 + 对应票的日线，全部进内存（百级信号，内存无压力）。"""
    cal = _calendar(con)
    if not cal:
        return [], {"error": "交易日历为空"}

    start_i = max(0, len(cal) - days)
    start_d = cal[start_i]
    meta: Dict[str, Any] = {
        "calendar_total_days": len(cal),
        "calendar_range": [cal[0], cal[-1]],
        "window_start": start_d,
        "window_end": cal[-1],
        "window_days": len(cal) - start_i,
        "src_table": src,
    }

    try:
        rows = con.execute(
            """
            SELECT trade_date, pool, ts_code, score, push_price, push_time,
                   strategy, strategy_ids, signal_uid, is_observation
            FROM signal_log
            WHERE push_price IS NOT NULL AND push_price > 0
              AND trade_date >= ?
            ORDER BY trade_date, pool, score DESC
            """,
            [start_d],
        ).fetchall()
    except Exception as e:
        logger.warning("[snapshot] 读 signal_log 失败: %s", e)
        return [], meta

    meta["signals_in_window"] = len(rows)
    if not rows:
        return [], meta

    codes_raw = sorted({str(r[2]) for r in rows})
    codes = sorted({_suffixed(c) for c in codes_raw})
    meta["unique_codes"] = len(codes_raw)

    ph = ",".join(["?"] * len(codes))
    try:
        px = con.execute(
            f"""
            SELECT ts_code, trade_date, open, high, low, close, pre_close
            FROM {src}
            WHERE ts_code IN ({ph}) AND trade_date >= ?
            ORDER BY ts_code, trade_date
            """,
            codes + [start_d],
        ).fetchall()
    except Exception as e:
        logger.warning("[snapshot] 读 %s 失败: %s", src, e)
        return [], meta

    series: Dict[str, Dict[str, Tuple]] = {}
    for code, td, o, h, l, c, pc in px:
        series.setdefault(str(code), {})[str(td)[:10]] = (o, h, l, c, pc)

    idx_of = {d: i for i, d in enumerate(cal)}
    signals: List[Dict[str, Any]] = []
    for td, pool, code, score, push_price, push_time, strategy, sids, uid, is_obs in rows:
        td = str(td)[:10]
        i = idx_of.get(td, -1)
        if i < 0 or not push_price or float(push_price) <= 0:
            continue
        signals.append({
            "trade_date": td, "pool": str(pool or "").lower(), "ts_code": str(code),
            "score": float(score) if score is not None else None,
            "push_price": float(push_price), "push_time": str(push_time) if push_time else None,
            "strategy": strategy or "", "strategy_ids": sids or "",
            "signal_uid": uid or "", "is_observation": bool(is_obs),
            "_i": i, "_bars": series.get(_suffixed(str(code)), {}),
        })

    meta["signals_usable"] = len(signals)
    meta["signals_with_bars"] = sum(1 for s in signals if s["_bars"])
    return signals, meta


def _bench_map(con, start_d: str) -> Dict[str, Dict[str, float]]:
    """{index_code: {trade_date: pct_chg}}"""
    out: Dict[str, Dict[str, float]] = {}
    if not _exists(con, "index_daily"):
        return out
    try:
        rows = con.execute(
            "SELECT ts_code, trade_date, pct_chg FROM index_daily "
            "WHERE trade_date >= ? AND pct_chg IS NOT NULL",
            [start_d],
        ).fetchall()
    except Exception as e:
        logger.warning("[snapshot] index_daily 读取失败: %s", e)
        return out
    for code, td, pch in rows:
        out.setdefault(str(code), {})[str(td)[:10]] = float(pch)
    return out


def _bench_cum(bm: Dict[str, Dict[str, float]], code: str, i: int, n: int,
               cal: List[str]) -> Optional[float]:
    """从第 i 个交易日起，持有 n 个交易日该基准的累计涨幅 %。"""
    d = bm.get(code)
    if not d:
        return None
    acc = 1.0
    got = 0
    for j in range(i + 1, min(len(cal), i + 1 + n)):
        v = d.get(cal[j])
        if v is not None:
            acc *= (1.0 + v / 100.0)
        got += 1
    return (acc - 1.0) * 100.0 if got else None


def _dd_mp(bars: Dict[str, Tuple], cal: List[str], i: int, n: int,
           entry: float) -> Tuple[Optional[float], Optional[float]]:
    """持有期最大回撤 % 与最大浮盈 %（相对入场价）。"""
    hi = None
    lo_after_hi = None
    peak = None
    for j in range(i + 1, min(len(cal), i + 1 + n)):
        b = bars.get(cal[j])
        if not b:
            continue
        h, l = b[1], b[2]
        if h is None or l is None:
            continue
        if peak is None or h > peak:
            peak = h
            lo_after_hi = l
        elif lo_after_hi is None or l < lo_after_hi:
            lo_after_hi = l
    dd = mp = None
    if peak is not None and entry > 0:
        mp = (peak / entry - 1.0) * 100.0
    if lo_after_hi is not None and entry > 0:
        dd = (lo_after_hi / entry - 1.0) * 100.0
    return dd, mp


def _ids(raw) -> List[str]:
    out: List[str] = []
    if raw is None:
        return out
    if isinstance(raw, (list, tuple, set)):
        for x in raw:
            s = str(x).strip().strip('"').strip("'")
            if s and s not in out:
                out.append(s)
        return out
    s = str(raw).strip()
    if not s:
        return out
    if s.startswith("["):
        try:
            return _ids(json.loads(s))
        except Exception:
            pass
    for token in s.replace("|", ",").replace(";", ",").split(","):
        t = token.strip().strip('"').strip("'")
        if t and t not in out:
            out.append(t)
    return out


def _labels(raw) -> Dict[str, str]:
    if not raw:
        return {}
    try:
        from core.strategy_id_registry import parse_labeled_atomic_strategies
        return {cid: lbl for cid, lbl in parse_labeled_atomic_strategies(str(raw))}
    except Exception:
        return {}


# ============================ 核心计算 ============================

def compute(signals: List[Dict[str, Any]], cal: List[str],
            bm: Dict[str, Dict[str, float]],
            hold: Tuple[int, ...] = HOLD_DAYS) -> Dict[str, Any]:
    """按战法 / 池 / 分数桶 / 信号 四个维度聚合 T+N 表现。"""
    by_s: Dict[str, Dict[int, Acc]] = {}
    by_pool: Dict[str, Dict[str, Dict[int, Acc]]] = {}
    by_bk: Dict[float, Dict[str, Dict[int, Acc]]] = {b: {} for b in SCORE_BUCKETS}
    by_uid: Dict[str, Dict[int, Acc]] = {}
    labels: Dict[str, str] = {}

    for sig in signals:
        i = sig["_i"]
        entry = float(sig["push_price"])
        bars = sig.get("_bars") or {}
        code = sig.get("ts_code") or ""
        sids = _ids(sig.get("strategy_ids"))
        if not sids:
            sids = _ids(sig.get("strategy"))
        if sig.get("strategy"):
            labels.update(_labels(sig.get("strategy")))

        pool = sig.get("pool") or ""
        score = sig.get("score")
        uid = sig.get("signal_uid") or ""
        bench_code = _bench_code(code)

        for n in hold:
            j = i + n
            if j >= len(cal):
                continue
            b = bars.get(cal[j])
            if not b:
                continue
            close = b[3]
            if close is None or float(close) <= 0:
                continue
            ret = (float(close) / entry - 1.0) * 100.0
            dd, mp = _dd_mp(bars, cal, i, n, entry)
            bp = _bench_cum(bm, bench_code, i, n, cal)

            for sid in sids:
                a = by_s.setdefault(sid, {}).setdefault(n, Acc())
                a.add(ret, dd, mp, code, bp)

            pa = by_pool.setdefault(pool, {}).setdefault(sid or "(无战法)", {}).setdefault(n, Acc())
            pa.add(ret, dd, mp, code, bp)

            if score is not None:
                for bk in SCORE_BUCKETS:
                    if float(score) >= bk:
                        ba = by_bk[bk].setdefault(sid or "(无战法)", {}).setdefault(n, Acc())
                        ba.add(ret, dd, mp, code, bp)

            if uid:
                ua = by_uid.setdefault(uid, {}).setdefault(n, Acc())
                ua.add(ret, dd, mp, code, bp)

    return {
        "by_strategy": {k: {str(n): v.summary() for n, v in d.items()} for k, d in by_s.items()},
        "by_pool": {
            p: {s: {str(n): a.summary() for n, a in d.items()} for s, d in sd.items()}
            for p, sd in by_pool.items()
        },
        "by_score_bucket": {
            str(bk): {s: {str(n): a.summary() for n, a in d.items()} for s, d in sd.items()}
            for bk, sd in by_bk.items()
        },
        "by_signal": {k: {str(n): v.summary() for n, v in d.items()} for k, d in by_uid.items()},
        "labels": labels,
    }


# ============================ 对外主 API ============================

def quick_effectiveness_snapshot(
    *,
    db_path: Optional[str] = None,
    days: int = 90,
    hold: Tuple[int, ...] = HOLD_DAYS,
    min_n: int = 1,
    use_cache: bool = True,
    write_files: bool = False,
    db_conn=None,
) -> Dict[str, Any]:
    """
    【常驻 API】战法有效性即时快照（只读、不落业务表）。

    参数：
        db_path     : DuckDB 路径，默认生产库
        days        : 回看交易日数
        hold        : 持有档位，默认 (1, 2, 5)
        min_n       : 展示时最小样本数（过滤噪声）
        use_cache   : 是否使用 TTL 缓存（UI 反复点时省算力）
        write_files : 是否额外落盘 json/md（默认否，纯内存返回）
        db_conn    : 【V30.10】复用已有 DuckDB 连接。
                     DuckDB 不允许同一文件同时存在不同配置的连接
                     （read_only=True 与 read_only=False 冲突 → 
                     "Can't open a connection to same database file with a
                     different configuration"）。
                     daemon 的 18:30 流水线里已有写连接，
                     此处必须复用，否则快照/标定一律失败。

    返回：
        {
          "generated_at": str,
          "meta": {...},
          "by_strategy": {...},
          "by_pool": {...},
          "by_score_bucket": {...},
          "by_signal": {...},
          "labels": {...},
          "ranking": [...],       # 按 Wilson 下界排序的战法榜
          "error": str|None,
        }
    """
    db = db_path or DEFAULT_DB
    key = "%s|%s|%s" % (db, int(days), ",".join(str(x) for x in hold))

    if use_cache:
        with _CACHE_LOCK:
            hit = _CACHE.get(key)
            if hit is not None:
                import time as _t
                if _t.time() - hit[0] < _CACHE_TTL_SEC:
                    return hit[1]

    result: Dict[str, Any] = {
        "generated_at": _now_iso(),
        "meta": {},
        "by_strategy": {},
        "by_pool": {},
        "by_score_bucket": {},
        "by_signal": {},
        "labels": {},
        "ranking": [],
        "error": None,
    }

    con = None
    _own_con = False
    try:
        if db_conn is not None:
            con = db_conn
        else:
            con = _open_ro(db)
            _own_con = True
        src = _pick_price_table(con)
        signals, meta = _load(con, days, src)
        result["meta"] = meta
        if not signals:
            result["error"] = meta.get("error") or "窗口内无带推送价的信号"
            return result

        cal = _calendar(con)
        bm = _bench_map(con, meta.get("window_start") or cal[0])
        computed = compute(signals, cal, bm, hold)
        result.update({
            "by_strategy": computed["by_strategy"],
            "by_pool": computed["by_pool"],
            "by_score_bucket": computed["by_score_bucket"],
            "by_signal": computed["by_signal"],
            "labels": computed["labels"],
        })

        # 战法榜：按各档位的 Wilson 下界排序（样本少时用下界更稳）
        ranking: List[Dict[str, Any]] = []
        for sid, per_n in result["by_strategy"].items():
            best_n = None
            best_lo = -1.0
            for n, s in per_n.items():
                if int(s.get("n", 0)) < int(min_n):
                    continue
                lo = float(s.get("wilson_lo", 0.0) or 0.0)
                if lo > best_lo:
                    best_lo = lo
                    best_n = n
            if best_n is None:
                continue
            s = per_n[best_n]
            ranking.append({
                "strategy_id": sid,
                "label": result["labels"].get(sid, ""),
                "best_n_days": int(best_n),
                "n": s.get("n", 0),
                "win_rate": s.get("win_rate"),
                "wilson_lo": s.get("wilson_lo"),
                "avg_return": s.get("avg_return"),
                "median_return": s.get("median_return"),
                "avg_max_drawdown": s.get("avg_max_drawdown"),
                "avg_max_profit": s.get("avg_max_profit"),
                "ge5_rate": s.get("ge5_rate"),
                "le5_rate": s.get("le5_rate"),
                # ---- 基准对比（无基准时 None） ----
                "avg_bench": s.get("avg_bench"),
                "avg_excess": s.get("avg_excess"),
                "median_excess": s.get("median_excess"),
                "beat_bench_rate": s.get("beat_bench_rate"),
            })
        ranking.sort(key=lambda x: (float(x.get("wilson_lo") or 0.0),
                                     float(x.get("avg_return") or -99.0)), reverse=True)
        result["ranking"] = ranking

    except Exception as e:
        logger.warning("[snapshot] 计算失败: %s", e)
        result["error"] = f"{type(e).__name__}: {e}"
    finally:
        # 【V30.10】只关自己开的连接，借来的（db_conn）绝不关闭
        if con is not None and _own_con:
            try:
                con.close()
            except Exception:
                pass

    if use_cache and result.get("error") is None:
        import time as _t
        with _CACHE_LOCK:
            _CACHE[key] = (_t.time(), result)

    if write_files:
        try:
            _write_outputs(result)
        except Exception as e:
            logger.warning("[snapshot] 落盘失败: %s", e)

    return result


def _write_outputs(result: Dict[str, Any]) -> None:
    """落盘 json + md（与一次性脚本同名文件，保持既有下游消费方可用）。"""
    payload = {
        "generated_at": result.get("generated_at"),
        "meta": result.get("meta"),
        "by_strategy": result.get("by_strategy"),
        "by_pool": result.get("by_pool"),
        "by_score_bucket": result.get("by_score_bucket"),
        "by_signal": result.get("by_signal"),
        "labels": result.get("labels"),
        "ranking": result.get("ranking"),
    }
    tmp = OUT_JSON + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT_JSON)

    lines: List[str] = []
    lines.append("# 战法有效性即时快照")
    lines.append("")
    lines.append("- 生成时间：%s" % result.get("generated_at"))
    m = result.get("meta") or {}
    lines.append("- 窗口：%s ~ %s（%s 个交易日）" % (
        m.get("window_start"), m.get("window_end"), m.get("window_days")))
    lines.append("- 样本：%s 条可用 / %s 条在窗口" % (
        m.get("signals_usable"), m.get("signals_in_window")))
    lines.append("")
    lines.append("## 战法榜（按 Wilson 95% 置信下界）")
    lines.append("")
    lines.append("| 战法 | 标签 | 档 | 样本 | 胜率 | Wilson下界 | 均收益 | 中位收益 | 均回撤 |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in (result.get("ranking") or []):
        lines.append("| %s | %s | T+%s | %s | %s | %s | %s | %s | %s |" % (
            r.get("strategy_id"), r.get("label") or "-", r.get("best_n_days"),
            r.get("n"),
            ("%.1f%%" % (float(r["win_rate"]) * 100)) if r.get("win_rate") is not None else "-",
            ("%.1f%%" % (float(r["wilson_lo"]) * 100)) if r.get("wilson_lo") is not None else "-",
            ("%+.2f%%" % float(r["avg_return"])) if r.get("avg_return") is not None else "-",
            ("%+.2f%%" % float(r["median_return"])) if r.get("median_return") is not None else "-",
            ("%.2f%%" % float(r["avg_max_drawdown"])) if r.get("avg_max_drawdown") is not None else "-",
        ))
    tmp = OUT_MD + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, OUT_MD)


def strategy_effectiveness_report(
    days: int = 90,
    min_n: int = 3,
    **kwargs: Any,
) -> Dict[str, Any]:
    """
    给 UI / 运维用的简版报告：只返回战法榜 + 池级汇总 + 错误信息。
    """
    snap = quick_effectiveness_snapshot(days=days, min_n=min_n, **kwargs)
    return {
        "generated_at": snap.get("generated_at"),
        "error": snap.get("error"),
        "meta": snap.get("meta"),
        "ranking": snap.get("ranking"),
        "by_pool": snap.get("by_pool"),
        "labels": snap.get("labels"),
    }


# ============================================================================
# 【Tier2.7 修复】UI 快照生成
# ============================================================================

def _read_json_file(path: str) -> Optional[Dict[str, Any]]:
    """安全读 JSON；文件缺失/损坏一律返回 None（绝不抛异常）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception as e:
        logger.debug("[snapshot] 读取 %s 失败: %s", os.path.basename(path), e)
        return None


def _max_stat_date(db_conn) -> str:
    """取 strategy_effectiveness 的最新统计日（YYYY-MM-DD）；取不到返回空串。"""
    if db_conn is None:
        return ""
    try:
        rows = db_conn.execute(
            "SELECT MAX(CAST(stat_date AS VARCHAR)) FROM strategy_effectiveness"
        ).fetchall()
        if rows and rows[0] and rows[0][0]:
            return str(rows[0][0])[:10]
    except Exception as e:
        logger.debug("[snapshot] 取 stat_date 失败: %s", e)
    return ""


def _collect_history(db_conn, lookback_days: int = 30) -> List[Dict[str, Any]]:
    """
    采集 strategy_effectiveness 近 N 天每日行。
    失败降级为 []，**不阻断快照生成**。
    """
    if db_conn is None:
        return []
    try:
        rows = db_conn.execute(
            "SELECT CAST(stat_date AS VARCHAR), pool, strategy, stat_n_days, "
            "n_signals, n_evaluated, n_win, win_rate, avg_return "
            "FROM strategy_effectiveness "
            "WHERE stat_date >= CURRENT_DATE - INTERVAL '%d days' "
            "ORDER BY CAST(stat_date AS VARCHAR), strategy" % int(lookback_days)
        ).fetchall()
    except Exception as e:
        logger.warning("[snapshot] history 采集失败: %s", e)
        return []
    out: List[Dict[str, Any]] = []
    for r in rows or []:
        try:
            out.append({
                "stat_date": str(r[0] or "")[:10],
                "pool": str(r[1] or ""),
                "strategy": str(r[2] or ""),
                "stat_n_days": int(r[3]) if r[3] is not None else None,
                "n_signals": int(r[4] or 0),
                "n_evaluated": int(r[5] or 0),
                "n_win": int(r[6] or 0),
                "win_rate": None if r[7] is None else float(r[7]),
                "avg_return": None if r[8] is None else float(r[8]),
            })
        except Exception:
            continue
    return out


def _collect_index_quotes(db_conn) -> List[Dict[str, Any]]:
    """采集 4 个基准指数最新行情（市场水位条）；失败降级为 []。"""
    if db_conn is None:
        return []
    try:
        from core import _pyarrow_compat as _pac

        _pac.disable_pyarrow()
        from data.index_daily_store import latest_index_quotes

        quotes = latest_index_quotes(db_conn)
        return quotes if isinstance(quotes, list) else []
    except Exception as e:
        logger.warning("[snapshot] index_quotes 采集失败: %s", e)
        return []


def _collect_index_coverage(db_conn) -> Dict[str, Any]:
    """采集基准指数覆盖度；失败降级为 primary_ready=False。"""
    cov: Dict[str, Any] = {"available_n_days": [], "primary_ready": False, "per_index": {}}
    if db_conn is None:
        return cov
    try:
        rows = db_conn.execute(
            "SELECT ts_code, COUNT(*), MIN(CAST(trade_date AS VARCHAR)), "
            "MAX(CAST(trade_date AS VARCHAR)) FROM index_daily GROUP BY ts_code"
        ).fetchall()
    except Exception as e:
        logger.debug("[snapshot] index_coverage 采集失败: %s", e)
        return cov
    per: Dict[str, Dict[str, Any]] = {}
    for r in rows or []:
        code = str(r[0] or "")
        if not code:
            continue
        per[code] = {
            "n": int(r[1] or 0),
            "start": str(r[2] or "")[:10],
            "end": str(r[3] or "")[:10],
        }
    cov["per_index"] = per
    cov["available_n_days"] = sorted(per.keys())
    try:
        from data.index_daily_store import INDEX_CODES
    except Exception:
        INDEX_CODES = ("000001.SH", "399001.SZ", "399006.SZ", "000300.SH")
    # primary_ready：4 个基准指数各至少 2 个交易日，超额口径才可用
    cov["primary_ready"] = all(
        int((per.get(c) or {}).get("n", 0)) >= 2 for c in INDEX_CODES
    )
    return cov


def build_ui_snapshot(db_conn=None, lookback_days: int = 30) -> str:
    """
    生成 UI 面板用的只读快照（只写 1 个 JSON 文件，不碰任何业务表）。

    【为什么必须有这个函数】
    auto_sniper_daemon.py 的 _signal_backfill_pipeline() 里写着：
        from core.strategy_effectiveness_snapshot import build_ui_snapshot
    但该模块在 V30.10 之前【根本不存在】——
    这意味着 daemon 的 18:30 回填流水线每一次都会 ImportError，
    于是 T+N 收益回填、战法有效性聚合、战法权重推荐【三件事全部从未执行】。
    （表现：signal_pnl 长期为空 / strategy_effectiveness 只有 33 行 / 权重推荐 json 从不更新）

    【Tier2.7 修复】
    旧实现把 quick_effectiveness_snapshot() 的 9 字段结果写到
    data/strategy_effectiveness_one_shot.json —— 那是【一次性快照】的路径，
    而 UI 面板读的是 data/strategy_effectiveness_ui_snapshot.json，
    需要 schema_version/stat_date/mode/maturity/recommendation/stats/history/
    index_quotes/index_coverage 等 15 个字段。
    后果：daemon 每晚跑完，面板数据永远停在最后一次手工生成的那版。
    现按 core/FIELD_CONTRACT.md 的契约重建 UI 快照，并与一次性快照彻底分离。

    本函数契约：
      入参：一个可选的 DuckDB 连接（给了就用，没给就自己开只读连接）
            lookback_days：history 采集窗口（交易日），默认 30
      返回：快照 JSON 的落盘路径

    【红线】只读查询；只写 1 个 JSON 文件；不写 signal_log / signal_pnl /
    strategy_effectiveness / 任何权重文件。
    """
    path = UI_SNAPSHOT_JSON
    own_con = False
    con = db_conn
    if con is None:
        try:
            con = _open_ro(DEFAULT_DB)
            own_con = True
        except Exception as e:
            logger.warning("[snapshot] 无法打开只读连接: %s", e)
            con = None

    try:
        # ---- 主数据源：优化器产出的推荐报表 ----
        rec_doc = _read_json_file(RECOMMENDATION_JSON) or {}

        def _dict_of(key: str) -> Dict[str, Any]:
            v = rec_doc.get(key)
            return v if isinstance(v, dict) else {}

        stats = _dict_of("stats")
        recommendation = _dict_of("recommendation")
        # 【Tier2.5】净口径推荐表（与旧 recommendation 并列，缺失时为空 dict）
        net_recommendation = _dict_of("net_recommendation")
        maturity = dict(_dict_of("maturity"))
        strategy_maturity = _dict_of("strategy_maturity")
        dep = rec_doc.get("deprecated")
        deprecated = [str(x) for x in dep] if isinstance(dep, (list, tuple)) else []
        mode = str(rec_doc.get("mode") or "statistics_only").strip() or "statistics_only"

        # ---- 统计日（MAX(stat_date)）----
        stat_date = _max_stat_date(con)
        if stat_date:
            maturity["stat_date"] = stat_date

        # ---- 趋势 / 行情 / 覆盖度（任一失败都只降级，不阻断）----
        history = _collect_history(con, lookback_days)
        index_quotes = _collect_index_quotes(con)
        index_coverage = _collect_index_coverage(con)

        payload = {
            "schema_version": "1.0",
        "generated_at": _now_iso(),
        "stat_date": stat_date,
            "mode": mode,
        "maturity": maturity,
            "strategy_maturity": strategy_maturity,
            "recommendation": recommendation,
            "net_recommendation": net_recommendation,
            "deprecated": deprecated,
            "stats": stats,
        "history": history,
        "index_quotes": index_quotes,
        "index_coverage": index_coverage,
            "source": _SNAPSHOT_SOURCE,
            "notice": _SNAPSHOT_NOTICE,
        }
    except Exception as e:
        logger.warning("[snapshot] UI 快照组装失败: %s", e)
        payload = {
            "schema_version": "1.0",
            "generated_at": _now_iso(),
            "stat_date": "",
            "mode": "statistics_only",
            "maturity": {},
            "strategy_maturity": {},
            "recommendation": {},
            "net_recommendation": {},
            "deprecated": [],
            "stats": {},
            "history": [],
            "index_quotes": [],
            "index_coverage": {"available_n_days": [], "primary_ready": False,
                               "per_index": {}},
            "source": _SNAPSHOT_SOURCE,
            "notice": _SNAPSHOT_NOTICE,
            "error": "%s: %s" % (type(e).__name__, e),
        }
    finally:
        if own_con and con is not None:
            try:
                con.close()
            except Exception:
                pass

    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)
    except Exception as e:
        logger.warning("[snapshot] UI 快照落盘失败: %s", e)
        return path
    logger.info(
        "[snapshot] UI 快照已生成: %s（战法 %d / 历史行 %d / 指数 %d）",
        path, len(payload.get("stats") or {}), len(payload.get("history") or []),
        len(payload.get("index_quotes") or []),
    )
    return path


__all__ = [
    "quick_effectiveness_snapshot",
    "strategy_effectiveness_report",
    "build_ui_snapshot",
    "UI_SNAPSHOT_JSON",
    "compute",
    "Acc",
    "HOLD_DAYS",
    "DEFAULT_DB",
]
