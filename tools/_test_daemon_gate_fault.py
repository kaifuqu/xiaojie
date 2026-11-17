# -*- coding: utf-8 -*-
"""V30.4 daemon 派生列闸故障注入（不打真实 Tushare，纯内存 mock）"""
import sys, types
sys.path.insert(0, r"D:\xiaojie")
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
from core._pyarrow_compat import disable_pyarrow
disable_pyarrow()

P=F=0
def ck(name, cond, extra=""):
    global P,F
    if cond: P+=1; print("  [PASS] %s %s" % (name, extra))
    else: F+=1; print("  [FAIL] %s %s" % (name, extra))

import auto_sniper_daemon as d

# --- 桩掉一切外部副作用
CALLS = {"sync": 0, "patch": [], "alert": []}
def fake_sync_core():
    CALLS["sync"] += 1
    return True
def fake_patch(**kw):
    CALLS["patch"].append(dict(kw))
    return True
def fake_alert(title, body="", **kw):
    CALLS["alert"].append(title)
    return True
def fake_index(): return None
def fake_val(): return None
def fake_cal(): return None
def fake_weekly(): return None
def fake_trading_day(): return True
def fake_skip_maint(reason=""): return False
def fake_skip_cruise(reason=""): return False
def fake_can_skip(): return False
def fake_now():
    import datetime
    return datetime.datetime(2026, 10, 18, 19, 45, 0)

d._maintenance_mode_skip = fake_skip_maint
d._daemon_cruise_off_skip = fake_skip_cruise
d._daemon_is_trading_day_safe = fake_trading_day
d._evening_incremental_sync_can_skip = fake_can_skip
d._sync_daily_incremental_core = fake_sync_core
d._write_pipeline_state_patch = fake_patch
d._notify_daemon_alert = fake_alert
d._sync_benchmark_indices_after_daily = fake_index
d._sync_index_valuation_after_daily = fake_val
d._sync_trade_calendar_after_daily = fake_cal
d._refresh_weekly_data_after_daily_sync = fake_weekly
d._now_bj = fake_now
# 【2026-11 适配】11-13 基线新增「防假成功」闸：写 last_sync_ok 前必须核实
# daily_data 真有该业务日数据（_daily_data_covers_bj_date）。本测试是纯内存
# mock，不碰真实库，对校验函数一并桩掉；并在末尾补一个「闸拦截」的正向用例。
_cover_flag = {"ok": True}
d._daily_data_covers_bj_date = lambda d8: _cover_flag["ok"]

import core.process_orchestrator  # noqa
import core.paper_trading.daemon_hooks as pth
real_init = pth.paper_init_job
pth.paper_init_job = lambda *a, **k: {"ok": True}

print("== 场景1：派生列同步成功 -> 放行 P1/P5 ==")
CALLS["sync"]=0; CALLS["patch"].clear(); CALLS["alert"].clear()
d._sync_daily_derived_features_after_daily = lambda: True
d._evening_sync_only_pipeline()
ck("日线同步被调用", CALLS["sync"]==1, "sync=%d"%CALLS["sync"])
ok_writes = [p for p in CALLS["patch"] if p.get("last_sync_ok_bj_date")]
ck("写了 last_sync_ok_bj_date", len(ok_writes)==1, str(ok_writes))
ck("清了 last_sync_fail", ok_writes and ok_writes[0].get("last_sync_fail_bj_date")=="")
ck("未发派生列告警", not any("派生列" in a for a in CALLS["alert"]))

print("== 场景2：派生列同步失败 -> 阻断 P1/P5 ==")
CALLS["sync"]=0; CALLS["patch"].clear(); CALLS["alert"].clear()
d._sync_daily_derived_features_after_daily = lambda: False
d._evening_sync_only_pipeline()
ok_writes = [p for p in CALLS["patch"] if p.get("last_sync_ok_bj_date")]
fail_writes = [p for p in CALLS["patch"] if p.get("last_sync_fail_bj_date")]
ck("日线同步仍被调用", CALLS["sync"]==1, "sync=%d"%CALLS["sync"])
ck("未写 last_sync_ok_bj_date", len(ok_writes)==0, str(CALLS["patch"]))
ck("写了 last_sync_fail_bj_date", len(fail_writes)==1 and fail_writes[0]["last_sync_fail_bj_date"]=="20261018", str(fail_writes))
ck("发出派生列告警", any("派生列" in a for a in CALLS["alert"]), str(CALLS["alert"]))

print("== 场景3：跳过拉取分支 + 派生列失败 -> 同样阻断 ==")
CALLS["sync"]=0; CALLS["patch"].clear(); CALLS["alert"].clear()
d._evening_incremental_sync_can_skip = lambda: True
d._evening_sync_only_pipeline()
ok_writes = [p for p in CALLS["patch"] if p.get("last_sync_ok_bj_date")]
fail_writes = [p for p in CALLS["patch"] if p.get("last_sync_fail_bj_date")]
ck("跳过拉取时不再调日线同步", CALLS["sync"]==0)
ck("未写 last_sync_ok_bj_date", len(ok_writes)==0, str(CALLS["patch"]))
ck("写了 last_sync_fail_bj_date", len(fail_writes)==1, str(fail_writes))
ck("发出派生列告警", any("派生列" in a for a in CALLS["alert"]), str(CALLS["alert"]))

print("== 场景4：跳过拉取分支 + 派生列成功 -> 放行 ==")
CALLS["sync"]=0; CALLS["patch"].clear(); CALLS["alert"].clear()
d._sync_daily_derived_features_after_daily = lambda: True
d._evening_sync_only_pipeline()
ok_writes = [p for p in CALLS["patch"] if p.get("last_sync_ok_bj_date")]
ck("写了 last_sync_ok_bj_date", len(ok_writes)==1 and ok_writes[0]["last_sync_ok_bj_date"]=="20261018", str(CALLS["patch"]))

print("== 场景5：防假成功闸——库内无该日数据 -> 拒绝写 last_sync_ok ==")
CALLS["sync"]=0; CALLS["patch"].clear(); CALLS["alert"].clear()
_cover_flag["ok"] = False   # 模拟「同步报成功但 daily_data 无该日数据」
d._evening_incremental_sync_can_skip = lambda: False   # 走完整拉取分支
d._evening_sync_only_pipeline()
ok_writes = [p for p in CALLS["patch"] if p.get("last_sync_ok_bj_date")]
fail_writes = [p for p in CALLS["patch"] if p.get("last_sync_fail_bj_date")=="20261018"]
ck("防假成功闸拒绝写 last_sync_ok（空写入）", all(not p.get("last_sync_ok_bj_date") for p in CALLS["patch"]) and len(ok_writes)==0, str(CALLS["patch"]))
ck("改写了 last_sync_fail_bj_date", len(fail_writes)>=1, str(fail_writes))
ck("发出「日线未真正落库」告警", any("未真正落库" in a for a in CALLS["alert"]), str(CALLS["alert"]))
_cover_flag["ok"] = True    # 还原

pth.paper_init_job = real_init
print("")
print("PASS=%d FAIL=%d" % (P,F))
sys.exit(1 if F else 0)
