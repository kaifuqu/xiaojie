# -*- coding: utf-8 -*-
"""
小杰AI选股系统 Pro V29 — 战法权重推荐 CLI 查看入口

用法：
    python -m core.cli_view_recommendation           # 完整列出
    python -m core.cli_view_recommendation --asc     # 按推荐权重升序
    python -m core.cli_view_recommendation --json    # 输出 JSON

【V29.1 关键修改】旧版只读 stats[*].t5_win_rate，而 T+5 未成熟时该字段为
NULL，表格显示 0.0%、汇总全走「样本不足」分支，等于看不到任何真实战法表现。
现改为：
  1) 胜率优先取 stats[*].win_rate（最新已成熟 N 档口径），旧报表自动回退；
  2) 表头显示每个战法当前实际采用的 N 档（如 T+2）；
  3) 新增「数据成熟度」区块：已成熟/缺失 N 档、已评估样本、距自动应用缺口；
  4) 表格补充平均收益、平均最大回撤、平均最大浮盈。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

RECOMMENDATION_FILE = "data/strategy_weight_recommendation.json"


def _load() -> dict:
    if not os.path.exists(RECOMMENDATION_FILE):
        print("[INFO] 尚未生成推荐报表（%s）。" % RECOMMENDATION_FILE)
        print("       推荐报表由 core.strategy_weight_optimizer 生成，")
        print("       在 daemon 18:30 的 T+N 回填链路里自动跑（仅统计，不改实盘权重）。")
        sys.exit(0)
    try:
        with open(RECOMMENDATION_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception as e:
        print("[ERROR] 读取失败: %s" % e)
        sys.exit(1)


def _pick_win_rate(info: dict):
    """兼容旧报表：优先 win_rate，缺失时按 N 档从大到小回退。"""
    for key in ("win_rate", "t5_win_rate", "t10_win_rate", "t2_win_rate", "t1_win_rate"):
        v = info.get(key)
        if v is None:
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def _fmt_pct(v) -> str:
    if v is None:
        return "-"
    try:
        return "%+.2f%%" % float(v)
    except (TypeError, ValueError):
        return "-"


def _print_maturity(data: dict) -> None:
    """打印数据成熟度区块（V29.1 新增）。"""
    m = data.get("maturity") or {}
    if not m:
        return
    print("")
    print("=== 数据成熟度 ===")
    print("  已成熟 N 档      : %s" % (m.get("available_n_days"),))
    print("  当前主口径       : T+%s" % (m.get("primary_n_days"),))
    print("  尚未成熟 N 档    : %s" % (m.get("missing_n_days"),))
    print("  已评估有效样本   : %s" % (m.get("total_evaluated"),))
    print("  信号总数         : %s（已定价 %s / 待定价 %s）"
          % (m.get("signals_total"), m.get("signals_ready"), m.get("signals_pending")))
    print("  自动应用门槛     : n>=%s，当前还差 %s 条（auto_apply=%s）"
          % (m.get("min_n_for_auto_apply"), m.get("next_auto_apply_remaining"),
             m.get("auto_apply_enabled")))


def _print_table(items: list, asc: bool = False) -> None:
    if not items:
        print("（无推荐条目）")
        return
    if asc:
        items = sorted(items, key=lambda x: x[7], reverse=False)
    else:
        items = sorted(items, key=lambda x: (-x[7], x[0]))

    print("")
    print("%-14s%-8s%8s%9s%10s%10s%10s%9s  %s"
          % ("战法", "档位", "样本", "胜率", "均收益", "均回撤", "均浮盈", "权重", "理由"))
    print("-" * 100)
    for name, primary, n_sig, win_rate, avg_ret, avg_dd, avg_mp, weight, reason in items:
        nd_str = ("T+%s" % primary) if primary else "-"
        wr_str = ("%.1f%%" % (win_rate * 100.0)) if win_rate is not None else "-"
        flag = ""
        if weight > 1.05:
            flag = "  [升权候选]"
        elif weight < 0.95:
            flag = "  [降权候选]"
        line = "%-14s%-8s%8d%9s%10s%10s%10s%9.2f  %s%s" % (
            name, nd_str, n_sig, wr_str, _fmt_pct(avg_ret), _fmt_pct(avg_dd),
            _fmt_pct(avg_mp), weight, reason, flag)
        try:
            print(line)
        except UnicodeEncodeError:
            print(line.encode("ascii", "replace").decode("ascii"))
    print("")


def _summarize(data: dict) -> None:
    mode = data.get("mode", "unknown")
    stats = data.get("stats") or {}
    n_total = len(stats)
    print("")
    print("=== 汇总 ===")
    print("  模式: %s" % mode)
    print("  战法数: %d" % n_total)
    if n_total == 0:
        print("  （无战法统计：请确认 strategy_effectiveness 表已有数据）")
        return
    promote = demote = deprecated = pending = 0
    for _s, info in stats.items():
        n = int(info.get("n_signals") or 0)
        wr = _pick_win_rate(info)
        if wr is None or n < 10:
            pending += 1
            continue
        if wr >= 0.70:
            promote += 1
        elif wr <= 0.30:
            deprecated += 1
        elif wr <= 0.45:
            demote += 1
        else:
            pending += 1
    print("  推荐升权: %d" % promote)
    print("  推荐降权: %d" % demote)
    print("  弃用候选: %d" % deprecated)
    print("  样本不足/未成熟: %d" % pending)
    print("  生效条件（auto_apply=True 且单战法样本 >= %s）"
          % ((data.get("maturity") or {}).get("min_n_for_auto_apply"),))


def main():
    parser = argparse.ArgumentParser(description="V29 战法权重推荐查看器")
    parser.add_argument("--asc", action="store_true", help="按推荐权重升序")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    data = _load()

    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return

    _print_maturity(data)
    _summarize(data)

    recs = data.get("recommendation") or {}
    stats = data.get("stats") or {}
    items = []
    for strat, rec in recs.items():
        s = stats.get(strat) or {}
        items.append((
            strat[:14],
            s.get("primary_n_days"),
            int(s.get("n_signals") or 0),
            _pick_win_rate(s),
            s.get("avg_return"),
            s.get("avg_max_drawdown"),
            s.get("avg_max_profit"),
            float(rec.get("weight") or 1.0),
            str(rec.get("reason") or ""),
        ))
    _print_table(items, asc=args.asc)


if __name__ == "__main__":
    main()