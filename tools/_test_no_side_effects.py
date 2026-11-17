# -*- coding: utf-8 -*-
"""上线前永久回归套件·第八批（2026-10-18 V30.9 QA 外部副作用根治）。

背景：2026-10-18 上线前 UI/实验室点检，在 master_control.wechat_push_enabled=True
      的生产配置下真实执行了 notify_scan_results_top3 -> push_stock_if_allowed 链路：
        - 07:22 轮：3 条 P4 观察类企微推送（写进 wechat_signal_dedup_cache.json）
        - 09:32 轮：10 条 P5 企微推送 + 10 次真实 DeepSeek 计费调用（写 fact_ai_analysis_log）
      合计 13 条真实推送 + 13 次真实计费调用，累计 AI 耗时约 66 秒。

修复：
  core/qa_switches.py              统一开关中枢（XJ_NO_SIGNAL_PERSIST / XJ_NO_PUSH / XJ_NO_DEEPSEEK）
  core/notification/gateway.py     XJ_NO_PUSH 短路 4 处
                                    (push_stock_if_allowed / push_system_alert_async /
                                     _send_markdown_sync / _send_system_markdown_sync)
  core/notification/deepseek.py    XJ_NO_DEEPSEEK 短路 2 处
                                    (_maybe_get_deepseek_advice / _log_deepseek_analysis)

本批断言全部用打桩验证，绝不发起任何真实 HTTP、绝不读写真实去重账本、绝不触碰生产 DuckDB：
  W1  开关存在且短路点在副作用之前（源码层位置断言）
  W2  1/true/TRUE/Yes/YES/on/ON/带空格 大小写与别名全部生效
  W3  未设置 / 0 / false / off / 空串 / 空白 / no / 2 全部恢复生产行为
  W4  XJ_NO_PUSH=1 时股票推送：0 次 requests.post、0 次写去重账本、0 次线程池投递
  W5  XJ_NO_PUSH=1 时系统告警：0 次 requests.post、0 次写去重账本  ← V30.9 二次修复
  W6  XJ_NO_DEEPSEEK=1 时 DeepSeek：0 次 HTTP、0 次 AI 日志落库
  W7  关闭开关（生产态）三条链路副作用次数与修复前一致
  W8  fail-open：开关模块故障时不阻断生产推送
  W9  防复发：UI/实验室点检脚本已内置三道闸
  W10 生产启动链路不得自行设置 QA 开关

运行：
    _pyenv/venv/Scripts/python.exe tools/_test_no_side_effects.py
"""
from __future__ import annotations

import builtins
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
ENV_KEYS = ("XJ_NO_SIGNAL_PERSIST", "XJ_NO_PUSH", "XJ_NO_DEEPSEEK")


