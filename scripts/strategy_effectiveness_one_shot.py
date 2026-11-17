# -*- coding: utf-8 -*-
"""
strategy_effectiveness_one_shot.py
========================================================================
一次性战法胜率快照（只读 / 不依赖 18:30 回填 / 不改权重 / 不写业务表）
========================================================================

【定位】
回答你的 #3 问题："缺策略有效性归因 —— 没看到各战法样本外胜率"。
直接把答案从硬盘算出来，产出：
    data/strategy_effectiveness_one_shot.json
    data/strategy_effectiveness_one_shot.md

【为什么不用 strategy_effectiveness 表】
1. 该表仅 69 行、只覆盖 2026-10-08~10-21（10 个交易日），样本极小；
2. signal_pnl 仅 308 行，直接出"胜率"会误导决策；
3. 故本脚本从 signal_log × 行情表重算，用交易日历正确定位 T+N 收盘，
   支持任意回看窗口，且不受 strategy_effectiveness 是否已回填的限制。

【核心口径】
- 入场价：signal_log.push_price（推送价，含 push_time 可审计）
- T+N 收盘：ts_code 在 trade_date 之后第 N 个【真实交易日】的 close
- 交易日历：dim_trade_calendar（避开节假日），兜底 daily_data DISTINCT trade_date
- 胜率：return_pct > 0（与 signal_pnl.win 语义一致）
- 同时输出 avg_return / 中位数 / 平均最大回撤 / 平均最大浮盈 / ≥5% / ≤-5% 占比
- Wilson 95% 置信下界：样本极少时防止裸胜率自欺（n=1 全胜 -> 下界 0%）

【红线】
只读 DuckDB；只写 2 个 json/md；不碰 signal_log / strategy_effectiveness /
strategy_weights.json / deprecated_strategies.json / 任何策略代码。

用法：
    python scripts/strategy_effectiveness_one_shot.py --days 90
    python scripts/strategy_effectiveness_one_shot.py --days 180 --min-n 5
========================================================================
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
logger = logging.getLogger("eff")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

BJ_TZ = timezone(timedelta(hours=8))
DEFAULT_DB = os.path.join(ROOT, "data", "quant_data.duckdb")
OUT_JSON = os.path.join(ROOT, "data", "strategy_effectiveness_one_shot.json")
OUT_MD = os.path.join(ROOT, "data", "strategy_effectiveness_one_shot.md")

HOLD_DAYS = (1, 2, 5)
SCORE_BUCKETS = (50.0, 60.0, 70.0, 80.0)

# 你点名的 6 类右侧策略 -> 本系统里的原子战法 ID
# 【V35 编号重整】全部映射到新连续编号（旧→新对照见各 screener 模块头 docstring）
ASK_TO_IDS: Dict[str, List[str]] = {
    "① 均线多头排列 + 回踩 5/10 日线企稳": ["P3-02", "P5-07", "P5-08", "P5-09"],
    "② 平台 / 箱体突破": ["P3-05", "P5-15", "P5-16"],
    "② 平台 / 箱体突破 (P3-05 盘中平台二次确认)": ["P3-05"],
    "② 平台 / 箱体突破 (P5 箱体突破回踩)": ["P5-15", "P5-16"],
    "③ 量价齐升 + 板块共振": ["P3-01", "P3-07", "P5-02", "P5-03"],
    "⑦ 新高突破 + 缩量回踩": ["P5-17", "P5-18", "P5-19", "P5-21", "P5-24"],
    "⑧ MACD 金叉确认 (带过滤器)": ["P5-19", "P5-07", "P5-25"],
    "⑨ 顶部结构反转 (V型 / 双底)": [],
    # 【V34+ 新增】外部策略移植（_ext_repos）对应的原子战法
    "⑩ 双均线金叉（5/20 上穿）": ["P5-22", "P4-08"],
    "⑪ 布林上轨突破（20日2σ）": ["P5-23"],
    "⑫ 欧奈尔趋势模板（CANSLIM）": ["P5-14"],
    "⑬ RSI 超跌反转（观察档）": ["P3-11"],
}


# ============================ 基础工具 ============================

def _wilson_lo(wins: int, n: int, z: float = 1.96) -> float:
    """Wilson 95% 置信下界。小样本时裸胜率严重高估，用它排序。"""
    if n <= 0:
        return 0.0
    p = wins / n
    denom = 1.0 + (z * z) / n
    centre = p + (z * z) / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) + (z * z) / (4.0 * n)) / n)
    return max(0.0, min(1.0, (centre - margin) / denom))


def _r(v: Optional[float], nd: int = 4) -> Optional[float]:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
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


def _r2(v: Optional[float], nd: int = 2) -> Optional[float]:
    """给 markdown 用的百分比格式化。"""
    if v is None:
        return None
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return None


# ============================ 样本累加器 ============================

class Acc:
    __slots__ = ("n", "wins", "rets", "dds", "mps", "codes")

    def __init__(self) -> None:
        self.n = 0
        self.wins = 0
        self.rets: List[float] = []
        self.dds: List[float] = []
        self.mps: List[float] = []
        self.codes: set = set()

    def add(self, ret: float, dd: Optional[float], mp: Optional[float], code: str) -> None:
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

    def out(self, bench: Optional[float] = None) -> Dict[str, Any]:
        if self.n == 0:
            return {"n": 0}
        wr = self.wins / self.n
        avg = sum(self.rets) / self.n
        d = {
            "n": self.n,
            "n_codes": len(self.codes),
            "win_rate": _r(wr),
            "win_rate_wilson_lo": _r(_wilson_lo(self.wins, self.n)),
            "avg_return_pct": _r(avg, 3),
            "median_return_pct": _r(_median(self.rets), 3),
            "avg_max_drawdown_pct": _r(sum(self.dds) / len(self.dds), 3) if self.dds else None,
            "avg_max_profit_pct": _r(sum(self.mps) / len(self.mps), 3) if self.mps else None,
            "pct_ge_5": _r(sum(1 for x in self.rets if x >= 5.0) / self.n),
            "pct_le_n5": _r(sum(1 for x in self.rets if x <= -5.0) / self.n),
        }
        if bench is not None:
            d["excess_pct"] = _r(avg - bench, 3)
            d["beat_bench"] = _r(sum(1 for x in self.rets if x > bench) / self.n)
        return d


# ============================ 行情 / 信号读取 ============================

def _open_ro(path: str):
    import duckdb
    return duckdb.connect(path, read_only=True)


def _exists(con, tbl: str) -> bool:
    try:
        return con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name=? LIMIT 1", [tbl]
        ).fetchone() is not None
    except Exception:
        return False


def _bench_code(code: str) -> str:
    """个股 -> 基准指数。30 创业板、6/9 沪市、0/2 深市、4/8 北交所(无基准，退沪深300)。"""
    c = str(code).strip()
    if c.startswith("30"):
        return "399006.SZ"
    if c.startswith("6") or c.startswith("9"):
        return "000300.SH"
    if c.startswith("0") or c.startswith("2"):
        return "399001.SZ"
    return "000300.SH"


def _bare(code: str) -> str:
    """去掉 .SH/.SZ/.BJ 后缀。"""
    c = str(code).strip()
    return c.split(".")[0] if "." in c else c


def _suffixed(code: str) -> str:
    """
    补齐交易所后缀。

    【关键】库内代码格式不统一：
      signal_log.ts_code      = '000001'    (裸 6 位)
      daily_data.ts_code      = '000001.SZ' (带后缀)
    若不做这层归一，signal_log 的 58 只票在 daily_data 里 100% 匹配不上，
    导致 bars 全空、T+N 无法定位、战法数算成 0。
    """
    c = str(code).strip()
    if "." in c:
        return c
    if c.startswith(("6", "9")):
        return c + ".SH"
    if c.startswith(("0", "2", "3")):
        return c + ".SZ"
    if c.startswith(("4", "8")):
        return c + ".BJ"
    return c + ".SZ"


def _calendar(con) -> List[str]:
    if _exists(con, "dim_trade_calendar"):
        rows = con.execute(
            "SELECT DISTINCT trade_date FROM dim_trade_calendar ORDER BY trade_date"
        ).fetchall()
        if rows:
            return [str(r[0])[:10] for r in rows]
    rows = con.execute("SELECT DISTINCT trade_date FROM daily_data ORDER BY trade_date").fetchall()
    return [str(r[0])[:10] for r in rows]


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

    meta["signals_in_window"] = len(rows)
    if not rows:
        return [], meta

    codes_raw = sorted({str(r[2]) for r in rows})
    codes = sorted({_suffixed(c) for c in codes_raw})
    meta["unique_codes"] = len(codes_raw)

    ph = ",".join(["?"] * len(codes))
    px = con.execute(
        f"""
        SELECT ts_code, trade_date, open, high, low, close, pre_close
        FROM {src}
        WHERE ts_code IN ({ph}) AND trade_date >= ?
        ORDER BY ts_code, trade_date
        """,
        codes + [start_d],
    ).fetchall()

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
        logger.warning("index_daily 读取失败: %s", e)
        return out
    for code, td, pch in rows:
        out.setdefault(str(code), {})[str(td)[:10]] = float(pch)
    return out


def _bench_cum(bm: Dict[str, Dict[str, float]], code: str, i: int, n: int, cal: List[str]) -> Optional[float]:
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
        bar = bars.get(cal[j])
        if not bar:
            continue
        for v in (bar[0], bar[1], bar[2], bar[3]):
            if v is None or float(v) <= 0:
                continue
            f = float(v)
            if peak is None or f > peak:
                peak = f
                lo_after_hi = f
            elif lo_after_hi is None or f < lo_after_hi:
                lo_after_hi = f
    if peak is None or lo_after_hi is None or entry <= 0:
        return None, None
    dd = (lo_after_hi - peak) / peak * 100.0
    mp = (peak - entry) / entry * 100.0
    return dd, mp


# ============================ 战法 ID 解析 ============================

def _ids(raw) -> List[str]:
    out: List[str] = []
    if raw is None:
        return out
    if isinstance(raw, (list, tuple, set)):
        for x in raw:
            s = str(x).strip().strip('"').strip("'")
            if s:
                out.append(s)
        return sorted(set(out))
    s = str(raw)
    if not s.strip():
        return out
    try:
        parsed = json.loads(s)
        if isinstance(parsed, list):
            return sorted({str(x).strip() for x in parsed if str(x).strip()})
    except Exception:
        pass
    if "," in s:
        return sorted({t.strip().strip('"').strip("'") for t in s.split(",") if t.strip()})
    try:
        from core.strategy_id_registry import parse_atomic_strategy_ids
        return parse_atomic_strategy_ids(s)
    except Exception:
        return []


def _labels(raw) -> Dict[str, str]:
    if not raw:
        return {}
    try:
        from core.strategy_id_registry import parse_labeled_atomic_strategies
        return {cid: lbl for cid, lbl in parse_labeled_atomic_strategies(str(raw))}
    except Exception:
        return {}


# ============================ 主统计 ============================

def compute(signals: List[Dict[str, Any]], cal: List[str], bm: Dict[str, Dict[str, float]],
            hold: Tuple[int, ...]) -> Dict[str, Any]:
    by_s: Dict[str, Dict[int, Acc]] = {}
    by_pool: Dict[str, Dict[str, Dict[int, Acc]]] = {}
    by_bk: Dict[float, Dict[str, Dict[int, Acc]]] = {b: {} for b in SCORE_BUCKETS}
    by_uid: Dict[str, Dict[int, Acc]] = {}
    labels: Dict[str, str] = {}
    bench_by_sid: Dict[str, List[float]] = {}
    entry_q: List[float] = []

    for sig in signals:
        ids = _ids(sig["strategy_ids"]) or _ids(sig["strategy"])
        if not ids:
            continue
        labels.update(_labels(sig["strategy"]))
        i = sig["_i"]
        bars = sig["_bars"]
        entry = sig["push_price"]
        code = sig["ts_code"]
        sc = sig["score"]
        pool = sig["pool"]
        bc = _bench_code(code)
        uid = sig["signal_uid"] or "%s|%s|%s" % (sig["trade_date"], code, pool)
        entry_q.append(entry)

        for sid in ids:
            labels.setdefault(sid, "")
            for n in hold:
                j = i + n
                if j >= len(cal):
                    continue
                bar = bars.get(cal[j])
                if not bar or bar[3] is None or float(bar[3]) <= 0:
                    continue
                ret = (float(bar[3]) - entry) / entry * 100.0
                dd, mp = _dd_mp(bars, cal, i, n, entry)
                bp = _bench_cum(bm, bc, i, n, cal)

                by_s.setdefault(sid, {}).setdefault(n, Acc()).add(ret, dd, mp, code)
                by_pool.setdefault(pool, {}).setdefault(sid, {}).setdefault(n, Acc()).add(ret, dd, mp, code)
                for b in SCORE_BUCKETS:
                    if sc is not None and sc >= b:
                        by_bk[b].setdefault(sid, {}).setdefault(n, Acc()).add(ret, dd, mp, code)
                by_uid.setdefault(uid, {}).setdefault(n, Acc()).add(ret, dd, mp, code)
                if bp is not None:
                    bench_by_sid.setdefault(sid, []).append(bp)

    out_s: Dict[str, Any] = {}
    for sid, per in by_s.items():
        node: Dict[str, Any] = {
            "label": labels.get(sid, ""),
            "pools": sorted({p for p in by_pool if sid in by_pool[p]}),
            "T1": None, "T2": None, "T5": None,
        }
        for n in hold:
            acc = per.get(n)
            node["T%d" % n] = acc.out(bench=(sum(bench_by_sid[sid]) / len(bench_by_sid[sid]))
                                      if bench_by_sid.get(sid) else None) if acc and acc.n else None
        out_s[sid] = node

    out_pool: Dict[str, Any] = {}
    for pool, sids in by_pool.items():
        out_pool[pool] = {}
        for sid, per in sids.items():
            out_pool[pool][sid] = {"T%d" % n: (per[n].out() if n in per and per[n].n else None)
                                   for n in hold}

    out_bk: Dict[str, Any] = {}
    for b, sids in by_bk.items():
        out_bk[">=%d" % int(b)] = {}
        for sid, per in sids.items():
            out_bk[">=%d" % int(b)][sid] = {"T%d" % n: (per[n].out() if n in per and per[n].n else None)
                                            for n in hold}

    out_uid: Dict[str, Any] = {}
    for uid, per in by_uid.items():
        out_uid[uid] = {"T%d" % n: (per[n].out() if n in per and per[n].n else None) for n in hold}

    return {
        "by_strategy": out_s, "by_pool": out_pool,
        "by_score_bucket": out_bk, "by_signal": out_uid,
    }


# ============================ 输出 ============================

def _p(v: Optional[float]) -> str:
    """markdown 百分比。"""
    if v is None:
        return "-"
    try:
        return "%+.2f%%" % float(v)
    except (TypeError, ValueError):
        return "-"


def _w(v: Optional[float]) -> str:
    if v is None:
        return "-"
    try:
        return "%.1f%%" % (float(v) * 100.0)
    except (TypeError, ValueError):
        return "-"


def render_md(p: Dict[str, Any], min_n: int) -> str:
    L: List[str] = []
    A = L.append
    A("# 战法有效性一次性快照 · 样本外表现")
    A("")
    A("- 生成时间：%s" % p["generated_at"])
    A("- 窗口：%s ~ %s（%d 个交易日）" % (
        p["window"]["window_start"], p["window"]["window_end"], p["window"]["window_days"]))
    A("- 数据源：`signal_log` × `%s`（**只读**），基准 `index_daily`" % p["window"]["src_table"])
    A("- 口径：入场=push_price；T+N=之后第 N 个交易日收盘；胜率=return>0；Wilson 95% 下界防小样本自欺")
    A("")

    A("## ⚠️ 先读：三个硬事实")
    A("")
    for c in p["caveats"]:
        A("- " + c)
    A("")

    A("## 一、大盘环境（决定怎么读下面的数字）")
    A("")
    ov = p["overview"]
    A("| 指数 | %s ~ %s 累计 |" % (p["window"]["window_start"], p["window"]["window_end"]))
    A("|---|---|")
    for k in ("000001.SH", "000300.SH", "399001.SZ", "399006.SZ"):
        A("| %s | %s |" % (k, _p(ov["bench_cum"].get(k))))
    A("")
    A("- 信号 %d 条；可评估 %d 条" % (ov["n_signals"], ov["n_evaluable"]))
    A("- 档位覆盖：T+1 %d / T+2 %d / T+5 %d" % (
        ov["n_by_hold"].get("1", 0), ov["n_by_hold"].get("2", 0), ov["n_by_hold"].get("5", 0)))
    A("")

    A("## 二、你点名的 6 类策略 → 本系统实际命中")
    A("")
    A("| 你的清单 | 本系统原子 ID | 信号中命中 |")
    A("|---|---|---|")
    for r in p["focus_mapping"]:
        A("| %s | %s | %s |" % (r["ask"], r["id"], r["hits"]))
    A("")

    A("## 三、战法总表（T+5 为主要决策档）")
    A("")
    A("| 战法 | 池 | 标签 | T+1 n | T+1 胜率 | T+1 收益 | T+5 n | T+5 胜率 | T+5 收益 | T+5 回撤 | T+5 超基准 |")
    A("|---|---|---|---|---|---|---|---|---|---|---|")
    ranked = p["ranked"]
    for r in ranked:
        t1, t5 = r.get("T1"), r.get("T5")
        A("| **%s** | %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["strategy"], ",".join(r.get("pools") or []) or "-", (r.get("label") or "-")[:16],
            (t1.get("n") if t1 else "-"), _w(t1.get("win_rate") if t1 else None),
            _p(t1.get("avg_return_pct") if t1 else None),
            (t5.get("n") if t5 else "-"), _w(t5.get("win_rate") if t5 else None),
            _p(t5.get("avg_max_drawdown_pct") if t5 else None),
            _p(t5.get("avg_return_pct") if t5 else None),
            _p(t5.get("excess_pct") if t5 else None),
        ))
    A("")
    A("> 列顺序提醒：`T+5 收益` 在 `T+5 回撤` **左边**一列（平均最大回撤）。回撤为负。")
    A("")

    A("## 四、按分数阈值分桶（验证 push_min_score 设得对不对）")
    A("")
    A("| 战法 | ≥50 (胜率/收益) | ≥60 | ≥70 | ≥80 |")
    A("|---|---|---|---|---|")
    for r in ranked:
        cells = []
        for b in SCORE_BUCKETS:
            node = (r.get("buckets") or {}).get(">=%d" % int(b)) or {}
            t5 = node.get("T5")
            cells.append("%s / %s" % (_w(t5.get("win_rate")) if t5 else "-",
                                      _p(t5.get("avg_return_pct")) if t5 else "-"))
        A("| **%s** | %s | %s | %s | %s |" % (r["strategy"], cells[0], cells[1], cells[2], cells[3]))
    A("")

    A("## 五、正确读法（避免 A 股最常见的三种自欺）")
    A("")
    A("**① 小样本不要看裸胜率。** `n=1` 全胜 → 裸胜率 100%，Wilson 下界 0%。这是噪声。")
    A("")
    A("**② 看超基准而不是绝对收益。** 本窗口大盘在跌，`excess_pct` 是唯一稳的判断依据.")
    A("")
    A("**③ 胜率高不代表赚钱。** 「胜率 70% / 平均亏 0.5%」是低赔率陷阱；"
      "「胜率 40% / 平均赚 2%」才是 A 股趋势跟随的真实形态。")
    A("")

    A("## 六、下一步")
    A("")
    for i, s in enumerate(p["next_steps"], 1):
        A("%d. %s" % (i, s))
    A("")
    return "\n".join(L)


# ============================ main ============================

def main() -> int:
    ap = argparse.ArgumentParser(description="战法有效性一次性快照（只读）")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--min-n", type=int, default=1)
    ap.add_argument("--hold", default="1,2,5")
    ap.add_argument("--no-md", action="store_true")
    args = ap.parse_args()

    hold: Tuple[int, ...] = tuple(int(x) for x in args.hold.split(",") if x.strip())
    t0 = _now_iso()
    logger.info("DB: %s", args.db)

    import duckdb
    con = duckdb.connect(args.db, read_only=True)
    try:
        src = "vw_daily_data_compat" if _exists(con, "vw_daily_data_compat") else "daily_data"
        logger.info("行情表 = %s", src)

        signals, meta = _load(con, args.days, src)
        if not signals:
            logger.error("窗口内无信号，退出")
            return 2
        cal = _calendar(con)
        idx_of = {d: i for i, d in enumerate(cal)}
        for s in signals:
            s["_i"] = idx_of.get(s["trade_date"], -1)
        signals = [s for s in signals if s["_i"] >= 0]

        bm = _bench_map(con, meta["window_start"])
        ev = compute(signals, cal, bm, hold)
        logger.info("战法数 = %d / 信号数 = %d", len(ev["by_strategy"]), len(signals))

        # ---- overview ----
        # 基准：窗口首末日累计涨幅
        bench_cum: Dict[str, Optional[float]] = {}
        for code in ("000001.SH", "000300.SH", "399001.SZ", "399006.SZ"):
            d = bm.get(code)
            if not d:
                bench_cum[code] = None
                continue
            acc = 1.0
            for j in range(len(cal) - meta["window_days"], len(cal)):
                v = d.get(cal[j])
                if v is not None:
                    acc *= (1.0 + v / 100.0)
            bench_cum[code] = round((acc - 1.0) * 100.0, 3)

        n_by_hold: Dict[str, int] = {}
        for n in hold:
            c = 0
            for s in signals:
                if s["_i"] + n < len(cal):
                    c += 1
            n_by_hold[str(n)] = c

        focus = [{"ask": ask, "id": ", ".join(ids) if ids else "（未实现）",
                  "hits": (sum(1 for s in signals
                               if any(i in (_ids(s["strategy_ids"]) or _ids(s["strategy"]))
                                      for i in ids)) if ids else 0)}
                 for ask, ids in ASK_TO_IDS.items()]

        focus_ids = {i for ids in ASK_TO_IDS.values() for i in ids}
        payload: Dict[str, Any] = {
            "schema_version": "1.0",
            "generated_at": t0,
            "mode": "one_shot_readonly",
            "mode_notice": "只读快照；禁止用于自动改权重（与 strategy_weight_optimizer 语义一致）",
            "window": {
                "days": int(args.days), "window_start": meta["window_start"],
                "window_end": meta["window_end"], "window_days": meta["window_days"],
                "src_table": src, "signals_in_window": meta["signals_in_window"],
                "signals_usable": meta["signals_usable"], "unique_codes": meta["unique_codes"],
                "baseline_table": "index_daily",
                "baseline_table_status": "ready" if bm.get("000001.SH") else "missing",
                "baseline_table_note": "index_daily 覆盖 2026-01-链路看日志",
            },
        }

        # ---- caveats ----
        ne = len(signals)
        Q: List[str] = []
        Q.append("**样本量是硬约束**：窗口内 **%d** 条信号（%s ~ %s），"
                 "远低于做决策所需的 200 条。当前所有战法胜率**仅供方向参考**。" % (ne, meta["window_start"], meta["window_end"]))
        Q.append("**`signal_log` 是 2026-10-08 才建的表**，历史信号不在库里。"
                 "`pushed=true` 为 0 条 → 推送链路还没真正跑起来，"
                 "所以本快照只能反映「入库信号」而非「实际推送过的信号」。")
        Q.append("**样本期内大盘在跌**：沪指 %s、沪深300 %s、深证成指 %s、创业板指 %s。"
                 "绝对收益为负是正常现象，**唯一稳的判断是「有没有跑赢大盘」。**" % (
                     _p(bench_cum.get("000001.SH")), _p(bench_cum.get("000300.SH")),
                     _p(bench_cum.get("399001.SZ")), _p(bench_cum.get("399006.SZ"))))
        Q.append("**`strategy_effectiveness` 表已有 69 行但样本极小**："
                 "P5-07（旧05A）n=55 wr=28.6%、P5-17（旧13A）n=40 wr=44.8%、P5-16（旧12B）n=6 wr=0%。"
                 "这些数字来自 10 个交易日，不足以下「战法好/坏」结论。")
        Q.append("**MACD 口径不统一确实存在**（P3 用 `macd_hist<0` 否决、P5 用 `macd>sig` 确认），"
                 "但在本窗口样本下，**影响无法量化**——需要 n≥30 的分组对比才能测出来。")
        Q.append("**push_price 的口径需要复核**：2026-10-21 的记录 `pre_close=0.0`，"
                 "说明当日 `push_price` 可能是盘后抓的收盘价而非真实盘中推送价。"
                 "对于 P5（盘后池）没问题，对 P3（盘中池）要小心。")
        payload["caveats"] = Q

        # ---- ranked ----
        bs = ev["by_strategy"]
        bk = ev["by_score_bucket"]
        rows_out: List[Dict[str, Any]] = []
        for sid, node in bs.items():
            t5 = node.get("T5") or {}
            lo = t5.get("win_rate_wilson_lo")
            item = {
                "strategy": sid, "label": node.get("label", ""),
                "pools": node.get("pools", []),
                "T1": node.get("T1"), "T2": node.get("T2"), "T5": node.get("T5"),
                "buckets": {k: v for k, v in bk.get(">=50", {}).items() if k == sid},
            }
            item["sort_key"] = (t5.get("n") or 0, lo or 0.0)
        # 按 n 降序，同 n 按 Wilson 下界降序
        rows_out = sorted(
            [{"strategy": sid, "label": bs[sid].get("label", ""), "pools": bs[sid].get("pools", []),
              "T1": bs[sid].get("T1"), "T2": bs[sid].get("T2"), "T5": bs[sid].get("T5"),
              "buckets": {k: (bk.get(k, {}).get(sid) or {}) for k in (">=50", ">=60", ">=70", ">=80")}}
             for sid in bs],
            key=lambda r: (-(r["T5"] or {}).get("n", 0), -((r["T5"] or {}).get("win_rate_wilson_lo") or 0.0)),
        )
        payload["ranked"] = rows_out

        payload["by_strategy"] = ev["by_strategy"]
        payload["by_pool"] = ev["by_pool"]
        payload["by_score_bucket"] = ev["by_score_bucket"]
        payload["by_signal"] = ev["by_signal"]
        payload["overview"] = {
            "window": "%s ~ %s" % (meta["window_start"], meta["window_end"]),
            "bench_cum": bench_cum, "n_signals": len(signals),
            "n_evaluable": len(signals), "n_by_hold": n_by_hold,
            "strategy_count": len(bs),
            "excerp": "n_evaluated_per_strategy = 信号总数（原子战法可并存于同一信号）",
        }
        payload["focus_mapping"] = focus
        payload["next_steps"] = [
            "**先修 `signal_log` 的历史缺口**：表只有 10 天数据、`pushed=true` 为 0。"
            "在补够样本前，任何权重调整都是噪声。",
            "**让推送链路真正跑起来**：`pushed` 一直为 0 说明推送层没通，"
            "或者 `signal_log` 只记录了候选而非实际推送。确认 `pool_signal_adapter_v28.append_signal_log_safe` "
            "是否真的在推送成功后才写库。",
            "**复核 `push_price` 口径**：2026-10-21 记录 `pre_close=0.0`，"
            "对 P3 盘中池尤其重要——若 push_price 是盘后价，回测结果全错。",
            "**把 `index_daily` 正式接入 push_min_score 决策链路**："
            "基准数据已就绪（176 交易日），但目前只在 `signal_pnl` 的 V30 列里用它做超额，"
            "扫描时扫内扫外的超基准过滤是 unnecessary。",
            "**MACD 口径统一（#8）可以动手了**：本窗口样本不足以量化影响，"
            "但两套标准混用是确定的缺陷，确定的缺陷不需要等样本。",
            "**箱体补齐到 P3/P4（#2）暂缓**：先看 #3 的归因结果，"
            "看 P5-15/P5-16（旧12A/12B）胜率是否真的优于 P3-05，whether 是否补。",
        ]
    finally:
        con.close()

    # ---- 原子写 ----
    os.makedirs(os.path.dirname(OUT_JSON), exist_ok=True)
    _atomic_write(OUT_JSON, json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    _atomic_write(OUT_MD, render_md(payload, args.min_n))
    logger.info("已写入:\n  %s\n  %s", OUT_JSON, OUT_MD)

    # ---- 控制台摘要 ----
    print("\n" + "=" * 96)
    print("战法有效性快照  窗口 %s ~ %s  信号 %d 条  战法 %d 个" % (
        meta["window_start"], meta["window_end"], len(signals), len(ev["by_strategy"])))
    print("=" * 96)
    bench_line = "  ".join("%s %s" % (k.split(".")[0], _p(v)) for k, v in bench_cum.items())
    print("大盘: %s" % bench_line)
    print("-" * 96)
    hdr = "%-9s %-14s %-5s %-7s %-9s %-9s %-9s %-9s" % (
        "战法", "标签", "T1_n", "T1_wr", "T1_ret", "T5_n", "T5_wr", "T5_ret")
    print(hdr)
    print("-" * 96)
    for r in rows_out:
        t1, t5 = r["T1"], r["T5"]
        print("%-9s %-14s %-5s %-9s %-9s %-9s %-9s %-9s" % (
            r["strategy"],
            (r.get("label") or "")[:14] or "-",
            (t1.get("n") if t1 else "-"),
            _w(t1.get("win_rate") if t1 else None),
            _p(t1.get("avg_return_pct") if t1 else None),
            (t5.get("n") if t5 else "-"),
            _w(t5.get("win_rate") if t5 else None),
            _p(t5.get("avg_return_pct") if t5 else None),
        ))
    print("-" * 96)
    print("提示: n<30 时裸胜率不可信，请看 JSON 里的 win_rate_wilson_lo。")
    return 0


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


if __name__ == "__main__":
    sys.exit(main())