# -*- coding: utf-8 -*-
"""
tools/p4_tail_calibration.py — P4 尾盘信号标定器（时间窗 / 分数档 / 仓位基准）
================================================================================
【它回答什么问题】
P4 的全部假设是「14:30 之后用临近收盘的信息做 T+1 先手底仓」。这个假设里有三组参数
目前是「按交易常识设的初值」，没有用自己系统的历史样本验证过：

  1) 尾盘阶段边界：14:30 / 14:35 / 14:45 / 14:55 —— 真的每个阶段都值得建仓吗？
  2) 分数档位：>=88 / >=78 / >=68 / >=60 —— 分数到底跟 T+1 收益正相关吗？
  3) 仓位基准 0.10、上限 0.20 —— 是不是过度下注 / 过度保守？

本工具把 signal_log 里池 = p4 的历史信号，按【信号发生时点】分桶，
对齐 daily_data 计算 T+1 开盘溢价 / T+1 收盘收益 / T+2 收益 / T+1 最大有利波动，
输出每个桶的样本数、胜率、均值，并在样本足够时给出可执行的参数建议。

【为什么以前没法标定】
signal_log 过去只有 trade_date（日级），没有分桶依据；而 remark 是给人看的文案。
V33 起 P4 会写 entry_basis = "p4_tail|phase=golden|min=880|pen=..|pos=..|grade=A"，
同时 signal_log.push_time 带时间戳，所以「按 14:35 / 14:45 分桶」终于可行。
push_time 缺失时本工具会回落到 entry_basis 里的 min=，再回落到 created_at。

【口径红线】
· read-only：只读 signal_log / daily_data，不写库、不改配置。
· 样本门槛：任何分桶样本数 < --min-sample（默认 30）一律标记 insufficient，
  绝不因为 5 条样本就改仓位参数（那是过拟合，不是优化）。
· 本工具只产出 data/p4_tail_calibration.json 建议文件，是否落到 config.yaml 由人决定。

用法：
    python tools/p4_tail_calibration.py                 # 默认回看 250 个自然日
    python tools/p4_tail_calibration.py --days 120
    python tools/p4_tail_calibration.py --probe         # 只看数据量，不做统计
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BJ_TZ = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# 阶段解析（与 core/strategies/tail_clock.py 保持同一事实来源）
# ---------------------------------------------------------------------------
def _phase_of_minute(minute: int, cfg=None) -> str:
    try:
        from core.strategies.tail_clock import resolve_tail_phase

        return resolve_tail_phase(int(minute), cfg, is_backtest=False).key
    except Exception:
        return "unknown"


def _minute_from_push_time(value: Any) -> Optional[int]:
    ts = pd.to_datetime(value, errors="coerce")
    if ts is None or pd.isna(ts):
        return None
    return int(ts.hour) * 60 + int(ts.minute)


def _minute_from_entry_basis(basis: Any) -> Optional[int]:
    s = str(basis or "")
    for token in s.replace(";", "|").split("|"):
        token = token.strip()
        if token.startswith("min="):
            try:
                return int(float(token[4:]))
            except (TypeError, ValueError):
                return None
    return None


def _gate_from_entry_basis(basis: Any) -> str:
    """从 entry_basis 里取「金门禁状态」：pass / block / unknown(历史行没有该位)。"""
    s = str(basis or "")
    for token in s.replace(";", "|").split("|"):
        token = token.strip()
        if token.startswith("gate="):
            v = token[5:].strip().lower()
            if v in ("pass", "block"):
                return v
    return "unknown"


# ---------------------------------------------------------------------------
# 读库
# ---------------------------------------------------------------------------
def _read_signals(days: int) -> pd.DataFrame:
    from data.db_core import get_read_conn, table_exists

    cutoff = (datetime.now(BJ_TZ) - timedelta(days=int(days))).strftime("%Y-%m-%d")
    with get_read_conn(read_only=True) as con:
        if not table_exists("signal_log"):
            return pd.DataFrame()
        sql = """
            SELECT ts_code, trade_date, score, burst_score, push_time, created_at,
                   push_price, pre_close, is_observation, entry_basis, remark
            FROM signal_log
            WHERE pool = 'p4' AND trade_date >= ?
            ORDER BY trade_date, ts_code
        """
        rows = con.execute(sql, [cutoff]).fetchdf()
    return rows if isinstance(rows, pd.DataFrame) else pd.DataFrame()


def _read_bars(ts_codes: List[str], start_date: str) -> pd.DataFrame:
    if not ts_codes:
        return pd.DataFrame()
    from data.db_core import get_read_conn, table_exists

    placeholders = ",".join(["?"] * len(ts_codes))
    with get_read_conn(read_only=True) as con:
        table = "vw_daily_data_compat" if table_exists("vw_daily_data_compat") else "daily_data"
        sql = (
            f"SELECT ts_code, trade_date, open, high, low, close FROM {table} "
            f"WHERE ts_code IN ({placeholders}) AND trade_date >= ? "
            f"ORDER BY ts_code, trade_date"
        )
        rows = con.execute(sql, list(ts_codes) + [start_date]).fetchdf()
    return rows if isinstance(rows, pd.DataFrame) else pd.DataFrame()


# ---------------------------------------------------------------------------
# 逐信号对齐 T+1 / T+2
# ---------------------------------------------------------------------------
def _attach_outcomes(sig: pd.DataFrame, bars: pd.DataFrame, cfg=None) -> pd.DataFrame:
    if sig.empty or bars.empty:
        return pd.DataFrame()

    # 时点分钟数：push_time → entry_basis[min=] → created_at
    minutes: List[Optional[int]] = []
    for _, r in sig.iterrows():
        m = _minute_from_push_time(r.get("push_time"))
        if m is None:
            m = _minute_from_entry_basis(r.get("entry_basis"))
        if m is None:
            m = _minute_from_push_time(r.get("created_at"))
        minutes.append(m)
    sig = sig.copy()
    sig["_minute"] = minutes
    sig["phase"] = [
        _phase_of_minute(m, cfg) if m is not None else "unknown" for m in sig["_minute"]
    ]

    bars = bars.copy()
    bars["trade_date"] = bars["trade_date"].astype(str)
    by_code: Dict[str, pd.DataFrame] = {
        str(k): v.reset_index(drop=True) for k, v in bars.groupby("ts_code")
    }

    out_rows: List[Dict[str, Any]] = []
    for _, r in sig.iterrows():
        code = str(r.get("ts_code") or "")
        td = str(r.get("trade_date") or "")[:10]
        b = by_code.get(code)
        if b is None or b.empty:
            continue
        idx_arr = b.index[b["trade_date"] == td].tolist()
        if not idx_arr:
            continue
        i = int(idx_arr[0])
        entry = float(r.get("push_price") or 0.0)
        if entry <= 0:
            entry = float(b.loc[i, "close"] or 0.0)
        if entry <= 0:
            continue
        row = {
            "ts_code": code,
            "trade_date": td,
            "phase": r.get("phase"),
            "gate": _gate_from_entry_basis(r.get("entry_basis")),
            "score": float(r.get("burst_score") or r.get("score") or 0.0),
            "observation": bool(r.get("is_observation") or False),
            "entry": entry,
            "t1_gap_pct": np.nan,
            "t1_ret_pct": np.nan,
            "t1_mfe_pct": np.nan,
            "t2_ret_pct": np.nan,
        }
        if i + 1 < len(b):
            o1 = float(b.loc[i + 1, "open"] or 0.0)
            h1 = float(b.loc[i + 1, "high"] or 0.0)
            c1 = float(b.loc[i + 1, "close"] or 0.0)
            if o1 > 0:
                row["t1_gap_pct"] = (o1 / entry - 1.0) * 100.0
            if h1 > 0:
                row["t1_mfe_pct"] = (h1 / entry - 1.0) * 100.0
            if c1 > 0:
                row["t1_ret_pct"] = (c1 / entry - 1.0) * 100.0
        if i + 2 < len(b):
            c2 = float(b.loc[i + 2, "close"] or 0.0)
            if c2 > 0:
                row["t2_ret_pct"] = (c2 / entry - 1.0) * 100.0
        out_rows.append(row)
    return pd.DataFrame(out_rows)


# ---------------------------------------------------------------------------
# 统计 + 建议
# ---------------------------------------------------------------------------
PHASE_ORDER = ("observe", "golden", "late", "freeze", "closed", "pre_tail", "unknown")
SCORE_BANDS = ((88.0, 101.0, ">=88"), (78.0, 88.0, "78-88"), (68.0, 78.0, "68-78"),
               (60.0, 68.0, "60-68"), (0.0, 60.0, "<60"))


def _bucket_stats(df: pd.DataFrame) -> Dict[str, Any]:
    valid = df.dropna(subset=["t1_ret_pct"])
    if valid.empty:
        return {"n": 0}

    def _stat(sub: pd.DataFrame) -> Dict[str, Any]:
        n = int(len(sub))
        if n == 0:
            return {"n": 0}
        rets = sub["t1_ret_pct"].astype(float)
        wins = sub[sub["t1_ret_pct"] > 0]["t1_ret_pct"].astype(float)
        losses = sub[sub["t1_ret_pct"] <= 0]["t1_ret_pct"].astype(float)
        avg_win = float(wins.mean()) if len(wins) else 0.0
        avg_loss = float(losses.mean()) if len(losses) else 0.0
        return {
            "n": n,
            "win_rate": round(float((rets > 0).mean()), 4),
            "mean_t1_ret_pct": round(float(rets.mean()), 4),
            "median_t1_ret_pct": round(float(rets.median()), 4),
            "mean_t1_gap_pct": round(float(sub["t1_gap_pct"].dropna().mean()), 4) if sub["t1_gap_pct"].notna().any() else None,
            "mean_t1_mfe_pct": round(float(sub["t1_mfe_pct"].dropna().mean()), 4) if sub["t1_mfe_pct"].notna().any() else None,
            "mean_t2_ret_pct": round(float(sub["t2_ret_pct"].dropna().mean()), 4) if sub["t2_ret_pct"].notna().any() else None,
            "avg_win_pct": round(avg_win, 4),
            "avg_loss_pct": round(avg_loss, 4),
            "expectancy_pct": round(float(rets.mean()), 4),
        }

    out: Dict[str, Any] = {"n": int(len(valid)), "overall": _stat(valid)}
    out["by_phase"] = {p: _stat(valid[valid["phase"] == p]) for p in PHASE_ORDER if (valid["phase"] == p).any()}
    bands = {}
    for lo, hi, label in SCORE_BANDS:
        sub = valid[(valid["score"] >= lo) & (valid["score"] < hi)]
        if len(sub):
            bands[label] = _stat(sub)
    out["by_score_band"] = bands
    if "observation" in valid.columns:
        out["observation_vs_actionable"] = {
            "observation": _stat(valid[valid["observation"]]),
            "actionable": _stat(valid[~valid["observation"]]),
        }
    return out


def _recommend(stats: Dict[str, Any], min_sample: int) -> Dict[str, Any]:
    """在样本足够时给出参数建议；不足则明确拒绝给建议（宁可不改，也不要过拟合）。"""
    rec: Dict[str, Any] = {
        "generated_at": datetime.now(BJ_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "min_sample_per_bucket": int(min_sample),
        "auto_apply": False,
        "config_patch": {},
        "notes": [],
    }
    by_phase = stats.get("by_phase") or {}
    if not by_phase:
        rec["status"] = "no_data"
        rec["notes"].append("signal_log 中暂无可用 P4 样本（需 push_time 或 entry_basis.min 才能分桶）")
        return rec

    usable = {k: v for k, v in by_phase.items() if int(v.get("n", 0)) >= min_sample}
    rec["phase_table"] = by_phase
    if not usable:
        rec["status"] = "insufficient_sample"
        rec["notes"].append(
            f"每个阶段样本数均 < {min_sample}，维持当前阶段仓位系数与冻结边界"
            "（14:30–14:35 半仓 / 14:35–14:45 主建仓 / 14:45–14:55 减半 / 14:55 后冻结）"
        )
        return rec

    best = max(usable.items(), key=lambda kv: float(kv[1].get("expectancy_pct", -99)))
    best_exp = float(best[1].get("expectancy_pct", 0.0)) or 0.0
    patch: Dict[str, Any] = {}
    for phase_key, s in usable.items():
        exp = float(s.get("expectancy_pct", 0.0))
        if exp <= 0:
            patch[f"phase_position_mult.{phase_key}"] = 0.0
            rec["notes"].append(f"{phase_key}: T+1 期望 {exp:.2f}% ≤ 0 → 建议该时间段不建仓")
            continue
        mult = 1.0 if best_exp <= 0 else float(np.clip(exp / best_exp, 0.2, 1.0))
        patch[f"phase_position_mult.{phase_key}"] = round(mult, 2)

    # 分数档：找出「T+1 期望转正」的最低档 → 作为建仓下限参考
    bands = stats.get("by_score_band") or {}
    ok_bands = [(lo, label) for lo, hi, label in SCORE_BANDS
                if (b := bands.get(label)) and int(b.get("n", 0)) >= min_sample
                and float(b.get("expectancy_pct", 0.0)) > 0]
    if ok_bands:
        patch["score_min_entry_reference"] = max(lo for lo, _ in ok_bands)
        rec["notes"].append(
            "分数档 " + " / ".join(f"{lab}:{bands[lab]['expectancy_pct']:.2f}%" for _, lab in ok_bands)
            + f" → 建仓下限参考 {patch['score_min_entry_reference']} 分"
        )

    # 基准仓位：Quarter-Kelly（用全样本胜率/赔率）
    overall = stats.get("overall") or {}
    n = int(overall.get("n", 0))
    wr = float(overall.get("win_rate", 0.0) or 0.0)
    aw = float(overall.get("avg_win_pct", 0.0) or 0.0)
    al = abs(float(overall.get("avg_loss_pct", 0.0) or 0.0))
    if n >= min_sample and aw > 0 and al > 0:
        b = aw / al
        kelly = wr - (1.0 - wr) / b
        base = float(np.clip(kelly * 0.25, 0.03, 0.20))
        patch["p4_base_position_ratio"] = round(base, 3)
        rec["notes"].append(
            f"Quarter-Kelly: 胜率{wr:.0%} 赔率{b:.2f} → Kelly {kelly:.2f}，建议基准仓位 {base:.1%}"
        )

    rec["status"] = "ok"
    rec["config_patch"] = patch
    rec["auto_apply"] = False
    rec["notes"].append("建议仅供人工确认后写入 config.yaml 的 p4 段（工具不自动改配置）")
    return rec


# ---------------------------------------------------------------------------
# 日线代理研究：在没有历史分钟快照的情况下，验证「收盘位置」因子的真实有效性
# ---------------------------------------------------------------------------
_CPR_BUCKETS = ((0.0, 0.30, "收盘在日内下沿 0-30%"),
                (0.30, 0.50, "下沿 30-50%"),
                (0.50, 0.75, "中枢 50-75%"),
                (0.75, 0.90, "上沿 75-90%"),
                (0.90, 1.01, "贴日高 90-100%"))


def _read_daily_window(days: int) -> pd.DataFrame:
    from data.db_core import get_read_conn, table_exists

    cutoff = (datetime.now(BJ_TZ) - timedelta(days=int(days))).strftime("%Y-%m-%d")
    with get_read_conn(read_only=True) as con:
        table = "vw_daily_data_compat" if table_exists("vw_daily_data_compat") else "daily_data"
        sql = (
            f"SELECT ts_code, trade_date, open, high, low, close, pre_close, vol "
            f"FROM {table} WHERE trade_date >= ? ORDER BY ts_code, trade_date"
        )
        return con.execute(sql, [cutoff]).fetchdf()


def _cpr_bucketize(base: pd.DataFrame) -> Dict[str, Any]:
    buckets: Dict[str, Any] = {}
    for lo, hi, label in _CPR_BUCKETS:
        sub = base[(base["cpr"] >= lo) & (base["cpr"] < hi)]
        if len(sub) == 0:
            continue
        buckets[label] = {
            "n": int(len(sub)),
            "mean_t1_ret_pct": round(float(sub["t1_ret_pct"].mean()), 4),
            "mean_t1_gap_pct": round(float(sub["t1_gap_pct"].mean()), 4),
            "win_rate": round(float((sub["t1_ret_pct"] > 0).mean()), 4),
        }
    return buckets


def proxy_close_pos_study(days: int = 250, min_sample: int = 200,
                          pct_low: float = 0.0, pct_high: float = 5.0) -> Dict[str, Any]:
    """用全市场日线验证「收盘位于日内振幅的位置」对 T+1 的真实预测力。

    【为什么能做】P4 新增的正向因子 tail_edge_close_pos 只依赖日线可还原的量
    （close / high / low），因此可以用全市场多年数据直接检验，不必等分钟快照。
    【分层验证】原始全市场结果容易被「均值回归」主导（A 股涨幅 0-5% 的票，
    收盘越贴日高，次日越容易回落），所以必须再按「像不像 P4 候选」做条件切片：
      · pct 0-2 / 2-3.5 / 3.5-5 三档分别看 cpr 的单调性
      · P4-like：收盘站上 ma20 + 涨幅 1-4% + 量比 >= 1.0
    只有条件切片里贴日高仍占优，才配得上正向加分；否则就要降权。
    """
    df = _read_daily_window(days)
    out: Dict[str, Any] = {"days": int(days), "rows": int(len(df) if isinstance(df, pd.DataFrame) else 0)}
    if not isinstance(df, pd.DataFrame) or df.empty:
        out["status"] = "no_data"
        return out

    df = df.copy()
    df["trade_date"] = df["trade_date"].astype(str)
    df = df.sort_values(["ts_code", "trade_date"])
    g = df.groupby("ts_code", sort=False)
    df["next_open"] = g["open"].shift(-1)
    df["next_close"] = g["close"].shift(-1)

    for c in ("open", "high", "low", "close", "pre_close", "vol", "next_open", "next_close",
              "ma20", "vol_ratio"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    rng = (df["high"] - df["low"]).replace(0, np.nan)
    df["cpr"] = ((df["close"] - df["low"]) / rng).clip(0.0, 1.0)
    df["pct_chg"] = (df["close"] / df["pre_close"].replace(0, np.nan) - 1.0) * 100.0
    df["t1_gap_pct"] = (df["next_open"] / df["close"].replace(0, np.nan) - 1.0) * 100.0
    df["t1_ret_pct"] = (df["next_close"] / df["close"].replace(0, np.nan) - 1.0) * 100.0

    ok = (
        df["cpr"].notna() & df["t1_ret_pct"].notna() & df["t1_gap_pct"].notna()
        & (df["pct_chg"] >= pct_low) & (df["pct_chg"] <= pct_high) & (df["vol"] > 0)
    )
    base = df[ok]
    out["filter"] = {"pct_chg_range": [pct_low, pct_high], "rows_after_filter": int(len(base))}
    if base.empty:
        out["status"] = "no_rows_after_filter"
        return out

    out["by_close_pos"] = _cpr_bucketize(base)
    out["overall"] = {
        "n": int(len(base)),
        "mean_t1_ret_pct": round(float(base["t1_ret_pct"].mean()), 4),
        "mean_t1_gap_pct": round(float(base["t1_gap_pct"].mean()), 4),
        "win_rate": round(float((base["t1_ret_pct"] > 0).mean()), 4),
    }

    # 分涨幅档看 cpr 单调性（避免全市场平均掩盖结构）
    out["by_pct_band"] = {}
    for lo, hi, label in ((0.0, 2.0, "0-2%"), (2.0, 3.5, "2-3.5%"), (3.5, 5.0, "3.5-5%")):
        sub = base[(base["pct_chg"] >= lo) & (base["pct_chg"] < hi)]
        if len(sub):
            out["by_pct_band"][label] = {
                "n": int(len(sub)),
                "by_close_pos": _cpr_bucketize(sub),
            }

    # P4-like 切片：收盘站上 ma20 + 涨幅 1-4% + 量比 >= 1.0（与 P4 候选形态最接近）
    like = base.copy()
    if "ma20" in like.columns:
        like = like[(like["ma20"] > 0) & (like["close"] > like["ma20"])]
    if "vol_ratio" in like.columns:
        like = like[like["vol_ratio"].fillna(0.0) >= 1.0]
    like = like[(like["pct_chg"] >= 1.0) & (like["pct_chg"] <= 4.0)]
    out["p4_like"] = {
        "n": int(len(like)),
        "by_close_pos": _cpr_bucketize(like) if len(like) else {},
        "overall": {
            "mean_t1_ret_pct": round(float(like["t1_ret_pct"].mean()), 4) if len(like) else None,
            "win_rate": round(float((like["t1_ret_pct"] > 0).mean()), 4) if len(like) else None,
        },
    }

    # 结论：以「P4-like 切片」为准，样本不足再退回全市场宽口径
    notes: List[str] = []
    target = (out["p4_like"]["by_close_pos"] if int(out["p4_like"].get("n", 0)) >= min_sample
              else out["by_close_pos"])
    scope = "P4-like" if int(out["p4_like"].get("n", 0)) >= min_sample else "全市场"
    hi_band = target.get("贴日高 90-100%")
    mid_band = target.get("中枢 50-75%")
    verdict = "insufficient_sample"
    if hi_band and mid_band and hi_band["n"] >= min_sample and mid_band["n"] >= min_sample:
        edge = hi_band["mean_t1_ret_pct"] - mid_band["mean_t1_ret_pct"]
        out["edge_vs_mid_pct"] = round(float(edge), 4)
        out["scope"] = scope
        if edge > 0.05:
            verdict = "confirmed"
            notes.append(f"[{scope}] 贴日高相对中枢超额 {edge:+.2f}% → 0.90 阈值成立")
        elif edge > -0.05:
            verdict = "neutral"
            notes.append(f"[{scope}] 贴日高相对中枢 {edge:+.2f}%（≈0）→ 区分力弱，建议降权")
        else:
            verdict = "inverted"
            notes.append(f"[{scope}] 贴日高反而跑输中枢 {edge:+.2f}% → 收盘位置是「诱多」信号，不是优点")
    else:
        notes.append(f"分桶样本不足 {min_sample}，无法判定")
    out["verdict"] = verdict
    out["notes"] = notes
    out["config_patch"] = {}
    if verdict == "confirmed":
        out["config_patch"] = {"tail_edge_close_pos_strong": 0.9, "tail_edge_close_pos_mid": 0.75}
    elif verdict in ("neutral", "inverted"):
        # 收盘位置不再作为独立加分项：改为「贴着日高 = 需要更多确认」的风险提示
        out["config_patch"] = {"tail_edge_close_pos_bonus": 0.0,
                               "tail_edge_close_pos_flag_high": 0.9}
    return out


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P4 尾盘信号标定器")
    ap.add_argument("--days", type=int, default=250, help="回看自然日数（默认 250）")
    ap.add_argument("--min-sample", type=int, default=30, help="单桶最小样本数（默认 30）")
    ap.add_argument("--probe", action="store_true", help="只输出数据量，不做统计")
    ap.add_argument("--proxy", action="store_true",
                    help="日线代理研究：验证「收盘位置」因子对 T+1 的预测力（不依赖分钟快照）")
    ap.add_argument("--out", type=str, default=os.path.join("data", "p4_tail_calibration.json"))
    args = ap.parse_args(argv)

    if args.proxy:
        res = proxy_close_pos_study(days=args.days, min_sample=max(200, args.min_sample))
        print(f"\n[P4代理研究] 日线行数 {res.get('rows')} / 过滤后 {res.get('filter', {}).get('rows_after_filter')}")
        for label, s in (res.get("by_close_pos") or {}).items():
            print(f"  {label:22s} n={s['n']:6d} 胜率{s['win_rate']*100:5.1f}% "
                  f"T+1跳空{s['mean_t1_gap_pct']:+5.2f}% T+1收益{s['mean_t1_ret_pct']:+5.2f}%")
        print(f"  overall: {res.get('overall')}")
        for n in res.get("notes", []):
            print("  -", n)
        if res.get("config_patch"):
            print("  config_patch:", json.dumps(res["config_patch"], ensure_ascii=False))
        out_path = os.path.join("data", "p4_tail_close_pos_study.json")
        _write(out_path, res)
        print(f"\n[P4代理研究] 结果已写入 {out_path}")
        return 0

    try:
        from core.config_manager import get_p4_tail_screener_config

        cfg = get_p4_tail_screener_config()
    except Exception as ex:
        print(f"[warn] P4 配置加载失败，使用内置默认阶段边界: {ex}")
        cfg = None

    sig = _read_signals(args.days)
    print(f"[P4标定] signal_log 中 p4 信号 = {len(sig)} 条（回看 {args.days} 天）")
    if sig.empty:
        print("[P4标定] 无样本：P4 从未落库信号，或 trade_date 早于回看窗口。")
        _write(args.out, {"status": "no_data", "signal_rows": 0,
                          "notes": ["signal_log 无 p4 记录"], })
        return 0

    codes = sorted({str(c) for c in sig["ts_code"].dropna().unique().tolist()})
    start = str(sig["trade_date"].min())[:10]
    bars = _read_bars(codes, start)
    print(f"[P4标定] 对齐 daily_data：{len(codes)} 只票 / {len(bars)} 根 K 线")

    if args.probe:
        sig["_minute_guess"] = [
            _minute_from_push_time(v) or _minute_from_entry_basis(b) or _minute_from_push_time(c)
            for v, b, c in zip(sig.get("push_time", []), sig.get("entry_basis", []), sig.get("created_at", []))
        ]
        have_min = int(sig["_minute_guess"].notna().sum()) if "_minute_guess" in sig else 0
        print(f"[P4标定] 可解析出信号时点的行数 = {have_min}/{len(sig)}")
        print(sig.head(10).to_string())
        return 0

    df = _attach_outcomes(sig, bars, cfg)
    print(f"[P4标定] 可用 T+1 结果的样本 = {len(df)} 条")
    if df.empty:
        _write(args.out, {"status": "no_forward_data", "signal_rows": int(len(sig))})
        return 0

    # 【V33.1】按金门禁状态剔除「从未真正成为推送信号」的记录。
    # 历史行（2026-11 之前）没有 gate 位 → 归为 unknown，保留但单独计数，
    # 因为那批样本虽然可能含水，但也是当前唯一的历史证据，不能一句话删掉。
    gate_counts = df["gate"].value_counts().to_dict() if "gate" in df.columns else {}
    blocked = int(gate_counts.get("block", 0) or 0)
    if blocked:
        df = df[df["gate"] != "block"].copy()
        print(f"[P4标定] 剔除 gate=block（被金门禁拦掉、从未推送）{blocked} 条")
    print(f"[P4标定] gate 分布 = {gate_counts}（unknown=历史行无该位）")

    stats = _bucket_stats(df)
    rec = _recommend(stats, args.min_sample)

    print("\n=== 按尾盘阶段 ===")
    for p, s in (stats.get("by_phase") or {}).items():
        print(f"  {p:8s} n={s['n']:4d} 胜率{s['win_rate']*100:5.1f}% "
              f"T+1均值{s['mean_t1_ret_pct']:+6.2f}% T+2均值{(s.get('mean_t2_ret_pct') or 0):+6.2f}%")

    print("\n=== 按综合分档 ===")
    for lab, s in (stats.get("by_score_band") or {}).items():
        print(f"  {lab:6s} n={s['n']:4d} 胜率{s['win_rate']*100:5.1f}% T+1均值{s['mean_t1_ret_pct']:+6.2f}%")

    print("\n=== 建议 ===")
    for note in rec.get("notes", []):
        print("  -", note)
    if rec.get("config_patch"):
        print("  config_patch:", json.dumps(rec["config_patch"], ensure_ascii=False))

    payload = {"status": rec.get("status"), "signal_rows": int(len(sig)),
               "aligned_rows": int(len(df)), "stats": stats, "recommendation": rec}
    _write(args.out, payload)
    print(f"\n[P4标定] 建议已写入 {args.out}")
    return 0


def _write(path: str, payload: Dict[str, Any]) -> None:
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    except Exception as ex:
        print(f"[warn] 建议文件写入失败: {ex}")


if __name__ == "__main__":
    raise SystemExit(main())
