# -*- coding: utf-8 -*-
"""倍量阳线快车道 单元自测（不连网络/不写库）
复刻 single_stock.py L496 后的判定逻辑做独立验证（生产代码不受影响）。
"""
import os
import sys

# 【P0-20261018 V30.5】本脚本原先必须外部设置 PYTHONPATH 才能 import core.*，
# 双击直接运行会 ImportError。这里自解析仓库根，保证 `python tools\test_p1_vol_sun.py`
# 在任何工作目录下都能独立跑通。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import pandas as pd
from core.pool_manager.single_stock import _safe_float

VOL_MULT = 3.0  # 与生产代码对齐：今日量 / 昨日量


def make_df(vol_today, vol_yest, close_today, open_today, vr=2.5):
    """构造一个最小可用 df + curr dict（与 single_stock 内 L496 处字段对齐）"""
    df = pd.DataFrame({
        "vol":   [vol_yest, vol_today],
        "close": [close_today*0.99, close_today],
        "open":  [close_today*0.99, open_today],
    })
    curr = {
        "close": close_today, "open": open_today, "vol": vol_today,
        "vol_ratio": vr, "ma20": 10.0, "ma60": 9.0,
    }
    return df, curr


def is_triggered(df, curr, circ_yi=150.0):
    """复刻 single_stock 内的判定（独立可测，不动生产代码）"""
    try:
        c = _safe_float(curr.get('close', 0))
        o = _safe_float(curr.get('open', 0))
        vt = _safe_float(curr.get('vol', 0))
        vy = _safe_float(df.iloc[-2].get('vol', 0)) if len(df) >= 2 else 0
        vr = _safe_float(curr.get('vol_ratio', 0))
        return (
            (o > 0 and c > o)
            and vt > 0 and vy > 0
            and vt >= vy * VOL_MULT
            and vr >= 2.0
            and circ_yi >= 120.0
        )
    except Exception:
        return False


def t1():
    """场景1：3 倍倍量阳线 + 150 亿 → 触发"""
    df, curr = make_df(vol_today=300, vol_yest=80, close_today=11, open_today=10, vr=2.5)
    assert is_triggered(df, curr, circ_yi=150), "3倍量阳线+150亿 应触发"
    print("[OK] T1: 3倍量阳线+150亿 -> 触发")


def t2():
    """场景2：倍量阴线 → 不触发"""
    df, curr = make_df(vol_today=300, vol_yest=80, close_today=9, open_today=10, vr=2.5)
    assert not is_triggered(df, curr, circ_yi=150), "倍量阴线 不应触发"
    print("[OK] T2: 倍量阴线 -> 不触发")


def t3():
    """场景3：倍量阳线 + 50 亿 → 不触发（市值闸）"""
    df, curr = make_df(vol_today=300, vol_yest=80, close_today=11, open_today=10, vr=2.5)
    assert not is_triggered(df, curr, circ_yi=50), "倍量阳线+50亿 不应触发"
    print("[OK] T3: 倍量阳线+50亿 -> 不触发（市值闸）")


def t4():
    """场景4：2.5 倍量阳线（不达 3 倍）→ 不触发"""
    df, curr = make_df(vol_today=200, vol_yest=80, close_today=11, open_today=10, vr=1.5)
    assert not is_triggered(df, curr, circ_yi=150), "2.5倍量阳线 不应触发(要 3 倍)"
    print("[OK] T4: 2.5倍量阳线 -> 不触发（不达 3 倍）")


def t5():
    """场景5：恰好 3.0 倍边界 → 触发"""
    df, curr = make_df(vol_today=240, vol_yest=80, close_today=11, open_today=10, vr=2.0)
    assert is_triggered(df, curr, circ_yi=120), "3.0倍量阳线 边界应触发"
    print("[OK] T5: 3.0倍量阳线 边界 -> 触发")


def t6():
    """场景6：量比 1.5（不达 2.0）→ 不触发"""
    df, curr = make_df(vol_today=300, vol_yest=80, close_today=11, open_today=10, vr=1.5)
    assert not is_triggered(df, curr, circ_yi=150), "量比1.5 不应触发"
    print("[OK] T6: 量比 1.5 -> 不触发（不达 2.0 冗余保险）")


if __name__ == "__main__":
    t1(); t2(); t3(); t4(); t5(); t6()
    print("\nALL PASSED")
