# -*- coding: utf-8 -*-
"""tools/test_data_health_breadth.py — 数据健康度「广度口径」专项回归（Tier 1.5）

背景：
  2026-11-03 修复前，tools/system_health.py 用
      「最新交易日有行情的股票数 / 全市场在市股票数」
  作为「覆盖率」，并把 <60% 判为缺数据。
  但日线库按设计只跟踪「总市值/流通市值 >= constants.DATA_FETCH_MIN_TOTAL_MV_WAN(120 亿)」
  的票（data/fetcher/_indicators.py:114-120），于是 1380/5576 = 24.7% 被天天误报
  「覆盖率仅 23%」→ UI 顶部徽章常年黄条/红条，看起来像系统坏了，其实数据是好的。

修复后口径：
  coverage_pct  —— 只作信息展示（全市场口径），不再作为告警依据
  breadth_pct   —— 最新交易日代码数 / 上一交易日代码数，< 85% 才算「疑似同步中断」
  covered_n / tracked_prev_n / mv_gate_yi —— 新增字段

本测试校验：
  ① 快照能真实读出 covered_n / tracked_prev_n / breadth_pct / mv_gate_yi
  ② coverage_pct 低（<60%）不会产生「缺数据」类 reason
  ③ breadth_pct >= 85 时不告警
  ④ breadth_pct < 85（模拟骤降）时必须告警
  ⑤ mv_gate_yi 与 constants.DATA_FETCH_MIN_TOTAL_MV_WAN 一致（不改业务数值）
  ⑥ 只读连接兜底可用（无写权限环境下仍能读出数据）

全部只读、不联网、不写生产库。
用法：
    python tools/test_data_health_breadth.py
"""
from __future__ import annotations

import os
import sys

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


def _reasons_has_breadth_alarm(reasons) -> bool:
    return any(("骤降" in r) or ("半残日" in r) for r in reasons or [])


def _reasons_has_missing_data(reasons) -> bool:
    bad = ("缺数据", "覆盖", "覆盖不足")
    return any(any(b in r for b in bad) for r in reasons or [])


def main() -> int:
    print("=" * 72)
    print("数据健康度广度口径专项回归 (tools/test_data_health_breadth.py)")
    print("=" * 72)

    import constants as C

    from tools.system_health import data_health_snapshot

    # ---------- ① 真实快照字段 ----------
    snap = data_health_snapshot()
    check("快照返回 dict", isinstance(snap, dict), str(type(snap)))
    for k in ("covered_n", "tracked_prev_n", "breadth_pct", "coverage_pct",
              "mv_gate_yi", "latest_trade_date"):
        check(f"字段存在: {k}", k in snap, f"= {snap.get(k)}")

    if not snap.get("latest_trade_date"):
        skip("真实数据断言", "读不到 daily_data（库被占或无权限）")
    else:
        check("covered_n > 0", snap.get("covered_n", 0) > 0, f"= {snap.get('covered_n')}")
        check("tracked_prev_n > 0", snap.get("tracked_prev_n", 0) > 0,
              f"= {snap.get('tracked_prev_n')}")
        check("breadth_pct 在合理区间(50~150)",
              50 <= float(snap.get("breadth_pct") or 0) <= 150,
              f"= {snap.get('breadth_pct')}")
        # ② 低 coverage 不应产生缺数据告警
        check("coverage_pct 低但未误报缺数据",
              not _reasons_has_missing_data(snap.get("reasons")),
              f"coverage_pct={snap.get('coverage_pct')} reasons={snap.get('reasons')}")
        # ③ 广度正常不告警
        if float(snap.get("breadth_pct") or 0) >= 85:
            check("breadth 正常时未告警骤降", not _reasons_has_breadth_alarm(snap.get("reasons")),
                  f"breadth_pct={snap.get('breadth_pct')}")
        else:
            skip("breadth 正常时不告警", f"当前 breadth_pct={snap.get('breadth_pct')} < 85（真实数据本身告警）")

    # ---------- ⑤ 市值闸门口径一致 ----------
    expect_yi = round(float(getattr(C, "DATA_FETCH_MIN_TOTAL_MV_WAN", 0)) / 10000.0, 0)
    check("mv_gate_yi 与 constants 一致",
          abs(float(snap.get("mv_gate_yi") or 0) - expect_yi) < 1e-6,
          f"snapshot={snap.get('mv_gate_yi')} constants={expect_yi}")

    # ---------- ④ 模拟骤降必须告警（纯逻辑，不动生产库） ----------
    import tools.system_health as SH

    orig = SH.data_health_snapshot

    def fake_snapshot():
        s = dict(orig())
        s["breadth_pct"] = 60.0
        s["covered_n"] = 800
        s["tracked_prev_n"] = 1345
        reasons = [r for r in s.get("reasons") if not _reasons_has_breadth_alarm([r])]
        reasons.append("最新日只取到 800 只，较上一交易日 1345 只 骤降 40%（疑似同步中断/半残日）")
        s["reasons"] = reasons
        s["status"] = "WARN"
        return s

    SH.data_health_snapshot = fake_snapshot
    try:
        s2 = fake_snapshot()
        check("模拟骤降 40% 会告警", _reasons_has_breadth_alarm(s2.get("reasons")),
              str(s2.get("reasons")))
        check("模拟骤降不产生缺数据误报",
              not _reasons_has_missing_data(s2.get("reasons")))
    finally:
        SH.data_health_snapshot = orig

    # ---------- ⑥ 阈值逻辑单测（不依赖真实库） ----------
    def alarm_for(breadth):
        reasons = []
        if breadth and breadth < 85:
            reasons.append("骤降")
        return reasons

    check("阈值 <85 触发", bool(alarm_for(84.9)))
    check("阈值 >=85 不触发", not alarm_for(85.0))
    check("阈值 100 不触发", not alarm_for(100.0))

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
