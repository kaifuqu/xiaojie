# -*- coding: utf-8 -*-
"""V30.14 专项回归：侧栏「📡 同步诊断」dry-run 预览 + 二次确认。

【背景】
侧栏里唯一会真实调 Tushare API 并改库（dim_sw_sector / sw_sector_daily）的按钮。
修复前点一下就直接同步，没有任何预览与反馈，容易困惑。
V30.14：点「📡 同步诊断」只出只读预览，必须再点「✅ 确认同步」才真同步，
        「✖ 取消」直接丢弃，不调接口、不改库。

【断言】
  W1  源码层：同步按钮不再直接调 sync_and_diagnose_sw_sector（只有确认分支调）
  W2  源码层：确认 / 取消两个按钮都存在且 key 唯一
  W3  源码层：预览走的是只读 diagnose_sw_sector_coverage，且在确认分支之前
  W4  源码层：取消分支不调用任何写库/调接口函数
  W5  源码层：session_state 门闩默认未设置（不会首屏就弹预览）
  W6  源码层：只读诊断「🔎 只读诊断」路径完全保留
  W7  打桩：stub 掉 sync_and_diagnose_sw_sector，AppTest 点「📡 同步诊断」
           → 预览出现、sync 未被调用（同步调用次数 = 0）
  W8  打桩：再点「✖ 取消」→ 预览消失、sync 仍未被调用
  W9  打桩：再点「📡 同步诊断」再点「✅ 确认同步」→ sync 被调用恰好 1 次
  W10 源码层：生产启动链路无 XJ_NO_SIGNAL_PERSIST 之类误设（本功能不依赖任何环境变量）

运行：
    _pyenv/venv/Scripts/python.exe tools/_test_sw_sync_dryrun.py
"""
from __future__ import annotations

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
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


SB = os.path.join(ROOT, "ui", "ui_sidebar.py")
src = open(SB, encoding="utf-8").read()

BLOCK_START = src.find('st.caption("申万板块趋势诊断')
check("W0a 找到申万板块诊断区块", BLOCK_START > 0, "pos=%d" % BLOCK_START)
BLOCK = src[BLOCK_START:BLOCK_START + 6000]

_sync_pos = BLOCK.find("sync_and_diagnose_sw_sector(level=\"L1\")")
_confirm_pos = BLOCK.find("st.button(\"✅ 确认同步\"")
_cancel_pos = BLOCK.find("st.button(\"✖ 取消\"")
_gate_pos = BLOCK.find("st.session_state.get(sw_sync_confirm_key)")
_prev_pos = BLOCK.find("diagnose_sw_sector_coverage()")
_diag_btn_pos = BLOCK.find("st.button(\"🔎 只读诊断\"")
_sync_btn_pos = BLOCK.find("st.button(\"📡 同步诊断\"")

check("W1a 同步调用存在且只有一处", _sync_pos > 0 and BLOCK.count("sync_and_diagnose_sw_sector(level=\"L1\")") == 1)
check("W1b 确认按钮必须先设置执行门闩、再 rerun（同步在下一轮才发生）",
      0 < _confirm_pos < BLOCK.find("if sw_confirm:", _confirm_pos)
      < BLOCK.find("st.session_state[sw_sync_do_key] = True")
      < BLOCK.find("st.rerun()", BLOCK.find("if sw_confirm:", _confirm_pos)),
      "confirm=%d if=%d do=%d rerun=%d" % (_confirm_pos,
                                          BLOCK.find("if sw_confirm:", _confirm_pos),
                                          BLOCK.find("st.session_state[sw_sync_do_key] = True"),
                                          BLOCK.find("st.rerun()", BLOCK.find("if sw_confirm:", _confirm_pos))))
check("W1c 取消分支只有 st.rerun()，绝不设置执行门闩",
      0 < _cancel_pos < BLOCK.find("if sw_cancel:", _cancel_pos) < BLOCK.find("st.rerun()", _cancel_pos)
      and "sw_sync_do_key" not in BLOCK[BLOCK.find("if sw_cancel:", _cancel_pos):BLOCK.find("if sw_confirm:", _cancel_pos)],
      "cancel=%d" % _cancel_pos)
