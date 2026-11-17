# -*- coding: utf-8 -*-
"""
tools/_test_bj_scheduler_once.py —— BJ 日任务「当日只派发一次」回归

【事故背景 2026-11-05】
`_bj_daily_job_completed` 只认一张硬编码白名单，凡未登记的任务永远返回 False，
调度器便认为「没做成」，在 catchup 窗口内**每分钟重派一次**。
推送类任务因此每天重发同一内容：
  · 09:05 战法榜早盘推送（catchup=60）→ 实测当天已重推 25 次，最坏 61 次
  · 18:45 战法效果榜推送（catchup=120）→ 最坏 121 次
  · 15:05 模拟盘收盘结算（catchup=90）→ 实测 2026-11-04 重跑 90 次
同类事故此前已修过两次（79 次 / 48 次），说明「白名单登记」模式本身是坑。

【本测试锁定的契约】
  1. catchup 窗口内，成功任务**只派发一次**（不因窗口长而重复）
  2. 任务抛异常 → 仍在窗口内重试（保留 catchup 的价值）
  3. 任务返回 False（业务失败）→ 仍在窗口内重试
  4. 账本跨"进程重启"（全局变量重置）依然有效 —— 这是重启后不再重发的关键
  5. 跨自然日自动失效，第二天照常派发
  6. 已有 pipeline 状态位的晚间任务不受影响（语义不变）
"""
from __future__ import annotations

import importlib
import io
import logging
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

# Windows 控制台默认 GBK，打印 ✅ 会抛 UnicodeEncodeError 让本该通过的回归以
# exit=1 结束（假失败）。保留控制台编码，仅把无法编码的字符降级为占位符；
# 文件日志仍是完整 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

_OUT = io.open(os.path.join(ROOT, "tools", "_bj_once_result.txt"), "w", encoding="utf-8")
FAILS = []


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    _OUT.write(s + "\n")


def check(label, ok, detail=""):
    if ok:
        p("  [PASS] %s  %s" % (label, detail))
    else:
        FAILS.append(label)
        p("  [FAIL] %s  %s" % (label, detail))
    return ok


