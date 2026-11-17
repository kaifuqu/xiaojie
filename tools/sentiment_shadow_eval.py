# -*- coding: utf-8 -*-
"""tools/sentiment_shadow_eval.py — H1 情绪温度计 Shadow 评估器(只读)

背景：`core/sentiment_shadow.py` 每次扫描结束后把「若接线」的情绪状态/系数
追加到 `data/runtime/observability/sentiment_shadow.jsonl`（只记录不拦截）。
按 Shadow 纪律，攒满 ≥20 个交易日后必须回答一个问题：

    「情绪系数指向的方向」和「后续信号的真实质量」到底吻合不吻合？

本工具把两边数据接起来（全程只读）：
  · 情绪侧：读 shadow JSONL（同一业务日多条记录取最后一条；自动读 .1 轮转档）
  · 信号侧：signal_log ⋈ signal_pnl（T+1, n_days=1）按业务日 × 池聚合：
        信号数 n / 胜率 win / 平均收益 return_pct /
        平均超额 COALESCE(excess_blend_pct, excess_pct)
  · 吻合度（多口径互相印证，避免单一口径过拟合）：
      1) 逐日配对 Pearson r（情绪分数 vs 当日信号质量）
      2) 看多日（平均系数 > 1）vs 看空日（< 1）的信号质量差
      3) 命中率：看多日质量高于全样本中位数 / 看空日低于中位数的占比
      4) 分状态表：每种情绪状态下当日信号 T+1 质量的均值
  · 样本 < min_days（默认 20 个交易日）时明确标注「仅预览，不作为接线依据」。

红线：DuckDB 一律 read_only；JSONL 只读；不写任何文件、不动任何阈值。
      本工具输出只是「证据采集」，接不接线仍由人拍板。

用法：
    python tools/sentiment_shadow_eval.py                       # 生产库 + 默认 JSONL
    python tools/sentiment_shadow_eval.py --db <备份库路径>      # 备份库离线评估
    python tools/sentiment_shadow_eval.py --min-days 5          # 放宽预览门槛
    python tools/sentiment_shadow_eval.py --json                # JSON 输出（给 UI/管道）
"""
from __future__ import annotations

import argparse
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

SHADOW_JSONL = os.path.join(
    ROOT, "data", "runtime", "observability", "sentiment_shadow.jsonl")

_EVAL_POOLS = ("p2", "p3", "p4", "p5")   # p1 系数常年接近 1，评估意义弱


# ----------------------------------------------------------------------------
# 情绪侧：读 JSONL（去重 + 排序）
# ----------------------------------------------------------------------------
def _load_shadow_records(shadow_file: str = "") -> tuple:
    """返回 (records, info)。records 按 business_date 升序，同日取最后一条。"""
    path = shadow_file or SHADOW_JSONL
    files = [path + ".1", path]          # 轮转档在前（时间更早），后写入覆盖
    info = {"files": [f for f in files if os.path.exists(f)],
            "n_raw": 0, "n_bad": 0}
    by_date = {}
    for f in files:
        if not os.path.exists(f):
            continue
        try:
            with open(f, "r", encoding="utf-8", errors="replace") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    info["n_raw"] += 1
                    try:
                        rec = json.loads(line)
                    except Exception:
                        info["n_bad"] += 1
                        continue
                    d = str(rec.get("business_date") or "")
                    if not d:
                        info["n_bad"] += 1
                        continue
                    # 同一业务日多条（一天多次扫描）→ 保留 ts 最新的一条
                    prev = by_date.get(d)
                    if prev is None or str(rec.get("ts") or "") >= str(prev.get("ts") or ""):
                        by_date[d] = rec
        except Exception:
            continue
    records = [by_date[d] for d in sorted(by_date)]
    return records, info


