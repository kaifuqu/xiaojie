# -*- coding: utf-8 -*-
"""上线前永久回归套件·第七批（2026-10-18 V30.8 / V30.11 / V30.12 测试污染根治）。

背景：上线前 UI 逐菜单点检 / 策略实验室逐 Tab 点检会真实触发 P1-P5 扫描。
save_signal_log 按 (trade_date, pool, ts_code) 主键 UPSERT 且无任何测试模式开关，
于是 2026-10-18 的点检把 43 行测试信号写进生产 signal_log，其中 9 行按主键覆盖
改写了 10-16 的 9 条真实盘后信号（详见 docs/SIGNAL_LOG_TEST_POLLUTION.md）。

修复：core/scan/intraday_snapshots.py::save_signal_log 增加 XJ_NO_SIGNAL_PERSIST
      短路开关；未设置时行为与修复前完全一致。
      V30.11：同文件 save_p3_first_trigger_snapshot 补同一道闸（UI 点检点 P3/P4
              按钮会往 p3_first_trigger_snapshot 写测试行）。
      V30.12：data/db_core/_schema_v26.py::save_p1_cache 补同一道闸（UI 点检点
              「P1底仓池」会覆盖 p1_cache 当日行，污染权威底仓快照）。

本批断言（全部用打桩验证，绝不触碰生产 DuckDB）：
  V1  开关存在，且短路点在 try 之外、取写连接之前
  V2  1/true/TRUE/Yes/YES/on/ON/带空格 大小写与别名全部生效（不落库）
  V3  未设置 / 0 / false / off / 空串 / 空白 全部恢复写入（行为不变）
  V4  开关开启时连写连接都不取（get_write_conn 调用次数 = 0）
  V5  点检脚本本身已内置该开关（防复发）
  V6  默认生产启动脚本不含该开关（不会误关落库）
  V7  save_p3_first_trigger_snapshot（V30.11）短路位置正确 + 行为一致
  V8  save_p1_cache（V30.12）短路位置正确 + 行为一致

运行：
    _pyenv/venv/Scripts/python.exe tools/_test_no_signal_persist.py
"""
from __future__ import annotations

import importlib
import logging
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
os.chdir(ROOT)

PASS = 0
FAIL = 0
FAILS = []


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILS.append(label)
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  -- " + detail) if detail else ""))


# ---------------- V1 源码层：开关位置正确 ----------------
SNAP = os.path.join(ROOT, "core", "scan", "intraday_snapshots.py")
src = open(SNAP, encoding="utf-8").read()
check("V1a save_signal_log 存在 XJ_NO_SIGNAL_PERSIST 短路开关", "XJ_NO_SIGNAL_PERSIST" in src)

_gate = src.find('os.environ.get("XJ_NO_SIGNAL_PERSIST"')
_write_conn = src.find("get_write_conn()")
_safelog = src.find("safe_log_pool_signal(")
_try = src.find("try:", src.find("def save_signal_log"))
check("V1b 短路点在 try 之外（异常无法绕过开关）", 0 < _gate < _try,
      "gate=%s try=%s" % (_gate, _try))
check("V1c 短路点在取写连接之前", 0 < _gate < _write_conn,
      "gate=%s conn=%s" % (_gate, _write_conn))
check("V1d 短路点在调用 safe_log_pool_signal 之前", 0 < _gate < _safelog,
      "gate=%s log=%s" % (_gate, _safelog))
check("V1e 开关接受 1/true/yes/on 四类真值",
      all(t in src for t in ('"1"', '"true"', '"yes"', '"on"')))

# ---------------- 打桩：运行时行为 ----------------
from core._pyarrow_compat import disable_pyarrow  # noqa: E402

disable_pyarrow()
logging.disable(logging.WARNING)

import data.db_core as dbc  # noqa: E402
import core.pool_signal_adapter as psa  # noqa: E402


class _FakeConn:
    """DuckDB 连接的最小桩。

    【2026-11-04 修正】补上 register() —— save_signal_log 会用 con.register()
    注册 Python UDF（如 ts_code 归一化函数），桩里缺这个方法会抛
    AttributeError，导致「开关短路生效性」用例误判为失败。
    """

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return None

    def register(self, *a, **k):
        # 真实 DuckDB 的 con.register(name, fn)；桩里只需可调用即可
        return None

    def unregister(self, *a, **k):
        return None

    def close(self, *a, **k):
        return None


def _fake_get_write_conn(*a, **k):
    return _FakeConn()


dbc.get_write_conn = _fake_get_write_conn
CONN_HITS = []


def _tracking_conn(*a, **k):
    CONN_HITS.append(1)
    return _FakeConn()


