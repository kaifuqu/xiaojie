# -*- coding: utf-8 -*-
"""
tools/_test_push_content_dedup.py —— 企微「内容指纹」重复推送兜底回归

【事故背景 2026-11-05】
09:05 的「战法榜早盘推送」在 catchup 窗口内每分钟重发同一条战法榜，
当天实测重推 25+ 次（最坏 61 次）。调度器侧已修复（ST-P0-31），
此处补一层**与调用方无关**的兜底：完全相同的 markdown 在窗口内只发一次。

【本测试锁定的契约】
  1. 相同内容窗口内第二次 → 不发送
  2. 内容不同 → 照常发送（不能误伤不同票/不同报告）
  3. 超过窗口 → 允许再发（当天不同时点的正常推送不受影响）
  4. **发送失败不登记指纹** → 重试仍能发出（不能因兜底导致丢消息）
  5. clear_push_cache 能清掉指纹（排障时立刻重发）
  6. 并发安全：多线程同时收到相同内容，只发出 1 条

【安全】全程 mock requests，绝不真发企微、不碰生产账本。
"""
from __future__ import annotations

import atexit
import io
import os
import shutil
import sys
import tempfile
import threading
import time

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

_OUT = io.open(os.path.join(ROOT, "tools", "_push_dedup_result.txt"), "w", encoding="utf-8")
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


class _FakeResp:
    def __init__(self, code=200):
        self.status_code = code
        self.text = "{}"


