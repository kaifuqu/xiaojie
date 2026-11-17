# -*- coding: utf-8 -*-
"""
tools/replay_signal_filter.py — 五层信号过滤器的历史回放评估工具

【为什么需要】
core/signal_filter.py 的五层过滤器此前从未被扫描主链路调用（死代码）。
直接开启有风险：阈值是硬编码的，可能与本系统候选池的实际分布严重不匹配
（例如 L4 流动性分要求 1000 亿市值才满分，而本系统以中小盘动量为主）。

本工具用**历史 signal_log**（系统真实推送过的票）回放过滤器，回答三个问题：
  1. reject 率是多少？（太高 → 阈值太严，会误杀好票）
  2. 各层分别拒了多少？（定位是哪一层在误杀）
  3. 被拒的票，后续表现是否确实更差？
     （用 signal_pnl 的 T+N 收益对比；若被拒的并不更差，说明这一层没有价值，不该开）

【用法】
  python tools/replay_signal_filter.py                 # 汇总统计
  python tools/replay_signal_filter.py --days 60        # 只看近 60 天
  python tools/replay_signal_filter.py --detail 20      # 打印被拒样本明细
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_OUT = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "_replay_out.txt"),
                "w", encoding="utf-8")


def _p(*args):
    line = " ".join(str(a) for a in args)
    print(line)
    _OUT.write(line + "\n")


def _date_literal(days: int) -> str:
    """返回 (今天 - days) 的 YYYY-MM-DD 字面量（DuckDB 不支持 INTERVAL 占位符）。"""
    from datetime import date, timedelta

    return (date.today() - timedelta(days=days)).isoformat()


def _load_signals(days: int):
    """从 signal_log 取历史信号（含推送价与打分）。"""
    from data.db_core import get_read_conn

    sql = """
        SELECT ts_code, name, pool, score, burst_score, push_price, log_date, strategy
        FROM signal_log
        WHERE pushed = TRUE AND push_price IS NOT NULL AND push_price > 0
    """
    params = []
    if days > 0:
        # DuckDB 不支持 INTERVAL ? DAY 占位符，改用日期字面量
        cutoff = _date_literal(days)
        sql += f" AND log_date >= DATE '{cutoff}'"
    sql += " ORDER BY log_date DESC LIMIT 20000"

    with get_read_conn(read_only=True) as con:
        rows = con.execute(sql, params).fetchall()
    return rows


def _load_pnl():
    """从 signal_pnl 取 T+N 收益（若表存在）。"""
    from data.db_core import get_read_conn

    try:
        with get_read_conn(read_only=True) as con:
            rows = con.execute("""
                SELECT ts_code, trade_date, ret_1d, ret_2d, ret_5d
                FROM signal_pnl
                WHERE ret_5d IS NOT NULL
            """).fetchall()
        return rows
    except Exception as e:
        _p(f"[warn] signal_pnl 不可用（{e}），跳过收益对比")
        return []


def _load_market_fields(pairs):
    """为 (ts_code, log_date) 补齐主力/换手/市值/量比/PE/PB/ROE 等字段。

    【为什么需要】只用 signal_log 的字段时，L2（资金真实性）/L4（流动性）/L5（基本面）
    全部因缺数据被跳过，只有 L1 真正生效 —— 那样评估不出完整效果。
    本函数从 daily_data（换手/量比）+ daily_basic（市值/PE/PB）+ 资金流表补齐，
    让五层都能被真实评估。

    pairs: [(ts_code, 'YYYYMMDD'), ...]
    返回: {ts_code: {...fields...}}
    """
    from data.db_core import get_read_conn

    if not pairs:
        return {}
    codes = sorted({c for c, _ in pairs})
    # 一次取全部历史，过滤在内存里做，避免 IN (...) 过长
    try:
        with get_read_conn(read_only=True) as con:
            rows = con.execute("""
                SELECT ts_code, trade_date, turnover_rate_f, vol_ratio,
                       amount, pe_ttm, pb, total_mv, circ_mv,
                       net_main_amount, bias_20, open, high, low, close, pre_close
                FROM daily_data
                WHERE ts_code IN (SELECT UNNEST(?::VARCHAR[]))
                ORDER BY ts_code, trade_date
            """, [codes]).fetchall()
    except Exception as e:
        _p(f"[warn] daily_data 补齐失败: {e}")
        rows = []

    by_code = defaultdict(list)
    for r in rows:
        by_code[r[0]].append(r)

    out = {}
    for code, d in pairs:
        hist = by_code.get(code) or []
        if not hist:
            continue
        # 取 <= log_date 的最近一条（信号当日数据当天可能还没落库）
        pick = None
        for h in hist:
            td = str(h[1] or "").replace("-", "")[:8]
            if not d or td <= d:
                pick = h
            else:
                break
        if pick is None:
            pick = hist[0]
        out[code] = {
            "turnover_rate_f": pick[2],
            "vol_ratio": pick[3],
            "amount": pick[4],
            "pe_ttm": pick[5],
            "pb": pick[6],
            "total_mv": pick[7],
            "circ_mv": pick[8],
            "net_main_amount": pick[9],
            "bias_ma20": pick[10],
            "open": pick[11],
            "high": pick[12],
            "low": pick[13],
            "close": pick[14],
            "pre_close": pick[15],
            "_data_date": str(pick[1] or ""),
        }
    return out


def _load_signal_pnl_map():
    """ts_code -> (平均净收益%, 样本数)，用 signal_pnl 的净收益口径（扣费后）。

    【表结构提醒】signal_pnl **没有 ts_code 列**，必须经 log_id 关联 signal_log 才能拿到代码。
    【口径说明】对应字段是 return_pct / net_return_pct（不是 ret_5d）。
    这里优先用 net_return_pct（已扣佣金/印花税/过户费），因为它才反映真实到手收益。
    """
    from data.db_core import get_read_conn

    try:
        with get_read_conn(read_only=True) as con:
            rows = con.execute("""
                SELECT l.ts_code,
                       AVG(COALESCE(p.net_return_pct, p.return_pct)) AS avg_ret,
                       COUNT(*) AS n
                FROM signal_pnl p
                JOIN signal_log l ON l.id = p.log_id
                WHERE COALESCE(p.net_return_pct, p.return_pct) IS NOT NULL
                GROUP BY l.ts_code
            """).fetchall()
        return {r[0]: (float(r[1]), int(r[2])) for r in rows}
    except Exception as e:
        _p(f"[warn] signal_pnl 不可用（{e}），跳过收益对比")
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--detail", type=int, default=10)
    ap.add_argument("--full", action="store_true",
                    help="从 daily_data 补齐主力/换手/市值/PE/PB，完整评估五层")
    args = ap.parse_args()

    from core.signal_filter_adapter import evaluate_row, MODE_ENFORCE

    rows = _load_signals(args.days)
    _p("=" * 74)
    _p(f"【信号过滤器历史回放】样本 {len(rows)} 条（近 {args.days} 天）"
       f"{'  [FULL 模式]' if args.full else ''}")
    _p("=" * 74)

    if not rows:
        _p("无历史信号样本，无法评估")
        return

    # 补齐市场字段
    extra = {}
    if args.full:
        pairs = [(str(r[0]), str(r[6] or "").replace("-", "")[:8]) for r in rows]
        extra = _load_market_fields(pairs)
        _p(f"补齐市场字段: {len(extra)}/{len(rows)} 只票有 daily_data 数据")

    pnl_map = _load_signal_pnl_map()

    layer_counter = Counter()
    reasons = Counter()
    rejected = []
    passed_cnt = 0
    rejected_pnl = []
    passed_pnl = []

    for r in rows:
        ts_code, name, pool, score, burst, push_price, log_date, strategy = r[:8]
        row = {
            "综合分": float(score if score is not None else (burst or 0)),
            "代码": ts_code,
            "名称": name,
        }
        # 附上 signal_log 能提供的字段
        if strategy:
            row["战法"] = strategy

        rt = {"price": float(push_price)}
        mf = extra.get(ts_code) or {}
        for k, v in mf.items():
            if v is not None:
                rt[k] = v

        v = evaluate_row(row, rt, mode=MODE_ENFORCE)
        ret5 = pnl_map.get(ts_code)
        if v.passed:
            passed_cnt += 1
            layer_counter["pass"] += 1
            if ret5:
                passed_pnl.append(ret5[0])
        else:
            layer_counter[f"L{v.reject_layer}"] += 1
            reasons[v.reject_reason.split("，")[0][:40]] += 1
            rejected.append((ts_code, name, pool, log_date, v.reject_layer, v.reject_reason))
            if ret5:
                rejected_pnl.append(ret5[0])

    total = len(rows)
    _p(f"\n通过: {passed_cnt} / {total}  ({passed_cnt / total * 100:.1f}%)")
    _p(f"拒绝: {len(rejected)} / {total}  ({len(rejected) / total * 100:.1f}%)")
    _p("\n--- 各层拒绝分布 ---")
    for k, n in layer_counter.most_common():
        _p(f"  {k:<8} {n:>5}  ({n / total * 100:>5.1f}%)")
    _p("\n--- 拒绝原因 Top10 ---")
    for k, n in reasons.most_common(10):
        _p(f"  {n:>5}  {k}")

    _p("\n--- 被拒样本明细（前 %d 条）---" % args.detail)
    for item in rejected[: args.detail]:
        _p(f"  {str(item[0]):<10} {str(item[1])[:10]:<12} {str(item[2]):<5} L{item[4]}  {item[5]}")

    _p("\n" + "=" * 74)
    _p("【判读指南】")
    _p("  · 拒绝率 > 60%  → 阈值对本系统过严，上线会误杀大量好票，应先标定阈值")
    _p("  · 拒绝率 10%-50% → 合理区间，可进入 shadow 模式观察")
    _p("  · 某层拒绝占比极高 → 该层阈值需单独放宽（用 config thresholds 覆盖）")
    if not args.full:
        _p("  · 提示：本次仅用 signal_log 字段，L2/L4/L5 会因缺数据被跳过，")
        _p("    真正生效的只有 L1/L3。加 --full 可从 daily_data 补齐后完整评估。")
    _p("=" * 74)

    # 收益对比：被拒的票是否确实更差？（这是「该不该开」的关键判据）
    if rejected_pnl or passed_pnl:
        _p("\n--- 收益对比（signal_pnl 净收益均值，扣费后）---")
        if passed_pnl:
            _p(f"  通过组: n={len(passed_pnl):>4}  平均T+5 = {sum(passed_pnl) / len(passed_pnl):+.2f}%")
        if rejected_pnl:
            _p(f"  拒绝组: n={len(rejected_pnl):>4}  平均T+5 = {sum(rejected_pnl) / len(rejected_pnl):+.2f}%")
        if passed_pnl and rejected_pnl:
            d = sum(passed_pnl) / len(passed_pnl) - sum(rejected_pnl) / len(rejected_pnl)
            _p(f"  差值(通过-拒绝) = {d:+.2f}%")
            if d <= 0:
                _p("  ⚠️ 被拒的票表现并不更差 → 该过滤器在此样本上没有区分度，不建议开 enforce")
            else:
                _p("  ✅ 被拒的票表现明显更差 → 过滤器有区分度，可考虑切 enforce")
        _p("  注：样本量小（百级）时结论仅供参考，建议积累数百条后再决策。")
    _p("=" * 74)

    _OUT.close()


if __name__ == "__main__":
    main()