check("W1d 门闩判断在确认按钮之前", 0 < _gate_pos < _confirm_pos,
      "gate=%d confirm=%d" % (_gate_pos, _confirm_pos))
check("W1e 同步调用只出现在「执行门闩 pop」这一处",
      BLOCK.count("sync_and_diagnose_sw_sector(level=\"L1\")") == 1
      and 0 < _sync_pos < _gate_pos,
      "sync=%d gate=%d" % (_sync_pos, _gate_pos))

check("W2a 确认按钮存在", _confirm_pos > 0)
check("W2b 取消按钮存在", _cancel_pos > 0)
check("W2c 确认按钮 key = sidebar_btn_sw_sector_sync_confirm",
      'key="sidebar_btn_sw_sector_sync_confirm"' in BLOCK)
check("W2d 取消按钮 key = sidebar_btn_sw_sector_sync_cancel",
      'key="sidebar_btn_sw_sector_sync_cancel"' in BLOCK)
check("W2e 同步按钮 key = sidebar_btn_sw_sector_sync_diag",
      'key="sidebar_btn_sw_sector_sync_diag"' in BLOCK)
check("W2f 三个 key 互不重复", len({b for b in ("sidebar_btn_sw_sector_sync_confirm", "sidebar_btn_sw_sector_sync_cancel", "sidebar_btn_sw_sector_sync_diag") if BLOCK.count('key="%s"' % b) == 1}) == 3)

check("W3a 预览走只读 diagnose_sw_sector_coverage", _prev_pos > 0)
check("W3b 预览在确认按钮之前", 0 < _prev_pos < _confirm_pos,
      "preview=%d confirm=%d" % (_prev_pos, _confirm_pos))
check("W3c 预览文案明示「不调接口、不改库」", "不调接口、不改库" in BLOCK)
check("W3d 预览文案明示尚未执行写入", "尚未执行任何写入" in BLOCK)

cancel_branch = BLOCK[_cancel_pos:BLOCK.find("if sw_confirm:", _cancel_pos)]
check("W4a 取消分支内无同步调用", "sync_and_diagnose_sw_sector" not in cancel_branch)
check("W4b 取消分支内无 save_ 调用", "save_" not in cancel_branch)
check("W4c 取消分支内无 sync_stock_basic / sync_sw_", "sync_stock_basic" not in cancel_branch and "sync_sw_" not in cancel_branch)

check("W5a 门闩 key 定义存在", '_sw_sync_confirm_pending' in BLOCK)
check("W5b 模块顶层不默认置 True（首屏不弹预览）",
      'st.session_state["_sw_sync_confirm_pending"] = True' not in src.split("sw_sync_confirm_key = ")[0][-400:])

check("W6a 只读诊断按钮仍在", _diag_btn_pos > 0)
check("W6b 只读诊断路径未被门闩包住", _diag_btn_pos < _gate_pos, "diag=%d gate=%d" % (_diag_btn_pos, _gate_pos))

# ---------------- 打桩：AppTest 真实点按钮 ----------------
print("")
print("=== W7-W9 AppTest 打桩（stub 掉真实同步函数）===")

from core._pyarrow_compat import disable_pyarrow  # noqa: E402

disable_pyarrow()
os.environ.setdefault("XJ_NO_SIGNAL_PERSIST", "1")
os.environ.setdefault("XJ_NO_PUSH", "1")
os.environ.setdefault("XJ_NO_DEEPSEEK", "1")

import data.db_core as dbc  # noqa: E402

SYNC_CALLS = []
_PREV_CALLS = []


def _fake_sync(**k):
    SYNC_CALLS.append(dict(k))
    return {"ok": True, "counts": {"sw_l1_count": 31, "sw_sector_daily_rows": 3813,
                                    "sw_sector_daily_min_date": "2026-04-15",
                                    "sw_sector_daily_max_date": "2026-10-16",
                                    "system_industry_count": 111},
            "match": {"exact_hit_rate": 0.018}, "trend_context_count": 31,
            "trend_bonus_top": [], "suggestions": [], "mapping_suggestions": {},
            "unmatched_industries": [], "sync": {"basic_ok": True, "daily_ok": True, "level": "L1"}}


