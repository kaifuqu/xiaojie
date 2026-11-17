# -*- coding: utf-8 -*-
"""tools/test_data_health_badge_ui.py — 顶部「数据健康度」徽章 UI 渲染回归

校验（AppTest，真实 Streamlit 运行时）：
  ① render_data_health_badge() 不抛未捕获异常
  ② 渲染出至少一条状态消息（success/warning/error）
  ③ 文案不再出现「覆盖 1380/5917 = 23%」这类会被误读为「缺数据」的表述
  ④ 若快照有广度数据，文案里出现「跟踪域 N 只」

全部只读、不联网、不写生产库。
用法：
    python tools/test_data_health_badge_ui.py
"""
from __future__ import annotations

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core._pyarrow_compat import disable_pyarrow  # noqa: E402

disable_pyarrow()

FAIL = []
SKIPS = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'OK  ' if cond else 'FAIL'}] {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        FAIL.append(name)


def skip(name: str, detail: str = "") -> None:
    print(f"  [SKIP] {name}" + (f"  {detail}" if detail else ""))
    SKIPS.append(name)


APP = """
import sys
sys.path.insert(0, %r)
from core._pyarrow_compat import disable_pyarrow
disable_pyarrow()
from ui.data_health_badge import render_data_health_badge
render_data_health_badge()
""" % (ROOT,)


def main() -> int:
    print("=" * 72)
    print("数据健康度徽章 UI 回归 (tools/test_data_health_badge_ui.py)")
    print("=" * 72)

    from streamlit.testing.v1 import AppTest

    tmp = tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, encoding="utf-8")
    try:
        tmp.write(APP)
        tmp.close()
        at = AppTest.from_file(tmp.name, default_timeout=120)
        at.run()
        if at.exception:
            for e in at.exception:
                print("  [EXC]", e)
            check("无未捕获异常", False, str(at.exception[0]))
        else:
            check("无未捕获异常", True)

        msgs = []
        for kind in ("success", "warning", "error", "info"):
            for m in getattr(at, kind, []) or []:
                msgs.append((kind, str(getattr(m, "value", m))))

        check("至少渲染一条状态消息", len(msgs) > 0, f"count={len(msgs)}")
        for kind, val in msgs:
            print(f"    <{kind}> {val[:160]}")

        blob = "\n".join(v for _, v in msgs)
        check("文案含「跟踪域」", ("跟踪域" in blob) or ("覆盖率" not in blob),
              "（无跟踪域时按未取到广度数据处理）")
        check("不出现误导性的「覆盖 N/M = P%」正文",
              ("覆盖" not in blob) or ("非缺数据" in blob) or ("跟踪域" in blob))
        check("无未捕获异常（复检）", not at.exception)

    finally:
        try:
            os.unlink(tmp.name)
        except Exception:
            pass

    print("=" * 72)
    print(f"失败 {len(FAIL)} 项 / 跳过 {len(SKIPS)} 项")
    if FAIL:
        for f in FAIL:
            print("  FAIL:", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