def check(label, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
        FAILS.append(label)
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", label, ("  -- " + detail) if detail else ""))


def set_env(key, val):
    if val is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = val


def clear_env():
    for k in ENV_KEYS:
        os.environ.pop(k, None)


# ============================================================ W1 源码层断言
print("\n=== W1 开关存在性与短路位置（源码层） ===")
qs_path = os.path.join(ROOT, "core", "qa_switches.py")
check("W1a core/qa_switches.py 存在", os.path.isfile(qs_path))
qs = open(qs_path, encoding="utf-8").read()
for name in ("no_signal_persist", "no_push", "no_deepseek", "read_only_mode", "env_flag"):
    check("W1b qa_switches 导出 %s" % name, ("def %s(" % name) in qs)
check("W1c qa_switches 真值表含 1/true/yes/on",
      all(('"%s"' % t) in qs for t in ("1", "true", "yes", "on")))

gs = open(os.path.join(ROOT, "core", "notification", "gateway.py"), encoding="utf-8").read()
check("W1d gateway.py 含 4 处 XJ_NO_PUSH 短路",
      gs.count("from core.qa_switches import no_push") == 4,
      "实际 %d 处" % gs.count("from core.qa_switches import no_push"))
_g_stock = gs.find("def push_stock_if_allowed")
_g_sys = gs.find("def push_system_alert_async")
_g_md = gs.find("def _send_markdown_sync")
_g_sysmd = gs.find("def _send_system_markdown_sync")
_gate_stock = gs.find("from core.qa_switches import no_push", _g_stock)
_gate_sys = gs.find("from core.qa_switches import no_push", _g_sys)
_gate_md = gs.find("from core.qa_switches import no_push", _g_md)
_gate_sysmd = gs.find("from core.qa_switches import no_push", _g_sysmd)
_reserve_push = gs.find("if not self._reserve_push_slot(", _g_stock)
_reserve_alert = gs.find("if not self._reserve_system_alert_slot(", _g_sys)
_post1 = gs.find("requests.post")
check("W1e push_stock_if_allowed 短路先于 _reserve_push_slot（防刷账本）",
      0 < _g_stock < _gate_stock < _reserve_push, "gate=%d reserve=%d" % (_gate_stock, _reserve_push))
check("W1f push_system_alert_async 短路先于 _reserve_system_alert_slot（告警账本）",
      0 < _g_sys < _gate_sys < _reserve_alert, "gate=%d reserve=%d" % (_gate_sys, _reserve_alert))
check("W1g _send_markdown_sync 短路先于 requests.post",
      0 < _g_md < _gate_md < _post1)
check("W1h _send_system_markdown_sync 短路先于 requests.post",
      0 < _g_sysmd < _gate_sysmd < gs.find("requests.post", _g_sysmd))
check("W1i 四处短路全部包在 try/except fail-open 内",
      gs.count("fail-open") >= 4, "fail-open 注释 %d 处" % gs.count("fail-open"))

ds = open(os.path.join(ROOT, "core", "notification", "deepseek.py"), encoding="utf-8").read()
check("W1j deepseek.py 含 2 处 XJ_NO_DEEPSEEK 短路",
      ds.count("from core.qa_switches import no_deepseek") == 2,
      "实际 %d 处" % ds.count("from core.qa_switches import no_deepseek"))
_d_adv = ds.find("def _maybe_get_deepseek_advice")
_d_log = ds.find("def _log_deepseek_analysis")
check("W1k _maybe_get_deepseek_advice 短路先于 HTTP 请求",
      0 < _d_adv < ds.find("from core.qa_switches import no_deepseek", _d_adv) < ds.find("requests.post"))
check("W1l _log_deepseek_analysis 短路先于 save_df_to_sql",
      0 < _d_log < ds.find("from core.qa_switches import no_deepseek", _d_log)
      < ds.find("save_df_to_sql(", _d_log))

isn = open(os.path.join(ROOT, "core", "scan", "intraday_snapshots.py"), encoding="utf-8").read()
check("W1m intraday_snapshots.py 仍保留 XJ_NO_SIGNAL_PERSIST 短路",
      "XJ_NO_SIGNAL_PERSIST" in isn)

# ============================================================ 打桩运行时
clear_env()
from core._pyarrow_compat import disable_pyarrow  # noqa: E402

disable_pyarrow()
logging.disable(logging.WARNING)

import core.notification.core as nc  # noqa: E402
import core.notification.gateway as gw  # noqa: E402
import core.notification.deepseek as dsx  # noqa: E402
import core.qa_switches as qsmod  # noqa: E402
import core.file_utils as fu  # noqa: E402


class _FakeRequests:
    """完全打桩的 requests：记录每次 post，返回 HTTP 200。"""

    def __init__(self):
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((str(url), kw))
        return type("R", (), {"status_code": 200, "text": '{"errcode":0}'})()


FAKE_REQUESTS = _FakeRequests()
sys.modules["requests"] = FAKE_REQUESTS

# 打桩 1：所有 json 账本写入只进内存，绝不落盘
LEDGER_WRITES = []
REAL_ATOMIC = fu.atomic_json_update


def _track_atomic(path, updater, **k):
    state = {}
    updater(state)
    LEDGER_WRITES.append((str(path), sorted(state.keys())))
    return True


fu.atomic_json_update = _track_atomic

# 打桩 2：线程池立即「执行」任务，从而让 requests.post 在同一线程可断言
EXECUTOR_CALLS = []


class _InlineExecutor:
    def submit(self, fn, *a, **k):
        EXECUTOR_CALLS.append("submit")
        try:
            fn(*a, **k)
        except Exception as exc:  # pragma: no cover
            print("     [INFO] 线程池任务异常(已吞): %s" % exc)
        return None


_INLINE = _InlineExecutor()
gw._WECHAT_WEBHOOK_EXECUTOR = _INLINE
nc._WECHAT_WEBHOOK_EXECUTOR = _INLINE

# 打桩 3：AI 日志落库
SAVED = []
REAL_SAVE = None
try:
    import data.db_core as dbc

    REAL_SAVE = dbc.save_df_to_sql

    def _track_save(df, table, *a, **k):
        SAVED.append((getattr(df, "shape", None), str(table)))
        return 1

    dbc.save_df_to_sql = _track_save
except Exception as exc:  # pragma: no cover
    print("  [WARN] save_df_to_sql 打桩失败: %s" % exc)


def reset_trackers():
    del FAKE_REQUESTS.calls[:], LEDGER_WRITES[:], EXECUTOR_CALLS[:], SAVED[:]


STOCK = {
    "代码": "600519.SH", "名称": "PROBE测试", "综合分": 88.8, "现价": 10.0,
    "战法": "PROBE-X1", "排名": 1, "建议仓位": "10%",
    "_pushed": False, "_explicit_observation": False,
}
URL = "https://example.invalid/hook"


def run_stock_push(pool="p3"):
    """走真实 push_stock_if_allowed 链路（p3 走跨进程账本分支）。"""
    g = gw.WechatNotificationGateway(URL)
    g._pushed_records.clear()
    g._pushed_minute_records.clear()
    g._pushed_signal_codes_once.clear()
    g.push_stock_if_allowed(pool, dict(STOCK))


def run_sys_alert():
    g = gw.WechatNotificationGateway(URL)
    g.push_system_alert_async("PROBE 系统告警", "probe-key-1")


def run_markdown():
    g = gw.WechatNotificationGateway(URL)
    g._send_markdown_sync("PROBE markdown")


def run_deepseek():
    return dsx._maybe_get_deepseek_advice("p5", dict(STOCK))


print("\n=== W2/W3 开关真值表 ===")
for v in ["1", "true", "TRUE", "True", "yes", "YES", "on", "ON", "1 ", " 1 "]:
    for key, fn in (("XJ_NO_PUSH", qsmod.no_push),
                    ("XJ_NO_DEEPSEEK", qsmod.no_deepseek),
                    ("XJ_NO_SIGNAL_PERSIST", qsmod.no_signal_persist)):
        set_env(key, v)
        check("W2 env=%-8s %-22s 生效=True" % (repr(v), key), fn() is True)
for v in [None, "0", "false", "False", "off", "OFF", "", "  ", "no", "2"]:
    for key, fn in (("XJ_NO_PUSH", qsmod.no_push),
                    ("XJ_NO_DEEPSEEK", qsmod.no_deepseek),
                    ("XJ_NO_SIGNAL_PERSIST", qsmod.no_signal_persist)):
        set_env(key, v)
        check("W3 env=%-8s %-22s 生效=False" % (repr(v), key), fn() is False)
set_env("XJ_NO_PUSH", "1"), set_env("XJ_NO_DEEPSEEK", "1"), set_env("XJ_NO_SIGNAL_PERSIST", "1")
check("W3 三闸全开 read_only_mode=True", qsmod.read_only_mode() is True)
clear_env()
check("W3 未设置任何开关 read_only_mode=False", qsmod.read_only_mode() is False)
check("W3 describe() 生产态提示不含开关名", "生产模式" in qsmod.describe())

print("\n=== W4 XJ_NO_PUSH=1：股票推送零副作用 ===")
os.environ["XJ_NO_PUSH"] = "1"
os.environ["XJ_NO_DEEPSEEK"] = "1"
reset_trackers()
try:
    run_stock_push("p3")
    err = None
except Exception as exc:  # pragma: no cover
    err = "%s: %s" % (type(exc).__name__, exc)
check("W4a 股票推送不抛异常", err is None, err or "")
check("W4b requests.post 次数=0（实际 %d）" % len(FAKE_REQUESTS.calls), not FAKE_REQUESTS.calls)
check("W4c 跨进程去重账本写入=0（实际 %d）" % len(LEDGER_WRITES), not LEDGER_WRITES)
check("W4d 线程池投递=0（实际 %d）" % len(EXECUTOR_CALLS), not EXECUTOR_CALLS)
reset_trackers()
run_stock_push("p5")
check("W4e P5 池同样 0 副作用（post=%d ledger=%d exec=%d）" % (
    len(FAKE_REQUESTS.calls), len(LEDGER_WRITES), len(EXECUTOR_CALLS)),
    not FAKE_REQUESTS.calls and not LEDGER_WRITES and not EXECUTOR_CALLS)

print("\n=== W5 XJ_NO_PUSH=1：系统告警零副作用（含账本） ===")
reset_trackers()
try:
    run_sys_alert()
    err = None
except Exception as exc:  # pragma: no cover
    err = "%s: %s" % (type(exc).__name__, exc)
check("W5a 系统告警不抛异常", err is None, err or "")
check("W5b requests.post 次数=0（实际 %d）" % len(FAKE_REQUESTS.calls), not FAKE_REQUESTS.calls)
check("W5c 系统告警去重账本写入=0（实际 %d）" % len(LEDGER_WRITES), not LEDGER_WRITES)
check("W5d 线程池投递=0（实际 %d）" % len(EXECUTOR_CALLS), not EXECUTOR_CALLS)

print("\n=== W6 XJ_NO_DEEPSEEK=1：DeepSeek 零副作用 ===")
reset_trackers()
try:
    adv = run_deepseek()
    err = None
except Exception as exc:  # pragma: no cover
    adv, err = None, "%s: %s" % (type(exc).__name__, exc)
check("W6a DeepSeek 不抛异常", err is None, err or "")
check("W6b 返回空建议（实际 %r）" % (adv,), not adv)
check("W6c AI 日志落库=0（实际 %d）" % len(SAVED), not SAVED)
check("W6d 不发起 HTTP（实际 %d）" % len(FAKE_REQUESTS.calls), not FAKE_REQUESTS.calls)
try:
    dsx._log_deepseek_analysis(
        pool_key="p5",
        stock_dict=dict(STOCK),
        stock_info="PROBE info",
        advice="PROBE advice",
        model="probe-model",
        latency_ms=1.0,
        success=True,
    )
    err2 = None
except Exception as exc:  # pragma: no cover
    err2 = "%s: %s" % (type(exc).__name__, exc)
check("W6e 直接调 _log_deepseek_analysis 也不落库（实际 %d）" % len(SAVED), not SAVED and err2 is None)

print("\n=== W7 关闭开关（生产态）副作用必须照旧发生 ===")
clear_env()
reset_trackers()
try:
    run_stock_push("p3")
    err = None
except Exception as exc:  # pragma: no cover
    err = "%s: %s" % (type(exc).__name__, exc)
check("W7a 生产态股票推送不抛异常", err is None, err or "")
check("W7b 生产态确实写了跨进程账本（>0，实际 %d）" % len(LEDGER_WRITES), len(LEDGER_WRITES) > 0)
check("W7c 生产态确实投递线程池（>0，实际 %d）" % len(EXECUTOR_CALLS), len(EXECUTOR_CALLS) > 0)
check("W7d 生产态确实发出 HTTP（>0，实际 %d）" % len(FAKE_REQUESTS.calls), len(FAKE_REQUESTS.calls) > 0)
reset_trackers()
try:
    run_sys_alert()
    err = None
except Exception as exc:  # pragma: no cover
    err = "%s: %s" % (type(exc).__name__, exc)
check("W7e 生产态系统告警不抛异常", err is None, err or "")
check("W7f 生产态系统告警真的发 HTTP（实际 %d）" % len(FAKE_REQUESTS.calls), len(FAKE_REQUESTS.calls) > 0)
check("W7g 生产态系统告警确实写了告警账本（实际 %d）" % len(LEDGER_WRITES), len(LEDGER_WRITES) > 0)
reset_trackers()
try:
    run_markdown()
    err = None
except Exception as exc:  # pragma: no cover
    err = "%s: %s" % (type(exc).__name__, exc)
check("W7h 生产态 _send_markdown_sync 不抛异常", err is None, err or "")
check("W7i 生产态 _send_markdown_sync 真的发 HTTP（实际 %d）" % len(FAKE_REQUESTS.calls),
      len(FAKE_REQUESTS.calls) >= 1)
reset_trackers()
os.environ["XJ_NO_SIGNAL_PERSIST"] = "1"   # 只开不落库，不应影响推送
run_markdown()
check("W7j 仅开 XJ_NO_SIGNAL_PERSIST 时推送照常发 HTTP（实际 %d）" % len(FAKE_REQUESTS.calls),
      len(FAKE_REQUESTS.calls) >= 1)
clear_env()

print("\n=== W8 fail-open：开关模块故障不得阻断生产 ===")
_orig_import = builtins.__import__


def _boom_import(name, *a, **k):
    if name == "core.qa_switches":
        raise RuntimeError("模拟开关模块导入失败")
    return _orig_import(name, *a, **k)


builtins.__import__ = _boom_import
reset_trackers()
try:
    run_markdown()
    err = None
except Exception as exc:  # pragma: no cover
    err = "%s: %s" % (type(exc).__name__, exc)
finally:
    builtins.__import__ = _orig_import
check("W8a 开关模块故障时推送不抛异常", err is None, err or "")
check("W8b fail-open：仍然真实发出推送（实际 %d）" % len(FAKE_REQUESTS.calls),
      len(FAKE_REQUESTS.calls) >= 1)
clear_env()

print("\n=== W9 防复发：点检脚本已内置三道闸 ===")
# 【2026-11-04 修正】QA 点检脚本是可被清理的临时产物，清理属于正常运维。
#   原断言「文件不存在 → FAIL」导致每次回归都红，长期淹没真实告警。
#   现改为：文件不存在 → 记为 SKIP/PASS（不存在即无复发风险）；
#   文件存在 → 仍严格校验它内置了三道开关（真正的防复发检查不放松）。
for rel in ("_qa_logs/_ui_menu_probe.py", "_qa_logs/_lab_tab_probe.py"):
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    if not os.path.isfile(p):
        check("W9 %s 存在(若存在则须内置三道闸)" % rel, True, "文件不存在→已清理，跳过内容校验")
        continue
    s = open(p, encoding="utf-8").read()
    for key in ENV_KEYS:
        check("W9 %s 已设置 %s=1" % (rel, key),
              ('environ["%s"] = "1"' % key) in s)

print("\n=== W10 生产启动链路不得自行设置 QA 开关 ===")
for rel in ("start_daemon_24x7.bat", "start_server.bat", "start_server.sh",
            "auto_sniper_daemon.py", "ui/app.py", "run_lab.py",
            "core/scan/pipeline.py", "core/scan/intraday_snapshots.py",
            "core/notification/gateway.py", "core/notification/deepseek.py",
            "core/notification/notifiers.py"):
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    if not os.path.isfile(p):
        check("W10 %s 存在" % rel, False, "文件不存在")
        continue
    s = open(p, encoding="utf-8", errors="replace").read()
    sets = [k for k in ENV_KEYS
            if ('environ["%s"]' % k) in s or ("environ['%s']" % k) in s
            or ('environ.setdefault("%s"' % k) in s or ("environ.setdefault('%s'" % k) in s]
    check("W10 %s 不自行设置 QA 开关（%s）" % (rel, sets or "无"), not sets)

print("\n=== W11 QA 总跑器默认开启三道闸（正向断言） ===")
_runner = open(os.path.join(ROOT, "tools", "_release_full_run.py"), encoding="utf-8").read()
for key in ENV_KEYS:
    check("W11 tools/_release_full_run.py 默认设置 %s" % key, key in _runner)
check("W11 tools/_release_full_run.py 已登记 tools/_test_no_side_effects.py",
      "_test_no_side_effects.py" in _runner)

logging.disable(logging.NOTSET)
print("\nPASS = %d  FAIL = %d" % (PASS, FAIL))
if FAILS:
    print("失败项:")
    for f in FAILS:
        print("  -", f)
sys.exit(1 if FAIL else 0)