# ----------------------------------------------------------------------------
# 信号侧：signal_log ⋈ signal_pnl（T+1）按日 × 池聚合
# ----------------------------------------------------------------------------
def _signal_quality_by_date(con, dates) -> dict:
    """{date: {"ALL": {...}, "p2": {...}, ...}}；date 为 log_date 的字符串形式。"""
    out = {}
    if not dates:
        return out
    ph = ",".join(["?"] * len(dates))
    base_sql = f"""
        SELECT CAST(l.log_date AS VARCHAR) AS d, l.pool,
               COUNT(*)                                            AS n,
               AVG(CASE WHEN p.win THEN 1.0 ELSE 0.0 END)          AS win_rate,
               AVG(p.return_pct)                                   AS avg_ret,
               AVG(COALESCE(p.excess_blend_pct, p.excess_pct))     AS avg_excess
        FROM signal_log l
        JOIN signal_pnl p ON p.log_id = l.id
        WHERE p.n_days = 1 AND CAST(l.log_date AS VARCHAR) IN ({ph})
        GROUP BY 1, 2
    """
    try:
        for d, pool, n, win_rate, avg_ret, avg_excess in con.execute(
                base_sql, list(dates)).fetchall():
            out.setdefault(str(d), {})[str(pool)] = {
                "n": int(n or 0),
                "win_rate": None if win_rate is None else round(float(win_rate), 4),
                "avg_ret": None if avg_ret is None else round(float(avg_ret), 3),
                "avg_excess": None if avg_excess is None else round(float(avg_excess), 3),
            }
    except Exception:
        pass
    # 全池 rollup（由池明细合成，避免重复扫库）
    for d, pools in out.items():
        ns = sum(v["n"] for v in pools.values())
        if ns <= 0:
            continue
        wr = [v for v in pools.values() if v.get("win_rate") is not None]
        rt = [v for v in pools.values() if v.get("avg_ret") is not None]
        ex = [v for v in pools.values() if v.get("avg_excess") is not None]
        pools["ALL"] = {
            "n": ns,
            "win_rate": (round(sum(v["win_rate"] * v["n"] for v in wr) / sum(v["n"] for v in wr), 4)
                         if wr else None),
            "avg_ret": (round(sum(v["avg_ret"] * v["n"] for v in rt) / sum(v["n"] for v in rt), 3)
                        if rt else None),
            "avg_excess": (round(sum(v["avg_excess"] * v["n"] for v in ex) / sum(v["n"] for v in ex), 3)
                           if ex else None),
        }
    return out


# ----------------------------------------------------------------------------
# 统计小工具
# ----------------------------------------------------------------------------
def _pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return round(sxy / (sxx * syy) ** 0.5, 3)


def _median(vals):
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _mean(vals):
    return round(sum(vals) / len(vals), 3) if vals else None


def _day_quality(q_all: dict):
    """单日信号质量标量：优先平均超额，缺失回退平均收益，再缺失回退胜率偏差。"""
    if not q_all:
        return None
    if q_all.get("avg_excess") is not None:
        return float(q_all["avg_excess"])
    if q_all.get("avg_ret") is not None:
        return float(q_all["avg_ret"])
    if q_all.get("win_rate") is not None:
        return (float(q_all["win_rate"]) - 0.5) * 10.0
    return None


def _mult_avg(rec: dict) -> float:
    mult = rec.get("multiplier") or {}
    vals = [float(mult.get(p, 1.0) or 1.0) for p in _EVAL_POOLS]
    return sum(vals) / len(vals) if vals else 1.0


