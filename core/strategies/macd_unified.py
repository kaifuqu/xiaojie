# -*- coding: utf-8 -*-
"""
core/strategies/macd_unified.py
========================================================================
MACD 口径统一层
========================================================================

【为什么需要这个模块 —— #8 战略 #8 的真正病灶】
表面上 P3 用「macd_hist 未转强（<0）」否决、P5 用「macd > sig」确认，
是两套标准。但实测生产库（2026-10-21）：

  daily_data.macd_hist == (macd - macd_signal)      ← 1× 口径
  indicator_calc.macd_bar == (diff - dea) * 2        ← 2× 口径

  两者「dif>dea」与「hist>0」的布尔结果 227158 行【完全一致，0 行不一致】。
  也就是说：P3/P5 的【布尔判定】其实没有分歧。

  真正的病灶有三个，全部是「尺度 / 列名 / 缺失」问题：

  1) 尺度混用（致命）
     阈值 macd_bar_kill 是【绝对值】阈值（默认量级如 -0.09 / -0.13 / -0.22），
     而取数端 curr.get('macd_bar', curr.get('macd_hist', 0.0)) 优先取 2× 口径的
     macd_bar，取不到才退化到 1× 的 macd_hist。
     → 同一套阈值，在「有 macd_bar 的数据源」上比 2×，
       在「只有 macd_hist 的数据源」（生产 daily_data 就是）上比 1×，
       判定宽严整整差一倍 → 同一只票在不同池得到相反结论。

  2) 列名不存在
     macd_diff / macd_dea 这两个列名【在生产 daily_data 里根本不存在】
     （只有 macd / macd_signal / macd_hist 三列）。
     core/strategies/strat_p2_auction.py:103、strat_p3_intraday.py:322、
     strat_p4_tail.py:296 全部用 macd_diff / macd_dea 判定，
     取不到 → 两侧都得 0.0 → 0.0 > 0.0 恒为 False
     → 这三处的「MACD 强势」加分项【永远不成立】，恒定失效。

  3) 零值与缺失混淆
     0.0 既可能是「真的等于 0」，也可能是「列不存在，取不到」。
     判定 `macd_diff > macd_dea` 在缺失时静默为 False，无告警。

【本模块的职责】
  1) 唯一列名解析：MACD_HIST_COLS / MACD_DIF_COLS / MACD_SIG_COLS
  2) 唯一尺度归一：把任何来源的柱体折算成【1× 口径】（与生产 daily_data 对齐）
  3) 唯一布尔判定：is_macd_bull() / is_macd_bear() / macd_hist_series()
  4) 缺失可观测：返回 None 而不是 0.0，让调用方能区分「缺失」与「真零」

【口径锚点（不可改）】
  1× 柱体  = macd_hist = (macd - macd_signal)   ← 生产 daily_data 实测口径
  2× 柱体  = macd_bar  = (macd_diff - macd_dea) * 2  ← indicator_calc 计算口径
  归一目标：一切对外暴露的柱体数值，一律折算为 1× 口径。

【红线】
  · 不改变任何策略的阈值数值，只统一【取数口径】与【判定方式】
  · 缺失数据一律返回 None（表示"不可判定"），由调用方显式决策，
    绝不静默当成 0.0 参与比较
  · 所有函数纯函数、无副作用、可单测
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

try:
    import numpy as np
    import pandas as pd
except Exception:  # pragma: no cover
    np = None
    pd = None


# ============================ 列名口径 ============================
# 柱体候选列（按优先级）
MACD_HIST_COLS = ("macd_hist", "macd_bar")
# DIF 候选列（DIF / macd）
MACD_DIF_COLS = ("macd", "macd_diff", "macd_dif", "dif")
# DEA 候选列（DEA / macd_signal）
MACD_SIG_COLS = ("macd_signal", "macd_dea", "dea", "macd_sig")

# 2× → 1× 的折算因子
_HIST_2X_SCALE = 2.0


def _to_float(v: Any) -> Optional[float]:
    """安全转 float；无法转换或 NaN 返回 None（表示"缺失/不可判定"）。"""
    if v is None:
        return None
    try:
        if pd is not None and isinstance(v, (float, int)) and v != v:
            return None
    except Exception:
        pass
    try:
        if np is not None:
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            if f != f:  # NaN
                return None
            return f
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


def _row_get(row: Any, col: str) -> Optional[float]:
    """从 pandas Series / dict 取列值。"""
    if row is None:
        return None
    try:
        if hasattr(row, "get"):
            v = row.get(col, None)
            if v is not None:
                return _to_float(v)
    except Exception:
        pass
    try:
        if isinstance(row, dict) and col in row:
            return _to_float(row[col])
    except Exception:
        pass
    return None


# ============================ 核心解析 ============================

def macd_hist_raw(row: Any) -> Optional[float]:
    """
    取出【原始尺度】的 MACD 柱体。不做尺度归一。

    返回 None 表示数据缺失（列不存在 / 值为 NaN）。
    """
    for c in MACD_HIST_COLS:
        v = _row_get(row, c)
        if v is not None:
            return v
    return None


def macd_hist_normalized(row: Any) -> Optional[float]:
    """
    取出【1× 统一口径】的 MACD 柱体。

    折算规则：
      · macd_hist  → 已是 1×，直接用
      · macd_bar   → 2× 口径，除以 2 折算成 1×
      · 两个都没有 → 用 (macd - macd_signal) 现算（1×）
      · 全都没有   → None（不可判定）

    为什么要归一：见模块 docstring「尺度混用（致命）」。
    """
    # 1) 优先用 macd_hist（生产 1× 口径，最可信）
    v = _row_get(row, "macd_hist")
    if v is not None:
        return v

    # 2) macd_bar 是 2× 口径，折算
    v = _row_get(row, "macd_bar")
    if v is not None:
        return v / _HIST_2X_SCALE

    # 3) 用 dif - dea 现算（1×）
    dif = macd_dif(row)
    sig = macd_signal(row)
    if dif is not None and sig is not None:
        return dif - sig

    return None


def macd_dif(row: Any) -> Optional[float]:
    """取 DIF（macd_diff / macd / dif）。缺失返回 None。"""
    for c in MACD_DIF_COLS:
        v = _row_get(row, c)
        if v is not None:
            return v
    return None


def macd_signal(row: Any) -> Optional[float]:
    """取 DEA（macd_signal / macd_dea / dea）。缺失返回 None。"""
    for c in MACD_SIG_COLS:
        v = _row_get(row, c)
        if v is not None:
            return v
    return None


# ============================ 布尔判定 ============================

def is_macd_bull(row: Any, *, strict: bool = True) -> Optional[bool]:
    """
    MACD 多头判定（统一口径）。

    判定依据（两条等价路径任一成立即可）：
      A) 柱体 > 0        （1× 口径）
      B) DIF > DEA

    strict=True  ：柱体 > 0 或 DIF > DEA → True；两者都 ≤ 0/相等 → False
    strict=False ：柱体 ≥ 0 视为 True（含临界）

    缺失（柱体与 DIF/DEA 都取不到）→ 返回 None（不可判定），
    调用方必须显式处理，绝不能当成 False 静默通过。

    【与旧实现的差异 · 重要】
    旧实现：macd_diff > macd_dea，两列在生产 daily_data 中不存在 → 恒 False。
    新实现：自动回退到 macd_hist / macd_signal → 判定真实生效。
    """
    hist = macd_hist_normalized(row)
    dif = macd_dif(row)
    sig = macd_signal(row)

    if hist is not None:
        if strict:
            if hist > 0:
                return True
        else:
            if hist >= 0:
                return True
    if dif is not None and sig is not None:
        return bool(dif > sig) if strict else bool(dif >= sig)
    if hist is not None:
        return False
    return None


def is_macd_bear(row: Any, *, strict: bool = True) -> Optional[bool]:
    """MACD 空头判定（与 is_macd_bull 互为反向）。缺失返回 None。"""
    bull = is_macd_bull(row, strict=strict)
    if bull is None:
        return None
    return not bull


def macd_is_dead_cross(row: Any) -> Optional[bool]:
    """
    动能死叉预警（口径与 strat_p4_tail.py 原逻辑一致）：
        DEA < 0 且 DIF < 前值  → 死叉发散
    缺失返回 None。
    """
    dif = macd_dif(row)
    sig = macd_signal(row)
    if dif is None or sig is None:
        return None
    return bool(sig < 0 and dif < 0)


# ============================ 向量化工具 ============================

def macd_hist_series(df: Any) -> Any:
    """
    对 DataFrame 批量取【1× 统一口径】柱体，返回 pandas Series。

    优先 macd_hist（1×）；其次 macd_bar/2（2× 折算）；
    最后用 macd - macd_signal 现算。全缺则为 NaN。
    """
    if df is None or pd is None:
        return None
    if not hasattr(df, "columns"):
        return None

    out = pd.Series(np.nan, index=df.index, dtype=float)

    if "macd_hist" in df.columns:
        out = pd.to_numeric(df["macd_hist"], errors="coerce")
    if "macd_bar" in df.columns:
        b = pd.to_numeric(df["macd_bar"], errors="coerce")
        out = out.fillna(b / _HIST_2X_SCALE)

    if out.isna().any():
        dif = None
        for c in MACD_DIF_COLS:
            if c in df.columns:
                dif = pd.to_numeric(df[c], errors="coerce")
                break
        sig = None
        for c in MACD_SIG_COLS:
            if c in df.columns:
                sig = pd.to_numeric(df[c], errors="coerce")
                break
        if dif is not None and sig is not None:
            out = out.fillna(dif - sig)

    return out


def macd_bull_series(df: Any) -> Any:
    """向量化多头判定：柱体 > 0（缺失为 False 但另可查 macd_hist_series 的 NaN）。"""
    s = macd_hist_series(df)
    if s is None:
        return None
    return s > 0


# ============================ 诊断 ============================

def diagnose_source(row: Any, label: str = "") -> Dict[str, Any]:
    """
    诊断一条记录的 MACD 数据来源与口径，用于排查"某处判定恒 False"。

    返回：
      {
        "label": str,
        "hist_raw": float|None,       # 原始柱体
        "hist_source": str,           # 'macd_hist' / 'macd_bar' / 'computed' / '(缺失)'
        "hist_scale": "1x" | "2x" | None,
        "hist_normalized": float|None, # 1× 统一口径
        "dif": float|None,
        "dea": float|None,
        "is_bull": bool|None,
        "note": str,
      }
    """
    hist_raw = macd_hist_raw(row)
    source = "(缺失)"
    scale = None
    if _row_get(row, "macd_hist") is not None:
        source, scale = "macd_hist", "1x"
    elif _row_get(row, "macd_bar") is not None:
        source, scale = "macd_bar", "2x"

    hist_n = macd_hist_normalized(row)
    dif = macd_dif(row)
    sig = macd_signal(row)
    bull = is_macd_bull(row)

    note = ""
    if hist_n is None and dif is None and sig is None:
        note = "MACD 数据全缺失 → 判定不可判定（返回 None）"
    elif source == "(缺失)":
        note = "无柱体列，用 (macd - macd_signal) 现算"

    return {
        "label": label,
        "hist_raw": hist_raw,
        "hist_source": source,
        "hist_scale": scale,
        "hist_normalized": hist_n,
        "dif": dif,
        "dea": sig,
        "is_bull": bull,
        "note": note,
    }


__all__ = [
    "MACD_HIST_COLS",
    "MACD_DIF_COLS",
    "MACD_SIG_COLS",
    "macd_hist_raw",
    "macd_hist_normalized",
    "macd_dif",
    "macd_signal",
    "is_macd_bull",
    "is_macd_bear",
    "macd_is_dead_cross",
    "macd_hist_series",
    "macd_bull_series",
    "diagnose_source",
]
