# -*- coding: utf-8 -*-
"""tools/strategy_correlation_matrix.py — 战法相关性矩阵（只读分析）

问题：31 条战法（P2-xx ~ P5-xx）中可能存在大量「同一条策略换参数」，
      你以为的「多战法共振」其实是同一信号重复计票。
做法：用 signal_strategy_ledger 把每条战法按「日收益序列」展开
      （同日多条信号取均值），再算两两 Pearson 相关。
输出：
  · rho >= 阈值的「高相关对」清单（默认 0.9）
  · 连通簇（并查集）：每簇给出成员、各自信号数，
    「只留 1 条」与「全留」的信号量差 = 重复计票规模
  · --json 输出机器可读结果

口径说明（与 Tier2.5 净口径一致）：
  net_return_pct（扣费后净收益）优先，缺失回退 return_pct（毛收益）。
  n_days 默认 5（当前样本最多的持有档）；可用 --n-days 指定。

红线：纯只读；不改任何战法、不写库。
用法：
    python tools/strategy_correlation_matrix.py
    python tools/strategy_correlation_matrix.py --n-days 2 --min-overlap 8
    python tools/strategy_correlation_matrix.py --db _snapshots/xxx.duckdb --json out.json
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


def _load_series(con, n_days: int):
    """返回 {(strategy, date): mean_net_return_pct} 与 {(strategy): n_signals}。"""
    rows = con.execute(
        """
        SELECT l.strategy_id,
               l.log_date,
               AVG(COALESCE(p.net_return_pct, p.return_pct)) AS r
        FROM signal_strategy_ledger l
        JOIN signal_pnl p ON p.log_id = l.log_id AND p.n_days = ?
        WHERE l.log_date IS NOT NULL
          AND COALESCE(p.net_return_pct, p.return_pct) IS NOT NULL
        GROUP BY 1, 2
        """,
        [int(n_days)],
    ).fetchall()
    series = {}
    for sid, d, r in rows:
        if sid and d is not None and r is not None:
            series[(str(sid), str(d))] = float(r)
    cnt = con.execute(
        """
        SELECT l.strategy_id, COUNT(DISTINCT l.log_id)
        FROM signal_strategy_ledger l
        JOIN signal_pnl p ON p.log_id = l.log_id AND p.n_days = ?
        GROUP BY 1
        """,
        [int(n_days)],
    ).fetchall()
    counts = {str(a): int(b) for a, b in cnt}
    return series, counts


def _pearson(xs, ys) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 1e-12 or syy <= 1e-12:
        return 0.0
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-days", type=int, default=5)
    ap.add_argument("--min-overlap", type=int, default=8,
                    help="两条战法至少有多少个共同交易日才计算相关（默认 8）")
    ap.add_argument("--rho", type=float, default=0.9, help="高相关阈值（默认 0.9）")
    ap.add_argument("--db", default="", help="指定只读库（默认生产库）")
    ap.add_argument("--json", default="", help="把结果写入 JSON 文件")
    args = ap.parse_args()

    from tools._ro_conn import open_ro

    con = open_ro(args.db)
    try:
        series, counts = _load_series(con, args.n_days)
    finally:
        con.close()

    if not series:
        print(f"[SKIP] signal_pnl 中没有 n_days={args.n_days} 的可用样本。")
        print("       先用 --n-days 1 / 2 / 5 / 10 逐档试。")
        return 0

    strategies = sorted({k[0] for k in series})
    by_strategy: dict = {}
    for (sid, d), r in series.items():
        by_strategy.setdefault(sid, {})[d] = r

    pairs = []
    for i in range(len(strategies)):
        for j in range(i + 1, len(strategies)):
            a, b = strategies[i], strategies[j]
            ma, mb = by_strategy[a], by_strategy[b]
            common = sorted(set(ma) & set(mb))
            if len(common) < args.min_overlap:
                continue
            rho = _pearson([ma[d] for d in common], [mb[d] for d in common])
            pairs.append((a, b, rho, len(common)))

    pairs.sort(key=lambda x: -x[2])

    print("=" * 78)
    print(f"战法相关性矩阵（n_days={args.n_days} | 最少共同交易日={args.min_overlap}"
          f" | 战法数={len(strategies)}）")
    print("=" * 78)
    print("\n—— 高相关对（rho >= {:.2f}）——".format(args.rho))
    high = [p for p in pairs if p[2] >= args.rho]
    if not high:
        print("  （无）—— 说明现有样本下没有明显「同一策略换参数」的重复计票。")
    for a, b, rho, n in high:
        print(f"  rho={rho:+.3f}  overlap={n:>2}日  {a} ↔ {b}")

    # 并查集聚簇
    parent = {s: s for s in strategies}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    for a, b, rho, _n in high:
        union(a, b)

    clusters: dict = {}
    for s in strategies:
        clusters.setdefault(find(s), []).append(s)
    multi = {k: v for k, v in clusters.items() if len(v) >= 2}

    print("\n—— 高相关簇（rho >= {:.2f} 连通）——".format(args.rho))
    if not multi:
        print("  （无）")
    total_keep1 = total_keepN = 0
    cluster_out = []
    for members in multi.values():
        members = sorted(members)
        sig_cnt = {m: counts.get(m, 0) for m in members}
        keep_n = sum(sig_cnt.values())
        keep_1 = max(sig_cnt.values()) if sig_cnt else 0
        total_keep1 += keep_1
        total_keepN += keep_n
        cluster_out.append({"members": members, "signals": sig_cnt,
                            "keep_1": keep_1, "keep_all": keep_n})
        print("  簇: " + " | ".join(f"{m}({sig_cnt[m]}条)" for m in members))
        print(f"      全留={keep_n} 条信号 → 只留信号最多的 1 条={keep_1} 条，"
              f"重复计票≈{keep_n - keep_1} 条")
    if multi:
        print(f"\n  合计：{total_keepN} 条信号中约 {total_keepN - total_keep1} 条为簇内重复计票"
              f"（{((total_keepN - total_keep1) / max(total_keepN, 1) * 100):.0f}%）")

    print("\n—— Top 15 相关对（含负相关）——")
    for a, b, rho, n in pairs[:15]:
        print(f"  rho={rho:+.3f}  overlap={n:>2}日  {a} ↔ {b}")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({
                "n_days": args.n_days,
                "min_overlap": args.min_overlap,
                "rho_threshold": args.rho,
                "pair_count": len(pairs),
                "high_pairs": [{"a": a, "b": b, "rho": round(rho, 4), "overlap": n}
                               for a, b, rho, n in high],
                "clusters": cluster_out,
                "top_pairs": [{"a": a, "b": b, "rho": round(rho, 4), "overlap": n}
                              for a, b, rho, n in pairs[:30]],
            }, f, ensure_ascii=False, indent=2)
        print(f"\n[JSON] 已写入 {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