def main():
    import core.notification.gateway as gwmod
    from core.notification.gateway import WechatNotificationGateway, _CONTENT_DEDUP_WINDOW_SEC

    # clear_push_cache 内部会 os.unlink 系统告警去重账本。回归必须把它指到临时路径，
    # 否则每跑一次测试就删掉生产 data/runtime/alert_dedup_cache.json，
    # 正在冷却期内的系统告警会被重新推一遍到企微。
    _tmpdir = tempfile.mkdtemp(prefix="push_dedup_")
    atexit.register(shutil.rmtree, _tmpdir, True)
    _tmp_alert_ledger = os.path.join(_tmpdir, "alert_dedup_cache.json")
    gwmod._path_alert_dedup_cache_file = lambda: _tmp_alert_ledger

    p("=" * 74)
    p("【企微内容指纹重复推送兜底 回归】")
    p("=" * 74)
    p("  窗口 = %.0fs（可用 XJ_PUSH_CONTENT_DEDUP_SEC 覆盖）" % _CONTENT_DEDUP_WINDOW_SEC)

    sent = []
    state = {"fail": False, "code": 200}

    class _FakeRequests:
        @staticmethod
        def post(url, json=None, timeout=None, headers=None):
            if state["fail"]:
                raise RuntimeError("模拟网络失败")
            sent.append(json["markdown"]["content"])
            return _FakeResp(state["code"])

    sys.modules["requests"] = _FakeRequests

    # 让 master_control 恒为开启
    import core.master_control as mc
    mc.is_wechat_push_master_enabled = lambda: True
    mc.is_wechat_system_alert_enabled = lambda: True

    # 确保不触发 QA 旁路
    os.environ.pop("XJ_NO_PUSH", None)
    import core.qa_switches as qs
    qs.no_push = lambda: False

    BODY = "## 盘后战法榜\n- 战法 13 个 / 信号 355 条\n- P5-02 胜率 0.667"

    gw = WechatNotificationGateway("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=FAKE")

    # ---- 1. 相同内容第二次被挡 ----
    p("\n=== 1. 相同内容窗口内只发一次（核心：本次事故的兜底）===")
    sent.clear()
    for i in range(5):
        gw._send_markdown_sync(BODY)
    p("  连续发送 5 次相同内容，实际发出 %d 条" % len(sent))
    check("1a 只发出 1 条（修复前会发 5 条）", len(sent) == 1, "sent=%d" % len(sent))

    # ---- 2. 不同内容照常发 ----
    p("\n=== 2. 内容不同 → 照常发送（不误伤）===")
    sent.clear()
    gw2 = WechatNotificationGateway("https://fake/2")
    for i in range(3):
        gw2._send_markdown_sync(BODY + "\n- 第%d只票" % i)
    p("  3 条不同内容，实际发出 %d 条" % len(sent))
    check("2a 3 条全部发出", len(sent) == 3, "sent=%d" % len(sent))

    # ---- 3. 窗口过期后允许再发 ----
    p("\n=== 3. 超过窗口 → 允许再次发送（不影响当天不同时点推送）===")
    sent.clear()
    gw3 = WechatNotificationGateway("https://fake/3")
    gw3._send_markdown_sync(BODY)
    # 手工把指纹时间戳往前拨，模拟窗口已过
    with gw3._records_lock:
        for k in gw3._recent_content_hashes:
            gw3._recent_content_hashes[k] = time.time() - (_CONTENT_DEDUP_WINDOW_SEC + 5)
    gw3._send_markdown_sync(BODY)
    p("  窗口前后各发一次，实际发出 %d 条" % len(sent))
    check("3a 窗口过后可再发（共 2 条）", len(sent) == 2, "sent=%d" % len(sent))

    # ---- 4. 发送失败不登记指纹 ----
    p("\n=== 4. 发送失败不登记指纹 → 重试仍能发出（不丢消息）===")
    sent.clear()
    gw4 = WechatNotificationGateway("https://fake/4")
    state["fail"] = True
    gw4._send_markdown_sync(BODY)   # 三次重试全失败
    n_after_fail = len(gw4._recent_content_hashes)
    state["fail"] = False
    gw4._send_markdown_sync(BODY)   # 恢复后应能发出
    p("  失败后指纹数=%d，恢复后发出 %d 条" % (n_after_fail, len(sent)))
    check("4a 失败不登记指纹", n_after_fail == 0, "hashes=%d" % n_after_fail)
    check("4b 恢复后能正常发出", len(sent) == 1, "sent=%d" % len(sent))

    # ---- 5. clear_push_cache 清指纹 ----
    p("\n=== 5. clear_push_cache 可清指纹（排障时立刻重发）===")
    sent.clear()
    gw5 = WechatNotificationGateway("https://fake/5")
    gw5._send_markdown_sync(BODY)
    # 埋一个临时账本文件，验证 clear_push_cache 走的是隔离路径（不会删生产账本）
    with io.open(_tmp_alert_ledger, "w", encoding="utf-8") as _f:
        _f.write("{}")
    gw5.clear_push_cache()
    _tmp_ledger_removed = not os.path.isfile(_tmp_alert_ledger)
    gw5.gw_test_hashes_after = len(gw5._recent_content_hashes)
    gw5._send_markdown_sync(BODY)
    p("  clear 后指纹数=%d，再次发送后总发出 %d 条" % (gw5.gw_test_hashes_after, len(sent)))
    check("5a clear 后指纹已清空", gw5.gw_test_hashes_after == 0)
    check("5b clear 后可立即重发（共 2 条）", len(sent) == 2, "sent=%d" % len(sent))
    check("5c 账本清理只作用于隔离路径（未删生产告警账本）", _tmp_ledger_removed)

    # ---- 6. 并发安全 ----
    p("\n=== 6. 并发安全：多线程同时发相同内容只出 1 条 ===")
    sent.clear()
    gw6 = WechatNotificationGateway("https://fake/6")
    errs = []

    def _worker():
        try:
            gw6._send_markdown_sync(BODY)
        except Exception as e:
            errs.append(e)

    ths = [threading.Thread(target=_worker) for _ in range(8)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    p("  8 线程并发，实际发出 %d 条（异常 %d）" % (len(sent), len(errs)))
    check("6a 只发出 1 条", len(sent) == 1, "sent=%d" % len(sent))
    check("6b 无异常", not errs, str(errs[:2]))

    # ---- 7. 摘要：应挡住本次事故的 61 次 ----
    p("\n=== 7. 对本次事故的拦截效果 ===")
    sent.clear()
    gw7 = WechatNotificationGateway("https://fake/7")
    for _ in range(61):
        gw7._send_markdown_sync(BODY)
    p("  模拟 09:05 的 61 次重发 → 实际只发出 %d 条" % len(sent))
    check("7a 61 次重发被压缩为 1 条", len(sent) == 1, "sent=%d" % len(sent))

    p("\n" + "=" * 74)
    if FAILS:
        p("❌ 失败 %d 项：%s" % (len(FAILS), FAILS))
        _OUT.close()
        return 1
    p("✅ 内容指纹兜底回归全部通过")
    p("=" * 74)
    _OUT.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
