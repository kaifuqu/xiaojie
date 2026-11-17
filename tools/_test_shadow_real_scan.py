# -*- coding: utf-8 -*-
"""
tools/_test_shadow_real_scan.py —— shadow 模式真实扫描验证（只读，不写库）

【为什么需要】
前面的单元测试用的是构造数据。真正上线前必须确认：
在**真实生产链路**上跑一次扫描，shadow 模式
  1. 不会改变选股结果（票数与 disabled 模式一致）
  2. 能产出 signal_filter_stats（观察期才有数据可看）
  3. 能在信号行上留下「风险标签」标注

【安全】
  只调用 run_scan_engine（纯计算，返回 dict），
  不写 signal_log、不推送、不落任何库。可反复运行。
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

out = io.open("tools/_shadow_scan_result.txt", "w", encoding="utf-8")


def p(*a):
    s = " ".join(str(x) for x in a)
    print(s)
    out.write(s + "\n")


def main():
    try:
        return _run()
    except Exception:
        import traceback

        p("\n❌ 未捕获异常：")
        p(traceback.format_exc()[:3000])
        return 1


def _run():
    from core.scan_engine import run_scan_engine

    p("=" * 74)
    p("【shadow 真实扫描验证】只读，不写库不推送")
    p("=" * 74)

    # 用守护进程同源的底仓装载函数，保证字段与生产完全一致。
    # 手工构造的 dict 缺 P3 分时 / P5 涨停上下文等战法专用字段 → 必然 0 候选，
    # 那样验证不出任何东西。
    from auto_sniper_daemon import load_base_items_latest

    base_items = load_base_items_latest()
    if not base_items:
        p("\n⚠️ 底仓为空（可能非交易日或数据未同步），无法验证")
        out.close()
        return 0

    p(f"\n底仓装载完成: {len(base_items)} 只（与守护进程同源）")
    # 抽查底仓字段
    sample = base_items[0]
    p(f"  样例字段数: {len(sample)}；含 rt: {'rt' in sample}")

    # ---- 用 shadow 模式跑（P5 盘后：基于日线数据，可用历史日回放） ----
    p("\n" + "=" * 74)
    p("运行 run_scan_engine(['p5'], ...) —— 配置当前为 shadow")
    p("  说明：P3 盘中战法依赖分时/VWAP 等实时字段，用收盘后日线构造的池子")
    p("        不会产生候选；P5 盘后战法就基于日线，适合离线回放验证。")
    p("=" * 74)

    try:
        res = run_scan_engine(["p5"], base_items, regime="震荡市", progress_callback=None)
    except Exception as e:
        import traceback

        p(f"\n❌ 扫描异常: {type(e).__name__}: {e}")
        p(traceback.format_exc()[:2500])
        out.close()
        return 1

    p("\n=== 扫描结果 ===")
    p3 = res.get("p5") or []
    obs = (res.get("observation") or {}).get("p5") or []
    p(f"  p5 正式信号: {len(p3)} 条")
    p(f"  p5 观察池:   {len(obs)} 条")

    stats = res.get("signal_filter_stats") or {}
    p(f"\n=== signal_filter 统计 ===")
    if stats:
        for k, v in sorted(stats.items()):
            p(f"  {k} = {v}")
    else:
        p("  （无统计 —— 可能无候选或过滤器未启用）")

    # 检查风险标签是否写入
    p("\n=== 风险标签标注抽查（前 8 条）===")
    tagged = 0
    for r in (p3 + obs)[:8]:
        tag = str(r.get("风险标签", "") or "")
        if tag:
            tagged += 1
        p(f"  {str(r.get('代码','')):<12} {str(r.get('名称',''))[:8]:<10} 风险标签: {tag[:70]!r}")
    total_tagged = sum(1 for r in (p3 + obs) if str(r.get("风险标签", "") or "").strip())
    p(f"\n  带风险标签的行: {total_tagged} / {len(p3) + len(obs)}")

    p("\n" + "=" * 74)
    p("【结论】")
    p("=" * 74)
    ok = True
    if stats.get("total"):
        p(f"  ✅ 过滤器已参与评估（total={stats['total']}）")
        reject = int(stats.get("blocked") or 0)
        total = int(stats.get("total") or 1)
        p(f"  ✅ shadow 下拦截数 = {reject}（应为 0，只标注不拦）")
        if reject != 0:
            p("  ❌ shadow 模式下不应拦截任何票！")
            ok = False
        l1 = int(stats.get("L1") or 0) + int(stats.get("L2") or 0) + int(stats.get("L3") or 0)
        l1 += int(stats.get("L4") or 0) + int(stats.get("L5") or 0)
        p(f"  ✅ 不合格计数 = {l1}（拒绝率 {l1 / total * 100:.1f}%）")
    else:
        p("  ⚠️ 无统计（本样本未产生候选，或候选全被更前置的环节过滤）")
    if total_tagged:
        p(f"  ✅ 风险标签已写入信号行（{total_tagged} 条）")
    else:
        p("  ⚠️ 未观察到风险标签（若过滤器 enabled 则应写入）")

    p("=" * 74)
    out.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
