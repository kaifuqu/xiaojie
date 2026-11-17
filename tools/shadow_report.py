# -*- coding: utf-8 -*-
"""
Shadow 对照报表（Tier1-1.3 / 1.4 / 1.6 的"要不要真接线"决策依据）

读 data/runtime/shadow/shadow_*.jsonl，输出三组对照：
  1) 如果启用「剔除最大阴量」：会被否决多少条？被否决的那批，入场后 N 日收益均值/胜率是多少？
  2) 如果启用「RPS<50 否决」：同上
  3) 如果启用「推送三档」：alarm / important / observe 各多少条，占比多少

用法：
  python tools\\shadow_report.py                    # 全部日期，N=5
  python tools\\shadow_report.py --days 10          # 看 10 日
  python tools\\shadow_report.py --day 20261103     # 单日
  python tools\\shadow_report.py --md docs\\shadow_report.md   # 同时写 markdown

只读：绝不写库、绝不改任何配置。
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd


def _fmt(x):
    try:
        return "%.2f" % float(x)
    except Exception:
        return "-"


def _group_stats(recs, key, days, closes):
    """按 key 分组，算条目数与 N 日收益（相对记录里的推送价）。"""
    groups = {}
    for r in recs:
        g = bool(r.get(key))
        groups.setdefault(g, []).append(r)
    out = {}
    for g, rows in groups.items():
        rets = []
        for r in rows:
            code = str(r.get("ts_code") or "")
            d = str(r.get("trade_date") or "").replace("/", "-")[:10]
            entry = float(r.get("price") or 0.0)
            s = closes.get(code)
            if s is None or entry <= 0 or not d:
                continue
            try:
                pos = s.index.get_loc(d)
            except Exception:
                continue
            if pos < 0 or pos + days >= len(s):
                continue
            exit_px = float(s.iloc[pos + days])
            if exit_px > 0:
                rets.append((exit_px / entry - 1.0) * 100.0)
        n = len(rows)
        out[g] = {
            "n": n,
            "n_return": len(rets),
            "mean_ret": (sum(rets) / len(rets)) if rets else None,
            "win_rate": (100.0 * sum(1 for x in rets if x > 0) / len(rets)) if rets else None,
        }
    return out


def _load_closes(codes, min_date):
    try:
        from data.db_core import get_read_conn_singleton

        con = get_read_conn_singleton(max_wait_sec=5.0)
        if con is None:
            print("[提示] 只读连接不可用（daemon 可能占库），本次只出条数、不出收益。")
            return {}
        marks = ",".join(["?"] * len(codes))
        df = con.execute(
            "SELECT ts_code, trade_date, close FROM daily_data "
            "WHERE ts_code IN (%s) AND trade_date >= ? ORDER BY ts_code, trade_date" % marks,
            list(codes) + [min_date],
        ).df()
        if df is None or df.empty:
            return {}
        df["ts_code"] = df["ts_code"].astype(str).str.strip().str.upper()
        df["trade_date"] = df["trade_date"].astype(str).str.replace("/", "-", regex=False)
        df["trade_date"] = df["trade_date"].str.slice(0, 10)
        df["close"] = pd.to_numeric(df["close"], errors="coerce")
        df = df[df["close"] > 0]
        return {str(k): g.set_index("trade_date")["close"] for k, g in df.groupby("ts_code")}
    except Exception as e:  # noqa: BLE001
        print("[提示] 取行情失败(%s)，本次只出条数。" % type(e).__name__)
        return {}


def build_report(days=5, day=None):
    from core.shadow_filters import read_shadow_records

    recs = read_shadow_records(day)
    lines = []
    if not recs:
        lines.append("Shadow 记录为空（data/runtime/shadow 下没有 shadow_*.jsonl）。")
        lines.append("先让 daemon 或 UI 正常跑一次扫描，信号落库时会自动写入 Shadow 记录。")
        return "\n".join(lines), recs

    df = pd.DataFrame(recs)
    dates = sorted({str(x) for x in df.get("trade_date", pd.Series(dtype=str)).tolist() if x})
    codes = sorted({str(x) for x in df.get("ts_code", pd.Series(dtype=str)).tolist() if x})
    closes = _load_closes(codes, dates[0]) if (codes and dates) else {}

    lines.append("# Shadow 对照报表（%s，%d 条信号，%d 个交易日）" % (
        day or "全部", len(recs), len(dates)))
    lines.append("")

    for key, title in (("veto_bear_vol", "1.3 剔除最大阴量"),
                       ("veto_rps", "1.4 RPS<50 否决")):
        st = _group_stats(recs, key, days, closes)
        hit = st.get(True, {})
        keep = st.get(False, {})
        lines.append("## %s" % title)
        lines.append("- 会被否决：**%d 条**（占 %.1f%%）；保留：%d 条" % (
            hit.get("n", 0),
            100.0 * hit.get("n", 0) / max(1, len(recs)),
            keep.get("n", 0)))
        lines.append("- 被否决组 %d 日：均值 %s%% / 胜率 %s%%（样本 %d）" % (
            days, _fmt(hit.get("mean_ret")), _fmt(hit.get("win_rate")), hit.get("n_return", 0)))
        lines.append("- 保留组   %d 日：均值 %s%% / 胜率 %s%%（样本 %d）" % (
            days, _fmt(keep.get("mean_ret")), _fmt(keep.get("win_rate")), keep.get("n_return", 0)))
        verdict = "样本不足，先继续攒" 
        try:
            hm, km = hit.get("mean_ret"), keep.get("mean_ret")
            if hit.get("n_return", 0) >= 20 and km is not None and hm is not None:
                verdict = ("值得接线：被否决组明显更差（差 %.2fpp）" % (km - hm)) if hm < km - 0.5 else \
                          ("不建议接线：两组无显著差异（差 %.2fpp）" % (km - hm))
        except Exception:
            pass
        lines.append("- 结论：%s" % verdict)
        lines.append("")

    lines.append("## 1.6 推送三档（只统计，不改变投递）")
    tier = df.get("push_tier", pd.Series(dtype=str))
    if len(tier):
        vc = tier.value_counts()
        for k in ("alarm", "important", "observe"):
            n = int(vc.get(k, 0))
            lines.append("- %-9s：%4d 条（%.1f%%）" % (k, n, 100.0 * n / max(1, len(df))))
        lines.append("- 若启用三档：`observe` 这 %d 条建议只写库不推送，可减少 %.0f%% 的企微消息。"
                     % (int(vc.get("observe", 0)),
                        100.0 * int(vc.get("observe", 0)) / max(1, len(df))))
    else:
        lines.append("- 无 push_tier 字段（旧记录）。")
    lines.append("")

    lines.append("## 分池明细")
    if "pool" in df.columns:
        g = df.groupby("pool").agg(
            条数=("ts_code", "size"),
            阴量否决=("veto_bear_vol", "sum"),
            RPS否决=("veto_rps", "sum"),
        )
        lines.append("")
        lines.append("| 池 | 条数 | 阴量否决 | RPS否决 |")
        lines.append("|---|---|---|---|")
        for pool, row in g.iterrows():
            lines.append("| %s | %d | %d | %d |" % (
                pool, int(row["条数"]), int(row["阴量否决"]), int(row["RPS否决"])))
    return "\n".join(lines), recs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--day", default=None)
    ap.add_argument("--md", default=None, help="同时写出 markdown 文件")
    a = ap.parse_args()
    text, recs = build_report(days=a.days, day=a.day)
    print(text)
    if a.md:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(a.md)), exist_ok=True)
            with open(a.md, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            print("\n[已写出] %s" % a.md)
        except Exception as e:  # noqa: BLE001
            print("\n[写文件失败] %s" % e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
