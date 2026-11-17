"""端到端复刻生产代码里的判定块（验证 6 个场景）"""
import sys
sys.path.insert(0, r"d:\xiaojie")
import pandas as pd
from core.pool_manager.single_stock import _safe_float

def judge(df, curr, close_today, circ_mv_yi_early):
    score_details = {}
    used_fast_track = False
    try:
        _vs_close = _safe_float(curr.get('close', close_today))
        _vs_open  = _safe_float(curr.get('open', 0.0))
        _vs_vol_t = _safe_float(curr.get('vol', 0.0))
        _vs_vol_y = _safe_float(df.iloc[-2].get('vol', 0.0)) if len(df) >= 2 else 0.0
        _vs_vr    = _safe_float(curr.get('vol_ratio', 0.0))
        _vs_circ  = float(circ_mv_yi_early) if circ_mv_yi_early is not None else 0.0
        if (
            _vs_open > 0 and _vs_close > _vs_open
            and _vs_vol_t > 0 and _vs_vol_y > 0
            and _vs_vol_t >= _vs_vol_y * 3.0
            and _vs_vr >= 2.0
            and _vs_circ >= 120.0
        ):
            used_fast_track = True
            score_details['倍量阳线机构信号'] = f"今日量{_vs_vol_t:.0f}>=昨日{_vs_vol_y:.0f}x3"
    except Exception as e:
        print('异常:', e)
    return used_fast_track, score_details

def make(vt, vy, c, o, vr=2.5):
    df = pd.DataFrame({'vol':[vy,vt],'close':[c*0.99,c],'open':[c*0.99,o]})
    curr = {'close':c,'open':o,'vol':vt,'vol_ratio':vr}
    return df, curr

cases = [
    ('3x+阳+120亿',  300, 80, 11, 10, 2.5, 150, True),
    ('阴线',          300, 80,  9, 10, 2.5, 150, False),
    ('3x+阳+50亿',   300, 80, 11, 10, 2.5,  50, False),
    ('2.5x 不达3',   200, 80, 11, 10, 1.5, 150, False),
    ('3x 边界',      240, 80, 11, 10, 2.0, 120, True),
    ('量比1.5 不达2', 300, 80, 11, 10, 1.5, 150, False),
]
fails = 0
for name, vt, vy, c, o, vr, circ, expect in cases:
    df, curr = make(vt, vy, c, o, vr)
    ft, sd = judge(df, curr, c, circ)
    ok = ft == expect
    tag = '[OK]  ' if ok else '[FAIL]'
    print(f"  {tag} {name}: fast_track={ft} expect={expect}")
    if not ok: fails += 1
print()
print('ALL PASSED' if fails == 0 else f'FAILED {fails}')