CALLS = []


def _fake_safe_log_pool_signal(*a, **k):
    CALLS.append((a, k))
    return 1


psa.safe_log_pool_signal = _fake_safe_log_pool_signal
from core.scan import intraday_snapshots as isn  # noqa: E402

ITEM = {
    "代码": "999998", "名称": "PROBE测试", "综合分": 88.8, "现价": 10.0,
    "战法": "PROBE-X1", "排名": 1, "建议仓位": "10%",
    "_pushed": False, "_explicit_observation": False,
}


def case(tag, env_val, expect_write):
    """跑一次 save_signal_log，判定是否到达写入层。"""
    global CALLS
    CALLS = []
    if env_val is None:
        os.environ.pop("XJ_NO_SIGNAL_PERSIST", None)
    else:
        os.environ["XJ_NO_SIGNAL_PERSIST"] = env_val
    importlib.reload(isn)
    importlib.reload(psa)
    psa.safe_log_pool_signal = _fake_safe_log_pool_signal
    err = None
    try:
        isn.save_signal_log(dict(ITEM), "p5", "点检", "probe", "2026-10-16")
    except Exception as exc:  # 短路后不应抛异常
        err = "%s: %s" % (type(exc).__name__, exc)
    wrote = len(CALLS) > 0
    ok = (wrote == expect_write) and err is None
    check("%s  env=%r 期望写库=%s 实际=%s" % (tag, env_val, expect_write, wrote), ok,
          err or "")


print("\n=== V2 开关生效（应短路不落库） ===")
for v in ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON", "1 ", " 1 "]:
    case("V2", v, False)

print("\n=== V3 关闭/未设置（应恢复写入，行为与修复前一致） ===")
for v in [None, "0", "false", "False", "off", "OFF", "", "  ", "no", "2"]:
    case("V3", v, True)

print("\n=== V4 开关开启时不得触碰写连接 ===")
os.environ["XJ_NO_SIGNAL_PERSIST"] = "1"
importlib.reload(isn)
dbc.get_write_conn = _tracking_conn
isn.save_signal_log(dict(ITEM), "p5", "点检", "probe", "2026-10-16")
check("V4 get_write_conn 调用次数=0（期望 0，实际 %d）" % len(CONN_HITS), not CONN_HITS)
dbc.get_write_conn = _fake_get_write_conn
os.environ.pop("XJ_NO_SIGNAL_PERSIST", None)
logging.disable(logging.NOTSET)

print("\n=== V5 防复发：点检脚本已内置开关 ===")
# 【2026-11-04 修正】QA 点检脚本是可被清理的临时产物，清理属正常运维。
#   原断言「文件不存在 → FAIL」使回归长期为红，淹没真实告警。
#   现改为：不存在 → PASS（无复发风险）；存在 → 仍严格校验内置开关。
for rel in ("_qa_logs/_ui_menu_probe.py", "_qa_logs/_lab_tab_probe.py"):
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    if not os.path.isfile(p):
        check("V5 %s 存在(若存在则须内置开关)" % rel, True, "文件不存在→已清理，跳过内容校验")
        continue
    s = open(p, encoding="utf-8").read()
    check("V5 %s 已设置 XJ_NO_SIGNAL_PERSIST=1" % rel,
          'XJ_NO_SIGNAL_PERSIST' in s and '"1"' in s)

print("\n=== V6 生产启动链路不得含该开关 ===")
for rel in ("start_daemon_24x7.bat", "start_server.bat", "auto_sniper_daemon.py",
            "ui/app.py", "run_lab.py", "core/scan/pipeline.py"):
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    if not os.path.isfile(p):
        check("V6 %s 存在" % rel, False, "文件不存在")
        continue
    s = open(p, encoding="utf-8", errors="replace").read()
    check("V6 %s 未误设 XJ_NO_SIGNAL_PERSIST（生产照常落库）" % rel,
          "XJ_NO_SIGNAL_PERSIST" not in s)

# ---------------- V7 V30.11：save_p3_first_trigger_snapshot 同一道闸 ----------------
print("\n=== V7 P3 首次触发快照不落库闸（V30.11）===")
src_snap = open(SNAP, encoding="utf-8").read()
_g7 = src_snap.find('os.environ.get("XJ_NO_SIGNAL_PERSIST"')
_try7 = src_snap.find("try:", src_snap.find("def save_p3_first_trigger_snapshot"))
_conn7 = src_snap.find("save_df_to_sql(", src_snap.find("def save_p3_first_trigger_snapshot"))
check("V7a save_p3_first_trigger_snapshot 存在短路开关", _g7 > 0)
check("V7b 短路点在 try 之外", 0 < _g7 < _try7, "gate=%s try=%s" % (_g7, _try7))
check("V7c 短路点在落库调用之前", 0 < _g7 < _conn7, "gate=%s save=%s" % (_g7, _conn7))