def _fake_diag(**k):
    _PREV_CALLS.append(dict(k))
    out = _fake_sync()
    SYNC_CALLS.clear()
    return out


dbc.sync_and_diagnose_sw_sector = _fake_sync
dbc.diagnose_sw_sector_coverage = _fake_diag

APP = os.path.join(ROOT, "ui", "app.py")
from streamlit.testing.v1 import AppTest  # noqa: E402


def _find(at, label):
    for scope in (at.sidebar, at):
        for b in scope.button:
            if getattr(b, "label", "") == label:
                return b
    return None


try:
    at = AppTest.from_file(APP, default_timeout=300)
    at.run(timeout=300)
    check("W7a 首屏无异常", not at.exception and not at.error,
          str([str(e.value)[:160] for e in list(at.exception) + list(at.error)][:2]))

    b_sync = _find(at, "📡 同步诊断")
    check("W7b 找到「📡 同步诊断」按钮", b_sync is not None)
    if b_sync is not None:
        b_sync.click()
        at.run(timeout=300)
        check("W7c 点同步后无异常", not at.exception and not at.error,
              str([str(e.value)[:160] for e in list(at.exception) + list(at.error)][:2]))
        b_ok = _find(at, "✅ 确认同步")
        b_no = _find(at, "✖ 取消")
        check("W7d 预览出现：确认按钮可见", b_ok is not None)
        check("W7e 预览出现：取消按钮可见", b_no is not None)
        check("W7f 只点同步不触发真实同步（调用次数=0）", len(SYNC_CALLS) == 0, "实际 %d" % len(SYNC_CALLS))
        check("W7g 预览确实跑了只读诊断（调用次数>=1）", len(_PREV_CALLS) >= 1, "实际 %d" % len(_PREV_CALLS))

        if b_no is not None:
            b_no.click()
            at.run(timeout=300)
            check("W8a 取消后无异常", not at.exception and not at.error,
                  str([str(e.value)[:160] for e in list(at.exception) + list(at.error)][:2]))
            check("W8b 取消后仍不触发同步（调用次数=0）", len(SYNC_CALLS) == 0, "实际 %d" % len(SYNC_CALLS))
            check("W8c 取消后预览收起", _find(at, "✅ 确认同步") is None)

        b_sync = _find(at, "📡 同步诊断")
        b_sync.click()
        at.run(timeout=300)
        b_ok = _find(at, "✅ 确认同步")
        check("W9a 二次点同步后确认按钮重新可见", b_ok is not None)
        if b_ok is not None:
            b_ok.click()
            at.run(timeout=300)
            check("W9b 确认后无异常", not at.exception and not at.error,
                  str([str(e.value)[:160] for e in list(at.exception) + list(at.error)][:2]))
            check("W9c 确认后恰好触发 1 次同步", len(SYNC_CALLS) == 1, "实际 %d" % len(SYNC_CALLS))
            check("W9d 确认后预览收起", _find(at, "✅ 确认同步") is None)
            check("W9e 确认后同步参数仍是 level=L1（未改任何同步语义）",
                  bool(SYNC_CALLS) and SYNC_CALLS[0].get("level") == "L1", str(SYNC_CALLS[:1]))
except Exception as exc:
    check("W7-W9 AppTest 执行", False, "%s: %s" % (type(exc).__name__, exc))

print("")
print("=== W10 本功能不依赖任何 QA 环境变量 ===")
for rel in ("ui/ui_sidebar.py",):
    s = open(os.path.join(ROOT, rel.replace("/", os.sep)), encoding="utf-8").read()
    blk = s[s.find('st.caption("申万板块趋势诊断'):][:6000]
    check("W10 %s 同步诊断块内不含 XJ_NO_* 环境变量" % rel, "XJ_NO_" not in blk)

print("")
print("PASS = %d  FAIL = %d" % (PASS, FAIL))
if FAILS:
    print("失败项:")
    for f in FAILS:
        print("  -", f)
sys.exit(1 if FAIL else 0)
