# -*- coding: utf-8 -*-
"""tools/bootstrap_return_dist.py — bootstrap 收益分布（只读分析）

问题：只看「胜率 28.6% / 均值 -5.6%」信息量太小 ——
      看不出右尾肥不肥、均值置信区间宽不宽。
做法（day-trader-skill Time Machine 的简化版）：
  · 每条战法取 signal_pnl 的样本（净收益优先，缺失回退毛收益）
  · 10,000 次有放回重采样（默认 seed=42，可复现）
  · 输出：原始样本分位（5/25/50/75/95）+ P(收益<=0)
          + 均值的 bootstrap 95% 置信区间（2.5% / 97.5% 分位）
  · 同时输出池级汇总

统计警戒：n < 30 的战法标 ⚠ 样本不足，不参与"排名"结论。

用法：
    python tools/bootstrap_return_dist.py
    python tools/bootstrap_return_dist.py --n-days 5 --iters 10000
    python tools/bootstrap_return_dist.py --db _snapshots/xxx.duckdb --json bs.json

红线：纯只读；不写库、不改策略。
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _pct(xs, q: float) -> float:
    """线性插值分位数（q ∈ [0,1]），xs 需已排序。"""
    if not xs:
        return 0.0
    if len(xs) == 1:
        return xs[0]
    pos = q * (len(xs) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(xs) - 1)
    frac = pos - lo
    return xs[lo] * (1 - frac) + xs[hi] * frac


def _bootstrap(samples, iters: int, seed: int):
    rng = random.Random(seed)
    n = len(samples)
    means = []
    for _ in range(iters):
        s = 0.0
        for _ in range(n):
            s += samples[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    return means


def _fmt_block(name: str, samples, iters: int, seed: int):
    samples = sorted(samples)
    n = len(samples)
    mean = sum(samples) / n
    p5, p25, p50, p75, p95 = (_pct(samples, q) for q in (0.05, 0.25, 0.5, 0.75, 0.95))
    le0 = sum(1 for x in samples if x <= 0) / n * 100.0
    means = _bootstrap(samples, iters, seed)
    ci_lo, ci_hi = _pct(means, 0.025), _pct(means, 0.975)
    flag = "⚠" if n < 30 else " "
    print(f"  {flag} {name:<12} n={n:>3} | 均值 {mean:+6.2f}% | "
          f"P5 {p5:+6.2f} P50 {p50:+6.2f} P95 {p95:+6.2f} | "
          f"P(≤0)={le0:4.0f}% | 均值95%CI [{ci_lo:+5.2f}, {ci_hi:+5.2f}]")
    return {
        "n": n, "mean": round(mean, 3), "p5": round(p5, 3), "p25": round(p25, 3),
        "p50": round(p50, 3), "p75": round(p75, 3), "p95": round(p95, 3),
        "prob_le0_pct": round(le0, 1),
        "mean_ci95": [round(ci_lo, 3), round(ci_hi, 3)],
        "insufficient_sample": n < 30,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="")
    ap.add_argument("--n-days", type=int, default=5)
    ap.add_argument("--iters", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    from tools._ro_conn import open_ro

    con = open_ro(args.db)
    try:
        rows = con.execute(
            """
            SELECT l.strategy_id, lower(COALESCE(l.pool,'')) AS pool,
                   COALESCE(p.net_return_pct, p.return_pct) AS r
            FROM signal_strategy_ledger l
            JOIN signal_pnl p ON p.log_id = l.log_id AND p.n_days = ?
            WHERE COALESCE(p.net_return_pct, p.return_pct) IS NOT NULL
              AND l.strategy_id IS NOT NULL
            """,
            [int(args.n_days)],
        ).fetchall()
        pool_rows = con.execute(
            """
            SELECT lower(COALESCE(s.pool,'')) AS pool,
                   COALESCE(p.net_return_pct, p.return_pct) AS r
            FROM signal_log s
            JOIN signal_pnl p ON p.log_id = s.id AND p.n_days = ?
            WHERE COALESCE(p.net_return_pct, p.return_pct) IS NOT NULL
            """,
            [int(args.n_days)],
        ).fetchall()
    finally:
        con.close()

    if not rows:
        print(f"[SKIP] n_days={args.n_days} 无可用样本，可试 --n-days 1/2/5/10。")
        return 0

    by_strategy: dict = {}
    for sid, _pool, r in rows:
        by_strategy.setdefault(str(sid), []).append(float(r))
    by_pool: dict = {}
    for pool, r in pool_rows:
        by_pool.setdefault(str(pool) or "(空)", []).append(float(r))

    print("=" * 100)
    print(f"bootstrap 收益分布（n_days={args.n_days}，{args.iters} 次重采样，seed={args.seed}）")
    print("  口径：净收益优先，缺失回退毛收益；⚠ = 样本 n<30，仅参考不做排名")
    print("=" * 100)

    out = {"n_days": args.n_days, "iters": args.iters, "seed": args.seed,
           "by_strategy": {}, "by_pool": {}}

    print("\n—— 战法级（按时长 n 降序）——")
    for sid in sorted(by_strategy, key=lambda s: -len(by_strategy[s])):
        out["by_strategy"][sid] = _fmt_block(sid, by_strategy[sid], args.iters, args.seed)

    print("\n—— 池级汇总 ——")
    for pool in sorted(by_pool):
        out["by_pool"][pool] = _fmt_block(f"pool {pool}", by_pool[pool], args.iters, args.seed)

    print("\n读法提示：")
    print("  · 均值95%CI 跨过 0 → 现有样本无法证明该战法均值不为零（与 Wilson CI 结论一致时最可信）")
    print("  · P5 深度明显负、P95 不肥 → 右尾补偿不了左尾，属于结构性负向")
    print("  · 样本 n<30 一律只能当占位符，不要据此删/调战法")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n[JSON] 已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