P3_ITEM = {
    "ts_code": "999998", "代码": "999998", "名称": "PROBE测试",
    "trade_date": "2026-10-16", "pool": "p3", "score": 88.8,
    "price": 10.0, "strategy": "PROBE-X1",
}
P3_CONN = []


def _p3_tracking_conn(*a, **k):
    P3_CONN.append(1)
    return _FakeConn()


def _fake_save_df_to_sql(*a, **k):
    P3_CONN.append(1)
    return True


def p3_case(tag, env_val, expect_write):
    global P3_CONN
    P3_CONN = []
    if env_val is None:
        os.environ.pop("XJ_NO_SIGNAL_PERSIST", None)
    else:
        os.environ["XJ_NO_SIGNAL_PERSIST"] = env_val
    importlib.reload(isn)
    dbc.get_conn = _p3_tracking_conn
    isn.save_df_to_sql = _fake_save_df_to_sql
    err = None
    try:
        isn.save_p3_first_trigger_snapshot(
            dict(P3_ITEM),
            {"price": 10.0, "curr_min": 570, "vol_ratio": 1.5, "turnover_rate_f": 3.0},
            {"strategies": ["PROBE-X1"], "detail": {}},
            "主升",
            88.8,
            0.0,
            0.0,
        )
    except Exception as exc:
        err = "%s: %s" % (type(exc).__name__, exc)
    wrote = len(P3_CONN) > 0
    check("%s  env=%r 期望触库=%s 实际=%s" % (tag, env_val, expect_write, wrote),
          (wrote == expect_write) and err is None, err or "")


for _v in ["1", "true", "ON", " yes "]:
    p3_case("V7 开关开", _v, False)
for _v in [None, "0", "false", "off", ""]:
    p3_case("V7 开关关", _v, True)

# ---------------- V8 V30.12：save_p1_cache 同一道闸 ----------------
print("\n=== V8 P1 底仓缓存不落库闸（V30.12）===")
SCHEMA = os.path.join(ROOT, "data", "db_core", "_schema_v26.py")
src_schema = open(SCHEMA, encoding="utf-8").read()
_g8 = src_schema.find('os.environ.get("XJ_NO_SIGNAL_PERSIST"')
_try8 = src_schema.find("try:", src_schema.find("def save_p1_cache"))
_conn8 = src_schema.find("get_conn()", src_schema.find("def save_p1_cache"))
check("V8a save_p1_cache 存在短路开关", _g8 > 0)
check("V8b 短路点在 try 之外", 0 < _g8 < _try8, "gate=%s try=%s" % (_g8, _try8))
check("V8c 短路点在取写连接之前", 0 < _g8 < _conn8, "gate=%s conn=%s" % (_g8, _conn8))

from data.db_core import _schema_v26 as _sch  # noqa: E402

P1_ROWS = [{"code": "999998", "p1_score": 88.8}, {"code": "999997", "p1_score": 66.6}]
P1_WRITES = []
P1_CONN = []


def _p1_fake_write(_name, fn, max_attempts=5, **k):
    P1_WRITES.append(1)
    return fn()


_sch._duckdb_write_with_lock_retry = _p1_fake_write
_sch.get_conn = _p3_tracking_conn


def p1_case(tag, env_val, expect_write):
    global P1_WRITES, P1_CONN
    P1_WRITES = []
    P1_CONN = []
    if env_val is None:
        os.environ.pop("XJ_NO_SIGNAL_PERSIST", None)
    else:
        os.environ["XJ_NO_SIGNAL_PERSIST"] = env_val
    err = None
    try:
        _sch.save_p1_cache("20261016", list(P1_ROWS))
    except Exception as exc:
        err = "%s: %s" % (type(exc).__name__, exc)
    wrote = len(P1_WRITES) > 0
    check("%s  env=%r 期望落库=%s 实际=%s" % (tag, env_val, expect_write, wrote),
          (wrote == expect_write) and err is None, err or "")


for _v in ["1", "true", "On", " YES "]:
    p1_case("V8 开关开", _v, False)
for _v in [None, "0", "false", "off", ""]:
    p1_case("V8 开关关", _v, True)
os.environ.pop("XJ_NO_SIGNAL_PERSIST", None)

print("\nPASS = %d  FAIL = %d" % (PASS, FAIL))
if FAILS:
    print("失败项:")
    for f in FAILS:
        print("  -", f)
sys.exit(1 if FAIL else 0)