def _isolate_logging(path):
    """把 root logger 的 FileHandler 从生产 sniper.log 换到临时文件。

    daemon 模块在 import 阶段就会挂上 data/runtime/sniper.log 的 RotatingFileHandler，
    而本回归会模拟 61 次 catchup 补跑 —— 那会把几十条假告警写进生产日志，
    事后排查「补跑」时会被这堆假记录带偏。
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, logging.FileHandler):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
    fh = logging.FileHandler(path, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s [TEST] %(levelname)s %(message)s"))
    root.addHandler(fh)


class _FakeNow:
    """可注入的假时钟：只替换 hour/minute/second/strftime。"""

    def __init__(self, y, mo, d, h, mi):
        import datetime as _dt

        self._dt = _dt.datetime(y, mo, d, h, mi, 0)

    def strftime(self, fmt):
        return self._dt.strftime(fmt)

    @property
    def hour(self):
        return self._dt.hour

    @property
    def minute(self):
        return self._dt.minute


def _drive(daemon, minute_of_day, calls, today_key, tmplabel, catchup, fn):
    """模拟一个 tick：把 _now_bj 固定到指定分钟，然后跑调度器。

    【注意】每次都清空 _BJ_DISPATCHED_JOB_KEYS（进程内「同分钟只派发一次」的键）。
    这里要验证的是**跨分钟/跨重启靠持久账本去重**，若保留进程内键，
    子测试之间会互相干扰（分钟键相同 → 被同分钟去重短路），掩盖真实行为。
    """
    h, m = divmod(minute_of_day, 60)
    daemon._now_bj = lambda: _FakeNow(2026, 11, 5, h, m)
    daemon._BJ_LAST_TICK_MINUTE_KEY = ""
    daemon._BJ_DISPATCHED_JOB_KEYS.clear()
    daemon._BJ_DAILY_JOBS = [(tmplabel[0], fn, tmplabel[1], catchup)]
    daemon._tick_bj_daily_scheduler()


def main():
    p("=" * 74)
    p("【BJ 日任务「当日只派发一次」回归】")
    p("=" * 74)

    tmpdir = tempfile.mkdtemp(prefix="bj_once_")
    try:
        # 必须在 import 之前重定向日志：daemon 模块在 import 阶段就会把
        # RotatingFileHandler 挂到生产 data/runtime/sniper.log 上。
        os.environ["XJ_SNIPER_LOG_PATH"] = os.path.join(tmpdir, "daemon_test.log")
        import auto_sniper_daemon as daemon

        _isolate_logging(os.path.join(tmpdir, "daemon_test.log"))

        # 账本指向临时目录，绝不碰生产 state
        daemon._BJ_DAILY_DONE_LEDGER = os.path.join(tmpdir, "bj_daily_jobs_done.json")
        # 避免真跑业务：pipeline 状态读空
        daemon._read_pipeline_state = lambda: {}

        # ---------------- 1. catchup 窗口内只派发一次 ----------------
        p("\n=== 1. 成功任务在 catchup 窗口内只派发一次（核心）===")
        calls = []

        def _ok_job():
            calls.append(1)

        # 09:05 注册，catchup=60 → 模拟 09:05..10:05 共 61 个 tick
        for mod in range(545, 545 + 61):
            _drive(daemon, mod, calls, "20261105", ("09:05", "战法榜早盘推送"), 60, _ok_job)

        p("  窗口 61 个 tick，实际派发 %d 次" % len(calls))
        check("1a 只派发 1 次（修复前为 61 次）", len(calls) == 1, "calls=%d" % len(calls))

        # ---------------- 2. 异常仍重试 ----------------
        p("\n=== 2. 任务抛异常时仍在窗口内重试（保住 catchup 价值）===")
        daemon._BJ_DAILY_DONE_LEDGER = os.path.join(tmpdir, "ledger2.json")
        calls2 = []

        def _boom_job():
            calls2.append(1)
            raise RuntimeError("模拟任务失败")

        for mod in range(545, 545 + 10):
            _drive(daemon, mod, None, "20261105", ("09:05", "战法榜早盘推送"), 60, _boom_job)

        p("  10 个 tick，实际派发 %d 次（每次失败）" % len(calls2))
        check("2a 异常不被标记完成 → 持续重试", len(calls2) == 10, "calls=%d" % len(calls2))

        # ---------------- 3. 返回 False 仍重试 ----------------
        p("\n=== 3. 任务返回 False（业务失败）时仍重试 ===")
        daemon._BJ_DAILY_DONE_LEDGER = os.path.join(tmpdir, "ledger3.json")
        calls3 = []

        def _false_job():
            calls3.append(1)
            return False

        for mod in range(545, 545 + 8):
            _drive(daemon, mod, None, "20261105", ("09:05", "战法榜早盘推送"), 60, _false_job)

        p("  8 个 tick，实际派发 %d 次" % len(calls3))
        check("3a 返回 False → 不标记完成，继续重试", len(calls3) == 8, "calls=%d" % len(calls3))

        # ---------------- 4. 跨「进程重启」仍有效 ----------------
        p("\n=== 4. 账本落盘，进程重启后当天不再重发 ===")
        ledger_ok = os.path.isfile(daemon._BJ_DAILY_DONE_LEDGER) or os.path.isfile(
            os.path.join(tmpdir, "bj_daily_jobs_done.json")
        )
        # 模拟重启：清空全局变量与派发键
        daemon._BJ_LAST_TICK_MINUTE_KEY = ""
        daemon._BJ_DISPATCHED_JOB_KEYS.clear()
        calls4 = []

        def _ok_job2():
            calls4.append(1)

        daemon._BJ_DAILY_DONE_LEDGER = os.path.join(tmpdir, "bj_daily_jobs_done.json")
        for mod in range(560, 566):
            _drive(daemon, mod, None, "20261105", ("09:05", "战法榜早盘推送"), 60, _ok_job2)

        p("  重启后 6 个 tick，实际派发 %d 次" % len(calls4))
        check("4a 重启后当天不再重复派发", len(calls4) == 0, "calls=%d" % len(calls4))
        check("4b 账本文件已生成", ledger_ok)

        # ---------------- 5. 跨日重新生效 ----------------
        p("\n=== 5. 跨自然日账本失效，第二天照常派发 ===")
        calls5 = []

        def _ok_job3():
            calls5.append(1)

        daemon._now_bj = lambda: _FakeNow(2026, 11, 6, 9, 5)
        daemon._BJ_LAST_TICK_MINUTE_KEY = ""
        daemon._BJ_DISPATCHED_JOB_KEYS.clear()
        daemon._BJ_DAILY_JOBS = [("09:05", _ok_job3, "战法榜早盘推送", 60)]
        daemon._tick_bj_daily_scheduler()
        p("  次日 09:05 派发 %d 次" % len(calls5))
        check("5a 次日正常派发一次", len(calls5) == 1, "calls=%d" % len(calls5))

        # ---------------- 6. 晚间状态位任务语义不变 ----------------
        p("\n=== 6. 有 pipeline 状态位的晚间任务不受影响 ===")
        check("6a 晚间P1 仍走状态位", "晚间P1" in daemon._BJ_STATE_KEY_BY_LABEL)
        daemon._read_pipeline_state = lambda: {"last_p1_ok_bj_date": "20261105"}
        check("6b 状态位=今天 → 判定完成",
              daemon._bj_daily_job_completed("晚间P1", "20261105") is True)
        daemon._read_pipeline_state = lambda: {"last_p1_ok_bj_date": "20261104"}
        check("6c 状态位≠今天 → 判定未完成",
              daemon._bj_daily_job_completed("晚间P1", "20261105") is False)

        # ---------------- 7. 未登记 label 不再永久 False ----------------
        p("\n=== 7. 未登记 label 不再恒为 False（本次事故根因）===")
        daemon._read_pipeline_state = lambda: {}
        daemon._BJ_DAILY_DONE_LEDGER = os.path.join(tmpdir, "ledger7.json")
        check("7a 全新 label 初始未完成",
              daemon._bj_daily_job_completed("某个没人登记的新任务", "20261105") is False)
        daemon._mark_bj_daily_job_completed("某个没人登记的新任务", "20261105")
        check("7b 标记后判定完成",
              daemon._bj_daily_job_completed("某个没人登记的新任务", "20261105") is True)

        # ---------------- 8. 三个高危任务已无 catchup 重发风险 ----------------
        p("\n=== 8. 三个历史高危任务的当前配置 ===")
        for lbl in ("战法榜早盘推送", "战法效果榜推送", "模拟盘收盘结算"):
            has_state = lbl in daemon._BJ_STATE_KEY_BY_LABEL
            p("  %-16s 状态位=%s → 走通用账本兜底" % (lbl, "有" if has_state else "无"))
            # 真断言：标记后必须判定为完成，否则调度器仍会每分钟重派
            daemon._mark_bj_daily_job_completed(lbl, "20261105")
            check("8 %s 已被账本覆盖（不会再每分钟重发）" % lbl,
                  daemon._bj_daily_job_completed(lbl, "20261105") is True)

        p("\n" + "=" * 74)
        if FAILS:
            p("❌ 失败 %d 项：%s" % (len(FAILS), FAILS))
            return 1
        p("✅ BJ 日任务幂等回归全部通过")
        p("=" * 74)
        return 0
    finally:
        _OUT.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
