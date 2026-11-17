# -*- coding: utf-8 -*-
"""V30.10 全量回归验证。"""
import sys, importlib, os
sys.path.insert(0, r"d:\xiaojie")

FAILS = []

def chk(name, cond, extra=""):
    tag = "OK  " if cond else "FAIL"
    if not cond:
        FAILS.append(name)
    print("  [%s] %s %s" % (tag, name, extra))

print("=== 1) 新增/修改模块导入 ===")
for m in [
    "core.signal_ledger_bridge",
    "core.strategies.macd_unified",
    "core.strategy_effectiveness_snapshot",
    "core.push_threshold",
    "core.push_threshold_calibrator",
    "core.pool_signal_adapter_v28",
    "core.notification.gateway",
    "core.scan.intraday_snapshots",
    "core.strategies.strat_p2_auction",
    "core.strategies.strat_p3_intraday",
    "core.strategies.strat_p4_tail",
    "core.strategies.strat_p5_postmarket",
    "core.pool_manager.pool_builder",
    "core.pool_manager.single_stock",
    "core.pool_manager.thresholds",
    "core.strategies.p2_auction_screener",
    "core.strategies.p3_intraday_screener",
    "core.strategies.p4_tail_screener",
    "core.strategies.p5_postmarket_screener",
    "data.signal_logger",
    "data.signal_backtest",
    "data.fetcher._client",
]:
    try:
        importlib.import_module(m)
        chk("import %s" % m, True)
    except Exception as e:
        chk("import %s" % m, False, "-> %s: %s" % (type(e).__name__, str(e)[:120]))

print("\n=== 2) daemon 18:30 流水线的关键 import（原本 ImportError 的 P0） ===")
try:
    from core.strategy_effectiveness_snapshot import build_ui_snapshot
    chk("build_ui_snapshot 可导入", callable(build_ui_snapshot))
except Exception as e:
    chk("build_ui_snapshot 可导入", False, str(e)[:120])

print("\n=== 3) 生产代码里不再有死变量 _GLOBAL_DB_CONN ===")
import subprocess
r = subprocess.run(["rg", "-n", "_GLOBAL_DB_CONN", "core", "data", "ui", "auto_sniper_daemon.py",
                    "--glob", "!*.legacy.py", "--glob", "!*.md"],
                   cwd=r"d:\xiaojie", capture_output=True, text=True, encoding="utf-8", errors="replace")
out = r.stdout.strip()
# 只允许出现在注释里
code_lines = [l for l in out.splitlines()
              if l.strip() and not l.split(":", 2)[-1].strip().startswith("#")
              and "【" not in l and "globals().get" not in l]
chk("生产代码无 _GLOBAL_DB_CONN 实际调用", not code_lines, str(code_lines[:3]))

print("\n=== 4) 硬编码 push_min_score 已清除 ===")
r2 = subprocess.run(["rg", "-n", "push_min_score=[0-9]", "core"],
                    cwd=r"d:\xiaojie", capture_output=True, text=True, encoding="utf-8", errors="replace")
chk("无硬编码 push_min_score 数字", not r2.stdout.strip(), r2.stdout.strip()[:200])

print("\n=== 5) MACD 旧写法（macd_diff/macd_dea 取值）已清除 ===")
r3 = subprocess.run(["rg", "-n", 'get\\("macd_diff"|get\\("macd_dea"', "core"],
                    cwd=r"d:\xiaojie", capture_output=True, text=True, encoding="utf-8", errors="replace")
chk("无 macd_diff/macd_dea 取值残留", not r3.stdout.strip(), r3.stdout.strip()[:200])

print("\n=== 6) 阈值统一解析器 ===")
from core.push_threshold import resolve_push_min_score, resolve_all, describe
print(describe())
chk("p5 阈值 = 70（与 config.yaml 一致）", resolve_push_min_score("p5") == 70.0)
chk("p3 阈值 = 50", resolve_push_min_score("p3") == 50.0)
chk("p4 阈值 = 55", resolve_push_min_score("p4") == 55.0)
chk("p2 阈值 = 50", resolve_push_min_score("p2") == 50.0)
chk("未知池有兜底", resolve_push_min_score("p9") == 50.0)