# ----------------------------------------------------------------------------
# 主快照（CLI 与 UI 共用同一口径）
# ----------------------------------------------------------------------------
def shadow_quality_snapshot(min_days: int = 20, shadow_file: str = "",
                            db_path: str = "", retries: int = 2,
                            delay: float = 1.0) -> dict:
    """返回评估快照 dict（异常降级，绝不抛错）。"""
    out = {
        "ok": False, "error": "", "db_error": "", "ready": False,
        "min_days": int(min_days),
        "n_days": 0, "n_matched_days": 0,
        "shadow_info": {}, "records": [], "quality_by_date": {},
        "state_stats": [], "consistency": {},
    }
    try:
        records, info = _load_shadow_records(shadow_file)
        out["shadow_info"] = {
            "files": [os.path.basename(f) for f in info.get("files", [])],
            "n_raw": int(info.get("n_raw", 0)),
            "n_bad": int(info.get("n_bad", 0)),
            "n_days": len(records),
        }
        out["n_days"] = len(records)
        out["ready"] = len(records) >= int(min_days)
        if not records:
            out["error"] = ("sentiment_shadow.jsonl 尚无记录 —— daemon 接线 H1 后还没跑过扫描，"
                            "或文件在 data/runtime/observability/ 下尚未生成")
            return out

        dates = [str(r.get("business_date")) for r in records]
        # 库被 daemon 独占时优雅降级：情绪侧照常统计，信号质量侧标注缺失
        con = None
        quality = {}
        try:
            from tools._ro_conn import open_ro

            con = open_ro(db_path, retries=retries, delay=delay)
        except Exception as e:
            out["db_error"] = (f"{type(e).__name__}: {e} —— 情绪侧记录仍可查看，"
                               "信号质量配对暂缺（daemon 停机后可完整评估）")
        if con is not None:
            try:
                quality = _signal_quality_by_date(con, dates)
            finally:
                try:
                    con.close()
                except Exception:
                    pass
        out["quality_by_date"] = quality

        # ---- 逐日配对（情绪分数 / 平均系数 × 当日信号质量）----
        pairs = []
        for rec in records:
            d = str(rec.get("business_date"))
            q_all = (quality.get(d) or {}).get("ALL")
            qv = _day_quality(q_all)
            if qv is None:
                continue
            pairs.append({
                "date": d,
                "state": str(rec.get("state") or "未知"),
                "score": int(rec.get("score") or 0),
                "mult_avg": round(_mult_avg(rec), 4),
                "n_sig": int((q_all or {}).get("n") or 0),
                "win_rate": (q_all or {}).get("win_rate"),
                "avg_ret": (q_all or {}).get("avg_ret"),
                "avg_excess": (q_all or {}).get("avg_excess"),
                "quality": round(qv, 3),
            })
        out["n_matched_days"] = len(pairs)

        # ---- 分状态统计 ----
        by_state = {}
        for rec in records:
            st = str(rec.get("state") or "未知")
            d = str(rec.get("business_date"))
            q_all = (quality.get(d) or {}).get("ALL")
            item = by_state.setdefault(st, {
                "state": st, "days": 0, "scores": [], "mult_left": 0.0,
                "n_sig": 0, "win_rates": [], "avg_rets": [], "avg_excesses": [],
            })
            item["days"] += 1
            item["scores"].append(int(rec.get("score") or 0))
            item["mult_left"] += float((rec.get("multiplier") or {}).get("p2", 1.0) or 1.0)
            if q_all:
                item["n_sig"] += int(q_all.get("n") or 0)
                if q_all.get("win_rate") is not None:
                    item["win_rates"].append(float(q_all["win_rate"]))
                if q_all.get("avg_ret") is not None:
                    item["avg_rets"].append(float(q_all["avg_ret"]))
                if q_all.get("avg_excess") is not None:
                    item["avg_excesses"].append(float(q_all["avg_excess"]))
        state_stats = []
        for st, item in by_state.items():
            state_stats.append({
                "state": st,
                "days": item["days"],
                "avg_score": _mean(item["scores"]),
                "mult_p2": round(item["mult_left"] / item["days"], 3) if item["days"] else None,
                "n_sig": item["n_sig"],
                "win_rate": _mean([round(v, 4) for v in item["win_rates"]]) if item["win_rates"] else None,
                "avg_ret": _mean(item["avg_rets"]),
                "avg_excess": _mean(item["avg_excesses"]),
            })
        state_stats.sort(key=lambda x: -(x["avg_score"] or 0))
        out["state_stats"] = state_stats

        # ---- 吻合度三口径 ----
        cons = {"n_pairs": len(pairs)}
        if pairs:
            scores = [p["score"] for p in pairs]
            quals = [p["quality"] for p in pairs]
            cons["pearson_score_vs_quality"] = _pearson(scores, quals)

            bull = [p for p in pairs if p["mult_avg"] > 1.005]
            bear = [p for p in pairs if p["mult_avg"] < 0.995]
            bull_q = _mean([p["quality"] for p in bull])
            bear_q = _mean([p["quality"] for p in bear])
            cons["n_bull_days"] = len(bull)
            cons["n_bear_days"] = len(bear)
            cons["bull_quality"] = bull_q
            cons["bear_quality"] = bear_q
            cons["direction_ok"] = (
                None if (bull_q is None or bear_q is None)
                else bool(bull_q > bear_q))

            med = _median(quals)
            hits = checks = 0
            for p in pairs:
                if p["mult_avg"] > 1.005:
                    checks += 1
                    hits += 1 if (med is not None and p["quality"] >= med) else 0
                elif p["mult_avg"] < 0.995:
                    checks += 1
                    hits += 1 if (med is not None and p["quality"] <= med) else 0
            cons["hit_rate"] = (round(hits / checks, 3) if checks else None)
            cons["n_dir_days"] = checks

            # 分池口径：各池系数 vs 该池当日信号质量
            pool_checks = {}
            for pool in _EVAL_POOLS:
                bq, eq_ = [], []
                for rec in records:
                    d = str(rec.get("business_date"))
                    qp = (quality.get(d) or {}).get(pool)
                    qv = _day_quality(qp)
                    if qv is None:
                        continue
                    m = float((rec.get("multiplier") or {}).get(pool, 1.0) or 1.0)
                    if m > 1.005:
                        bq.append(qv)
                    elif m < 0.995:
                        eq_.append(qv)
                pool_checks[pool] = {
                    "bull_n": len(bq), "bull_quality": _mean(bq),
                    "bear_n": len(eq_), "bear_quality": _mean(eq_),
                }
            cons["per_pool"] = pool_checks
        out["consistency"] = cons
        out["records"] = [
            {
                "date": str(r.get("business_date")),
                "ts": str(r.get("ts") or ""),
                "state": str(r.get("state") or "未知"),
                "score": int(r.get("score") or 0),
                "multiplier": r.get("multiplier") or {},
                "kept_n": r.get("kept_n") or {},
            }
            for r in records
        ]
        out["ok"] = True
        return out
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        return out


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def _fmt(v, nd=2, pct=False):
    if v is None:
        return "—"
    if pct:
        return f"{v * 100:.1f}%"
    return f"{v:.{nd}f}"