print("\n=== 7) MACD 统一判定 ===")
import pandas as pd
from core.strategies.macd_unified import is_macd_bull, macd_hist_normalized
row = pd.Series({"macd": 0.2, "macd_signal": 0.1, "macd_hist": 0.1})
chk("生产行判定为多头", is_macd_bull(row) is True)
chk("缺列返回 None（不可判定）", is_macd_bull(pd.Series({"close": 1.0})) is None)
row2 = pd.Series({"macd_bar": 0.10, "macd": 0.10, "macd_signal": 0.05})
chk("2x 尺度归一为 1x", abs(macd_hist_normalized(row2) - 0.05) < 1e-9)

print("\n=== 8) 常驻快照 API ===")
from core.strategy_effectiveness_snapshot import quick_effectiveness_snapshot
snap = quick_effectiveness_snapshot(days=120, use_cache=False)
chk("快照无错误", snap.get("error") is None, str(snap.get("error"))[:80])
chk("有战法榜", len(snap.get("ranking") or []) > 0)
chk("榜含超额维度", "avg_excess" in (snap["ranking"][0] if snap.get("ranking") else {}))

print("\n=== 9) 阈值标定器 ===")
from core.push_threshold_calibrator import calc_recommended_thresholds, apply_recommended_thresholds
cal = calc_recommended_thresholds(days=120, min_samples=8)
chk("标定无错误", cal.get("error") is None, str(cal.get("error"))[:80])
chk("有推荐结果", len(cal.get("recommendations") or {}) > 0)
dry = apply_recommended_thresholds(cal, dry_run=True)
chk("dry_run 不落盘", dry.get("applied") is False)

print("\n=== 10) 账本桥接 ===")
from core.signal_ledger_bridge import with_exchange_suffix, bare_code
chk("000001 -> 000001.SZ", with_exchange_suffix("000001") == "000001.SZ")
chk("600000 -> 600000.SH", with_exchange_suffix("600000") == "600000.SH")
chk("920296 -> 920296.BJ", with_exchange_suffix("920296") == "920296.BJ")
chk("bare_code 去后缀", bare_code("000001.SZ") == "000001")

print("\n=== 11) push_dedup 修复（历史自我挤爆） ===")
import tempfile, shutil, os as _os
from core import push_dedup as _pd
_orig_hist = _pd.HISTORY_FILE
_tmpdir = tempfile.mkdtemp(prefix="v3010_pd_")
_pd.HISTORY_FILE = _os.path.join(_tmpdir, "push_history.json")
try:
    # 同日同池反复写入 30 次 —— 旧实现会产生 20 条重复并挤掉真实历史
    for _i in range(30):
        _pd.record_push("600000.SH", "p1", 80.0, trade_date="20261020")
    _h = _pd._load_history()
    _arr = _h.get("600000.SH", [])
    chk("同日同池不重复堆积", len(_arr) == 1, "实际 %d 条" % len(_arr))
    chk("分数被保留", abs(float(_arr[0].get("score", 0)) - 80.0) < 1e-6)

    # 不同交易日应各自成条，且不被上限挤掉
    for _d in ("20261015", "20261016", "20261017", "20261020", "20261021"):
        _pd.record_push("600000.SH", "p1", 70.0, trade_date=_d)
    _arr = _pd._load_history().get("600000.SH", [])
    _dates = sorted({str(x.get("date")) for x in _arr})
    chk("5 个交易日全部保留", len(_dates) == 5, str(_dates))
    chk("交易日按传入值落库", "20261015" in _dates, str(_dates))
finally:
    _pd.HISTORY_FILE = _orig_hist
    shutil.rmtree(_tmpdir, ignore_errors=True)

print("\n=== 12) 账本对账（池口径错位） ===")
import duckdb as _dk
_con = _dk.connect(_os.path.join(r"d:\xiaojie", "data", "quant_data.duckdb"), read_only=True)
from core.signal_ledger_bridge import reconcile_push_history
_rep = reconcile_push_history(_con, apply=False)
_tot = _rep.get("log_total") or 0
_m = (_rep.get("matched") or 0) + (_rep.get("matched_cross_pool") or 0)
chk("对账覆盖率 >= 50%", _tot == 0 or (_m / float(_tot)) >= 0.5,
    "匹配 %d / 总 %d" % (_m, _tot))
_con.close()

print("\n" + "=" * 64)
if FAILS:
    print("失败项 %d 个:" % len(FAILS))
    for f in FAILS:
        print("  - %s" % f)
    sys.exit(1)
print("全部通过 ✅")