def main() -> int:
    ap = argparse.ArgumentParser(description="H1 情绪温度计 Shadow 评估器（只读）")
    ap.add_argument("--db", default="", help="指定数据库（备份/QA 库离线评估）")
    ap.add_argument("--shadow-file", default="", help="指定 shadow JSONL 路径")
    ap.add_argument("--min-days", type=int, default=20,
                    help="接线评估的最小交易日样本（默认 20）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 快照")
    args = ap.parse_args()

    snap = shadow_quality_snapshot(
        min_days=args.min_days, shadow_file=args.shadow_file, db_path=args.db)
    if args.json:
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0 if snap["ok"] else 1
    if not snap["ok"]:
        print(f"[FAIL] {snap['error']}")
        return 1

    si = snap["shadow_info"]
    print("=" * 78)
    print(f"H1 情绪温度计 Shadow 评估（记录 {snap['n_days']} 个交易日 | "
          f"可配对 T+1 信号落地 {snap['n_matched_days']} 天）")
    print("=" * 78)
    if not snap["ready"]:
        print(f"⚠ 样本不足：{snap['n_days']} / {snap['min_days']} 个交易日 —— "
              "以下仅为预览，不作为接线依据。")
    else:
        print(f"✔ 样本已达评估门槛（≥{snap['min_days']} 个交易日），可结合三口径做接线判断。")
    print(f"  JSONL 文件：{', '.join(si.get('files') or []) or '（无）'}"
          f" | 原始行 {si.get('n_raw', 0)} | 坏行 {si.get('n_bad', 0)}")
    if snap.get("db_error"):
        print(f"⚠ 读库受限：{snap['db_error']}")
    print()

    # 分状态表
    print(f"{'情绪状态':<10} {'天数':>4} {'均分':>5} {'P2系数':>7} "
          f"{'信号':>4} {'胜率':>7} {'均收益':>8} {'均超额':>8}")
    for s in snap["state_stats"]:
        print(f"{s['state']:<10} {s['days']:>4} {_fmt(s['avg_score'], 1):>5} "
              f"{_fmt(s['mult_p2'], 3):>7} {s['n_sig']:>4} "
              f"{_fmt(s['win_rate'], pct=True):>7} "
              f"{_fmt(s['avg_ret'], 2):>8} {_fmt(s['avg_excess'], 2):>8}")
    print()

    cons = snap["consistency"]
    if cons.get("n_pairs"):
        print("—— 吻合度（情绪系数方向 vs 后续 T+1 信号质量）——")
        r = cons.get("pearson_score_vs_quality")
        print(f"  · 逐日 Pearson r（分数×质量）：{_fmt(r, 3)}"
              f"（配对 {cons['n_pairs']} 天；|r|<0.3 视为无明显方向）")
        print(f"  · 看多日({cons.get('n_bull_days')}天) 均质量 {_fmt(cons.get('bull_quality'), 3)}"
              f" vs 看空日({cons.get('n_bear_days')}天) 均质量 {_fmt(cons.get('bear_quality'), 3)}"
              f" → 方向{'吻合' if cons.get('direction_ok') else ('不吻合' if cons.get('direction_ok') is False else '样本不足')}")
        print(f"  · 命中率（看多日质量≥中位 / 看空日≤中位）：{_fmt(cons.get('hit_rate'), 3)}"
              f"（有效判定 {cons.get('n_dir_days', 0)} 天；0.5=抛硬币）")
        per_pool = cons.get("per_pool") or {}
        line = []
        for pool in _EVAL_POOLS:
            pp = per_pool.get(pool) or {}
            if pp.get("bull_quality") is not None or pp.get("bear_quality") is not None:
                line.append(f"{pool}: 多{_fmt(pp.get('bull_quality'), 2)}"
                            f"({pp.get('bull_n')}天)/空{_fmt(pp.get('bear_quality'), 2)}"
                            f"({pp.get('bear_n')}天)")
        if line:
            print("  · 分池：" + "；".join(line))
    else:
        print("—— 吻合度：暂无可配对的 T+1 信号（signal_pnl 尚未回填这些日期）——")

    print()
    print("说明：本工具只读（DuckDB read_only + JSONL 只读）；接线与否属策略决策，")
    print("      建议样本 ≥ 20 个交易日且三口径方向一致后再人工评审。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
